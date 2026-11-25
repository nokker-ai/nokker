#!/bin/sh
# install.sh --update : ménage des bacs de l'ANCIENNE étiquette (sha256("/opt/data")[:12], commune à tous les Lokkal)
# de CETTE installation seulement — réseau <projet>-sandbox de l'installation —, liste affichée AVANT la suppression.
#   sh deploy/lokkal/tests/legacy-reap.sh      (sous sh, dash, bash s'ils sont là)
# legacy_reap est lue dans install.sh (jamais recopiée) et jouée contre un FAUX docker volontairement laxiste (filtres
# ps par préfixe et sous-chaîne) : seul le re-contrôle exact d'install.sh protège les bacs des autres installations.
set -u
HERE=$(cd "$(dirname "$0")/.." && pwd)
FAIL=0; N=0
ok()   { N=$((N + 1)); }
fail() { N=$((N + 1)); FAIL=$((FAIL + 1)); printf 'FAIL %s\n' "$*"; }
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
sed -n '/^legacy_reap() {/,/^}/p' "$HERE/install.sh" > "$TMP/fns.sh"
grep -q '^legacy_reap() {' "$TMP/fns.sh" && ok || fail "legacy_reap introuvable dans install.sh"
LEG=$(printf %s /opt/data | sha256sum | cut -c1-12)
[ "$LEG" = b7dd404ef0fc ] && ok || fail "valeur legacy $LEG (attendue b7dd404ef0fc, relevée sur lokkal-test1)"
mkdir -p "$TMP/bin"
cat > "$TMP/bin/docker" <<'FAKE'
#!/bin/sh
# faux docker : $FAKE_DB = lignes « id étiquette réseau nom » ; ps LAXISTE (préfixe d'étiquette, sous-chaîne de réseau)
case "$1" in
ps) lab=""; net=""
    for a in "$@"; do case "$a" in label=nokker.instance=*) lab=${a#label=nokker.instance=};; network=*) net=${a#network=};; esac; done
    while read -r id l n nm; do case "$l" in "$lab"*) case "$n" in *"$net"*) echo "$id";; esac;; esac; done < "$FAKE_DB";;
inspect) f=$3; id=$4; line=$(grep "^$id " "$FAKE_DB"); set -- $line
    case "$f" in *nokker.instance*) echo "$2";; *Networks*) echo "$3 ";; *.Name*) echo "/$4";; esac;;
rm) shift; [ "$1" = -f ] && shift; echo "RM $*" >> "$FAKE_RM"
    grep -c "^SAY" "$FAKE_OUT" > "$FAKE_RM.say";;   # nombre de lignes SAY déjà écrites au moment du rm
