"""Sites connectés — routes de l'écran (docs/CONNECTED-SITES.md §15), servies par l'API locale de Lokkal sous
`/v1/local/sites*` (jeton local ; par la passerelle : le propriétaire relayé).

Les réponses ne portent JAMAIS de mot de passe, de code, de lien magique ni de cookie. Les saisies arrivent scellées
(`{sealed}`, `crypto.py` / `sealed.js`) ; une saisie en clair est refusée (400), même sur la machine.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlsplit

from nokker.sites.model import Connector
from nokker.sites.requests import RequestError
from nokker.sites.service import CONSENT_VERSION, SiteError, SitesService
from nokker.sites.store import StoreError

PREFIX = "/v1/local/sites"


def _err(status: int, code: str, message: str) -> tuple[int, dict[str, Any]]:
    return status, {"error": {"code": code, "message": message}}


class SitesHttp:
    def __init__(self, service: SitesService, who: Callable[[], str],
                 minor: Callable[[str], bool] | None = None) -> None:
        self.service = service
        self.who = who
        # profil mineur : la fonction ne lui est pas ouverte (§4.1) — 403 `minor` sur TOUTES les routes
        self.minor = minor or (lambda account: False)

    def card(self, conn: Connector, account: str) -> dict[str, Any]:
        access = self.service.access(account, conn)
        gov = self.service.gov(conn)
        out: dict[str, Any] = {
            "id": conn.id, "site": conn.site, "site_name": conn.site_name, "label": conn.label,
            "default": conn.default, "username": conn.username, "account_name": conn.account_name,
            "state": conn.state.value, "signal": conn.signal,
            "state_text": conn.state_text(self.service.where(account)), "paused_until": conn.paused_until,
            "last_ok_at": conn.last_ok_at, "last_auto_login_at": conn.last_auto_login_at,
            "access": access, "quotas": {c: {"used": gov.used(conn.id, c), "max": lim.day}
                                         for c, lim in gov.limits.items()}}
        if access == "owner":
            out["credentials"] = {"stored": self.service.credentials.has(conn),
                                  "updated_at": self.service.credentials.updated_at(conn)}
            out["grants"] = [{"account_id": a, "level": lvl} for a, lvl in conn.grants.items()]
        else:
            out["owner"] = {"account_id": conn.account}
        req = self.service.requests.open_for(conn.id) if access == "owner" else None
        if req is not None:
            out["request"] = {"id": req.id, "kind": req.kind, "expires_at": req.expires_at}
        if access == "owner" and conn.state.value == "needs_you":
            url = self.service.handoff_url(conn)  # usage unique, propriétaire seulement
            if url:
                out["handoff_url"] = url
        return out

    def handle(self, method: str, path: str, body: Any, relayed: bool = False) -> tuple[int, dict[str, Any]] | None:
        account = self.who()
        parts = urlsplit(path)
        p = parts.path.rstrip("/")
        if not p.startswith(PREFIX):
            return None
        rest = p[len(PREFIX):].strip("/").split("/") if p != PREFIX else []
        body = body if isinstance(body, dict) else {}
        if self.minor(account):
            return _err(403, "minor", "les sites connectés ne sont pas proposés à un profil mineur")
        try:
            if not rest:
                if method == "GET":
                    return 200, {"enabled": True,
                                 "sites": [self.card(c, account) for c, _ in self.service.visible(account)],
                                 "catalog": self.service.catalog(), "consent_version": CONSENT_VERSION,
                                 "consents": self.service.store.consented_sites(account, CONSENT_VERSION)}
                if method == "POST":
                    return self._add(account, body)
                return None
            if rest[0] == "handoff" and len(rest) == 2 and method == "POST":
                screen = self.service.open_handoff(rest[1], account)
                if screen is None:
                    return _err(410, "handoff_gone", "lien de passation déjà utilisé, expiré ou pas à toi")
                return 200, {"screen_url": screen}
            if rest[0] == "requests" and len(rest) >= 2:
                return self._request(account, method, rest[1], rest[2:] if len(rest) > 2 else [], body)
            if rest[0] == "consent" and method == "POST":
                site = self.service.find_site(str(body.get("site") or ""))
                if site is None or body.get("version") != CONSENT_VERSION:
                    return _err(400, "bad_consent", "site ou version de l'avertissement inconnus")
                self.service.store.consent(account, site.site, CONSENT_VERSION)
                return 204, {}
            conn = self.service.store.get(rest[0])
            if conn is None or self.service.access(account, conn) is None:
                return _err(404, "not_found", "site inconnu")
            owner = self.service.access(account, conn) == "owner"
            sub = rest[1] if len(rest) > 1 else ""
            if not sub and method == "GET":
                return 200, self.card(conn, account)
            if not owner:
                return _err(403, "not_owner", "action réservée au propriétaire du site")
            if not sub and method == "PATCH":
                if "label" in body:
                    conn = self.service.store.rename(conn.id, str(body["label"]))
                if body.get("default") is True:
                    conn = self.service.store.set_default(conn.id)
                return 200, self.card(conn, account)
            if not sub and method == "DELETE":
                q = parse_qs(parts.query)
                forget = q.get("forget_credentials", ["true"])[0] != "false"
                self.service.delete(conn, forget_credentials=forget)
                return 200, {"deleted": True}
            if sub == "cancel" and method == "POST":
                if not self.service.cancel(conn):
                    return _err(409, "not_connecting", "aucune connexion en cours")
                return 202, {"status": "cancelling"}
            if sub == "connect" and method == "POST":
                if body.get("answer_here") is True:
                    self.service.answer_here(conn)
                flow = self.service.connect(conn, wait_s=0.5)
                return 202, {"status": flow.status}
            if sub == "pause" and method == "DELETE":  # lever une pause : l'utilisateur seulement, jamais le modèle
                conn = self.service.lift_pause(conn, wait_s=0.5)  # état utile vérifié dans le navigateur
                return 200, self.card(conn, account)
            if sub == "credentials" and method == "DELETE":
                self.service.credentials.delete(conn)
                return 204, {}
            if sub == "credentials" and method == "POST":
                # Remplacer le mot de passe : une demande `password` est ouverte ; la page y répond scellé.
                req = self.service.replace_password(conn)
                return 201, {"request": req.public()}
            return None
        except StoreError as e:
            status = {"not_found": 404, "label_taken": 409, "too_many": 409}.get(e.code, 400)
            return _err(status, e.code, str(e))
        except SiteError as e:
            return _err(400, e.code, e.code)

    def _add(self, account: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if "password" in body or "code" in body:
            return _err(400, "no_plain_secret", "un mot de passe ne s'envoie jamais en clair : saisie scellée seulement")
        site = self.service.find_site(str(body.get("site") or body.get("url") or ""))
        if site is None:
            return _err(400, "unsupported", "site sans recette (« Autre site » : lot suivant)")
        if not self.service.store.has_consent(account, site.site, CONSENT_VERSION):
            return _err(409, "consent_required", "avertissement sur les conditions du site à accepter d'abord")
        conn = self.service.add(account, site.site, str(body.get("username") or ""), body.get("label") or None)
        if body.get("answer_here") is True:
            self.service.answer_here(conn)  # saisie à l'écran : la demande Telegram attend 30 s
        flow = self.service.connect(conn, wait_s=0.5)
        return 201, {**self.card(self.service.store.get(conn.id) or conn, account), "flow": flow.status}

    def _request(self, account: str, method: str, rid: str, sub: list[str],
                 body: dict[str, Any]) -> tuple[int, dict[str, Any]] | None:
        req = self.service.requests.get(rid)
        if req is None or req.account != account:
            return _err(404, "not_found", "demande inconnue")
        if method == "GET" and not sub:
            return 200, req.public()
        if method == "POST" and sub == ["answer"]:
            if "value" in body:
                return _err(400, "no_plain_secret", "saisie scellée seulement")
            try:
                if req.kind == "app_approval":
                    if body.get("done") is not True:
                        return _err(400, "bad_answer", "cette demande attend « Fait »")
                    self.service.requests.answer_done(rid, account)
                else:
                    self.service.requests.answer_sealed(rid, body.get("sealed") or {}, account)
            except RequestError as e:
                return _err({"expired": 410, "answered": 409, "not_found": 404}.get(e.code, 400), e.code, str(e))
            return 204, {}
        return None


__all__ = ["PREFIX", "SitesHttp"]
