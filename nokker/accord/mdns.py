"""ACCORD — annonce mDNS `_nokker-accord._tcp` (docs/ACCORD-DEVICE.md §1). Extra OPTIONNEL `nokker[accord]` (zeroconf,
LGPL-2.1) : sans lui, `import zeroconf` lève ImportError et l'appareil reçoit l'adresse à la main."""
from __future__ import annotations

import socket
from typing import Any

import zeroconf  # noqa: F401 — ImportError si l'extra n'est pas installé

SERVICE = "_nokker-accord._tcp.local."


def announce(port: int, host_fp: str, lan_ip: str = "") -> Any:
    from zeroconf import ServiceInfo, Zeroconf

    ip = lan_ip or socket.gethostbyname(socket.gethostname())
    info = ServiceInfo(SERVICE, f"nokker-{host_fp[:8]}.{SERVICE}", port=port, addresses=[socket.inet_aton(ip)],
                       properties={"v": "1", "fp8": host_fp[:8]})
    zc = Zeroconf()
    zc.register_service(info)
    return zc
