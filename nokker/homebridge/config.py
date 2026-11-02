"""Configuration de la passerelle du foyer : `home.yaml` (monté en lecture seule) et liste blanche de destinations.

    allow:                                  # LIVRÉ VIDE : aucune destination tant que l'utilisateur ne l'a pas rempli
      - {host: 192.168.1.10, ports: [8123]}             # Home Assistant
      - {cidr: 192.168.1.0/24, ports: [1400, 1705, 4953, 7000]}   # Sonos, Snapcast (RPC + source TCP), AirPlay
    lan:
      listen: 0.0.0.0:8801                  # écoute des appareils du foyer (écrans, enceintes qui lisent un extrait)
      url: http://192.168.1.5:8801          # adresse de la passerelle VUE du LAN (image_url, extraits audio)
    home_assistant: {url: http://192.168.1.10:8123, token_file: secrets/ha_token}
    rooms:
      salon:
        occupancy: [binary_sensor.salon_presence]
        speakers:
          - {sink: music_assistant, target: media_player.salon}
          - {sink: sonos, host: 192.168.1.30}
          - {sink: snapcast, host: 192.168.1.40, group: <id du groupe>, stream: tts, source_port: 4953}
          - {sink: airplay, host: 192.168.1.50}
    members: {us_alice: {room_sensor: sensor.alice_piece}}
    guests: input_boolean.invites

Garde-fou réseau (`Allowlist.check`) : toute connexion sortante vise une adresse PRIVÉE du foyer listée (hôte ou CIDR)
ET un port listé. Une adresse publique est refusée même si elle est listée : le réseau `homelan` sort aussi vers
Internet (pont Docker), c'est au code de l'interdire.
"""
from __future__ import annotations

import ipaddress
import os
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

TOKEN_ENV = "HOME_BRIDGE_TOKEN"
CONFIG_ENV = "HOME_CONFIG"
DEFAULT_CONFIG = "/etc/home/home.yaml"


class BridgeConfigError(ValueError):
    pass


class Refused(PermissionError):
    """Destination hors liste blanche (ou publique)."""


# Réseau du foyer = plages privées au sens strict (RFC 1918, lien local, ULA IPv6), listées une à une.
# `ipaddress.is_private` ne convient pas : il range aussi parmi les « privées » les plages de DOCUMENTATION
# (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24, 2001:db8::/32), des réservations (240.0.0.0/4) et d'autres plages
# spéciales qui ne sont pas le LAN d'un foyer.
HOME_NETS = tuple(ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16",  # RFC 1918, lien local IPv4
    "fc00::/7", "fe80::/10",  # ULA et lien local IPv6
))


def _private(ip: ipaddress._BaseAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return any(ip.version == n.version and ip in n for n in HOME_NETS)


@dataclass
class Allowlist:
    rules: list[tuple[str, ipaddress._BaseNetwork | None, frozenset[int]]] = field(default_factory=list)
    resolve: Any = None  # Callable[[str], list[str]] — remplaçable en test

    @classmethod
    def from_config(cls, raw: Any) -> Allowlist:
        rules = []
        for r in raw or []:
            if not isinstance(r, dict):
                raise BridgeConfigError("allow : une liste de {host|cidr, ports}")
            ports = frozenset(int(p) for p in r.get("ports") or [])
            if not ports:
                raise BridgeConfigError("allow : chaque entrée liste ses ports")
            if r.get("cidr"):
                net = ipaddress.ip_network(str(r["cidr"]), strict=False)
                if not (_private(net.network_address) and _private(net.broadcast_address)):
                    raise BridgeConfigError(f"allow : {net} n'est pas une plage privée du foyer")
                rules.append(("", net, ports))
            elif r.get("host"):
                rules.append((str(r["host"]).lower(), None, ports))
            else:
                raise BridgeConfigError("allow : host ou cidr")
        return cls(rules=rules)

    def _ips(self, host: str) -> list[ipaddress._BaseAddress]:
        try:
            ips = [ipaddress.ip_address(host)]
        except ValueError:
            if self.resolve is not None:
                names = self.resolve(host)
            else:
                names = [i[4][0] for i in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)]
            ips = [ipaddress.ip_address(n) for n in names]
        # ::ffff:a.b.c.d est l'adresse IPv4 a.b.c.d : jugée (et jointe) comme telle
        return [ip.ipv4_mapped if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped else ip for ip in ips]

    def check(self, host: str, port: int) -> str:
        """Adresse IP à joindre (résolue UNE fois : pas de second tour DNS), ou `Refused`."""
        host = (host or "").lower().strip("[]")
        try:
            ips = self._ips(host)
        except (OSError, ValueError):
            raise Refused(f"destination {host} : introuvable") from None
        if not ips or not all(_private(ip) for ip in ips):
            raise Refused(f"destination {host} : adresse hors du réseau du foyer")
        for name, net, ports in self.rules:
            if port not in ports:
                continue
            if name and name == host:
                return str(ips[0])
            if net is not None and all(ip in net for ip in ips):
                return str(ips[0])
        raise Refused(f"destination {host}:{port} : absente de la liste blanche (home.yaml)")

    def check_url(self, url: str) -> str:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise Refused("adresse http(s) attendue")
        port = parts.port or (443 if parts.scheme == "https" else 80)
        self.check(parts.hostname, port)
        return url


@dataclass
class BridgeConfig:
    token: str
    allow: Allowlist
    raw: dict[str, Any]
    base_dir: Path
    lan_listen: tuple[str, int] = ("0.0.0.0", 8801)
    lan_url: str = ""
    api_listen: tuple[str, int] = ("0.0.0.0", 8800)

    def secret(self, rel: str) -> str:
        """Secret propre à la passerelle (fichier sous le dossier de home.yaml), jamais dans un message."""
        p = (self.base_dir / rel).resolve()
        if self.base_dir.resolve() not in p.parents:
            raise BridgeConfigError("fichier secret hors du dossier de la passerelle")
        return p.read_text(encoding="utf-8").strip()


def _hostport(spec: str, default: tuple[str, int]) -> tuple[str, int]:
    if not spec:
        return default
    host, _, port = str(spec).rpartition(":")
    return (host or default[0], int(port))


def load(env: dict[str, str] | None = None) -> BridgeConfig:
    env = dict(os.environ if env is None else env)
    token = env.get(TOKEN_ENV, "")
    if len(token) < 16:
        raise BridgeConfigError(f"{TOKEN_ENV} absent ou trop court : la passerelle refuse de démarrer")
    path = Path(env.get(CONFIG_ENV) or DEFAULT_CONFIG)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    return from_dict(raw or {}, token=token, base_dir=path.parent)


def from_dict(raw: dict[str, Any], *, token: str, base_dir: Path) -> BridgeConfig:
    lan = raw.get("lan") or {}
    return BridgeConfig(token=token, allow=Allowlist.from_config(raw.get("allow")), raw=raw, base_dir=base_dir,
                        lan_listen=_hostport(str(lan.get("listen") or ""), ("0.0.0.0", 8801)),
                        lan_url=str(lan.get("url") or "").rstrip("/"),
                        api_listen=_hostport(str(raw.get("api_listen") or ""), ("0.0.0.0", 8800)))


__all__ = ["Allowlist", "BridgeConfig", "BridgeConfigError", "Refused", "from_dict", "load"]
