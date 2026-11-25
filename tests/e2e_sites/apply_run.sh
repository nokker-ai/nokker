#!/bin/sh
# e2e réel du dépôt par Lokkal (pack Emploi, lot L2) : image sites-chrome, jobsd du noyau, portails SIMULÉS Workday et
# SuccessFactors servis en HTTP, sur un réseau docker jetable. Hors de la suite unitaire. Aucun portail réel.
#   SC_IMAGE=local/sites-chrome:dev CORE_IMAGE=ghcr.io/nokker-ai/nokker:<v>-lokkal sh tests/e2e_sites/apply_run.sh
set -e
HERE=$(cd "$(dirname "$0")" && pwd); ROOT=$(cd "$HERE/../.." && pwd)
SC_IMAGE=${SC_IMAGE:-local/sites-chrome:dev}; CORE_IMAGE=${CORE_IMAGE:?image du noyau}; NET=${NET:-applye2e}
TOK=$(head -c 32 /dev/urandom | base64 | tr -d '/+=' | head -c 40)
cleanup() { docker rm -f sca fakewd fakesf >/dev/null 2>&1 || true; docker network rm "$NET" >/dev/null 2>&1 || true; }
trap cleanup EXIT; cleanup
docker network create ${SUBNET:+--subnet $SUBNET} "$NET" >/dev/null
for S in wd:workday sf:successfactors; do
  N=fake${S%%:*}; SKIN=${S#*:}
  docker run -d --name "$N" --network "$NET" -w /src -e PYTHONPATH=/src -v "$ROOT:/src:ro" --entrypoint python3 "$CORE_IMAGE" \
    -m nokker.jobs.apply.fakeats_http --skin "$SKIN" --host "$N:8080" --port 8080 >/dev/null
done
docker run -d --name sca --network "$NET" --shm-size 1g --read-only --cap-drop ALL --security-opt no-new-privileges:true --init \
  --tmpfs /tmp:size=256m --tmpfs /home/sites:size=64m,uid=10002,gid=10002 \
  -e NOKKER_SITES_BROWSER_TOKEN="$TOK" -e SITES_ALLOW_PRIVATE=fakewd:8080,fakesf:8080 "$SC_IMAGE" >/dev/null
sleep 4
docker run --rm --network "$NET" -e NOKKER_SITES_BROWSER_URL=http://sca:8119 -e NOKKER_SITES_BROWSER_TOKEN="$TOK" \
  -e WD=fakewd:8080 -e SF=fakesf:8080 -w /src -e PYTHONPATH=/src -v "$ROOT:/src:ro" --entrypoint python3 "$CORE_IMAGE" \
  /src/tests/e2e_sites/apply_e2e.py
