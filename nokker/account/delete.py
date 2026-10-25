"""Suppression d'un compte par la personne elle-même (« Mon compte », docs/CLIENT-API.md ; conditions de la bêta,
RGPD art. 17).

1. `POST /v1/account/delete` pose une carte d'approbation FORTE (canal `account`, action `account_delete`) : seule une
   preuve d'appareil fort la décide (Mini App avec biométrie, passkey avec vérification) — une session volée ne suffit
   pas. L'action n'est pas un outil du registre : le modèle ne peut ni la voir ni la demander.
2. Approuvée : le compte et son locataire passent `suspended` tout de suite (plus aucune requête, plus aucun tour :
   `resolve_tenant` refuse un compte inactif), et une ligne entre dans `account_deletions` (migration 017).
3. L'effacement lui-même reste à l'outil d'opérateur (`nokker erase --pending --apply`, connexion propriétaire, même
   code que l'effacement sur demande), lancé à intervalle régulier : moins de 24 h entre l'accord et l'effacement.
   Le noyau ne reçoit aucun droit d'effacer.
"""
from __future__ import annotations

import time
from typing import Any

from nokker.core.turn import Decision, Gate, InputCheck, ToolCall, Turn, Verdict
from nokker.tenancy.context import scoped_thread_id

ACCOUNT_CHANNEL = "account"
ACTION = "account_delete"
REASON = ("Supprimer ce compte et toutes ses données (conversations, bulles, connecteurs, exports) : irréversible. "
          "Le compte est suspendu dès l'accord, l'effacement suit dans les 24 h.")


def request(approvals: Any, account: Any, tenant_id: str) -> Any:
    """Pose la carte forte de suppression (une carte en attente suffit : on la rend si elle existe déjà)."""
    thread = scoped_thread_id(tenant_id, "account")
    for a in approvals.pending():  # visibles dans le locataire courant seulement
        if a.thread_id == thread and a.tool_name == ACTION:
            return a
    turn = Turn(thread_id=thread, index=0, input=InputCheck(text="suppression du compte"), channel=ACCOUNT_CHANNEL,
                sender=account.account_id)
    call = ToolCall(id=f"del-{int(time.time())}", name=ACTION, arguments={"account_id": account.account_id})
    verdict = Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule="account:delete", reason=REASON,
                      subject=call.id, strength="strong")
    return approvals.request(turn, call, verdict, None)


def executor_action(tenancy: Any, cancel_subscriptions: Any = None) -> Any:
    """L'action exécutée quand la carte est approuvée : résiliation des abonnements (Stripe, tout de suite, sans
    remboursement), puis suspension + file d'effacement. Si la résiliation échoue (Stripe injoignable), RIEN n'est
    suspendu : la carte passe en échec et la personne peut redemander. Rend le texte du résultat."""

    def run(approval: Any) -> str:
        account_id = str(approval.arguments.get("account_id") or "")
        acc = tenancy.account(account_id)
        if acc is None or approval.decided_by != account_id:
            raise PermissionError("la suppression ne peut être accordée que par le compte lui-même")
        if cancel_subscriptions is not None:
            cancel_subscriptions(acc.tenant_id)  # sinon Stripe facture un compte effacé (et `nokker erase` refuse)
        tenancy.suspend_for_deletion(account_id, approval.id)
        return "compte suspendu, effacement dans les 24 h"

    return run


__all__ = ["ACCOUNT_CHANNEL", "ACTION", "executor_action", "request"]
