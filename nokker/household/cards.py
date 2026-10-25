"""Cartes fortes des profils mineurs, décidées par leur représentant (lot X23, docs/LOKKAL.md §7.29.4).

Une carte `household:guardian` vit dans le locataire du MINEUR (celui de son fil) ; elle n'y est décidable que par
le représentant, depuis SES appareils de confiance (preuve forte : passkey vérifiée ou Mini App), jamais par le
mineur (`BaseApprovals.decider_allowed`). Le représentant n'entre pas dans le locataire du mineur : ces routes
ouvrent le locataire du mineur le temps de lire OU décider ses cartes `household:guardian`, et rien d'autre.

Ce que voit le représentant : la carte elle-même (outil, arguments, motif), c'est-à-dire l'action qu'il signe ; pas
le fil, ni la conversation qui y a mené.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from nokker.household.model import GUARDIAN_RULE
from nokker.team.model import TeamError


def _status(exc: Exception) -> tuple[int, str]:
    from nokker.approvals.errors import (
        ApprovalExpired,
        ApprovalNotFound,
        ApprovalNotPending,
        ProofInvalid,
    )

    if isinstance(exc, ApprovalNotFound):
        return 404, "not_found"
    if isinstance(exc, ApprovalExpired):
        return 410, "expired"
    if isinstance(exc, ApprovalNotPending):
        return 409, "not_pending"
    if isinstance(exc, ProofInvalid):
        return 403, "proof_invalid"
    return 400, "bad_request"


@dataclass
class GuardianCards:
    household: Any  # nokker.household.service.Household
    approvals: Any  # BaseApprovals
    to_json: Callable[[Any], dict[str, Any]]  # forme canonique d'une carte (api/server.py::approval_to_json)
    # Exécution après la décision (api/server.py::schedule_decision) : (carte) → objet avec `.wait(s)` ; None = rien.
    schedule: Callable[[Any], Any] | None = None
    wait_s: float = 25.0

    def _minor_tenant(self, actor: Any, member: Any) -> tuple[Any, str]:
        rows = {r.account_id: r for r in self.household.minors_of(actor)}  # payeur seulement (403 sinon)
        row = rows.get(str(member or ""))
        acc = self.household.team.tenancy.account(row.account_id) if row is not None else None
        if acc is None:
            raise TeamError(404, "not_found", "profil mineur inconnu dans ce foyer")
        return row, acc.tenant_id

    def _card(self, approval_id: str) -> Any:
        card = self.approvals.get(approval_id)
        if card is None or card.rule != GUARDIAN_RULE:
            raise TeamError(404, "not_found", "carte inconnue")
        return card

    def pending(self, actor: Any) -> dict[str, Any]:
        from nokker.tenancy.context import tenant_scope

        out = []
        for row in self.household.minors_of(actor):
            acc = self.household.team.tenancy.account(row.account_id)
            if acc is None:
                continue
            with tenant_scope(acc.tenant_id):
                for card in self.approvals.pending():
                    if card.rule == GUARDIAN_RULE:
                        out.append({"member": {"account_id": row.account_id, "email": row.email,
                                               "profile": row.profile}, "approval": self.to_json(card)})
        return {"approvals": out}

    def challenge(self, actor: Any, approval_id: str, body: dict[str, Any]) -> dict[str, Any]:
        from nokker.tenancy.context import tenant_scope

        _, tenant = self._minor_tenant(actor, body.get("member"))
        with tenant_scope(tenant):
            self._card(approval_id)
            try:
                ch = self.approvals.challenge(approval_id)
            except Exception as e:  # noqa: BLE001 — erreurs d'approbation : statut clair
                status, code = _status(e)
                raise TeamError(status, code, str(e)) from None
        return {"challenge": ch.canonical(), "nonce": ch.nonce, "exp": ch.exp}

    def decide(self, actor: Any, approval_id: str, body: dict[str, Any]) -> dict[str, Any]:
        from nokker.core.ports import Proof
        from nokker.tenancy.context import tenant_scope

        _, tenant = self._minor_tenant(actor, body.get("member"))
        webauthn = body.get("webauthn") or {}
        proof = Proof(challenge=body.get("challenge"), signature=body.get("signature"), device_id=body.get("device_id"),
                      telegram_init_data=body.get("init_data"),
                      webauthn_client_data_json=webauthn.get("client_data_json") or webauthn.get("clientDataJSON"),
                      webauthn_authenticator_data=webauthn.get("authenticator_data")
                      or webauthn.get("authenticatorData"),
                      webauthn_signature=webauthn.get("signature"))
        with tenant_scope(tenant):
            self._card(approval_id)
            try:
                decided = self.approvals.decide(approval_id, str(body.get("decision") or ""), proof)
            except Exception as e:  # noqa: BLE001
                status, code = _status(e)
                raise TeamError(status, code, str(e)) from None
            if self.schedule is not None:
                waiter = self.schedule(decided)
                if waiter is not None:
                    waiter.wait(self.wait_s)
            final = self.approvals.get(decided.id) or decided
        return self.to_json(final)


__all__ = ["GuardianCards"]
