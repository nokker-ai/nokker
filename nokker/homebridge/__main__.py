"""`python -m nokker.homebridge [serve]` : la passerelle du foyer (profil compose `home`). Refuse de démarrer sans
`HOME_BRIDGE_TOKEN`. `discovery` (profil `home-mdns`, conteneur home-discovery) : découverte multicast des écrans
(Cast, AirPlay, DLNA), servie à home-bridge par socket Unix (docs/SCREEN-CAST.md). La sous-commande `audio` (profil
`home-mic`) relève d'un autre chantier (docs/LOKKAL-HOME-BRIDGE.md)."""
from __future__ import annotations

import logging
import sys
import threading


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    cmd = args[0] if args else "serve"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if cmd == "discovery":
        from nokker.cast.bridge import discovery_main

        return discovery_main()
    if cmd != "serve":
        print(f"home-bridge : sous-commande {cmd!r} non fournie par cette image", file=sys.stderr)
        return 2
    from nokker.homebridge.config import BridgeConfigError, load
    from nokker.homebridge.server import Bridge, serve

    try:
        cfg = load()
        bridge = Bridge.build(cfg)
    except (BridgeConfigError, OSError, ValueError) as e:
        print(f"home-bridge : {e}", file=sys.stderr)
        return 1
    serve(bridge)
    logging.getLogger("nokker.homebridge").info("home-bridge prêt (api %s:%d, foyer %s:%d)", *cfg.api_listen,
                                                *cfg.lan_listen)
    threading.Event().wait()
    return 0


if __name__ == "__main__":
    sys.exit(main())
