"""Portes des paiements (docs/PAYMENTS.md §2) — enveloppe les portes du noyau, comme `household/guard.MinorGates`.

1. **Arrêt au paiement** : sur un outil navigateur à effet, un champ de carte (numéro, CVV/CVC, expiration, titulaire,
   3-D Secure), un numéro de carte tapé (Luhn), ou un clic sur le bouton qui déclenche le paiement → REFUS, avec une
   raison qui dit au modèle d'appeler `pay_request`. L'agent ne saisit JAMAIS un numéro de carte, un CVV ni un code 3DS.
2. **Carte « à payer »** (`pay_request`) : arguments validés, budget vérifié AVANT la carte (refus sinon), puis carte
   FORTE (`payments:to_pay`, ou `household:guardian` pour un profil mineur permis par le payeur).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from nokker.core.ports import ToolSpec
from nokker.core.turn import Decision, Gate, ToolCall, Turn, Verdict
from nokker.payments.detect import card_field, looks_like_pan, looks_like_secret_code, pay_button
from nokker.payments.mandate import MandateError, format_minor, parse_cart
from nokker.payments.service import GUARDIAN_RULE, PAY_RULE, PAY_TOOL, Payments
from nokker.qa.untrusted import inline

CARD_RULE = "payments:card_field"
CHECKOUT_RULE = "payments:checkout"
INVALID_RULE = "payments:invalid"

# Textes vus par le modèle (dans « REFUSÉ par le noyau (<règle>: <raison>). Ne pas réessayer autrement. »).
CARD_REASON = ("champ de carte (« {term} ») : Nokker ne saisit jamais un numéro de carte, un code de sécurité ni un "
               "code 3-D Secure. Arrête-toi ici et appelle pay_request : la personne paiera elle-même.")
CHECKOUT_REASON = ("étape de paiement (« {label} ») : l'agent s'arrête avant de payer. Appelle pay_request avec le "
                   "marchand, le montant, la devise, les articles et le lien ; la personne paiera elle-même.")
INVALID_REASON = "carte « à payer » illisible : {error}."
TO_PAY_REASON = ("à payer : {amount} chez {merchant}. La personne approuve avec sa passkey puis paie elle-même ; "
                 "l'agent ne paie jamais.")

BROWSER_INPUT = frozenset({"browser_type", "browser_fill", "browser_fill_form", "browser_select_option",
                           "browser_press_key", "browser_evaluate", "browser_click"})
_SUBMIT_KEYS = frozenset({"enter", "numpadenter"})


def _deny(rule: str, reason: str, call: ToolCall) -> Verdict:
    return Verdict(gate=Gate.ACTION, decision=Decision.DENY, layer=1, rule=rule, reason=reason, subject=call.id)


def browser_verdict(payments: Payments | None, call: ToolCall, thread_id: str) -> Verdict | None:
    """Refus d'un appel navigateur qui toucherait au paiement, ou None. `payments` donne les libellés des références
    `eN` vues dans le dernier instantané du fil (None : seulement `element` et le texte)."""
    name, a = call.name, call.arguments if isinstance(call.arguments, dict) else {}

    def label_of(ref: Any, element: Any) -> str:
        parts = [str(element or "")]
        if payments is not None and isinstance(ref, str):
            parts.append(payments.ref_label(thread_id, ref) or "")
        return " | ".join(p for p in parts if p)

    def card(label: str, value: Any) -> Verdict | None:
        if looks_like_pan(str(value) if value is not None else ""):
            return _deny(CARD_RULE, CARD_REASON.format(term="numéro de carte"), call)
        term = card_field(label)
        if term:
            return _deny(CARD_RULE, CARD_REASON.format(term=term), call)
        return None

    checkout = payments is not None and payments.on_checkout(thread_id)
    if name in ("browser_type", "browser_fill"):
        v = card(label_of(a.get("ref"), a.get("element") or a.get("name")), a.get("text", a.get("value")))
        if v is None and checkout and a.get("submit") is True:
            return _deny(CHECKOUT_RULE, CHECKOUT_REASON.format(label="envoi du formulaire"), call)
        return v
    if name == "browser_fill_form":
        for f in a.get("fields") or []:
            if isinstance(f, dict):
                v = card(label_of(f.get("ref"), f.get("name")), f.get("value"))
                if v is not None:
                    return v
        return None
    if name == "browser_select_option":
        label = label_of(a.get("ref"), a.get("element"))
        term = card_field(label)
        return _deny(CARD_RULE, CARD_REASON.format(term=term), call) if term else None
    if name == "browser_evaluate":
        script = str(a.get("function") or "")
        v = card(f"{script} | {label_of(a.get('ref'), a.get('element'))}", script)
        if v is None and checkout and (".click(" in script or "submit(" in script or "requestSubmit" in script):
            return _deny(CHECKOUT_RULE, CHECKOUT_REASON.format(label="script sur la page de paiement"), call)
        return v
    if name == "browser_press_key":
        key = str(a.get("key") or "")
        if checkout and (key.lower() in _SUBMIT_KEYS or looks_like_secret_code(key) or key.isdigit()):
            return _deny(CHECKOUT_RULE, CHECKOUT_REASON.format(label=f"touche {inline(key, 40)}"), call)
        return None
    if name == "browser_click":
        label = label_of(a.get("ref"), a.get("element"))
        found = pay_button(label)
        return _deny(CHECKOUT_RULE, CHECKOUT_REASON.format(label=found), call) if found else None
    return None


@dataclass
class PaymentGates:
    """Portes du noyau précédées des règles de paiement (même forme que `MinorGates`)."""

    inner: Any
    payments: Payments

    def action(self, call: ToolCall, spec: ToolSpec | None, turn: Turn) -> list[Verdict]:
        if call.name in BROWSER_INPUT:
            v = browser_verdict(self.payments, call, turn.thread_id)
            if v is not None:
                return [v]
            return self.inner.action(call, spec, turn)
        if call.name != PAY_TOOL:
            return self.inner.action(call, spec, turn)
        sender = getattr(turn, "sender", None)
        try:
            cart = parse_cart(call.arguments)
        except MandateError as e:
            return [_deny(INVALID_RULE, INVALID_REASON.format(error=inline(e, 200)), call)]
        inner = self.inner.action(call, spec, turn)
        denied = [v for v in inner if v.decision is Decision.DENY]
        if denied:  # mode plan, règle de refus, profil suspendu… : le refus du noyau prime
            return denied
        refused = self.payments.refusal(sender, cart)
        if refused is not None:
            rule, reason = refused
            self.payments.note_refused(turn.thread_id, sender, rule, reason, cart)
            return [_deny(rule, reason, call)]
        _, guardian = self.payments.approver_of(sender)
        return [Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule=GUARDIAN_RULE if guardian else PAY_RULE,
                        reason=TO_PAY_REASON.format(amount=format_minor(cart.amount_minor, cart.currency),
                                                    merchant=inline(cart.merchant, 120)),
                        subject=call.id, strength="strong")]

    def switch(self, reply: str, turn: Turn, tier: Any) -> list[Verdict]:
        return self.inner.switch(reply, turn, tier)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


__all__ = [
    "BROWSER_INPUT",
    "CARD_REASON",
    "CARD_RULE",
    "CHECKOUT_REASON",
    "CHECKOUT_RULE",
    "INVALID_REASON",
    "INVALID_RULE",
    "TO_PAY_REASON",
    "PaymentGates",
    "browser_verdict",
]
