#!/bin/sh
# install.sh --uninstall --purge SANS terminal et sans --yes (ssh sans -t, CI, agent) : arrêt net, message qui dit quoi
# faire (--yes), RIEN touché ; avec un terminal (pty de `script`), la question est posée et « n » abandonne.
#   sh deploy/lokkal/tests/uninstall-notty.sh      (sous sh, dash, bash s'ils sont là ; setsid requis, script optionnel)
# Avant le 17.11 : « cannot open /dev/tty » puis « aborted » (et sous dash, une redirection ratée sur « : » tuait le script).
set -u
HERE=$(cd "$(dirname "$0")/.." && pwd)
FAIL=0; N=0
ok()   { N=$((N + 1)); }
fail() { N=$((N + 1)); FAIL=$((FAIL + 1)); printf 'FAIL %s\n' "$*"; }
command -v setsid >/dev/null || { echo "setsid absent : test sauté"; exit 0; }
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/i"; echo 'services: {}' > "$TMP/i/docker-compose.yml"; cp "$TMP/i/docker-compose.yml" "$TMP/avant"
for sh_ in sh dash bash; do
  command -v "$sh_" >/dev/null || continue
  for L in en fr; do
    setsid -w "$sh_" "$HERE/install.sh" --uninstall --purge --dir "$TMP/i" --lang "$L" < /dev/null > "$TMP/out" 2>&1; rc=$?
    [ "$rc" = 1 ] && ok || fail "($sh_ $L) code $rc au lieu de 1 : $(cat "$TMP/out")"
    case $L in en) m='no terminal to confirm the erase: nothing was touched. Re-run with --yes';;
               fr) m="pas de terminal pour confirmer l'effacement : rien n'a été touché. Relancez avec --yes";; esac
    grep -q "$m" "$TMP/out" && ok || fail "($sh_ $L) message manquant : $(cat "$TMP/out")"
    grep -q '/dev/tty' "$TMP/out" && fail "($sh_ $L) erreur /dev/tty brute montrée" || ok
    cmp -s "$TMP/i/docker-compose.yml" "$TMP/avant" && ok || fail "($sh_ $L) dossier touché"
  done
  if command -v script >/dev/null; then
    printf 'n\n' | script -qec "$sh_ $HERE/install.sh --uninstall --purge --dir $TMP/i --lang en" /dev/null > "$TMP/out" 2>&1
    grep -q 'ALSO erase the Nokker database' "$TMP/out" && grep -q 'aborted' "$TMP/out" && ok \
      || fail "($sh_ pty) question pas posée ou « n » pas respecté : $(tr -d '\r' < "$TMP/out")"
    [ -f "$TMP/i/docker-compose.yml" ] && ok || fail "($sh_ pty) dossier effacé malgré « n »"
  fi
done
printf '%s : %s contrôles, %s échec(s)\n' "${0##*/}" "$N" "$FAIL"
[ "$FAIL" -eq 0 ]
