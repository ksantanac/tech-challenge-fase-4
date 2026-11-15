"""
Self-Healing (Runbook Automation) do ToggleMaster.

Recebe o webhook de alertas do Grafana e executa a ação corretiva sem intervenção
humana: um "rollout restart" do Deployment afetado. É exatamente o que o comando
`kubectl rollout restart deployment/<nome>` faz: um PATCH na annotation
`kubectl.kubernetes.io/restartedAt` do template dos pods, o que faz o Kubernetes
substituir os pods antigos por pods novos (rolling update, sem downtime).

Proteções (para a automação não virar um problema):
  - só age em alertas FIRING que tenham o label remediation=rollout-restart;
  - só reinicia deployments da lista ALLOWED_DEPLOYMENTS;
  - cooldown por deployment (evita loop de restarts / "alert storm");
  - o webhook exige um token (Authorization: Bearer <WEBHOOK_TOKEN>);
  - a ServiceAccount só tem permissão de get/patch em deployments do namespace alvo.
"""
import hmac
import logging
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, request

logging.basicConfig(level=logging.INFO, format="%(asctime)s level=%(levelname)s logger=self-healing msg=%(message)s")
log = logging.getLogger("self-healing")

TARGET_NAMESPACE = os.getenv("TARGET_NAMESPACE", "togglemaster")
ALLOWED_DEPLOYMENTS = {
    d.strip() for d in os.getenv(
        "ALLOWED_DEPLOYMENTS",
        "auth-service,flag-service,targeting-service,evaluation-service,analytics-service",
    ).split(",") if d.strip()
}
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "300"))
WEBHOOK_TOKEN = os.getenv("WEBHOOK_TOKEN", "")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"  # para testes fora do cluster

K8S_API = "https://kubernetes.default.svc"
SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"

app = Flask(__name__)
_last_action = {}             # deployment -> epoch do último restart
_history = deque(maxlen=50)   # histórico das ações (GET /history)
_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# Kubernetes API (com o token da ServiceAccount do próprio pod)
# --------------------------------------------------------------------------- #
def _k8s(method, path, **kwargs):
    with open(f"{SA_DIR}/token", encoding="utf-8") as f:
        token = f.read().strip()
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {token}"
    resp = requests.request(method, f"{K8S_API}{path}", headers=headers,
                            verify=f"{SA_DIR}/ca.crt", timeout=10, **kwargs)
    resp.raise_for_status()
    return resp.json()


def _deployment_path(name):
    return f"/apis/apps/v1/namespaces/{TARGET_NAMESPACE}/deployments/{name}"


def rollout_restart(deployment):
    """Equivalente ao `kubectl rollout restart deployment/<deployment>`."""
    restarted_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if DRY_RUN:
        log.info("DRY_RUN: rollout restart de deployment/%s seria executado", deployment)
        return restarted_at
    patch = {"spec": {"template": {"metadata": {"annotations": {
        "kubectl.kubernetes.io/restartedAt": restarted_at}}}}}
    _k8s("PATCH", _deployment_path(deployment), json=patch,
         headers={"Content-Type": "application/strategic-merge-patch+json"})
    return restarted_at


def wait_rollout(deployment, alert_name, timeout=300):
    """Acompanha o rollout (como `kubectl rollout status`) e avisa quando terminar."""
    if DRY_RUN:
        return
    start = time.time()
    while time.time() - start < timeout:
        time.sleep(5)
        try:
            dep = _k8s("GET", _deployment_path(deployment))
        except requests.RequestException as e:
            log.warning("erro ao consultar o rollout de %s: %s", deployment, e)
            continue
        spec = dep["spec"].get("replicas", 1)
        st = dep.get("status", {})
        if (st.get("observedGeneration", 0) >= dep["metadata"]["generation"]
                and st.get("updatedReplicas", 0) == spec
                and st.get("availableReplicas", 0) == spec
                and st.get("replicas", 0) == spec):
            secs = int(time.time() - start)
            log.info("rollout concluido: deployment/%s com %d/%d pods novos prontos em %ds",
                     deployment, spec, spec, secs)
            notify_discord(
                title=f"✅ Self-Healing concluído: {deployment} recuperado",
                description=(f"Os pods de `{deployment}` foram substituídos e estão prontos "
                             f"(**{spec}/{spec}**) em {secs}s. Aguardando o alerta "
                             f"**{alert_name}** voltar ao normal."),
                color=0x2ECC71,
            )
            return
    log.error("rollout de deployment/%s nao concluiu em %ds", deployment, timeout)


