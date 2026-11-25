#!/bin/sh
# install.sh --update : local.code.require_sandbox ajouté à un config.yaml gardé (modifié localement) qui ne l'a pas.
#   sh deploy/lokkal/tests/require-sandbox.sh        (sous sh, puis dash, bash et busybox sh s'ils sont là)
# Les fonctions sandbox_flag et ensure_require_sandbox sont lues dans install.sh (jamais recopiées) et jouées sur des
# copies du gabarit livré : (a) clé absente → une seule ligne ajoutée sous local.code, copie datée, rejouer ne change
# rien ; (b) clé déjà posée (true, puis false) → fichier intact, pas de copie ; (c) pas de bloc local.code → arrêt
# avec message, fichier intact, aucune copie ni fichier temporaire laissé ; (d) backend local et (e) racine de travail qui
# recouvre le dossier d'installation (parent, dedans, « / ») → avertissement, rien d'écrit ; racine voisine → clé ajoutée ;
# (f) noyau qui refuse au démarrage (faux compose) → copie remise et up relancé, seulement si CETTE exécution a posé la clé.
set -u
HERE=$(cd "$(dirname "$0")/.." && pwd)
FAIL=0; N=0
ok()   { N=$((N + 1)); }
fail() { N=$((N + 1)); FAIL=$((FAIL + 1)); printf 'FAIL %s\n' "$*"; }
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
sed -n '/^sandbox_flag() {/,/^ensure_require_sandbox$/p' "$HERE/install.sh" | sed '$d' > "$TMP/fns.sh"
grep -q '^ensure_require_sandbox() {' "$TMP/fns.sh" && ok || fail "fonctions introuvables dans install.sh"
TPL="$HERE/core-config.yaml"

run() { # run <dossier> : joue ensure_require_sandbox dans un sous-shell (die = exit 1), messages dans <dossier>/out
  ( DIR=$1
    msg() { _f=$1; shift; printf -- "$_f" "$@"; }
    say() { printf '%s\n' "$(msg "$@")"; }
    die() { printf 'DIE %s\n' "$(msg "$@")"; exit 1; }
    warn() { printf 'WARN %s\n' "$(msg "$@")"; }
    getv() { grep "^$1=" "$DIR/.env" 2>/dev/null | head -1 | cut -d= -f2-; }
    . "$TMP/fns.sh"; ensure_require_sandbox ) > "$1/out" 2>&1
}
nbak() { ls "$1/config" | grep -c 'before-require-sandbox' || true; }

for sh_ in sh dash bash "busybox sh"; do
  case "$sh_" in "busybox sh") command -v busybox >/dev/null || continue;; *) command -v "$sh_" >/dev/null || continue;; esac
  [ "$sh_" = sh ] || [ -n "${RS_INNER:-}" ] || { RS_INNER=1 $sh_ "$0" >/dev/null 2>&1 && ok || fail "sous $sh_ (relancer : $sh_ $0)"; continue; }
done

# (a) clé absente : config d'avant la clé (gabarit sans la ligne), modifiée localement comme le serait une vraie
a=$TMP/a; mkdir -p "$a/config"
grep -v '^    require_sandbox:' "$TPL" | sed 's/^log_level: .*/log_level: debug   # modifié localement/' > "$a/config/config.yaml"
cp "$a/config/config.yaml" "$TMP/a.avant"
run "$a" && ok || fail "(a) arrêt inattendu : $(cat "$a/out")"
[ "$(sed -n '/^    require_sandbox:/p' "$a/config/config.yaml" | wc -l)" -eq 1 ] && ok || fail "(a) clé pas ajoutée une fois"
[ "$(wc -l < "$a/config/config.yaml")" -eq $(( $(wc -l < "$TMP/a.avant") + 1 )) ] && ok || fail "(a) plus d'une ligne changée"
grep -v '^    require_sandbox:' "$a/config/config.yaml" | cmp -s - "$TMP/a.avant" && ok || fail "(a) autre chose a bougé"
awk '/^    backend:/ {b = NR} /^    require_sandbox:/ {r = NR} END {exit !(r == b + 1)}' "$a/config/config.yaml" && ok \
  || fail "(a) ligne pas placée juste après backend: dans local.code"
