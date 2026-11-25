"""Accès en LECTURE à l'image privée `sites-chrome` (docs/CONNECTED-SITES.md §3.1.1, option B du 06.11).

Le noyau de nokker.ai émet, pour chaque compte qui a une machine Lokkal liée, UN robot Harbor de lecture
(`lokkal-<compte>`, projet `lokkal-private` seul, 30 jours). Il parle à l'API de Harbor avec l'utilisateur
`lokkal-issuer`, administrateur de ce seul projet ; ses identifiants viennent de l'environnement du pod, jamais de la
configuration ni du code :

    NOKKER_HARBOR_ISSUER_URL       https://<registre> (l'API est sous /api/v2.0 ; le registre rendu = son hôte)
    NOKKER_HARBOR_ISSUER_USER      lokkal-issuer
    NOKKER_HARBOR_ISSUER_PASSWORD  mot de passe de lokkal-issuer

Sans l'une d'elles, l'émission est éteinte (`POST /v1/lokkal/sites-registry` → 503 `sites_registry_unavailable`).

- **Émission** : le robot du compte est cherché par son nom ; absent → créé ; présent → MÊME robot, secret régénéré
  et échéance repoussée à 30 jours (jamais un second robot). Six émissions par 24 h glissantes et par compte (429
  au-delà ; en mémoire du processus, un seul réplica, comme les autres débits de nokker/cloud/http.py).
- **Révocation** : le robot est supprimé à la déliaison de la dernière machine Lokkal du compte, à l'effacement du
  compte (nokker/admin/erase.py) et par la console de l'opérateur.
- **Journal** : compte, machine, id du robot, action et échéance ; JAMAIS le secret (ni ici, ni dans une exception :
  les erreurs de Harbor ne remontent que leur statut).
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

logger = logging.getLogger("nokker.cloud.sites_registry")

ENV_URL = "NOKKER_HARBOR_ISSUER_URL"
ENV_USER = "NOKKER_HARBOR_ISSUER_USER"
ENV_PASSWORD = "NOKKER_HARBOR_ISSUER_PASSWORD"
PROJECT = "lokkal-private"
ROBOT_PREFIX = "lokkal-"
DURATION_DAYS = 30
QUOTA_PER_DAY = 6
QUOTA_WINDOW_S = 86400.0
# Comptes de nokker.ai : `us_` + 12 hexadécimaux (nokker/tenancy/store.py) ; le nom du robot doit rester un nom Harbor
# valide (minuscules, chiffres, séparateurs . _ -).
_RE_ACCOUNT = re.compile(r"^us_[a-z0-9]{1,64}$")


class SitesRegistryError(Exception):
    """Harbor a refusé ou n'a pas répondu. Le message ne porte que l'étape et le statut, jamais un corps de Harbor."""


class QuotaExceeded(Exception):
    def __init__(self, retry_after_s: int) -> None:
        super().__init__("quota d'émission atteint")
        self.retry_after_s = retry_after_s


def robot_name(account: str) -> str:
    if not _RE_ACCOUNT.match(account or ""):
        raise ValueError("compte illisible pour un nom de robot")
    return ROBOT_PREFIX + account


@dataclass
class HarborIssuer:
    """Client de l'API Harbor v2.0, avec l'utilisateur `lokkal-issuer` (authentification basique)."""

    url: str
    user: str
    password: str = field(repr=False)
    project: str = PROJECT
    duration_days: int = DURATION_DAYS
    client: Any = None  # httpx.Client injectable (tests : httpx.MockTransport)
    clock: Any = time.time
    _project_id: int | None = field(default=None, init=False, repr=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, **kw: Any) -> HarborIssuer | None:
        env = os.environ if env is None else env
        url, user, pw = (str(env.get(k) or "").strip() for k in (ENV_URL, ENV_USER, ENV_PASSWORD))
        if not (url and user and pw):
            return None
        if "://" not in url:
            url = "https://" + url
        return cls(url=url.rstrip("/"), user=user, password=pw, **kw)

    @property
    def registry(self) -> str:
        """Hôte du registre, tel que `docker login` l'attend (sans schéma)."""
        return urlsplit(self.url).netloc

    def _http(self) -> Any:
        if self.client is None:
            self.client = httpx.Client(timeout=10.0)
        return self.client

    def _call(self, method: str, path: str, step: str, json: Any = None, params: Any = None,
              ok: tuple[int, ...] = (200,)) -> httpx.Response:
        client = self._http()
        # Harbor pose un cookie `sid` sur toute réponse ; renvoyé, il fait passer la requête suivante pour une session
        # navigateur et Harbor exige alors un jeton CSRF : POST /robots → 403 « CSRF token not found » (prod, 06.11.2026).
        # L'API se parle en Basic seule, sans état : aucun cookie n'est jamais renvoyé.
        if hasattr(client, "cookies"):
            client.cookies.clear()
        try:
            r = client.request(method, f"{self.url}/api/v2.0{path}", json=json, params=params,
                                     auth=(self.user, self.password),
                                     headers={"X-Is-Resource-Name": "true", "Accept": "application/json"})
        except httpx.HTTPError as e:
            raise SitesRegistryError(f"harbor {step} : {type(e).__name__}") from None
        if r.status_code not in ok:
            raise SitesRegistryError(f"harbor {step} : HTTP {r.status_code}")
        return r

    def project_id(self) -> int:
        if self._project_id is None:
            body = self._call("GET", f"/projects/{quote(self.project, safe='')}", "projet").json()
            self._project_id = int(body["project_id"])
        return self._project_id

    def find(self, account: str) -> dict[str, Any] | None:
        """Le robot du compte (nom complet `robot$lokkal-private+lokkal-<compte>`), ou None."""
        name = robot_name(account)
        r = self._call("GET", "/robots", "liste", params={
            "q": f"Level=project,ProjectID={self.project_id()},name=~{name}", "page_size": 100})
        found = [x for x in (r.json() or []) if isinstance(x, dict)
                 and str(x.get("name") or "").endswith(f"+{name}")]
        return found[0] if found else None

    def _permissions(self) -> list[dict[str, Any]]:
        return [{"kind": "project", "namespace": self.project,
                 "access": [{"resource": "repository", "action": "pull"}]}]

    def issue(self, account: str) -> dict[str, Any]:
        """Crée ou renouvelle le robot du compte : `{registry, username, password, expires_at, robot_id, renewed}`."""
        name = robot_name(account)
        robot = self.find(account)
        if robot is None:
            r = self._call("POST", "/robots", "création", ok=(201, 409), json={
                "name": name, "level": "project", "duration": self.duration_days, "disable": False,
                "description": f"Lokkal sites-chrome, lecture seule ({account})",
                "permissions": self._permissions()})
            if r.status_code == 201:
                body = r.json()
                return {"registry": self.registry, "username": str(body["name"]), "password": str(body["secret"]),
                        "expires_at": int(body.get("expires_at") or 0), "robot_id": int(body["id"]),
                        "renewed": False}
            robot = self.find(account)  # créé entre-temps (autre réplica) : on le renouvelle
            if robot is None:
                raise SitesRegistryError("harbor création : HTTP 409")
        return self._renew(robot)

    def _renew(self, robot: dict[str, Any]) -> dict[str, Any]:
        rid = int(robot["id"])
        full = self._call("GET", f"/robots/{rid}", "lecture").json()
        # Harbor calcule l'échéance depuis la création du robot (creation_time + duration jours) : la durée est
        # étendue au nombre de jours écoulés + 30, pour que la nouvelle échéance tombe ~30 jours après maintenant.
        created = _epoch(full.get("creation_time"))
        elapsed = max(0, int((self.clock() - created) // 86400)) if created else 0
        update = {k: full[k] for k in ("name", "description", "level", "permissions", "editable") if k in full}
        update.update({"duration": elapsed + self.duration_days, "disable": False,
                       "permissions": self._permissions()})
        self._call("PUT", f"/robots/{rid}", "renouvellement", json=update)
        secret = str((self._call("PATCH", f"/robots/{rid}", "secret", json={"secret": ""}).json() or {})
                     .get("secret") or "")
        if not secret:
            raise SitesRegistryError("harbor secret : réponse vide")
        after = self._call("GET", f"/robots/{rid}", "lecture").json()
        return {"registry": self.registry, "username": str(after.get("name") or full.get("name")), "password": secret,
                "expires_at": int(after.get("expires_at") or 0), "robot_id": rid, "renewed": True}

    def revoke(self, account: str) -> int | None:
        """Supprime le robot du compte ; rend son id, ou None s'il n'y en avait pas."""
        robot = self.find(account)
        if robot is None:
            return None
        rid = int(robot["id"])
        self._call("DELETE", f"/robots/{rid}", "suppression", ok=(200, 404))
        return rid


def iso_utc(epoch: int) -> str:
    """Échéance rendue par les routes : ISO 8601 UTC à la seconde, `2026-12-06T00:00:00Z`."""
    from datetime import datetime, timezone

    return datetime.fromtimestamp(int(epoch), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch(v: Any) -> float:
    """`creation_time` de Harbor (ISO 8601, `Z`) en secondes ; 0 si illisible."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    if not isinstance(v, str) or not v:
        return 0.0
    from datetime import datetime

    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


@dataclass
class SitesRegistry:
    """Émission par compte, avec le quota et le journal. `issuer` None = éteint (pas d'identifiants dans l'env)."""

    issuer: HarborIssuer | None
    quota: int = QUOTA_PER_DAY
    window_s: float = QUOTA_WINDOW_S
    clock: Any = time.monotonic
    _hits: dict[str, deque[float]] = field(default_factory=dict, init=False, repr=False)
    _locks: dict[str, threading.Lock] = field(default_factory=dict, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, **kw: Any) -> SitesRegistry:
        return cls(issuer=HarborIssuer.from_env(env), **kw)

    @property
    def available(self) -> bool:
        return self.issuer is not None

    def _account_lock(self, account: str) -> threading.Lock:
        with self._lock:
            return self._locks.setdefault(account, threading.Lock())

    def _take(self, account: str) -> None:
        now = self.clock()
        with self._lock:
            if len(self._hits) > 10_000:  # comptes sans émission depuis 24 h : mémoire bornée
                for k in [k for k, q in self._hits.items() if not q or now - q[-1] > self.window_s]:
                    del self._hits[k]
            q = self._hits.setdefault(account, deque())
            while q and now - q[0] > self.window_s:
                q.popleft()
            if len(q) >= self.quota:
                raise QuotaExceeded(max(1, int(self.window_s - (now - q[0])) + 1))
            q.append(now)

    def issue(self, account: str, machine_id: str) -> dict[str, Any]:
        """`{registry, username, password, expires_at}` ; lève QuotaExceeded, SitesRegistryError ou ValueError."""
        if self.issuer is None:
            raise SitesRegistryError("émetteur non configuré")
        robot_name(account)  # compte illisible : refusé avant de consommer le quota
        self._take(account)
        with self._account_lock(account):  # deux demandes simultanées d'un compte : un seul robot
            out = self.issuer.issue(account)
        logger.info("sites-registry : robot %s %s pour %s (machine %s), échéance %s",
                    out["robot_id"], "renouvelé" if out["renewed"] else "créé", account, machine_id,
                    out["expires_at"])
        return {"registry": out["registry"], "username": out["username"], "password": out["password"],
                "expires_at": iso_utc(out["expires_at"])}

    def revoke(self, account: str, reason: str) -> bool:
        """Supprime le robot du compte (`reason` : unlink | erase | operator) ; True s'il existait."""
        if self.issuer is None or not _RE_ACCOUNT.match(account or ""):
            return False  # un compte au nom illisible n'a jamais eu de robot
        with self._account_lock(account):
            rid = self.issuer.revoke(account)
        if rid is not None:
            logger.info("sites-registry : robot %s supprimé pour %s (%s)", rid, account, reason)
        return rid is not None


__all__ = ["DURATION_DAYS", "ENV_PASSWORD", "ENV_URL", "ENV_USER", "PROJECT", "QUOTA_PER_DAY", "HarborIssuer",
           "QuotaExceeded", "SitesRegistry", "SitesRegistryError", "iso_utc", "robot_name"]
