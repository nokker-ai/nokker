#!/bin/sh
# install.sh --uninstall --purge : les fils retenus par la commande nokker (local-threads.json, clé « <serveur>|<cwd> »)
# de CETTE installation sont oubliés — sinon une installation neuve sur le même port reprend un fil de la pile effacée.
#   sh deploy/lokkal/tests/purge-threads.sh      (sous sh, dash, bash s'ils sont là)
# purge_threads est lue dans install.sh (jamais recopiée). (a) entrées du port de l'installation retirées sous toutes les
# orthographes de la boucle locale, autres ports et autres serveurs gardés, version gardée, 0600 ; (b) rien à retirer :
# fichier intact, aucun message ; (c) sans python3 : fichier supprimé, message ; (d) fichier illisible : supprimé ;
# (e) pas de fichier : rien ; (f) install.sh appelle purge_threads au --purge, AVANT d'effacer le dossier (.env lu).
set -u
HERE=$(cd "$(dirname "$0")/.." && pwd)
FAIL=0; N=0
ok()   { N=$((N + 1)); }
fail() { N=$((N + 1)); FAIL=$((FAIL + 1)); printf 'FAIL %s\n' "$*"; }
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
sed -n '/^purge_threads() {/,/^}/p' "$HERE/install.sh" > "$TMP/fns.sh"
grep -q '^purge_threads() {' "$TMP/fns.sh" && ok || fail "purge_threads introuvable dans install.sh"
command -v python3 >/dev/null || { echo "python3 absent : cas (a) et (b) non joués"; }
mkdir -p "$TMP/nopy"; for t in rm sh; do ln -s "$(command -v $t)" "$TMP/nopy/$t"; done
seed() { cat > "$TMP/c/local-threads.json" <<'J'
{
 "version": 1,
 "entries": {
  "http://127.0.0.1:8743|/home/u/nokker/a": {"thread_id": "th_a", "at": "2026-11-17T22:00:00Z"},
  "http://localhost:8743|/home/u/nokker/b": {"thread_id": "th_b", "at": "2026-11-17T22:01:00Z"},
  "http://[::1]:8743|/home/u/nokker/c": {"thread_id": "th_c", "at": "2026-11-17T22:02:00Z"},
  "http://127.0.0.1:8742|/home/u/nokker/a": {"thread_id": "th_main", "at": "2026-11-17T22:03:00Z"},
  "https://lokkal.example.org:8743|/home/u/x": {"thread_id": "th_remote", "at": "2026-11-17T22:04:00Z"},
  "http://127.0.0.1:18743|/home/u/y": {"thread_id": "th_other_port", "at": "2026-11-17T22:05:00Z"}
 }
}
J
chmod 600 "$TMP/c/local-threads.json"; }
run() { # run <sh> <port> [PATH]
  ( CONF_DIR=$TMP/c; [ -n "${3:-}" ] && PATH=$3
    msg() { _f=$1; shift; printf -- "$_f" "$@"; }; say() { printf 'SAY %s\n' "$(msg "$@")"; }
    . "$TMP/fns.sh"; purge_threads "$2" ) > "$TMP/out" 2>&1
}
for sh_ in sh dash bash; do
  command -v "$sh_" >/dev/null || continue
  rm -rf "$TMP/c"; mkdir -p "$TMP/c"
  if command -v python3 >/dev/null; then
    seed; run "$sh_" 8743
    keys=$(python3 -c 'import json,sys; print(" ".join(sorted(v["thread_id"] for v in json.load(open(sys.argv[1]))["entries"].values())))' "$TMP/c/local-threads.json")
    [ "$keys" = "th_main th_other_port th_remote" ] && ok || fail "($sh_ a) restent « $keys »"
    python3 -c 'import json,sys; sys.exit(json.load(open(sys.argv[1])).get("version") != 1)' "$TMP/c/local-threads.json" && ok || fail "($sh_ a) version perdue"
    [ "$(stat -c %a "$TMP/c/local-threads.json" 2>/dev/null || stat -f %Lp "$TMP/c/local-threads.json")" = 600 ] && ok || fail "($sh_ a) droits pas 600"
    grep -q '^SAY nokker command: 3 thread(s) of this install forgotten' "$TMP/out" && ok || fail "($sh_ a) message : $(cat "$TMP/out")"
    [ -z "$(ls -A "$TMP/c" | grep -v '^local-threads.json$')" ] && ok || fail "($sh_ a) fichier temporaire laissé"
    seed; cp "$TMP/c/local-threads.json" "$TMP/avant"; run "$sh_" 9999
    cmp -s "$TMP/c/local-threads.json" "$TMP/avant" && [ ! -s "$TMP/out" ] && ok || fail "($sh_ b) fichier touché ou message : $(cat "$TMP/out")"
  fi
  seed; run "$sh_" 8743 "$TMP/nopy"
  [ ! -e "$TMP/c/local-threads.json" ] && grep -q '^SAY nokker command: remembered threads forgotten' "$TMP/out" && ok || fail "($sh_ c) sans python3 : $(cat "$TMP/out")"
  printf '{pas du json' > "$TMP/c/local-threads.json"; run "$sh_" 8743
  [ ! -e "$TMP/c/local-threads.json" ] && ok || fail "($sh_ d) fichier illisible gardé"
  run "$sh_" 8743
  [ ! -s "$TMP/out" ] && ok || fail "($sh_ e) sans fichier : $(cat "$TMP/out")"
done
awk '/^    purge_threads "\$\(getv NOKKER_API_PORT\)"$/ {p = NR} /^    rm -rf "\$DIR"( |$)/ && !r {r = NR} END {exit !(p && r > p && r - p <= 2)}' "$HERE/install.sh" \
  && ok || fail "(f) install.sh n'appelle pas purge_threads juste avant d'effacer le dossier"
printf '%s : %s contrôles, %s échec(s)\n' "${0##*/}" "$N" "$FAIL"
[ "$FAIL" -eq 0 ]
