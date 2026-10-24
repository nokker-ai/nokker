"""Client des invitations de `nokker-beta-api` (front1, routes /internal/invitations/*, Bearer BETA_INTERNAL_TOKEN).

Les candidatures et les jetons d'invitation restent en Suisse sur front1 : ce client ne détient jamais de jeton, il
l'envoie à front1 pour vérification (`check`), consommation (`redeem`) ou restitution (`release`). Le point d'entrée de
l'opérateur (console) n'utilise que `send`, `invitations` et `preview`, avec l'id front1 de la candidature (`source_id`).
Si le processus meurt entre `redeem` et `release`, le jeton reste consommé : l'opérateur renvoie une invitation, qui
remplace la précédente.
"""
from __future__ import annotations

import json
import secrets
from collections.abc import Callable
from typing import Any, Protocol
from urllib import error as urllib_error
from urllib import request as urllib_request


class FrontError(Exception):
    """Refus ou panne de front1. `code` : `template_not_approved`, `unknown_candidature`, `invalid_token`,
    `not_releasable`, `onboarding_url_missing`, `send_failed`, `invalid`, `unauthorized`, `unavailable`."""

    def __init__(self, code: str, status: int = 0):
        super().__init__(code)
        self.code, self.status = code, status


class Front(Protocol):
    def send(self, candidature_id: int, lang: str | None, actor: str) -> dict[str, Any]: ...
    def invitations(self, candidature_id: int) -> list[dict[str, Any]]: ...
    def preview(self, candidature_id: int, lang: str | None = None) -> dict[str, Any]: ...
    def check(self, token: str) -> dict[str, Any]: ...
    def redeem(self, token: str) -> dict[str, Any]: ...
    def release(self, invitation_id: int) -> None: ...
    def send_code(self, email: str, code: str, lang: str) -> None: ...


Transport = Callable[[str, dict[str, Any], dict[str, str], float], tuple[int, dict[str, Any]]]


def _urllib_post(url: str, body: dict[str, Any], headers: dict[str, str], timeout_s: float) -> tuple[int, dict[str, Any]]:
    req = urllib_request.Request(url, data=json.dumps(body).encode(), method="POST", headers=headers)
    try:
        with urllib_request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310 — URL fixée par la config
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib_error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


