#!/bin/sh
# Service `vault-pay-init` (profil pay-init, lancé une fois : `docker compose --profile pay-init run --rm vault-pay-init`)
# — accès de l'AGENT DE PAIEMENT au coffre (docs/PAY-AGENT.md §11), sur un coffre déjà initialisé (installation neuve
# OU existante) : montage KV v2 `payagent/`, policy et AppRole `payagent` limitées à ce montage, role_id et secret_id
# dans /vault/approle-pay (0600), dossier À PART de celui du noyau. La policy `nokker-core` ne couvre pas `payagent/` :
# le noyau ne lit jamais la carte.
# Jeton root ÉPHÉMÈRE par generate-root avec la part de descellement (mode auto : /vault/unseal/unseal.key), révoqué à
# la fin. Mode manuel (pas de part sur la machine) : refus, à faire à la main (voir PAY-AGENT §11.5).
# Idempotent : sortie 0 si l'AppRole est déjà en place.
set -eu
umask 077
export BAO_ADDR=${BAO_ADDR:-http://openbao:8200}
if [ -s /vault/approle-pay/role_id ] && [ -s /vault/approle-pay/secret_id ]; then echo "accès de l'agent de paiement déjà en place"; exit 0; fi
[ -s /vault/unseal/unseal.key ] || { echo "part de descellement absente (mode manuel) : voir docs/PAY-AGENT.md §11.5" >&2; exit 3; }
i=0
until bao status >/dev/null 2>&1; do
  i=$((i+1)); [ $i -lt 60 ] || { echo "openbao injoignable ou scellé sur $BAO_ADDR" >&2; exit 1; }; sleep 1
done
field() { tr -d ' \n' | sed -n "s/.*\"$1\":\"\([^\"]*\)\".*/\1/p"; }

# generate-root : OTP à nous, la part de descellement LUE D'UN FICHIER (key=@…, jamais en argument), puis décodage.
bao operator generate-root -cancel >/dev/null 2>&1 || true
INIT=$(bao operator generate-root -init -format=json)
NONCE=$(printf '%s' "$INIT" | field nonce)
OTP=$(printf '%s' "$INIT" | field otp)
unset INIT
ENC=$(bao write -format=json sys/generate-root/update key=@/vault/unseal/unseal.key nonce="$NONCE" | field encoded_token)
[ -n "$ENC" ] || { echo "generate-root : jeton non obtenu" >&2; exit 1; }
BAO_TOKEN=$(bao operator generate-root -decode="$ENC" -otp="$OTP")
export BAO_TOKEN
unset ENC OTP NONCE
trap 'bao token revoke -self >/dev/null 2>&1 || true; unset BAO_TOKEN' EXIT

bao secrets list -format=json | grep -q '"payagent/"' || bao secrets enable -path=payagent -version=2 kv >/dev/null
bao policy write payagent - >/dev/null <<'HCL'
path "payagent/data/*"     { capabilities = ["create", "read", "update", "delete"] }
path "payagent/metadata/*" { capabilities = ["read", "list", "delete"] }
HCL
bao auth list -format=json | grep -q '"approle/"' || bao auth enable approle >/dev/null
bao write auth/approle/role/payagent token_policies=payagent token_ttl=1h token_max_ttl=24h \
  secret_id_ttl=0 secret_id_num_uses=0 >/dev/null
bao read -field=role_id auth/approle/role/payagent/role-id > /vault/approle-pay/role_id
bao write -f -field=secret_id auth/approle/role/payagent/secret-id > /vault/approle-pay/secret_id
chmod 600 /vault/approle-pay/role_id /vault/approle-pay/secret_id
echo "accès de l'agent de paiement au coffre en place (montage payagent/, AppRole payagent)"
