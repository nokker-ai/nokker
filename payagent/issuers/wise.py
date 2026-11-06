"""Émetteur `wise` : carte virtuelle au nom de la personne, émise par Wise Platform (modèle Embedded : KYC et fonds
chez Wise, profil Wise de la personne). docs/PAY-AGENT.md §10.

Contrat lu sur docs.wise.com le 02.11.2026 (pages citées dans docs/PAY-AGENT.md §10.8), API `2026Q4` :
- jeton client credentials (`POST /oauth/token`, 12 h ; le modèle Embedded l'utilise pour toutes les requêtes) ;
- `GET  /spend/profiles/{profileId}/card-orders/availability`  → `cardPrograms[]` (`cardType` VIRTUAL_NON_UPGRADEABLE)
- `POST /spend/profiles/{profileId}/card-orders` (`X-idempotence-uuid`, `program`, `cardHolderName`, `lifetimeLimit`)
- `GET  /spend/profiles/{profileId}/card-orders/{cardOrderId}`  → `status`, `cardToken`
- `PATCH /spend/profiles/{profileId}/cards/{cardToken}/spend-limits` (`lifetime`/`transaction`.`value`)
- `PATCH /spend/profiles/{profileId}/cards/{cardToken}/spending-permissions` (`ECOM` seul)
- `PUT  /spend/profiles/{profileId}/cards/{cardToken}/status` (`BLOCKED` : irréversible)
- `GET  /spend/profiles/{profileId}/cards/{cardToken}`, `.../transactions`, `GET /profiles/{profileId}/balances`
- webhook `cards#3ds-challenge` (en-tête `X-Signature-SHA256`, RSA-SHA256 du corps, clés publiques fixes ci-dessous)
- `POST /spend/profiles/{profileId}/3dsecure/challenge-result` (`transactionReference`, `challengeStatus`) → 204
- données sensibles : `GET /twcard-data/v1/clientSideEncryption/fetchEncryptingKey`, puis
  `POST /twcard-data/v1/sensitive-card-data/details` (`x-tw-twcard-card-token`, `keyVersion`, `encryptedPayload`).
  Ces deux appels partent du COFFRE (`payagent/vault.py`), qui fabrique et déchiffre le JWE : l'agent ne voit rien.

AUCUN appel réel dans ce lot : pas de compte, pas de sandbox (tests : transport enregistré d'après la doc). Ce que la doc
laisse ouvert (jeton, SCA des profils CH sur le résultat 3DS et les données sensibles, en-tête du JWE…) est listé dans
docs/PAY-AGENT.md §10.9 ; le code lève `IssuerError` plutôt que de deviner.
"""
from __future__ import annotations

import base64
import json
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import quote, urlencode

from payagent.issuers import (Balance, CardTransaction, IssuedCard, IssuerError, IssuerEvent, ThreeDSChallenge)

API_VERSION = "2026Q4"
BASE_PROD = "https://api.wise.com"
BASE_SANDBOX = "https://api.wise-sandbox.com"
EVENT_3DS = "cards#3ds-challenge"
EVENT_CARD_STATUS = "cards#card-status-change"
EVENT_TRANSACTION = "cards#transaction-state-change"
EVENT_KYC = "kyc-reviews#state-change"
# Statuts d'une revue KYC (API) ; le webhook dit `state`, la ressource `status`. Pas de « refusé » : un refus revient
# à WAITING_CUSTOMER_INPUT avec des exigences à refaire.
KYC_OK = ("PASSED", "PASSED_WITH_REQUIREMENTS")
VIRTUAL = "VIRTUAL_NON_UPGRADEABLE"
# Statuts d'une commande où la carte existe (doc : « a card will now be available to be retrieved »)
ORDER_READY = ("CARD_DETAILS_CREATED", "PRODUCED", "COMPLETED")
ORDER_DEAD = ("CANCELLED", "RETURNED")
_ZERO_DECIMALS = {"JPY", "KRW", "ISK", "CLP", "VND"}  # même liste que nokker.payments.mandate

