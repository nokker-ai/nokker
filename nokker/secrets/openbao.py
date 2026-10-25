"""Fournisseur `openbao` : KV v2 d'un OpenBao (ou d'un Vault compatible), choisi explicitement.

Disposition (contrat compose, docs/LOKKAL.md §7.10) :
  - montage KV v2 `nokker/` ; un secret = `nokker/data/<prefix>/<portée>/<NOM>` → `{"value": …}` ;
    `prefix` vide par défaut (une installation = un OpenBao) ; la date vient des métadonnées (`updated_time`) ;
  - la policy du noyau (`nokker-core`) se limite à `nokker/data/*` et `nokker/metadata/*` : pas de transit (le
    chiffrement au repos est celui d'OpenBao lui-même ; une enveloppe transit demanderait une policy de plus et une
    clé à gérer, pour rien de plus côté Lokkal) ;
  - `rm` efface toutes les versions (`DELETE nokker/metadata/…`), pas seulement la dernière.

Authentification : **AppRole depuis des fichiers** (`role_id_file`, `secret_id_file`, défaut lus dans
`NOKKER_OPENBAO_ROLE_ID_FILE` / `NOKKER_OPENBAO_SECRET_ID_FILE`), ou un jeton depuis un fichier (`token_file`,
dev). Jamais une valeur en variable d'environnement : elle fuirait dans les sous-processus (bacs à sable, MCP).
Le jeton AppRole expire : relogin à 2/3 de sa durée, et sur un 403 on refait le login UNE fois puis on rend
`SecretsError(code="denied")`.

Coffre scellé : le noyau ne descelle jamais et ne voit jamais une part de descellement (service à côté). Un 503 ou
une erreur « sealed » donne `SecretsError(code="sealed")` (« coffre scellé ») ; les routes rendent 503, les
références `secret:NOM` échouent avec ce message. Jamais de repli sur `file`.

Transport : `https` (CA propre par `ca_file`), ou `http` seulement vers la boucle locale ou un nom d'hôte sans point
(service du réseau docker du compose, `http://openbao:8200`) ; ailleurs, `allow_http` explicite.
"""
from __future__ import annotations

import ipaddress
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from nokker.secrets.base import SecretInfo, SecretsError, check_name, check_value

ENV_ADDR = "NOKKER_OPENBAO_ADDR"
ENV_ROLE_ID_FILE = "NOKKER_OPENBAO_ROLE_ID_FILE"
ENV_SECRET_ID_FILE = "NOKKER_OPENBAO_SECRET_ID_FILE"
ENV_TOKEN_FILE = "NOKKER_OPENBAO_TOKEN_FILE"
ENV_ALLOW_HTTP = "NOKKER_OPENBAO_ALLOW_HTTP"
_FRACTION_RE = re.compile(r"(\.\d{6})\d+")


