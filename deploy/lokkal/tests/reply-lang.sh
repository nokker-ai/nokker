#!/bin/sh
# install.sh --update : reply_language et reply_language_tail posés à true dans la config de Lokkal SI ABSENTS (jamais une
# valeur déjà posée, même false), clés de premier niveau ajoutées en fin de fichier, copie datée ; rejouer ne change rien.
# Le gabarit livré (installation neuve) les porte déjà à true. ensure_reply_lang est lue dans install.sh (jamais recopiée).
#   sh deploy/lokkal/tests/reply-lang.sh      (sous sh, dash, bash s'ils sont là)
set -u
HERE=$(cd "$(dirname "$0")/.." && pwd)
FAIL=0; N=0
ok()   { N=$((N + 1)); }
fail() { N=$((N + 1)); FAIL=$((FAIL + 1)); printf 'FAIL %s\n' "$*"; }
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
TPL="$HERE/core-config.yaml"
sed -n '/^ensure_reply_lang() {/,/^}/p' "$HERE/install.sh" > "$TMP/fns.sh"
grep -q '^ensure_reply_lang() {' "$TMP/fns.sh" && ok || fail "ensure_reply_lang introuvable dans install.sh"
grep -q '^ensure_reply_lang$' "$HERE/install.sh" && ok || fail "install.sh n'appelle pas ensure_reply_lang"
grep -q '^reply_language: true' "$TPL" && grep -q '^reply_language_tail: true' "$TPL" && ok || fail "gabarit sans les deux clés à true"
run() { # run <sh> <dossier>
  "$1" -c 'DIR=$0; msg() { _f=$1; shift; printf -- "$_f" "$@"; }; say() { printf "SAY %s\n" "$(msg "$@")"; }
    die() { printf "DIE %s\n" "$(msg "$@")"; exit 1; }; . "$DIR/../fns.sh"; ensure_reply_lang' "$2" > "$2/out" 2>&1
}
nbak() { ls "$1/config" | grep -c before-reply-lang || true; }
yaml_ok() { python3 -c 'import sys, yaml; d = yaml.safe_load(open(sys.argv[1])); sys.exit(0 if (d["reply_language"], d["reply_language_tail"]) == (True, True) else 1)' "$1" 2>/dev/null; }
for sh_ in sh dash bash; do
  command -v "$sh_" >/dev/null || continue
  # (a) config gardée d'avant les clés : les deux ajoutées, deux lignes en plus, le reste identique, copie datée
  d=$TMP/a-$sh_; mkdir -p "$d/config"; grep -vE '^reply_language(_tail)?:' "$TPL" > "$d/config/config.yaml"; cp "$d/config/config.yaml" "$d/avant"
  run "$sh_" "$d"
  [ "$(wc -l < "$d/config/config.yaml")" -eq $(( $(wc -l < "$d/avant") + 2 )) ] && ok || fail "($sh_ a) pas exactement 2 lignes en plus"
  grep -vE '^reply_language(_tail)?: true' "$d/config/config.yaml" | cmp -s - "$d/avant" && ok || fail "($sh_ a) autre chose a bougé"
  [ "$(nbak "$d")" -eq 1 ] && grep -q '^SAY config/config.yaml: reply_language, reply_language_tail: true added' "$d/out" && ok \
    || fail "($sh_ a) copie ou message : $(cat "$d/out")"
  if command -v python3 >/dev/null && python3 -c 'import yaml' 2>/dev/null; then yaml_ok "$d/config/config.yaml" && ok || fail "($sh_ a) YAML relu faux"; fi
  # (b) rejouer : rien ne change
  cp "$d/config/config.yaml" "$d/apres"; run "$sh_" "$d"
  cmp -s "$d/config/config.yaml" "$d/apres" && [ "$(nbak "$d")" -eq 1 ] && [ ! -s "$d/out" ] && ok || fail "($sh_ b) rejouer a changé quelque chose"
  # (c) valeur déjà posée à false par la personne : jamais touchée ; l'autre clé absente : ajoutée seule
  d=$TMP/c-$sh_; mkdir -p "$d/config"; grep -vE '^reply_language(_tail)?:' "$TPL" > "$d/config/config.yaml"
  echo 'reply_language: false   # choix de la personne' >> "$d/config/config.yaml"
  run "$sh_" "$d"
  grep -q '^reply_language: false   # choix de la personne$' "$d/config/config.yaml" && grep -q '^reply_language_tail: true' "$d/config/config.yaml" \
    && [ "$(grep -c '^reply_language' "$d/config/config.yaml")" -eq 2 ] && ok || fail "($sh_ c) false écrasé ou clé en double : $(grep '^reply' "$d/config/config.yaml")"
  # (d) fichier sans saut de ligne final : un saut ajouté, aucune clé collée à la dernière ligne
  d=$TMP/d-$sh_; mkdir -p "$d/config"; printf 'log_level: info' > "$d/config/config.yaml"
  run "$sh_" "$d"
  [ "$(sed -n 1p "$d/config/config.yaml")" = "log_level: info" ] && grep -q '^reply_language: true' "$d/config/config.yaml" && ok \
    || fail "($sh_ d) clé collée : $(cat "$d/config/config.yaml")"
  # (e) gabarit neuf (déjà à true) : rien
  d=$TMP/e-$sh_; mkdir -p "$d/config"; cp "$TPL" "$d/config/config.yaml"; run "$sh_" "$d"
  cmp -s "$d/config/config.yaml" "$TPL" && [ "$(nbak "$d")" -eq 0 ] && ok || fail "($sh_ e) gabarit touché"
done
printf '%s : %s contrôles, %s échec(s)\n' "${0##*/}" "$N" "$FAIL"
[ "$FAIL" -eq 0 ]