# Clés publiques de signature des webhooks (docs.wise.com/guides/developer/webhooks/event-handling, recopiées mot pour
# mot) : publiques, fixes, une par environnement.
WEBHOOK_KEY_PROD = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAvO8vXV+JksBzZAY6GhSO
XdoTCfhXaaiZ+qAbtaDBiu2AGkGVpmEygFmWP4Li9m5+Ni85BhVvZOodM9epgW3F
bA5Q1SexvAF1PPjX4JpMstak/QhAgl1qMSqEevL8cmUeTgcMuVWCJmlge9h7B1CS
D4rtlimGZozG39rUBDg6Qt2K+P4wBfLblL0k4C4YUdLnpGYEDIth+i8XsRpFlogx
CAFyH9+knYsDbR43UJ9shtc42Ybd40Afihj8KnYKXzchyQ42aC8aZ/h5hyZ28yVy
Oj3Vos0VdBIs/gAyJ/4yyQFCXYte64I7ssrlbGRaco4nKF3HmaNhxwyKyJafz19e
HwIDAQAB
-----END PUBLIC KEY-----
"""
WEBHOOK_KEY_SANDBOX = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAwpb91cEYuyJNQepZAVfP
ZIlPZfNUefH+n6w9SW3fykqKu938cR7WadQv87oF2VuT+fDt7kqeRziTmPSUhqPU
ys/V2Q1rlfJuXbE+Gga37t7zwd0egQ+KyOEHQOpcTwKmtZ81ieGHynAQzsn1We3j
wt760MsCPJ7GMT141ByQM+yW1Bx+4SG3IGjXWyqOWrcXsxAvIXkpUD/jK/L958Cg
nZEgz0BSEh0QxYLITnW1lLokSx/dTianWPFEhMC9BgijempgNXHNfcVirg1lPSyg
z7KqoKUN0oHqWLr2U1A+7kqrl6O2nx3CKs1bj1hToT1+p4kcMoHXA7kA+VBLUpEs
VwIDAQAB
-----END PUBLIC KEY-----
"""

# (méthode, url, en-têtes, corps) -> (statut, corps JSON ou None, en-têtes de réponse en minuscules)
Transport = Callable[[str, str, dict[str, str], bytes | None], tuple[int, Any, dict[str, str]]]


