#!/bin/sh
# Service `vault-init` (lancé une fois par install.sh --vault : `docker compose --profile vault run --rm vault-init`).
# Initialise `openbao` HORS du noyau (docs/LOKKAL.md §7 X4) : 1 part / seuil 1, KV v2 `nokker/`, policy et AppRole
# `nokker-core`, root token révoqué à la fin. Le noyau ne reçoit que role_id et secret_id (/vault/approle, 0600).
#   VAULT_UNSEAL=auto   (défaut) la part va dans /vault/unseal/unseal.key (0600) ; vault-unseal descelle seul.
#   VAULT_UNSEAL=manual la part est affichée UNE fois sur la sortie standard, puis oubliée ; descellement à la main
#                       (`docker compose -f ~/.nokker/docker-compose.yml exec openbao bao operator unseal`, la part est demandée sans écho).
# Idempotent : un coffre déjà initialisé n'est pas réinitialisé (sortie 0 si l'AppRole est en place, 3 sinon).
set -eu
umask 077
export BAO_ADDR=${BAO_ADDR:-http://openbao:8200}
MODE=${VAULT_UNSEAL:-auto}
# Descellement par l'API (la CLI ne lit pas la part sur l'entrée standard) : JSON dans /tmp (tmpfs, 0600), jamais en
# argument d'un processus.
unseal_with() {
  ( umask 077; printf '{"key":"%s"}' "$1" > /tmp/unseal.json )
  wget -qO /dev/null --header='Content-Type: application/json' --post-file=/tmp/unseal.json "$BAO_ADDR/v1/sys/unseal"
  rc=$?; rm -f /tmp/unseal.json; return $rc
}
case "$MODE" in auto|manual) ;; *) echo "VAULT_UNSEAL=$MODE : auto ou manual" >&2; exit 2;; esac

i=0
until bao status >/dev/null 2>&1 || [ $? = 2 ]; do
  i=$((i+1)); [ $i -lt 60 ] || { echo "openbao injoignable sur $BAO_ADDR" >&2; exit 1; }; sleep 1
done
if bao status -format=json 2>/dev/null | grep -q '"initialized": *true'; then
  if [ -s /vault/approle/role_id ] && [ -s /vault/approle/secret_id ]; then echo "coffre déjà initialisé"; exit 0; fi
  echo "coffre initialisé mais AppRole absent de /vault/approle : réparation par generate-root (voir LOKKAL §7)" >&2
  exit 3
fi

OUT=$(bao operator init -key-shares=1 -key-threshold=1 -format=json)
KEY=$(printf '%s' "$OUT" | tr -d ' \n' | sed -n 's/.*"unseal_keys_b64":\["\([^"]*\)".*/\1/p')
ROOT=$(printf '%s' "$OUT" | tr -d ' \n' | sed -n 's/.*"root_token":"\([^"]*\)".*/\1/p')
unset OUT
[ -n "$KEY" ] && [ -n "$ROOT" ] || { echo "réponse d'init illisible" >&2; exit 1; }
unseal_with "$KEY" && bao status >/dev/null || { echo "descellement après init impossible" >&2; exit 1; }

export BAO_TOKEN="$ROOT"
bao secrets enable -path=nokker -version=2 kv >/dev/null
bao policy write nokker-core - >/dev/null <<'HCL'
path "nokker/data/*"     { capabilities = ["create", "read", "update", "delete"] }
path "nokker/metadata/*" { capabilities = ["read", "list", "delete"] }
HCL
bao auth enable approle >/dev/null
bao write auth/approle/role/nokker-core token_policies=nokker-core token_ttl=1h token_max_ttl=24h \
  secret_id_ttl=0 secret_id_num_uses=0 >/dev/null
bao read -field=role_id auth/approle/role/nokker-core/role-id > /vault/approle/role_id
bao write -f -field=secret_id auth/approle/role/nokker-core/secret-id > /vault/approle/secret_id
chmod 600 /vault/approle/role_id /vault/approle/secret_id
bao token revoke -self >/dev/null
unset BAO_TOKEN ROOT

if [ "$MODE" = auto ]; then
  printf '%s' "$KEY" > /vault/unseal/unseal.key; chmod 600 /vault/unseal/unseal.key
  echo "coffre initialisé (descellement automatique)"
else
  echo "coffre initialisé (descellement MANUEL). Part de descellement, affichée une seule fois, à ranger hors de cette machine :"
  echo "  $KEY"
  echo "Après chaque redémarrage : docker compose -f ~/.nokker/docker-compose.yml exec openbao bao operator unseal"
fi
unset KEY
