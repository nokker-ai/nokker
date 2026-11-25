#!/bin/sh
# Banc de l'image PRIVÉE de sites-chrome (docs/CONNECTED-SITES.md §3.1.1) : `install.sh --sites-only` contre un
# registre local jetable, une fausse route du noyau (/healthz, /v1/local/sites/registry) et une clé cosign de test.
#   SC_IMAGE=local/sites-chrome:dev sh tests/e2e_sites/private_install.sh
# Cas : machine liée → image tirée par digest, signature vérifiée, profil allumé, container démarré, aucun identifiant
# laissé sur disque ; machine non liée (409) → refus, rien tiré ; image signée par une AUTRE clé → refus, image retirée.
set -u
HERE=$(cd "$(dirname "$0")" && pwd); ROOT=$(cd "$HERE/../.." && pwd)
SC_IMAGE=${SC_IMAGE:-local/sites-chrome:dev}
W=$(mktemp -d /tmp/nokker_test_sitespriv.XXXX); chmod 755 "$W"
REGP=5055; API=18742; REG=127.0.0.1:$REGP; PROJ=nokkertest-sitespriv
COSIGN_V=v2.4.1
export NOKKER_LANG=fr   # messages vérifiés en français (install.sh est anglais par défaut)
R=0; ok() { if [ "$1" = 0 ]; then echo "VERT  | $2"; else echo "ROUGE | $2"; R=1; fi; }
cleanup() {
  docker compose -p $PROJ -f "$W/dir/docker-compose.yml" --env-file "$W/dir/.env" down >/dev/null 2>&1
  docker rm -f nokker_test_reg >/dev/null 2>&1; [ -n "${FAKE:-}" ] && kill "$FAKE" 2>/dev/null
  rmrefs; rm -rf "$W"
}
# Retire les RÉFÉRENCES du registre de test seulement (jamais par identifiant : l'image source a le même)
rmrefs() { for r in $(docker images --digests --format '{{.Repository}}@{{.Digest}}' "$REG/lokkal-private/sites-chrome" 2>/dev/null) \
                    $(docker images --format '{{.Repository}}:{{.Tag}}' "$REG/lokkal-private/sites-chrome" 2>/dev/null); do
             case "$r" in *"<none>"*) ;; *) docker rmi "$r" >/dev/null 2>&1;; esac; done; }
trap cleanup EXIT

mkdir -p "$W/bin"
curl -fsSL -o "$W/bin/cosign" "https://github.com/sigstore/cosign/releases/download/$COSIGN_V/cosign-linux-amd64" && chmod +x "$W/bin/cosign"
export PATH="$W/bin:$PATH"
# Registre AVEC authentification (comme Harbor) : le robot de test, mot de passe « good » ; un autre mot de passe
# (secret renouvelé entre-temps par une autre machine du compte) est refusé.
RUSER='robot$lokkal-private+lokkal-test'
docker run --rm --entrypoint htpasswd httpd:2-alpine -Bbn "$RUSER" Gd7xQ2tst > "$W/htpasswd" 2>/dev/null
docker run -d --name nokker_test_reg -p 127.0.0.1:$REGP:5000 -v "$W/htpasswd:/auth/htpasswd:ro" \
  -e REGISTRY_AUTH=htpasswd -e REGISTRY_AUTH_HTPASSWD_REALM=test -e REGISTRY_AUTH_HTPASSWD_PATH=/auth/htpasswd \
  registry:2 >/dev/null; sleep 2
PUSHCFG="$W/pushcfg"; mkdir -p "$PUSHCFG"
printf Gd7xQ2tst | docker --config "$PUSHCFG" login -u "$RUSER" --password-stdin "$REG" >/dev/null 2>&1

