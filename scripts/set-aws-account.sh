#!/usr/bin/env bash
# =============================================================================
# O AWS Academy pode trocar o Account ID entre sessões. Este script substitui o
# ID antigo pelo da conta atual em todos os arquivos que o referenciam
# (manifests GitOps, workflows do CI e backend do Terraform).
#   bash scripts/set-aws-account.sh
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")/.."
NEW=$(aws sts get-caller-identity --query Account --output text)
OLD=$(grep -ohE '[0-9]{12}\.dkr\.ecr' .github/workflows/_ci-go.yml | head -1 | cut -d. -f1)
if [ "$OLD" = "$NEW" ]; then echo ">> Conta já é $NEW — nada a fazer."; exit 0; fi
echo ">> Trocando $OLD -> $NEW"
grep -rlE "$OLD" gitops .github terraform/versions.tf | xargs sed -i "s/$OLD/$NEW/g"
grep -rlE "$NEW" gitops .github terraform/versions.tf | sed 's/^/   alterado: /'
