"""Service des paiements « l'agent prépare, l'humain paie » (docs/PAYMENTS.md), drapeau `payments` (éteint par défaut).

- **Budgets** (§4) : plafond par achat, par jour et par mois d'un membre, posés par le payeur du foyer (ou par le titulaire
  d'un compte seul). Le noyau les vérifie AVANT de poser la carte : un budget dépassé est un refus, pas une carte.
  Profils mineurs : refusés, sauf si le payeur l'a permis explicitement (`minor_allowed`) — la carte va alors à SON
  représentant (`household:guardian`). Un membre sans budget posé ne dépense pas ; le payeur a le budget par défaut
  de la configuration tant qu'il n'en pose pas.
- **Mandats** (§3) : construit quand la carte est posée (`on_request`), son empreinte entre dans le défi de la carte
  (`binding`), scellé par la passkey à l'approbation (`check_and_seal`), refusé avec la carte (`on_rejected`).
- **Événements** (§5) : `payment.to_pay`, `payment.mandate_signed`, `payment.rejected`, `payment.refused`,
  `payment.budget_changed`, publiés dans le locataire concerné.

Ce module ne connaît ni HTTP ni le noyau ; `gates.py` (portes), `tools.py` (outil `pay_request`), `http.py` (routes)
et `nokker/approvals/base.py` (crochet `payment_mandates`) l'appellent.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from nokker.payments.mandate import (
    FORMAT,
    Cart,
    MandateError,
    build_mandate,
    format_minor,
    mandate_sha256,
    new_mandate_id,
    parse_cart,
    principal_handle,
    to_minor,
    verify_envelope,
)
from nokker.payments.store import Budget, MandateRecord
from nokker.tenancy.context import current_tenant_or_none, tenant_of_thread, tenant_scope

logger = logging.getLogger("nokker.payments")

PAY_TOOL = "pay_request"
PAY_RULE = "payments:to_pay"
GUARDIAN_RULE = "household:guardian"  # = nokker.household.model.GUARDIAN_RULE


class PaymentError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


@dataclass(frozen=True)
class Settings:
    mandate_ttl_s: float = 24 * 3600.0
    timezone: str = "Europe/Zurich"
    # Budget du payeur (ou d'un compte seul) tant qu'il n'en pose pas ; None = aucun budget par défaut.
    default_budget: dict[str, Any] | None = None
    # Preuves acceptées pour sceller un mandat : `webauthn` (passkey avec UV). `signature+telegram` n'est PAS une
    # passkey : il n'est pas accepté (le mandat ne serait pas vérifiable hors ligne par `nokker mandate verify`).
    accept_proofs: tuple[str, ...] = ("webauthn",)
    handoff: bool = True  # passage de main dans le navigateur après l'approbation (source `browser`)

    @classmethod
    def from_config(cls, raw: dict[str, Any] | None) -> Settings:
        raw = dict(raw or {})
        default = raw.get("default_budget", {"currency": "CHF", "per_purchase": 200, "per_day": 300,
                                              "per_month": 1000})
        return cls(mandate_ttl_s=float(raw.get("mandate_ttl_s", 24 * 3600)), timezone=str(raw.get("timezone",
                                                                                                    "Europe/Zurich")),
                   default_budget=dict(default) if default else None,
                   accept_proofs=tuple(x for x in (raw.get("accept_proofs") or ["webauthn"]) if x == "webauthn")
                   or ("webauthn",),
                   handoff=bool(raw.get("handoff", True)))


@dataclass(frozen=True)
class Person:
    """Ce que le service doit savoir d'un compte pour décider d'une dépense."""

    account: str | None
    payer: str | None  # payeur de son foyer ; lui-même s'il est seul ; None : inconnu (opérateur interne)
    minor: bool = False
    guardian: str | None = None

    @property
    def is_payer(self) -> bool:
        return self.account is not None and self.payer == self.account


class SoloPeople:
    """Sans foyer : chaque compte est son propre payeur, personne n'est mineur."""

    def person(self, account: str | None) -> Person:
        return Person(account=account, payer=account)

    def members_of(self, payer: str) -> list[Person]:
        return [self.person(payer)]

    def tenant_of(self, account: str) -> str | None:
        return current_tenant_or_none()


