"""Veille du foyer dans la passerelle (docs/HOME-WATCH.md §4-§5) : Frigate (caméras), UniFi (réseau), Synology et
TrueNAS (NAS). Opérations NOMMÉES greffées sur la table `OPS` de `nokker/homebridge/server.py`, réponses en TEXTE
seulement ; les images ne sortent que par les routes média de l'écoute du foyer, vers un écran local, sur une URL
signée par le noyau (`media_key`, ≤ 5 min).

`home.yaml` :

    watch:
      frigate: {url: http://192.168.1.10:5000, user: veille, password: "secret:FRIGATE_PASSWORD", cameras: [...],
                live: [portail]}
      unifi: {url: https://192.168.1.1, api_key: "secret:UNIFI_API_KEY", site: default, verify_tls: false,
              pause: {console_salon: <uuid d'une règle de pare-feu BLOCK créée dans la console>}}
      synology: {url: https://192.168.1.20:5001, user: veille-ro, password: "secret:DSM_PASSWORD",
                 dsm_internal_api: false, backups: {photos: 3}}
      truenas: {url: wss://192.168.1.30/api/current, api_key: "secret:TRUENAS_API_KEY",
                backups: {photos_s3: "cloudsync:2"}}

`secret:NOM` = fichier `secrets/NOM` à côté de home.yaml (0600), jamais une valeur en clair (refusée).
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from nokker.homebridge.config import BridgeConfig, BridgeConfigError, Refused
from nokker.homebridge.watch.net import MAX_MEDIA_BYTES, UpstreamError, WatchEnv

logger = logging.getLogger("nokker.homebridge.watch")

MEDIA_KEY_LABEL = b"nokker-home-media-v1"
MEDIA_TTL_MAX_S = 300
LIVE_MAX_S = 600
ACTIONS = ("pause_device", "resume_device", "run_backup")
SECRET_KEYS = ("password", "api_key", "token")
_RE_THUMB = re.compile(r"^/media/v1/thumb/([A-Za-z0-9._-]{1,64})$")
_RE_LIVE = re.compile(r"^/media/v1/live/([A-Za-z0-9_-]{1,64})$")
_HEX = re.compile(r"^[0-9a-f]{64}$")


def media_key(token: str) -> bytes:
    return hmac.new(token.encode("utf-8"), MEDIA_KEY_LABEL, hashlib.sha256).digest()


def media_sig(key: bytes, path: str, exp: int) -> str:
    return hmac.new(key, f"{path}\n{int(exp)}".encode(), hashlib.sha256).hexdigest()


@dataclass
class Watch:
    """Les adaptateurs configurés (absents = None) et la clé des URL média."""

    frigate: Any = None
    unifi: Any = None
    nas: list[Any] = field(default_factory=list)
    key: bytes = b""
    clock: Any = time.time
    live_max_s: int = LIVE_MAX_S

    @classmethod
    def build(cls, cfg: BridgeConfig, truenas_rpc: Any = None) -> Watch | None:
        w = cfg.raw.get("watch") or {}
        if not w:
            return None
        for name, block in w.items():
            for key in SECRET_KEYS:
                v = (block or {}).get(key)
                if v is not None and not str(v).startswith("secret:"):
                    raise BridgeConfigError(f"watch.{name}.{key} : une référence secret:NOM, jamais une valeur en clair")
        from nokker.homebridge.watch.frigate import Frigate
        from nokker.homebridge.watch.nas import Synology, TrueNAS
        from nokker.homebridge.watch.unifi import UniFi

        env = WatchEnv(cfg)
        nas: list[Any] = []
        if w.get("synology"):
            nas.append(Synology.from_config(env, w["synology"]))
        if w.get("truenas"):
            nas.append(TrueNAS.from_config(env, w["truenas"], truenas_rpc))
        return cls(frigate=Frigate.from_config(env, w["frigate"]) if w.get("frigate") else None,
                   unifi=UniFi.from_config(env, w["unifi"]) if w.get("unifi") else None, nas=nas,
                   key=media_key(cfg.token))

    def media_ok(self, path: str, query: dict[str, list[str]]) -> bool:
        try:
            exp = int((query.get("exp") or ["0"])[0])
        except ValueError:
            return False
        sig = (query.get("sig") or [""])[0]
        now = self.clock()
        if not _HEX.match(sig) or exp < now or exp > now + MEDIA_TTL_MAX_S + 5:
            return False
        return hmac.compare_digest(sig, media_sig(self.key, path, exp))


# ─────────────────────────────── opérations nommées (table OPS) ───────────────────────────────

def _err(status: int, message: str) -> Exception:
    from nokker.homebridge.server import OpError

    return OpError(status, message)


def _guard(fn: Any) -> Any:
    def run(bridge: Any, p: dict[str, Any]) -> dict[str, Any]:
        w = getattr(bridge, "watch", None)
        if w is None:
            raise _err(404, "veille non configurée (watch dans home.yaml)")
        try:
            return fn(w, p)
        except UpstreamError as e:
            raise _err(502, str(e)) from None
        except (Refused, BridgeConfigError) as e:
            raise _err(403, str(e)) from None
    run.__name__ = fn.__name__
    return run


@_guard
def op_targets(w: Watch, p: dict[str, Any]) -> dict[str, Any]:
    return {"pause": w.unifi.pause_targets() if w.unifi else [],
            "backups": sorted({k for n in w.nas for k in n.backups}),
            "live": list(w.frigate.live_cameras) if w.frigate else []}


@_guard
def op_reviews(w: Watch, p: dict[str, Any]) -> dict[str, Any]:
    if w.frigate is None:
        raise _err(404, "Frigate non configuré")
    try:
        after, limit = float(p.get("after") or 0), int(p.get("limit") or 100)
    except (TypeError, ValueError):
        raise _err(400, "after/limit numériques") from None
    return {"items": w.frigate.reviews(after, limit), "now": w.clock()}


@_guard
def op_clients(w: Watch, p: dict[str, Any]) -> dict[str, Any]:
    if w.unifi is None:
        raise _err(404, "UniFi non configuré")
    return {"clients": w.unifi.clients(), "at": w.clock()}


@_guard
def op_nas(w: Watch, p: dict[str, Any]) -> dict[str, Any]:
    systems = []
    for n in w.nas:
        try:
            systems.append(n.status())
        except (UpstreamError, Refused, BridgeConfigError) as e:
            systems.append({"kind": type(n).__name__.lower(), "name": n.name, "disks": [], "volumes": [],
                            "backups": [], "warnings": [f"lecture impossible : {e}"]})
    return {"systems": systems, "at": w.clock()}


@_guard
def op_action(w: Watch, p: dict[str, Any]) -> dict[str, Any]:
    action, target = str(p.get("action") or ""), str(p.get("target") or "")
    if action not in ACTIONS or not target:
        raise _err(400, "action nommée et cible déclarée attendues")
    if action in ("pause_device", "resume_device"):
        if w.unifi is None:
            raise _err(404, "UniFi non configuré")
        return {"action": action, "target": target, **w.unifi.set_pause(target, action == "pause_device")}
    for n in w.nas:
        if target in n.backups:
            return {"action": action, "target": target, "nas": n.name, **n.run_backup(target)}
    raise _err(404, "sauvegarde non déclarée")


WATCH_OPS = {
    "watch.targets": op_targets,
    "cameras.reviews": op_reviews,
    "network.clients": op_clients,
    "nas.status": op_nas,
    "watch.action": op_action,
}


# ─────────────────────────────── médias (écoute du foyer) ───────────────────────────────

def serve_media(bridge: Any, h: Any, path: str, query: dict[str, list[str]]) -> bool:
    """Route média de l'écoute du foyer ; True si la requête a reçu sa réponse."""
    if not path.startswith("/media/v1/"):
        return False
    from nokker.homebridge.server import _json

    w = getattr(bridge, "watch", None)
    if w is None or not w.media_ok(path, query):
        _json(h, 403, {"error": "lien expiré ou invalide"})
        return True
    m_thumb, m_live = _RE_THUMB.match(path), _RE_LIVE.match(path)
    f = w.frigate
    if f is None or not (m_thumb or m_live):
        _json(h, 404, {"error": "média inconnu"})
        return True
    try:
        url = f.thumb_url(m_thumb.group(1)) if m_thumb else f.live_url(m_live.group(1))
        with f.http.stream(url, headers=f.media_headers()) as r:
            if r.status_code != 200:
                _json(h, 502, {"error": f"caméra : {r.status_code}"})
                return True
            h.send_response(200)
            h.send_header("Content-Type", r.headers.get("content-type", "application/octet-stream"))
            h.send_header("Cache-Control", "no-store")
            h.send_header("X-Content-Type-Options", "nosniff")
            h.end_headers()
            sent, deadline = 0, time.monotonic() + w.live_max_s
            for chunk in r.iter_bytes():
                h.wfile.write(chunk)
                sent += len(chunk)
                if (m_thumb and sent > MAX_MEDIA_BYTES) or time.monotonic() > deadline:
                    break
        logger.info("média %s : ok", "vignette" if m_thumb else "direct")
    except (UpstreamError, Refused) as e:
        _json(h, 502, {"error": str(e)})
    except (BrokenPipeError, ConnectionResetError):
        pass
    return True


__all__ = ["ACTIONS", "MEDIA_TTL_MAX_S", "WATCH_OPS", "Watch", "media_key", "media_sig", "serve_media"]
