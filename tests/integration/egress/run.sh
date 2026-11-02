#!/bin/sh
# Compose de test local de l'injection des secrets (docs/EGRESS-SECRETS.md §8). Valeur et clé tirées au hasard ; le
# script vérifie la substitution, les refus, le caviardage et que la valeur n'apparaît nulle part côté bac à sable.
set -eu
cd "$(dirname "$0")"
export TEST_SECRET_VALUE="tok_$(head -c 18 /dev/urandom | od -An -tx1 | tr -d ' \n')"
export TEST_CONTROL_KEY="$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"
trap 'docker compose down -v --remove-orphans >/dev/null 2>&1 || true' EXIT
docker compose build -q
docker compose up --abort-on-container-exit --exit-code-from sandbox sandbox core egress upstream >/dev/null 2>&1 || true
OUT=$(docker compose logs --no-color --no-log-prefix sandbox)
LOGS=$(docker compose logs --no-color egress core)
fail() { echo "ÉCHEC : $1"; echo "$OUT"; exit 1; }
echo "$OUT" | grep -q '"auth_ok": true' || fail "valeur non posée sur l'hôte autorisé"
echo "$OUT" | grep -q '"echo_auth": "Bearer secret:GH_TOKEN"' || fail "corps non caviardé"
echo "$OUT" | grep -qi '^x-echo-auth: Bearer secret:GH_TOKEN' || fail "en-tête non caviardé"
echo "$OUT" | sed -n '/== hote_refuse/{n;p}' | grep -q '^403' || fail "hôte non autorisé : pas de 403"
echo "$OUT" | sed -n '/== emplacement_refuse/{n;p}' | grep -q '^403' || fail "emplacement non déclaré : pas de 403"
echo "$OUT" | sed -n '/== direct/,/== env/p' | grep -q '^200' && fail "sortie directe possible"
echo "$OUT" | grep -qF "$TEST_SECRET_VALUE" && fail "valeur visible dans le bac à sable"
echo "$LOGS" | grep -qF "$TEST_SECRET_VALUE" && fail "valeur dans les journaux"
echo "$LOGS" | grep -q '"event": "substitution"' || fail "journal de substitution absent"
echo "OK : substitution, refus (hôte, emplacement), caviardage, valeur absente du bac à sable et des journaux"
