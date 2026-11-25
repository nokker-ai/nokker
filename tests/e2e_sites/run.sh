#!/bin/sh
# e2e réel des sites connectés (docs/CONNECTED-SITES.md §13.3 étape 1) : image sites-chrome, pilote du noyau, sites
# simulés en HTTP, sur un réseau docker jetable. Hors de la suite unitaire.
#   SC_IMAGE=local/sites-chrome:dev CORE_IMAGE=ghcr.io/nokker-ai/nokker:<v>-lokkal sh tests/e2e_sites/run.sh recipe_e2e.py
#   (e2e_sc.py : mécanique — connexion masquée, profil, écran, sortie ; recipe_e2e.py : recette LinkedIn vs simulé ;
#    MFA=code pour la connexion avec code)
set -e
HERE=$(cd "$(dirname "$0")" && pwd); ROOT=$(cd "$HERE/../.." && pwd)
SC_IMAGE=${SC_IMAGE:-local/sites-chrome:dev}; CORE_IMAGE=${CORE_IMAGE:?image du noyau}; NET=${NET:-sitese2e}
TOK=$(head -c 32 /dev/urandom | base64 | tr -d '/+=' | head -c 40)
cleanup() { docker rm -f sc fakesocial fakesite >/dev/null 2>&1 || true; docker network rm "$NET" >/dev/null 2>&1 || true; }
trap cleanup EXIT; cleanup
docker network create ${SUBNET:+--subnet $SUBNET} "$NET" >/dev/null
docker run -d --name fakesite --network "$NET" -v "$HERE/fakesite.py:/f.py:ro" python:3.12-slim python /f.py >/dev/null
docker run -d --name fakesocial --network "$NET" -w /src -e PYTHONPATH=/src -v "$ROOT:/src:ro" --entrypoint python3 "$CORE_IMAGE" \
  -m nokker.sites.fakesocial_http --skin linkedin --host fakesocial:8080 --port 8080 ${MFA:+--mfa $MFA} >/dev/null
docker run -d --name sc --network "$NET" --shm-size 1g --read-only --cap-drop ALL --security-opt no-new-privileges:true --init \
  --tmpfs /tmp:size=256m --tmpfs /home/sites:size=64m,uid=10002,gid=10002 \
  -e NOKKER_SITES_BROWSER_TOKEN="$TOK" -e SITES_ALLOW_PRIVATE=fakesite:8080,fakesocial:8080 "$SC_IMAGE" >/dev/null
sleep 4
docker run --rm --network "$NET" -e MFA="${MFA:-none}" -e NOKKER_SITES_BROWSER_URL=http://sc:8119 -e NOKKER_SITES_BROWSER_TOKEN="$TOK" \
  -e PYTHONPATH=/src -v "$ROOT:/src:ro" -v "$HERE/${1:-recipe_e2e.py}:/t.py:ro" --entrypoint python3 "$CORE_IMAGE" /t.py