class FrontInvitations:
    """`base_url` = http://<front>:8122 (adresse privée du front) ou https://intake.nokker.ai ; `token` =
    BETA_INTERNAL_TOKEN ; `extra_headers` = jeton de service CF Access (CF-Access-Client-Id / -Secret) derrière intake."""

    def __init__(self, base_url: str, token: str, timeout_s: float = 30.0, transport: Transport | None = None,
                 extra_headers: dict[str, str] | None = None):
        if not base_url or not token:
            raise ValueError("base_url et token requis")
        self._base, self._timeout = base_url.rstrip("/"), timeout_s
        # User-Agent explicite : derrière intake.nokker.ai, Cloudflare refuse celui de urllib (« error code: 1010 »,
        # vu depuis le pod OVH le 21.10 — toute invitation répondait front_unavailable).
        self._headers = {**(extra_headers or {}), "Authorization": f"Bearer {token}", "Content-Type": "application/json",
                         "User-Agent": "nokker-core/1"}
        self._post = transport or _urllib_post

    def _call(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._internal(f"invitations/{path}", body)

    def _internal(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            status, data = self._post(f"{self._base}/internal/{path}", body, self._headers, self._timeout)
        except (OSError, ValueError):
            raise FrontError("unavailable") from None
        if status == 200 and data.get("ok"):
            return data
        if status == 401:
            raise FrontError("unauthorized", status)
        raise FrontError(str(data.get("error") or "unavailable"), status)

    def send(self, candidature_id: int, lang: str | None, actor: str) -> dict[str, Any]:
        r = self._call("send", {"candidature_id": candidature_id, "lang": lang, "actor": actor})
        return {k: r[k] for k in ("invitation_id", "sent_at", "expires_at", "lang")}

    def invitations(self, candidature_id: int) -> list[dict[str, Any]]:
        return self._call("list", {"candidature_id": candidature_id})["invitations"]

    def preview(self, candidature_id: int, lang: str | None = None) -> dict[str, Any]:
        r = self._call("preview", {"candidature_id": candidature_id, "lang": lang})
        return {k: r[k] for k in ("to", "lang", "subject", "text", "html", "template_sha")}

    def check(self, token: str) -> dict[str, Any]:
        r = self._call("check", {"token": token})
        return {"valid": True, "lang": r["lang"], "prenom": r["prenom"]} if r.get("valid") else {"valid": False}

    def redeem(self, token: str) -> dict[str, Any]:
        r = self._call("redeem", {"token": token})
        return {k: r[k] for k in ("invitation_id", "email", "lang", "prenom")}

    def release(self, invitation_id: int) -> None:
        self._call("release", {"invitation_id": invitation_id})

    def send_code(self, email: str, code: str, lang: str) -> None:
        """Code de connexion (6 chiffres) : front1 l'envoie avec son gabarit figé, seulement à une adresse invitée."""
        self._internal("auth/code/send", {"email": email, "code": code, "lang": lang})


class MemoryFront:
    """Faux front1 en mémoire (tests de la console et du parcours). Un jeton émis par `send` se lit dans `.tokens`."""

    def __init__(self, candidates: dict[int, dict[str, str]] | None = None):
        self.candidates = candidates or {}
        self.tokens: dict[str, dict[str, Any]] = {}
        self.sent: list[dict[str, Any]] = []
        self.codes: list[tuple[str, str, str]] = []  # (adresse, code, langue) envoyés par send_code
        self.fail_send = False

    def send(self, candidature_id: int, lang: str | None, actor: str) -> dict[str, Any]:
        cand = self.candidates.get(candidature_id)
        if cand is None:
            raise FrontError("unknown_candidature", 404)
        if self.fail_send:
            raise FrontError("send_failed", 502)
        for t in self.tokens.values():
            if t["candidature_id"] == candidature_id:
                t["superseded"] = True
        inv_id = len(self.tokens) + 1
        token = secrets.token_urlsafe(16)
        lang = lang or cand.get("lang", "fr")
        self.tokens[token] = {"id": inv_id, "candidature_id": candidature_id, "email": cand["email"], "lang": lang,
                              "prenom": cand.get("prenom", ""), "redeemed": False, "superseded": False}
        r = {"invitation_id": inv_id, "sent_at": "2026-10-22T09:00:00Z", "expires_at": "2026-10-29T09:00:00Z",
             "lang": lang}
        self.sent.append({**r, "to": cand["email"], "actor": actor})
        return r

    def invitations(self, candidature_id: int) -> list[dict[str, Any]]:
        return [{"id": t["id"], "lang": t["lang"],
                 "state": "redeemed" if t["redeemed"] else "superseded" if t["superseded"] else "sent"}
                for t in self.tokens.values() if t["candidature_id"] == candidature_id]

    def preview(self, candidature_id: int, lang: str | None = None) -> dict[str, Any]:
        cand = self.candidates.get(candidature_id)
        if cand is None:
            raise FrontError("unknown_candidature", 404)
        return {"to": cand["email"], "lang": lang or cand.get("lang", "fr"), "subject": "…", "text": "…",
                "html": "…", "template_sha": "0" * 64}

    def _live(self, token: str) -> dict[str, Any] | None:
        t = self.tokens.get(token)
        return t if t and not t["redeemed"] and not t["superseded"] else None

    def check(self, token: str) -> dict[str, Any]:
        t = self._live(token)
        return {"valid": True, "lang": t["lang"], "prenom": t["prenom"]} if t else {"valid": False}

    def redeem(self, token: str) -> dict[str, Any]:
        t = self._live(token)
        if t is None:
            raise FrontError("invalid_token", 410)
        t["redeemed"] = True
        return {"invitation_id": t["id"], "email": t["email"], "lang": t["lang"], "prenom": t["prenom"]}

    def release(self, invitation_id: int) -> None:
        for t in self.tokens.values():
            if t["id"] == invitation_id and t["redeemed"] and not t["superseded"]:
                t["redeemed"] = False
                return
        raise FrontError("not_releasable", 409)

    def send_code(self, email: str, code: str, lang: str) -> None:
        if self.fail_send:
            raise FrontError("send_failed", 502)
        self.codes.append((email, code, lang))
