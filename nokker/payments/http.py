"""Routes des paiements sur nokker.ai (docs/PAYMENTS.md §5), derrière le drapeau `payments` :

    from nokker.payments import http as payments_http
    payments_http.configure(Payments(...), device_key=..., origin=...)   # drapeau allumé seulement
    if payments_http.handle(self, method, path, account): return          # dans _account_route

`configure(None)` (le défaut) : `handle` rend False, le serveur répond comme avant (404).

Session du compte (`us_…`) ; erreurs `{"error": {"code", "message"}}` ; 60 écritures par minute et par compte :
    GET    /v1/payments/budgets                          → {is_payer, timezone, members: [ligne]}
    PUT    /v1/payments/budgets/{us} {currency, per_purchase, per_day, per_month, minor_allowed?}   → ligne (payeur)
    DELETE /v1/payments/budgets/{us}                     → ligne (retour au défaut ; payeur)
    GET    /v1/payments/mandates[?member=us_…]           → {mandates: [carte]}
    GET    /v1/payments/mandates/{md}[?member=us_…]      → carte + mandate + envelope
    GET    /v1/payments/mandates/{md}/verify[?member=…]  → {mandate_id, status, valid, expired, checks, errors}
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlsplit

from nokker.payments.service import PaymentError, Payments

PREFIX = "/v1/payments"
RATE_MAX, RATE_WINDOW_S = 60, 60.0

_svc: Payments | None = None
_device_key: Callable[[str], str | None] | None = None
_origin: str | None = None
_hits: dict[str, deque[float]] = defaultdict(deque)
_lock = threading.Lock()


def configure(svc: Payments | None, *, device_key: Callable[[str], str | None] | None = None,
              origin: str | None = None) -> None:
    global _svc, _device_key, _origin
    _svc, _device_key, _origin = svc, device_key, origin
    _hits.clear()


def service() -> Payments | None:
    return _svc


def is_payments_path(path: str) -> bool:
    """Route des paiements ET drapeau allumé (sinon le serveur répond comme avant)."""
    route = path.split("?", 1)[0].rstrip("/")
    return _svc is not None and (route == PREFIX or route.startswith(PREFIX + "/"))


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


def handle(h: Any, method: str, path: str, account: Any) -> bool:
    svc = _svc
    route = path.split("?", 1)[0].rstrip("/")
    if svc is None or not (route == PREFIX or route.startswith(PREFIX + "/")):
        return False
    parts = [x for x in route[len(PREFIX):].split("/") if x]
    if method != "GET" and not _rate_ok(account.account_id):
        h._json(429, {"error": {"code": "rate", "message": "trop d'écritures, réessayer dans une minute"}})
        return True
    member = (parse_qs(urlsplit(h.path).query).get("member") or [None])[0]
    try:
        out = dispatch(svc, method, parts, account.account_id, member, lambda: h._read_json_body())
    except PaymentError as e:
        h._json(e.status, {"error": {"code": e.code, "message": e.message}})
        return True
    if out is None:
        h._json(404, {"error": {"code": "not_found", "message": "not found"}})
    else:
        h._json(*out)
    return True


def dispatch(svc: Payments, method: str, parts: list[str], me: str, member: str | None,
             body: Callable[[], Any]) -> tuple[int, Any] | None:
    n = len(parts)
    if n >= 1 and parts[0] == "budgets":
        if n == 1 and method == "GET":
            return 200, svc.budgets_view(me)
        if n == 2 and method == "PUT":
            b = body()
            return 200, svc.set_budget(me, parts[1], b if isinstance(b, dict) else {})
        if n == 2 and method == "DELETE":
            return 200, svc.reset_budget(me, parts[1])
        return None
    if n >= 1 and parts[0] == "mandates" and method == "GET":
        if n == 1:
            return 200, {"mandates": svc.mandates(me, member)}
        if n == 2:
            return 200, svc.mandate(me, parts[1], member)
        if n == 3 and parts[2] == "verify":
            return 200, svc.verify(me, parts[1], member, device_key=_device_key, origin=_origin)
    return None


__all__ = ["PREFIX", "configure", "dispatch", "handle", "is_payments_path", "service"]
