"""Serveur de la passerelle du foyer : deux écoutes séparées.

1. API du noyau (`api_listen`, défaut :8800, réseau interne `homeapi` seulement) : `POST /v1/op/<nom>` avec
   `Authorization: Bearer <HOME_BRIDGE_TOKEN>` (comparé en temps constant) ; `GET /v1/health`. Opérations NOMMÉES
   seulement (table `OPS`), jamais de proxy ; chaque opération est journalisée sans contenu ni secret.
2. Écoute du foyer (`lan.listen`, défaut :8801, publiée sur le LAN) : ce que les APPAREILS lisent, rien d'autre :
     GET  /clip/<id>             extrait audio (enceintes, Home Assistant) ;
     GET  /eink/<jeton>.png      image d'un écran (Kindle, cadre générique) ;
     GET  /api/setup             appairage TRMNL (en-tête ID) ;
     GET  /api/display           protocole TRMNL (en-tête Access-Token) ;
     GET  /media/v1/thumb/<id>   vignette d'un événement de caméra (URL signée par le noyau, docs/HOME-WATCH.md) ;
     GET  /media/v1/live/<cam>   direct d'une caméra listée dans `watch.frigate.live` (même signature) ;
     POST /api/log               journaux TRMNL (acceptés et jetés) ;
     POST /knock/event           déclencheur d'un satellite du heurtoir (en-têtes X-Satellite + Bearer du satellite).
   Aucune opération du noyau n'est joignable par cette écoute.
3. Écrans du foyer (`cast.*`, docs/SCREEN-CAST.md, section `cast` de home.yaml) : moteur `nokker.cast.bridge.CastEngine`
   servi par la MÊME API (:8800) ; ses pages éphémères ont leur propre écoute (`CAST_LISTEN`, défaut :8802, annoncée aux
   écrans par `CAST_ADVERTISE`). La découverte multicast est le conteneur `home-discovery`
   (`python -m nokker.homebridge discovery`), joint par socket Unix.
"""
from __future__ import annotations

import base64
import binascii
import hmac
import json
import logging
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from nokker.ambient.presence import RoomsConfig, StatesPresence
from nokker.cast.server import RELAY_HEADERS  # en-têtes posés par un relais : jamais le noyau
from nokker.homebridge.audio import AudioRouter, ClipStore, build_sink
from nokker.homebridge.config import BridgeConfig
from nokker.homebridge.eink import FrameCache
from nokker.homebridge.ha import HaError, HaOps
from nokker.homebridge.ha import from_config as ha_from_config
from nokker.homebridge.lan import Lan, LanError
from nokker.homebridge.watch import WATCH_OPS, serve_media

logger = logging.getLogger("nokker.homebridge")

MAX_OP_BODY = 12 * 1024 * 1024  # audio en base64
_RE_OP = re.compile(r"^/v1/op/([a-z_]+\.[a-z_]+)$")
_RE_CLIP = re.compile(r"^/clip/([A-Za-z0-9_-]{20,64})$")
_RE_PNG = re.compile(r"^/eink/(nkd_[A-Za-z0-9_-]{32,64})\.png$")


