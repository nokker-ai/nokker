#!/bin/sh
# Chaîne complète de l'écran de passation par la passerelle (docs/CONNECTED-SITES.md §4.3) : relais (deploy/gateway)
# → tunnel (nokker/local/tunnel.py) → web Lokkal (nginx, deploy/lokkal/web) → sites-chrome → websockify → x11vnc.
#   SC_IMAGE=local/sites-chrome:dev CORE_IMAGE=ghcr.io/nokker-ai/nokker:<v>-lokkal sh tests/e2e_sites/chain.sh
set -e
HERE=$(cd "$(dirname "$0")" && pwd); ROOT=$(cd "$HERE/../.." && pwd)
SC_IMAGE=${SC_IMAGE:-local/sites-chrome:dev}; CORE_IMAGE=${CORE_IMAGE:?image du noyau}; NET=${NET:-sitechain}
TOK=$(head -c 32 /dev/urandom | base64 | tr -d '/+=' | head -c 40)
cleanup() { docker rm -f sc web chain >/dev/null 2>&1 || true; docker network rm "$NET" >/dev/null 2>&1 || true; }
trap cleanup EXIT; cleanup
docker network create ${SUBNET:+--subnet $SUBNET} "$NET" >/dev/null
docker run -d --name sc --network "$NET" --network-alias sites-chrome --shm-size 1g --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true --init --tmpfs /tmp:size=256m --tmpfs /home/sites:size=64m,uid=10002,gid=10002 \
  -e NOKKER_SITES_BROWSER_TOKEN="$TOK" "$SC_IMAGE" >/dev/null
docker run -d --name web --network "$NET" --network-alias web --add-host core:127.0.0.1 \
  -v "$ROOT/deploy/lokkal/web/lokkal.conf:/etc/nginx/conf.d/default.conf:ro" \
  -v "$ROOT/deploy/lokkal/web/security-headers.conf:/etc/nginx/snippets/lokkal-security-headers.conf:ro" nginx:1.27-alpine >/dev/null
sleep 4
docker run --rm --name chain --network "$NET" --network-alias relay.nokker.app -u 0 -e NOKKER_SITES_BROWSER_TOKEN="$TOK" \
  -e PYTHONPATH=/src -v "$ROOT:/src:ro" -v "$HERE/chain.py:/c.py:ro" --entrypoint python3 "$CORE_IMAGE" /c.py
