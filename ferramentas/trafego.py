"""Gerador de tráfego HTTP contra as APIs fictícias do laboratório.

Dois modos, mesmo script:

  baseline  - taxa moderada e contínua (serviço "trafego"), o suficiente
              para acumular milhões de registros em alguns dias sem
              sufocar a máquina.
  estresse  - rajada de concorrência alta por tempo limitado (serviço
              one-shot "trafego-estresse", disparado por
              "make gera-carga-de-estresse"). Tem teto máximo no próprio
              código (não só sugerido por variável de ambiente) para não
              derrubar a máquina por engano.

Cada chamada é um span OTel *client*, então o APM do Zabbix mostra a árvore
de dependência completa (trafego -> nginx -> API).
"""

import logging
import os
import random
import signal
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import SpanKind, Status, StatusCode

ENDPOINT = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
SERVICO = os.environ.get("OTEL_SERVICE_NAME", "trafego")
MODO = os.environ.get("TRAFEGO_MODO", "baseline")

# nome=URL,nome=URL,... ; o caminho de cada alvo é só para legibilidade dos
# spans, a api_ficticia.py trata qualquer caminho da mesma forma.
ALVOS_PADRAO = (
    "pedidos=http://lb-01/comprar,"
    "pagamentos=http://lb-01/status,"
    "catalogo=http://lb-02/listar,"
    "streaming=http://lb-02/assistir,"
    "autenticacao=http://lb-02/login"
)

MAX_CONCORRENCIA = 200
MAX_DURACAO_S = 600

REQ_S_BASELINE = float(os.environ.get("TRAFEGO_REQ_S", "3"))
ESTRESSE_CONCORRENCIA = min(int(os.environ.get("ESTRESSE_CONCORRENCIA", "20")), MAX_CONCORRENCIA)
ESTRESSE_DURACAO_S = min(int(os.environ.get("ESTRESSE_DURACAO_S", "60")), MAX_DURACAO_S)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("trafego")

rodando = True


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

    return provedor_traces, provedor_metricas


def ler_alvos():
    bruto = os.environ.get("TRAFEGO_ALVOS", ALVOS_PADRAO)
    alvos = []
    for par in bruto.split(","):
        nome, _, url = par.partition("=")
        if nome and url:
            alvos.append((nome.strip(), url.strip()))
    return alvos


def requisitar(tracer, contador, duracao, nome, url):
    inicio = time.monotonic()
    with tracer.start_as_current_span(f"GET {nome}", kind=SpanKind.CLIENT) as span:
        span.set_attribute("peer.service", nome)
        span.set_attribute("url.full", url)
        span.set_attribute("http.request.method", "GET")
        status = 0
        try:
            with urllib.request.urlopen(url, timeout=10) as resposta:
                status = resposta.status
                corpo = resposta.read()
                span.set_attribute("http.response.body.size", len(corpo))
        except urllib.error.HTTPError as erro:
            status = erro.code
        except (urllib.error.URLError, OSError) as erro:
            span.record_exception(erro)
            span.set_status(Status(StatusCode.ERROR, str(erro)))
            log.warning("falha ao chamar %s (%s): %s", nome, url, erro)

        if status and status >= 500:
            span.set_status(Status(StatusCode.ERROR, f"status {status}"))
        span.set_attribute("http.response.status_code", status)

        atributos = {"alvo": nome, "status": str(status)}
        contador.add(1, atributos)
        duracao.record(time.monotonic() - inicio, atributos)


def modo_baseline(tracer, contador, duracao, alvos):
    intervalo = 1.0 / REQ_S_BASELINE if REQ_S_BASELINE > 0 else 1.0
    log.info("modo baseline: ~%.1f req/s contra %d alvo(s)", REQ_S_BASELINE, len(alvos))

    with ThreadPoolExecutor(max_workers=10) as executor:
        while rodando:
            nome, url = random.choice(alvos)
            executor.submit(requisitar, tracer, contador, duracao, nome, url)
            time.sleep(intervalo)


def modo_estresse(tracer, contador, duracao, alvos):
    fim = time.monotonic() + ESTRESSE_DURACAO_S
    log.info(
        "modo estresse: %d workers por %ds contra %d alvo(s)",
        ESTRESSE_CONCORRENCIA, ESTRESSE_DURACAO_S, len(alvos),
    )

    def trabalhador():
        while rodando and time.monotonic() < fim:
            nome, url = random.choice(alvos)
            requisitar(tracer, contador, duracao, nome, url)
            time.sleep(random.uniform(0.01, 0.1))

    with ThreadPoolExecutor(max_workers=ESTRESSE_CONCORRENCIA) as executor:
        futuros = [executor.submit(trabalhador) for _ in range(ESTRESSE_CONCORRENCIA)]
        for futuro in futuros:
            futuro.result()

    log.info("pico de carga concluído")


def main():
    global rodando

    alvos = ler_alvos()
    if not alvos:
        sys.exit("[erro] TRAFEGO_ALVOS vazio ou mal formado")

    provedores = configurar()
    tracer = trace.get_tracer("trafego", "0.1.0")
    meter = metrics.get_meter("trafego", "0.1.0")
    contador = meter.create_counter("trafego.requisicoes", unit="{req}", description="Chamadas feitas")
    duracao = meter.create_histogram("trafego.duracao", unit="s", description="Duração vista pelo cliente")

    def parar(*_):
        global rodando
        rodando = False

    signal.signal(signal.SIGTERM, parar)
    signal.signal(signal.SIGINT, parar)

    try:
        if MODO == "estresse":
            modo_estresse(tracer, contador, duracao, alvos)
        else:
            modo_baseline(tracer, contador, duracao, alvos)
    finally:
        for provedor in provedores:
            try:
                provedor.shutdown()
            except Exception:
                pass


if __name__ == "__main__":
    main()
