"""
Instrumentação OpenTelemetry do serviço: traces, métricas e correlação de logs.

Os dados saem via OTLP para o OTel Collector, que os roteia para o Prometheus
(métricas), o Loki (logs) e o New Relic (APM). A configuração usa as variáveis
padrão do OTel: OTEL_SERVICE_NAME, OTEL_RESOURCE_ATTRIBUTES e
OTEL_EXPORTER_OTLP_ENDPOINT. Com OTEL_SDK_DISABLED=true tudo fica desligado.
"""
import logging
import os

LOG_FORMAT = ("%(asctime)s level=%(levelname)s logger=%(name)s "
              "trace_id=%(otelTraceID)s span_id=%(otelSpanID)s msg=%(message)s")
PLAIN_LOG_FORMAT = "%(asctime)s level=%(levelname)s logger=%(name)s msg=%(message)s"


def _enabled():
    return os.getenv("OTEL_SDK_DISABLED", "false").lower() != "true"


def setup_telemetry():
    """Inicializa os providers de trace/métrica e instrumenta as bibliotecas.

    Deve ser chamado ANTES de criar conexões (banco, HTTP, AWS) para que elas
    já nasçam instrumentadas.
    """
    if not _enabled():
        logging.basicConfig(level=logging.INFO, format=PLAIN_LOG_FORMAT)
        logging.getLogger(__name__).info("OpenTelemetry desabilitado (OTEL_SDK_DISABLED=true)")
        return

    from opentelemetry import metrics, trace
    from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.logging import LoggingInstrumentor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    # service.name + atributos do Kubernetes (pod, namespace) vindos das variáveis de ambiente
    resource = Resource.create()

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(tracer_provider)

    reader = PeriodicExportingMetricReader(OTLPMetricExporter(), export_interval_millis=15000)
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[reader]))

    # Coloca trace_id/span_id em toda linha de log (do log no Loki para o trace no APM)
    # (os logs em si saem pelo stdout e o Collector os envia ao Loki — sem duplicar via OTLP)
    LoggingInstrumentor().instrument(set_logging_format=False, inject_trace_context=True,
                                     enable_log_auto_instrumentation=False)
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)

    _instrument_libraries()
    logging.getLogger(__name__).info("OpenTelemetry inicializado (traces + métricas via OTLP)")


def _instrument_libraries():
    """Instrumenta automaticamente as bibliotecas usadas pelo serviço (se instaladas)."""
    try:
        from opentelemetry.instrumentation.requests import RequestsInstrumentor
        RequestsInstrumentor().instrument()  # propaga o trace nas chamadas HTTP
    except ImportError:
        pass
    try:
        from opentelemetry.instrumentation.psycopg2 import Psycopg2Instrumentor
        Psycopg2Instrumentor().instrument()  # span para cada query no PostgreSQL
    except ImportError:
        pass
    try:
        from opentelemetry.instrumentation.botocore import BotocoreInstrumentor
        BotocoreInstrumentor().instrument()  # spans das chamadas AWS (SQS, DynamoDB)
    except ImportError:
        pass


def instrument_flask(app):
    """Cria um span + métricas HTTP para cada requisição (o /health fica de fora)."""
    if not _enabled():
        return
    from opentelemetry.instrumentation.flask import FlaskInstrumentor
    FlaskInstrumentor().instrument_app(app, excluded_urls="health")


def register_access_log(app, log):
    """Uma linha de log por requisição, com status e trace_id (via formato do log)."""
    from flask import request

    @app.after_request
    def _access_log(response):
        if request.path != "/health":
            level = logging.ERROR if response.status_code >= 500 else logging.INFO
            log.log(level, "method=%s path=%s status=%s", request.method, request.path, response.status_code)
        return response
