#!/usr/bin/env bash
# =============================================================================
# Cria as tabelas nos 3 bancos RDS e carrega os dados iniciais (chave de API do
# evaluation-service + flags/regras de exemplo). Idempotente: pode rodar de novo.
# As URLs dos bancos são lidas do Secret togglemaster-secrets (criado pelo
# scripts/bootstrap-config.sh). Roda um pod temporário com o cliente psql.
#   bash scripts/db-init.sh
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")/.."
NS=togglemaster

db_url() {
  kubectl get secret togglemaster-secrets -n "$NS" -o jsonpath="{.data.$1}" | base64 -d
}

run_sql() {  # $1=nome  $2=URL  $3...=arquivos .sql
  local name=$1 url=$2; shift 2
  echo ">> [$name] aplicando: $*"
  cat "$@" | kubectl run "db-init-$name" -n "$NS" --rm -i --restart=Never --quiet \
    --image=postgres:16-alpine -- psql "$url" -v ON_ERROR_STOP=1 -q -f -
}

run_sql auth      "$(db_url AUTH_DATABASE_URL)"      auth-service/db/init.sql auth-service/db/seed-dev-key.sql
run_sql flags     "$(db_url FLAG_DATABASE_URL)"      flag-service/db/init.sql scripts/sql/seed-flags.sql
run_sql targeting "$(db_url TARGETING_DATABASE_URL)" targeting-service/db/init.sql scripts/sql/seed-rules.sql

echo ">> Bancos prontos (tabelas + chave de API + flags de exemplo)."
