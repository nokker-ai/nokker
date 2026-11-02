"""Frigate 0.17 (API HTTP officielle, https://docs.frigate.video/integrations/api/) — vérifié sur le code de v0.17.0 :

- `GET /api/review?after=<epoch>&before=&cameras=a,b&severity=alert|detection&limit=` → liste d'éléments de revue
  `{id, camera, start_time, end_time, severity, thumb_path, has_been_reviewed, data}` ;
- `data.metadata` (résumé GenAI de l'élément de revue, `frigate/data_processing/post/types.py::ReviewMetadata`) :
  `title`, `scene`, `shortSummary`, `confidence`, `potential_threat_level` (0 normal, 1 suspect, 2 dangereux : c'est
  l'échelle du prompt de Frigate, « Level 0/1/2 » de `review.genai.activity_context_prompt`), `other_concerns` ;
- `GET /api/review/<id>` → un élément ; vignette = `thumb_path` (`/media/frigate/clips/review/thumb-….webp`, servi
  sous `/clips/review/…`) ;
- direct : `GET /api/<caméra>` (MJPEG) ;
- authentification (port 8971) : `POST /api/login {user, password}` → jeton JWT (cookie `frigate_token`), rejoué en
  `Authorization: Bearer`. Port 5000 (interne, sans authentification) : `user` absent.

Ce que la passerelle rend au noyau : du texte seulement. Ni `thumb_path`, ni URL, ni octet d'image.
"""
from __future__ import annotations

import datetime as _dt
import re
import threading
from dataclasses import dataclass, field
from typing import Any

from nokker.homebridge.watch.net import LanHttp, UpstreamError, WatchEnv

LEVELS = {0: "normal", 1: "suspect", 2: "dangereux"}
_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_CAMERA = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_THUMB = re.compile(r"^/media/frigate/(clips/review/[A-Za-z0-9._-]{1,200}\.(?:webp|jpg))$")
TEXT_MAX = {"title": 160, "summary": 400, "scene": 1200, "concern": 200}
MAX_ITEMS = 200


def _ts(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        d = _dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.UTC)
    return d.timestamp()


def _text(v: Any, limit: int) -> str:
    s = "" if v is None else str(v)
    s = "".join(c if c.isprintable() or c == " " else " " for c in s)
    return " ".join(s.split())[:limit]


def _names(v: Any, limit: int = 12) -> list[str]:
    out: list[str] = []
    for x in v or []:
        name = x[0] if isinstance(x, (list, tuple)) and x else x
        t = _text(name, 40)
        if t and t not in out:
            out.append(t)
    return out[:limit]


def normalize_review(item: dict[str, Any]) -> dict[str, Any] | None:
    """Élément de revue Frigate → texte normalisé (aucune référence d'image)."""
    rid = str(item.get("id") or "")
    camera = str(item.get("camera") or "")
    if not _ID.match(rid) or not _CAMERA.match(camera):
        return None
    data = item.get("data") or {}
    if isinstance(data, str):
        import json

        try:
            data = json.loads(data)
        except ValueError:
            data = {}
    meta = data.get("metadata") if isinstance(data, dict) else None
    meta = meta if isinstance(meta, dict) else None
    level = None
    if meta is not None:
        try:
            level = int(meta.get("potential_threat_level", 0))
        except (TypeError, ValueError):
            level = None
    return {
        "id": rid, "camera": camera,
        "start": _ts(item.get("start_time")), "end": _ts(item.get("end_time")),
        "severity": "alert" if item.get("severity") == "alert" else "detection",
        "niveau": LEVELS.get(level, "inconnu") if meta is not None else "inconnu",
        "threat_level": level,
        "summarized": meta is not None,
        "title": _text((meta or {}).get("title"), TEXT_MAX["title"]),
        "summary": _text((meta or {}).get("shortSummary"), TEXT_MAX["summary"]),
        "scene": _text((meta or {}).get("scene"), TEXT_MAX["scene"]),
        "concerns": [_text(c, TEXT_MAX["concern"]) for c in ((meta or {}).get("other_concerns") or [])][:5],
        "confidence": (meta or {}).get("confidence") if isinstance((meta or {}).get("confidence"), (int, float))
        else None,
        "objects": _names((data or {}).get("objects") if isinstance(data, dict) else None),
        "zones": _names((data or {}).get("zones") if isinstance(data, dict) else None),
        "has_thumb": bool(_THUMB.match(str(item.get("thumb_path") or ""))),
    }