@dataclass
class HouseholdPeople:
    """Avec le foyer (lot X23) : payeur, profils mineurs et représentant viennent de `Household`."""

    household: Any
    tenancy: Any

    def person(self, account: str | None) -> Person:
        if not account:
            return Person(account=account, payer=None)
        minor = self.household.minor_state(account) if hasattr(self.household, "minor_state") else None
        found = self.household.membership(account)
        if found is None:
            return Person(account=account, payer=account if minor is None else None, minor=minor is not None,
                          guardian=getattr(minor, "guardian", None))
        _, p, row = found
        return Person(account=account, payer=p.owner_account, minor=minor is not None or bool(row.minor),
                      guardian=getattr(minor, "guardian", None) or (p.owner_account if row.minor else None))

    def members_of(self, payer: str) -> list[Person]:
        found = self.household.membership(payer)
        if found is None:
            return [self.person(payer)]
        acc, p, row = found
        if not row.payer or p.owner_account != payer:
            return [self.person(payer)]
        rows = self.household.store.hh_members(acc, p.id)
        return [self.person(r.account_id) for r in rows if r.status == "active" or r.payer]

    def tenant_of(self, account: str) -> str | None:
        acc = self.tenancy.account(account) if self.tenancy is not None else None
        return getattr(acc, "tenant_id", None)


