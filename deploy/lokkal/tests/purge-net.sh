#!/bin/sh
# install.sh --uninstall [--purge] : les bacs à sable sont retirés AVANT `compose down`, sinon, encore attachés au réseau
# <projet>-sandbox, ils empêchent compose de le retirer (vu le 18.11 ; prouvé sur une vraie machine : ancien ordre →
# réseau resté, nouvel ordre → « Network …-sandbox Removed »).
#   sh deploy/lokkal/tests/purge-net.sh      (sous sh, dash, bash s'ils sont là)
# install.sh est joué en entier contre un FAUX docker qui journalise ses appels.
set -u
HERE=$(cd "$(dirname "$0")/.." && pwd)
FAIL=0; N=0
ok()   { N=$((N + 1)); }
fail() { N=$((N + 1)); FAIL=$((FAIL + 1)); printf 'FAIL %s\n' "$*"; }
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin"
cat > "$TMP/bin/docker" <<'FAKE'
#!/bin/sh
echo "$*" >> "$FAKE_LOG"
case "$1 $2" in
  "ps -aq") case "$*" in *label=nokker.sandbox*) echo bac1;; esac;;
  "compose version") echo "Docker Compose version v2";;
esac
exit 0
FAKE
chmod +x "$TMP/bin/docker"
for sh_ in sh dash bash; do
  command -v "$sh_" >/dev/null || continue
  for p in 1 0; do
    D=$TMP/inst; rm -rf "$D" "$TMP/xdg"; mkdir -p "$D/config" "$TMP/xdg"; : > "$TMP/log"
    printf 'services: {}\n' > "$D/docker-compose.yml"
    printf 'COMPOSE_PROJECT_NAME=nokker-t\nNOKKER_SANDBOX_NETWORK=nokker-t-sandbox\nNOKKER_API_PORT=8742\n' > "$D/.env"
    if [ $p = 1 ]; then a="--uninstall --purge --yes"; else a="--uninstall"; fi
    PATH=$TMP/bin:$PATH FAKE_LOG=$TMP/log XDG_CONFIG_HOME=$TMP/xdg "$sh_" "$HERE/install.sh" $a --dir "$D" --lang en < /dev/null > "$TMP/out" 2>&1
    r=$(grep -n '^rm -f bac1$' "$TMP/log" | head -1 | cut -d: -f1); c=$(grep -n 'compose .* down' "$TMP/log" | head -1 | cut -d: -f1)
    [ -n "$r" ] && [ -n "$c" ] && [ "$r" -lt "$c" ] && ok || fail "($sh_ purge=$p) ordre : rm du bac ligne ${r:-?}, compose down ligne ${c:-?} : $(cat "$TMP/out")"
    grep -q 'network=nokker-t-sandbox' "$TMP/log" && ok || fail "($sh_ purge=$p) bacs pas filtrés par le réseau de l'installation"
  done
done
printf '%s : %s contrôles, %s échec(s)\n' "${0##*/}" "$N" "$FAIL"
[ "$FAIL" -eq 0 ]
