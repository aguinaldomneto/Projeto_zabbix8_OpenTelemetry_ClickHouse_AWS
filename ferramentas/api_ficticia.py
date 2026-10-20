"""API fictícia reutilizável para o laboratório (tráfego de teste).

Um único arquivo serve às 5 APIs fictícias (pedidos, pagamentos, catalogo,
streaming, autenticacao); o que muda é a variável de ambiente API_NOME e
algumas regras específicas de negócio ligadas a esse nome. Cada requisição:

  - sorteia o status (95% 200 / 3% 404 / 2% 500, por padrão);
  - varia o tamanho do payload de resposta e a latência (log-normal, com
    cauda longa ocasional);
  - vira um span OTel (servidor), exportado por OTLP para o zabbix-proxy.

"pedidos" publica no Redpanda quando fecha uma compra; "pagamentos" consome
esse tópico e confirma de volta por webhook HTTP em "pedidos" — cobre Kafka
e webhook no mesmo fluxo de "compra". "streaming" registra um histograma de
buffering simulado. Tudo usa só a biblioteca padrão + OpenTelemetry +
confluent-kafka (cliente leve sobre librdkafka).
"""

import json
import logging
import os
import random
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import SpanKind, Status, StatusCode

NOME = os.environ.get("API_NOME", "api-ficticia")
PORTA = int(os.environ.get("API_PORT", "8080"))
ENDPOINT = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")

TAXA_404 = float(os.environ.get("API_TAXA_404", "0.03"))
TAXA_500 = float(os.environ.get("API_TAXA_500", "0.02"))
PAYLOAD_MIN = int(os.environ.get("API_PAYLOAD_MIN", "200"))
PAYLOAD_MAX = int(os.environ.get("API_PAYLOAD_MAX", "65536"))
LATENCIA_MEDIANA_MS = float(os.environ.get("API_LATENCIA_MEDIANA_MS", "60"))
LATENCIA_SIGMA = float(os.environ.get("API_LATENCIA_SIGMA", "0.7"))
TAXA_ABANDONO = float(os.environ.get("API_TAXA_ABANDONO", "0.15"))

KAFKA_BROKERS = os.environ.get("KAFKA_BROKERS", "")
KAFKA_TOPICO_PEDIDO = os.environ.get("KAFKA_TOPICO_PEDIDO", "pedido-criado")
WEBHOOK_PEDIDOS_URL = os.environ.get("WEBHOOK_PEDIDOS_URL", "")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(NOME)

rodando = True
produtor_kafka = None
TRACER = None
CONTADOR_REQ = DURACAO = CONTADOR_FATURAMENTO = CONTADOR_ABANDONO = HIST_BUFFERING = None


