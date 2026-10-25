#!/bin/sh
# Compagnon `vault-unseal` (profil vault, docs/LOKKAL.md §7 X4) : toutes les 30 s, descelle `openbao` s'il est scellé.
# Mode auto seulement : la part est lue dans /vault/unseal/unseal.key (0600, dossier ~/.nokker/vault/unseal de
# l'hôte), jamais passée en argument d'un processus. Mode manual (pas de fichier) : ne fait rien.
# Le noyau ne voit jamais la part.
export BAO_ADDR=${BAO_ADDR:-http://openbao:8200}
KEY=/vault/unseal/unseal.key
# Descellement par l'API (la CLI ne lit pas la part sur l'entrée standard) : JSON dans /tmp (tmpfs, 0600), jamais en
# argument d'un processus.
unseal_with() {
  ( umask 077; printf '{"key":"%s"}' "$1" > /tmp/unseal.json )
  wget -qO /dev/null --header='Content-Type: application/json' --post-file=/tmp/unseal.json "$BAO_ADDR/v1/sys/unseal"
  rc=$?; rm -f /tmp/unseal.json; return $rc
}
while :; do
  bao status >/dev/null 2>&1; rc=$?          # 0 descellé, 2 scellé, 1 injoignable ou pas initialisé
  if [ "$rc" = 2 ] && [ -s "$KEY" ]; then
    if unseal_with "$(cat "$KEY")" && bao status >/dev/null 2>&1; then echo "vault-unseal : coffre descellé"
    else echo "vault-unseal : descellement refusé (part invalide ?)" >&2; fi
  fi
  sleep "${UNSEAL_EVERY:-30}"
done
