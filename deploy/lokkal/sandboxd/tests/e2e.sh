#!/usr/bin/env bash
# Recette du proxy docker + proxy de sortie contre un VRAI démon, avec le VRAI CLI docker (image docker:cli), en
# rejouant les appels du DockerEnvironment de la bibliothèque amont. À lancer sur une machine de test, jamais en prod.
#   e2e.sh   (depuis ce dossier)
set -uo pipefail
P=nokker-e2e-$$
WS=/tmp/$P-ws; mkdir -p "$WS"; echo bonjour > "$WS/hello.txt"
SB=nokker-sandbox            # nom attendu par le noyau ; on vérifie qu'il n'existe pas déjà
ok=0; ko=0
pass() { echo "  OK  $*"; ok=$((ok+1)); }
fail() { echo "  KO  $*"; ko=$((ko+1)); }
cleanup() {
  docker rm -f $P-proxy $P-egress >/dev/null 2>&1
  docker ps -aq --filter label=nokker.sandbox | xargs -r docker rm -f >/dev/null 2>&1
  docker network rm $P-api $SB >/dev/null 2>&1; docker rmi $P-img >/dev/null 2>&1; rm -rf "$WS"
}
trap cleanup EXIT
docker network inspect $SB >/dev/null 2>&1 && { echo "réseau $SB déjà présent : abandon"; exit 2; }
docker build -q -t $P-img . >/dev/null
docker network create --internal $P-api >/dev/null
docker network create --internal $SB >/dev/null
docker pull -q alpine:3.20 >/dev/null; docker pull -q hello-world >/dev/null; docker pull -q docker:27-cli >/dev/null
docker run -d --name $P-egress --network $SB --cap-drop ALL --security-opt no-new-privileges $P-img >/dev/null
docker network connect bridge $P-egress
docker run -d --name $P-proxy --network $P-api --user 0 --cap-drop ALL --security-opt no-new-privileges --read-only \
  -v /var/run/docker.sock:/var/run/docker.sock -e NOKKER_HOST_WORKSPACE="$WS" -e NOKKER_SANDBOX_IMAGES=alpine \
  -e NOKKER_SANDBOX_EGRESS=http://$P-egress:3128 $P-img python3 dockerproxy.py >/dev/null
sleep 2
D() { docker run --rm --network $P-api -e DOCKER_HOST=tcp://$P-proxy:2375 docker:27-cli "$@"; }

echo "— sondes"
D version --format '{{.Server.Version}}' >/dev/null 2>&1 && pass version || fail version
D info --format '{{.OSType}}' >/dev/null 2>&1 && pass info || fail info
D run --rm --cpus 0.5 --memory 64m --pids-limit 32 alpine:3.20 true >/dev/null 2>&1 && pass "sonde cgroup (run --rm … true)" || fail "sonde cgroup"
id=$(D create --storage-opt size=1m hello-world 2>&1); case "$id" in *Forbidden*|*nokker*) fail "sonde disque : $id";; *)
  pass "sonde disque create (ou refus du pilote de stockage, hors proxy)"; D rm "$id" >/dev/null 2>&1;; esac

echo "— bac à sable façon bibliothèque amont"
cid=$(D run -d --init --name $P-sb --label nokker.sandbox=fil-1 -w /workspace --cap-drop ALL \
  --cap-add DAC_OVERRIDE --cap-add CHOWN --cap-add FOWNER --security-opt no-new-privileges --pids-limit 256 \
  --cpus 1 --memory 512m --tmpfs /tmp --tmpfs /run -v "$WS:/workspace" --network $SB alpine:3.20 sleep infinity 2>&1)
