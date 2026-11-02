"""Mandats de dépense signés par la passkey (docs/PAYMENTS.md §3) — construction et vérification, pures.

Un mandat dit : QUI (le membre du foyer, et qui approuve), QUOI (le panier exact), COMBIEN au plus (montant et devise),
CHEZ QUI (le marchand), JUSQU'À QUAND (validité) et l'EMPREINTE du panier. Il est construit par le noyau quand la carte
« à payer » est posée, son empreinte entre dans le défi de la carte (`Challenge.mandate_sha256`), et la passkey de la
personne signe ce défi (WebAuthn, docs/APPROVALS.md §3.4) : la signature couvre donc le mandat entier.

Format. AP2 v0.2 (FIDO Alliance, schémas `code/sdk/schemas/ap2/*.json` du dépôt google-agentic-commerce/AP2, commit
e1ea56d du 29.04.2026) transporte ses mandats en SD-JWT signés par une clé ES256 détenue par l'utilisateur. Une passkey
ne signe pas un JWS arbitraire : elle signe `authenticatorData ‖ SHA-256(clientDataJSON)`. Le mandat Nokker est donc un
**JSON canonique signé** (clés triées, sans espaces, UTF-8), dont le bloc `ap2` reprend mot pour mot les champs d'un
« Open Payment Mandate » AP2 (`vct`, `constraints` de type `payment.amount_range`, `payment.allowed_payees`,
`payment.reference`, `payee`, `payment_amount` en unités mineures ISO 4217, `iat`, `exp`) — ce n'est PAS un SD-JWT
conforme, il pourra le devenir quand un émetteur ou un PSP le lira (veille paiements, niveau 3.4).

Enveloppe scellée (ce que `nokker mandate verify` lit) :
    {"format": "nokker.mandate.v1", "mandate": {...}, "mandate_sha256": "…",
     "proof": {"kind": "webauthn", "challenge": "<défi exact>", "decision": "approve", "device_id", "public_key_spki",
               "client_data_json", "authenticator_data", "signature", "origin", "rp_id"},
     "signed_by": "us_…", "signed_at": "…Z"}
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit

FORMAT = "nokker.mandate.v1"
AP2_VCT = "mandate.payment.open.1"  # vct d'un Open Payment Mandate AP2 v0.2
SOURCES = ("browser", "qr_bill", "pain001")
# Devises à 0 décimale (ISO 4217) parmi celles qu'un foyer CH/UE croise ; toutes les autres : 2.
_ZERO_DECIMALS = {"JPY", "KRW", "ISK", "CLP", "VND"}
_CURRENCY = re.compile(r"^[A-Z]{3}$")
MAX_ITEMS = 50
MAX_TEXT = 300


class MandateError(ValueError):
    """Arguments d'une carte « à payer » illisibles, ou mandat invalide."""


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def to_minor(amount: Any, currency: str) -> int:
    """Montant décimal (« 129.90 », 129.9, « 129,90 ») → unités mineures entières ISO 4217. Lève `MandateError`."""
    if isinstance(amount, bool):
        raise MandateError("montant illisible")
    text = str(amount).strip().replace("'", "").replace(" ", "").replace(",", ".")
    try:
        value = Decimal(text)
    except InvalidOperation as e:
        raise MandateError(f"montant illisible : {amount!r}") from e
    if not value.is_finite() or value <= 0:
        raise MandateError("le montant doit être positif")
    exp = 0 if currency in _ZERO_DECIMALS else 2
    minor = int((value * (10 ** exp)).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    if minor <= 0 or minor > 10 ** 12:
        raise MandateError("montant hors bornes")
    return minor


def format_minor(minor: int, currency: str) -> str:
    exp = 0 if currency in _ZERO_DECIMALS else 2
    if exp == 0:
        return f"{minor} {currency}"
    return f"{minor // 100}.{minor % 100:02d} {currency}"


@dataclass(frozen=True)
class Cart:
    """Arguments d'un appel `pay_request`, validés. `arguments` reste l'objet EXACT de l'appel (son empreinte est
    `args_sha256` de la carte) ; les autres champs en sont dérivés."""

    arguments: dict[str, Any]
    merchant: str
    url: str | None
    amount_minor: int
    currency: str
    items: list[dict[str, Any]]
    source: str
    reference: str | None

    @property
    def sha256(self) -> str:
        return sha256_hex(canonical_json(self.arguments))


def _text(value: Any, what: str, *, required: bool = True) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise MandateError(f"{what} manquant")
        return None
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise MandateError(f"{what} illisible")
    text = str(value).strip()
    if len(text) > MAX_TEXT:
        raise MandateError(f"{what} trop long")
    return text


def parse_cart(arguments: dict[str, Any]) -> Cart:
    """Valide les arguments d'une carte « à payer ». Lève `MandateError` avec une raison en français."""
    if not isinstance(arguments, dict):
        raise MandateError("arguments illisibles")
    currency = (_text(arguments.get("currency"), "devise") or "").upper()
    if not _CURRENCY.match(currency):
        raise MandateError("devise : code ISO 4217 à trois lettres attendu (CHF, EUR…)")
    merchant = _text(arguments.get("merchant"), "marchand")
    source = str(arguments.get("source") or "browser")
    if source not in SOURCES:
        raise MandateError(f"source inconnue : {source!r} (browser, qr_bill ou pain001)")
    url = _text(arguments.get("url"), "lien", required=False)
    if url is not None:
        parts = urlsplit(url)
        if parts.scheme not in ("https", "http") or not parts.hostname:
            raise MandateError("lien : adresse http(s) attendue")
    elif source == "browser":
        raise MandateError("lien du panier manquant")
    items_in = arguments.get("items")
    if not isinstance(items_in, list) or not items_in:
        raise MandateError("articles manquants")
    if len(items_in) > MAX_ITEMS:
        raise MandateError("trop d'articles")
    items = []
    for it in items_in:
        if isinstance(it, str):
            it = {"name": it}
        if not isinstance(it, dict):
            raise MandateError("article illisible")
        qty = it.get("quantity", 1)
        if isinstance(qty, bool) or not isinstance(qty, (int, float)) or int(qty) != qty or not 1 <= qty <= 10_000:
            raise MandateError("quantité illisible")
        line = {"name": _text(it.get("name"), "nom d'article"), "quantity": int(qty)}
        if it.get("unit_price") not in (None, ""):
            line["unit_price_minor"] = to_minor(it.get("unit_price"), currency)
        items.append(line)
    return Cart(arguments=dict(arguments), merchant=merchant or "", url=url,
                amount_minor=to_minor(arguments.get("amount"), currency), currency=currency, items=items,
                source=source, reference=_text(arguments.get("reference"), "référence", required=False))


def merchant_id(cart: Cart) -> str:
    """Identifiant AP2 du marchand : le nom d'hôte du lien, sinon le nom en minuscules."""
    host = urlsplit(cart.url).hostname if cart.url else None
    return (host or re.sub(r"\s+", "-", cart.merchant.lower()))[:120]


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_mandate_id() -> str:
    return "md_" + secrets.token_hex(8)


def principal_handle(mandate_id: str, account: str | None) -> str | None:
    """Identifiant opaque d'un compte DANS un mandat (propre à ce mandat) : le mandat et son enveloppe, rendus par les
    routes et vérifiables hors ligne, ne portent ni le locataire ni l'identifiant de compte d'un membre ; le noyau
    garde le lien (enregistrement du mandat)."""
    if not account:
        return None
    return "pr_" + hashlib.sha256(f"{mandate_id}\x00{account}".encode()).hexdigest()[:16]


def build_mandate(cart: Cart, *, mandate_id: str, approval_id: str, args_sha256: str,
                  account: str | None, approver: str | None, created_at: float, valid_until: float) -> dict[str, Any]:
    """Le mandat (non signé). Tout ce qu'il contient est couvert par la signature de la passkey. Ni locataire ni
    identifiant de compte : `principal` porte des identifiants opaques (`principal_handle`)."""
    iat, exp = int(created_at), int(valid_until)
    payee = {"id": merchant_id(cart), "name": cart.merchant, **({"website": cart.url} if cart.url else {})}
    return {
        "format": FORMAT,
        "mandate_id": mandate_id,
        "approval_id": approval_id,
        # qui : le membre pour qui l'agent prépare, et le compte qui doit approuver (lui, ou son représentant), opaques
        "principal": {"account": principal_handle(mandate_id, account),
                      "approver": principal_handle(mandate_id, approver or account),
                      "by_representative": bool(approver and account and approver != account)},
        "agent": "nokker",
        # quoi : le panier EXACT de la carte (arguments de l'appel) et son empreinte (= args_sha256 de la carte)
        "cart": cart.arguments,
        "cart_sha256": args_sha256,
        "merchant": payee,
        "max_amount": {"amount": cart.amount_minor, "currency": cart.currency},
        "source": cart.source,
        "valid_from": _iso(iat),
        "valid_until": _iso(exp),
        "human_pays": True,  # l'agent prépare, l'humain paie : aucun moyen de paiement dans le mandat
        "ap2": {
            "vct": AP2_VCT,
            "constraints": [
                {"type": "payment.amount_range", "currency": cart.currency, "max": cart.amount_minor},
                {"type": "payment.allowed_payees", "allowed": [payee]},
                {"type": "payment.reference", "conditional_transaction_id": args_sha256},
            ],
            "payee": payee,
            "payment_amount": {"amount": cart.amount_minor, "currency": cart.currency},
            "iat": iat,
            "exp": exp,
        },
    }


def mandate_sha256(mandate: dict[str, Any]) -> str:
    return sha256_hex(canonical_json(mandate))


@dataclass
class Verification:
    valid: bool
    expired: bool
    checks: dict[str, bool] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {"valid": self.valid, "expired": self.expired, "checks": dict(self.checks), "errors": list(self.errors)}


def verify_envelope(env: Any, *, now: float | None = None, trusted_spki: str | None = None,
                    expected_origin: str | None = None) -> Verification:
    """Vérifie une enveloppe scellée SANS rien d'autre qu'elle (hors ligne). `trusted_spki` : la clé publique enrôlée
    de l'appareil, si on la connaît (la route la prend en base) — sinon celle de l'enveloppe, et le contrôle
    `device_key_trusted` reste faux. `expected_origin` : l'origine WebAuthn attendue (sinon celle de l'enveloppe).
    Un mandat expiré reste VALIDE (la signature tient) : `expired` le dit à part."""
    from nokker.approvals.crypto import SignatureError, WebAuthnError, verify_webauthn_assertion

    out = Verification(valid=False, expired=False)
    c = out.checks

    def fail(name: str, why: str) -> Verification:
        c[name] = False
        out.errors.append(why)
        return out

    if not isinstance(env, dict) or env.get("format") != FORMAT or not isinstance(env.get("mandate"), dict):
        return fail("format", f"enveloppe illisible (format {FORMAT} attendu)")
    c["format"] = True
    m = env["mandate"]
    digest = mandate_sha256(m)
    if env.get("mandate_sha256") != digest:
        return fail("mandate_sha256", "empreinte du mandat fausse : le mandat a été modifié")
    c["mandate_sha256"] = True
    try:
        cart = parse_cart(m.get("cart") or {})
    except MandateError as e:
        return fail("cart", f"panier illisible : {e}")
    if sha256_hex(canonical_json(m.get("cart"))) != m.get("cart_sha256"):
        return fail("cart_sha256", "empreinte du panier fausse : le panier a été modifié")
    c["cart_sha256"] = True
    expected_max = {"amount": cart.amount_minor, "currency": cart.currency}
    ap2 = m.get("ap2") or {}
    cons = {x.get("type"): x for x in ap2.get("constraints") or [] if isinstance(x, dict)}
    coherent = (m.get("max_amount") == expected_max and ap2.get("payment_amount") == expected_max
                and (cons.get("payment.amount_range") or {}).get("max") == cart.amount_minor
                and (cons.get("payment.amount_range") or {}).get("currency") == cart.currency
                and (cons.get("payment.reference") or {}).get("conditional_transaction_id") == m.get("cart_sha256")
                and (m.get("merchant") or {}).get("name") == cart.merchant
                and ap2.get("payee") == m.get("merchant") and ap2.get("vct") == AP2_VCT)
    if not coherent:
        return fail("coherence", "montant, devise, marchand ou référence ne correspondent pas au panier")
    c["coherence"] = True
    p = env.get("proof") or {}
    if p.get("kind") != "webauthn" or p.get("decision") != "approve":
        return fail("proof", "preuve absente ou pas une approbation par passkey")
    try:
        ch = json.loads(p.get("challenge") or "")
    except (TypeError, json.JSONDecodeError):
        return fail("challenge", "défi illisible")
    if not (isinstance(ch, dict) and ch.get("aud") == "nokker-approval" and ch.get("approval_id") == m.get("approval_id")
            and ch.get("args_sha256") == m.get("cart_sha256") and ch.get("mandate_sha256") == digest):
        return fail("challenge", "le défi signé ne porte pas sur ce mandat")
    c["challenge"] = True
    spki = trusted_spki or p.get("public_key_spki") or ""
    origin = expected_origin or p.get("origin") or ""
    rp_id = p.get("rp_id") or (urlsplit(origin).hostname or "")
    try:
        verify_webauthn_assertion(spki, p.get("client_data_json") or "", p.get("authenticator_data") or "",
                                  p.get("signature") or "",
                                  expected_challenge=hashlib.sha256(f"{p['challenge']}.approve".encode()).digest(),
                                  expected_origin=origin, expected_rp_id=rp_id, require_uv=True)
    except (WebAuthnError, SignatureError, ValueError) as e:
        return fail("signature", f"signature de la passkey invalide : {e}")
    c["signature"] = True
    c["device_key_trusted"] = bool(trusted_spki) and trusted_spki == p.get("public_key_spki")
    out.valid = True
    out.expired = (now if now is not None else time.time()) > float(ap2.get("exp") or 0)
    return out


__all__ = ["AP2_VCT", "FORMAT", "SOURCES", "Cart", "MandateError", "Verification", "build_mandate", "canonical_json",
           "principal_handle",
           "format_minor", "mandate_sha256", "merchant_id", "new_mandate_id", "parse_cart", "sha256_hex", "to_minor",
           "verify_envelope"]