[ "$(nbak "$a")" -eq 1 ] && ok || fail "(a) copie datée absente"
cmp -s "$a/config/$(ls "$a/config" | grep before-require-sandbox)" "$TMP/a.avant" && ok || fail "(a) la copie n'est pas l'avant"
grep -q 'require_sandbox: true added' "$a/out" && ok || fail "(a) message manquant"
if command -v python3 >/dev/null && python3 -c 'import yaml' 2>/dev/null; then
  python3 -c 'import sys, yaml; sys.exit(0 if yaml.safe_load(open(sys.argv[1]))["local"]["code"]["require_sandbox"] is True else 1)' \
    "$a/config/config.yaml" && ok || fail "(a) YAML relu : local.code.require_sandbox n'est pas true"
fi
cp "$a/config/config.yaml" "$TMP/a.apres"; run "$a" && ok || fail "(a) 2e passage : arrêt"
cmp -s "$a/config/config.yaml" "$TMP/a.apres" && [ "$(nbak "$a")" -eq 1 ] && ok || fail "(a) 2e passage : fichier ou copies changés"

# (b) clé déjà posée : true (gabarit), puis false (choix de l'utilisateur) → jamais touchée
for v in true false; do
  b=$TMP/b-$v; mkdir -p "$b/config"
  sed "s/^    require_sandbox: true/    require_sandbox: $v/" "$TPL" > "$b/config/config.yaml"; cp "$b/config/config.yaml" "$TMP/b.avant"
  run "$b" && ok || fail "(b $v) arrêt inattendu"
  cmp -s "$b/config/config.yaml" "$TMP/b.avant" && [ "$(nbak "$b")" -eq 0 ] && ok || fail "(b $v) fichier touché ou copie faite"
done

# (c) pas de bloc local.code : arrêt avec message, rien d'écrit
c=$TMP/c; mkdir -p "$c/config"
printf 'local:\n  token_path: /opt/data/local-token\nlog_level: info\n' > "$c/config/config.yaml"; cp "$c/config/config.yaml" "$TMP/c.avant"
run "$c" && fail "(c) pas d'arrêt" || ok
grep -q '^DIE .*no local.code block' "$c/out" && ok || fail "(c) message d'arrêt manquant : $(cat "$c/out")"
cmp -s "$c/config/config.yaml" "$TMP/c.avant" && [ "$(ls "$c/config" | wc -l)" -eq 1 ] && ok || fail "(c) fichier touché ou reste laissé"

# (d) backend local : le noyau refuserait de démarrer avec la clé → avertissement, rien d'écrit, pas d'arrêt
d=$TMP/d; mkdir -p "$d/config"
grep -v '^    require_sandbox:' "$TPL" | sed 's/^    backend: docker/    backend: local/' > "$d/config/config.yaml"; cp "$d/config/config.yaml" "$TMP/d.avant"
grep -q '^    backend: local' "$d/config/config.yaml" && ok || fail "(d) gabarit sans « backend: docker » à remplacer"
run "$d" && ok || fail "(d) arrêt inattendu : $(cat "$d/out")"
cmp -s "$d/config/config.yaml" "$TMP/d.avant" && [ "$(nbak "$d")" -eq 0 ] && ok || fail "(d) fichier touché ou copie faite"
grep -q '^WARN .*backend local: sandbox not required' "$d/out" && ok || fail "(d) avertissement manquant : $(cat "$d/out")"

# (e) racine de travail qui recouvre le dossier d'installation (parent, dedans, « / ») → avertissement, rien d'écrit ;
#     racine séparée (dossier voisin) → clé ajoutée
for k in parent dedans racine voisin; do
  e=$TMP/e-$k/inst; mkdir -p "$e/config" "$e/state" "$TMP/e-$k/travail"
  grep -v '^    require_sandbox:' "$TPL" > "$e/config/config.yaml"; cp "$e/config/config.yaml" "$TMP/e.avant"
  case $k in parent) ws=$TMP/e-$k;; dedans) ws=$TMP/e-$k/travail:$e/state;; racine) ws=/;; voisin) ws=$TMP/e-$k/travail;; esac
  printf 'NOKKER_API_PORT=7420\nNOKKER_HOST_WORKSPACE=%s\n' "$ws" > "$e/.env"
  run "$e" && ok || fail "(e $k) arrêt inattendu : $(cat "$e/out")"
  if [ $k = voisin ]; then
    [ "$(sandbox_v=$(sed -n 's/^    require_sandbox: *\([a-z]*\).*/\1/p' "$e/config/config.yaml"); echo "$sandbox_v")" = true ] \
      && [ "$(nbak "$e")" -eq 1 ] && ok || fail "(e voisin) clé non ajoutée : $(cat "$e/out")"
  else
    cmp -s "$e/config/config.yaml" "$TMP/e.avant" && [ "$(nbak "$e")" -eq 0 ] && ok || fail "(e $k) fichier touché ou copie faite"
    grep -q '^WARN .*overlaps the install folder' "$e/out" && ok || fail "(e $k) avertissement manquant : $(cat "$e/out")"
  fi
