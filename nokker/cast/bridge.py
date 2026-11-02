"""Écrans du foyer côté passerelle (docs/SCREEN-CAST.md §10, conception de référence docs/LOKKAL-HOME-BRIDGE.md).

Le noyau reste fermé au réseau local : il garde la logique (outil, rendu caviardé, approbations, accords, bulle) et
n'appelle que des OPÉRATIONS NOMMÉES de `home-bridge`, jamais un proxy. Deux pièces ici :

  - **home-discovery** (profil `home-mdns`, `network_mode: host`, aucun port TCP) : la découverte multicast seulement
    (mDNS Cast/AirPlay, SSDP DLNA), servie à home-bridge par la socket Unix `/run/home/discovery.sock` ;
  - **home-bridge** (profil `home`, réseaux `homeapi` + `homelan`) : opérations `cast.*` pour le noyau (jeton
    `HOME_BRIDGE_TOKEN`, comparé en temps constant ; refus de démarrer sans), envoi UNICAST aux écrans découverts
    (pychromecast par hôte, pyatv par hôte, SOAP DLNA), écouteur des pages éphémères (port publié sur l'adresse LAN de
    l'hôte, annoncé aux écrans), identifiants d'appairage AirPlay À LUI (`HOME_STATE/airplay.json`, 0600).

Ces opérations sont servies par la passerelle COMMUNE du foyer (`nokker/homebridge`, table `OPS`, même jeton, même
écoute :8800 que `ha.*`, `audio.*`, `eink.*`, `watch.*`) ; ce module garde le moteur (`CastEngine`), la découverte
(`python -m nokker.homebridge discovery`) et le client du noyau (`HttpBridge`).

Opérations (`POST /v1/op/<nom>`, corps et réponse JSON, `Authorization: Bearer <jeton>`) :

  cast.screens  {refresh?}                                        → {screens: [{id, name, kind, model, caps, paired}]}
  cast.show     {cast_id, screen_id, view, ttl_s, max_uses, pin_screen_ip} → {expires_at}
  cast.update   {cast_id, view, ttl_s, max_uses, pin_screen_ip}    → {expires_at}
  cast.stop     {cast_id}                                         → {stopped}
  cast.pair     {screen_id, pin?}                                 → {state: pin_shown | paired | failed}

`view` = page déjà rendue ET caviardée par le noyau : {kind, title, body, interval_s, media_b64?, media_mime?}.
Erreurs : {"error": {"code", "message"}} ; codes `unknown_screen` 404, `gone` 410, `images_only` 422, `pairing` 409,
`disabled` 403, `unavailable` 503, `unreachable` 502, `bad_request` 400, `unauthorized` 401. Ni l'adresse d'un écran
ni le lien de la page ne sortent vers le noyau."""
from __future__ import annotations

import base64
import http.client
import json
import logging
import os
import socket
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingUnixStreamServer
from typing import Any

from nokker.cast.render import Rendered, page
from nokker.cast.screens import (
    IMAGE,
    PAGE,
    AirPlayBackend,
    CastError,
    ChromecastBackend,
    DlnaBackend,
    NeedsPairing,
    Screen,
    screen_dict,
    screen_from_dict,
)
from nokker.cast.server import CastServer, is_lan

logger = logging.getLogger("nokker.cast.bridge")

MAX_BODY = 16 * 1024 * 1024
OPS = ("cast.screens", "cast.show", "cast.update", "cast.stop", "cast.pair")
DISCOVERY_SOCK = "/run/home/discovery.sock"
STATUS = {"unknown_screen": 404, "gone": 410, "images_only": 422, "pairing": 409, "disabled": 403,
          "unavailable": 503, "bad_request": 400, "unauthorized": 401}


