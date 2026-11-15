#!/usr/bin/env bash
# =============================================================================
# Injeção de falha (chaos) no auth-service — para provar alerta + self-healing.
#
#   bash scripts/chaos.sh on      # liga a falha em TODOS os pods do auth-service
#   bash scripts/chaos.sh off     # desliga
#   bash scripts/chaos.sh status  # mostra o estado de cada pod
#
# A falha é um estado "corrompido" em memória: o /validate passa a responder 500.
# Por viver só na memória do pod, um rollout restart (self-healing) cura o serviço.
# =============================================================================
set -euo pipefail
NS=togglemaster
ACTION="${1:-status}"

case "$ACTION" in
  on)     BODY='{"enabled": true}' ;;
  off)    BODY='{"enabled": false}' ;;
  status) BODY='' ;;
  *) echo "uso: $0 on|off|status"; exit 1 ;;
esac

for pod in $(kubectl get pods -n "$NS" -l app=auth-service -o jsonpath='{.items[*].metadata.name}'); do
  if [ -n "$BODY" ]; then
    kubectl exec -n "$NS" "$pod" -- sh -c \
      "wget -qO- --header \"Authorization: Bearer \$MASTER_KEY\" --header 'Content-Type: application/json' --post-data '$BODY' http://localhost:8001/admin/chaos"
  else
    kubectl exec -n "$NS" "$pod" -- sh -c \
      "wget -qO- --header \"Authorization: Bearer \$MASTER_KEY\" http://localhost:8001/admin/chaos"
  fi
  echo
done
