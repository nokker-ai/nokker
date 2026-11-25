#!/bin/sh
# install.sh --uninstall --purge : lokkal.json (commun aux installations d'un compte) n'est retiré que s'il désigne
# CETTE installation. Deux installations, purge de la latérale → lokkal.json de la principale intact (vu le 17.11).
#   sh deploy/lokkal/tests/purge-lokkaljson.sh      (sous sh, dash, bash s'ils sont là)
# lokkal_json_purge est lue dans install.sh (jamais recopiée). (a) lokkal.json de la principale, purge de la latérale :
# gardé, octet pour octet, message ; (b) lokkal.json de la latérale : retiré ; (c) même dossier par un lien symbolique :
# retiré ; (d) ancien lokkal.json sans install_dir, compose de CETTE installation : retiré, d'une autre : gardé ;
# (e) pas de lokkal.json : rien ; (f) install.sh : appel avant `rm -rf "$DIR"`, plus aucun rm direct de lokkal.json.
set -u
HERE=$(cd "$(dirname "$0")/.." && pwd)
FAIL=0; N=0
ok()   { N=$((N + 1)); }
fail() { N=$((N + 1)); FAIL=$((FAIL + 1)); printf 'FAIL %s\n' "$*"; }
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
sed -n '/^lokkal_json_purge() {/,/^}/p' "$HERE/install.sh" > "$TMP/fns.sh"
grep -q '^lokkal_json_purge() {' "$TMP/fns.sh" && ok || fail "lokkal_json_purge introuvable dans install.sh"
mkdir -p "$TMP/principale" "$TMP/laterale" "$TMP/c"; ln -s "$TMP/laterale" "$TMP/lien-laterale"
lj() { # lj <dossier d'installation> [sans-install-dir]
  if [ -z "${2:-}" ]; then
    printf '{"compose": "%s/docker-compose.yml", "project": "p", "version": "v", "core_service": "core",\n "api_url": "http://127.0.0.1:8742", "web_url": "http://127.0.0.1:7420",\n "token_path": "%s/state/local-token", "env_file": "%s/.env", "install_dir": "%s"}\n' "$1" "$1" "$1" "$1"
  else
    printf '{"compose": "%s/docker-compose.yml", "project": "p", "api_url": "http://127.0.0.1:8742"}\n' "$1"
  fi > "$TMP/c/lokkal.json"
}
run() { # run <DIR purgé>
  ( CONF_DIR=$TMP/c; DIR=$1
    msg() { _f=$1; shift; printf -- "$_f" "$@"; }; say() { printf 'SAY %s\n' "$(msg "$@")"; }
    . "$TMP/fns.sh"; lokkal_json_purge ) > "$TMP/out" 2>&1
}
lj "$TMP/principale"; cp "$TMP/c/lokkal.json" "$TMP/avant"; run "$TMP/laterale"
cmp -s "$TMP/c/lokkal.json" "$TMP/avant" && ok || fail "(a) lokkal.json de la principale touché"
grep -q "^SAY lokkal.json kept: it points to another install ($TMP/principale)" "$TMP/out" && ok || fail "(a) message : $(cat "$TMP/out")"
lj "$TMP/laterale"; run "$TMP/laterale"
[ ! -e "$TMP/c/lokkal.json" ] && [ ! -s "$TMP/out" ] && ok || fail "(b) lokkal.json de la latérale gardé : $(cat "$TMP/out")"
lj "$TMP/laterale"; run "$TMP/lien-laterale"
[ ! -e "$TMP/c/lokkal.json" ] && ok || fail "(c) même dossier par un lien : gardé"
lj "$TMP/laterale" x; run "$TMP/laterale"
[ ! -e "$TMP/c/lokkal.json" ] && ok || fail "(d) ancien format, cette installation : gardé"
lj "$TMP/principale" x; cp "$TMP/c/lokkal.json" "$TMP/avant"; run "$TMP/laterale"
cmp -s "$TMP/c/lokkal.json" "$TMP/avant" && ok || fail "(d) ancien format, autre installation : touché"
rm -f "$TMP/c/lokkal.json"; run "$TMP/laterale"
[ ! -s "$TMP/out" ] && ok || fail "(e) sans lokkal.json : $(cat "$TMP/out")"
awk '/^    lokkal_json_purge$/ {p = NR} /^    rm -rf "\$DIR"$/ {r = NR} END {exit !(p && r == p + 1)}' "$HERE/install.sh" && ok \
  || fail "(f) lokkal_json_purge pas appelée juste avant rm -rf \"\$DIR\""
grep -nE 'rm -(r)?f[^#]*CONF_DIR/lokkal\.json' "$HERE/install.sh" > "$TMP/rms"; [ ! -s "$TMP/rms" ] && ok || fail "(f) rm direct de lokkal.json restant : $(cat "$TMP/rms")"
printf '%s : %s contrôles, %s échec(s)\n' "${0##*/}" "$N" "$FAIL"
[ "$FAIL" -eq 0 ]
