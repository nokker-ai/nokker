"""Outil `pay_request` et veille de page de paiement (docs/PAYMENTS.md §2) — drapeau `payments` seulement.

- `PayTools` : l'outil `pay_request` (effet SPEND). Le modèle l'appelle à l'étape de paiement ; les portes
  (`gates.PaymentGates`) en font une carte FORTE « à payer » ou un refus (budget, mineur). Il ne s'exécute qu'APRÈS
  l'approbation par passkey (`approvals/executor.py`, `ctx.approval_id`) : il ne paie rien, il constate le mandat
  scellé et passe la main à la personne (navigateur : `browser_handoff` ; QR-facture et pain.001 : son e-banking).
- `CheckoutWatch` : enveloppe la source navigateur du pool de la bêta ; retient les références `eN` du dernier
  instantané de chaque fil (libellés pour les portes) et ajoute une ligne au résultat quand la page lue est une page
  de paiement.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from nokker.core.ports import ToolContext, ToolSpec
from nokker.core.turn import Effect, ToolResult
from nokker.payments.detect import checkout_page, refs_of
from nokker.payments.mandate import format_minor
from nokker.payments.service import PAY_TOOL, Payments
from nokker.qa.untrusted import fence, inline

logger = logging.getLogger("nokker.payments.tools")

# ── textes vus par le modèle ──
PAY_DESCRIPTION = ("Arrêt au paiement : prépare une carte « à payer » (marchand, montant, devise, articles, lien) ; "
                   "la personne approuve avec sa passkey et paie elle-même. Ne saisis jamais de numéro de carte.")
PAY_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "merchant": {"type": "string", "description": "Nom du marchand (ou du créancier d'une QR-facture)."},
        "amount": {"type": "string", "description": "Montant total à payer, en unités (ex. « 129.90 »)."},
        "currency": {"type": "string", "description": "Devise ISO 4217 (CHF, EUR…)."},
        "items": {"type": "array", "description": "Articles du panier.",
                  "items": {"type": "object", "properties": {
                      "name": {"type": "string"}, "quantity": {"type": "integer"},
                      "unit_price": {"type": "string"}}, "required": ["name"]}},
        "url": {"type": "string", "description": "Lien de la page de paiement ou du panier."},
        "source": {"type": "string", "enum": ["browser", "qr_bill", "pain001"],
                   "description": "browser (défaut), qr_bill ou pain001."},
        "reference": {"type": "string", "description": "Référence de paiement (QR-facture), facultative."},
    },
    "required": ["merchant", "amount", "currency", "items"],
}
CHECKOUT_NOTICE = ("\n[Paiement] Page de paiement détectée : ne remplis aucun champ de carte et ne clique pas sur le "
                   "bouton de paiement. Appelle pay_request (marchand, montant, devise, articles, lien) ; la personne "
                   "paiera elle-même.")
NOTICE_TAG = "[Paiement]"
NOTICE_TAG_NEUTRAL = "(Paiement)"  # « [Paiement] » écrit par une page, neutralisé dans le résultat du navigateur
DONE_TEXT = "Mandat {mid} signé : {amount} chez {merchant}, valable jusqu'au {until}. {how} Ne touche plus au paiement."
HOW = {"browser": "La personne paie elle-même : le navigateur lui est passé.",
       "browser_nohandoff": "La personne paie elle-même dans le navigateur.",
       "qr_bill": "La personne paie la QR-facture depuis son e-banking.",
       "pain001": "La personne signe l'ordre (pain.001) dans son e-banking."}
NO_MANDATE_TEXT = "Aucun mandat signé pour cette carte : rien n'est payé."
# Texte du passage de main (pour la personne, pas pour le modèle).
HANDOFF_REASON = "Paiement à faire par toi : {amount} chez {merchant} (mandat {mid}). L'agent s'est arrêté avant de payer."


@dataclass
class PayTools:
    payments: Payments
    source_name: str = "builtin:payments"

    def specs(self) -> list[ToolSpec]:
        return [ToolSpec(name=PAY_TOOL, description=PAY_DESCRIPTION, parameters=PAY_PARAMETERS, effect=Effect.SPEND,
                         source=self.source_name)]

    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if name != PAY_TOOL:
            return ToolResult(call_id="", name=name, content=f"outil inconnu : {name}", is_error=True)
        rec = self.payments.store.by_approval(ctx.approval_id) if ctx.approval_id else None
        if rec is None or rec.status != "signed":
            return ToolResult(call_id="", name=name, content=NO_MANDATE_TEXT, is_error=True)
        source = (rec.mandate.get("cart") or {}).get("source") or "browser"
        how = HOW.get(source, HOW["browser_nohandoff"])
        amount = format_minor(rec.amount_minor, rec.currency)
        if source == "browser" and not (self.payments.settings.handoff and self._handoff(ctx, amount, rec)):
            how = HOW["browser_nohandoff"]
        return ToolResult(call_id="", name=name, content=DONE_TEXT.format(
            mid=rec.id, amount=amount, merchant=rec.merchant, until=rec.mandate.get("valid_until"), how=how))

    @staticmethod
    def _handoff(ctx: ToolContext, amount: str, rec: Any) -> bool:
        """`browser_handoff` (mécanisme existant) par le registre du noyau, dans le locataire du fil. Faux s'il
        n'existe pas ou échoue : la carte reste valable, la personne ouvre la page elle-même."""
        tools = getattr(getattr(ctx, "kernel", None), "tools", None)
        if tools is None:
            return False
        try:
            res = tools.call("browser_handoff", {"reason": HANDOFF_REASON.format(
                amount=amount, merchant=inline(rec.merchant, 120), mid=rec.id)}, ctx)
        except Exception:
            logger.warning("browser_handoff en échec pour le mandat %s", rec.id, exc_info=True)
            return False
        return not getattr(res, "is_error", True)


