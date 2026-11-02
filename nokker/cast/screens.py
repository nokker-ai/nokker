"""Écrans du foyer : ce qu'on en sait (`Screen`), ce qu'on en retient (`ScreenBook`, approbations et accords), et les
trois familles d'appareils (docs/SCREEN-CAST.md §3) :

  - **Chromecast / Google TV** (pychromecast, MIT) : découverte mDNS (`get_chromecasts`), page par DashCast
    (`DashCastController.load_url(url, force=True)` : la télé ouvre NOTRE page, jamais un site tiers en iframe), image
    par le receiver par défaut (`media_controller.play_media(url, type)`) ;
  - **Apple TV / AirPlay** (pyatv, MIT) : découverte mDNS (`pyatv.scan`), média par `stream.play_url(url)` — pyatv
    documente la vidéo (MP4), pas les pages ni les images : un écran AirPlay ne reçoit qu'une image, et seulement
    après appairage (code affiché sur la télé, identifiants rangés au coffre système, jamais dans un fichier) ;
  - **télé DLNA / UPnP** (SSDP + AVTransport, sans bibliothèque) : URL de média seulement (image).

Une page (diagramme, tableau…) sur un écran « média seulement » demande une conversion en image (`rasterize`, non
fournie par défaut) : sans elle, refus explicite. Les bibliothèques sont importées à l'usage (extra `cast`) ; les tests
passent de faux modules."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import socket
import threading
import time
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

logger = logging.getLogger("nokker.cast.screens")

PAGE, IMAGE = "page", "image"
KINDS = ("chromecast", "airplay", "dlna")
AIRPLAY_SECRET = "cast:airplay:{id}"  # coffre SYSTÈME (comme machine_token) : identifiants d'appairage AirPlay


class CastError(RuntimeError):
    """Échec côté appareil (connexion, lancement) : texte court, caviardé avant d'atteindre le modèle."""


class NeedsPairing(CastError):
    pass


@dataclass
class Screen:
    id: str
    name: str
    kind: str
    host: str = ""
    port: int = 0
    model: str = ""
    caps: tuple[str, ...] = (IMAGE,)
    room: str = ""
    approved: bool = False
    owner: str | None = None  # membre du foyer dont c'est l'écran personnel ; None = écran commun
    paired: bool | None = None  # AirPlay : appairé ? None = sans objet
    extra: dict[str, Any] = field(default_factory=dict, repr=False)  # DLNA : control_url

    def public(self) -> dict[str, Any]:
        """Ce qui sort vers un client (bulle, route) : jamais l'hôte ni l'URL de contrôle."""
        return {"id": self.id, "name": self.name, "room": self.room or self.name, "kind": self.kind,
                "caps": list(self.caps), "approved": self.approved, "personal": self.owner is not None}


def _safe_id(raw: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "-", raw)[:80]


