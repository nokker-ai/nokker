"""Routes du foyer sur nokker.ai (lot X23, contrat docs/LOKKAL.md §7.29), derrière le drapeau `household` :

    from nokker.household import http as household_http
    household_http.configure(Household(...), approvals=GuardianCards(...))   # drapeau allumé seulement
    if household_http.handle(self, method, path, account): return             # dans _account_route

`configure(None)` (drapeau éteint, le défaut) : `enabled()` est faux, `handle` rend False, le serveur répond comme
avant (404 sur /v1/household*).

Session du compte (`us_…`) ; erreurs `{"error": {"code", "message"}}` ; 60 écritures par minute et par compte :
    GET    /v1/household                                → vue (membres, tours par membre, plafonds ; jamais de contenu)
    POST   /v1/household {name?}                        → 201 vue (le créateur est le payeur)
    DELETE /v1/household                                → {deleted} (payeur)
    GET    /v1/household/consent?lang=fr|en             → {version, lang, text} (attestation du représentant légal)
    GET    /v1/household/consents                       → {consents: [...]} (payeur : journal des attestations)
    GET    /v1/household/invitations                    → {invitations} (payeur)
    POST   /v1/household/invitations {email, profile?, attestation?}   → 201 invitation (payeur)
    DELETE /v1/household/invitations/{iv}               → {revoked} (payeur)
    GET    /v1/household/invitations/received           → {invitations} (adressées à moi)
    POST   /v1/household/invitations/{iv}/accept|decline
    DELETE /v1/household/members/{us}                   → {removed} (payeur ; ou soi-même, adulte)
    PUT    /v1/household/members/{us}/cap {turns_per_day: n | null}   → ligne (payeur) ; DELETE → défaut
    PUT    /v1/household/members/{us}/profile {profile, attestation?} → ligne (payeur, drapeau household_minors)
    POST   /v1/household/members/{us}/consent {attestation}           → ligne (payeur : renouvellement)
    DELETE /v1/household/members/{us}/consent           → ligne `suspended` (payeur : retrait)
    GET    /v1/household/policy   PUT {minor_connectors?, minor_agents?}   (payeur)
    GET    /v1/household/notes?since=                   → {notes, cursor}
    PUT    /v1/household/notes/{nom} {kind, title, text, base_sha?, links?}   → {note, accepted, skipped, quarantined}
    DELETE /v1/household/notes/{nom}                    → {forgotten}
    GET    /v1/household/notes/quarantine               POST …/quarantine/{q}/restore   (payeur)
    GET    /v1/household/approvals                      → cartes fortes en attente des profils mineurs (payeur)
    POST   /v1/household/approvals/{id}/challenge {member}         → {challenge, nonce, exp}
    POST   /v1/household/approvals/{id}/decision {member, decision, …preuve d'appareil}   → carte
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Any
from urllib.parse import parse_qs, urlsplit

from nokker.household.service import Household
from nokker.team.model import TeamError

PREFIX = "/v1/household"
RATE_MAX, RATE_WINDOW_S = 60, 60.0

_hh: Household | None = None
_approvals: Any = None  # GuardianCards (nokker/api/server.py) : cartes des profils mineurs, décidées par le payeur
_hits: dict[str, deque[float]] = defaultdict(deque)
_lock = threading.Lock()


def configure(household: Household | None, *, approvals: Any = None) -> None:
    global _hh, _approvals
    _hh, _approvals = household, approvals
    _hits.clear()


def enabled() -> bool:
    return _hh is not None


def service() -> Household | None:
    return _hh


def is_household_path(path: str) -> bool:
    route = path.split("?", 1)[0]
    return route == PREFIX or route.startswith(PREFIX + "/")


def _rate_ok(key: str) -> bool:
    now = time.monotonic()
    with _lock:
        q = _hits[key]
        while q and now - q[0] > RATE_WINDOW_S:
            q.popleft()
        if len(q) >= RATE_MAX:
            return False
        q.append(now)
        return True


def _body(h: Any) -> dict[str, Any]:
    b = h._read_json_body()
    return b if isinstance(b, dict) else {}


def _q(h: Any, key: str) -> str | None:
    return (parse_qs(urlsplit(h.path).query).get(key) or [None])[0]


def _ip(h: Any) -> str | None:
    try:
        from nokker.onboarding.http import client_ip

        return client_ip(h)
    except Exception:  # noqa: BLE001 — l'adresse est un indice du journal, jamais une condition
        return None


def handle(h: Any, method: str, path: str, account: Any) -> bool:
    hh = _hh
    route = path.split("?", 1)[0].rstrip("/")
    if hh is None or not is_household_path(route):
        return False
    parts = [x for x in route[len(PREFIX):].split("/") if x]
    if method != "GET" and not _rate_ok(account.account_id):
        h._json(429, {"error": {"code": "rate", "message": "trop d'écritures, réessayer dans une minute"}})
        return True
    try:
        out = _dispatch(hh, h, method, parts, account)
    except TeamError as e:
        h._json(e.status, {"error": {"code": e.code, "message": e.message}})
        return True
    if out is None:
        h._json(404, {"error": {"code": "not_found", "message": "not found"}})
    else:
        h._json(*out)
    return True


def _dispatch(hh: Household, h: Any, method: str, parts: list[str], a: Any) -> tuple[int, Any] | None:
    n = len(parts)
    if n == 0:
        if method == "GET":
            return 200, hh.view(a)
        if method == "POST":
            return 201, hh.create(a, _body(h))
        if method == "DELETE":
            return 200, hh.delete(a)
        return None
    head = parts[0]
    if head == "consent" and n == 1 and method == "GET":
        return 200, hh.consent_document(_q(h, "lang") or "fr")
    if head == "consents" and n == 1 and method == "GET":
        return 200, hh.consents_of(a)
    if head == "invitations":
        if n == 1 and method == "GET":
            return 200, hh.invitations(a)
        if n == 1 and method == "POST":
            return 201, hh.invite(a, _body(h), ip=_ip(h))
        if n == 2 and parts[1] == "received" and method == "GET":
            return 200, hh.received(a)
        if n == 2 and method == "DELETE":
            return 200, hh.revoke_invite(a, parts[1])
        if n == 3 and method == "POST" and parts[2] == "accept":
            return 200, hh.accept(a, parts[1])
        if n == 3 and method == "POST" and parts[2] == "decline":
            return 200, hh.decline(a, parts[1])
        return None
    if head == "members" and n >= 2:
        acc = parts[1]
        if n == 2 and method == "DELETE":
            return 200, hh.remove(a, acc)
        if n == 3 and parts[2] == "cap":
            if method == "PUT":
                return 200, hh.set_cap(a, acc, _body(h))
            if method == "DELETE":
                return 200, hh.reset_cap(a, acc)
        if n == 3 and parts[2] == "profile" and method == "PUT":
            return 200, hh.set_profile(a, acc, _body(h), ip=_ip(h))
        if n == 3 and parts[2] == "consent":
            if method == "POST":
                return 200, hh.renew_consent(a, acc, _body(h), ip=_ip(h))
            if method == "DELETE":
                return 200, hh.revoke_consent(a, acc)
        return None
    if head == "policy" and n == 1:
        if method == "GET":
            return 200, hh.policy(a)
        if method == "PUT":
            return 200, hh.put_policy(a, _body(h))
        return None
    if head == "notes":
        if n == 1 and method == "GET":
            return 200, hh.notes(a, _q(h, "since"))
        if n == 2 and parts[1] == "quarantine" and method == "GET":
            return 200, hh.quarantined(a)
        if n == 4 and parts[1] == "quarantine" and parts[3] == "restore" and method == "POST":
            return 200, hh.restore_note(a, parts[2])
        if n == 2 and method == "PUT":
            return 200, hh.put_note(a, parts[1], _body(h))
        if n == 2 and method == "DELETE":
            return 200, hh.forget_note(a, parts[1])
        return None
    if head == "approvals":
        if _approvals is None:
            return None
        if n == 1 and method == "GET":
            return 200, _approvals.pending(a)
        if n == 3 and method == "POST" and parts[2] == "challenge":
            return 200, _approvals.challenge(a, parts[1], _body(h))
        if n == 3 and method == "POST" and parts[2] == "decision":
            return 200, _approvals.decide(a, parts[1], _body(h))
        return None
    return None


__all__ = ["PREFIX", "configure", "enabled", "handle", "is_household_path", "service"]