[ ${#cid} -ge 12 ] && pass "run -d étiqueté" || fail "run -d : $cid"
[ "$(D exec -i -w /workspace -e A=1 $P-sb sh -c 'cat hello.txt; echo $A')" = "$(printf 'bonjour\n1')" ] && pass "exec lit /workspace + env" || fail "exec"
D exec $P-sb sh -c 'echo écrit > /workspace/out.txt' && [ -f "$WS/out.txt" ] && pass "exec écrit dans le dossier" || fail "écriture"
D inspect -f '{{.State.Running}} {{.HostConfig.NetworkMode}}' $P-sb | grep -q "true $SB" && pass inspect || fail inspect
D ps -a --filter label=nokker.sandbox --format '{{.Names}}' | grep -q $P-sb && pass "ps filtré" || fail "ps"
# wget de busybox n'envoie pas CONNECT (il refait GET https://… au proxy) : on passe par apk (libfetch, CONNECT) puis curl.
D exec $P-sb sh -c 'apk add -q --no-cache curl' >/dev/null 2>&1 && pass "apk add par le proxy (https, CONNECT)" || fail "apk add"
out=$(D exec $P-sb sh -c 'curl -sS -m 10 https://pypi.org/simple/pip/ | head -c 60' 2>&1); case "$out" in *html*|*DOCTYPE*|*pip*) pass "curl https://pypi.org par le proxy";; *) fail "curl https : $out";; esac
out=$(D exec $P-sb sh -c 'curl -sS -m 10 -o /dev/null -w "%{http_code}" http://deb.debian.org/' 2>&1); case "$out" in 200|30*) pass "http en clair par le proxy";; *) fail "http : $out";; esac
out=$(D exec $P-sb sh -c 'wget -q -O- -T 5 http://169.254.169.254/ 2>&1; wget -q -O- -T 5 http://10.0.0.1/ 2>&1; echo fin'); echo "$out" | grep -qi "forbidden\|403" && pass "plages privées refusées par egress" || fail "privé : $out"
out=$(D exec $P-sb sh -c 'unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY; wget -q -O- -T 5 http://1.1.1.1/ 2>&1; echo rc=$?'); echo "$out" | grep -q "rc=0" && fail "sortie directe sans proxy : $out" || pass "pas de sortie directe (réseau interne)"
D exec $P-sb sh -c 'ls /var/run/docker.sock' >/dev/null 2>&1 && fail "socket visible" || pass "pas de socket dans le bac à sable"

echo "— refus"
r() { local what=$1; shift; out=$(D "$@" 2>&1); echo "$out" | grep -q "nokker dockerproxy" && pass "$what refusé" || fail "$what : $out"; }
r "privileged" run -d --label nokker.sandbox=x --security-opt no-new-privileges --privileged --network $SB alpine:3.20 true
r "montage /" run -d --label nokker.sandbox=x --security-opt no-new-privileges -v /:/host --network $SB alpine:3.20 true
r "montage socket" run -d --label nokker.sandbox=x --security-opt no-new-privileges -v /var/run/docker.sock:/s --network $SB alpine:3.20 true
r "réseau host" run -d --label nokker.sandbox=x --security-opt no-new-privileges --network host alpine:3.20 true
r "réseau bridge" run -d --label nokker.sandbox=x --security-opt no-new-privileges alpine:3.20 true
r "pid host" run -d --label nokker.sandbox=x --security-opt no-new-privileges --pid host --network $SB alpine:3.20 true
r "SYS_ADMIN" run -d --label nokker.sandbox=x --security-opt no-new-privileges --cap-add SYS_ADMIN --network $SB alpine:3.20 true
r "sans no-new-privileges" run -d --label nokker.sandbox=x --network $SB alpine:3.20 true
r "sans étiquette" run -d --network $SB alpine:3.20 sleep 9
r "exec privilégié" exec --privileged $P-sb id
r "exec hors bac à sable" exec $P-proxy id
r "inspect hors bac à sable" inspect $P-proxy
r "rm hors bac à sable" rm -f $P-egress
r "build" build -q - <<<"FROM alpine"
r "network create" network create x
r "volume create" volume create x
D cp /etc/hostname $P-sb:/tmp/nokker-cp >/dev/null 2>&1; D exec $P-sb test -e /tmp/nokker-cp && fail "cp vers le container passé" || pass "cp vers le container refusé"
D ps -a --format '{{.Names}}' | grep -q $P-proxy && fail "ps montre le proxy" || pass "ps ne montre que les bacs à sable"

echo "— ménage"
D stop -t 10 $P-sb >/dev/null && pass stop || fail stop
D rm -f $P-sb >/dev/null && pass rm || fail rm
echo "== $ok OK, $ko KO"
[ $ko -eq 0 ]
