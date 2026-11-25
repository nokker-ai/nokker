#!/bin/sh
# Kiosque HDMI d'un Lokkal (docs/KIOSK.md §4) : ouvre /kiosk (défaut) ou /present/<bs_…> (démo) dans un Chrome plein
# écran à profil propre, lecture audio sans geste (narration et voix de Nina sur la télé).
#   nokker-kiosk                      → /kiosk
#   nokker-kiosk present              → /present (suit seul la session navigateur en cours : mode démo)
#   nokker-kiosk present bs_0123…     → /present/bs_0123… (grand écran d'une session précise)
# Variables équivalentes pour la unit : NOKKER_KIOSK_MODE=kiosk|present, NOKKER_PRESENT_SESSION=bs_…
# La clé locale ne quitte JAMAIS la machine ni ce script : elle émet un CODE d'accès à usage unique (60 s) À PORTÉE
# (`kiosk` : la vue du kiosque seule ; `present` : parler et afficher, sans gestion ni décision de carte, 12 h fixes),
# passé à curl par l'entrée standard (jamais en argument, visible dans ps). Une page de lancement 0600 porte le code
# dans le fragment de l'adresse ; à l'arrêt, la page est effacée et les sessions de la portée sont révoquées.
set -eu
NOKKER_DIR=${NOKKER_DIR:-$HOME/.nokker}
RUN=${XDG_RUNTIME_DIR:?XDG_RUNTIME_DIR absent : lancer depuis la session graphique (systemctl --user)}
PORT=$(sed -n 's/^NOKKER_PORT=//p' "$NOKKER_DIR/.env" 2>/dev/null | tail -n 1)
WEB=${NOKKER_WEB:-http://127.0.0.1:${PORT:-7420}}
CHROME=${NOKKER_KIOSK_CHROME:-$(command -v google-chrome || command -v chromium || command -v chromium-browser)}
MODE=${1:-${NOKKER_KIOSK_MODE:-kiosk}}
case "$MODE" in
  kiosk) TARGET=/kiosk; SCOPE=kiosk ;;
  present)
    SCOPE=present
    SID=${2:-${NOKKER_PRESENT_SESSION:-}}
    if [ -z "$SID" ]; then
      TARGET=/present  # attente « Nina va te montrer… », puis bascule seule sur la session navigateur en cours
    else
      printf '%s' "$SID" | grep -Eq '^bs_[A-Za-z0-9]{8,40}$' || { echo "mode present : identifiant bs_… invalide" >&2; exit 2; }
      TARGET="/present/$SID"
    fi ;;
  *) echo "mode inconnu : $MODE (kiosk | present [bs_…])" >&2; exit 2 ;;
esac
PAGE="$RUN/nokker-kiosk.html"
TOKF="$NOKKER_DIR/state/local-token"
[ -r "$TOKF" ] || { echo "clé locale introuvable : $TOKF" >&2; exit 1; }
API_PORT=$(sed -n 's/^NOKKER_API_PORT=//p' "$NOKKER_DIR/.env" 2>/dev/null | tail -n 1)
API=${NOKKER_API:-http://127.0.0.1:${API_PORT:-8742}}
umask 077
# En-tête Authorization lu sur l'entrée standard par curl (`-H @-`) : jamais dans la ligne de commande.
local_call() {  # local_call <méthode> <chemin> [corps JSON]
  printf 'Authorization: Bearer %s\n' "$(cat "$TOKF")" |
    curl -fsS -m 10 -X "$1" -H @- -H 'Content-Type: application/json' ${3:+--data "$3"} "$API$2"
}
revoke() { local_call DELETE "/v1/local/access-sessions/scope/$SCOPE" >/dev/null 2>&1 || true; }
trap 'rm -f "$PAGE"; revoke' EXIT
trap 'exit 143' INT TERM
revoke  # une session laissée par un lancement précédent (coupure, crash) ne survit pas au suivant
code=$(local_call POST /v1/local/session-code "{\"scope\":\"$SCOPE\"}" | sed -n 's/.*"code": *"\([A-Za-z0-9_-]*\)".*/\1/p')
[ -n "$code" ] || { echo "code d'accès non émis (noyau arrêté ?) : $API" >&2; exit 1; }
printf '<!doctype html><meta charset="utf-8"><title>Nokker</title><script>location.replace(%s)</script>\n' \
  "\"$WEB$TARGET#code=$code\"" > "$PAGE"
unset code
# shellcheck disable=SC2086
"$CHROME" --kiosk --noerrdialogs --disable-infobars --no-first-run --disable-session-crashed-bubble \
  --disable-features=Translate --autoplay-policy=no-user-gesture-required \
  --user-data-dir="$HOME/.config/nokker-kiosk-chrome" ${NOKKER_KIOSK_ARGS:-} "file://$PAGE"
