#!/bin/sh
# install.sh --update : tiers.small.model réécrit de l'ancien 35B vers le 27B dans un config.yaml gardé (modifié localement).
#   sh deploy/lokkal/tests/small-27b.sh
# Les fonctions small_model et ensure_small_27b sont lues dans install.sh (jamais recopiées) et jouées sur des copies du
# gabarit livré : (a) ancienne valeur exacte → une seule ligne change, copie datée, rejouer ne change rien ; (b) autre
# valeur (27B, modèle BYOK) → fichier intact, pas de copie ; (c) pas de palier small → rien, pas d'arrêt.
set -u
HERE=$(cd "$(dirname "$0")/.." && pwd)
FAIL=0; N=0
ok()   { N=$((N + 1)); }
fail() { N=$((N + 1)); FAIL=$((FAIL + 1)); printf 'FAIL %s\n' "$*"; }
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
sed -n '/^SMALL_OLD=/,/^ensure_small_27b$/p' "$HERE/install.sh" | sed '$d' > "$TMP/fns.sh"
grep -q '^ensure_small_27b() {' "$TMP/fns.sh" && ok || fail "fonctions introuvables dans install.sh"
TPL="$HERE/core-config.yaml"
OLD=qwen/qwen3.6-35b-a3b-eu; NEW=qwen/qwen3.8-27b

run() { # run <dossier> : joue ensure_small_27b dans un sous-shell (die = exit 1), messages dans <dossier>/out
  ( DIR=$1
    msg() { _f=$1; shift; printf -- "$_f" "$@"; }
    say() { printf '%s\n' "$(msg "$@")"; }
    die() { printf 'DIE %s\n' "$(msg "$@")"; exit 1; }
    warn() { printf 'WARN %s\n' "$(msg "$@")"; }
    . "$TMP/fns.sh"; ensure_small_27b ) > "$1/out" 2>&1
}
nbak() { ls "$1/config" | grep -c 'before-small-27b' || true; }
small_of() { awk '/^[^ #]/ {t = ($0 ~ /^tiers:/); s = 0} t && /^  [^ #]/ {s = ($0 ~ /^  small:/)}
                  t && s && /^    model:/ {sub(/^    model:[ ]*/, ""); sub(/[ ]*#.*/, ""); print; exit}' "$1"; }

[ "$(small_of "$TPL")" = "$NEW" ] && ok || fail "le gabarit livré n'a pas $NEW en tiers.small.model"

# (a) ancienne valeur exacte, config modifiée localement comme le serait une vraie
a=$TMP/a; mkdir -p "$a/config"
awk -v old="$OLD" '/^[^ #]/ {t = ($0 ~ /^tiers:/); s = 0} t && /^  [^ #]/ {s = ($0 ~ /^  small:/)}
     t && s && /^    model:/ {print "    model: " old; next} {print}' "$TPL" \
  | sed 's/^log_level: .*/log_level: debug   # modifié localement/' > "$a/config/config.yaml"
cp "$a/config/config.yaml" "$TMP/a.avant"
[ "$(small_of "$a/config/config.yaml")" = "$OLD" ] && ok || fail "(a) préparation : ancienne valeur absente"
run "$a" && ok || fail "(a) arrêt inattendu : $(cat "$a/out")"
[ "$(small_of "$a/config/config.yaml")" = "$NEW" ] && ok || fail "(a) valeur pas réécrite"
[ "$(diff "$TMP/a.avant" "$a/config/config.yaml" | grep -c '^[<>]')" -eq 2 ] && ok || fail "(a) plus d'une ligne changée"
[ "$(nbak "$a")" -eq 1 ] && ok || fail "(a) copie datée absente"
cmp -s "$a/config/$(ls "$a/config" | grep before-small-27b)" "$TMP/a.avant" && ok || fail "(a) la copie n'est pas l'avant"
grep -q "tiers.small.model $OLD -> $NEW" "$a/out" && ok || fail "(a) message manquant"
if command -v python3 >/dev/null && python3 -c 'import yaml' 2>/dev/null; then
  python3 -c 'import sys, yaml; sys.exit(0 if yaml.safe_load(open(sys.argv[1]))["tiers"]["small"]["model"] == sys.argv[2] else 1)' \
    "$a/config/config.yaml" "$NEW" && ok || fail "(a) YAML relu : tiers.small.model n'est pas $NEW"
fi
cp "$a/config/config.yaml" "$TMP/a.apres"; run "$a" && ok || fail "(a) 2e passage : arrêt"
cmp -s "$a/config/config.yaml" "$TMP/a.apres" && [ "$(nbak "$a")" -eq 1 ] && ok || fail "(a) 2e passage : fichier ou copies changés"

# (b) autre valeur : le 27B (gabarit), puis un modèle BYOK → jamais touchée
for v in "$NEW" byok/mon-modele; do
  b=$TMP/b-$(echo "$v" | tr '/' '_'); mkdir -p "$b/config"
  awk -v v="$v" '/^[^ #]/ {t = ($0 ~ /^tiers:/); s = 0} t && /^  [^ #]/ {s = ($0 ~ /^  small:/)}
       t && s && /^    model:/ {print "    model: " v; next} {print}' "$TPL" > "$b/config/config.yaml"
  cp "$b/config/config.yaml" "$TMP/b.avant"
  run "$b" && ok || fail "(b $v) arrêt inattendu"
  cmp -s "$b/config/config.yaml" "$TMP/b.avant" && [ "$(nbak "$b")" -eq 0 ] && ok || fail "(b $v) fichier touché ou copie faite"
done

# (c) pas de palier small : rien, pas d'arrêt
c=$TMP/c; mkdir -p "$c/config"
printf 'tiers:\n  ship:\n    model: %s\nlog_level: info\n' "$NEW" > "$c/config/config.yaml"; cp "$c/config/config.yaml" "$TMP/c.avant"
run "$c" && ok || fail "(c) arrêt inattendu"
cmp -s "$c/config/config.yaml" "$TMP/c.avant" && [ "$(ls "$c/config" | wc -l)" -eq 1 ] && ok || fail "(c) fichier touché ou reste laissé"

printf '%s : %d contrôles, %d échec(s)\n' "$(basename "$0")" "$N" "$FAIL"
[ "$FAIL" -eq 0 ]
