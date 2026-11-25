#!/bin/sh
# Bureau virtual (docs/DESKTOP.md §1.1) : Xvfb, bus de session, bus d'accessibilité, WM EWMH, puis le nœud.
# Sans WM EWMH, agent-desktop refuse launch/list-windows (native_window_unproven, relevé au spike).
set -eu
export DISPLAY="${DISPLAY:-:99}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/nokker-desktop}"
export GTK_A11Y=atspi GNOME_ACCESSIBILITY=1 NO_AT_BRIDGE=0 SAL_USE_VCLPLUGIN=gtk3
mkdir -p "$XDG_RUNTIME_DIR" && chmod 700 "$XDG_RUNTIME_DIR"
Xvfb "$DISPLAY" -screen 0 1600x1000x24 -nolisten tcp -noreset &
i=0; while [ ! -e "/tmp/.X11-unix/X${DISPLAY#:}" ] && [ $i -lt 50 ]; do i=$((i+1)); sleep 0.1; done
# Bus de session à adresse fixe ($XDG_RUNTIME_DIR/bus) plutôt que dbus-run-session : le contrôle de santé (docker exec)
# doit le joindre, et il n'hérite pas de l'environnement de ce processus.
export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
dbus-daemon --session --address="$DBUS_SESSION_BUS_ADDRESS" --nofork --nopidfile --nosyslog &
i=0; while [ ! -S "$XDG_RUNTIME_DIR/bus" ] && [ $i -lt 50 ]; do i=$((i+1)); sleep 0.1; done
/usr/libexec/at-spi-bus-launcher --launch-immediately &
metacity --replace --no-composite &
sleep 1
exec python3 -m nokker.desktop.node "$@"
