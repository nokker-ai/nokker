#!/usr/bin/env bash
# Chart test on a real cluster (k3d, one node): install, OpenBao init and unseal, core health, one session pod created
# by the core itself (nokker/tools/k8s_sandbox.py) and its network rules, a privileged pod refused by Pod Security
# (namespace enforced at `restricted`), then an upgrade that must not re-initialise anything.
#   ci/k3d-test.sh [core image]        e.g. ci/k3d-test.sh local/nokker:x19-lokkal (imported into the cluster)
# Needs k3d, kubectl and helm on PATH. Leaves the cluster running (k3d cluster delete nokker-ci to remove it).
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd); CHART=$(dirname "$HERE")
CLUSTER=${CLUSTER:-nokker-ci}; NS=${NS:-nokker}; REL=${REL:-t}
CORE_IMAGE=${1:-}
ok()   { printf '\033[32m✓\033[0m %s\n' "$*"; }
fail() { printf '\033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }

k3d cluster list "$CLUSTER" >/dev/null 2>&1 || k3d cluster create "$CLUSTER" --servers 1 --agents 0 --wait --timeout 180s
kubectl config use-context "k3d-$CLUSTER" >/dev/null
kubectl get ns "$NS" >/dev/null 2>&1 || kubectl create ns "$NS"
kubectl label ns "$NS" --overwrite pod-security.kubernetes.io/enforce=restricted \
  pod-security.kubernetes.io/enforce-version=latest >/dev/null

SET=()
if [ -n "$CORE_IMAGE" ]; then
  k3d image import -c "$CLUSTER" "$CORE_IMAGE" >/dev/null
  SET+=(--set "core.image.repository=${CORE_IMAGE%:*}" --set "core.image.tag=${CORE_IMAGE##*:}" \
        --set core.image.pullPolicy=Never)
fi
helm upgrade --install "$REL" "$CHART" -n "$NS" -f "$HERE/k3d-values.yaml" "${SET[@]}" --wait --timeout 10m
FN=$(kubectl -n "$NS" get deploy -l app.kubernetes.io/component=core -o jsonpath='{.items[0].metadata.name}')
FN=${FN%-core}
ok "helm install ($FN)"

kubectl -n "$NS" get secret "$FN-openbao-approle" >/dev/null || fail "AppRole Secret absent"
kubectl -n "$NS" rollout status "deploy/$FN-core" --timeout=5m >/dev/null
kubectl -n "$NS" exec "$FN-openbao-0" -c openbao -- bao status >/dev/null || fail "OpenBao scellé"
ok "OpenBao initialisé, descellé, AppRole stocké"

H=$(kubectl -n "$NS" exec "deploy/$FN-core" -c core -- curl -sf http://127.0.0.1:8742/healthz)
echo "  /healthz $H"
case "$H" in *'"status": "ok"'*) ok "core /healthz";; *) fail "core /healthz : $H";; esac
C=$(kubectl -n "$NS" exec "deploy/$FN-core" -c core -- curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8742/v1/code/sessions)
[ "$C" = 401 ] || fail "/v1/code/sessions sans identité : $C (401 attendu : sessions de code allumées)"
ok "sessions de code allumées (/v1/code/sessions → 401 sans identité)"

# Session pod created BY THE CORE (its service account), then exec, network rules, cleanup.
kubectl -n "$NS" exec -i "deploy/$FN-core" -c core -- python3 - <<'PY' || fail "session pod"
import yaml
from nokker.tools.k8s_sandbox import KubernetesEnvironment, reap_k8s_sandboxes
code = yaml.safe_load(open("/etc/nokker/config.yaml"))["code"]
cfg = {**code["kubernetes"], "idle_ttl_s": code["idle_ttl_s"], "start_timeout_s": 180}
# exactly the core's call (docs/LOKKAL.md §7.28 point 6), under a test instance so the core's own reap ignores it
env = KubernetesEnvironment(cfg=cfg, cwd="/workspace", sub_path="ci-tenant/ci-user/ci-session",
                            labels={"app.kubernetes.io/name": "nokker-sandbox", "nokker.instance": "ci-core",
                                    "nokker.tenant": "ci-tenant", "nokker.session": "ci-1"}, timeout=120)
def run(cmd):
    r = env.execute(cmd)
    return r["returncode"], r["output"].strip()
try:
    rc, out = run("id -u; touch /workspace/proof && echo écrit; touch /etc/x 2>&1 || echo rootfs-ro")
    print("  exec:", out.replace("\n", " | ")); assert rc == 0 and "écrit" in out and "rootfs-ro" in out
    rc, out = run("cat /var/run/secrets/kubernetes.io/serviceaccount/token 2>&1 || echo pas-de-jeton")
    assert "pas-de-jeton" in out, out
    rc, out = run("curl -s -o /dev/null -w '%{http_code}' -m 20 https://example.com")
    print("  par le proxy :", out); assert out in ("200", "301", "302"), out
    rc, out = run("curl -s -o /dev/null -w '%{http_code}' -m 8 --noproxy '*' https://example.com || true")
    print("  sans proxy :", out); assert out in ("000", ""), out
    rc, out = run("curl -s -o /dev/null -w '%{http_code}' -m 8 http://10.43.0.1/ || true")
    print("  plage privée par le proxy :", out); assert out != "200", out
finally:
    env.cleanup()
print("  reap:", reap_k8s_sandboxes("ci-core"))
PY
kubectl -n "$NS" get pvc >/dev/null
ok "pod de session : créé par le core, exec, rootfs en lecture seule, sans jeton, sortie par le proxy seulement"

if kubectl -n "$NS" run privileged-probe --image=busybox --restart=Never \
     --overrides='{"spec":{"containers":[{"name":"p","image":"busybox","securityContext":{"privileged":true}}]}}' \
     >/dev/null 2>&1; then
  kubectl -n "$NS" delete pod privileged-probe --now >/dev/null; fail "pod privilégié accepté"
fi
ok "pod privilégié refusé (Pod Security restricted)"

before=$(kubectl -n "$NS" get secret "$FN-openbao-approle" -o jsonpath='{.data.role_id}')
helm upgrade "$REL" "$CHART" -n "$NS" -f "$HERE/k3d-values.yaml" "${SET[@]}" --wait --timeout 10m >/dev/null
after=$(kubectl -n "$NS" get secret "$FN-openbao-approle" -o jsonpath='{.data.role_id}')
[ "$before" = "$after" ] || fail "AppRole changé par l'upgrade"
kubectl -n "$NS" exec "$FN-openbao-0" -c openbao -- bao status >/dev/null || fail "OpenBao scellé après upgrade"
ok "upgrade idempotent (AppRole inchangé, coffre descellé)"
echo "OK"
