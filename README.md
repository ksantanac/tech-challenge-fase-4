# ToggleMaster — Tech Challenge Fase 4 (FIAP POSTECH)

**Observabilidade Total e Resposta Ativa** para o ecossistema de microsserviços **ToggleMaster**:
instrumentação com **OpenTelemetry**, stack open source (**Prometheus, Loki, Grafana**) no
Kubernetes via **GitOps**, **APM com New Relic**, **alertas inteligentes** com **PagerDuty** e
**Discord (ChatOps)** e **Self-Healing** automático.

> Esta fase roda **em cima** de toda a base das fases anteriores: 5 microsserviços conteinerizados,
> infraestrutura com **Terraform**, CI/CD com **DevSecOps** (GitHub Actions) e deploy no **EKS**
> gerenciado por **GitOps (ArgoCD)**.

---

## 📋 Relatório de Entrega

| | |
|---|---|
| **Nome** | Kaue Matheus Santana Alexandre |
| **RM** | 372355 |
| **Discord** | ksantanac_ |

- **Repositório:** https://github.com/ksantanac/tech-challenge-fase-4
- **Vídeo de demonstração:** _(adicionar link do YouTube)_

---

## 🎯 O problema
O `evaluation-service` começou a falhar silenciosamente; o HPA escalou para compensar, o time levou
**6 horas** para ser avisado (pelos usuários) e **mais 4 horas** para achar a causa nos logs
espalhados pelos pods.

**O que foi construído para isso não acontecer de novo:**

| Necessidade | Solução |
|---|---|
| Serviço lento → ver o **trace** | Instrumentação OpenTelemetry + **New Relic APM** (trace distribuído e Service Map) |
| Erro → **logs centralizados** | **Loki** recebendo os logs de todos os contêineres (com `trace_id` em cada linha) |
| Limite atingido → **acordar o time** | Alerta no **Grafana** → incidente no **PagerDuty** + notificação no **Discord** |
| O sistema deve **se curar** | **Self-Healing**: webhook do alerta → `rollout restart` automático |

---

## 🏗️ Arquitetura de observabilidade

```
                         ┌──────────────── Cluster EKS (provisionado com Terraform) ────────────────┐
                         │                                                                          │
  Gerador de tráfego ──► │  evaluation ──► flag ──► auth ◄── targeting        analytics ◄── SQS      │
                         │      │  (5 microsserviços instrumentados com OpenTelemetry SDK)           │
                         │      │  OTLP (traces + métricas)        stdout (logs)                     │
                         │      ▼                                      ▼                             │
                         │  ┌──────────────── OpenTelemetry Collector (DaemonSet) ───────────────┐   │
                         │  │ receivers:  otlp  ·  filelog (/var/log/pods)  ·  prometheus(self)  │   │
                         │  │ processors: memory_limiter · k8sattributes · transform · filter ·  │   │
                         │  │             cumulativetodelta · batch                              │   │
                         │  └──────┬──────────────────┬──────────────────────────┬───────────────┘   │
                         │         │ métricas         │ logs                     │ traces/métricas/logs
                         │         ▼ (remote write)   ▼ (OTLP)                   ▼ (OTLP)            │
                         │    Prometheus            Loki ───────────┐       ┌─────────────────────┐  │
                         │         └──────────┬───────────────────┘ │       │   New Relic (APM)   │──┼──► Service Map
                         │                    ▼                     │       └─────────────────────┘  │    Trace distribuído
                         │                 Grafana  (dashboard + alerta 5xx > 5%)                    │
                         │                    │  contact point "togglemaster-oncall"                 │
                         │       ┌────────────┼──────────────────────┐                               │
                         │       ▼            ▼                      ▼                               │
                         │   PagerDuty     Discord          self-healing ──► kubectl rollout restart │
                         │  (incidente)   (ChatOps)        (Runbook Automation)  deployment/auth-service
                         └──────────────────────────────────────────────────────────────────────────┘
          Tudo acima é declarado no Git e aplicado pelo ArgoCD (App of Apps).
```

### Por que o OTel Collector no centro?
Os microsserviços **não sabem** para onde a telemetria vai: eles só falam **OTLP** com o Collector
do próprio nó (`http://$(HOST_IP):4317`). O Collector é quem decide o destino:

