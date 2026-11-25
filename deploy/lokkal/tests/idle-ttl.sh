#!/bin/sh
# install.sh --update : local.code.idle_ttl_s ajouté à un config.yaml gardé (modifié localement) qui ne l'a pas.
#   sh deploy/lokkal/tests/idle-ttl.sh        (sous sh, puis dash, bash et busybox sh s'ils sont là)
# ensure_idle_ttl est lue dans install.sh (jamais recopiée) et jouée sur des copies du gabarit livré : (a) clé absente →
# une seule ligne sous local.code, copie datée, rejouer ne change rien, require_sandbox et network_name intacts ;
# (b) clé déjà posée (1800 du gabarit, puis 0 choisi par l'utilisateur) → fichier intact, pas de copie ; (c) le gabarit
# livré porte la clé et la ligne network_name reste sans commentaire (sed de l'ancien installeur).
set -u
HERE=$(cd "$(dirname "$0")/.." && pwd)
FAIL=0; N=0
ok()   { N=$((N + 1)); }
fail() { N=$((N + 1)); FAIL=$((FAIL + 1)); printf 'FAIL %s\n' "$*"; }
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
{ sed -n '/^sandbox_flag() {/,/^}/p' "$HERE/install.sh"; sed -n '/^ensure_idle_ttl() {/,/^ensure_idle_ttl$/p' "$HERE/install.sh" | sed '$d'; } > "$TMP/fns.sh"
grep -q '^ensure_idle_ttl() {' "$TMP/fns.sh" && grep -q '^sandbox_flag() {' "$TMP/fns.sh" && ok || fail "fonctions introuvables dans install.sh"
TPL="$HERE/core-config.yaml"

run() { # run <dossier> : joue ensure_idle_ttl dans un sous-shell (die = exit 1), messages dans <dossier>/out
  ( DIR=$1
    msg() { _f=$1; shift; printf -- "$_f" "$@"; }
    say() { printf '%s\n' "$(msg "$@")"; }
    die() { printf 'DIE %s\n' "$(msg "$@")"; exit 1; }
    warn() { printf 'WARN %s\n' "$(msg "$@")"; }
    . "$TMP/fns.sh"; ensure_idle_ttl ) > "$1/out" 2>&1
}
nbak() { ls "$1/config" | grep -c 'before-idle-ttl' || true; }

for sh_ in sh dash bash "busybox sh"; do
  case "$sh_" in "busybox sh") command -v busybox >/dev/null || continue;; *) command -v "$sh_" >/dev/null || continue;; esac
  [ "$sh_" = sh ] || [ -n "${IT_INNER:-}" ] || { IT_INNER=1 $sh_ "$0" >/dev/null 2>&1 && ok || fail "sous $sh_ (relancer : $sh_ $0)"; continue; }
done

# (a) clé absente : config d'avant la clé, modifiée localement comme le serait une vraie
a=$TMP/a; mkdir -p "$a/config"
awk '/^    idle_ttl_s:/ {skip = 1; next} skip && /^                                               #/ {next} {skip = 0; print}' "$TPL" \
  | sed 's/^log_level: .*/log_level: debug   # modifié localement/' > "$a/config/config.yaml"
! grep -q '^    idle_ttl_s:' "$a/config/config.yaml" && ok || fail "(a) préparation : la clé est encore là"
cp "$a/config/config.yaml" "$TMP/a.avant"
run "$a" && ok || fail "(a) arrêt inattendu : $(cat "$a/out")"
[ "$(sed -n '/^    idle_ttl_s:/p' "$a/config/config.yaml" | wc -l)" -eq 1 ] && ok || fail "(a) clé pas ajoutée une fois"
[ "$(wc -l < "$a/config/config.yaml")" -eq $(( $(wc -l < "$TMP/a.avant") + 1 )) ] && ok || fail "(a) plus d'une ligne changée"
grep -v '^    idle_ttl_s:' "$a/config/config.yaml" | cmp -s - "$TMP/a.avant" && ok || fail "(a) autre chose a bougé"
awk '/^    backend:/ {b = NR} /^    idle_ttl_s:/ {r = NR} END {exit !(r == b + 1)}' "$a/config/config.yaml" && ok \
  || fail "(a) ligne pas placée juste après backend: dans local.code"
grep -qx '    network_name: nokker-sandbox' "$a/config/config.yaml" && ok || fail "(a) network_name touchée"
[ "$(nbak "$a")" -eq 1 ] && cmp -s "$a/config/$(ls "$a/config" | grep before-idle-ttl)" "$TMP/a.avant" && ok || fail "(a) copie datée absente ou différente"
grep -q 'idle_ttl_s: 1800 added' "$a/out" && ok || fail "(a) message manquant"
if command -v python3 >/dev/null && python3 -c 'import yaml' 2>/dev/null; then
  python3 -c 'import sys, yaml; sys.exit(0 if yaml.safe_load(open(sys.argv[1]))["local"]["code"]["idle_ttl_s"] == 1800 else 1)' \
    "$a/config/config.yaml" && ok || fail "(a) YAML relu : local.code.idle_ttl_s n'est pas 1800"
fi
cp "$a/config/config.yaml" "$TMP/a.apres"; run "$a" && ok || fail "(a) 2e passage : arrêt"
cmp -s "$a/config/config.yaml" "$TMP/a.apres" && [ "$(nbak "$a")" -eq 1 ] && ok || fail "(a) 2e passage : fichier ou copies changés"

# (b) clé déjà posée : 1800 (gabarit), puis 0 (choix de l'utilisateur : jamais) → jamais touchée
for v in 1800 0; do
  b=$TMP/b-$v; mkdir -p "$b/config"
  sed "s/^    idle_ttl_s: 1800 /    idle_ttl_s: $v /" "$TPL" > "$b/config/config.yaml"; cp "$b/config/config.yaml" "$TMP/b.avant"
  run "$b" && ok || fail "(b $v) arrêt inattendu"
  cmp -s "$b/config/config.yaml" "$TMP/b.avant" && [ "$(nbak "$b")" -eq 0 ] && ok || fail "(b $v) fichier touché ou copie faite"
done
grep -q '^    idle_ttl_s: 0 ' "$TMP/b-0/config/config.yaml" && ok || fail "(b) préparation : 0 pas posé"

# (c) le gabarit livré
[ "$(sed -n '/^    idle_ttl_s:/p' "$TPL" | wc -l)" -eq 1 ] && ok || fail "(c) gabarit : idle_ttl_s absent ou en double"
grep -qx '    network_name: nokker-sandbox' "$TPL" && ok || fail "(c) gabarit : la ligne network_name doit rester sans commentaire"
# sans bloc local.code : rien d'écrit, pas d'arrêt (ensure_require_sandbox s'arrête déjà là-dessus)
d=$TMP/d; mkdir -p "$d/config"; printf 'log_level: info\n' > "$d/config/config.yaml"
run "$d" && [ "$(cat "$d/config/config.yaml")" = "log_level: info" ] && [ "$(nbak "$d")" -eq 0 ] && ok || fail "(c) sans bloc local.code : fichier changé ou arrêt"

[ "$FAIL" -eq 0 ] && printf 'idle-ttl : %s contrôles OK\n' "$N" || { printf 'idle-ttl : %s FAIL sur %s\n' "$FAIL" "$N"; exit 1; }