class OpError(Exception):
    def __init__(self, status: int, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        # Code stable d'une opération `cast.*` (`nokker.cast.bridge.STATUS`) : la réponse devient
        # {"error": {"code", "message"}}, forme lue par `nokker.cast.bridge.HttpBridge`. None : {"error": message}.
        self.code = code


@dataclass
class Bridge:
    """L'état de la passerelle et ses opérations (sans HTTP : testable directement)."""

    cfg: BridgeConfig
    frames: FrameCache
    clips: ClipStore
    audio: AudioRouter
    presence_source: Any  # StatesPresence | None
    ha_ops: HaOps | None = None  # opérations ha.* (docs/HOME-ASSISTANT.md §2.3), si home_assistant.url est posé
    watch: Any = None  # veille du foyer (nokker/homebridge/watch/, docs/HOME-WATCH.md) : `watch` de home.yaml, ou None
    cast: Any = None  # écrans du foyer (`nokker.cast.bridge.CastEngine`) si `cast.enabled: true` dans home.yaml, sinon None
    knock: Any = None  # KnockBridge | None (heurtoir, docs/KNOCK.md §8 ; bloc `knock` de home.yaml)

    @classmethod
    def build(cls, cfg: BridgeConfig, *, ha: Any = None, sinks: dict[str, list[Any]] | None = None,
              watch: Any = None, cast: Any = None) -> Bridge:
        lan = Lan(cfg.allow)
        raw = cfg.raw
        ha_raw = raw.get("home_assistant") or {}
        if ha is None and ha_raw.get("url"):
            ha = ha_from_config(ha_raw, lan, cfg.secret)
        rooms_cfg = RoomsConfig.from_config(raw)
        if sinks is None:
            sinks = {room: [build_sink(s, lan=lan, ha=ha) for s in (r.get("speakers") or [])]
                     for room, r in rooms_cfg.rooms.items()}
        clips = ClipStore()
        presence = None
        if ha is not None and rooms_cfg.rooms:
            wanted = rooms_cfg.entities()
            presence = StatesPresence(states=lambda: [s for s in ha.states() if s.get("entity_id") in wanted],
                                      cfg=rooms_cfg)
        ha_ops = HaOps(ha=ha, entities=tuple(str(e) for e in ha_raw.get("entities") or []),
                       notify_services=tuple(str(e) for e in ha_raw.get("notify_services") or [])) \
            if ha is not None and hasattr(ha, "registry") else None
        if watch is None:
            from nokker.homebridge.watch import Watch

            watch = Watch.build(cfg)
        knock = None
        if raw.get("knock"):
            from nokker.homebridge.knock.events import KnockBridge, KnockConfig

            knock = KnockBridge(KnockConfig.from_raw(raw["knock"], cfg.secret))
        return cls(cfg=cfg, frames=FrameCache(), clips=clips,
                   audio=AudioRouter(rooms=sinks, clips=clips, lan_url=cfg.lan_url), presence_source=presence,
                   ha_ops=ha_ops, watch=watch, cast=cast if cast is not None else _cast_engine(raw), knock=knock)

    # ── opérations nommées ──
    def op(self, name: str, p: dict[str, Any]) -> dict[str, Any]:
        fn = OPS.get(name)
        if fn is None:
            raise OpError(404, f"opération inconnue : {name}")
        return fn(self, p)

    def _presence(self, p: dict[str, Any]) -> dict[str, Any]:
        if self.presence_source is None:
            return {"ok": False, "rooms": {}, "guests": False}
        pr = self.presence_source.presence()
        return {"ok": pr.ok, "guests": pr.guests,
                "rooms": {r: {"members": sorted(st.members), "occupied": st.occupied, "unknown": st.unknown}
                          for r, st in pr.rooms.items()}}

    def _play(self, p: dict[str, Any]) -> dict[str, Any]:
        try:
            audio = base64.b64decode(str(p.get("audio_b64") or ""), validate=True)
        except (binascii.Error, ValueError):
            raise OpError(400, "audio_b64 illisible") from None
        return self.audio.play(str(p.get("room") or ""), audio, str(p.get("mime") or "audio/ogg"))

    def _publish(self, p: dict[str, Any]) -> dict[str, Any]:
        try:
            png = base64.b64decode(str(p.get("png_b64") or ""), validate=True)
            self.frames.publish(str(p.get("display_id") or ""), str(p.get("token_sha256") or ""), png,
                                str(p.get("filename") or ""), int(p.get("refresh_s") or 900),
                                int(p.get("ttl_s") or 2700))
        except (binascii.Error, ValueError, TypeError) as e:
            raise OpError(400, str(e) or "eink.publish invalide") from None
        return {"ok": True}

    def _revoke(self, p: dict[str, Any]) -> dict[str, Any]:
        return {"ok": self.frames.revoke(str(p.get("display_id") or ""))}

    def _retain(self, p: dict[str, Any]) -> dict[str, Any]:
        ids = p.get("display_ids")
        if not isinstance(ids, list):
            raise OpError(400, "display_ids : une liste")
        return {"dropped": self.frames.retain([str(i) for i in ids])}

    def _knock(self, name: str, p: dict[str, Any]) -> dict[str, Any]:
        if self.knock is None:
            raise OpError(404, "heurtoir absent de home.yaml")
        try:
            return getattr(self.knock, name)(p)
        except ValueError as e:
            raise OpError(400, str(e)) from None

    def _offer(self, p: dict[str, Any]) -> dict[str, Any]:
        try:
            self.frames.setup_offer(str(p.get("mac_sha256") or ""), str(p.get("token") or ""),
                                    int(p.get("ttl_s") or 900))
        except (ValueError, TypeError) as e:
            raise OpError(400, str(e)) from None
        return {"ok": True}


    def _ha(self, name: str, p: dict[str, Any]) -> dict[str, Any]:
        if self.ha_ops is None:
            raise OpError(404, "Home Assistant absent de home.yaml")
        try:
            return getattr(self.ha_ops, name)(p)
        except PermissionError as e:  # entité hors liste, destination refusée (Refused)
            raise OpError(403, str(e)) from None
        except ValueError as e:
            raise OpError(400, str(e)) from None
        except (HaError, LanError) as e:
            raise OpError(502, str(e)) from None


    def _cast(self, name: str, p: dict[str, Any]) -> dict[str, Any]:
        from nokker.cast.bridge import STATUS, BridgeError

        if self.cast is None:
            raise OpError(STATUS["disabled"], "écrans non autorisés (section cast de home.yaml)", code="disabled")
        try:
            return self.cast.op(name, p)
        except BridgeError as e:
            raise OpError(STATUS.get(e.code, 502), str(e), code=e.code) from None
        except (KeyError, TypeError, ValueError):
            raise OpError(400, "bad_request", code="bad_request") from None


def _cast_engine(raw: dict[str, Any]) -> Any:
    """Moteur des écrans (docs/SCREEN-CAST.md) si `cast.enabled: true` dans home.yaml ; None sinon (aucune écoute)."""
    conf = raw.get("cast")
    if not isinstance(conf, dict) or conf.get("enabled") is not True:
        return None
    from nokker.cast.bridge import engine_from_env

    return engine_from_env(conf=conf)


def _cast_op(name: str) -> Callable[[Bridge, dict[str, Any]], dict[str, Any]]:
    return lambda bridge, p: bridge._cast(name, p)


def _ha_op(name: str) -> Callable[[Bridge, dict[str, Any]], dict[str, Any]]:
    return lambda bridge, p: bridge._ha(name, p)


OPS: dict[str, Callable[[Bridge, dict[str, Any]], dict[str, Any]]] = {
    "ha.states": _ha_op("states"),
    "ha.registry": _ha_op("registry"),
    "ha.call_service": _ha_op("call"),
    "ha.write_config": _ha_op("write_config"),
    "ha.events": _ha_op("events"),
    "ha.query": _ha_op("query"),
    "ha.history": _ha_op("history"),
    "ha.logbook": _ha_op("logbook"),
    "ha.notify": _ha_op("notify"),
    "presence.get": Bridge._presence,
    "audio.play": Bridge._play,
    "eink.publish": Bridge._publish,
    "eink.revoke": Bridge._revoke,
    "eink.retain": Bridge._retain,
    "eink.setup_offer": Bridge._offer,
    "knock.events": lambda b, p: b._knock("op_events", p),
    "knock.status": lambda b, p: b._knock("op_status", p),
    "knock.mute": lambda b, p: b._knock("op_mute", p),
}
OPS.update(WATCH_OPS)  # veille du foyer : watch.targets, cameras.reviews, network.clients, nas.status, watch.action
# écrans du foyer (docs/SCREEN-CAST.md) : liste fermée de `nokker.cast.bridge.OPS`
OPS.update({n: _cast_op(n) for n in ("cast.screens", "cast.show", "cast.update", "cast.stop", "cast.pair")})


def _send(h: BaseHTTPRequestHandler, status: int, body: bytes, ctype: str = "application/json") -> None:
    h.send_response(status)
    h.send_header("Content-Type", ctype)
    h.send_header("Content-Length", str(len(body)))
    h.send_header("Cache-Control", "no-store")
    h.end_headers()
    h.wfile.write(body)


def _json(h: BaseHTTPRequestHandler, status: int, obj: Any) -> None:
    _send(h, status, json.dumps(obj, ensure_ascii=False).encode("utf-8"))


def api_handler(bridge: Bridge) -> type[BaseHTTPRequestHandler]:
    class Api(BaseHTTPRequestHandler):
        server_version = "home-bridge"

        def log_message(self, *a: Any) -> None:  # pas de journal d'accès (pas d'en-tête recopié)
            pass

        def _authed(self) -> bool:
            auth = self.headers.get("Authorization") or ""
            given = auth[7:].strip() if auth.startswith("Bearer ") else ""
            return bool(given) and hmac.compare_digest(given.encode(), bridge.cfg.token.encode())

        def do_GET(self) -> None:
            if self.path == "/v1/health":
                _json(self, 200, {"ok": True, "ops": sorted(OPS)}) if self._authed() else _json(self, 401, {})
                return
            _json(self, 404, {"error": "not found"})

        def do_POST(self) -> None:
            # Un appel relayé (en-tête de proxy) n'est jamais celui du noyau, qui parle en direct sur `homeapi`.
            if not self._authed() or any(self.headers.get(h) for h in RELAY_HEADERS):
                _json(self, 401, {"error": "unauthorized"})
                return
            m = _RE_OP.match(self.path)
            if not m:
                _json(self, 404, {"error": "not found"})
                return
            try:
                n = int(self.headers.get("Content-Length", 0) or 0)
            except ValueError:
                n = -1
            if n < 0 or n > MAX_OP_BODY:
                _json(self, 413, {"error": "corps trop gros"})
                return
            try:
                payload = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                _json(self, 400, {"error": "json illisible"})
                return
            name = m.group(1)
            try:
                out = bridge.op(name, payload if isinstance(payload, dict) else {})
                logger.info("op %s : ok", name)
                _json(self, 200, out)
            except OpError as e:
                logger.info("op %s : %s", name, e.status)
                _json(self, e.status, {"error": {"code": e.code, "message": str(e)} if e.code else str(e)})
            except Exception as e:  # noqa: BLE001
                logger.warning("op %s : %s", name, type(e).__name__)
                _json(self, 502, {"error": type(e).__name__})

    return Api


def lan_handler(bridge: Bridge) -> type[BaseHTTPRequestHandler]:
    class Lan_(BaseHTTPRequestHandler):
        server_version = "home-bridge"

        def log_message(self, *a: Any) -> None:
            pass

        def _image_url(self, token: str, filename: str) -> str:
            base = bridge.cfg.lan_url or f"http://{self.headers.get('Host') or 'localhost'}"
            return f"{base}/eink/{token}.png?v={filename}"

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            m = _RE_CLIP.match(path)
            if m:
                got = bridge.clips.get(m.group(1))
                if got is None:
                    _json(self, 404, {"error": "not found"})
                else:
                    _send(self, 200, got[0], got[1])
                return
            m = _RE_PNG.match(path)
            if m:
                e = bridge.frames.lookup(m.group(1))
                if e is None:
                    _json(self, 401, {"error": "jeton d'écran inconnu ou révoqué"})
                else:
                    _send(self, 200, e.png, "image/png")
                return
            if path == "/api/setup":
                token = bridge.frames.take_offer(self.headers.get("ID") or "")
                if token is None:
                    _json(self, 404, {"status": 404, "api_key": None, "friendly_id": None, "image_url": None,
                                      "message": "écran inconnu : déclarez-le d'abord dans l'app (adresse MAC)"})
                else:
                    _json(self, 200, {"status": 200, "api_key": token, "friendly_id": "", "filename": "setup",
                                      "image_url": self._image_url(token, "setup"), "message": "Écran relié au foyer"})
                return
            if path.startswith("/media/v1/"):  # veille : vignette ou direct, URL signée par le noyau (≤ 5 min)
                from urllib.parse import parse_qs

                serve_media(bridge, self, path, parse_qs(self.path.split("?", 1)[1] if "?" in self.path else ""))
                return
            if path == "/api/display":
                token = (self.headers.get("Access-Token") or self.headers.get("Access_Token") or "").strip()
                e = bridge.frames.lookup(token)
                if e is None:
                    _json(self, 401, {"status": 401, "error": "jeton d'écran inconnu ou révoqué"})
                    return
                _json(self, 200, {"status": 0, "image_url": self._image_url(token, e.filename),
                                  "filename": e.filename, "refresh_rate": e.refresh_s, "reset_firmware": False,
                                  "update_firmware": False, "firmware_url": None, "special_function": "none"})
                return
            _json(self, 404, {"error": "not found"})

        def do_POST(self) -> None:
            if self.path.split("?", 1)[0] == "/knock/event":
                try:
                    n = min(int(self.headers.get("Content-Length", 0) or 0), 4096)
                    body = json.loads(self.rfile.read(n) or b"{}")
                except ValueError:
                    _json(self, 400, {"error": "json illisible"})
                    return
                auth = self.headers.get("Authorization") or ""
                ok = bridge.knock is not None and bridge.knock.accept_satellite(
                    self.headers.get("X-Satellite") or "", auth[7:].strip() if auth.startswith("Bearer ") else "",
                    body)
                _json(self, 202, {"ok": True}) if ok else _json(self, 401, {"error": "unauthorized"})
                return
            if self.path.split("?", 1)[0] == "/api/log":
                try:
                    n = min(int(self.headers.get("Content-Length", 0) or 0), 64 * 1024)
                except ValueError:
                    n = 0
                self.rfile.read(n)  # lu et jeté
                token = (self.headers.get("Access-Token") or self.headers.get("Access_Token") or "").strip()
                _send(self, 204, b"") if bridge.frames.lookup(token) else _json(self, 401, {"status": 401})
                return
            _json(self, 404, {"error": "not found"})

    return Lan_


def serve(bridge: Bridge) -> tuple[ThreadingHTTPServer, ThreadingHTTPServer]:
    api = ThreadingHTTPServer(bridge.cfg.api_listen, api_handler(bridge))
    lan = ThreadingHTTPServer(bridge.cfg.lan_listen, lan_handler(bridge))
    for s, name in ((api, "home-bridge-api"), (lan, "home-bridge-lan")):
        threading.Thread(target=s.serve_forever, name=name, daemon=True).start()
    if bridge.knock is not None:
        bridge.knock.serve_socket()  # home-audio → passerelle (socket Unix du volume home-sock)
    return api, lan


__all__ = ["OPS", "Bridge", "OpError", "api_handler", "lan_handler", "serve"]