| Sinal | Origem | Pipeline do Collector | Destino |
|---|---|---|---|
| **Métricas** | SDK OTel dos serviços (HTTP, DB, negócio) | `otlp → k8sattributes → batch` | **Prometheus** (remote write) |
| | | `otlp → k8sattributes → cumulativetodelta → batch` | **New Relic** |
| **Logs** | stdout de **todos** os contêineres (`filelog`) | `filelog → k8sattributes → transform → batch` | **Loki** |
| | | `… → filter (só namespace togglemaster)` | **New Relic** |
| **Traces** | SDK OTel dos serviços | `otlp → k8sattributes → batch` | **New Relic APM** |

Vantagens: trocar de APM (ex.: Datadog) é **mudar um exporter** no Collector, sem tocar no código;
o `k8sattributes` adiciona pod/deployment/nó em tudo; o `memory_limiter` protege o nó.

### Instrumentação do código
| Serviço | Linguagem | Instrumentação |
|---|---|---|
| auth-service | Go | `otelhttp` (servidor), `otelsql` (PostgreSQL), log com `trace_id`, **endpoint de chaos** |
| evaluation-service | Go | `otelhttp` (servidor e cliente → propaga o `traceparent`), hook de tracing do Redis, span *producer* no SQS com contexto nos `MessageAttributes`, métricas de negócio (`togglemaster.evaluations`, `togglemaster.cache.requests`) |
| flag-service / targeting-service | Python | `opentelemetry-instrumentation-flask`, `-requests`, `-psycopg2`, correlação de logs |
| analytics-service | Python | `-botocore` (SQS/DynamoDB) + span *consumer* que **continua o trace** vindo pela fila |
| self-healing | Python | logs estruturados (é o "runbook") |

Resultado: **uma requisição no `/evaluate` gera um único trace** que passa pelos 5 serviços:

```
[evaluation-service] GET /evaluate
  ├─ redis GET
  ├─ GET flag-service:8002  ──► [flag-service] GET /flags/<name> ──► [auth-service] GET /validate ──► SELECT
  │                                                └─ SELECT (PostgreSQL)
  ├─ GET targeting-service:8003 ──► [targeting-service] GET /rules/<name> ──► [auth-service] …
  ├─ redis SET
  └─ togglemaster-events publish (SQS) ──► [analytics-service] process ──► DynamoDB.PutItem
```

---

## 🚨 Alerta inteligente + Incidente + ChatOps + Self-Healing

**Regra** (Grafana, como código em `gitops/observability/config/grafana-alerting.yaml`):
```promql
100 * (sum(rate(http_server_request_duration_seconds_count{service_name="auth-service", http_response_status_code=~"5.."}[1m])) or vector(0))
    / sum(rate(http_server_request_duration_seconds_count{service_name="auth-service"}[1m]))   > 5   por 1 min
```

Cuidados contra **alert fatigue**: `for: 1m` (ignora picos isolados), agrupamento por alerta,
`repeat_interval: 4h`, *resolve* automático e **um único contact point** que faz as três coisas:

1. **PagerDuty** (Events API v2) → abre o incidente e aciona o on-call; resolve sozinho na recuperação.
2. **Discord** → mensagem detalhada (serviço, severidade, resumo, ação automática, runbook, link do dashboard).
3. **Webhook → `self-healing`** → executa `kubectl rollout restart deployment/auth-service`
   (PATCH na annotation `kubectl.kubernetes.io/restartedAt` via API do Kubernetes) e acompanha o rollout.

Proteções do self-healing: token no webhook, lista de deployments permitidos, cooldown de 5 min,
**RBAC mínimo** (só `get`/`patch` em deployments do namespace `togglemaster`) e contêiner sem root.

**Como provar (chaos engineering):** `bash scripts/chaos.sh on` coloca o `auth-service` num estado
"corrompido" em memória (o `/validate` responde 500). O alerta dispara, o PagerDuty abre o
incidente, o Discord é notificado e o self-healing reinicia os pods — que voltam saudáveis.

Runbook do alerta: [`docs/RUNBOOK.md`](docs/RUNBOOK.md).

---

## 📊 Dashboard customizado (Grafana)
**ToggleMaster — Saúde do Ecossistema** (`gitops/observability/config/grafana-dashboard-togglemaster.yaml`):
- **SLOs:** req/s, % de erros 5xx, % 5xx do auth-service (o mesmo valor do alerta), latência p95, pods, nós
- **Microsserviços:** taxa de requisições, erros e latência por serviço, status HTTP, métricas de negócio, cache Redis
- **Cluster:** CPU e memória do cluster, por namespace e por pod, pods disponíveis por deployment
- **Logs em tempo real (Loki):** volume por serviço, erros por serviço, stream de logs e filtro de erros
- **OTel Collector:** o que foi recebido e o que foi enviado para cada destino

