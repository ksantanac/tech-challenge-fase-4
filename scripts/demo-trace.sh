#!/usr/bin/env bash
# =============================================================================
# Faz uma requisição de teste no evaluation-service que PERCORRE OS 5 SERVIÇOS
# (usa uma flag nova, então não há cache) e mostra o trace_id para buscar no APM.
#   bash scripts/demo-trace.sh
# =============================================================================
set -euo pipefail
NS=togglemaster
KEY=$(kubectl get secret togglemaster-secrets -n "$NS" -o jsonpath='{.data.SERVICE_API_KEY}' | base64 -d)
FLAG="demo-trace-$(date +%H%M%S)"

echo ">> Criando a flag '$FLAG' (com regra de 30%) e avaliando para o usuário 'professor-fiap'..."
kubectl run demo-trace -n "$NS" --rm -i --restart=Never --quiet --image=curlimages/curl:8.11.1 -- sh -c "
  curl -s -o /dev/null -X POST http://flag-service:8002/flags -H 'Authorization: Bearer $KEY' \
       -H 'Content-Type: application/json' -d '{\"name\": \"$FLAG\", \"is_enabled\": true}'
  curl -s -o /dev/null -X POST http://targeting-service:8003/rules -H 'Authorization: Bearer $KEY' \
       -H 'Content-Type: application/json' -d '{\"flag_name\": \"$FLAG\", \"rules\": {\"type\": \"PERCENTAGE\", \"value\": 30}}'
  echo; echo '>> Resposta do evaluation-service:'
  curl -s -D /tmp/h 'http://evaluation-service:8004/evaluate?user_id=professor-fiap&flag_name=$FLAG'
  echo; echo
  grep -i 'x-trace-id' /tmp/h | sed 's/^/>> /'
"
echo ">> Copie o trace id acima e cole na busca do New Relic (Distributed tracing)."
