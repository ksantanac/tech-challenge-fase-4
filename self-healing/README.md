# self-healing (Runbook Automation)

Serviço que recebe o **webhook de alertas do Grafana** e executa a ação corretiva
automaticamente — um `kubectl rollout restart deployment/<serviço>` feito via API do
Kubernetes — sem intervenção humana.

## Fluxo
1. O alerta do Grafana dispara (ex.: taxa de 5xx do `auth-service` > 5%).
2. O contact point envia o webhook para `POST /webhook/grafana` (com `Authorization: Bearer <token>`).
3. Se o alerta está **firing** e tem os labels `remediation=rollout-restart` e
   `deployment=<nome>`, o serviço faz o PATCH `kubectl.kubernetes.io/restartedAt` no Deployment.
4. Acompanha o rollout e notifica o Discord no início e no fim da recuperação.

## Proteções
- Lista de deployments permitidos (`ALLOWED_DEPLOYMENTS`)
- Cooldown por deployment (`COOLDOWN_SECONDS`, padrão 300s) para evitar loop de restarts
- Token no webhook (`WEBHOOK_TOKEN`)
- RBAC mínimo: a ServiceAccount só pode `get`/`patch` deployments do namespace `togglemaster`

## Endpoints
| Método | Rota | Descrição |
|---|---|---|
| POST | `/webhook/grafana` | recebe os alertas do Grafana |
| GET | `/history` | últimas ações executadas |
| GET | `/health` | health check |