---

## ⚖️ Justificativas técnicas

**New Relic (vs Datadog).** O New Relic tem *free tier* permanente (100 GB/mês de ingestão, 1 usuário
completo) e recebe **OTLP nativamente** (`otlp.nr-data.net`), então os dados saem do nosso OTel
Collector sem agente proprietário — mantém o OpenTelemetry como padrão e evita *lock-in*. O
Datadog também aceita OTLP, mas o gratuito é um trial de 14 dias e o caminho recomendado usa o
Datadog Agent.

**PagerDuty (vs OpsGenie).** A Atlassian encerrou a venda do OpsGenie e anunciou o fim do suporte
(migração para o Jira Service Management/Compass). O PagerDuty tem plano gratuito, é o padrão de
mercado em on-call e tem integração nativa no Grafana (Events API v2) com *trigger* e *resolve*
automáticos.

**Discord (ChatOps).** Integração nativa no Grafana via webhook, mensagem formatada por template.

**Self-Healing dentro do cluster (vs Lambda / GitHub Action).** As credenciais do AWS Academy
expiram a cada sessão; uma Lambda ou um GitHub Action precisariam de credenciais da AWS e acesso
externo ao API server. O serviço in-cluster usa uma ServiceAccount com permissão mínima, reage em
segundos e o código está versionado e entregue por GitOps como todo o resto.

**Prometheus via remote write.** O Collector *empurra* as métricas das aplicações para o Prometheus
(`enableRemoteWriteReceiver`), enquanto o Prometheus continua fazendo *scrape* da infraestrutura
(kubelet/cAdvisor, node-exporter, kube-state-metrics).

---

## 📁 Estrutura do repositório

```
.
├── terraform/                       # IaC (Fase 3) — nós t3.large + ECR do self-healing (Fase 4)
├── .github/workflows/               # CI/CD + DevSecOps (6 pipelines: 5 serviços + self-healing)
├── gitops/
│   ├── argocd/                      # App of Apps (root) + Applications
│   ├── apps/<serviço>/              # manifests dos microsserviços (com variáveis do OTel)
│   ├── apps/self-healing/           # Deployment + Service + RBAC mínimo
│   ├── apps/traffic-generator/      # simula usuários (~5 req/s)
│   └── observability/
│       ├── values/                  # values dos Helm charts (kube-prometheus-stack, loki, otel-collector)
│       └── config/                  # dashboard + alerting do Grafana (como código)
├── auth-service/ … analytics-service/   # código instrumentado (telemetry.go / telemetry.py)
├── self-healing/                    # serviço de Runbook Automation
├── scripts/
│   ├── bootstrap-config.sh          # ConfigMap/Secret da aplicação a partir do Terraform
│   ├── create-observability-secrets.sh  # chaves New Relic / PagerDuty / Discord (fora do git)
│   ├── db-init.sh                   # tabelas + dados iniciais nos 3 RDS
│   ├── chaos.sh                     # injeção de falha no auth-service
│   ├── initial-image-push.sh / install-argocd.sh
├── docs/RUNBOOK.md                  # runbook do alerta
├── local/otel-collector.yaml        # Collector do ambiente local
└── docker-compose.yml               # ambiente local (com OTel Collector + Jaeger)
```

## 💻 Ambiente local
```bash
docker compose up -d --build
# traces locais no Jaeger: http://localhost:16686
curl "localhost:8004/evaluate?user_id=ana&flag_name=minha-flag"
```

## 🚀 Deploy no EKS (resumo)
```bash
cd terraform && terraform apply                        # infra (EKS t3.large, RDS, Redis, SQS, ECR…)
aws eks update-kubeconfig --name togglemaster-eks --region us-east-1
bash scripts/install-argocd.sh                         # ArgoCD
DB_PASSWORD='…' bash scripts/bootstrap-config.sh       # config + secrets da aplicação
bash scripts/create-observability-secrets.sh           # chaves New Relic / PagerDuty / Discord
bash scripts/initial-image-push.sh                     # imagens iniciais no ECR
kubectl apply -f gitops/argocd/root-app.yaml           # GitOps: o ArgoCD sobe TODO o resto
bash scripts/db-init.sh                                # tabelas + flags de exemplo
```