push_signed() { # push_signed <tag> <clé> → digest ; « evil » = image dérivée (digest distinct), signée par l'autre clé
  if [ "$1" = evil ]; then printf 'FROM %s\nLABEL nokker.test=evil\n' "$SC_IMAGE" | docker build -q -t "$REG/lokkal-private/sites-chrome:$1" - >/dev/null
  else docker tag "$SC_IMAGE" "$REG/lokkal-private/sites-chrome:$1"; fi
  docker --config "$PUSHCFG" push -q "$REG/lokkal-private/sites-chrome:$1" >/dev/null
  d=$(docker inspect --format '{{index .RepoDigests 0}}' "$REG/lokkal-private/sites-chrome:$1" | sed 's/.*@//')
  DOCKER_CONFIG="$PUSHCFG" COSIGN_PASSWORD=t cosign sign --yes --tlog-upload=false --allow-insecure-registry --key "$2" \
    "$REG/lokkal-private/sites-chrome@$d" >/dev/null 2>&1
  docker rmi "$REG/lokkal-private/sites-chrome:$1" >/dev/null 2>&1; echo "$d"
}
(cd "$W" && COSIGN_PASSWORD=t cosign generate-key-pair --output-key-prefix good >/dev/null 2>&1 \
         && COSIGN_PASSWORD=t cosign generate-key-pair --output-key-prefix evil >/dev/null 2>&1)
GOOD=$(push_signed good "$W/good.key"); EVIL=$(push_signed evil "$W/evil.key")
echo "digests : bon=${GOOD:-VIDE} autre=${EVIL:-VIDE}"
rmrefs

# fausse route du noyau local : MODE=linked|unlinked
cat > "$W/fake.py" <<'EOF'
import http.server, json, os, sys
TOK = open(sys.argv[2]).read().strip()
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, body=None):
        raw = json.dumps(body).encode() if body is not None else b""
        self.send_response(code); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def do_GET(self):
        self._send(200, {"ok": True}) if self.path == "/healthz" else self._send(404)
    def do_POST(self):
        if self.path != "/v1/local/sites/registry" or self.headers.get("Authorization") != f"Bearer {TOK}":
            return self._send(401)
        mode = open(sys.argv[3]).read().strip()
        n = int(open(sys.argv[3] + ".n").read() or 0) + 1 if os.path.exists(sys.argv[3] + ".n") else 1
        open(sys.argv[3] + ".n", "w").write(str(n))
        if mode == "unlinked":
            return self._send(409, {"error": {"code": "not_linked", "message": "machine non liée"}})
        if mode == "quota":
            return self._send(429, {"error": {"code": "quota_exceeded", "message": "quota"}, "retry_after_s": 7300})
        # race : 1re demande → secret déjà périmé ; stale : toujours périmé ; linked : bon secret
        pw = "stale" if mode == "stale" or (mode == "race" and n == 1) else "Gd7xQ2tst"
        self._send(200, {"registry": sys.argv[4], "username": "robot$lokkal-private+lokkal-test", "password": pw,
                         "expires_at": "2026-12-06T00:00:00Z"})
http.server.HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
EOF
D="$W/dir"; mkdir -p "$D/state" "$D/keys"; head -c 24 /dev/urandom | base64 > "$D/state/local-token"
cp "$W/good.pub" "$D/keys/sites-chrome.pub"
cat > "$D/docker-compose.yml" <<EOF
services:
  sites-chrome:
    image: \${NOKKER_SITES_CHROME_IMAGE:-nokker-sites-chrome-absent}@\${NOKKER_SITES_CHROME_DIGEST:-sha256:0000000000000000000000000000000000000000000000000000000000000000}
    pull_policy: never
    profiles: [sites]
    init: true
    environment:
      NOKKER_SITES_BROWSER_TOKEN: \${NOKKER_SITES_BROWSER_TOKEN:-}
    shm_size: 1g
networks:
  default:
    ipam: {config: [{subnet: "${SUBNET:-10.250.47.0/24}"}]}
EOF
printf 'COMPOSE_PROJECT_NAME=%s\nNOKKER_API_PORT=%s\nNOKKER_SITES_CHROME_DIGEST=%s\nNOKKER_SITES_BROWSER_TOKEN=%s\nCOMPOSE_PROFILES=\n' \
  $PROJ $API "$GOOD" "$(head -c 32 /dev/urandom | base64 | tr -d '/+=' | head -c 40)" > "$D/.env"
echo unlinked > "$W/mode"
python3 "$W/fake.py" $API "$D/state/local-token" "$W/mode" "$REG" & FAKE=$!; sleep 1

