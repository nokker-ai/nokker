"""Proxy de sortie des bacs à sable Lokkal (service `egress`, port 3128).

Le réseau `nokker-sandbox` est `internal` : un bac à sable n'y voit que ce proxy. `dockerproxy` lui pose
HTTP(S)_PROXY=http://egress:3128 ; pip, npm, git, curl passent donc par ici. On résout le nom, on refuse TOUT DE SUITE
une destination privée (Postgres, noyau, proxy docker, box et LAN de l'utilisateur, 169.254.169.254, réseau docker),
puis on se connecte à l'IP vérifiée elle-même : un DNS qui change d'avis entre la vérification et la connexion ne mène
pas au réseau interne. Même logique que le proxy de sortie du pod navigateur de la bêta (infra/nokker-browser).

Hôtes ssh déclarés (docs/en/ops.md, docs/LOKKAL.md §7.14) : le client ssh des outils `ssh_*` sort par ici en CONNECT
(ProxyCommand). Un CONNECT vers une adresse ET un port déclarés dans l'inventaire de l'utilisateur passe, même vers une
plage privée (LAN, tailnet) ; jamais vers le bouclage, le lien local (169.254, métadonnées cloud), le multicast, ni les
réseaux de ce container (réseaux docker de Lokkal : noyau, Postgres, proxy docker). La liste est écrite par le noyau
depuis l'inventaire VALIDÉ (`ssh-hosts.json`, dossier monté en lecture seule) : même règle d'entrée invalide que le
noyau, pas d'analyseur YAML ici. Non déclaré : vers le port 22, refusé ; ailleurs, plages privées refusées comme avant.
Fichier absent ou illisible : aucun hôte déclaré (échec fermé).
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import socket
import sys
from urllib.parse import urlsplit

log = logging.getLogger("egress")

BLOCKED = [ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24",
    "192.168.0.0/16", "198.18.0.0/15", "224.0.0.0/4", "240.0.0.0/4",
    "::/128", "::1/128", "fc00::/7", "fe80::/10", "ff00::/8", "64:ff9b::/96", "100::/64")]
CONNECT_TIMEOUT = 15
# Toujours refusés, même déclarés : bouclage, lien local (dont 169.254.169.254), multicast, réservés, non spécifiés.
ALWAYS_BLOCKED = [ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "127.0.0.0/8", "169.254.0.0/16", "224.0.0.0/4", "240.0.0.0/4",
    "::/128", "::1/128", "fe80::/10", "ff00::/8")]
ALLOW_FILE = os.environ.get("NOKKER_EGRESS_SSH_HOSTS", "/etc/nokker/egress/ssh-hosts.json")
SSH_PORT = 22
_allow_cache: dict = {"key": None, "hosts": frozenset()}


class Denied(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


def is_blocked(ip: str) -> bool:
    a = ipaddress.ip_address(ip)
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped:
        a = a.ipv4_mapped
    return any(a in n for n in BLOCKED if n.version == a.version)


def _own_networks() -> list:
    """Réseaux directement reliés à ce container (réseaux docker de Lokkal) : jamais une destination, même déclarée."""
    nets = []
    try:
        with open("/proc/net/route") as f:
            for line in list(f)[1:]:
                p = line.split()
                if len(p) >= 8 and p[2] == "00000000" and p[1] != "00000000":   # sans passerelle : réseau relié
                    dest = ipaddress.IPv4Address(int(p[1], 16).to_bytes(4, "little"))
                    mask = ipaddress.IPv4Address(int(p[7], 16).to_bytes(4, "little"))
                    nets.append(ipaddress.ip_network(f"{dest}/{mask}", strict=False))
    except (OSError, ValueError):
        pass
    try:
        with open("/proc/net/if_inet6") as f:
            for line in f:
                p = line.split()
                if len(p) >= 6 and p[5] != "lo":
                    a = ipaddress.IPv6Address(int(p[0], 16).to_bytes(16, "big"))
                    nets.append(ipaddress.ip_network(f"{a}/{int(p[2], 16)}", strict=False))
    except (OSError, ValueError):
        pass
    return nets


OWN_NETWORKS = _own_networks()


def never_reachable(ip: str, own: list | None = None) -> bool:
    a = ipaddress.ip_address(ip)
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped:
        a = a.ipv4_mapped
    nets = ALWAYS_BLOCKED + (OWN_NETWORKS if own is None else own)
    return any(a in n for n in nets if n.version == a.version)


def declared_hosts(path: str | None = None) -> frozenset:
    """{(adresse en minuscules, port)} de `ssh-hosts.json` (`{"hosts": [{"address", "port"}]}`), relu quand il change.
    Absent, illisible ou mal formé : ensemble vide (échec fermé)."""
    path = path or ALLOW_FILE
    try:
        st = os.stat(path)
        key = (path, st.st_mtime_ns, st.st_size, st.st_ino)
    except OSError:
        return frozenset()
    if _allow_cache["key"] == key:
        return _allow_cache["hosts"]
    hosts = set()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        for h in data.get("hosts") or []:
            addr, port = str(h.get("address") or "").strip().lower(), h.get("port", SSH_PORT)
            if addr and not addr.startswith("-") and isinstance(port, int) and 0 < port < 65536:
                hosts.add((addr.strip("[]"), port))
    except (OSError, ValueError, AttributeError, TypeError):
        log.warning("liste des hôtes ssh illisible (%s) : aucun hôte déclaré", path)
        hosts = set()
    _allow_cache.update(key=key, hosts=frozenset(hosts))
    return _allow_cache["hosts"]


async def _resolve(h: str) -> list[str]:
    try:
        ipaddress.ip_address(h)
        return [h]
    except ValueError:
        pass
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(h, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise Denied(502, f"nom introuvable : {h}")
    ips = [i[4][0] for i in infos]
    if not ips:
        raise Denied(502, f"nom introuvable : {h}")
    return ips


async def resolve_connect(host: str, port: int) -> str:
    """Destination d'un CONNECT : un hôte ssh déclaré (adresse ET port) peut être privé ; sinon, port 22 refusé et
    plages privées refusées (`resolve_safe`)."""
    h = host.strip("[]")
    if (h.lower(), port) in declared_hosts():
        ips = await _resolve(h)
        bad = next((ip for ip in ips if never_reachable(ip)), None)
        if bad:
            raise Denied(403, f"destination jamais joignable, même déclarée : {h} → {bad}")
        return ips[0]
    if port == SSH_PORT:
        raise Denied(403, f"ssh vers {h}:{port} refusé : hôte non déclaré dans hosts.yaml (adresse et port)")
    return await resolve_safe(host)


async def resolve_safe(host: str) -> str:
    h = host.strip("[]")
    try:
        ipaddress.ip_address(h)
        ips = [h]
    except ValueError:
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(h, None, type=socket.SOCK_STREAM)
        except socket.gaierror:
            raise Denied(502, f"nom introuvable : {h}")
        ips = [i[4][0] for i in infos]
    if not ips:
        raise Denied(502, f"nom introuvable : {h}")
    bad = next((ip for ip in ips if is_blocked(ip)), None)
    if bad:
        raise Denied(403, f"destination interne refusée : {h} → {bad}")
    return ips[0]


async def pipe(src, dst):
    try:
        while chunk := await src.read(65536):
            dst.write(chunk)
            await dst.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        try:
            dst.close()
        except RuntimeError:
            pass


async def reply(writer, status: int, msg: str):
    reason = {400: "Bad Request", 403: "Forbidden", 502: "Bad Gateway", 504: "Gateway Timeout"}.get(status, "Error")
    body = (msg + "\n").encode()
    writer.write(f"HTTP/1.1 {status} {reason}\r\nContent-Type: text/plain; charset=utf-8\r\n"
                 f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
    await writer.drain()
    writer.close()


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
        head = await reader.readuntil(b"\r\n\r\n")
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        writer.close()
        return
    lines = head.decode("latin-1").split("\r\n")
    try:
        method, target, version = lines[0].split(" ", 2)
    except ValueError:
        return await reply(writer, 400, "requête de proxy invalide")
    try:
        if method == "CONNECT":
            host, _, port = target.rpartition(":")
            if not host or not port.isdigit():
                raise Denied(400, "CONNECT host:port attendu")
            ip = await resolve_connect(host, int(port))
            ur, uw = await asyncio.wait_for(asyncio.open_connection(ip, int(port)), CONNECT_TIMEOUT)
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            await asyncio.gather(pipe(reader, uw), pipe(ur, writer))
            return
        u = urlsplit(target)
        if u.scheme != "http" or not u.hostname:
            raise Denied(400, "schéma refusé (http:// absolu ou CONNECT)")
        ip = await resolve_safe(u.hostname)
        ur, uw = await asyncio.wait_for(asyncio.open_connection(ip, u.port or 80), CONNECT_TIMEOUT)
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        hdrs = [ln for ln in lines[1:] if ln and ln.split(":", 1)[0].strip().lower() not in
                ("proxy-connection", "proxy-authorization", "connection")]
        uw.write((f"{method} {path} {version}\r\n" + "".join(h + "\r\n" for h in hdrs) +
                  "Connection: close\r\n\r\n").encode("latin-1"))
        await uw.drain()
        await asyncio.gather(pipe(reader, uw), pipe(ur, writer))
    except Denied as e:
        if e.status == 403:
            log.warning("%s", e)
        await reply(writer, e.status, str(e))
    except asyncio.TimeoutError:
        await reply(writer, 504, "délai de connexion dépassé")
    except OSError as e:
        await reply(writer, 502, f"connexion impossible : {e.strerror or e}")


async def main():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), stream=sys.stdout,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    port = int(os.environ.get("LISTEN_PORT", "3128"))
    server = await asyncio.start_server(handle, "0.0.0.0", port)
    log.info("proxy de sortie des bacs à sable sur :%s", port)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
