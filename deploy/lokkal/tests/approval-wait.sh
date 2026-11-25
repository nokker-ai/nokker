#!/bin/sh
# install.sh --update : local.approval_wait_s ajouté à un config.yaml gardé (modifié localement) qui ne l'a pas.
#   sh deploy/lokkal/tests/approval-wait.sh        (sous sh, puis dash, bash et busybox sh s'ils sont là)
# ensure_approval_wait est lue dans install.sh (jamais recopiée) et jouée sur des copies du gabarit livré : (a) clé absente
# → une seule ligne juste sous « local: », copie datée, rejouer ne change rien, le reste intact ; (b) clé déjà posée (10 du
# gabarit, puis 0 choisi par l'utilisateur) → fichier intact, pas de copie ; (c) le gabarit livré porte la clé ; sans bloc
# « local: » en clair : rien d'écrit, pas d'arrêt.
set -u
HERE=$(cd "$(dirname "$0")/.." && pwd)
FAIL=0; N=0
ok()   { N=$((N + 1)); }
fail() { N=$((N + 1)); FAIL=$((FAIL + 1)); printf 'FAIL %s\n' "$*"; }
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
sed -n '/^ensure_approval_wait() {/,/^ensure_approval_wait$/p' "$HERE/install.sh" | sed '$d' > "$TMP/fns.sh"
grep -q '^ensure_approval_wait() {' "$TMP/fns.sh" && ok || fail "fonction introuvable dans install.sh"
TPL="$HERE/core-config.yaml"

run() { # run <dossier> : joue ensure_approval_wait dans un sous-shell (die = exit 1), messages dans <dossier>/out
  ( DIR=$1
    msg() { _f=$1; shift; printf -- "$_f" "$@"; }
    say() { printf '%s\n' "$(msg "$@")"; }
    die() { printf 'DIE %s\n' "$(msg "$@")"; exit 1; }
    . "$TMP/fns.sh"; ensure_approval_wait ) > "$1/out" 2>&1
}
nbak() { ls "$1/config" | grep -c 'before-approval-wait' || true; }

for sh_ in sh dash bash "busybox sh"; do
  case "$sh_" in "busybox sh") command -v busybox >/dev/null || continue;; *) command -v "$sh_" >/dev/null || continue;; esac
  [ "$sh_" = sh ] || [ -n "${AW_INNER:-}" ] || { AW_INNER=1 $sh_ "$0" >/dev/null 2>&1 && ok || fail "sous $sh_ (relancer : $sh_ $0)"; continue; }
done

# (a) clé absente : config d'avant la clé, modifiée localement comme le serait une vraie
a=$TMP/a; mkdir -p "$a/config"
awk '/^  approval_wait_s:/ {skip = 1; next} skip && /^                                               #/ {next} {skip = 0; print}' "$TPL" \
  | sed 's/^log_level: .*/log_level: debug   # modifié localement/' > "$a/config/config.yaml"
! grep -q '^  approval_wait_s:' "$a/config/config.yaml" && ok || fail "(a) préparation : la clé est encore là"
cp "$a/config/config.yaml" "$TMP/a.avant"
run "$a" && ok || fail "(a) arrêt inattendu : $(cat "$a/out")"
[ "$(sed -n '/^  approval_wait_s:/p' "$a/config/config.yaml" | wc -l)" -eq 1 ] && ok || fail "(a) clé pas ajoutée une fois"
[ "$(wc -l < "$a/config/config.yaml")" -eq $(( $(wc -l < "$TMP/a.avant") + 1 )) ] && ok || fail "(a) plus d'une ligne changée"
grep -v '^  approval_wait_s:' "$a/config/config.yaml" | cmp -s - "$TMP/a.avant" && ok || fail "(a) autre chose a bougé"
awk '/^local:/ {b = NR} /^  approval_wait_s:/ {r = NR} END {exit !(r == b + 1)}' "$a/config/config.yaml" && ok \
  || fail "(a) ligne pas placée juste après local:"
[ "$(nbak "$a")" -eq 1 ] && cmp -s "$a/config/$(ls "$a/config" | grep before-approval-wait)" "$TMP/a.avant" && ok || fail "(a) copie datée absente ou différente"
grep -q 'local.approval_wait_s: 10 added' "$a/out" && ok || fail "(a) message manquant"
if command -v python3 >/dev/null && python3 -c 'import yaml' 2>/dev/null; then
  python3 -c 'import sys, yaml; sys.exit(0 if yaml.safe_load(open(sys.argv[1]))["local"]["approval_wait_s"] == 10 else 1)' \
    "$a/config/config.yaml" && ok || fail "(a) YAML relu : local.approval_wait_s n'est pas 10"
fi
cp "$a/config/config.yaml" "$TMP/a.apres"; run "$a" && ok || fail "(a) 2e passage : arrêt"
cmp -s "$a/config/config.yaml" "$TMP/a.apres" && [ "$(nbak "$a")" -eq 1 ] && ok || fail "(a) 2e passage : fichier ou copies changés"

# (b) clé déjà posée : 10 (gabarit), puis 0 (choix de l'utilisateur : éteint) → jamais touchée
for v in 10 0; do
  b=$TMP/b-$v; mkdir -p "$b/config"
  sed "s/^  approval_wait_s: 10 /  approval_wait_s: $v /" "$TPL" > "$b/config/config.yaml"; cp "$b/config/config.yaml" "$TMP/b.avant"
  run "$b" && ok || fail "(b $v) arrêt inattendu"
  cmp -s "$b/config/config.yaml" "$TMP/b.avant" && [ "$(nbak "$b")" -eq 0 ] && ok || fail "(b $v) fichier touché ou copie faite"
done
grep -q '^  approval_wait_s: 0 ' "$TMP/b-0/config/config.yaml" && ok || fail "(b) préparation : 0 pas posé"

# (c) le gabarit livré ; sans bloc local: en clair, rien d'écrit
[ "$(sed -n '/^  approval_wait_s:/p' "$TPL" | wc -l)" -eq 1 ] && ok || fail "(c) gabarit : approval_wait_s absent ou en double"
d=$TMP/d; mkdir -p "$d/config"; printf 'log_level: info\n' > "$d/config/config.yaml"
run "$d" && [ "$(cat "$d/config/config.yaml")" = "log_level: info" ] && [ "$(nbak "$d")" -eq 0 ] && ok || fail "(c) sans bloc local: fichier changé ou arrêt"
e=$TMP/e; mkdir -p "$e/config"; printf 'local: {max_calls: 5}\n' > "$e/config/config.yaml"
run "$e" && [ "$(cat "$e/config/config.yaml")" = "local: {max_calls: 5}" ] && ok || fail "(c) local: compact : fichier changé ou arrêt"

[ "$FAIL" -eq 0 ] && printf 'approval-wait : %s contrôles OK\n' "$N" || { printf 'approval-wait : %s FAIL sur %s\n' "$FAIL" "$N"; exit 1; }
