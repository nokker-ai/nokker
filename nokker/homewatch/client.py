"""Client noyau → passerelle `home-bridge` (API interne authentifiée, `POST /v1/op/<nom>`, docs/HOME-WATCH.md §5).

Le noyau n'appelle que les opérations nommées de la veille ; il n'a AUCUNE méthode qui lise une image. Pour montrer
une vignette ou le direct sur un écran local, il SIGNE une URL de la route média de la passerelle (≤ 5 min), que
l'écran charge lui-même.
"""
from __future__ import annotations

import hashlib
import hmac
import threading
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

MEDIA_KEY_LABEL = b"nokker-home-media-v1"  # même étiquette que nokker/homebridge/server.py
MEDIA_TTL_S = 120
MAX_BODY = 4 * 1024 * 1024


class BridgeError(RuntimeError):
    def __init__(self, message: str, code: str = "bridge") -> None:
        super().__init__(message)
        self.code = code


@dataclass
class BridgeClient:
    base_url: str
    token: str
    media_base_url: str = ""
    timeout_s: float = 15.0
    transport: Any = None  # tests : httpx.MockTransport ou None (réseau réel)
    clock: Any = time.time
    _client: httpx.Client | None = field(default=None, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        parts = urlsplit(self.base_url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise BridgeError("home_watch.bridge_url : URL http(s) de la passerelle attendue", "bad_url")
        if len(self.token) < 16:  # même seuil que la passerelle (nokker/homebridge/config.py)
            raise BridgeError("jeton de passerelle absent ou trop court (NOKKER_HOME_BRIDGE_TOKEN)", "no_token")
        self.base_url = self.base_url.rstrip("/")

    def _http(self) -> httpx.Client:
        with self._lock:
            if self._client is None:
                self._client = httpx.Client(timeout=self.timeout_s, follow_redirects=False, transport=self.transport,
                                            trust_env=False)
            return self._client

    def _call(self, method: str, path: str, *, params: Any = None, body: Any = None) -> Any:
        try:
            r = self._http().request(method, f"{self.base_url}{path}", params=params, json=body,
                                     headers={"Authorization": f"Bearer {self.token}"})
        except httpx.HTTPError as e:
            raise BridgeError(f"passerelle injoignable ({type(e).__name__})", "unreachable") from None
        if len(r.content) > MAX_BODY:
            raise BridgeError("réponse de la passerelle trop grande", "too_large")
        try:
            out = r.json()
        except ValueError:
            raise BridgeError(f"réponse illisible de la passerelle ({r.status_code})", "bad_json") from None
        if r.status_code >= 400:
            err = out.get("error") if isinstance(out, dict) else None
            raise BridgeError(str(err or f"passerelle : {r.status_code}")[:200], str(r.status_code))
        return out

    def op(self, name: str, payload: dict[str, Any] | None = None) -> Any:
        """Opération nommée de la passerelle : `POST /v1/op/<nom>` (table OPS de nokker/homebridge/server.py)."""
        return self._call("POST", f"/v1/op/{name}", body=payload or {})

    # ── opérations nommées ──
    def health(self) -> dict[str, Any]:
        return self._call("GET", "/v1/health")

    def targets(self) -> dict[str, Any]:
        return self.op("watch.targets")

    def reviews(self, after: float, limit: int = 100) -> list[dict[str, Any]]:
        return list((self.op("cameras.reviews", {"after": after, "limit": limit}) or {}).get("items") or [])

    def clients(self) -> list[dict[str, Any]]:
        return list((self.op("network.clients") or {}).get("clients") or [])

    def nas(self) -> list[dict[str, Any]]:
        return list((self.op("nas.status") or {}).get("systems") or [])

    def action(self, action: str, target: str) -> dict[str, Any]:
        return self.op("watch.action", {"action": action, "target": target})

    # ── URL média signée (le noyau ne lit jamais l'image) ──
    def media_url(self, kind: str, ident: str, ttl_s: int = MEDIA_TTL_S) -> tuple[str, int]:
        if not self.media_base_url:
            raise BridgeError("home_watch.media_base_url absent : aucun écran local ne peut charger le média",
                              "no_media")
        if kind not in ("thumb", "live"):
            raise BridgeError("média inconnu", "bad_media")
        path = f"/media/v1/{kind}/{quote(ident, safe='')}"
        exp = int(self.clock()) + max(10, min(int(ttl_s), 300))
        key = hmac.new(self.token.encode("utf-8"), MEDIA_KEY_LABEL, hashlib.sha256).digest()
        sig = hmac.new(key, f"{path}\n{exp}".encode(), hashlib.sha256).hexdigest()
        return f"{self.media_base_url.rstrip('/')}{path}?exp={exp}&sig={sig}", exp


__all__ = ["MEDIA_TTL_S", "BridgeClient", "BridgeError"]