class BridgeError(RuntimeError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


def view_of(r: Rendered) -> dict[str, Any]:
    """Page rendue (noyau) → charge utile de `cast.show` / `cast.update`."""
    v: dict[str, Any] = {"kind": r.kind, "title": r.title, "body": r.body, "interval_s": r.interval_s}
    if r.media is not None:
        v["media_b64"] = base64.b64encode(r.media[0]).decode()
        v["media_mime"] = r.media[1]
    return v


def rendered_of(v: dict[str, Any]) -> Rendered:
    media = None
    if v.get("media_b64"):
        media = (base64.b64decode(str(v["media_b64"])), str(v.get("media_mime") or "image/png"))
    return Rendered(kind=str(v.get("kind") or ""), title=str(v.get("title") or ""), body=str(v.get("body") or ""),
                    interval_s=int(v.get("interval_s") or 0), media=media)


@dataclass
class CredStore:
    """Identifiants d'appairage AirPlay de la passerelle : un fichier 0600 à elle, jamais vu du noyau."""

    path: Path
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def _load(self) -> dict[str, str]:
        try:
            return dict(json.loads(self.path.read_text("utf-8")))
        except (OSError, ValueError):
            return {}

    def get(self, name: str) -> str | None:
        with self._lock:
            return self._load().get(name)

    def set(self, name: str, value: str) -> None:
        with self._lock:
            data = self._load()
            data[name] = value
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data), "utf-8")
            os.chmod(tmp, 0o600)
            tmp.replace(self.path)


# ─────────────────────────────── home-discovery ───────────────────────────────


@dataclass
class Discovery:
    """La découverte multicast (home-discovery) : chaque famille, une panne n'arrête pas les autres. Rend les écrans
    COMPLETS (adresse, port, uuid…) : ils ne vont qu'à home-bridge."""

    backends: dict[str, Any]

    def screens(self, refresh: bool = False) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for kind, backend in self.backends.items():
            try:
                out.extend(screen_dict(s) for s in backend.discover())
            except Exception as e:  # noqa: BLE001 — famille absente (extra non installé), réseau
                logger.info("écrans : découverte %s indisponible (%s)", kind, type(e).__name__)
        return out


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self.unix_path = path

    def connect(self) -> None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self.unix_path)
        self.sock = s


@dataclass
class DiscoveryClient:
    """home-bridge → home-discovery, par la socket Unix du volume partagé."""

    path: str = DISCOVERY_SOCK
    timeout_s: float = 20.0

    def screens(self, refresh: bool = False) -> list[dict[str, Any]]:
        if not os.path.exists(self.path):
            raise BridgeError("unavailable", "découverte absente (profil home-mdns)")
        conn = _UnixHTTPConnection(self.path, self.timeout_s)
        try:
            conn.request("GET", "/v1/discovery/screens" + ("?refresh=1" if refresh else ""))
            r = conn.getresponse()
            data = json.loads(r.read() or b"{}")
        except (OSError, ValueError):
            raise BridgeError("unavailable", "découverte injoignable") from None
        finally:
            conn.close()
        return list(data.get("screens") or [])


class _UnixHTTPServer(ThreadingUnixStreamServer):
    daemon_threads = True

    def get_request(self) -> tuple[Any, Any]:
        req, _ = super().get_request()
        return req, ("unix", 0)  # BaseHTTPRequestHandler attend une adresse (hôte, port)


def discovery_handler(discovery: Discovery) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def do_GET(self) -> None:
            path, _, query = self.path.partition("?")
            if path != "/v1/discovery/screens":
                status, data = 404, {"error": {"code": "bad_request"}}
            else:
                status, data = 200, {"screens": discovery.screens(refresh="refresh=1" in query)}
            raw = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    return Handler


def serve_discovery(discovery: Discovery, path: str = DISCOVERY_SOCK) -> _UnixHTTPServer:
    """Socket Unix 0600 dans un dossier 0700 (volume `home-sock`) ; aucun port TCP."""
    d = os.path.dirname(path) or "."
    os.makedirs(d, mode=0o700, exist_ok=True)
    if os.path.exists(path):
        os.unlink(path)
    old = os.umask(0o177)
    try:
        srv = _UnixHTTPServer(path, discovery_handler(discovery))
    finally:
        os.umask(old)
    threading.Thread(target=srv.serve_forever, daemon=True, name="nokker-home-discovery").start()
    return srv


# ─────────────────────────────── home-bridge ───────────────────────────────


@dataclass
class _Live:
    screen: Screen
    via_media: bool