@dataclass
class CheckoutWatch:
    """Source navigateur vue par les portes de paiement : mêmes outils, mêmes résultats, plus la ligne
    `CHECKOUT_NOTICE` quand la page lue est une page de paiement (contenu de la page alors sous clôture
    `<untrusted-data kind="page">`, la ligne hors de la clôture). Un « [Paiement] » écrit par une page devient
    « (Paiement) »."""

    inner: Any
    payments: Payments

    def specs(self) -> list[ToolSpec]:
        return self.inner.specs()

    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        res = self.inner.call(name, arguments, ctx)
        if not name.startswith("browser_") or getattr(res, "is_error", False) or not isinstance(res.content, str):
            return res
        refs = refs_of(res.content)
        seen = checkout_page(res.content) if refs or name in ("browser_snapshot", "browser_read_text") else False
        navigated = name in ("browser_navigate", "browser_navigate_back")
        if refs or seen or navigated:
            self.payments.note_page(ctx.thread_id, refs, seen, reset=navigated)
        if NOTICE_TAG in res.content:  # une page ne peut pas imiter la ligne du noyau
            res.content = res.content.replace(NOTICE_TAG, NOTICE_TAG_NEUTRAL)
        if seen:
            # Page de paiement : son contenu sous clôture `<untrusted-data kind="page">`, la ligne du noyau APRÈS le
            # bloc, hors de la clôture (elle seule commence par « [Paiement] »).
            res.content = fence("page", res.content) + CHECKOUT_NOTICE
        return res

    def __getattr__(self, name: str) -> Any:  # turn_done, refresh… : ceux de la source
        return getattr(self.inner, name)


__all__ = [
    "CHECKOUT_NOTICE",
    "DONE_TEXT",
    "HANDOFF_REASON",
    "HOW",
    "NO_MANDATE_TEXT",
    "PAY_DESCRIPTION",
    "PAY_PARAMETERS",
    "CheckoutWatch",
    "PayTools",
]