@dataclass
class Payments:
    store: Any  # MemoryPaymentStore | PgPaymentStore
    settings: Settings = field(default_factory=Settings)
    people: Any = field(default_factory=SoloPeople)
    publish: Callable[[str, dict], Any] | None = None  # EventBus.publish (filtré par `_tenant`)
    clock: Callable[[], float] = time.time
    # Instantanés navigateur par fil (posés par `CheckoutWatch`) : références `eN` → libellé, page de paiement vue.
    _refs: dict[tuple[str, str], dict[str, str]] = field(default_factory=dict, init=False, repr=False)
    _checkout: set[tuple[str, str]] = field(default_factory=set, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    # ───────────────────────── instantanés navigateur ─────────────────────────

    def _key(self, thread_id: str) -> tuple[str, str]:
        return (current_tenant_or_none() or tenant_of_thread(thread_id), thread_id)

    def note_page(self, thread_id: str, refs: dict[str, str], checkout: bool, *, reset: bool = False) -> None:
        key = self._key(thread_id)
        with self._lock:
            if reset:  # navigation : l'instantané d'avant ne vaut plus
                self._refs.pop(key, None)
                self._checkout.discard(key)
            if refs:
                self._refs[key] = dict(list(refs.items())[:2000])
            if checkout:
                self._checkout.add(key)
            elif refs:  # nouvelle page lue, sans paiement
                self._checkout.discard(key)

    def ref_label(self, thread_id: str, ref: str | None) -> str | None:
        if not ref:
            return None
        with self._lock:
            return self._refs.get(self._key(thread_id), {}).get(ref)

    def on_checkout(self, thread_id: str) -> bool:
        with self._lock:
            return self._key(thread_id) in self._checkout

    # ───────────────────────── événements ─────────────────────────

    def _emit(self, event: str, data: dict[str, Any]) -> None:
        if self.publish is None:
            return
        try:
            self.publish(event, data)
        except Exception:
            logger.exception("publication %s en échec", event)

    # ───────────────────────── budgets ─────────────────────────

    def _windows(self, now: float) -> tuple[float, float]:
        tz = ZoneInfo(self.settings.timezone)
        local = datetime.fromtimestamp(now, tz)
        day = local.replace(hour=0, minute=0, second=0, microsecond=0)
        return day.timestamp(), day.replace(day=1).timestamp()

    def _default_budget(self, account: str) -> Budget | None:
        d = self.settings.default_budget
        if not d:
            return None
        cur = str(d.get("currency", "CHF")).upper()
        return Budget(account=account, currency=cur, per_purchase_minor=to_minor(d["per_purchase"], cur),
                      per_day_minor=to_minor(d["per_day"], cur), per_month_minor=to_minor(d["per_month"], cur),
                      set_by=None)

    def effective_budget(self, person: Person) -> tuple[Budget | None, str]:
        """(budget en vigueur, source `set` | `default` | `none`). Lu dans le locataire COURANT (celui du membre)."""
        if person.account is None:
            return None, "none"
        b = self.store.get_budget(person.account)
        if b is not None:
            return b, "set"
        if person.is_payer and not person.minor:
            return self._default_budget(person.account), "default"
        return None, "none"

    def refusal(self, account: str | None, cart: Cart) -> tuple[str, str] | None:
        """(règle, raison) si la dépense est refusée AVANT la carte, sinon None. Raisons vues par le modèle."""
        person = self.people.person(account)
        budget, _ = self.effective_budget(person)
        amount = format_minor(cart.amount_minor, cart.currency)
        if person.minor and (budget is None or not budget.minor_allowed):
            return ("payments:minor", ("dépense refusée : un profil mineur ne dépense pas, sauf accord explicite du "
                                      "payeur du foyer dans ses réglages de budget."))
        if budget is None:
            return ("payments:budget", ("dépense refusée : aucun budget de dépense pour ce membre ; le payeur du foyer "
                                       "le règle."))
        if budget.currency != cart.currency:
            return ("payments:budget", (f"dépense refusée : le budget est en {budget.currency}, la dépense en "
                                       f"{cart.currency}."))
        if cart.amount_minor > budget.per_purchase_minor:
            return ("payments:budget", (f"dépense refusée : {amount} dépasse le plafond par achat "
                                       f"({format_minor(budget.per_purchase_minor, cart.currency)})."))
        now = self.clock()
        day0, month0 = self._windows(now)
        spent_day = self.store.spent(account, cart.currency, day0, now)
        if spent_day + cart.amount_minor > budget.per_day_minor:
            return ("payments:budget", (f"dépense refusée : {amount} dépasserait le plafond du jour "
                                       f"({format_minor(spent_day, cart.currency)} déjà engagés sur "
                                       f"{format_minor(budget.per_day_minor, cart.currency)})."))
        spent_month = self.store.spent(account, cart.currency, month0, now)
        if spent_month + cart.amount_minor > budget.per_month_minor:
            return ("payments:budget", (f"dépense refusée : {amount} dépasserait le plafond du mois "
                                       f"({format_minor(spent_month, cart.currency)} déjà engagés sur "
                                       f"{format_minor(budget.per_month_minor, cart.currency)})."))
        return None

    def note_refused(self, thread_id: str, account: str | None, rule: str, reason: str, cart: Cart | None) -> None:
        self._emit("payment.refused", {"thread_id": thread_id, "account": account, "rule": rule, "reason": reason,
                                       **({"merchant": cart.merchant, "amount_minor": cart.amount_minor,
                                           "currency": cart.currency} if cart is not None else {})})

    def approver_of(self, account: str | None) -> tuple[str | None, bool]:
        """(compte qui approuve, carte de représentant ?) : le membre, ou le représentant d'un profil mineur."""
        person = self.people.person(account)
        if person.minor:
            return person.guardian, True
        return account, False

    def _manageable(self, actor_account: str, member: str) -> Person:
        """Le payeur gère les budgets de son foyer ; un compte seul (ou le payeur) gère le sien. Lève sinon."""
        person = self.people.person(member)
        if person.payer == actor_account or (member == actor_account and person.is_payer):
            return person
        if member == actor_account:
            raise PaymentError(403, "payer_only", "seul le payeur du foyer règle les budgets de dépense")
        raise PaymentError(404, "not_found", "membre inconnu dans ce foyer")

    def _member_scope(self, member: str):
        tid = self.people.tenant_of(member)
        if not tid:
            raise PaymentError(404, "not_found", "membre inconnu dans ce foyer")
        return tenant_scope(tid)

    def _budget_line(self, person: Person) -> dict[str, Any]:
        budget, source = self.effective_budget(person)
        line: dict[str, Any] = {"account": person.account, "role": "minor" if person.minor else
                                ("payer" if person.is_payer else "member"), "source": source,
                                "budget": budget.to_json() if budget is not None else None}
        if budget is not None:
            now = self.clock()
            day0, month0 = self._windows(now)
            line["spent_today_minor"] = self.store.spent(person.account, budget.currency, day0, now)
            line["spent_month_minor"] = self.store.spent(person.account, budget.currency, month0, now)
        line["can_spend"] = budget is not None and (not person.minor or budget.minor_allowed)
        return line

    def budgets_view(self, actor_account: str) -> dict[str, Any]:
        me = self.people.person(actor_account)
        people = self.people.members_of(actor_account) if me.is_payer else [me]
        lines = []
        for p in people:
            try:
                with self._member_scope(p.account):
                    lines.append(self._budget_line(p))
            except PaymentError:
                continue
        return {"is_payer": me.is_payer, "timezone": self.settings.timezone, "members": lines}

    def set_budget(self, actor_account: str, member: str, body: dict[str, Any]) -> dict[str, Any]:
        person = self._manageable(actor_account, member)
        if not isinstance(body, dict):
            raise PaymentError(400, "invalid", "corps JSON attendu")
        cur = str(body.get("currency") or "").upper()
        try:
            caps = [to_minor(body.get(k), cur) for k in ("per_purchase", "per_day", "per_month")]
        except MandateError as e:
            raise PaymentError(400, "invalid", f"plafond illisible : {e}") from None
        if len(cur) != 3 or not cur.isalpha():
            raise PaymentError(400, "invalid", "devise : code ISO 4217 à trois lettres attendu")
        if not caps[0] <= caps[1] <= caps[2]:
            raise PaymentError(400, "invalid", "plafonds incohérents : par achat ≤ par jour ≤ par mois")
        minor_allowed = body.get("minor_allowed", False)
        if not isinstance(minor_allowed, bool):
            raise PaymentError(400, "invalid", "minor_allowed : booléen attendu")
        if minor_allowed and not person.minor:
            minor_allowed = False
        b = Budget(account=member, currency=cur, per_purchase_minor=caps[0], per_day_minor=caps[1],
                   per_month_minor=caps[2], minor_allowed=minor_allowed, set_by=actor_account, updated_at=self.clock())
        with self._member_scope(member):
            self.store.put_budget(b)
            line = self._budget_line(person)
            self._emit("payment.budget_changed", {"account": member, "by": actor_account, "line": line})
        if self.people.tenant_of(member) != current_tenant_or_none():  # le payeur l'entend aussi
            self._emit("payment.budget_changed", {"account": member, "by": actor_account, "line": line})
        return line

    def reset_budget(self, actor_account: str, member: str) -> dict[str, Any]:
        person = self._manageable(actor_account, member)
        with self._member_scope(member):
            self.store.delete_budget(member)
            line = self._budget_line(person)
            self._emit("payment.budget_changed", {"account": member, "by": actor_account, "line": line})
        return line

    # ───────────────────────── mandats (crochet des approbations) ─────────────────────────

    @staticmethod
    def covers(approval: Any) -> bool:
        return getattr(approval, "tool_name", None) == PAY_TOOL

    def on_request(self, approval: Any, account: str | None) -> MandateRecord | None:
        """Carte « à payer » posée : construit le mandat (non signé), l'enregistre, annonce `payment.to_pay`."""
        if not self.covers(approval):
            return None
        cart = parse_cart(approval.arguments)  # déjà validé par la porte ; une erreur ici est un bogue
        approver, _ = self.approver_of(account)
        now = float(approval.created_at)
        valid_until = min(now + self.settings.mandate_ttl_s, float(approval.expires_at))
        mid = new_mandate_id()
        mandate = build_mandate(cart, mandate_id=mid, approval_id=approval.id, args_sha256=approval.args_sha256,
                                account=account, approver=approver, created_at=now, valid_until=valid_until)
        rec = MandateRecord(id=mid, approval_id=approval.id, account=account, approver=approver, status="pending",
                            amount_minor=cart.amount_minor, currency=cart.currency, merchant=cart.merchant,
                            created_at=now, valid_until=valid_until, mandate=mandate,
                            mandate_sha256=mandate_sha256(mandate))
        self.store.put_mandate(rec)
        self._emit("payment.to_pay", self.card_json(rec, approval))
        return rec

    def binding(self, approval: Any) -> str | None:
        """Empreinte du mandat à lier au défi de cette carte, ou None (pas une carte « à payer »)."""
        if not self.covers(approval):
            return None
        rec = self.store.by_approval(approval.id)
        if rec is None:
            raise PaymentError(409, "no_mandate", "carte « à payer » sans mandat")
        return rec.mandate_sha256

    def check_and_seal(self, approval: Any, proof: Any, device: Any, decided_by: str, decided_via: str, *,
                       origin: str | None, rp_id: str | None) -> MandateRecord:
        """Approbation d'une carte « à payer » : la preuve doit être une passkey (WebAuthn, UV) sur un défi qui porte
        l'empreinte du mandat. Scelle le mandat (enveloppe vérifiable hors ligne). Lève `ValueError` sinon (l'appelant
        la traduit en `ProofInvalid`)."""
        import json

        rec = self.store.by_approval(approval.id)
        if rec is None:
            raise ValueError("carte « à payer » sans mandat")
        if decided_via not in self.settings.accept_proofs:
            raise ValueError("mandat de dépense : approbation par passkey (WebAuthn) exigée")
        try:
            ch = json.loads(proof.challenge or "")
        except (TypeError, ValueError):
            raise ValueError("défi illisible") from None
        if not isinstance(ch, dict) or ch.get("mandate_sha256") != rec.mandate_sha256:
            raise ValueError("le défi signé ne porte pas sur le mandat de cette carte")
        if mandate_sha256(rec.mandate) != rec.mandate_sha256:
            raise ValueError("mandat modifié depuis la demande")
        if rec.approver and decided_by != rec.approver:
            raise ValueError("ce mandat s'approuve par son titulaire (ou son représentant)")
        env = {"format": FORMAT, "mandate": rec.mandate, "mandate_sha256": rec.mandate_sha256,
               "proof": {"kind": "webauthn", "challenge": proof.challenge, "decision": "approve",
                         "device_id": getattr(device, "id", None),
                         "public_key_spki": getattr(device, "public_key_spki", None),
                         "client_data_json": proof.webauthn_client_data_json,
                         "authenticator_data": proof.webauthn_authenticator_data,
                         "signature": proof.webauthn_signature, "origin": origin,
                         "rp_id": rp_id or (urlsplit(origin or "").hostname or None)},
               "signed_by": decided_by,
               "signed_at": datetime.fromtimestamp(self.clock(), tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}
        check = verify_envelope(env, now=self.clock(), trusted_spki=getattr(device, "public_key_spki", None),
                                expected_origin=origin)
        if not check.valid:  # défense en profondeur : le scellé doit se revérifier seul, hors ligne
            raise ValueError("mandat non vérifiable : " + "; ".join(check.errors))
        rec.status, rec.envelope, rec.signed_at = "signed", env, self.clock()
        self.store.put_mandate(rec)
        self._emit("payment.mandate_signed", {"mandate_id": rec.id, "approval_id": approval.id,
                                              "thread_id": approval.thread_id, "account": rec.account,
                                              "merchant": rec.merchant, "amount_minor": rec.amount_minor,
                                              "currency": rec.currency, "signed_by": decided_by})
        return rec

    def on_rejected(self, approval: Any) -> None:
        if not self.covers(approval):
            return
        rec = self.store.by_approval(approval.id)
        if rec is not None and rec.status == "pending":
            rec.status = "rejected"
            self.store.put_mandate(rec)
            self._emit("payment.rejected", {"mandate_id": rec.id, "approval_id": approval.id,
                                            "thread_id": approval.thread_id, "account": rec.account})

    # ───────────────────────── lecture ─────────────────────────

    def _status(self, rec: MandateRecord) -> str:
        return "expired" if rec.status == "pending" and self.clock() > rec.valid_until else rec.status

    def card_json(self, rec: MandateRecord, approval: Any = None) -> dict[str, Any]:
        cart = rec.mandate.get("cart") or {}
        out = {"mandate_id": rec.id, "approval_id": rec.approval_id, "account": rec.account,
               "approver": rec.approver, "status": self._status(rec), "merchant": rec.merchant,
               "amount_minor": rec.amount_minor, "currency": rec.currency,
               "amount": format_minor(rec.amount_minor, rec.currency), "items": cart.get("items") or [],
               "url": cart.get("url"), "source": cart.get("source") or "browser", "reference": cart.get("reference"),
               "valid_until": rec.mandate.get("valid_until"), "mandate_sha256": rec.mandate_sha256,
               "handoff": {"kind": "browser", "url": cart.get("url")}
               if (cart.get("source") or "browser") == "browser" else {"kind": cart.get("source")}}
        if approval is not None:
            out["thread_id"] = approval.thread_id
            out["strength"] = approval.strength
        return out

    def _visible(self, actor_account: str, rec: MandateRecord) -> bool:
        """Routes `/mandates/{id}` et `/verify` : le TITULAIRE seulement ; un autre compte, même du foyer (payeur
        compris), ne la voit pas (404). Exception : le représentant, pour la carte d'un mineur qu'il a SIGNÉE."""
        if actor_account == rec.account:
            return True
        return (rec.approver == actor_account and rec.approver != rec.account
                and (rec.envelope or {}).get("signed_by") == actor_account)

    def _route_view(self, actor_account: str, rec: MandateRecord) -> dict[str, Any]:
        """Carte vue par une route : jamais le locataire, jamais l'identifiant de compte d'un AUTRE membre (titulaire
        vu par le représentant, représentant vu par le mineur, signataire de l'enveloppe)."""
        out = self.card_json(rec)
        out["account"] = rec.account if rec.account == actor_account else None
        out["approver"] = rec.approver if rec.approver == actor_account else None
        out["by_representative"] = bool(rec.approver and rec.approver != rec.account)
        return out

    def _envelope_view(self, actor_account: str, rec: MandateRecord) -> dict[str, Any] | None:
        if rec.envelope is None:
            return None
        env = dict(rec.envelope)
        signer = env.get("signed_by")
        if signer is not None and signer != actor_account:  # hors empreinte : remplacé, la vérification tient
            env["signed_by"] = principal_handle(rec.id, signer)
        return env

    def mandates(self, actor_account: str, member: str | None = None) -> list[dict[str, Any]]:
        target = member or actor_account
        if target != actor_account:
            self._manageable(actor_account, target)
        with self._member_scope(target):
            return [self.card_json(r) for r in self.store.list_mandates(target)]

    def _find(self, actor_account: str, mandate_id: str, member: str | None) -> MandateRecord:
        target = member or actor_account
        if target != actor_account:
            person = self.people.person(target)
            if not (person.minor and person.guardian == actor_account):  # payeur ou autre membre : 404, comme absent
                raise PaymentError(404, "not_found", "mandat inconnu")
        with self._member_scope(target):
            rec = self.store.get_mandate(mandate_id)
        if rec is None or not self._visible(actor_account, rec):
            raise PaymentError(404, "not_found", "mandat inconnu")
        return rec

    def mandate(self, actor_account: str, mandate_id: str, member: str | None = None) -> dict[str, Any]:
        rec = self._find(actor_account, mandate_id, member)
        return {**self._route_view(actor_account, rec), "mandate": rec.mandate,
                "envelope": self._envelope_view(actor_account, rec)}

    def verify(self, actor_account: str, mandate_id: str, member: str | None = None, *,
               device_key: Callable[[str], str | None] | None = None,
               origin: str | None = None) -> dict[str, Any]:
        rec = self._find(actor_account, mandate_id, member)
        if rec.envelope is None:
            return {"mandate_id": rec.id, "status": self._status(rec), "valid": False, "expired": False,
                    "checks": {}, "errors": ["mandat non signé"]}
        dev = (rec.envelope.get("proof") or {}).get("device_id")
        trusted = device_key(dev) if device_key is not None and dev else None
        out = verify_envelope(rec.envelope, now=self.clock(), trusted_spki=trusted, expected_origin=origin)
        return {"mandate_id": rec.id, "status": self._status(rec), **out.to_json()}


__all__ = ["GUARDIAN_RULE", "PAY_RULE", "PAY_TOOL", "HouseholdPeople", "PaymentError", "Payments", "Person",
           "Settings", "SoloPeople"]
