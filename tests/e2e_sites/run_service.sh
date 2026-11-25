#!/bin/sh
# e2e du service des sites connectés (service_e2e.py) : noyau = code de l'IMAGE (CORE_SRC vide) ou d'un arbre monté
# (CORE_SRC=/chemin) ; site simulé et banc toujours pris dans cet arbre.
#   CORE_IMAGE=nokker:<sha> SC_IMAGE=local/sites-chrome:dev sh tests/e2e_sites/run_service.sh
set -e
HERE=$(cd "$(dirname "$0")" && pwd); ROOT=$(cd "$HERE/../.." && pwd)
SC_IMAGE=${SC_IMAGE:-local/sites-chrome:dev}; CORE_IMAGE=${CORE_IMAGE:?image du noyau}; NET=${NET:-sitese2e}
TOK=$(head -c 32 /dev/urandom | base64 | tr -d '/+=' | head -c 40)
cleanup() { docker rm -f sc fakesocial >/dev/null 2>&1 || true; docker network rm "$NET" >/dev/null 2>&1 || true; }
trap cleanup EXIT; cleanup
docker network create --subnet "${SUBNET:-10.250.45.0/24}" "$NET" >/dev/null
docker run -d --name fakesocial --network "$NET" -e FAKESOCIAL_CONTROL=1 -w /src -e PYTHONPATH=/src -v "$ROOT:/src:ro" \
  --entrypoint python3 "$CORE_IMAGE" -m nokker.sites.fakesocial_http --skin linkedin --host fakesocial:8080 --port 8080 \
  --account "bea@example.org:Mdp-Bea-55kR:unknown:Bea Rossi" >/dev/null
docker run -d --name sc --network "$NET" --shm-size 1g --read-only --cap-drop ALL --security-opt no-new-privileges:true --init \
  --tmpfs /tmp:size=256m --tmpfs /home/sites:size=64m,uid=10002,gid=10002 \
  -e NOKKER_SITES_BROWSER_TOKEN="$TOK" -e SITES_ALLOW_PRIVATE=fakesocial:8080 "$SC_IMAGE" >/dev/null
sleep 4
docker run --rm --network "$NET" -e DUMP="${DUMP:-}" -e NOKKER_SITES_BROWSER_URL=http://sc:8119 -e NOKKER_SITES_BROWSER_TOKEN="$TOK" \
  ${CORE_SRC:+-e PYTHONPATH=/core -v "$CORE_SRC:/core:ro"} -v "$HERE/service_e2e.py:/t.py:ro" \
  --entrypoint python3 "$CORE_IMAGE" /t.py