# --------------------------------------------------------------------------- #
# Notificação no Discord (ChatOps)
# --------------------------------------------------------------------------- #
def notify_discord(title, description, color, fields=None):
    if not DISCORD_WEBHOOK_URL or "placeholder" in DISCORD_WEBHOOK_URL:
        return
    embed = {
        "title": title,
        "description": description,
        "color": color,
        "fields": fields or [],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "ToggleMaster • Runbook Automation"},
    }
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"username": "ToggleMaster Self-Healing", "embeds": [embed]},
                      timeout=5).raise_for_status()
    except requests.RequestException as e:
        log.warning("falha ao notificar o Discord: %s", e)


# --------------------------------------------------------------------------- #
# Webhook do Grafana
# --------------------------------------------------------------------------- #
def _authorized():
    if not WEBHOOK_TOKEN:
        return True
    auth = request.headers.get("Authorization", "")
    return hmac.compare_digest(auth, f"Bearer {WEBHOOK_TOKEN}")


def handle_alert(alert):
    labels = alert.get("labels", {})
    name = labels.get("alertname", "desconhecido")
    deployment = labels.get("deployment", "")
    status = alert.get("status", "")

    if status != "firing":
        log.info("alerta '%s' status=%s -> nada a fazer", name, status)
        return {"alert": name, "action": "none", "reason": f"status={status}"}
    if labels.get("remediation") != "rollout-restart":
        return {"alert": name, "action": "none", "reason": "alerta sem remediation=rollout-restart"}
    if deployment not in ALLOWED_DEPLOYMENTS:
        log.warning("alerta '%s' pediu restart de '%s', que NAO esta na lista permitida", name, deployment)
        return {"alert": name, "action": "refused", "reason": f"deployment '{deployment}' não permitido"}

    with _lock:
        elapsed = time.time() - _last_action.get(deployment, 0)
        if elapsed < COOLDOWN_SECONDS:
            log.info("cooldown ativo para %s (restart ha %ds) -> ignorando", deployment, elapsed)
            return {"alert": name, "action": "skipped", "reason": f"cooldown ({int(elapsed)}s)"}
        _last_action[deployment] = time.time()

    log.warning("ALERTA FIRING '%s' -> executando: kubectl rollout restart deployment/%s -n %s",
                name, deployment, TARGET_NAMESPACE)
    try:
        restarted_at = rollout_restart(deployment)
    except requests.RequestException as e:
        with _lock:
            _last_action.pop(deployment, None)
        log.error("falha ao reiniciar deployment/%s: %s", deployment, e)
        return {"alert": name, "action": "error", "reason": str(e)}

    log.info("rollout restart aplicado em deployment/%s (restartedAt=%s)", deployment, restarted_at)
    _history.appendleft({"time": restarted_at, "alert": name, "deployment": deployment,
                         "action": "rollout-restart"})
    notify_discord(
        title=f"🤖 Self-Healing acionado: {deployment}",
        description=(f"O alerta **{name}** disparou e a automação executou a ação corretiva "
                     f"sem intervenção humana:\n```kubectl rollout restart deployment/{deployment} "
                     f"-n {TARGET_NAMESPACE}```"),
        color=0xF39C12,
        fields=[
            {"name": "Serviço", "value": deployment, "inline": True},
            {"name": "Severidade", "value": labels.get("severity", "-"), "inline": True},
            {"name": "Resumo", "value": alert.get("annotations", {}).get("summary", "-")[:1000]},
        ],
    )
    threading.Thread(target=wait_rollout, args=(deployment, name), daemon=True).start()
    return {"alert": name, "action": "rollout-restart", "deployment": deployment, "restartedAt": restarted_at}


@app.route("/webhook/grafana", methods=["POST"])
def grafana_webhook():
    if not _authorized():
        log.warning("webhook recusado: token invalido")
        return jsonify({"error": "unauthorized"}), 401
    payload = request.get_json(silent=True) or {}
    alerts = payload.get("alerts", [])
    log.info("webhook recebido do Grafana: status=%s, %d alerta(s)", payload.get("status"), len(alerts))
    results = [handle_alert(a) for a in alerts]
    return jsonify({"results": results})


@app.route("/history")
def history():
    return jsonify(list(_history))


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
