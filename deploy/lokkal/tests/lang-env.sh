#!/bin/sh
# NOKKER_LANG (langue de repli du NOYAU) : install.sh l'écrit dans .env SEULEMENT s'il est absent, d'après --lang, sinon
# NOKKER_LANG/LC_ALL/LC_MESSAGES/LANG d'origine (fr, en, de, it ; anglais sinon) ; le compose le passe au core.
#   sh deploy/lokkal/tests/lang-env.sh      (sous sh, dash, bash s'ils sont là)
# Le bloc de langue et la ligne d'écriture sont lus dans install.sh (jamais recopiés).
set -u
HERE=$(cd "$(dirname "$0")/.." && pwd)
FAIL=0; N=0
ok()   { N=$((N + 1)); }
fail() { N=$((N + 1)); FAIL=$((FAIL + 1)); printf 'FAIL %s\n' "$*"; }
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
sed -n '/^lang_of() {/,/^NOKKER_LANG=\$L; NOKKER_INSTALL_KLANG=/p' "$HERE/install.sh" > "$TMP/lang.sh"
grep '^\[ -n "\$(getv NOKKER_LANG)" \] || setv NOKKER_LANG "\$KLANG"$' "$HERE/install.sh" > "$TMP/ensure.sh"
grep -q klang_of "$TMP/lang.sh" && [ -s "$TMP/ensure.sh" ] && ok || fail "bloc de langue ou écriture introuvables dans install.sh"
run() { # run <sh> <contenu .env initial> <env…> -- [args install.sh] : écrit NOKKER_LANG du .env final dans $TMP/res
  sh_=$1; init=$2; shift 2
  printf '%s' "$init" > "$TMP/.env"
  envs=""; while [ "$#" -gt 0 ] && [ "$1" != -- ]; do envs="$envs $1"; shift; done; [ "${1:-}" = -- ] && shift
  env -i PATH="$PATH" $envs "$sh_" -c '
    DIR=$0
    getv() { grep "^$1=" "$DIR/.env" 2>/dev/null | head -1 | cut -d= -f2-; }
    setv() { if grep -q "^$1=" "$DIR/.env"; then sed -i.bak "s|^$1=.*|$1=$2|" "$DIR/.env"; else printf "%s=%s\n" "$1" "$2" >> "$DIR/.env"; fi; }
    . "$DIR/lang.sh"; . "$DIR/ensure.sh"; printf "%s" "$NOKKER_INSTALL_KLANG" > "$DIR/klang"' "$TMP" "$@"
  grep '^NOKKER_LANG=' "$TMP/.env" | cut -d= -f2 > "$TMP/res"; grep -c '^NOKKER_LANG=' "$TMP/.env" > "$TMP/count"
}
for sh_ in sh dash bash; do
  command -v "$sh_" >/dev/null || continue
  chk() { [ "$(cat "$TMP/res")" = "$1" ] && [ "$(cat "$TMP/count")" = 1 ] && ok || fail "($sh_ $2) NOKKER_LANG=« $(cat "$TMP/res") » ×$(cat "$TMP/count"), attendu « $1 » ×1"; }
  run "$sh_" "" LANG=de_CH.UTF-8;                 chk de "installation neuve, LANG=de_CH"
  run "$sh_" "" LANG=fr_CH.UTF-8 -- --lang en;    chk en "installation neuve, --lang en l'emporte"
  run "$sh_" "" LC_ALL=it_IT.UTF-8 LANG=fr_FR;    chk it "LC_ALL avant LANG"
  run "$sh_" "" LANG=C;                           chk en "locale C → en"
  run "$sh_" "" ;                                 chk en "aucune locale → en"
  run "$sh_" "NOKKER_VERSION=x
NOKKER_LANG=it
" LANG=fr_FR.UTF-8 -- --lang fr;                  chk it "MAJ : valeur existante gardée"
  # relance (groupe docker, installeur de la version cible) : NOKKER_LANG=en|fr, la langue du noyau passe par NOKKER_INSTALL_KLANG
  run "$sh_" "" NOKKER_LANG=en NOKKER_INSTALL_KLANG=de LANG=de_DE; chk de "relance : NOKKER_INSTALL_KLANG gardé"
  run "$sh_" "" LANG=de_DE.UTF-8; [ "$(cat "$TMP/klang")" = de ] && ok || fail "($sh_) NOKKER_INSTALL_KLANG pas exporté pour les relances"
done
# compose : le core reçoit NOKKER_LANG, valeur entre guillemets (un « : » dans ${…:-…} casserait le YAML, cf. rc.9.34)
grep -q '^      NOKKER_LANG: "\${NOKKER_LANG:-en}"$' "$HERE/docker-compose.yml" && ok || fail "compose : NOKKER_LANG absent du core ou non quoté"
if command -v python3 >/dev/null && python3 -c 'import yaml' 2>/dev/null; then
  python3 -c 'import sys, yaml; sys.exit(yaml.safe_load(open(sys.argv[1]))["services"]["core"]["environment"]["NOKKER_LANG"] != "${NOKKER_LANG:-en}")' \
    "$HERE/docker-compose.yml" && ok || fail "compose : core.environment.NOKKER_LANG mal relu"
fi
printf '%s : %s contrôles, %s échec(s)\n' "${0##*/}" "$N" "$FAIL"
[ "$FAIL" -eq 0 ]