@dataclass
class ScreenBook:
    """Écrans approuvés, pièces, propriétaires et accords, dans `<état>/cast/screens.json` (fichier de la machine,
    0600). Aucun secret ici : les identifiants AirPlay vont au coffre système."""

    path: Path
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            data = {}
        data.setdefault("screens", {})
        data.setdefault("consents", [])
        return data

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True), "utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    def entry(self, screen_id: str) -> dict[str, Any]:
        with self._lock:
            return dict(self._load()["screens"].get(screen_id) or {})

    def entries(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return dict(self._load()["screens"])

    def merge(self, s: Screen) -> Screen:
        e = self.entry(s.id)
        s.approved = bool(e.get("approved_at"))
        s.room = str(e.get("room") or s.room or s.name)
        s.owner = e.get("owner") or None
        if e.get("name"):
            s.name = str(e["name"])
        return s

    def remember(self, s: Screen, *, by: str, now: float) -> None:
        with self._lock:
            data = self._load()
            e = data["screens"].setdefault(s.id, {})
            e.update({"approved_at": now, "approved_by": by, "kind": s.kind, "seen_name": s.name})
            self._save(data)

    def set(self, screen_id: str, *, room: str | None = None, name: str | None = None,
            owner: str | None | bool = False) -> dict[str, Any]:
        """Pièce, nom, propriétaire (owner=None : écran commun ; False : inchangé)."""
        with self._lock:
            data = self._load()
            e = data["screens"].setdefault(screen_id, {})
            if room is not None:
                e["room"] = room.strip()[:60]
            if name is not None:
                e["name"] = name.strip()[:60]
            if owner is not False:
                e["owner"] = owner or None
            self._save(data)
            return dict(e)

    def forget(self, screen_id: str) -> bool:
        with self._lock:
            data = self._load()
            gone = data["screens"].pop(screen_id, None) is not None
            data["consents"] = [c for c in data["consents"] if c.get("screen") != screen_id]
            self._save(data)
            return gone

    def consented(self, member: str, screen_id: str) -> bool:
        with self._lock:
            return any(c.get("member") == member and c.get("screen") == screen_id for c in self._load()["consents"])

    def consent(self, member: str, screen_id: str, *, now: float) -> None:
        with self._lock:
            data = self._load()
            if not any(c.get("member") == member and c.get("screen") == screen_id for c in data["consents"]):
                data["consents"].append({"member": member, "screen": screen_id, "at": now})
                self._save(data)

    def revoke_consent(self, member: str, screen_id: str) -> bool:
        with self._lock:
            data = self._load()
            before = len(data["consents"])
            data["consents"] = [c for c in data["consents"]
                                if not (c.get("member") == member and c.get("screen") == screen_id)]
            self._save(data)
            return len(data["consents"]) != before


# ─────────────────────────────── Chromecast ───────────────────────────────


@dataclass
class ChromecastBackend:
    """pychromecast ≥ 14.1 (vérifié le 01.11.2026 : `get_chromecasts`, `get_listed_chromecasts`, `CastInfo`,
    `controllers.dashcast.DashCastController.load_url(url, force=…)`, `media_controller.play_media`)."""

    lib: Any = None
    dashcast: Any = None  # classe DashCastController (injectable)
    timeout_s: float = 5.0

    def _lib(self) -> Any:
        if self.lib is None:
            import pychromecast  # extra `cast`

            self.lib = pychromecast
        return self.lib

    def _dashcast(self) -> Any:
        if self.dashcast is None:
            from pychromecast.controllers.dashcast import DashCastController

            self.dashcast = DashCastController
        return self.dashcast

    def discover(self) -> list[Screen]:
        lib = self._lib()
        casts, browser = lib.get_chromecasts(timeout=self.timeout_s)
        try:
            out = []
            for cc in casts:
                info = cc.cast_info
                out.append(Screen(id=_safe_id(f"cc-{info.uuid}"), name=str(info.friendly_name or "Chromecast"),
                                  kind="chromecast", host=str(info.host or ""), port=int(info.port or 0),
                                  model=str(info.model_name or ""), caps=(PAGE, IMAGE),
                                  extra={"uuid": str(info.uuid)}))
            return out
        finally:
            lib.discovery.stop_discovery(browser)

    def _connect(self, s: Screen) -> tuple[Any, Any]:
        """Connexion UNICAST à l'écran découvert (hôte, port, uuid) : pas de multicast ici (home-bridge, réseau NAT)."""
        from uuid import UUID

        if not s.host:
            raise CastError("écran sans adresse")
        lib = self._lib()
        cc = lib.get_chromecast_from_host((s.host, s.port or 8009, UUID(str(s.extra.get("uuid"))), s.model or None,
                                           s.name))
        cc.wait(timeout=self.timeout_s * 2)
        return cc, None

    def show(self, s: Screen, url: str, *, media: str | None = None) -> None:
        cc, _ = self._connect(s)
        try:
            if media is None:
                d = self._dashcast()()
                cc.register_handler(d)
                d.load_url(url, force=True)  # force : navigation directe, pas d'iframe (contenu mixte, X-Frame)
            else:
                cc.media_controller.play_media(url, media)
        finally:
            cc.disconnect(timeout=self.timeout_s)

    def stop(self, s: Screen) -> None:
        cc, _ = self._connect(s)
        try:
            cc.quit_app()
        finally:
            cc.disconnect(timeout=self.timeout_s)


# ─────────────────────────────── AirPlay ───────────────────────────────


@dataclass
class AirPlayBackend:
    """pyatv ≥ 0.18 (vérifié le 01.11.2026 : `scan`, `pair(conf, Protocol.AirPlay, loop)` → `begin`/`pin`/`finish`,
    `service.credentials`, `connect`, `stream.play_url`). pyatv est asynchrone : une boucle à elle, dans un fil."""

    lib: Any = None
    secret_get: Callable[[str], str | None] = lambda _n: None
    secret_set: Callable[[str, str], Any] = lambda _n, _v: None
    timeout_s: float = 5.0
    _loop: asyncio.AbstractEventLoop | None = field(default=None, repr=False)
    _pairings: dict[str, Any] = field(default_factory=dict, repr=False)

    def _lib(self) -> Any:
        if self.lib is None:
            import pyatv  # extra `cast`

            self.lib = pyatv
        return self.lib

    def _run(self, coro: Any) -> Any:
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
            threading.Thread(target=self._loop.run_forever, daemon=True, name="nokker-cast-airplay").start()
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=self.timeout_s * 6)

    def _protocol(self) -> Any:
        return self._lib().const.Protocol.AirPlay

    def discover(self) -> list[Screen]:
        lib = self._lib()

        async def scan() -> list[Any]:
            return await lib.scan(asyncio.get_running_loop(), timeout=self.timeout_s)

        out = []
        for conf in self._run(scan()):
            svc = conf.get_service(self._protocol())
            if svc is None:
                continue
            sid = _safe_id(f"atv-{conf.identifier}")
            paired = bool(self.secret_get(AIRPLAY_SECRET.format(id=sid)))
            need = str(getattr(getattr(svc, "pairing", None), "name", "") or "")
            out.append(Screen(id=sid, name=str(conf.name), kind="airplay", host=str(conf.address),
                              model=str(getattr(getattr(conf, "device_info", None), "model_str", "") or ""),
                              caps=(IMAGE,), paired=paired or need in ("NotNeeded", "Disabled"),
                              extra={"identifier": str(conf.identifier), "pairing": need}))
        return out

    async def _conf(self, s: Screen) -> Any:
        lib = self._lib()
        confs = await lib.scan(asyncio.get_running_loop(), identifier=s.extra.get("identifier"),
                               hosts=[s.host] if s.host else None, timeout=self.timeout_s)
        if not confs:
            raise CastError("écran injoignable")
        return confs[0]

    def show(self, s: Screen, url: str, *, media: str | None = None) -> None:
        lib = self._lib()
        creds = self.secret_get(AIRPLAY_SECRET.format(id=s.id))

        async def go() -> None:
            conf = await self._conf(s)
            if creds:
                conf.set_credentials(self._protocol(), creds)
            elif str(s.extra.get("pairing") or "") == "Mandatory":
                raise NeedsPairing("appairage requis")
            atv = await lib.connect(conf, asyncio.get_running_loop())
            try:
                await atv.stream.play_url(url)
            finally:
                atv.close()

        self._run(go())

    def stop(self, s: Screen) -> None:
        lib = self._lib()
        creds = self.secret_get(AIRPLAY_SECRET.format(id=s.id))

        async def go() -> None:
            conf = await self._conf(s)
            if creds:
                conf.set_credentials(self._protocol(), creds)
            atv = await lib.connect(conf, asyncio.get_running_loop())
            try:
                await atv.remote_control.stop()
            finally:
                atv.close()

        self._run(go())

    def pair_begin(self, s: Screen) -> None:
        """Lance l'appairage : la télé affiche un code à quatre chiffres."""
        lib = self._lib()

        async def go() -> Any:
            conf = await self._conf(s)
            p = await lib.pair(conf, self._protocol(), asyncio.get_running_loop())
            await p.begin()
            return p

        self._pairings[s.id] = self._run(go())

    def pair_finish(self, s: Screen, pin: str) -> bool:
        p = self._pairings.pop(s.id, None)
        if p is None:
            raise CastError("aucun appairage en cours pour cet écran")

        async def go() -> Any:
            try:
                p.pin(int(pin))
                await p.finish()
                return p.service.credentials if p.has_paired else None
            finally:
                await p.close()

        creds = self._run(go())
        if not creds:
            return False
        self.secret_set(AIRPLAY_SECRET.format(id=s.id), str(creds))
        return True