@dataclass
class CastEngine:
    """Les opérations `cast.*` de home-bridge. `discover` = la découverte (client de la socket de home-discovery ;
    `Discovery` en direct dans les tests). Les tests du noyau l'utilisent aussi en direct (mêmes méthodes que
    `HttpBridge`)."""

    backends: dict[str, Any]
    server: CastServer
    discover: Any = None  # .screens(refresh) -> écrans complets
    enabled: bool = True  # `cast.enabled` de home.yaml (livré vide : éteint)
    rasterize: Callable[[str], bytes] | None = None  # page → PNG pour les écrans média seulement ; None = refus
    _screens: dict[str, Screen] = field(default_factory=dict, repr=False)
    _live: dict[str, _Live] = field(default_factory=dict, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def _check(self) -> None:
        if not self.enabled:
            raise BridgeError("disabled", "écrans non autorisés dans home.yaml (cast.enabled)")

    def _caps(self, s: Screen) -> list[str]:
        return list(s.caps) if PAGE in s.caps or self.rasterize is None else [PAGE, IMAGE]

    def _public(self, s: Screen) -> dict[str, Any]:
        return {"id": s.id, "name": s.name, "kind": s.kind, "model": s.model, "caps": self._caps(s),
                "paired": s.paired}

    def screens(self, refresh: bool = False) -> list[dict[str, Any]]:
        self._check()
        with self._lock:
            if self._screens and not refresh:
                return [self._public(s) for s in self._screens.values()]
        found: dict[str, Screen] = {}
        for d in (self.discover.screens(refresh) if self.discover is not None else []):
            s = screen_from_dict(d)
            # liste blanche : un écran n'est joint que sur une adresse privée du LAN, jamais Internet
            if s.kind in self.backends and s.host and is_lan(s.host, loopback=self.server.allow_loopback):
                found[s.id] = s
        with self._lock:
            self._screens = found
        return [self._public(s) for s in found.values()]

    def _screen(self, screen_id: str) -> Screen:
        with self._lock:
            s = self._screens.get(screen_id)
        if s is None:
            self.screens(refresh=True)
            with self._lock:
                s = self._screens.get(screen_id)
        if s is None:
            raise BridgeError("unknown_screen", "écran inconnu")
        return s

    def _media(self, s: Screen, r: Rendered) -> tuple[bytes, str] | None:
        if PAGE in s.caps:
            return r.media if r.kind == "image" and s.kind == "chromecast" else None
        if r.media is not None:
            return r.media
        if self.rasterize is None:
            raise BridgeError("images_only", "écran média seulement")
        r.media = (self.rasterize(page(r, live=False)), "image/png")
        return r.media

    def show(self, cast_id: str, screen_id: str, view: dict[str, Any], *, ttl_s: float = 900, max_uses: int = 4,
             pin_screen_ip: bool = True) -> dict[str, Any]:
        self._check()
        s = self._screen(screen_id)
        backend = self.backends.get(s.kind)
        if backend is None:
            raise BridgeError("unknown_screen", "famille d'écran désactivée")
        r = rendered_of(view)
        media = self._media(s, r)
        token = self.server.publish(cast_id, r, ttl_s=min(float(ttl_s), 3600.0), max_uses=int(max_uses),
                                    pin_ip=s.host if pin_screen_ip else None)
        try:
            backend.show(s, self.server.url_for(token, "/media" if media is not None else ""),
                         media=media[1] if media is not None else None)
        except NeedsPairing:
            self.server.revoke(cast_id, "failed")
            raise BridgeError("pairing", "appairage requis") from None
        except Exception as e:  # noqa: BLE001 — appareil injoignable, bibliothèque absente
            self.server.revoke(cast_id, "failed")
            raise BridgeError("unreachable", str(e) if isinstance(e, CastError) else type(e).__name__) from None
        with self._lock:
            self._live[cast_id] = _Live(screen=s, via_media=media is not None)
        logger.info("cast.show %s sur %s (%s)", cast_id, s.id, "média" if media is not None else "page")
        return {"expires_at": self.server.expires_at(cast_id)}

    def update(self, cast_id: str, view: dict[str, Any], *, ttl_s: float = 900, max_uses: int = 4,
               pin_screen_ip: bool = True) -> dict[str, Any]:
        self._check()
        with self._lock:
            live = self._live.get(cast_id)
        if live is None:
            raise BridgeError("gone", "affichage terminé")
        r = rendered_of(view)
        if not live.via_media and self._media(live.screen, r) is None:
            if not self.server.update(cast_id, r):  # page vivante : poussée par le flux SSE
                raise BridgeError("gone", "affichage terminé")
            return {"expires_at": self.server.expires_at(cast_id)}
        return self.show(cast_id, live.screen.id, view, ttl_s=ttl_s, max_uses=max_uses, pin_screen_ip=pin_screen_ip)

    def stop(self, cast_id: str) -> dict[str, Any]:
        self.server.revoke(cast_id, "stop")
        with self._lock:
            live = self._live.pop(cast_id, None)
        if live is not None:
            try:
                self.backends[live.screen.kind].stop(live.screen)
            except Exception:
                logger.debug("écrans : arrêt sur l'appareil en échec", exc_info=True)
        return {"stopped": True}

    def pair(self, screen_id: str, pin: str | None) -> dict[str, Any]:
        self._check()
        s = self._screen(screen_id)
        backend = self.backends.get("airplay")
        if s.kind != "airplay" or backend is None:
            raise BridgeError("unknown_screen", "écran AirPlay inconnu")
        try:
            if not pin:
                backend.pair_begin(s)
                return {"state": "pin_shown"}
            ok = backend.pair_finish(s, str(pin))
        except CastError as e:
            raise BridgeError("pairing", str(e)) from None
        if ok:
            s.paired = True
        return {"state": "paired" if ok else "failed"}

    def op(self, name: str, body: dict[str, Any]) -> dict[str, Any]:
        """Une opération nommée (liste fermée `OPS`)."""
        kw = {k: body[k] for k in ("ttl_s", "max_uses", "pin_screen_ip") if k in body}
        if name == "cast.screens":
            return {"screens": self.screens(refresh=body.get("refresh") is True)}
        if name == "cast.show":
            return self.show(str(body["cast_id"]), str(body["screen_id"]), dict(body["view"]), **kw)
        if name == "cast.update":
            return self.update(str(body["cast_id"]), dict(body["view"]), **kw)
        if name == "cast.stop":
            return self.stop(str(body["cast_id"]))
        if name == "cast.pair":
            return self.pair(str(body["screen_id"]), body.get("pin") or None)
        raise BridgeError("bad_request", "opération inconnue")

    def close(self) -> None:
        self.server.close()


# ─────────────────────────────── client du noyau ───────────────────────────────


@dataclass
class HttpBridge:
    """Ce que le noyau utilise : les opérations `cast.*` de home-bridge, rien d'autre (aucun accès LAN)."""

    base_url: str
    token: Callable[[], str]
    timeout_s: float = 30.0
    client: Any = None  # httpx.Client injectable

    def _op(self, name: str, body: dict[str, Any]) -> dict[str, Any]:
        import httpx

        c = self.client or httpx.Client(timeout=self.timeout_s)
        try:
            r = c.post(self.base_url.rstrip("/") + f"/v1/op/{name}", json=body,
                       headers={"Authorization": f"Bearer {self.token()}"})
        except httpx.HTTPError:
            raise BridgeError("unreachable", "home-bridge injoignable") from None
        finally:
            if self.client is None:
                c.close()
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code >= 400:
            err = (data or {}).get("error") if isinstance(data, dict) else None
            if not isinstance(err, dict):  # refus de la passerelle hors moteur (jeton, opération inconnue) : texte seul
                err = {"code": "unauthorized" if r.status_code == 401 else "unreachable", "message": str(err or "")}
            raise BridgeError(str(err.get("code") or "unreachable"), str(err.get("message") or r.status_code))
        return data

    def screens(self, refresh: bool = False) -> list[dict[str, Any]]:
        return list(self._op("cast.screens", {"refresh": refresh}).get("screens") or [])

    def show(self, cast_id: str, screen_id: str, view: dict[str, Any], **kw: Any) -> dict[str, Any]:
        return self._op("cast.show", {"cast_id": cast_id, "screen_id": screen_id, "view": view, **kw})

    def update(self, cast_id: str, view: dict[str, Any], **kw: Any) -> dict[str, Any]:
        return self._op("cast.update", {"cast_id": cast_id, "view": view, **kw})

    def stop(self, cast_id: str) -> dict[str, Any]:
        return self._op("cast.stop", {"cast_id": cast_id})

    def pair(self, screen_id: str, pin: str | None) -> dict[str, Any]:
        return self._op("cast.pair", {"screen_id": screen_id, "pin": pin})

    def close(self) -> None:
        return None


# ─────────────────────────────── lancement ───────────────────────────────


def _backends(families: list[str], timeout: float, creds: CredStore | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if "chromecast" in families:
        out["chromecast"] = ChromecastBackend(timeout_s=timeout)
    if "airplay" in families:
        out["airplay"] = AirPlayBackend(secret_get=creds.get if creds else (lambda _n: None),
                                        secret_set=creds.set if creds else (lambda _n, _v: None), timeout_s=timeout)
    if "dlna" in families:
        out["dlna"] = DlnaBackend(timeout_s=min(timeout, 3.0))
    return out


def _home_cast(env: dict[str, str]) -> dict[str, Any]:
    """Section `cast` de home.yaml (`HOME_CONFIG`) ; fichier livré vide → écrans non autorisés."""
    path = env.get("HOME_CONFIG") or "/etc/home/home.yaml"
    try:
        import yaml

        data = yaml.safe_load(Path(path).read_text("utf-8")) or {}
    except (OSError, ValueError):
        data = {}
    cast = data.get("cast") if isinstance(data, dict) else None
    return cast if isinstance(cast, dict) else {}


def engine_from_env(env: dict[str, str] | None = None, conf: dict[str, Any] | None = None) -> CastEngine:
    """Moteur des écrans de home-bridge. `conf` = section `cast` de home.yaml déjà lue par la passerelle (sinon lue
    depuis HOME_CONFIG) : `{enabled, families, ttl_max_s}`. Variables : HOME_STATE (identifiants AirPlay, volume
    inscriptible), HOME_SOCK (socket de home-discovery), CAST_LISTEN (écoute des pages dans le conteneur, défaut
    0.0.0.0:8802 — 8801 est l'écoute du foyer de la passerelle), CAST_ADVERTISE (`<adresse LAN de l'hôte>:<port
    publié>`, obligatoire)."""
    env = dict(os.environ if env is None else env)
    conf = _home_cast(env) if conf is None else conf
    families = list(conf.get("families") or ["chromecast", "airplay", "dlna"])
    creds = CredStore(Path(env.get("HOME_STATE") or "/var/lib/home") / "airplay.json")
    host, _, port = (env.get("CAST_LISTEN") or "0.0.0.0:8802").rpartition(":")
    server = CastServer(bind=host or None, port=int(port or 8802), advertise=env.get("CAST_ADVERTISE") or None)
    return CastEngine(backends=_backends(families, 5.0, creds), server=server,
                      discover=DiscoveryClient(env.get("HOME_SOCK") or DISCOVERY_SOCK),
                      enabled=conf.get("enabled") is True)


def discovery_main(env: dict[str, str] | None = None) -> int:  # pragma: no cover
    """`python -m nokker.homebridge discovery` (conteneur home-discovery, profil `home-mdns`) : découverte multicast,
    servie à home-bridge par la socket Unix HOME_SOCK. Aucun port TCP."""
    import time

    env = dict(os.environ if env is None else env)
    families = [f.strip() for f in (env.get("CAST_FAMILIES") or "chromecast,airplay,dlna").split(",")]
    serve_discovery(Discovery(_backends(families, 5.0, None)), env.get("HOME_SOCK") or DISCOVERY_SOCK)
    logger.info("home-discovery : socket %s", env.get("HOME_SOCK") or DISCOVERY_SOCK)
    while True:
        time.sleep(3600)


__all__ = ["DISCOVERY_SOCK", "OPS", "STATUS", "BridgeError", "CastEngine", "CredStore", "Discovery", "DiscoveryClient",
           "HttpBridge", "discovery_main", "engine_from_env", "rendered_of", "serve_discovery", "view_of"]
