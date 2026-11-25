#!/bin/sh
# Lanceur du kiosque (deploy/lokkal/kiosk/nokker-kiosk.sh, docs/KIOSK.md §4) : la clé locale ne paraît JAMAIS dans une
# ligne de commande (curl la lit sur son entrée standard), la page de lancement porte un CODE (#code=, jamais #token=),
# la portée suit le mode (kiosk | present), les sessions de la portée sont révoquées au démarrage et à l'arrêt, la page
# 0600 est effacée. curl et Chrome sont simulés (aucun réseau, aucun navigateur).
#   sh deploy/lokkal/tests/kiosk-launcher.sh      (sous sh, dash, bash s'ils sont là)
set -u
HERE=$(cd "$(dirname "$0")/.." && pwd)
FAIL=0; N=0
ok()   { N=$((N + 1)); }
fail() { N=$((N + 1)); FAIL=$((FAIL + 1)); printf 'FAIL %s\n' "$*"; }
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
TOK=tok-local-secret-0123456789abcdefghijklmnop
mkdir -p "$TMP/home/.nokker/state" "$TMP/bin" "$TMP/run"
printf '%s\n' "$TOK" > "$TMP/home/.nokker/state/local-token"
printf 'NOKKER_PORT=7420\nNOKKER_API_PORT=8742\n' > "$TMP/home/.nokker/.env"
cat > "$TMP/bin/curl" <<'EOS'
#!/bin/sh
# faux curl : note ses arguments et l'en-tête lu sur stdin, répond un code pour POST session-code
printf '%s\n' "ARGS $*" >> "$LOG"
[ -t 0 ] || printf 'STDIN %s\n' "$(cat)" >> "$LOG"
case "$*" in *session-code*) printf '{"code": "C0deC0deC0deC0deC0de12", "expires_in": 60, "scope": "x"}' ;; *) printf '{}' ;; esac
EOS
cat > "$TMP/bin/chrome" <<'EOS'
#!/bin/sh
printf '%s\n' "CHROME $*" >> "$LOG"
page=$(printf '%s\n' "$@" | sed -n 's#^file://##p')
cp "$page" "$LOG.page"; ls -l "$page" | cut -c1-10 > "$LOG.mode"
EOS
chmod +x "$TMP/bin/curl" "$TMP/bin/chrome"
for sh_ in sh dash bash; do
  command -v "$sh_" >/dev/null || continue
  for mode in kiosk present; do
    LOG="$TMP/log-$sh_-$mode"; : > "$LOG"
    HOME="$TMP/home" XDG_RUNTIME_DIR="$TMP/run" PATH="$TMP/bin:$PATH" NOKKER_KIOSK_CHROME="$TMP/bin/chrome" LOG="$LOG" \
      "$sh_" "$HERE/kiosk/nokker-kiosk.sh" "$mode" > "$LOG.out" 2>&1 && ok || fail "$sh_ $mode : sortie $(cat "$LOG.out")"
    grep '^ARGS ' "$LOG" | grep -q "$TOK" && fail "$sh_ $mode : jeton local dans les arguments de curl" || ok
    grep '^CHROME ' "$LOG" | grep -q "$TOK" && fail "$sh_ $mode : jeton local dans les arguments de Chrome" || ok
    grep -q "^STDIN Authorization: Bearer $TOK" "$LOG" && ok || fail "$sh_ $mode : en-tête non passé par stdin"
    grep -q "\"scope\":\"$mode\"" "$LOG" && ok || fail "$sh_ $mode : portée $mode absente de l'émission"
    [ "$(grep -c "DELETE.*/v1/local/access-sessions/scope/$mode" "$LOG")" = 2 ] && ok \
      || fail "$sh_ $mode : révocation par portée pas faite au démarrage ET à l'arrêt"
    target=/kiosk; [ "$mode" = present ] && target=/present
    grep -q "http://127.0.0.1:7420$target#code=C0deC0deC0deC0deC0de12" "$LOG.page" && ok || fail "$sh_ $mode : page sans #code="
    grep -q '#token=' "$LOG.page" && fail "$sh_ $mode : #token= dans la page" || ok
    grep -q "$TOK" "$LOG.page" && fail "$sh_ $mode : jeton local dans la page" || ok
    [ "$(cat "$LOG.mode")" = "-rw-------" ] && ok || fail "$sh_ $mode : page non 0600 ($(cat "$LOG.mode"))"
    [ -e "$TMP/run/nokker-kiosk.html" ] && fail "$sh_ $mode : page non effacée à l'arrêt" || ok
  done
done
grep -q '#token=' "$HERE/kiosk/nokker-kiosk.sh" && fail "nokker-kiosk.sh construit encore un #token=" || ok
printf '%s tests, %s échec(s)\n' "$N" "$FAIL"
[ "$FAIL" = 0 ]
