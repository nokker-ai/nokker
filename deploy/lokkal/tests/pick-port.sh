#!/bin/sh
# pick_port + reserve_port d'install.sh (lues dans install.sh, jamais recopiées) : deux installations lancées en même
# temps ne prennent plus le même port (vu le 17.11 : 7421/8743 pris deux fois à 16 s d'écart).
#   sh deploy/lokkal/tests/pick-port.sh      (sous sh, dash, bash s'ils sont là ; ports de test 47420+, jamais publiés)
# (a) 8 choix simultanés → 8 ports distincts ; (b) réservation de plus d'une heure reprise, récente respectée ;
# (c) répertoire de réservation inutilisable → choix d'avant, sans boucle sans fin ; (d) port vraiment occupé sauté.
set -u
HERE=$(cd "$(dirname "$0")/.." && pwd)
FAIL=0; N=0
ok()   { N=$((N + 1)); }
fail() { N=$((N + 1)); FAIL=$((FAIL + 1)); printf 'FAIL %s\n' "$*"; }
TMP=$(mktemp -d); trap 'rm -rf "$TMP"; [ -n "${SRV:-}" ] && kill "$SRV" 2>/dev/null; true' EXIT
sed -n '/^port_free() {/,/^pick_port() {/p' "$HERE/install.sh" > "$TMP/fns.sh"
grep -q '^reserve_port() {' "$TMP/fns.sh" && ok || fail "fonctions introuvables dans install.sh"
for sh_ in sh dash bash; do
  command -v "$sh_" >/dev/null || continue
  R=$TMP/res-$sh_; B=$((47420 + $(printf %s "$sh_" | wc -c) * 100))
  # (a) 8 installations en parallèle
  for k in 1 2 3 4 5 6 7 8; do NOKKER_PORT_RESERVATIONS=$R "$sh_" -c ". $TMP/fns.sh; pick_port $B" > "$TMP/p-$sh_-$k" & done; wait
  [ "$(cat "$TMP"/p-"$sh_"-* | sort -u | wc -l)" -eq 8 ] && ok || fail "($sh_ a) ports en double : $(cat "$TMP"/p-"$sh_"-* | tr '\n' ' ')"
  [ "$(stat -c %a "$R" 2>/dev/null || stat -f %Lp "$R")" = 1777 ] && ok || fail "($sh_ a) répertoire de réservation pas en 1777"
  # (b) réservation périmée (2 h) reprise, récente respectée
  rm -rf "$R"; mkdir -p "$R/$B" "$R/$((B + 1))"; touch -d '2 hours ago' "$R/$B" 2>/dev/null || touch -t "$(date -v-2H +%Y%m%d%H%M 2>/dev/null)" "$R/$B"
  [ "$(NOKKER_PORT_RESERVATIONS=$R "$sh_" -c ". $TMP/fns.sh; pick_port $B")" = "$B" ] && ok || fail "($sh_ b) réservation périmée pas reprise"
  [ "$(NOKKER_PORT_RESERVATIONS=$R "$sh_" -c ". $TMP/fns.sh; pick_port $((B + 1))")" = "$((B + 2))" ] && ok || fail "($sh_ b) réservation récente pas respectée"
  # (c) répertoire inutilisable (un FICHIER à sa place) : pas de réservation, pas de boucle
  : > "$TMP/fichier-$sh_"
  out=$(NOKKER_PORT_RESERVATIONS=$TMP/fichier-$sh_ timeout 10 "$sh_" -c ". $TMP/fns.sh; pick_port $B"); rc=$?
  [ "$rc" = 0 ] && [ "$out" = "$B" ] && ok || fail "($sh_ c) code $rc, port « $out » (boucle sans fin ?)"
done
# (d) port réellement occupé (serveur python) : sauté même sans réservation
if command -v python3 >/dev/null; then
  P=47999; python3 -m http.server "$P" --bind 127.0.0.1 >/dev/null 2>&1 & SRV=$!
  i=0; until curl -s -m 1 -o /dev/null "http://127.0.0.1:$P/"; do i=$((i + 1)); [ $i -gt 50 ] && break; sleep 0.1; done
  [ "$(NOKKER_PORT_RESERVATIONS=$TMP/res-d sh -c ". $TMP/fns.sh; pick_port $P")" = $((P + 1)) ] && ok || fail "(d) port occupé pas sauté"
fi
printf '%s : %s contrôles, %s échec(s)\n' "${0##*/}" "$N" "$FAIL"
[ "$FAIL" -eq 0 ]
