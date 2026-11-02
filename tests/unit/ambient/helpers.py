"""Faux appareils et faux services de l'ambiant : aucun réseau, aucun modèle, aucune voix réelle."""
from __future__ import annotations

import io
import json
from dataclasses import dataclass, field
from typing import Any

from nokker.ambient.presence import Presence, RoomState

OGG = b"OggS" + b"\x00" * 60


class FakeHandler:
    """Le strict nécessaire d'un BaseHTTPRequestHandler pour les modules http.py."""

    def __init__(self, headers: dict[str, str] | None = None, body: Any = None) -> None:
        raw = b"" if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
        self.headers = {"Content-Length": str(len(raw)), **(headers or {})}
        self.rfile = io.BytesIO(raw)
        self.wfile = io.BytesIO()
        self.status: int | None = None
        self.sent: dict[str, str] = {}

    # BaseHTTPRequestHandler.headers est insensible à la casse : on imite
    def _h(self) -> None:
        pass

    def send_response(self, status: int) -> None:
        self.status = status

    def send_header(self, k: str, v: str) -> None:
        self.sent[k] = v

    def end_headers(self) -> None:
        pass

    def json(self) -> Any:
        return json.loads(self.wfile.getvalue() or b"null")

    def body(self) -> bytes:
        return self.wfile.getvalue()


class CIHeaders(dict):
    def get(self, k: str, default: Any = None) -> Any:  # type: ignore[override]
        for kk, v in self.items():
            if kk.lower() == k.lower():
                return v
        return default


def handler(headers: dict[str, str] | None = None, body: Any = None) -> FakeHandler:
    h = FakeHandler(headers, body)
    h.headers = CIHeaders(h.headers)
    return h


@dataclass
class FakeTTS:
    """Voix factice : garde tout texte reçu (pour prouver qu'un contenu privé n'a jamais été synthétisé)."""

    said: list[str] = field(default_factory=list)
    name: str = "fake"

    def synthesize(self, text: str, lang: str) -> bytes:
        self.said.append(text)
        return OGG + text.encode("utf-8")


def presence(rooms: dict[str, tuple[set[str], bool]] | None = None, guests: bool = False, ok: bool = True) -> Presence:
    """rooms = {pièce: (membres, occupant_inconnu)}."""
    return Presence(rooms={r: RoomState(members=frozenset(m), occupied=bool(m) or u, unknown=u)
                           for r, (m, u) in (rooms or {}).items()}, guests=guests, ok=ok)
