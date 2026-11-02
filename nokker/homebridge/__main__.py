"""`python -m nokker.homebridge [serve]` : la passerelle du foyer (profil compose `home`). Refuse de démarrer sans
`HOME_BRIDGE_TOKEN`. `discovery` (profil `home-mdns`, conteneur home-discovery) : découverte multicast des écrans
(Cast, AirPlay, DLNA), servie à home-bridge par socket Unix (docs/SCREEN-CAST.md). `audio` (profil `home-mic`) :
l'écoute du heurtoir (docs/KNOCK.md §4)."""
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
    if cmd == "audio":  # profil home-mic : micro du heurtoir (docs/KNOCK.md §4), réseau none, /dev/snd
        from nokker.homebridge.knock.audio import main as audio_main

        return audio_main(args[1:])
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
