#!/bin/sh
# Lancé par l'unité systemd --user `nokker-desktop.service` (mode `session`, docs/DESKTOP.md §1.1), SUR L'HÔTE, dans la
# session graphique de la personne. Lit l'adresse du bus d'ACCESSIBILITÉ (org.a11y.Bus.GetAddress, sur le bus de
# session de la personne, côté hôte seulement) puis lance le conteneur du nœud avec :
#   - l'écran X (/tmp/.X11-unix, lecture seule) ;
#   - le dossier du socket du bus d'accessibilité (lecture seule, même chemin) ;
#   - JAMAIS le bus de session de la personne (le conteneur a le sien, privé : nokker.desktop.a11y_shim).
# `session-run.sh --print-a11y` : affiche l'adresse retenue et s'arrête (contrôle, tests).
set -eu

a11y_address() {
  out=""
  if command -v gdbus >/dev/null 2>&1; then
    out=$(gdbus call --session --dest org.a11y.Bus --object-path /org/a11y/bus --method org.a11y.Bus.GetAddress 2>/dev/null || true)
  fi
  if [ -z "$out" ] && command -v busctl >/dev/null 2>&1; then
    out=$(busctl --user call org.a11y.Bus /org/a11y/bus org.a11y.Bus GetAddress 2>/dev/null || true)
  fi
  # gdbus : ('unix:path=/run/user/1000/at-spi/bus_0,guid=…',)   busctl : s "unix:path=/run/user/1000/at-spi/bus_0,guid=…"
  printf '%s\n' "$out" | sed -n "s/.*[\"']\(unix:path=\/[^,\"']*\).*/\1/p" | head -n 1
}

ADDR=$(a11y_address)
case "$ADDR" in
  unix:path=/*) ;;
  *) echo "nokker-desktop : bus d'accessibilité introuvable ou non montable (socket unix par chemin attendu)" >&2; exit 1;;
esac
if [ "${1:-}" = "--print-a11y" ]; then
  printf '%s\n' "$ADDR"
  exit 0
fi
case "${XDG_SESSION_TYPE:-x11}" in
  x11) ;;
  *) echo "nokker-desktop : session ${XDG_SESSION_TYPE} : le mode session ne prend en charge que X11" >&2; exit 1;;
esac
SOCK=${ADDR#unix:path=}
DIR=$(dirname "$SOCK")
UID_=$(id -u); GID_=$(id -g)
STATE="${NOKKER_DESKTOP_STATE_DIR:-$HOME/.nokker/desktop/state}"   # posé par l'installeur dans l'unité (--dir)
mkdir -p "$STATE" && chmod 700 "$STATE"
# Santé : le battement du nœud la porte au noyau ; le HEALTHCHECK de l'image vise le bus à adresse fixe du mode virtual
# (--no-healthcheck). tmpfs à l'uid de la personne : le conteneur tourne sous son uid, HOME en lecture seule sinon.
# Cookie X : monté en lecture seule s'il existe (sinon l'hôte doit admettre le même uid, si:localuser).
XAUTH_ARGS=""
if [ -n "${XAUTHORITY:-}" ] && [ -f "$XAUTHORITY" ]; then XAUTH_ARGS="-v $XAUTHORITY:$XAUTHORITY:ro -e XAUTHORITY"; fi
# shellcheck disable=SC2086
exec docker run --rm --name "nokker-desktop-session-$UID_" \
  --network "${NOKKER_DESKTOP_NETWORK:-nokker_desktopnet}" --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true --user "$UID_:$GID_" \
  --no-healthcheck --tmpfs /run/nokker-desktop:mode=0700,uid=$UID_,gid=$GID_ \
  --tmpfs /home/desk:mode=0700,uid=$UID_,gid=$GID_ --tmpfs /tmp \
  -e DISPLAY -e NOKKER_A11Y_BUS="$ADDR" -e XDG_RUNTIME_DIR=/run/nokker-desktop \
  -e NOKKER_CORE_URL=http://core:8742 -e NOKKER_CORE_HOST=127.0.0.1:8742 -e NOKKER_DESKTOP_STATE=/state \
  -v /tmp/.X11-unix:/tmp/.X11-unix:ro -v "$DIR:$DIR:ro" -v "$STATE:/state" $XAUTH_ARGS \
  --entrypoint /usr/local/bin/nokker-desktop-session "${NOKKER_DESKTOP_IMAGE:-nokker-desktop:local}"
