#!/usr/bin/env bash
# =============================================================================
# Cria no cluster os Secrets da stack de observabilidade a partir do arquivo
# local .env.observability (NUNCA versionado). Rode ANTES de ativar o GitOps e
# sempre que trocar alguma chave:
#   bash scripts/create-observability-secrets.sh
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")/.."
ENV_FILE=".env.observability"
[ -f "$ENV_FILE" ] || { echo "Crie o $ENV_FILE a partir do .env.observability.example"; exit 1; }
set -a; . "./$ENV_FILE"; set +a

NS=observability
# Valores provisórios permitem subir a stack antes de ter todas as contas criadas
NEW_RELIC_LICENSE_KEY="${NEW_RELIC_LICENSE_KEY:-pendente}"
PAGERDUTY_INTEGRATION_KEY="${PAGERDUTY_INTEGRATION_KEY:-pendente-configure-o-pagerduty}"
DISCORD_WEBHOOK_URL="${DISCORD_WEBHOOK_URL:-https://discord.com/api/webhooks/placeholder}"
: "${GRAFANA_ADMIN_PASSWORD:?Defina GRAFANA_ADMIN_PASSWORD no $ENV_FILE}"
if [ -z "${SELF_HEALING_TOKEN:-}" ]; then
  SELF_HEALING_TOKEN=$(openssl rand -hex 24)
  echo "SELF_HEALING_TOKEN=$SELF_HEALING_TOKEN" >> "$ENV_FILE"   # reaproveita nas próximas vezes
fi

apply_secret() {  # $1=nome  demais = --from-literal=...
  local name=$1; shift
  kubectl create secret generic "$name" -n "$NS" "$@" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  echo "   secret/$name ok"
}

kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
echo ">> Criando secrets no namespace $NS"
apply_secret grafana-admin \
  --from-literal=admin-user=admin --from-literal=admin-password="$GRAFANA_ADMIN_PASSWORD"
apply_secret grafana-alerting \
  --from-literal=PAGERDUTY_INTEGRATION_KEY="$PAGERDUTY_INTEGRATION_KEY" \
  --from-literal=DISCORD_WEBHOOK_URL="$DISCORD_WEBHOOK_URL" \
  --from-literal=SELF_HEALING_TOKEN="$SELF_HEALING_TOKEN"
apply_secret newrelic-license --from-literal=license-key="$NEW_RELIC_LICENSE_KEY"
apply_secret self-healing \
  --from-literal=webhook-token="$SELF_HEALING_TOKEN" --from-literal=discord-webhook-url="$DISCORD_WEBHOOK_URL"

# Se a stack já estiver rodando, reinicia para carregar os valores novos
for d in deployment/kube-prometheus-stack-grafana daemonset/otel-collector-opentelemetry-collector-agent deployment/self-healing; do
  kubectl rollout restart "$d" -n "$NS" >/dev/null 2>&1 && echo ">> reiniciado: $d" || true
done
echo ">> Pronto."
