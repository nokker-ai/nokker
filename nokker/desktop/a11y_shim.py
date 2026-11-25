"""Bus de session PRIVÉ du conteneur du mode `session` (docs/DESKTOP.md §1.1, correctif du 06.11.2026).

Le conteneur du nœud ne reçoit JAMAIS le bus de session de la personne (il donnerait le Secret Service, les portails,
les notifications…). agent-desktop exige pourtant un bus de session qui expose `org.a11y.Bus` : il y lit l'adresse du
bus d'ACCESSIBILITÉ, qui est un bus séparé. Le conteneur lance donc son propre bus de session, vide, où ce module est le
SEUL service : `org.a11y.Bus.GetAddress` rend l'adresse du bus d'accessibilité de l'hôte (`NOKKER_A11Y_BUS`, lue sur
l'hôte par `deploy/desktop/session-run.sh` ; socket monté au même chemin), `org.a11y.Status.IsEnabled` vaut vrai.

Vérifié au banc (deploy/desktop/bench/session_bus.py) : sur ce bus, aucun autre nom n'existe ; agent-desktop voit
l'accessibilité et pilote les applis de la session. (Un `xdg-dbus-proxy --filter --talk=org.a11y.Bus` sur le bus de la
personne a été essayé d'abord : le client D-Bus d'agent-desktop y échoue, « Invalid message header read ».)"""
from __future__ import annotations

import os
import re
import sys

_ADDR = re.compile(r"^unix:path=(/[A-Za-z0-9._/@:+-]{1,200})(,guid=[0-9a-f]{32})?$")

XML = """<node>
 <interface name="org.a11y.Bus">
  <method name="GetAddress"><arg type="s" direction="out"/></method>
 </interface>
 <interface name="org.a11y.Status">
  <property name="IsEnabled" type="b" access="read"/>
  <property name="ScreenReaderEnabled" type="b" access="read"/>
 </interface>
</node>"""


def valid_address(addr: str | None) -> str | None:
    """L'adresse si c'est un socket unix PAR CHEMIN (montable dans le conteneur) ; None sinon (abstrait, tcp, vide)."""
    m = _ADDR.match((addr or "").strip())
    return m.group(0) if m else None


def main() -> int:
    addr = valid_address(os.environ.get("NOKKER_A11Y_BUS"))
    if addr is None:
        print("NOKKER_A11Y_BUS : adresse unix:path=/… attendue (bus d'accessibilité de l'hôte)", file=sys.stderr)
        return 2
    from gi.repository import Gio, GLib

    info = Gio.DBusNodeInfo.new_for_xml(XML)

    def call(conn, sender, path, iface, method, params, inv):  # noqa: ANN001
        if method == "GetAddress":
            inv.return_value(GLib.Variant("(s)", (addr,)))
        else:
            inv.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)

    def prop(conn, sender, path, iface, name):  # noqa: ANN001
        return GLib.Variant("b", name == "IsEnabled")

    owned = {"yes": False}

    def acquired(conn, name):  # noqa: ANN001
        owned["yes"] = True
        conn.register_object("/org/a11y/bus", info.interfaces[0], call, None, None)
        conn.register_object("/org/a11y/bus", info.interfaces[1], None, prop, None)

    def lost(conn, name):  # noqa: ANN001
        # Jamais acquis : un autre org.a11y.Bus existe, ce n'est pas le bus privé. Perdu après coup : le bus privé
        # s'arrête (fin du conteneur), rien d'anormal.
        if not owned["yes"]:
            print("org.a11y.Bus déjà pris sur ce bus : ce n'est pas le bus privé du conteneur", file=sys.stderr)
        loop.quit()

    loop = GLib.MainLoop()
    Gio.bus_own_name(Gio.BusType.SESSION, "org.a11y.Bus", Gio.BusNameOwnerFlags.DO_NOT_QUEUE, acquired, None, lost)
    loop.run()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