out=$(sh "$ROOT/deploy/lokkal/install.sh" --sites-only --dir "$D" 2>&1); rc=$?
ok $([ $rc = 3 ] && echo "$out" | grep -q "non liée" && echo 0 || echo 1) "machine non liée → refus (exit 3), message de liaison"
ok $(docker images -q "$REG/lokkal-private/sites-chrome" | grep -q . && echo 1 || echo 0) "machine non liée → rien tiré"

echo linked > "$W/mode"
out=$(sh "$ROOT/deploy/lokkal/install.sh" --sites-only --dir "$D" 2>&1); rc=$?
ok $([ $rc = 0 ] && echo 0 || echo 1) "machine liée → --sites-only réussi"; echo "$out" | sed 's/^/      /'
ok $(grep -q '^COMPOSE_PROFILES=sites$' "$D/.env" && grep -q '^NOKKER_SITES=1$' "$D/.env" && echo 0 || echo 1) "profil sites et NOKKER_SITES=1 posés"
ok $([ "$(docker inspect --format '{{.State.Running}}' ${PROJ}-sites-chrome-1 2>/dev/null)" = true ] && echo 0 || echo 1) "sites-chrome démarré, image par digest"
ok $([ ! -s "$D/docker/config.json" ] && ! grep -rq "Gd7xQ2tst" "$D" && echo 0 || echo 1) "aucun identifiant du registre laissé sur disque"

reset() { docker compose -p $PROJ -f "$D/docker-compose.yml" --env-file "$D/.env" down >/dev/null 2>&1
  docker rmi "$REG/lokkal-private/sites-chrome@$GOOD" >/dev/null 2>&1; rm -f "$W/mode.n"
  sed -i "s|^COMPOSE_PROFILES=.*|COMPOSE_PROFILES=|; s|^NOKKER_SITES=.*|NOKKER_SITES=0|" "$D/.env"; }
calls() { cat "$W/mode.n" 2>/dev/null || echo 0; }

reset; echo race > "$W/mode"
out=$(sh "$ROOT/deploy/lokkal/install.sh" --sites-only --dir "$D" 2>&1); rc=$?
ok $([ $rc = 0 ] && [ "$(calls)" = 2 ] && echo "$out" | grep -q "nouvelle demande" && echo 0 || echo 1) "secret renouvelé entre-temps (401) → UNE nouvelle demande, puis réussi ($(calls) demandes)"
reset; echo stale > "$W/mode"
out=$(sh "$ROOT/deploy/lokkal/install.sh" --sites-only --dir "$D" 2>&1); rc=$?
ok $([ $rc = 3 ] && [ "$(calls)" = 2 ] && echo 0 || echo 1) "secret toujours refusé → 2 demandes au plus, puis refus (exit $rc, $(calls) demandes)"
reset; echo quota > "$W/mode"
out=$(sh "$ROOT/deploy/lokkal/install.sh" --sites-only --dir "$D" 2>&1); rc=$?
ok $([ $rc = 3 ] && [ "$(calls)" = 1 ] && echo "$out" | grep -q "dans 3 h" && echo 0 || echo 1) "quota (429, retry_after_s=7300) → refus, délai dit en heures, pas de nouvelle demande"
echo "$out" | grep -i quota | sed 's/^/      /'

reset; echo linked > "$W/mode"
sed -i "s|^NOKKER_SITES_CHROME_DIGEST=.*|NOKKER_SITES_CHROME_DIGEST=$EVIL|; s|^COMPOSE_PROFILES=.*|COMPOSE_PROFILES=|; s|^NOKKER_SITES=.*|NOKKER_SITES=0|" "$D/.env"
out=$(sh "$ROOT/deploy/lokkal/install.sh" --sites-only --dir "$D" 2>&1); rc=$?
ok $([ $rc = 3 ] && echo "$out" | grep -q "INVALIDE" && echo 0 || echo 1) "image signée par une autre clé → refus (exit 3)"
ok $(docker images --digests | grep -q "${EVIL#sha256:}" && echo 1 || echo 0) "image refusée retirée"
ok $(grep -q '^COMPOSE_PROFILES=$' "$D/.env" && echo 0 || echo 1) "profil sites resté éteint"
[ $R = 0 ] && echo "RÉSUMÉ : tout vert" || echo "RÉSUMÉ : ROUGE"
exit $R
