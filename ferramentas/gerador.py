"""Gerador de telemetria de teste para o laboratório Zabbix 8 + OpenTelemetry.

Simula um coletor que, a cada ciclo, "baixa" e "processa" um arquivo por UF e
envia os três sinais do OpenTelemetry por OTLP/gRPC:

  traces   - um trace por ciclo, com spans filhos por UF (alguns com erro)
  métricas - contador de coletas, histograma de duração e tamanho de fila
  logs     - uma linha por ciclo e avisos/erros nas falhas simuladas

Nada aqui acessa a internet: os tempos e erros são sorteados. Este arquivo
existe só para validar o caminho aplicação -> proxy -> ClickHouse -> frontend.
"""

import logging
import os
import random
import signal
import sys
import time

from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.metrics import CallbackOptions, Observation
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import SpanKind, Status, StatusCode

ENDPOINT = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://zabbix-proxy:4317")
SERVICO = os.environ.get("OTEL_SERVICE_NAME", "gerador-demo")
INTERVALO_S = float(os.environ.get("GERADOR_INTERVALO_S", "5"))
TAXA_ERRO = float(os.environ.get("GERADOR_TAXA_ERRO", "0.08"))
UFS = ["sc", "sp", "rj", "mg", "rs", "pr", "ba", "pe", "df", "am"]

# Saída no console (docker compose logs gerador)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("gerador")

fila_simulada = 0


def configurar():
    recurso = Resource.create(
        {
            "service.name": SERVICO,
            "service.version": "0.1.0",
            "deployment.environment.name": "lab",
        }
    )

    provedor_traces = TracerProvider(resource=recurso)
    provedor_traces.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=ENDPOINT, insecure=True))
    )
    trace.set_tracer_provider(provedor_traces)

    leitor = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=ENDPOINT, insecure=True),
        export_interval_millis=15000,
    )
    provedor_metricas = MeterProvider(resource=recurso, metric_readers=[leitor])
    metrics.set_meter_provider(provedor_metricas)

    provedor_logs = LoggerProvider(resource=recurso)
    provedor_logs.add_log_record_processor(
        BatchLogRecordProcessor(OTLPLogExporter(endpoint=ENDPOINT, insecure=True))
    )
    set_logger_provider(provedor_logs)
    # O handler OTLP fica só no logger da aplicação. Se ficasse no logger raiz,
    # os avisos de falha de envio do próprio SDK voltariam para a fila de envio.
    log.addHandler(LoggingHandler(level=logging.INFO, logger_provider=provedor_logs))

    return provedor_traces, provedor_metricas, provedor_logs


def observar_fila(_: CallbackOptions):
    yield Observation(fila_simulada, {"fila": "arquivos"})


def ciclo(tracer, contador, duracao):
    global fila_simulada

    inicio_ciclo = time.monotonic()
    falhas = 0

    with tracer.start_as_current_span("ciclo de coleta", kind=SpanKind.SERVER) as raiz:
        ufs = random.sample(UFS, k=random.randint(3, 6))
        raiz.set_attribute("coleta.ufs", len(ufs))
        fila_simulada = len(ufs)

        for uf in ufs:
            inicio = time.monotonic()
            atributos = {"uf": uf}

            with tracer.start_as_current_span("baixar arquivo", kind=SpanKind.CLIENT) as span:
                span.set_attribute("uf", uf)
                span.set_attribute("http.request.method", "GET")
                span.set_attribute("url.path", f"/demo/{uf}/resultado.json")
                time.sleep(random.uniform(0.02, 0.25))

                if random.random() < TAXA_ERRO:
                    falhas += 1
                    erro = TimeoutError(f"tempo esgotado ao baixar o arquivo de {uf}")
                    span.record_exception(erro)
                    span.set_status(Status(StatusCode.ERROR, str(erro)))
                    span.set_attribute("http.response.status_code", 504)
                    log.error("falha ao baixar o arquivo de %s: %s", uf, erro)
                    contador.add(1, {**atributos, "resultado": "erro"})
                    duracao.record(time.monotonic() - inicio, atributos)
                    fila_simulada -= 1
                    continue

                span.set_attribute("http.response.status_code", 200)
                span.add_event("arquivo recebido", {"bytes": random.randint(2000, 90000)})

            with tracer.start_as_current_span("processar arquivo", kind=SpanKind.INTERNAL) as span:
                span.set_attribute("uf", uf)
                time.sleep(random.uniform(0.005, 0.04))

            contador.add(1, {**atributos, "resultado": "ok"})
            duracao.record(time.monotonic() - inicio, atributos)
            fila_simulada -= 1

        if falhas:
            raiz.set_status(Status(StatusCode.ERROR, f"{falhas} arquivo(s) com falha"))
            log.warning("ciclo concluído com %d falha(s) em %d UFs", falhas, len(ufs))
        else:
            log.info("ciclo concluído: %d UFs em %.2fs", len(ufs), time.monotonic() - inicio_ciclo)


def main():
    provedores = configurar()

    tracer = trace.get_tracer("gerador-demo", "0.1.0")
    meter = metrics.get_meter("gerador-demo", "0.1.0")
    contador = meter.create_counter("demo.coletas", unit="{coleta}", description="Arquivos coletados")
    duracao = meter.create_histogram("demo.coleta.duracao", unit="s", description="Duração da coleta por UF")
    meter.create_observable_gauge(
        "demo.fila.tamanho", callbacks=[observar_fila], unit="{arquivo}", description="Arquivos na fila"
    )

    rodando = True

    def parar(*_):
        nonlocal rodando
        rodando = False

    signal.signal(signal.SIGTERM, parar)
    signal.signal(signal.SIGINT, parar)

    log.info("enviando telemetria para %s a cada %.0fs (serviço %s)", ENDPOINT, INTERVALO_S, SERVICO)

    while rodando:
        try:
            ciclo(tracer, contador, duracao)
        except Exception:  # o laço não pode morrer por um erro de simulação
            log.exception("erro inesperado no ciclo")

        fim = time.monotonic() + INTERVALO_S
        while rodando and time.monotonic() < fim:
            time.sleep(0.2)

    log.info("encerrando e enviando o que ficou na fila")
    for provedor in provedores:
        try:
            provedor.shutdown()
        except Exception:
            pass


if __name__ == "__main__":
    main()
