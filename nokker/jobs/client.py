"""Client du service emploi (API partenaire, docs/JOBS-PACK.md §13).

Un client de SERVICE, pas un compte utilisateur : jeton machine (`Authorization: Bearer <JWT ES256>`, signé À CHAQUE
requête par `Es256Signer` : `iss` = client, `aud` = audience du service, `iat`, `exp` ≤ 5 min, `jti` unique ; le service
n'accepte rien d'autre) et compte partenaire visé par l'en-tête `X-Partner-Account`. Préfixe `/api/v1/partner`. Erreurs `{"error": {"code", "message"}}`. Chaque réponse JSON porte
`usage` (jetons consommés par la requête) ; une réponse binaire (PDF) le porte dans l'en-tête `X-Partner-Usage` (JSON).
Les opérations longues rendent 202 `{job_id}` puis se suivent par `GET /partner/jobs/{id}`.

Le transport est injectable (`transport(method, url, headers, body) -> (status, headers, bytes)`) : les tests passent
un faux serveur, jamais le réseau. Le transport par défaut est httpx, sans redirection, délai borné.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("nokker.jobs.client")

PREFIX = "/api/v1/partner"
Transport = Callable[[str, str, dict[str, str], bytes | None], tuple[int, dict[str, str], bytes]]


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    calls: tuple[dict[str, Any], ...] = ()

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    @classmethod
    def parse(cls, raw: Any) -> Usage:
        if not isinstance(raw, dict):
            return cls()
        try:
            return cls(max(0, int(raw.get("input_tokens") or 0)), max(0, int(raw.get("output_tokens") or 0)),
                       tuple(c for c in raw.get("calls") or () if isinstance(c, dict)))
        except (TypeError, ValueError):
            return cls()

    def __add__(self, other: Usage) -> Usage:
        return Usage(self.input_tokens + other.input_tokens, self.output_tokens + other.output_tokens,
                     self.calls + other.calls)


class PartnerError(Exception):
    """Refus ou panne du service emploi. `code` : celui du contrat (`not_found`, `rate`…), ou `unavailable` (réseau,
    5xx, réponse illisible, délai)."""

    def __init__(self, code: str, message: str = "", status: int = 0, usage: Usage | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.status, self.usage = code, message, status, usage or Usage()


@dataclass
class Reply:
    data: Any
    usage: Usage = field(default_factory=Usage)
    content: bytes = b""


def httpx_transport(timeout_s: float = 30.0, proxy: str | None = None) -> Transport:
    """`proxy` : proxy HTTP explicite (ex. `http://egress-proxy:1055`, nœud Tailscale de la bêta) ; il ignore alors les
    variables d'environnement (`HTTP_PROXY`, `NO_PROXY`…), qui ne peuvent ni le contourner ni en ajouter un autre.
    Sans `proxy` : comportement inchangé."""
    extra: dict[str, Any] = {"proxy": proxy, "trust_env": False} if proxy else {}

    def send(method: str, url: str, headers: dict[str, str], body: bytes | None) -> tuple[int, dict[str, str], bytes]:
        import httpx

        r = httpx.request(method, url, headers=headers, content=body, timeout=timeout_s, follow_redirects=False,
                          **extra)
        return r.status_code, {k.lower(): v for k, v in r.headers.items()}, r.content

    return send


def _b64url(data: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class Es256Signer:
    """Jeton machine : JWT ES256 neuf à chaque appel (`jti` aléatoire, durée `ttl_s` ≤ 300 s). La clé privée (PEM,
    courbe P-256) est lue par `private_key()` à chaque signature (coffre, `secret:NOM`) ; elle n'est jamais gardée en
    attribut ni journalisée."""

    MAX_TTL_S = 300

    def __init__(self, private_key: Callable[[], str], issuer: str, audience: str, ttl_s: int = 120,
                 clock: Callable[[], float] = time.time) -> None:
        if not issuer or not audience:
            raise ValueError("émetteur et audience requis")
        self._key, self.issuer, self.audience = private_key, issuer, audience
        self.ttl_s, self._clock = max(1, min(int(ttl_s), self.MAX_TTL_S)), clock

    def __call__(self) -> str:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

        pem = self._key()
        if not pem:
            return ""
        key = serialization.load_pem_private_key(pem.encode("utf-8"), password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
            raise PartnerError("unavailable", "clé machine : P-256 attendue")
        now = int(self._clock())
        header = {"alg": "ES256", "typ": "JWT"}
        claims = {"iss": self.issuer, "aud": self.audience, "iat": now, "exp": now + self.ttl_s,
                  "jti": uuid.uuid4().hex}
        signing = (_b64url(json.dumps(header, separators=(",", ":")).encode()) + "."
                   + _b64url(json.dumps(claims, separators=(",", ":")).encode()))
        r, s_ = decode_dss_signature(key.sign(signing.encode("ascii"), ec.ECDSA(hashes.SHA256())))
        return signing + "." + _b64url(r.to_bytes(32, "big") + s_.to_bytes(32, "big"))


def multipart(field_name: str, filename: str, mime: str, data: bytes) -> tuple[str, bytes]:
    """Corps `multipart/form-data` d'un seul fichier : (type de contenu, octets)."""
    boundary = "nokker-" + uuid.uuid4().hex
    safe = filename.replace('"', "_").replace("\r", "_").replace("\n", "_")
    head = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field_name}\"; filename=\"{safe}\"\r\n"
            f"Content-Type: {mime or 'application/octet-stream'}\r\n\r\n").encode("utf-8")
    return f"multipart/form-data; boundary={boundary}", head + data + f"\r\n--{boundary}--\r\n".encode("ascii")


