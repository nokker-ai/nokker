#!/bin/sh
# Bout en bout de l'accès de l'agent de paiement au coffre (profil pay-init, docs/PAY-AGENT.md §11.5) contre le VRAI
# docker-compose.yml, sur une machine avec Docker :   sh deploy/lokkal/vault/tests/pay-e2e.sh
# Coffre initialisé AVANT (comme chez un utilisateur existant), puis vault-pay-init : AppRole payagent en 0600 dans un
# dossier à part, relance idempotente ; isolation dans les deux sens (l'agent ne lit pas nokker/ ; le noyau ne lit, ne
# liste ni ne s'ouvre payagent/). La part n'est jamais affichée.
set -eu
HERE=$(cd "$(dirname "$0")/../.." && pwd)            # deploy/lokkal
W=$(mktemp -d); P=nokkerpay$$
cleanup() { (cd "$W" && C down -v >/dev/null 2>&1) || true; rm -rf "$W"; }
trap cleanup EXIT
C() { docker compose -p "$P" --env-file "$W/.env" -f "$W/docker-compose.yml" "$@"; }
ok() { printf 'ok   %s\n' "$*"; }
ko() { printf 'KO   %s\n' "$*" >&2; exit 1; }

cp "$HERE/docker-compose.yml" "$W/"; mkdir -p "$W/vault/unseal" "$W/vault/approle" "$W/vault/approle-pay" "$W/state" "$W/ws" "$W/config"
cp "$HERE/vault/init.sh" "$HERE/vault/unseal.sh" "$HERE/vault/pay-init.sh" "$W/vault/"; touch "$W/config/config.yaml"
chmod 700 "$W/vault" "$W/vault/unseal" "$W/vault/approle" "$W/vault/approle-pay"; chmod 600 "$W"/vault/*.sh
cat > "$W/.env" <<E
NOKKER_VERSION=0.0.0
NOKKER_UID=$(id -u)
NOKKER_GID=$(id -g)
NOKKER_STATE_DIR=$W/state
NOKKER_HOST_WORKSPACE=$W/ws
NOKKER_PG_ADMIN_PASSWORD=x
NOKKER_OWNER_PASSWORD=x
NOKKER_APP_PASSWORD=x
NOKKER_APPROVALS_KEY=x
NOKKER_LOCAL_SECRET_KEY=x
COMPOSE_PROFILES=vault
NOKKER_SECRETS_PROVIDER=openbao
NOKKER_VAULT_UNSEAL=auto
E
C up -d openbao vault-unseal >/dev/null 2>&1
C --profile vault-init run --rm -q vault-init >/dev/null 2>&1 || ko "init du coffre"
ok "coffre initialisé (utilisateur existant)"
C --profile pay-init run --rm -q vault-pay-init >/dev/null 2>&1 || ko "vault-pay-init"
for f in role_id secret_id; do
  [ "$(stat -c '%a %u' "$W/vault/approle-pay/$f")" = "600 $(id -u)" ] || ko "approle-pay/$f : droits ou propriétaire"
done; ok "AppRole payagent en 0600, uid $(id -u), dossier à part"
C --profile pay-init run --rm -q vault-pay-init 2>/dev/null | grep -q "déjà en place" || ko "relance de vault-pay-init"
ok "vault-pay-init idempotent"
out=$(docker run --rm --user "$(id -u):$(id -g)" --network "${P}_internal" -v "$W/vault/approle:/core:ro" \
  -v "$W/vault/approle-pay:/pay:ro" -e BAO_ADDR=http://openbao:8200 -e HOME=/tmp --entrypoint sh openbao/openbao:2.4.1 -c '
  login() { bao write -field=token auth/approle/login role_id=@$1/role_id secret_id=@$1/secret_id; }
  PT=$(login /pay); CT=$(login /core)
  BAO_TOKEN=$PT bao kv put -mount=payagent e2e v=1 >/dev/null && echo pay_put
  BAO_TOKEN=$PT bao kv get -mount=nokker e2e >/dev/null 2>&1 || echo pay_no_nokker
  BAO_TOKEN=$CT bao kv get -mount=payagent e2e >/dev/null 2>&1 || echo core_no_get
  BAO_TOKEN=$CT bao kv list -mount=payagent / >/dev/null 2>&1 || echo core_no_list
  BAO_TOKEN=$CT bao write -f auth/approle/role/payagent/secret-id >/dev/null 2>&1 || echo core_no_secretid')
[ "$(echo $out)" = "pay_put pay_no_nokker core_no_get core_no_list core_no_secretid" ] || ko "isolation : $out"
ok "isolation : l'agent ne lit pas nokker/, le noyau ne touche pas payagent/"
docker compose -p "$P" --env-file "$W/.env" -f "$W/docker-compose.yml" config | grep -A3 'approle-pay' | grep -q '/run/nokker-vault' && ko "approle-pay monté dans core"
ok "approle-pay jamais monté dans core"
