#!/bin/sh
# Bout en bout du profil vault (LOKKAL §7 X4) contre le VRAI docker-compose.yml, sur une machine avec Docker :
#   sh deploy/lokkal/vault/tests/e2e.sh
# Mode auto : init hors du noyau (fichiers 0600 au bon uid), relance idempotente, AppRole nokker-core limité à
# nokker/ (lecture, écriture, liste, suppression ; sys et policy refusés), redescellement par le compagnon après un
# redémarrage d'openbao. Mode manual : aucune part sur disque, le coffre reste scellé après redémarrage.
# La part n'est jamais affichée par ce test.
set -eu
HERE=$(cd "$(dirname "$0")/../.." && pwd)            # deploy/lokkal
W=$(mktemp -d); P=nokkervt$$
cleanup() { (cd "$W" && C down -v >/dev/null 2>&1) || true; rm -rf "$W"; }
trap cleanup EXIT
C() { docker compose -p "$P" --env-file "$W/.env" -f "$W/docker-compose.yml" "$@"; }
ok() { printf 'ok   %s\n' "$*"; }
ko() { printf 'KO   %s\n' "$*" >&2; exit 1; }

cp "$HERE/docker-compose.yml" "$W/"; mkdir -p "$W/vault/unseal" "$W/vault/approle" "$W/state" "$W/ws" "$W/config"
cp "$HERE/vault/init.sh" "$HERE/vault/unseal.sh" "$W/vault/"; touch "$W/config/config.yaml"
chmod 700 "$W/vault" "$W/vault/unseal" "$W/vault/approle"; chmod 600 "$W"/vault/*.sh
env_for() {
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
NOKKER_VAULT_UNSEAL=$1
E
}
sealed() { C exec -T openbao bao status 2>/dev/null | grep -q 'Sealed *true'; }

# ---- mode auto
env_for auto
C config --services | grep -qx openbao || ko "COMPOSE_PROFILES=vault du .env non pris en compte"
C up -d openbao vault-unseal >/dev/null 2>&1
C --profile vault-init run --rm -q vault-init >/dev/null 2>&1 || ko "init"
for f in unseal/unseal.key approle/role_id approle/secret_id; do
  [ "$(stat -c '%a %u' "$W/vault/$f")" = "600 $(id -u)" ] || ko "$f : droits ou propriétaire"
done; ok "init : part et AppRole en 0600, uid $(id -u)"
C --profile vault-init run --rm -q vault-init 2>/dev/null | grep -q "déjà initialisé" || ko "relance de l'init"
ok "init idempotente"
sealed && ko "scellé après init"
out=$(docker run --rm --user "$(id -u):$(id -g)" --network "${P}_internal" -v "$W/vault/approle:/run/nokker-vault:ro" \
  -e BAO_ADDR=http://openbao:8200 -e HOME=/tmp --entrypoint sh openbao/openbao:2.4.1 -c '
  export BAO_TOKEN=$(bao write -field=token auth/approle/login role_id=@/run/nokker-vault/role_id secret_id=@/run/nokker-vault/secret_id) || exit 1
  bao kv put -mount=nokker e2e/demo v=1 >/dev/null && echo put
  [ "$(bao kv get -mount=nokker -field=v e2e/demo)" = 1 ] && echo get
  bao kv list -mount=nokker e2e | grep -q demo && echo list
  bao kv metadata delete -mount=nokker e2e/demo >/dev/null && echo del
  bao secrets list >/dev/null 2>&1 || echo nosys
  echo x | bao policy write x - >/dev/null 2>&1 || echo nopolicy')
[ "$(echo $out)" = "put get list del nosys nopolicy" ] || ko "AppRole : $out"
ok "AppRole nokker-core : nokker/ seulement"
C restart openbao >/dev/null 2>&1; sleep 3; sealed || ko "openbao redémarré mais pas scellé ?"
i=0; while sealed; do i=$((i+1)); [ $i -lt 40 ] || ko "pas redescellé en 40 s"; sleep 1; done
ok "redescellé par vault-unseal après redémarrage (${i} s)"
C down -v >/dev/null 2>&1; rm -f "$W"/vault/unseal/* "$W"/vault/approle/*

# ---- mode manual
env_for manual
C up -d openbao vault-unseal >/dev/null 2>&1
C --profile vault-init run --rm -q vault-init >/dev/null 2>&1 || ko "init manual"
[ -z "$(ls -A "$W/vault/unseal")" ] || ko "manual : une part est restée sur disque"
[ -s "$W/vault/approle/role_id" ] || ko "manual : AppRole absent"
C restart openbao >/dev/null 2>&1; sleep 35; sealed || ko "manual : descellé sans personne"
ok "manual : aucune part sur disque, reste scellé après redémarrage"
echo "vault e2e : OK"
