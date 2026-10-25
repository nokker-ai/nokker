"""Routes HTTP d'intégration (contrat C5). L'API branche ce module en 3 lignes dans `nokker/api/server.py` :

    from nokker.onboarding import http as onboarding_http
    onboarding_http.configure(onboarding=Onboarding(...))            # une fois au démarrage
    if onboarding_http.handle(self, "GET" | "POST", path): return    # tête de do_GET / do_POST

`handle` rend False pour tout chemin qui n'est pas à lui, ou tant que `configure` n'a pas été appelé (l'API se
comporte alors exactement comme avant). Routes publiques (le testeur n'a pas encore de session) :

    GET  /v1/onboarding/invitation?t=<jeton>    → {valid, lang, prenom}            (ne consomme rien)
    GET  /v1/onboarding/terms?lang=fr|en        → {lang, sha256, text}
    POST /v1/onboarding/accept                  → 201 {account_id, device_id, lang}
         {token, terms_sha256, public_key_spki, name, training_consent?}   (la clé privée ne quitte jamais l'appareil)

Connexion par code e-mail (nokker/onboarding/emailcode.py), publiques elles aussi :

    POST /v1/auth/email/start   {email, lang}                          → 200 {sent: true}, toujours
    POST /v1/auth/email/verify  {email, code, name, public_key_spki}   → 200 {device_id, user} | 401 invalid_code
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

from nokker.onboarding import terms
from nokker.onboarding.consents import SCOPES
from nokker.onboarding.emailcode import CodeError, EmailCodes
from nokker.onboarding.service import Onboarding, OnboardingError

PREFIX = "/v1/onboarding/"
AUTH_PREFIX = "/v1/auth/email/"
RATE_MAX, RATE_WINDOW_S = 30, 60.0

_onboarding: Onboarding | None = None
_codes: EmailCodes | None = None
_hits: dict[str, deque[float]] = defaultdict(deque)
_lock = threading.Lock()


# Profil mineur d'un foyer (lot X23, `Household.is_minor`) : jamais de consentement d'entraînement, refusé ici (403
# `minor_training_refused`) comme en base (trigger de la migration 039). None (défaut, drapeau éteint) : aucun contrôle.
_is_minor: Any = None


def configure_minors(is_minor: Any) -> None:
    global _is_minor
    _is_minor = is_minor


def configure(onboarding: Onboarding | None, codes: EmailCodes | None = None) -> None:
    global _onboarding, _codes
    _onboarding, _codes = onboarding, codes
    _hits.clear()


def client_ip(handler: Any) -> str | None:
    h = handler.headers
    return (h.get("CF-Connecting-IP") or h.get("X-Real-IP") or handler.client_address[0] or None)


def _rate_ok(ip: str | None) -> bool:
    now = time.monotonic()
    with _lock:
        q = _hits[ip or "?"]
        while q and now - q[0] > RATE_WINDOW_S:
            q.popleft()
        if len(q) >= RATE_MAX:
            return False
        q.append(now)
        return True


def _handle_code(handler: Any, method: str, route: str) -> bool:
    if method != "POST" or route not in ("start", "verify") or _codes is None:
        return False
    body = handler._read_json_body()
    ip = client_ip(handler)
    try:
        if route == "start":
            handler._json(200, _codes.start(str(body.get("email") or ""), str(body.get("lang") or "fr"), ip))
        else:
            handler._json(200, _codes.verify(str(body.get("email") or ""), str(body.get("code") or ""),
                                             str(body.get("name") or ""), str(body.get("public_key_spki") or ""), ip))
    except CodeError as e:
        if e.code == "rate":
            handler.send_response(429)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Retry-After", "900")
            payload = b'{"error": "rate"}'
            handler.send_header("Content-Length", str(len(payload)))
            handler.end_headers()
            handler.wfile.write(payload)
        else:
            handler._json(e.status, {"error": e.code})
    return True


def handle(handler: Any, method: str, path: str) -> bool:
    url = urlsplit(path)
    if url.path.startswith(AUTH_PREFIX):
        return _handle_code(handler, method, url.path[len(AUTH_PREFIX):])
    if _onboarding is None or not url.path.startswith(PREFIX):
        return False
    route = url.path[len(PREFIX):]
    query = parse_qs(url.query)
    ob = _onboarding
    if not _rate_ok(client_ip(handler)):
        handler._json(429, {"error": "rate"})
        return True
    try:
        if method == "GET" and route == "invitation":
            handler._json(200, ob.invitation((query.get("t") or [""])[0]))
        elif method == "GET" and route == "terms":
            t = terms.load((query.get("lang") or ["fr"])[0])
            handler._json(200, {"lang": t.lang, "sha256": t.sha256, "text": t.text})
        elif method == "POST" and route == "accept":
            body = handler._read_json_body()
            out = ob.accept(str(body.get("token") or ""), str(body.get("terms_sha256") or ""),
                            str(body.get("public_key_spki") or ""), str(body.get("name") or ""), client_ip(handler),
                            training_consent=body.get("training_consent") is True)
            handler._json(201, out)
        else:
            return False
    except OnboardingError as e:
        handler._json(e.status, {"error": e.code})
    return True


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        value = datetime.fromtimestamp(value, tz=UTC)
    return value.isoformat()


def account_route(handler: Any, method: str, path: str, account: Any) -> bool:
    """« Mon compte » (docs/CLIENT-API.md), appelé par `_account_route` du serveur : session vérifiée, locataire fixé,
    compte `us_…`. `GET /v1/account` et `PUT /v1/account/consent` (retrait ou accord du consentement d'usage des
    données : session normale, pas d'appareil fort ; une ligne seulement quand le choix change)."""
    ob = _onboarding
    if path == "/v1/account" and method == "GET":
        accepted = ob.acceptances.for_account(account.account_id) if ob is not None else []
        last = accepted[-1] if accepted else None
        training = bool(ob is not None and ob.consents is not None and ob.consents.current(account.account_id, "training"))
        minor = bool(_is_minor is not None and _is_minor(account.account_id))
        handler._json(200, {
            "email": account.email, "lang": account.lang, "created_at": _iso(account.created_at),
            "status": account.status,
            "terms": {"version": last["terms_version_sha256"], "accepted_at": _iso(last["accepted_at"])} if last else None,
            "consents": {"training": training, **({"training_locked": "minor"} if minor else {})}})
        return True
    if path == "/v1/account/consent" and method == "PUT":
        if ob is None or ob.consents is None:
            handler._json(503, {"error": "consents_disabled"})
            return True
        body = handler._read_json_body()
        scope, granted = body.get("scope"), body.get("granted")
        if scope not in SCOPES or not isinstance(granted, bool):
            handler._json(400, {"error": "invalid"})
            return True
        if granted and scope == "training" and _is_minor is not None and _is_minor(account.account_id):
            handler._json(403, {"error": "minor_training_refused"})
            return True
        if ob.consents.current(account.account_id, scope) != granted:
            accepted = ob.acceptances.for_account(account.account_id)
            ob.consents.record(account.account_id, scope, granted, "account",
                               accepted[-1]["terms_version_sha256"] if accepted else None, client_ip(handler))
        handler._json(200, {"scope": scope, "granted": granted})
        return True
    return False
