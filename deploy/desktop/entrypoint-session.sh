#!/bin/sh
# Mode `session` (docs/DESKTOP.md §1.1) : le conteneur a SON bus de session, privé et vide, où seul org.a11y.Bus existe
# (nokker.desktop.a11y_shim) ; il rend l'adresse du bus d'accessibilité de l'hôte (NOKKER_A11Y_BUS, socket monté au
# même chemin). Le bus de session de la personne n'est jamais monté.
set -eu
case "${NOKKER_A11Y_BUS:-}" in
  unix:path=/*) ;;
  *) echo "NOKKER_A11Y_BUS : adresse unix:path=/… attendue" >&2; exit 2;;
esac
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/nokker-desktop}"
mkdir -p "$XDG_RUNTIME_DIR" && chmod 700 "$XDG_RUNTIME_DIR"
exec dbus-run-session --config-file=/usr/local/share/nokker-desktop/private-bus.conf -- sh -c '
  python3 -m nokker.desktop.a11y_shim &
  i=0; while ! gdbus call --session --dest org.a11y.Bus --object-path /org/a11y/bus --method org.a11y.Bus.GetAddress >/dev/null 2>&1; do
    i=$((i+1)); [ $i -lt 50 ] || { echo "bus privé : org.a11y.Bus absent" >&2; exit 1; }; sleep 0.1; done
  exec python3 -m nokker.desktop.node --mode session "$@"' nokker-desktop-session "$@"