# ─────────────────────────────── DLNA ───────────────────────────────

_SSDP = ("239.255.255.250", 1900)
_AVT = "urn:schemas-upnp-org:service:AVTransport:1"
_SOAP = ('<?xml version="1.0" encoding="utf-8"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
         's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body><u:{action} xmlns:u="' + _AVT + '">'
         '<InstanceID>0</InstanceID>{args}</u:{action}></s:Body></s:Envelope>')


def _private_http(url: str) -> bool:
    """Une URL de description/contrôle DLNA n'est suivie que sur le réseau local (pas de SSRF par une réponse SSDP)."""
    import ipaddress

    try:
        p = urlparse(url)
        ip = ipaddress.ip_address(p.hostname or "")
    except ValueError:
        return False
    return p.scheme == "http" and (ip.is_private or ip.is_link_local) and not ip.is_loopback


@dataclass
class DlnaBackend:
    """Télé DLNA (MediaRenderer + AvTransport) : M-SEARCH SSDP, description XML, `SetAVTransportURI` puis `Play`."""

    timeout_s: float = 3.0
    search: Callable[[float], list[str]] | None = None  # rend les LOCATION des réponses (injectable)
    http: Callable[[str, bytes | None, dict[str, str]], bytes] | None = None  # (url, corps, en-têtes) → corps

    def _search(self) -> list[str]:
        if self.search is not None:
            return self.search(self.timeout_s)
        msg = (b"M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\nMX: 2\r\n"
               b"ST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n\r\n")
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.settimeout(0.5)
        locs: list[str] = []
        try:
            sock.sendto(msg, _SSDP)
            end = time.monotonic() + self.timeout_s
            while time.monotonic() < end:
                try:
                    data, _ = sock.recvfrom(4096)
                except TimeoutError:
                    continue
                m = re.search(rb"(?im)^location:\s*(\S+)", data)
                if m and m.group(1).decode(errors="ignore") not in locs:
                    locs.append(m.group(1).decode(errors="ignore"))
        finally:
            sock.close()
        return locs

    def _http(self, url: str, body: bytes | None = None, headers: dict[str, str] | None = None) -> bytes:
        if not _private_http(url):
            raise CastError("adresse DLNA hors du réseau local")
        if self.http is not None:
            return self.http(url, body, headers or {})
        req = urllib.request.Request(url, data=body, headers=headers or {}, method="POST" if body else "GET")
        with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
            return r.read(256 * 1024)

    def discover(self) -> list[Screen]:
        out = []
        for loc in self._search():
            try:
                root = ElementTree.fromstring(self._http(loc))
            except (CastError, OSError, ElementTree.ParseError):
                continue
            ns = {"d": "urn:schemas-upnp-org:device-1-0"}
            dev = root.find("d:device", ns)
            if dev is None:
                continue
            udn = (dev.findtext("d:UDN", "", ns) or loc).strip()
            control = None
            for svc in dev.iter("{urn:schemas-upnp-org:device-1-0}service"):
                if (svc.findtext("d:serviceType", "", ns) or "").startswith("urn:schemas-upnp-org:service:AVTransport"):
                    control = urljoin(loc, (svc.findtext("d:controlURL", "", ns) or "").strip())
            if not control or not _private_http(control):
                continue
            out.append(Screen(id=_safe_id(f"dlna-{udn.removeprefix('uuid:')}"),
                              name=(dev.findtext("d:friendlyName", "", ns) or "Télé DLNA").strip()[:60],
                              kind="dlna", host=urlparse(loc).hostname or "",
                              model=(dev.findtext("d:modelName", "", ns) or "").strip()[:60], caps=(IMAGE,),
                              extra={"control_url": control}))
        return out

    def _soap(self, s: Screen, action: str, args: str) -> None:
        body = _SOAP.format(action=action, args=args).encode()
        self._http(str(s.extra.get("control_url") or ""), body,
                   {"Content-Type": 'text/xml; charset="utf-8"', "SOAPACTION": f'"{_AVT}#{action}"'})

    def show(self, s: Screen, url: str, *, media: str | None = None) -> None:
        from html import escape

        meta = ('<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
                'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/"><item id="0" parentID="-1" restricted="1">'
                '<upnp:class>object.item.imageItem.photo</upnp:class>'
                f'<res protocolInfo="http-get:*:{media or "image/png"}:*">{escape(url)}</res></item></DIDL-Lite>')
        self._soap(s, "SetAVTransportURI",
                   f"<CurrentURI>{escape(url)}</CurrentURI><CurrentURIMetaData>{escape(meta)}</CurrentURIMetaData>")
        self._soap(s, "Play", "<Speed>1</Speed>")

    def stop(self, s: Screen) -> None:
        self._soap(s, "Stop", "")


def screen_dict(s: Screen) -> dict[str, Any]:
    """Écran complet (adresse comprise) : de home-discovery à home-bridge seulement, jamais vers le noyau."""
    return asdict(s)


def screen_from_dict(d: dict[str, Any]) -> Screen:
    known = {k: d[k] for k in ("id", "name", "kind", "host", "port", "model", "room", "paired", "extra") if k in d}
    return Screen(caps=tuple(d.get("caps") or (IMAGE,)), **known)


__all__ = [
    "AIRPLAY_SECRET",
    "IMAGE",
    "KINDS",
    "PAGE",
    "AirPlayBackend",
    "CastError",
    "ChromecastBackend",
    "DlnaBackend",
    "NeedsPairing",
    "Screen",
    "ScreenBook",
    "screen_dict",
    "screen_from_dict",
]
