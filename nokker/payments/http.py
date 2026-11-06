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

Agent de paiement (drapeau `pay_agent`, docs/PAY-AGENT.md §7) — absentes (404) sinon :
    GET    /v1/payments/method                       → {publishable_key, method: {kind, brand, last4, …} | null}
    POST   /v1/payments/method/challenge {payment_method}  → {challenge}  (à signer par la passkey, tel quel)
    PUT    /v1/payments/method {payment_method, proof}      → {method}     (preuve vérifiée par l'agent)
    DELETE /v1/payments/method                       → {removed}       (révoque aussi les jetons vivants)
    POST   /v1/payments/mandates/{md}/revoke         → carte + agent  (approbateur du mandat)
    GET    /v1/payments/mandates/{md}/3ds            → défi 3-D Secure en attente (émetteur wise, approbateur)
    POST   /v1/payments/method/link {person}         → {link}   (wise : ouvre le compte émetteur, lance le KYC Wise)
    POST   /v1/payments/method/link/url              → {url, expires_at}  (wise : lien KYC hébergé, retour fixé ici)
    GET    /v1/payments/method/balance               → {balances: [{currency, amount_minor}]}  (wise)
    PUT    /v1/payments/method/card {number, exp_month, exp_year, holder, keep_cvv?, cvv?} → {method}  (own_card, Lokkal)
    POST   /v1/payments/method/cvv-mode {keep, cvv?} → {cvv_kept}                       (own_card)
    POST   /v1/payments/mandates/{md}/cvv {value}    → {accepted}  (own_card : CVV de ce paiement, en mémoire)
    POST   /v1/payments/mandates/{md}/otp {value}    → {accepted}  (own_card : code de la banque, tapé par l'agent)
    POST   /v1/payments/mandates/{md}/3ds {challenge_id, proof | decline: true}  → carte + agent
    POST   /v1/payments/agent/events                 (rappels de l'agent, HMAC ; aucune session)
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
_callback_secret: str = ""
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


def configure_agent(callback_secret: str) -> None:
    global _callback_secret
    _callback_secret = callback_secret


def _agent_on() -> bool:
    return _svc is not None and _svc.agent is not None


def is_payments_post_path(path: str) -> bool:
    """POST des paiements : seulement les routes de l'agent de paiement, drapeau `pay_agent` allumé."""
    route = path.split("?", 1)[0].rstrip("/")
    return _agent_on() and (route in (PREFIX + "/method/challenge", PREFIX + "/method/link", PREFIX + "/method/link/url")
                            or (route.startswith(PREFIX + "/mandates/")
                                and (route.endswith("/revoke") or route.endswith("/3ds") or route.endswith("/cvv")
                                     or route.endswith("/otp")))
                            or route in (PREFIX + "/method/card", PREFIX + "/method/cvv-mode"))


def handle_agent_callback(h: Any, method: str, path: str) -> bool:
    """Rappel de l'agent de paiement (HMAC `X-Nokker-PayAgent-Signature`) : sans session, avant les routes de compte."""
    from nokker.payments.agent import CALLBACK_PATH, verify_callback

    if method != "POST" or path.split("?", 1)[0].rstrip("/") != CALLBACK_PATH or not _agent_on():
        return False
    n = int(h.headers.get("Content-Length", 0) or 0)
    if n <= 0 or n > 64 * 1024:
        h._json(400, {"error": {"code": "bad_length", "message": "corps attendu"}})
        return True
    body = h.rfile.read(n)
    try:
        data = verify_callback(body, h.headers.get("X-Nokker-PayAgent-Signature", ""), _callback_secret)
    except ValueError:
        h._json(401, {"error": {"code": "signature", "message": "rappel non signé"}})
        return True
    ok = _svc.on_agent_callback(data) if _svc is not None else False
    h._json(200 if ok else 404, {"received": ok})
    return True


def agent_callback_path() -> str:
    from nokker.payments.agent import CALLBACK_PATH

    return CALLBACK_PATH


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
    if n >= 1 and parts[0] == "method" and svc.agent is not None:
        return _method_route(svc, method, parts, me, body)
    if n == 3 and parts[0] == "mandates" and parts[2] == "revoke" and method == "POST" and svc.agent is not None:
        return 200, svc.agent_revoke(me, parts[1])
    if n == 3 and parts[0] == "mandates" and parts[2] in ("cvv", "otp") and method == "POST" and svc.agent is not None:
        b = body()
        return 200, svc.agent_provide(me, parts[1], parts[2], b if isinstance(b, dict) else {})
    if n == 3 and parts[0] == "mandates" and parts[2] == "3ds" and svc.agent is not None:
        if method == "GET":
            return 200, svc.agent_threeds(me, parts[1])
        if method == "POST":
            b = body()
            return 200, svc.agent_answer_threeds(me, parts[1], b if isinstance(b, dict) else {})
    if n >= 1 and parts[0] == "mandates" and method == "GET":
        if n == 1:
            return 200, {"mandates": svc.mandates(me, member)}
        if n == 2:
            return 200, svc.mandate(me, parts[1], member)
        if n == 3 and parts[2] == "verify":
            return 200, svc.verify(me, parts[1], member, device_key=_device_key, origin=_origin)
    return None


def _method_route(svc: Payments, method: str, parts: list[str], me: str,
                  body: Callable[[], Any]) -> tuple[int, Any] | None:
    from nokker.payments.agent import AgentUnavailable, method_challenge

    def agent_error(e: AgentUnavailable) -> PaymentError:
        status = e.status if e.status in (400, 403, 404, 409) else 502
        return PaymentError(status, e.code, "moyen de paiement refusé" if status < 500 else
                            "agent de paiement indisponible")

    try:
        wise = svc.agent_settings.issuer == "wise"
        if svc.agent_settings.issuer == "own_card":
            # Carte de la personne (Lokkal) : corps relayés à l'agent de paiement, jamais gardés ni journalisés ici.
            if len(parts) == 1 and method == "GET":
                return 200, {"issuer": "own_card", "method": svc.agent.card(me), "publishable_key": None}
            if len(parts) == 2 and parts[1] == "card" and method == "PUT":
                b = body()
                return 200, {"method": svc.agent.put_card(me, b if isinstance(b, dict) else {})}
            if len(parts) == 2 and parts[1] == "cvv-mode" and method == "POST":
                b = body()
                b = b if isinstance(b, dict) else {}
                return 200, svc.agent.cvv_mode(me, b.get("keep") is True, b.get("cvv"))
            if len(parts) == 1 and method == "DELETE":
                return 200, {"removed": svc.agent.remove(me)}
            return None
        if len(parts) == 1 and method == "GET":
            out = {"issuer": svc.agent_settings.issuer, "method": svc.agent.method(me),
                   "publishable_key": svc.agent_settings.publishable_key or None}
            if wise:
                link = svc.agent.link(me)
                out["link"] = {k: link.get(k) for k in ("state", "required_by", "updated_at")} if link else None
            return 200, out
        if wise and len(parts) == 2 and parts[1] == "balance" and method == "GET":
            return 200, {"balances": svc.agent.balance(me)}
        if wise and len(parts) == 2 and parts[1] == "link" and method == "POST":
            b = body()
            person = b.get("person") if isinstance(b, dict) else None
            link = svc.agent.link_start(me, person if isinstance(person, dict) else {})
            return 200, {"link": {k: link.get(k) for k in ("state", "required_by", "updated_at")} if link else None}
        if wise and len(parts) == 3 and parts[1:] == ["link", "url"] and method == "POST":
            if not svc.agent_settings.link_return_url:
                raise PaymentError(409, "not_configured", "adresse de retour du KYC non configurée")
            return 200, svc.agent.link_url(me, svc.agent_settings.link_return_url)
        if len(parts) == 2 and parts[1] == "challenge" and method == "POST":
            b = body()
            pm = b.get("payment_method") if isinstance(b, dict) else None
            if wise:  # le profil est celui que l'agent a ouvert pour CE compte, jamais celui que le web nommerait
                link = svc.agent.link(me)
                pm = link.get("profile_ref") if link else None
                if not isinstance(pm, str):
                    raise PaymentError(409, "no_link", "aucun compte émetteur ouvert")
            want = "wise:" if wise else "pm_"
            if not isinstance(pm, str) or not pm.startswith(want):
                raise PaymentError(400, "invalid", f"payment_method {want}… attendue")
            return 200, {"challenge": method_challenge(me, pm, int(svc.clock())), "payment_method": pm}
        if len(parts) == 1 and method == "PUT":
            b = body()
            if not isinstance(b, dict) or not isinstance(b.get("proof"), dict):
                raise PaymentError(400, "invalid", "payment_method et proof attendus")
            holder = b.get("holder_name") if isinstance(b.get("holder_name"), str) else None
            return 200, {"method": svc.agent.register(me, str(b.get("payment_method") or ""), b["proof"],
                                                      holder_name=holder)}
        if len(parts) == 1 and method == "DELETE":
            return 200, {"removed": svc.agent.remove(me)}
    except AgentUnavailable as e:
        raise agent_error(e) from None
    return None


__all__ = ["PREFIX", "configure", "configure_agent", "dispatch", "handle", "handle_agent_callback",
           "is_payments_path", "is_payments_post_path", "service"]
