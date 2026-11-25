#!/bin/sh
# install.sh : NOKKER_ACCORD_LAN (IP:port EXPLICITE) allume le profil `accord` et l'écoute interne du noyau ; vide ou
# joker (0.0.0.0, [::], sans IP, port 0, nom d'hôte) → profil retiré, rien de publié. accord_lan, add_profile,
# remove_profile, getv et setv sont lues dans install.sh (jamais recopiées).
#   sh deploy/lokkal/tests/accord-lan.sh      (sous sh, dash, bash s'ils sont là)
set -u
HERE=$(cd "$(dirname "$0")/.." && pwd)
FAIL=0; N=0
ok()   { N=$((N + 1)); }
fail() { N=$((N + 1)); FAIL=$((FAIL + 1)); printf 'FAIL %s\n' "$*"; }
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
{ sed -n '/^setv() {/,/^}/p' "$HERE/install.sh"; sed -n '/^getv() {/p; /^remove_profile() {/p' "$HERE/install.sh"
  sed -n '/^add_profile() {/,/^}/p' "$HERE/install.sh"; sed -n '/^accord_lan() {/,/^}/p' "$HERE/install.sh"; } > "$TMP/fns.sh"
grep -q '^accord_lan() {' "$TMP/fns.sh" && grep -q '^setv() {' "$TMP/fns.sh" && ok || fail "fonctions introuvables dans install.sh"
grep -q '^accord_lan$' "$HERE/install.sh" && ok || fail "install.sh n'appelle pas accord_lan"
run() { # run <sh> <contenu .env> → .env final dans $TMP/.env, sortie dans $TMP/out
  printf '%s' "$2" > "$TMP/.env"
  "$1" -c 'DIR=$0; msg() { _f=$1; shift; printf -- "$_f" "$@"; }; say() { printf "SAY %s\n" "$(msg "$@")"; }
    warn() { printf "WARN %s\n" "$(msg "$@")"; }; . "$DIR/fns.sh"; accord_lan' "$TMP" > "$TMP/out" 2>&1
}
v() { grep "^$1=" "$TMP/.env" | cut -d= -f2-; }
for sh_ in sh dash bash; do
  command -v "$sh_" >/dev/null || continue
  run "$sh_" 'COMPOSE_PROFILES=home
'
  [ "$(v COMPOSE_PROFILES)" = home ] && [ -z "$(v NOKKER_ACCORD_BIND)" ] && [ ! -s "$TMP/out" ] && ok || fail "($sh_) vide : $(cat "$TMP/.env")"
  run "$sh_" 'COMPOSE_PROFILES=home
NOKKER_ACCORD_LAN=192.168.1.20:8443
'
  [ "$(v COMPOSE_PROFILES)" = home,accord ] && [ "$(v NOKKER_ACCORD_BIND)" = 0.0.0.0:8443 ] && grep -q '^SAY ACCORD relay published on 192.168.1.20:8443' "$TMP/out" && ok \
    || fail "($sh_) IPv4 explicite : $(cat "$TMP/.env" "$TMP/out")"
  run "$sh_" 'COMPOSE_PROFILES=home,accord
NOKKER_ACCORD_LAN=192.168.1.20:8443
NOKKER_ACCORD_BIND=0.0.0.0:9443
'
  [ "$(v COMPOSE_PROFILES)" = home,accord ] && [ "$(v NOKKER_ACCORD_BIND)" = 0.0.0.0:9443 ] && ok || fail "($sh_) rejoué : profil en double ou écoute remplacée"
  run "$sh_" 'NOKKER_ACCORD_LAN=[fd00::20]:8443
'
  [ "$(v COMPOSE_PROFILES)" = accord ] && ok || fail "($sh_) IPv6 explicite refusée"
  for bad in 0.0.0.0:8443 '[::]:8443' :8443 192.168.1.20:0 lokkal.local:8443 192.168.1.20; do
    run "$sh_" "COMPOSE_PROFILES=home,accord
NOKKER_ACCORD_LAN=$bad
"
    [ "$(v COMPOSE_PROFILES)" = home ] && grep -qF "WARN NOKKER_ACCORD_LAN=$bad refused" "$TMP/out" && ok \
      || fail "($sh_) « $bad » pas refusé : $(cat "$TMP/.env" "$TMP/out")"
  done
done
printf '%s : %s contrôles, %s échec(s)\n' "${0##*/}" "$N" "$FAIL"
[ "$FAIL" -eq 0 ]