def configurar():
    recurso = Resource.create(
        {
            "service.name": NOME,
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
    log.addHandler(LoggingHandler(level=logging.INFO, logger_provider=provedor_logs))

    return provedor_traces, provedor_metricas, provedor_logs


def sortear_latencia_s():
    mediana_s = LATENCIA_MEDIANA_MS / 1000
    atraso = random.lognormvariate(0, LATENCIA_SIGMA) * mediana_s
    return min(atraso, 5.0)


def sortear_status():
    r = random.random()
    if r < TAXA_500:
        return 500
    if r < TAXA_500 + TAXA_404:
        return 404
    return 200


def payload_aleatorio():
    tamanho = random.randint(PAYLOAD_MIN, PAYLOAD_MAX)
    return os.urandom(tamanho)


def confirmar_webhook(pedido_id, resultado):
    with TRACER.start_as_current_span("webhook confirmar pagamento", kind=SpanKind.CLIENT) as span:
        span.set_attribute("http.request.method", "POST")
        span.set_attribute("url.full", WEBHOOK_PEDIDOS_URL)
        span.set_attribute("pedido.id", pedido_id)
        corpo = json.dumps({"pedido_id": pedido_id, "resultado": resultado}).encode()
        requisicao = urllib.request.Request(
            WEBHOOK_PEDIDOS_URL, data=corpo, headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with urllib.request.urlopen(requisicao, timeout=5) as resposta:
                span.set_attribute("http.response.status_code", resposta.status)
        except (urllib.error.URLError, OSError) as erro:
            span.record_exception(erro)
            span.set_status(Status(StatusCode.ERROR, str(erro)))
            log.warning("falha ao confirmar webhook do pedido %s: %s", pedido_id, erro)


def consumir_pedidos(contador_processados):
    from confluent_kafka import Consumer

    consumidor = Consumer(
        {
            "bootstrap.servers": KAFKA_BROKERS,
            "group.id": "pagamentos",
            "auto.offset.reset": "earliest",
        }
    )
    consumidor.subscribe([KAFKA_TOPICO_PEDIDO])
    log.info("consumindo o tópico '%s' em %s", KAFKA_TOPICO_PEDIDO, KAFKA_BROKERS)

    try:
        while rodando:
            msg = consumidor.poll(1.0)
            if msg is None or msg.error():
                continue

            with TRACER.start_as_current_span("processar pagamento", kind=SpanKind.CONSUMER) as span:
                span.set_attribute("messaging.system", "kafka")
                span.set_attribute("messaging.destination.name", KAFKA_TOPICO_PEDIDO)
                try:
                    dados = json.loads(msg.value())
                except (json.JSONDecodeError, TypeError):
                    continue

                pedido_id = dados.get("pedido_id", "")
                span.set_attribute("pedido.id", pedido_id)
                time.sleep(sortear_latencia_s())
                sucesso = random.random() > TAXA_500
                resultado = "aprovado" if sucesso else "recusado"
                span.set_attribute("pagamento.resultado", resultado)
                contador_processados.add(1, {"resultado": resultado})

                if WEBHOOK_PEDIDOS_URL:
                    confirmar_webhook(pedido_id, resultado)
    finally:
        consumidor.close()


class ManipuladorHTTP(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # evita duplicar log (já mandamos via OTel)
        pass

    def _tratar(self):
        inicio = time.monotonic()
        with TRACER.start_as_current_span(f"{self.command} {self.path}", kind=SpanKind.SERVER) as span:
            span.set_attribute("http.request.method", self.command)
            span.set_attribute("url.path", self.path)

            if self.path == "/confirmar" and self.command == "POST":
                self._tratar_confirmacao(span)
                return

            time.sleep(sortear_latencia_s())
            status = sortear_status()

            if NOME == "pedidos" and status == 200:
                status = self._tratar_compra(span)

            if NOME == "streaming":
                buffering = max(0.0, random.lognormvariate(-1.5, 1.0))
                HIST_BUFFERING.record(buffering)
                span.set_attribute("negocio.buffering_s", round(buffering, 3))

            corpo = payload_aleatorio()
            span.set_attribute("http.response.status_code", status)
            span.set_attribute("http.response.body.size", len(corpo))
            if status >= 500:
                span.set_status(Status(StatusCode.ERROR, "erro simulado"))
                log.error("erro simulado em %s %s", self.command, self.path)

            CONTADOR_REQ.add(1, {"status": str(status)})
            DURACAO.record(time.monotonic() - inicio, {"status": str(status)})

            self.send_response(status)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(corpo)))
            self.end_headers()
            self.wfile.write(corpo)

    def _tratar_compra(self, span):
        if random.random() < TAXA_ABANDONO:
            CONTADOR_ABANDONO.add(1)
            span.set_attribute("negocio.evento", "carrinho_abandonado")
            return 200

        valor = round(random.lognormvariate(5.0, 0.6), 2)
        CONTADOR_FATURAMENTO.add(valor)
        span.set_attribute("negocio.evento", "compra_concluida")
        span.set_attribute("negocio.valor", valor)

        if produtor_kafka is not None:
            pedido_id = f"{int(time.time() * 1000)}-{random.randint(0, 9999)}"
            produtor_kafka.produce(KAFKA_TOPICO_PEDIDO, json.dumps({"pedido_id": pedido_id, "valor": valor}).encode())
            produtor_kafka.poll(0)
            span.set_attribute("pedido.id", pedido_id)

        return 200

    def _tratar_confirmacao(self, span):
        tamanho = int(self.headers.get("Content-Length", 0) or 0)
        corpo = self.rfile.read(tamanho) if tamanho else b"{}"
        try:
            dados = json.loads(corpo)
        except json.JSONDecodeError:
            dados = {}

        span.set_attribute("pedido.id", dados.get("pedido_id", ""))
        span.set_attribute("pagamento.resultado", dados.get("resultado", ""))
        log.info("pagamento confirmado: pedido %s -> %s", dados.get("pedido_id"), dados.get("resultado"))

        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = _tratar
    do_POST = _tratar


def main():
    global rodando, produtor_kafka, TRACER, CONTADOR_REQ, DURACAO, CONTADOR_FATURAMENTO, CONTADOR_ABANDONO, HIST_BUFFERING

    provedores = configurar()
    TRACER = trace.get_tracer(NOME, "0.1.0")
    meter = metrics.get_meter(NOME, "0.1.0")

    CONTADOR_REQ = meter.create_counter("http.requisicoes", unit="{req}", description="Requisições atendidas")
    DURACAO = meter.create_histogram("http.duracao", unit="s", description="Duração da requisição")
    CONTADOR_FATURAMENTO = meter.create_counter("negocio.faturamento", unit="BRL", description="Faturamento simulado")
    CONTADOR_ABANDONO = meter.create_counter(
        "negocio.carrinho_abandonado", unit="{carrinho}", description="Carrinhos abandonados"
    )
    HIST_BUFFERING = meter.create_histogram(
        "negocio.buffering_s", unit="s", description="Tempo de buffering simulado"
    )

    if KAFKA_BROKERS and NOME == "pedidos":
        from confluent_kafka import Producer

        produtor_kafka = Producer({"bootstrap.servers": KAFKA_BROKERS})
        log.info("produzindo no tópico '%s' em %s", KAFKA_TOPICO_PEDIDO, KAFKA_BROKERS)

    if KAFKA_BROKERS and NOME == "pagamentos":
        contador_processados = meter.create_counter("negocio.pagamentos_processados", unit="{pagamento}")
        threading.Thread(target=consumir_pedidos, args=(contador_processados,), daemon=True).start()

    servidor = ThreadingHTTPServer(("0.0.0.0", PORTA), ManipuladorHTTP)
    log.info("API '%s' escutando em :%d, enviando telemetria para %s", NOME, PORTA, ENDPOINT)

    def parar(*_):
        global rodando
        rodando = False
        threading.Thread(target=servidor.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, parar)
    signal.signal(signal.SIGINT, parar)

    try:
        servidor.serve_forever()
    finally:
        for provedor in provedores:
            try:
                provedor.shutdown()
            except Exception:
                pass


if __name__ == "__main__":
    main()