class PartnerClient:
    def __init__(self, base_url: str, token: Callable[[], str], transport: Transport | None = None, *,
                 poll_s: float = 2.5, wait_s: float = 300.0, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.base_url = base_url.rstrip("/")
        self._token, self._send = token, transport or httpx_transport()
        self.poll_s, self.wait_s, self._sleep, self._clock = poll_s, wait_s, sleep, clock

    # ── requête ──
    def request(self, method: str, path: str, *, account: str | None = None, body: Any = None,
                query: dict[str, Any] | None = None, binary: bool = False,
                upload: tuple[str, str, bytes] | None = None) -> Reply:
        """`upload` = (nom, type MIME, octets) : envoyé en `multipart/form-data`, champ `file`."""
        from urllib.parse import urlencode

        url = f"{self.base_url}{PREFIX}{path}"
        q = {k: v for k, v in (query or {}).items() if v not in (None, "")}
        if q:
            url += "?" + urlencode(q)
        try:
            token = self._token()
        except PartnerError:
            raise
        except Exception as exc:  # noqa: BLE001 — clé illisible : jamais sa valeur dans un message
            raise PartnerError("unavailable", "jeton machine illisible") from exc
        if not token:
            raise PartnerError("unavailable", "jeton machine absent")
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json", "X-Request-Id": uuid.uuid4().hex}
        if account:
            headers["X-Partner-Account"] = account
        raw = None
        if upload is not None:
            headers["Content-Type"], raw = multipart("file", *upload)
        elif body is not None:
            headers["Content-Type"] = "application/json"
            raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        try:
            status, rh, content = self._send(method, url, headers, raw)
        except Exception as exc:  # noqa: BLE001 — réseau : jamais une trace vers le modèle
            logger.warning("service emploi injoignable (%s %s) : %s", method, path, type(exc).__name__)
            raise PartnerError("unavailable", "injoignable") from exc
        rh = {k.lower(): v for k, v in (rh or {}).items()}
        if binary and 200 <= status < 300:
            try:
                usage = Usage.parse(json.loads(rh.get("x-partner-usage") or "{}"))
            except ValueError:
                usage = Usage()
            return Reply(None, usage, content)
        try:
            data = json.loads(content.decode("utf-8")) if content else {}
        except (UnicodeDecodeError, ValueError) as exc:
            raise PartnerError("unavailable", "réponse illisible", status) from exc
        usage = Usage.parse(data.get("usage") if isinstance(data, dict) else None)
        if status >= 400:
            e = data.get("error") if isinstance(data, dict) else None
            code = str((e or {}).get("code") or ("unavailable" if status >= 500 else "error"))
            raise PartnerError("unavailable" if status >= 500 else code, str((e or {}).get("message") or ""), status,
                               usage)
        return Reply(data, usage)

    def fetch_signed(self, path: str) -> Reply:
        """Fichier d'une URL SIGNÉE rendue par le service (PDF d'une lettre, fichier de préparation) : chemin relatif
        sous le préfixe partenaire seulement, sans jeton (la signature fait foi), usage dans `X-Partner-Usage`."""
        from urllib.parse import urlsplit

        u = urlsplit(path)
        if u.scheme or u.netloc or not u.path.startswith(PREFIX + "/") or "sig=" not in u.query:
            raise PartnerError("unavailable", "adresse signée inattendue")
        try:
            status, rh, content = self._send("GET", self.base_url + path, {"Accept": "*/*"}, None)
        except Exception as exc:  # noqa: BLE001
            raise PartnerError("unavailable", "injoignable") from exc
        rh = {k.lower(): v for k, v in (rh or {}).items()}
        try:
            usage = Usage.parse(json.loads(rh.get("x-partner-usage") or "{}"))
        except ValueError:
            usage = Usage()
        if status >= 400:
            raise PartnerError("forbidden" if status == 403 else "unavailable", "", status, usage)
        return Reply(None, usage, content)

    def wait(self, job_id: str, account: str, usage: Usage | None = None) -> Reply:
        """Suit une opération 202 jusqu'à `done` (rend `result`) ou `error` ; usage cumulé de tous les relevés."""
        total = usage or Usage()
        end = self._clock() + self.wait_s
        while True:
            r = self.request("GET", f"/jobs/{job_id}", account=account)
            total = total + r.usage
            st = str((r.data or {}).get("status") or "")
            if st == "done":
                return Reply((r.data or {}).get("result") or {}, total)
            if st in ("error", "failed"):
                e = (r.data or {}).get("error") or {}
                raise PartnerError(str(e.get("code") or "error"), str(e.get("message") or ""), 200, total)
            if self._clock() >= end:
                raise PartnerError("unavailable", "délai dépassé", 0, total)
            self._sleep(self.poll_s)

    def run(self, method: str, path: str, *, account: str, body: Any = None,
            upload: tuple[str, str, bytes] | None = None) -> Reply:
        """Requête qui peut rendre 202 `{job_id}` : suivie jusqu'au résultat."""
        r = self.request(method, path, account=account, body=body, upload=upload)
        if isinstance(r.data, dict) and r.data.get("job_id") and "result" not in r.data:
            return self.wait(str(r.data["job_id"]), account, r.usage)
        return r

    # ── comptes partenaires (§13.2, §13.3) ──
    def create_account(self, display_name: str, locale: str, residence_country: str) -> Reply:
        return self.request("POST", "/accounts", body={"display_name": display_name, "locale": locale,
                                                        "residence_country": residence_country})

    def delete_account(self, partner_account_id: str) -> bool:
        """Effacement complet chez le service. Déjà effacé (404) : vrai aussi."""
        try:
            self.request("DELETE", f"/accounts/{partner_account_id}")
        except PartnerError as exc:
            if exc.status == 404:
                return True
            raise
        return True


__all__ = ["PREFIX", "Es256Signer", "PartnerClient", "PartnerError", "Reply", "Transport", "Usage", "httpx_transport", "multipart"]
