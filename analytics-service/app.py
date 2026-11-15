import os
import sys
import threading
import json
import uuid
import time
import logging
import boto3
from botocore.exceptions import NoCredentialsError, ClientError
from flask import Flask, jsonify
from dotenv import load_dotenv
from opentelemetry import metrics, propagate, trace
from opentelemetry.instrumentation.utils import suppress_instrumentation
import telemetry

# Observabilidade (OpenTelemetry) + logging com trace_id.
# Fica antes dos clientes Boto3 para as chamadas AWS já nascerem instrumentadas.
telemetry.setup_telemetry()
log = logging.getLogger(__name__)
tracer = trace.get_tracer("analytics-service")
events_counter = metrics.get_meter("analytics-service").create_counter(
    "togglemaster.analytics.events", description="Eventos de avaliação processados (status=ok|error)")

# Carrega .env para desenvolvimento local
load_dotenv()

# --- Configuração ---
AWS_REGION = os.getenv("AWS_REGION")
SQS_QUEUE_URL = os.getenv("AWS_SQS_URL")
DYNAMODB_TABLE_NAME = os.getenv("AWS_DYNAMODB_TABLE")

if not all([AWS_REGION, SQS_QUEUE_URL, DYNAMODB_TABLE_NAME]):
    log.critical("Erro: AWS_REGION, AWS_SQS_URL, e AWS_DYNAMODB_TABLE devem ser definidos.")
    sys.exit(1)

# --- Clientes Boto3 ---
# Criamos a sessão uma vez
try:
    session = boto3.Session(region_name=AWS_REGION)
    # Endpoints opcionais para apontar para emuladores locais (ex: DynamoDB Local) em desenvolvimento
    sqs_client = session.client("sqs", endpoint_url=os.getenv("AWS_SQS_ENDPOINT_URL") or None)
    dynamodb_client = session.client("dynamodb", endpoint_url=os.getenv("AWS_DYNAMODB_ENDPOINT_URL") or None)
    log.info(f"Clientes Boto3 inicializados na região {AWS_REGION}")
except NoCredentialsError:
    log.critical("Credenciais da AWS não encontradas. Verifique seu ambiente.")
    sys.exit(1)
except Exception as e:
    log.critical(f"Erro ao inicializar o Boto3: {e}")
    sys.exit(1)


# --- SQS Worker ---

def process_message(message):
    """ Processa uma única mensagem SQS e a insere no DynamoDB.

    O evaluation-service manda o contexto do trace (traceparent) nos MessageAttributes.
    Extraímos esse contexto para que o processamento continue o MESMO trace distribuído.
    """
    carrier = {
        name: attr.get("StringValue")
        for name, attr in message.get("MessageAttributes", {}).items()
        if attr.get("DataType") == "String"
    }
    parent_ctx = propagate.extract(carrier)
    queue_name = SQS_QUEUE_URL.rsplit("/", 1)[-1]
    with tracer.start_as_current_span(
        f"{queue_name} process",
        context=parent_ctx,
        kind=trace.SpanKind.CONSUMER,
        attributes={
            "messaging.system": "aws_sqs",
            "messaging.operation": "process",
            "messaging.destination.name": queue_name,
            "messaging.message.id": message["MessageId"],
        },
    ) as span:
        ok = _process_message(message)
        if not ok:
            span.set_status(trace.Status(trace.StatusCode.ERROR, "falha ao processar a mensagem"))
        events_counter.add(1, {"status": "ok" if ok else "error"})


def _process_message(message):
    """ Lógica original: salva o evento no DynamoDB e remove a mensagem da fila """
    try:
        log.info(f"Processando mensagem ID: {message['MessageId']}")
        body = json.loads(message['Body'])
        
        # Gera um ID único para o item no DynamoDB
        event_id = str(uuid.uuid4())
        
        # Constrói o item no formato do DynamoDB
        item = {
            'event_id': {'S': event_id},
            'user_id': {'S': body['user_id']},
            'flag_name': {'S': body['flag_name']},
            'result': {'BOOL': body['result']},
            'timestamp': {'S': body['timestamp']}
        }
        
        # Insere no DynamoDB
        dynamodb_client.put_item(
            TableName=DYNAMODB_TABLE_NAME,
            Item=item
        )
        
        log.info(f"Evento {event_id} (Flag: {body['flag_name']}) salvo no DynamoDB.")
        
        # Se tudo deu certo, deleta a mensagem da fila
        sqs_client.delete_message(
            QueueUrl=SQS_QUEUE_URL,
            ReceiptHandle=message['ReceiptHandle']
        )
        return True

    except json.JSONDecodeError:
        log.error(f"Erro ao decodificar JSON da mensagem ID: {message['MessageId']}")
        # Não deleta a mensagem, pode ser uma "poison pill"
    except ClientError as e:
        log.error(f"Erro do Boto3 (DynamoDB ou SQS) ao processar {message['MessageId']}: {e}")
        # Não deleta a mensagem, tenta novamente
    except Exception as e:
        log.error(f"Erro inesperado ao processar {message['MessageId']}: {e}")
        # Não deleta a mensagem, tenta novamente
    return False


def sqs_worker_loop():
    """ Loop principal do worker que ouve a fila SQS """
    log.info("Iniciando o worker SQS...")
    while True:
        try:
            # Long-polling: espera até 20s por mensagens. O polling em si não gera
            # span (seria ruído de 20s); o que importa é o processamento de cada evento.
            with suppress_instrumentation():
                response = sqs_client.receive_message(
                    QueueUrl=SQS_QUEUE_URL,
                    MaxNumberOfMessages=10,  # Processa em lotes de até 10
                    WaitTimeSeconds=20,
                    MessageAttributeNames=['All']  # traz o traceparent
                )
            
            messages = response.get('Messages', [])
            if not messages:
                # Nenhuma mensagem, continua o loop
                continue
                
            log.info(f"Recebidas {len(messages)} mensagens.")
            
            for message in messages:
                process_message(message)
                
        except ClientError as e:
            log.error(f"Erro do Boto3 no loop principal do SQS: {e}")
            time.sleep(10) # Pausa antes de tentar novamente
        except Exception as e:
            log.error(f"Erro inesperado no loop principal do SQS: {e}")
            time.sleep(10)

# --- Servidor Flask (Apenas para Health Check) ---

app = Flask(__name__)
telemetry.instrument_flask(app)

@app.route('/health')
def health():
    # Uma verificação de saúde real poderia checar a conexão com o DynamoDB/SQS
    return jsonify({"status": "ok"})

# --- Inicialização ---

def start_worker():
    """ Inicia o worker SQS em uma thread separada """
    worker_thread = threading.Thread(target=sqs_worker_loop, daemon=True)
    worker_thread.start()

# Inicia o worker SQS em uma thread de background
# Isso garante que ele inicie tanto com 'flask run' quanto com 'gunicorn'
start_worker()

if __name__ == '__main__':
    port = int(os.getenv("PORT", 8005))
    app.run(host='0.0.0.0', port=port, debug=False)