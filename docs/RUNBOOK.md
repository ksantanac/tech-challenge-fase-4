# 📕 Runbook — `auth-service`: taxa de erros 5xx acima de 5%

| | |
|---|---|
| **Alerta** | `auth-service: taxa de erros 5xx acima de 5%` (Grafana, uid `auth-service-5xx-rate`) |
| **Severidade** | `critical` |
| **Condição** | `% de respostas 5xx do auth-service no último minuto > 5` por **1 minuto** |
| **Notifica** | PagerDuty (abre incidente) · Discord `#togglemaster-alertas` · Self-Healing (webhook) |
| **Ação automática** | `kubectl rollout restart deployment/auth-service -n togglemaster` |

---

## 1. O que esse alerta significa
O `auth-service` valida as chaves de API de **todas** as chamadas do `flag-service` e do
`targeting-service`. Se ele responde 5xx, a falha se propaga em **cascata**:

```
evaluation-service ──► flag-service / targeting-service ──► auth-service (/validate)  ❌ 5xx
        502                       401
```

Impacto para o cliente: o `/evaluate` responde **502** e as feature flags param de ser avaliadas.

## 2. O que a automação já fez (Self-Healing)
Ao disparar, o Grafana chama o serviço `self-healing` (namespace `observability`), que:
1. confere o token, o label `remediation=rollout-restart` e se o deployment é permitido;
2. aplica o **rollout restart** (pods novos substituem os antigos, sem downtime);
3. avisa no Discord quando começou e quando os pods novos ficaram prontos;
4. respeita um **cooldown de 5 minutos** por deployment (não reinicia em loop).

Falhas de **estado em memória** (pool de conexões corrompido, memory leak, deadlock) costumam
ser resolvidas só com isso. Em ~2 minutos o alerta deve ir para **Resolved** sozinho.

## 3. Como verificar
```bash
# a automação executou?
kubectl logs -n observability deploy/self-healing --tail=20

# pods novos subiram?
kubectl get pods -n togglemaster -l app=auth-service
kubectl rollout status deployment/auth-service -n togglemaster

# o erro parou? (deve voltar a 200)
kubectl logs -n togglemaster deploy/auth-service --tail=20
```
- **Grafana** → dashboard *ToggleMaster — Saúde do Ecossistema* → painel *auth-service 5xx (%)*.
- **Loki** (Grafana → Explore): `{service_name="auth-service"} |= "level=ERROR"`
- **New Relic** → APM → `auth-service` → *Errors* / *Distributed tracing* (o `trace_id` está em cada linha de log).

## 4. Se o restart NÃO resolveu (escalonamento manual)
O problema não é de estado do pod — é de **dependência ou de código**:

| Sintoma | Causa provável | Ação |
|---|---|---|
| logs com erro de conexão no PostgreSQL | RDS `auth` indisponível / senha | Console AWS → RDS; checar o Secret `togglemaster-secrets` |
| erro começou logo após um deploy | versão nova com bug | **rollback via GitOps**: `git revert` do commit da tag em `gitops/apps/auth-service/` (o ArgoCD aplica sozinho) |
| CPU/memória no limite | carga acima do esperado | aumentar réplicas/limites em `gitops/apps/auth-service/deployment.yaml` |

> Não altere o cluster na mão (`kubectl edit`): o ArgoCD desfaz. Toda correção passa pelo Git.

## 5. Pós-incidente
- Resolver o incidente no PagerDuty (o Grafana já envia o *resolve* automaticamente).
- Registrar no post-mortem: linha do tempo (Discord + PagerDuty), causa raiz (trace + logs), ação
  automática, MTTR e ações preventivas.