done

# (f) filet SandboxRequired au démarrage (start_stack, rs_restore_if_refused) contre un FAUX compose : `up` échoue tant
#     que config.yaml porte la clé (le vrai noyau sort, jamais healthy → « dependency failed to start »), `logs` rend la
#     trace relevée sur l'image rc.9.33 (17.11). Pas de docker, pas de /opt monté.
REFUS='nokker.local.api.SandboxRequired: local.code.require_sandbox : backend « local » refusé (le modèle lirait le jeton local) ; utiliser backend docker ou kubernetes'
frun() { # frun <dossier> <journal du noyau> <RS_BAK posé par cette exécution : 1|0>
  ( DIR=$1; TMP=$1; PROJECT=nokker-test; LOGS=$2; UPS=0
    msg() { _f=$1; shift; printf -- "$_f" "$@"; }
    say() { printf '%s\n' "$(msg "$@")"; }
    die() { printf 'DIE %s\n' "$(msg "$@")"; exit 1; }
    warn() { printf 'WARN %s\n' "$(msg "$@")"; }
    getv() { :; }
    compose() { [ "$1" = logs ] && printf '%s\n' "$LOGS"; }
    up_kept() { UPS=$((UPS + 1)); echo "up $UPS" >> "$DIR/ups"
      if grep -q '^    require_sandbox:' "$DIR/config/config.yaml"; then echo 'dependency failed to start: container core is unhealthy' >&2; return 1; fi; }
    . "$TMP/../fns.sh"
    if [ "$3" = 1 ]; then RS_BAK=$DIR/config/avant.yaml; else RS_BAK=""; fi
    start_stack; echo "FIN rs_bak=[$RS_BAK]" ) > "$1/out" 2>&1
}
for k in refus autre pas-par-moi; do
  f=$TMP/f-$k; mkdir -p "$f/config"
  grep -v '^    require_sandbox:' "$TPL" > "$f/config/avant.yaml"; cp "$TPL" "$f/config/config.yaml"; : > "$f/ups"
  case $k in
  refus) frun "$f" "Traceback (most recent call last):
    raise SandboxRequired(\"local.code.require_sandbox : backend « local » refusé\"
$REFUS" 1
    grep -q '^FIN rs_bak=\[\]' "$f/out" && ok || fail "(f refus) start_stack n'a pas fini : $(cat "$f/out")"
    cmp -s "$f/config/config.yaml" "$f/config/avant.yaml" && ok || fail "(f refus) copie d'avant pas remise"
    [ "$(wc -l < "$f/ups")" -eq 2 ] && ok || fail "(f refus) up relancé $(wc -l < "$f/ups") fois au lieu de 2"
    grep -q '^WARN .*SandboxRequired.*restored' "$f/out" && ok || fail "(f refus) avertissement manquant";;
  autre) frun "$f" '/usr/local/bin/python3: No module named nokker' 1
    grep -q '^DIE .*start failed' "$f/out" && ok || fail "(f autre) pas d'arrêt : $(cat "$f/out")"
    cmp -s "$f/config/config.yaml" "$TPL" && ok || fail "(f autre) config touchée sans refus SandboxRequired"
    grep -q 'dependency failed' "$f/out" && ok || fail "(f autre) journal de compose pas montré";;
  pas-par-moi) frun "$f" "$REFUS" 0
    grep -q '^DIE .*start failed' "$f/out" && ok || fail "(f pas-par-moi) pas d'arrêt : $(cat "$f/out")"
    cmp -s "$f/config/config.yaml" "$TPL" && ok || fail "(f pas-par-moi) clé posée par l'utilisateur retirée";;
  esac
done

printf '%s : %s contrôles, %s échec(s)\n' "${0##*/}" "$N" "$FAIL"
[ "$FAIL" -eq 0 ]