def urllib_transport(method: str, url: str, headers: dict[str, str], body: bytes | None) -> tuple[int, Any, dict[str, str]]:
    """Transport par défaut. mTLS (exigé par Wise pour OAuth) : contexte TLS du processus (à poser au déploiement)."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None), {k.lower(): v for k, v in r.headers.items()}
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            data = json.loads(raw) if raw else None
        except ValueError:
            data = None
        return e.code, data, {k.lower(): v for k, v in (e.headers or {}).items()}


def to_major(minor: int, currency: str) -> float:
    """Unités mineures → nombre décimal attendu par Wise (`amount`, `lifetimeLimit` : « number »)."""
    exp = 0 if currency in _ZERO_DECIMALS else 2
    return float(Decimal(minor) / (Decimal(10) ** exp))


def from_major(value: Any, currency: str) -> int:
    exp = 0 if currency in _ZERO_DECIMALS else 2
    return int((Decimal(str(value)) * (Decimal(10) ** exp)).to_integral_value())


def _profile(profile_ref: str) -> int:
    """`profile_ref` = « wise:<profileId> » (profil Wise de la personne, entier chez Wise)."""
    kind, _, pid = (profile_ref or "").partition(":")
    if kind != "wise" or not pid.isdigit() or len(pid) > 20:
        raise IssuerError(400, "profile", "profil Wise illisible")
    return int(pid)


class Wise:
    name = "wise"

    def __init__(self, client_id: str, client_secret: str, *, base: str = BASE_PROD,
                 transport: Transport | None = None, webhook_key_pem: str | None = None,
                 clock: Callable[[], float] = time.time, order_wait_s: float = 20.0,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._id, self._secret, self._base = client_id, client_secret, base.rstrip("/")
        self._t = transport or urllib_transport
        self._key_pem = webhook_key_pem or (WEBHOOK_KEY_SANDBOX if "sandbox" in self._base else WEBHOOK_KEY_PROD)
        self._clock, self._sleep, self._order_wait_s = clock, sleep, order_wait_s
        self._token: tuple[str, float] | None = None
        self._lock = threading.Lock()

    # ───────────────────────── transport ─────────────────────────

    def _access_token(self, *, renew: bool = False) -> str:
        with self._lock:
            if self._token and not renew and self._token[1] > self._clock() + 60:
                return self._token[0]
            basic = base64.b64encode(f"{self._id}:{self._secret}".encode()).decode()
            status, data, _ = self._t("POST", f"{self._base}/{API_VERSION}/oauth/token",
                                      {"Authorization": f"Basic {basic}",
                                       "Content-Type": "application/x-www-form-urlencoded"},
                                      urlencode({"grant_type": "client_credentials"}).encode())
            tok = (data or {}).get("access_token") if isinstance(data, dict) else None
            if status >= 400 or not isinstance(tok, str) or not tok:
                raise IssuerError(status or 502, "auth", "jeton Wise refusé")
            ttl = (data or {}).get("expires_in")
            self._token = (tok, self._clock() + (float(ttl) if isinstance(ttl, (int, float)) else 3600.0))
            return tok

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None, *,
              headers: dict[str, str] | None = None, versioned: bool = True, ok: tuple[int, ...] = (200,)) -> Any:
        url = f"{self._base}/{API_VERSION}{path}" if versioned else f"{self._base}{path}"
        raw = json.dumps(body).encode() if body is not None else None
        for attempt in (0, 1):
            h = {"Authorization": f"Bearer {self._access_token(renew=attempt == 1)}", "Accept": "application/json",
                 **(headers or {})}
            if raw is not None:
                h["Content-Type"] = "application/json"
            try:
                status, data, rh = self._t(method, url, h, raw)
            except OSError as e:
                raise IssuerError(0, "unreachable", f"Wise injoignable : {type(e).__name__}") from None
            if status == 401 and attempt == 0:  # doc : `invalid_token` → jeter, en refaire un, réessayer
                continue
            if status in ok:
                return data
            if status == 403 and rh.get("x-2fa-approval"):
                # SCA exigée (profils hors US/AU/NZ/SG/CA/MY) : pas de parcours SCA dans ce lot (§10.9)
                raise IssuerError(403, "sca_required", "Wise exige une authentification forte (SCA)")
            code = _error_code(data) or ("rate_limited" if status == 429 else "wise_error")
            raise IssuerError(status, code, f"Wise {status} ({code})")
        raise IssuerError(401, "auth", "jeton Wise refusé")

    # ───────────────────────── profil, carte ─────────────────────────

    def check_profile(self, profile_ref: str) -> dict[str, Any]:
        """Le profil peut commander une carte virtuelle (programme disponible) ; rend la devise par défaut."""
        prog = self._virtual_program(_profile(profile_ref))
        return {"issuer": self.name, "scheme": prog.get("scheme"), "default_currency": prog.get("defaultCurrency")}

    def _virtual_program(self, pid: int) -> dict[str, Any]:
        data = self._call("GET", f"/spend/profiles/{pid}/card-orders/availability") or {}
        progs = [p for p in (data.get("cardPrograms") or []) if isinstance(p, dict) and p.get("cardType") == VIRTUAL]
        if not progs:  # doc : aucun programme rendu = le profil ne peut plus commander de carte (quota de 3)
            raise IssuerError(409, "no_program", "ce profil Wise ne peut pas commander de carte virtuelle")
        return progs[0]

    def issue_card(self, *, profile_ref: str, currency: str, limit_minor: int, idempotency_key: str,
                   vault: Any, holder_name: str = "") -> IssuedCard:
        pid = _profile(profile_ref)
        if not holder_name:
            raise IssuerError(400, "holder", "nom du titulaire manquant")
        prog = self._virtual_program(pid)
        # Idempotence Wise : un UUID stable par mandat (même mandat rejoué = même commande, pas une 2e carte).
        idem = str(uuid.uuid5(uuid.NAMESPACE_URL, "nokker-pay-agent:" + idempotency_key))
        order = self._call("POST", f"/spend/profiles/{pid}/card-orders",
                           # lifetimeLimit 0 : « a card cannot be used until the lifetime limit is updated » — la
                           # carte reste inutilisable tant que le plafond en devise du mandat n'est pas posé ci-dessous.
                           {"program": prog["name"], "cardHolderName": holder_name[:100], "lifetimeLimit": 0},
                           headers={"X-idempotence-uuid": idem}) or {}
        token = self._wait_card(pid, order)
        amount = {"amount": to_major(limit_minor, currency), "currency": currency}
        try:
            self._call("PATCH", f"/spend/profiles/{pid}/cards/{quote(token, safe='')}/spend-limits",
                       {"lifetime": {"value": amount}, "transaction": {"value": amount}})
            self._call("PATCH", f"/spend/profiles/{pid}/cards/{quote(token, safe='')}/spending-permissions",
                       {"permissions": [{"type": "ECOM", "isEnabled": True},
                                        {"type": "ATM_WITHDRAWAL", "isEnabled": False},
                                        {"type": "MOBILE_WALLETS", "isEnabled": False}]})
            vcard = self._deliver_to_vault(token, vault)
        except IssuerError:
            self._safe_block(pid, token)  # plafond ou coffre en échec : la carte ne doit jamais rester utilisable
            raise
        card = self._call("GET", f"/spend/profiles/{pid}/cards/{quote(token, safe='')}") or {}
        return IssuedCard(card_ref=token, status=str((card.get("status") or {}).get("value") or "ACTIVE"),
                          last4=card.get("lastFourDigits"), raw_status=card.get("status") or {}, vault_card=vcard)

    def _wait_card(self, pid: int, order: dict[str, Any]) -> str:
        deadline = self._clock() + self._order_wait_s
        while True:
            token, status = order.get("cardToken"), str(order.get("status") or "")
            if isinstance(token, str) and token and status in ORDER_READY:
                return token
            if status in ORDER_DEAD:
                raise IssuerError(409, "order", f"commande de carte {status}")
            if self._clock() >= deadline or not isinstance(order.get("id"), int):
                raise IssuerError(504, "order_pending", "carte pas encore créée par Wise")
            self._sleep(1.0)
            order = self._call("GET", f"/spend/profiles/{pid}/card-orders/{order['id']}") or {}

    def _deliver_to_vault(self, token: str, vault: Any) -> Any:
        """Le PAN va de Wise au coffre, jamais par ici. La doc décrit la réponse de `sensitive-card-data/details`
        tantôt en JWE, tantôt en JSON clair (`pan`, `cvv2`) : les DEUX appels partent donc du coffre lui-même (son
        proxy sortant), qui fabrique le JWE (clé AES à lui, clé RSA de Wise), déchiffre la réponse et rend des jetons.
        L'agent ne lui passe que les URL et les en-têtes (jeton Wise, `x-tw-twcard-card-token`)."""
        try:
            return vault.capture_card(issuer=self.name, card_ref=token,
                                      key_url=f"{self._base}/twcard-data/v1/clientSideEncryption/fetchEncryptingKey",
                                      details_url=f"{self._base}/twcard-data/v1/sensitive-card-data/details",
                                      headers={"Authorization": f"Bearer {self._access_token()}",
                                               "x-tw-twcard-card-token": token})
        except Exception as e:  # coffre en panne ou pas branché : la carte sera bloquée par l'appelant
            raise IssuerError(502, "vault", f"coffre indisponible ({getattr(e, 'code', type(e).__name__)})") from None

    def _safe_block(self, pid: int, token: str) -> None:
        try:
            self._call("PUT", f"/spend/profiles/{pid}/cards/{quote(token, safe='')}/status", {"status": "BLOCKED"})
        except IssuerError:
            pass

    def block_card(self, *, profile_ref: str, card_ref: str) -> None:
        self._call("PUT", f"/spend/profiles/{_profile(profile_ref)}/cards/{quote(card_ref, safe='')}/status",
                   {"status": "BLOCKED"})

    def card_status(self, *, profile_ref: str, card_ref: str) -> IssuedCard:
        card = self._call("GET", f"/spend/profiles/{_profile(profile_ref)}/cards/{quote(card_ref, safe='')}") or {}
        return IssuedCard(card_ref=card_ref, status=str((card.get("status") or {}).get("value") or ""),
                          last4=card.get("lastFourDigits"), raw_status=card.get("status") or {})

    def balances(self, *, profile_ref: str) -> list[Balance]:
        data = self._call("GET", f"/profiles/{_profile(profile_ref)}/balances?types=STANDARD") or []
        out = []
        for b in data if isinstance(data, list) else []:
            amt = (b or {}).get("amount") or {}
            if isinstance(amt.get("currency"), str) and isinstance(amt.get("value"), (int, float)):
                out.append(Balance(currency=amt["currency"], amount_minor=from_major(amt["value"], amt["currency"])))
        return out

    def transactions(self, *, profile_ref: str, card_ref: str, days: int = 89) -> list[CardTransaction]:
        now = datetime.fromtimestamp(self._clock(), tz=UTC)
        q = urlencode({"fromCreationTime": (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                       "toCreationTime": now.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "pageSize": 100})
        data = self._call("GET", f"/spend/profiles/{_profile(profile_ref)}/cards/{quote(card_ref, safe='')}"
                                 f"/transactions?{q}") or {}
        out = []
        for t in data.get("transactions") or []:
            amt = (t or {}).get("transactionAmount") or {}
            cur = str(amt.get("currency") or "")
            if not cur or not isinstance(amt.get("amount"), (int, float)):
                continue
            out.append(CardTransaction(ref=str(t.get("id")), status=str(t.get("state") or ""),
                                       amount_minor=from_major(amt["amount"], cur), currency=cur,
                                       merchant=str(((t.get("merchant") or {}).get("name")) or ""),
                                       created_at=str(t.get("creationTime") or "")))
        return out

    # ───────────────────────── profil et KYC (modèle Embedded, KYC fait par Wise) ─────────────────────────

    def create_profile(self, person: dict[str, Any], *, external_id: str) -> str:
        """`POST /profiles/personal-profile` ; rend « wise:<id> ». `person` : champs déjà validés par l'agent."""
        body = {"firstName": person["first_name"], "lastName": person["last_name"],
                "dateOfBirth": person["date_of_birth"],
                "address": {"addressFirstLine": person["address_line"], "city": person["city"],
                            "countryIso3Code": person["country_iso3"],
                            **({"postCode": person["post_code"]} if person.get("post_code") else {})},
                "contactDetails": {"email": person["email"], "phoneNumber": person["phone"]},
                "externalCustomerId": external_id}
        data = self._call("POST", "/profiles/personal-profile", body, ok=(200, 201)) or {}
        if not isinstance(data.get("id"), int):
            raise IssuerError(502, "profile", "profil Wise sans identifiant")
        return f"wise:{data['id']}"

    def start_kyc(self, profile_ref: str) -> None:
        """Revue KYC « carte » (`POST /profiles/{id}/kyc-reviews`, 202 sans corps documenté)."""
        self._call("POST", f"/profiles/{_profile(profile_ref)}/kyc-reviews", {"type": "CARD"}, ok=(200, 201, 202))

    def kyc_reviews(self, profile_ref: str) -> list[dict[str, Any]]:
        data = self._call("GET", f"/profiles/{_profile(profile_ref)}/kyc-reviews?pageNumber=1&pageSize=100") or []
        items = data if isinstance(data, list) else (data.get("content") or data.get("items") or [])
        return [r for r in items if isinstance(r, dict) and isinstance(r.get("id"), str)]

    def kyc_status(self, profile_ref: str, review_id: str) -> str:
        data = self._call("GET", f"/profiles/{_profile(profile_ref)}/kyc-reviews/{quote(review_id, safe='')}") or {}
        return str(data.get("status") or "")

    def kyc_link(self, profile_ref: str, review_id: str, redirect_url: str) -> tuple[str, str, str]:
        """Lien KYC hébergé par Wise (5 min, valable une fois ; `expiresAt` fait foi). Rend (url, expire, statut).
        Wise ne le rend que si la revue attend la personne (WAITING_CUSTOMER_INPUT)."""
        data = self._call("PATCH", f"/profiles/{_profile(profile_ref)}/kyc-reviews/{quote(review_id, safe='')}",
                          {"redirectUrl": redirect_url}) or {}
        link = data.get("link") if isinstance(data.get("link"), dict) else {}
        url = link.get("value")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise IssuerError(409, "kyc_no_link", f"pas de lien KYC (revue {data.get('status')})")
        return url, str(link.get("expiresAt") or ""), str(data.get("status") or "")

    # ───────────────────────── webhooks, 3-D Secure ─────────────────────────

    def verify_signature(self, payload: bytes, signature_b64: str) -> None:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding

        try:
            sig = base64.b64decode(signature_b64 or "", validate=True)
            key = serialization.load_pem_public_key(self._key_pem.encode())
            key.verify(sig, payload, padding.PKCS1v15(), hashes.SHA256())  # type: ignore[union-attr]
        except (InvalidSignature, ValueError, TypeError):
            raise ValueError("signature du webhook Wise invalide") from None

    def parse_webhook(self, payload: bytes, headers: Any) -> IssuerEvent:
        self.verify_signature(payload, headers.get("X-Signature-SHA256") or "")
        try:
            ev = json.loads(payload)
        except ValueError:
            raise ValueError("webhook Wise illisible") from None
        if not isinstance(ev, dict):
            raise ValueError("webhook Wise illisible")
        etype = str(ev.get("event_type") or "")
        # Pas d'identifiant d'événement dans le corps : `X-Delivery-Id` (redélivrances jusqu'à 25 fois)
        eid = str(headers.get("X-Delivery-Id") or "")
        data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
        res = data.get("resource") if isinstance(data.get("resource"), dict) else {}
        card = str(res.get("card_token") or "") or None
        if etype == EVENT_3DS:
            return IssuerEvent(kind="3ds_challenge", event_id=eid, card_ref=card,
                               challenge=_challenge(data, res, self._clock()), data={"event_type": etype})
        if etype == EVENT_CARD_STATUS:
            return IssuerEvent(kind="card_status", event_id=eid, card_ref=card,
                               data={"card_status": data.get("card_status")})
        if etype == EVENT_KYC:
            return IssuerEvent(kind="kyc", event_id=eid, card_ref=None,
                               data={"review_id": res.get("id"), "state": res.get("state"),
                                     "profile_ref": f"wise:{res['profileId']}" if isinstance(res.get("profileId"), int)
                                     else None, "required_by": res.get("requiredBy")})
        if etype == EVENT_TRANSACTION:
            return IssuerEvent(kind="transaction", event_id=eid, card_ref=card,
                               data={k: data.get(k) for k in ("transaction_id", "transaction_type",
                                                               "transaction_step_type", "transaction_state",
                                                               "decline_reason")})
        return IssuerEvent(kind="ignored", event_id=eid, data={"event_type": etype})

    def challenge_result(self, *, challenge: ThreeDSChallenge, approved: bool) -> None:
        self._call("POST", f"/spend/profiles/{_profile(challenge.profile_ref)}/3dsecure/challenge-result",
                   {"transactionReference": challenge.challenge_ref,
                    "challengeStatus": "APPROVED" if approved else "REJECTED"}, ok=(200, 204))


def _challenge(data: dict[str, Any], res: dict[str, Any], now: float) -> ThreeDSChallenge:
    """Champs du webhook `cards#3ds-challenge` (doc, schéma 4.0.0) ; `ValueError` s'il en manque un."""
    tx = data.get("transaction") if isinstance(data.get("transaction"), dict) else {}
    money = tx.get("money_value") if isinstance(tx.get("money_value"), dict) else {}
    merchant = tx.get("merchant") if isinstance(tx.get("merchant"), dict) else {}
    ref, cur, value = tx.get("reference"), money.get("currency"), money.get("value")
    ttl = data.get("challenge_expires_after")
    if not (isinstance(ref, (str, int)) and str(ref) and isinstance(cur, str) and len(cur) == 3
            and isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0
            and isinstance(res.get("profile_id"), int) and res.get("card_token")
            and isinstance(ttl, int) and 0 < ttl <= 3600):
        raise ValueError("défi 3-D Secure Wise incomplet")
    # Point de départ du délai non précisé par la doc (`occurred_at` ou `sent_at`) : on compte depuis la réception,
    # moins une marge, pour ne jamais répondre après l'échéance réelle.
    return ThreeDSChallenge(challenge_ref=str(ref), card_ref=str(res["card_token"]),
                            profile_ref=f"wise:{res['profile_id']}", amount_minor=from_major(value, cur.upper()),
                            currency=cur.upper(), merchant_name=str(merchant.get("name") or ""),
                            merchant_url=merchant.get("url") if isinstance(merchant.get("url"), str) else None,
                            merchant_country=merchant.get("country") if isinstance(merchant.get("country"), str)
                            else None, expires_at=now + ttl - 15, raw={"channel": tx.get("channel")})


def _error_code(data: Any) -> str | None:
    if isinstance(data, dict):
        if isinstance(data.get("code"), str):
            return data["code"]
        errs = data.get("errors")
        if isinstance(errs, list) and errs and isinstance(errs[0], dict) and isinstance(errs[0].get("code"), str):
            return errs[0]["code"]
        if isinstance(data.get("error"), str):
            return data["error"]
    return None


__all__ = ["API_VERSION", "BASE_PROD", "BASE_SANDBOX", "EVENT_3DS", "Transport", "Wise", "from_major", "to_major",
           "urllib_transport"]