@dataclass
class Frigate:
    config: WatchEnv
    url: str
    user: str | None = None
    password_ref: str | None = None
    cameras: tuple[str, ...] = ()
    live_cameras: tuple[str, ...] = ()
    http: LanHttp | None = None
    _token: str | None = field(default=None, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    @classmethod
    def from_config(cls, config: WatchEnv, raw: dict[str, Any]) -> Frigate:
        url = str(raw.get("url") or "").rstrip("/")
        if not url:
            raise ValueError("watch.frigate.url obligatoire")
        config.check_url(url)
        cams = tuple(str(c) for c in raw.get("cameras") or [] if _CAMERA.match(str(c)))
        live = tuple(str(c) for c in raw.get("live") or [] if _CAMERA.match(str(c)))
        verify = raw.get("ca_file") or bool(raw.get("verify_tls", True))
        return cls(config=config, url=url, user=raw.get("user"), password_ref=raw.get("password"), cameras=cams,
                   live_cameras=live, http=LanHttp(config, verify=verify))

    # ── authentification ──
    def _headers(self) -> dict[str, str]:
        if not self.user:
            return {}
        with self._lock:
            if self._token is None:
                password = self.config.secret(str(self.password_ref or ""))
                r = self.http.request("POST", f"{self.url}/api/login",
                                      json_body={"user": self.user, "password": password})
                if r.status_code != 200:
                    raise UpstreamError(f"connexion à Frigate refusée ({r.status_code})", "auth")
                token = r.cookies.get("frigate_token") or ""
                if not token:
                    try:
                        token = str((r.json() or {}).get("access_token") or "")
                    except ValueError:
                        token = ""
                if not token:
                    raise UpstreamError("Frigate n'a pas rendu de jeton", "auth")
                self._token = token
            return {"Authorization": f"Bearer {self._token}"}

    def _get(self, path: str, params: Any = None) -> Any:
        for attempt in (0, 1):
            try:
                return self.http.json("GET", f"{self.url}{path}", headers=self._headers(), params=params)
            except UpstreamError as e:
                if e.code == "auth" and self.user and attempt == 0:
                    self._token = None
                    continue
                raise
        raise UpstreamError("Frigate : accès refusé", "auth")

    def camera_allowed(self, camera: str) -> bool:
        return bool(_CAMERA.match(camera)) and (not self.cameras or camera in self.cameras)

    # ── opérations nommées ──
    def reviews(self, after: float, limit: int = 100) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"after": float(after), "limit": max(1, min(int(limit), MAX_ITEMS))}
        if self.cameras:
            params["cameras"] = ",".join(self.cameras)
        raw = self._get("/api/review", params)
        out = []
        for item in raw if isinstance(raw, list) else []:
            if isinstance(item, dict):
                n = normalize_review(item)
                if n is not None and self.camera_allowed(n["camera"]):
                    out.append(n)
        return out

    def thumb_url(self, review_id: str) -> str:
        """URL interne de la vignette (utilisée par la route média de la passerelle seulement)."""
        if not _ID.match(review_id):
            raise UpstreamError("identifiant de revue invalide", "bad_id")
        item = self._get(f"/api/review/{review_id}")
        if not isinstance(item, dict) or not self.camera_allowed(str(item.get("camera") or "")):
            raise UpstreamError("revue inconnue", "not_found")
        m = _THUMB.match(str(item.get("thumb_path") or ""))
        if not m:
            raise UpstreamError("vignette absente", "not_found")
        return f"{self.url}/{m.group(1)}"

    def live_url(self, camera: str) -> str:
        if camera not in self.live_cameras or not self.camera_allowed(camera):
            raise UpstreamError("direct non permis pour cette caméra (liste `live` de home.yaml)", "forbidden")
        return f"{self.url}/api/{camera}?fps=5&height=480"

    def media_headers(self) -> dict[str, str]:
        return self._headers()


__all__ = ["LEVELS", "Frigate", "normalize_review"]
