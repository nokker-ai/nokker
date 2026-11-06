"""Banc du BYOK cloud : un faux résolveur DNS (noms → IP), un faux point d'accès compatible OpenAI en mémoire
(httpx.MockTransport : /models et /chat/completions, qui note la clé reçue) et le service sur une base et un coffre en
mémoire."""
from __future__ import annotations

import json
import socket
from dataclasses import dataclass, field
from typing import Any

import httpx

from nokker.byok.service import ByokSecrets, CloudByok
from nokker.byok.store import MemoryByokStore

PUBLIC = "93.184.216.34"
DNS: dict[str, list[str]] = {
    "api.example.com": [PUBLIC],
    "other.example.com": ["93.184.216.35"],
    "intranet.example.com": ["10.0.0.5"],
    "metadata.example.com": ["169.254.169.254"],
    "loop.example.com": ["127.0.0.1"],
    "ula.example.com": ["fd00::1"],
    "tailnet.example.com": ["100.89.156.12"],
    "mixed.example.com": [PUBLIC, "192.168.1.10"],
}


def fake_resolver(dns: dict[str, list[str]] | None = None):
    table = DNS if dns is None else dns

    def resolve(host, port, proto=None, **_kw):
        ips = table.get(str(host).rstrip(".").lower())
        if ips is None:
            try:  # IP littérale : rendue telle quelle (comme getaddrinfo)
                import ipaddress

                ipaddress.ip_address(host)
                ips = [host]
            except ValueError:
                raise socket.gaierror(socket.EAI_NONAME, "Name or service not known") from None
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ips]

    return resolve


@dataclass
class FakeEndpoint:
    """Point d'accès compatible OpenAI. `keys` : clé attendue par hôte (None = sans auth) ; `down` : hôtes en panne
    (401 qui recopie l'en-tête Authorization, comme un amont mal élevé)."""

    keys: dict[str, str | None] = field(default_factory=dict)
    down: set[str] = field(default_factory=set)
    models: list[str] = field(default_factory=lambda: ["m-small", "m-large"])
    seen: list[dict[str, Any]] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        auth = request.headers.get("authorization", "")
        self.seen.append({"host": host, "path": request.url.path, "auth": auth,
                          "session": request.headers.get("x-session-id"),
                          "body": json.loads(request.content) if request.content else None})
        if host in self.down:
            return httpx.Response(401, text=f"invalid credentials: {auth}")
        want = self.keys.get(host)
        if want is not None and auth != f"Bearer {want}":
            return httpx.Response(401, json={"error": "bad key"})
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": m} for m in self.models]})
        if request.url.path.endswith("/chat/completions"):
            body = json.loads(request.content)
            return httpx.Response(200, json={
                "model": body["model"],
                "choices": [{"message": {"role": "assistant", "content": f"réponse de {host} ({body['model']})"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3}})
        return httpx.Response(404)

    def transport(self, base_url: str) -> httpx.BaseTransport:
        return httpx.MockTransport(self.handler)


def make_service(entitled: set[str], endpoint: FakeEndpoint | None = None, **kw) -> tuple[CloudByok, FakeEndpoint]:
    ep = endpoint or FakeEndpoint()
    svc = CloudByok(store=MemoryByokStore(), secrets=ByokSecrets(), entitled=lambda tid: tid in entitled,
                    resolver=fake_resolver(), transport_factory=ep.transport, **kw)
    return svc, ep