esac
FAKE
chmod +x "$TMP/bin/docker"
cat > "$TMP/db" <<EOF2
a1 $LEG nokker-ubuntu-sandbox bac-a-moi
b2 $LEG nokker-bob-sandbox bac-de-bob
c3 0123456789abcdef nokker-ubuntu-sandbox bac-nouvelle-etiquette
d4 ${LEG}ff nokker-ubuntu-sandbox bac-etiquette-plus-longue
e5 $LEG nokker-ubuntu-sandbox2 bac-reseau-voisin
EOF2
run() { # run <sh> <MODE> <NOKKER_INSTANCE> <PROJECT> <SANDBOX_NET> [local.instance]
  d=$TMP/run; rm -rf "$d"; mkdir -p "$d/config"; : > "$TMP/rm"
  printf 'NOKKER_VERSION=x\n%s\n' "${3:+NOKKER_INSTANCE=$3}" > "$d/.env"
  printf 'local:\n  token_path: /opt/data/local-token\n%s  code:\n    backend: docker\n' "${6:+  instance: $6
}" > "$d/config/config.yaml"
  rm -f "$TMP/rm.say"; PATH=$TMP/bin:$PATH FAKE_DB=$TMP/db FAKE_RM=$TMP/rm FAKE_OUT=$TMP/out "$1" -c '
    DIR=$1; MODE=$2; PROJECT=$3; SANDBOX_NET=$4
    msg() { _f=$1; shift; printf -- "$_f" "$@"; }; say() { printf "SAY %s\n" "$(msg "$@")"; }; warn() { printf "WARN %s\n" "$(msg "$@")"; }
    getv() { grep "^$1=" "$DIR/.env" | head -1 | cut -d= -f2-; }
    . "$5"; legacy_reap' x "$d" "$2" "$4" "$5" "$TMP/fns.sh" > "$TMP/out" 2>&1
}
for sh_ in sh dash bash; do
  command -v "$sh_" >/dev/null || continue
  # (a) mise à jour : SEUL bac-a-moi part ; liste affichée AVANT le rm
  run "$sh_" update 0123456789abcdef nokker-ubuntu nokker-ubuntu-sandbox
  [ "$(cat "$TMP/rm")" = "RM a1" ] && ok || fail "($sh_ a) supprimés : « $(cat "$TMP/rm") » au lieu de « RM a1 »"
  grep -q "^SAY .*removed: bac-a-moi$" "$TMP/out" && ok || fail "($sh_ a) liste pas affichée : $(cat "$TMP/out")"
  [ "$(cat "$TMP/rm.say" 2>/dev/null)" = 1 ] && ok || fail "($sh_ a) rm lancé avant que la liste soit affichée"
  # (b) installation neuve, (c) NOKKER_INSTANCE absente, (d) égale à l'ancienne valeur, (e) local.instance posé : rien
  run "$sh_" install 0123456789abcdef nokker-ubuntu nokker-ubuntu-sandbox
  [ ! -s "$TMP/rm" ] && [ ! -s "$TMP/out" ] && ok || fail "($sh_ b) installation neuve : $(cat "$TMP/out")"
  run "$sh_" update "" nokker-ubuntu nokker-ubuntu-sandbox
  [ ! -s "$TMP/rm" ] && ok || fail "($sh_ c) sans NOKKER_INSTANCE : $(cat "$TMP/out")"
  run "$sh_" update "$LEG" nokker-ubuntu nokker-ubuntu-sandbox
  [ ! -s "$TMP/rm" ] && ok || fail "($sh_ d) NOKKER_INSTANCE = ancienne valeur : $(cat "$TMP/out")"
  run "$sh_" update 0123456789abcdef nokker-ubuntu nokker-ubuntu-sandbox "$LEG"
  [ ! -s "$TMP/rm" ] && ok || fail "($sh_ e) local.instance posé : $(cat "$TMP/out")"
  # (f) ancien réseau commun nokker-sandbox : projet ≠ nokker → signalés, rien supprimé ; projet nokker → supprimés
  sed 's/nokker-ubuntu-sandbox /nokker-sandbox /' "$TMP/db" > "$TMP/db2"; cp "$TMP/db" "$TMP/db1"; cp "$TMP/db2" "$TMP/db"
  run "$sh_" update 0123456789abcdef nokker-ubuntu nokker-sandbox
  [ ! -s "$TMP/rm" ] && grep -q '^WARN .*shared network nokker-sandbox, not removed' "$TMP/out" && ok || fail "($sh_ f) réseau commun, autre projet : $(cat "$TMP/out")"
  run "$sh_" update 0123456789abcdef nokker nokker-sandbox
  [ "$(cat "$TMP/rm")" = "RM a1" ] && ok || fail "($sh_ f) réseau commun, projet nokker : « $(cat "$TMP/rm") »"
  cp "$TMP/db1" "$TMP/db"
  # (g) aucun bac legacy : rien, sans message
  printf 'c3 0123456789abcdef nokker-ubuntu-sandbox x\n' > "$TMP/db"
  run "$sh_" update 0123456789abcdef nokker-ubuntu nokker-ubuntu-sandbox
  [ ! -s "$TMP/rm" ] && [ ! -s "$TMP/out" ] && ok || fail "($sh_ g) sans bac legacy : $(cat "$TMP/out")"
  cp "$TMP/db1" "$TMP/db"
done
grep -q '^legacy_reap$' "$HERE/install.sh" && ok || fail "install.sh n'appelle pas legacy_reap"
printf '%s : %s contrôles, %s échec(s)\n' "${0##*/}" "$N" "$FAIL"
[ "$FAIL" -eq 0 ]