def _parse_ts(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:  # OpenBao rend des nanosecondes : fromisoformat n'en prend que six chiffres
        return datetime.fromisoformat(_FRACTION_RE.sub(r"\1", raw))
    except ValueError:
        return None


def _read_secret_file(path: str | None, what: str) -> str:
    """Contenu d'un fichier d'identifiant. Absent = coffre non configuré (le compose monte toujours le dossier, vide
    sans le profil `vault`) : une erreur claire, jamais un crash."""
    if not path:
        raise SecretsError(f"coffre OpenBao non configuré : fichier de {what} non indiqué", "config")
    p = Path(path).expanduser()
    if not p.exists():
        raise SecretsError(f"coffre OpenBao non configuré : {p} absent (profil « vault » non activé ?)", "config")
    try:
        value = p.read_text(encoding="utf-8").strip()
    except OSError:
        raise SecretsError(f"OpenBao : fichier de {what} illisible ({path})", "config") from None
    if not value:
        raise SecretsError(f"OpenBao : fichier de {what} vide ({path})", "config")
    return value


def http_allowed(address: str) -> bool:
    """`http://` admis : boucle locale, ou nom d'hôte sans point (service docker du compose)."""
    host = urlsplit(address).hostname or ""
    if host == "localhost" or "." not in host and ":" not in host and host:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass
class OpenBaoSession:
    """Connexion authentifiée à OpenBao, partagée par les deux portées (un login, un jeton)."""

    address: str
    auth: str = "approle"  # approle | token
    role_id_file: str | None = None
    secret_id_file: str | None = None
    token_file: str | None = None
    approle_mount: str = "approle"
    namespace: str | None = None
    ca_file: str | None = None
    allow_http: bool = False
    timeout_s: float = 5.0
    client: Any = None  # httpx.Client injectable (tests)
    _token: str | None = field(default=None, init=False, repr=False)
    _renew_at: float | None = field(default=None, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        self.address = (self.address or "").rstrip("/")
        scheme = urlsplit(self.address).scheme
        if scheme not in ("http", "https"):
            raise SecretsError("OpenBao : adresse http(s) requise (local.secrets.openbao.address ou "
                               f"{ENV_ADDR})", "config")
        if scheme == "http" and not (self.allow_http or http_allowed(self.address)):
            raise SecretsError("OpenBao : http refusé hors boucle locale et réseau du compose (https, ou "
                               "allow_http explicite)", "config")
        if self.auth not in ("approle", "token"):
            raise SecretsError("OpenBao : auth approle | token", "config")

    def __repr__(self) -> str:
        return f"OpenBaoSession({self.address!r}, auth={self.auth!r})"

    def _http(self) -> Any:
        if self.client is None:
            self.client = httpx.Client(timeout=self.timeout_s, verify=self.ca_file or True)
        return self.client

    def _headers(self, token: str | None) -> dict[str, str]:
        h = {"X-Vault-Token": token} if token else {}
        if self.namespace:
            h["X-Vault-Namespace"] = self.namespace
        return h

    def _raw(self, method: str, path: str, token: str | None, body: dict[str, Any] | None = None,
             params: dict[str, str] | None = None) -> Any:
        try:
            return self._http().request(method, f"{self.address}/v1/{path}", json=body, params=params,
                                        headers=self._headers(token))
        except httpx.HTTPError:
            raise SecretsError(f"OpenBao injoignable ({self.address})") from None

    @staticmethod
    def _errors(r: Any) -> str:
        try:
            return " ".join(str(e) for e in (r.json().get("errors") or []))
        except (ValueError, AttributeError):
            return ""

    def _raise_for(self, r: Any, what: str) -> None:
        errors = self._errors(r).lower()
        if r.status_code == 503 or "sealed" in errors:
            raise SecretsError("coffre scellé : OpenBao doit être descellé (hors du noyau)", "sealed")
        if r.status_code in (401, 403):
            raise SecretsError(f"OpenBao : droits refusés ({what})", "denied")
        raise SecretsError(f"OpenBao : {what} a échoué (HTTP {r.status_code})")

    def login(self) -> str:
        if self.auth == "token":
            tok = _read_secret_file(self.token_file, "jeton")
            self._token, self._renew_at = tok, None
            return tok
        body = {"role_id": _read_secret_file(self.role_id_file, "role_id"),
                "secret_id": _read_secret_file(self.secret_id_file, "secret_id")}
        r = self._raw("POST", f"auth/{self.approle_mount}/login", None, body)
        if r.status_code != 200:
            self._raise_for(r, "login AppRole")
        auth = (r.json() or {}).get("auth") or {}
        tok, ttl = auth.get("client_token"), int(auth.get("lease_duration") or 0)
        if not tok:
            raise SecretsError("OpenBao : login AppRole sans jeton", "denied")
        self._token = tok
        self._renew_at = time.monotonic() + ttl * 2 / 3 if ttl > 0 else None
        return tok

    def token(self, fresh: bool = False) -> str:
        with self._lock:
            if fresh or self._token is None or (self._renew_at is not None and time.monotonic() >= self._renew_at):
                return self.login()
            return self._token

    def call(self, method: str, path: str, body: dict[str, Any] | None = None,
             params: dict[str, str] | None = None, what: str = "requête") -> Any | None:
        """Réponse JSON (ou {} sans corps) ; None sur 404. 403 → relogin une fois, puis `denied`."""
        r = self._raw(method, path, self.token(), body, params)
        if r.status_code == 403 and "sealed" not in self._errors(r).lower():
            r = self._raw(method, path, self.token(fresh=True), body, params)
        if r.status_code == 404:
            return None
        if r.status_code >= 300:
            self._raise_for(r, what)
        if r.status_code == 204 or not r.content:
            return {}
        return r.json()

    def health(self) -> dict[str, Any]:
        """`/v1/sys/health` sans jeton : {reachable, initialized, sealed}. Jamais d'exception."""
        try:
            r = self._http().get(f"{self.address}/v1/sys/health", headers=self._headers(None),
                                 params={"standbyok": "true", "perfstandbyok": "true"})
            j = r.json()
            return {"reachable": True, "initialized": bool(j.get("initialized")), "sealed": bool(j.get("sealed"))}
        except (httpx.HTTPError, ValueError, AttributeError):
            return {"reachable": False, "initialized": None, "sealed": None}


@dataclass
class OpenBaoSecrets:
    session: OpenBaoSession
    scope: str = "user"
    mount: str = "nokker"
    prefix: str = ""
    kind: str = field(default="openbao", init=False)

    def __repr__(self) -> str:
        return f"OpenBaoSecrets({self.session.address!r}, mount={self.mount!r}, scope={self.scope!r})"

    def _base(self) -> str:
        parts = [p for p in self.prefix.strip("/").split("/") if p] + [self.scope]
        return "/".join(quote(p, safe="") for p in parts)

    def _path(self, kind: str, name: str) -> str:
        return f"{self.mount}/{kind}/{self._base()}/{quote(name, safe='')}"

    def get(self, name: str) -> str | None:
        check_name(name, self.scope)
        j = self.session.call("GET", self._path("data", name), what="lecture")
        if not j:
            return None
        data = (j.get("data") or {}).get("data") or {}
        value = data.get("value")
        return value if isinstance(value, str) else None

    def set(self, name: str, value: str) -> SecretInfo:
        check_name(name, self.scope)
        check_value(value)
        j = self.session.call("POST", self._path("data", name), {"data": {"value": value}}, what="écriture") or {}
        return SecretInfo(name, _parse_ts((j.get("data") or {}).get("created_time")))

    def delete(self, name: str) -> bool:
        check_name(name, self.scope)
        if self.session.call("GET", self._path("metadata", name), what="lecture") is None:
            return False
        self.session.call("DELETE", self._path("metadata", name), what="effacement")
        return True

    def _names(self) -> list[str]:
        j = self.session.call("GET", f"{self.mount}/metadata/{self._base()}", params={"list": "true"}, what="liste")
        keys = ((j or {}).get("data") or {}).get("keys") or []
        return sorted(k for k in keys if isinstance(k, str) and not k.endswith("/"))

    def list(self) -> list[SecretInfo]:
        out = []
        for name in self._names():
            meta = self.session.call("GET", self._path("metadata", name), what="lecture") or {}
            out.append(SecretInfo(name, _parse_ts((meta.get("data") or {}).get("updated_time"))))
        return out

    def values(self) -> list[tuple[str, str]]:
        """(nom, valeur) de la portée : pour le caviardage seulement (nokker/secrets/vault.py)."""
        return [(n, v) for n in self._names() if (v := self.get(n)) is not None]


def session_from_config(cfg: dict[str, Any]) -> OpenBaoSession:
    """Session depuis `local.secrets.openbao` ; l'environnement du compose prime (adresse, fichiers AppRole)."""
    env = os.environ
    address = env.get(ENV_ADDR) or cfg.get("address") or ""
    token_file = env.get(ENV_TOKEN_FILE) or cfg.get("token_file")
    role_file = env.get(ENV_ROLE_ID_FILE) or cfg.get("role_id_file")
    sid_file = env.get(ENV_SECRET_ID_FILE) or cfg.get("secret_id_file")
    auth = cfg.get("auth") or ("token" if token_file and not role_file else "approle")
    return OpenBaoSession(address=address, auth=auth, role_id_file=role_file, secret_id_file=sid_file,
                          token_file=token_file, approle_mount=cfg.get("approle_mount") or "approle",
                          namespace=cfg.get("namespace"), ca_file=cfg.get("ca_file"),
                          allow_http=bool(cfg.get("allow_http")) or env.get(ENV_ALLOW_HTTP) == "1",
                          timeout_s=float(cfg.get("timeout_s", 5.0)))


__all__ = ["ENV_ADDR", "ENV_ROLE_ID_FILE", "ENV_SECRET_ID_FILE", "ENV_TOKEN_FILE", "OpenBaoSecrets",
           "OpenBaoSession", "http_allowed", "session_from_config"]
