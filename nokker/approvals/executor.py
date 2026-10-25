"""Exécuteur d'approbation (docs/APPROVALS.md §2, étape « Exécution ») — appelé par `nokker/api/server.py`
juste après un `Approvals.decide()` réussi (jamais par le port `Approvals` lui-même : `decide()` ne fait
QUE vérifier la preuve, cf. `core/ports.py::Approvals`, l'exécution a besoin du registre d'outils et des
invariants, que le port Approvals n'a pas).

Approuvé → recalcule `args_sha256` (défense en profondeur : `decide()` l'a déjà fait), appelle l'outil via
le registre du `Kernel`, passe le résultat par `redact_result`, pose le statut `executed`/`failed`, puis
lance un tour de continuation sur le MÊME fil avec un bloc `[Approbation]…[/Approbation]` dans le message
utilisateur (docs/CONTEXT-FORMAT.md : blocs du tour avant le message humain — ici, le bloc EST le message,
il n'y a pas d'humain qui tape dans un tour de continuation automatique).
Refusé → continuation « refusé par X », aucun outil appelé.

Sortie de l'outil (docs/LOKKAL.md §7.4.9) : `on_result` reçoit chaque exécution (réussie, en échec, refusée par
l'empreinte) d'une carte du modèle AVANT le tour de suite, pour le `tool.result` qui ferme l'appel resté « en attente »
(`call_id` de la carte). L'exécuteur ne prend pas de verrou : son appelant le lance sous la file du fil (le serveur,
`schedule_decision`), et le tour de suite reprend le même verrou (réentrant).

Carte d'un SOUS-AGENT (fil `<parent>/sa-<id>`, docs/LOKKAL.md §7.22) : l'outil et le tour de suite tournent avec le
noyau de l'ENFANT (`subagent_kernel(fil)`, son registre filtré, ses enveloppes), JAMAIS avec celui du parent (dont le
registre rendrait `clarify`, `send_*`… appelables : élévation). Pas de noyau d'enfant (enfant annulé, arrêt du noyau,
pas de résolveur) : la carte approuvée passe `failed` sans rien exécuter, son `tool.result` part en erreur ; refusée,
elle n'a pas de tour de suite.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from nokker.approvals.crypto import args_sha256
from nokker.account.delete import ACCOUNT_CHANNEL
from nokker.core.loop import DIRECT_CHANNEL, Kernel
from nokker.tenancy.context import tenant_of_thread, tenant_scope
from nokker.core.ports import Approval, ToolContext
from nokker.core.turn import Turn, TurnInput

logger = logging.getLogger("nokker.approvals.executor")

SUBAGENT_MARK = "/sa-"  # fil d'un sous-agent : `<fil parent>/sa-<id>` (nokker/tools/delegate.py)
CHILD_GONE = ("sous-agent introuvable (terminé, annulé ou noyau redémarré) : carte refusée, jamais exécutée avec le "
              "noyau du parent")
RESULT_CHAR_LIMIT = 6000  # un résultat très long n'a pas sa place entière dans le bloc du tour (A-1 réservoir à part)


def _approbation_block(approval: Approval, *, outcome: str) -> str:
    lines = [
        "[Approbation]",
        f"Outil : {approval.tool_name}",
        *([f"Commande : {approval.arguments['command']}"] if approval.tool_name in ("terminal", "process_start")
          and approval.arguments.get("command") else []),
        f"Décision : {approval.status}",
        f"Décideur : {approval.decided_by}",
        f"Moyen : {approval.decided_via}",
        f"Résultat : {outcome}",
        # Banc CLI de Lokkal (23.10) : sans cette ligne, le modèle redemandait la même commande après l'avoir obtenue.
        "Cette action est tranchée et son résultat complet est ci-dessus : ne la relance pas, ni sous une autre forme "
        "(autre outil de test, mêmes vérifications). Continue la tâche à partir de ce résultat.",
        "[/Approbation]",
    ]
    return "\n".join(lines)


@dataclass
class ApprovalExecutor:
    kernel: Kernel
    approvals: object  # backend concret (InMemoryApprovals/PgApprovals) — pour mark_executed/mark_failed
    # Carte d'un appel direct du propriétaire (canal DIRECT_CHANNEL, §7.4.4) : pas de tour modèle après la décision ;
    # `on_direct(approval, content, is_error)` publie le résultat (événement `tool.result`), et une ligne « [hors
    # tour] » attend le prochain tour du fil.
    on_direct: Callable[[Approval, str, bool], Any] | None = None
    # Cartes qui ne portent pas un appel d'outil du modèle mais un geste du noyau (armement d'un agent Crew par carte
    # forte, lot X14) : `handlers[tool_name](approval)` applique la décision (approuvée OU refusée) et rend le résumé
    # posé sur la carte ; ni outil du registre, ni tour de continuation.
    handlers: dict[str, Callable[[Approval], str]] = field(default_factory=dict)
    # Noyau qui porte la suite d'un fil particulier (passage d'un agent Crew : ses portes, son budget, ses secrets) ;
    # None (ou une réponse None) = le noyau ordinaire.
    kernel_for: Callable[[str], Any] | None = None
    # Noyau du sous-agent qui a posé une carte (`DelegateToolSource.kernel_for_card`) ; None = aucun : toute carte d'un
    # fil `<parent>/sa-<id>` est alors refusée (jamais de repli sur le noyau du parent).
    subagent_kernel: Callable[[str], Any] | None = None

    def _k(self, thread_id: str) -> Any:
        if SUBAGENT_MARK in thread_id:  # jamais self.kernel : son registre n'est pas celui de l'enfant
            if self.subagent_kernel is None:
                return None
            try:
                return self.subagent_kernel(thread_id)
            except Exception:  # noqa: BLE001 — résolveur en panne : échec fermé
                logger.exception("noyau du sous-agent introuvable pour %s", thread_id)
                return None
        k = self.kernel_for(thread_id) if self.kernel_for is not None else None
        return k if k is not None else self.kernel
    # Carte posée par le modèle : `on_result(approval, content, is_error, ms)` publie la sortie (`tool.result` du même
    # `call_id`) avant le tour de suite. None = rien n'est publié (tests, bancs).
    on_result: Callable[[Approval, str, bool, int | None], Any] | None = None
    # Cartes « Mon compte » (canal `account`, nokker/account/delete.py) : une action du noyau, pas un outil du
    # registre (le modèle ne peut pas la demander) ; aucun tour modèle après la décision.
    account_actions: dict[str, Callable[[Approval], str]] = field(default_factory=dict)

    def run(self, approval: Approval) -> Turn | None:
        """À appeler APRÈS `approvals.decide()`. `approval.status` doit déjà être `approved` ou
        `rejected` (sinon : erreur de l'appelant, pas de ce module)."""
        # La carte appartient au locataire de son fil (docs/TENANCY.md §3) : l'outil et la suite du tour tournent là, quel
        # que soit le fil d'exécution de l'appelant.
        with tenant_scope(tenant_of_thread(approval.thread_id)):
            handler = self.handlers.get(approval.tool_name)
            if handler is not None:
                return self._handle(approval, handler)
            if SUBAGENT_MARK in approval.thread_id and approval.status in ("approved", "rejected") \
                    and self._k(approval.thread_id) is None:
                return self._child_gone(approval)
            if approval.status == "rejected":
                return self._continue_with(approval, _approbation_block(
                    approval, outcome=f"refusé par {approval.decided_by or 'inconnu'}."), "refusé", True)
            if approval.status != "approved":
                raise ValueError(f"ApprovalExecutor.run() appelé sur un statut inattendu : {approval.status!r}")
            return self._execute_approved(approval)

    def _child_gone(self, approval: Approval) -> None:
        """Carte d'un sous-agent qui n'existe plus : approuvée → `failed` sans rien exécuter (et son `tool.result` en
        erreur) ; refusée → rien (pas de tour de suite)."""
        logger.warning("carte %s d'un sous-agent disparu (%s) : %s", approval.id, approval.thread_id,
                       "refusée" if approval.status == "approved" else "pas de suite")
        if approval.status != "approved":
            return None
        approval = self.approvals.mark_failed(approval.id, CHILD_GONE)
        if self.on_result is not None:
            try:
                self.on_result(approval, CHILD_GONE, True, None)
            except Exception:  # noqa: BLE001
                logger.exception("publication du refus de l'approbation %s impossible", approval.id)
        return None

    def _handle(self, approval: Approval, handler: Callable[[Approval], str]) -> None:
        if approval.status not in ("approved", "rejected"):
            raise ValueError(f"ApprovalExecutor.run() appelé sur un statut inattendu : {approval.status!r}")
        try:
            summary = handler(approval)
        except Exception as e:  # noqa: BLE001 — l'échec est posé sur la carte
            logger.exception("geste du noyau en échec : approval=%s", approval.id)
            self.approvals.mark_failed(approval.id, f"{type(e).__name__}: {e}")
            return None
        if approval.status == "approved":
            self.approvals.mark_executed(approval.id, summary or "")
        return None

    def _execute_approved(self, approval: Approval) -> Turn | None:
        fresh_hash = args_sha256(approval.arguments)
        if fresh_hash != approval.args_sha256:
            error = "args_sha256 modifiée depuis la demande — exécution refusée"
            approval = self.approvals.mark_failed(approval.id, error)
            logger.error("exécution refusée (args_sha256) : approval=%s", approval.id)
            return self._continue_with(approval, _approbation_block(
                approval, outcome="ÉCHEC — les arguments ont changé depuis la demande, exécution refusée."), error, True,
                ran=True)

        if approval.channel == ACCOUNT_CHANNEL:
            action = self.account_actions.get(approval.tool_name)
            try:
                if action is None:
                    raise LookupError(f"action de compte inconnue : {approval.tool_name}")
                summary = action(approval)
            except Exception as e:  # noqa: BLE001 — rien n'est fait : la carte passe en échec
                self.approvals.mark_failed(approval.id, f"{type(e).__name__}: {e}")
                logger.exception("action de compte en échec : approval=%s", approval.id)
                return None
            self.approvals.mark_executed(approval.id, summary)
            return None

        kernel = self._k(approval.thread_id)
        spec = kernel.tools.spec(approval.tool_name)
        ctx = ToolContext(thread_id=approval.thread_id, turn_index=approval.turn_index, channel=approval.channel,
                          sender=approval.decided_by, approval_id=approval.id)
        t0 = time.monotonic()
        try:
            result = kernel.tools.call(approval.tool_name, approval.arguments, ctx)
            result = kernel.invariants.redact_result(result, spec)
        except Exception as e:  # noqa: BLE001 — un outil qui plante n'est pas un bug de l'exécuteur
            error = f"{type(e).__name__}: {e}"
            approval = self.approvals.mark_failed(approval.id, error)
            logger.exception("exécution de l'outil approuvé a levé : approval=%s", approval.id)
            return self._continue_with(approval, _approbation_block(approval, outcome=f"ÉCHEC — {error}"), error, True,
                                       ran=True, ms=int((time.monotonic() - t0) * 1000))

        ms = int((time.monotonic() - t0) * 1000)
        outcome = result.content
        if len(outcome) > RESULT_CHAR_LIMIT:
            # Début ET fin : la ligne qui compte d'une sortie de tests (« OK », « FAILED ») est à la fin — coupée, le
            # modèle relançait la commande (banc CLI de Lokkal, 23.10).
            head, tail = outcome[:RESULT_CHAR_LIMIT // 3], outcome[-(2 * RESULT_CHAR_LIMIT // 3):]
            outcome = f"{head}\n[… {len(outcome) - len(head) - len(tail)} caractères coupés …]\n{tail}"
        if result.is_error:
            approval = self.approvals.mark_failed(approval.id, outcome)
            outcome = f"ÉCHEC — {outcome}"
        else:
            approval = self.approvals.mark_executed(approval.id, outcome)
        return self._continue_with(approval, _approbation_block(approval, outcome=outcome), result.content,
                                   result.is_error, ran=True, ms=ms)

    def _continue_with(self, approval: Approval, block: str, content: str = "", is_error: bool = False, *,
                       ran: bool = False, ms: int | None = None) -> Turn | None:
        if approval.channel == ACCOUNT_CHANNEL:
            return None  # refus d'une carte de compte : rien à faire, aucun tour modèle
        if approval.channel == DIRECT_CHANNEL:
            self.kernel.add_owner_note(approval.thread_id, "[hors tour] " + block)
            if self.on_direct is not None:
                self.on_direct(approval, content, is_error)
            return None
        if ran and self.on_result is not None:
            try:
                self.on_result(approval, content, is_error, ms)
            except Exception:  # noqa: BLE001 — un événement manqué ne bloque pas la suite du tour
                logger.exception("publication du résultat de l'approbation %s impossible", approval.id)
        # Fil d'un passage d'agent : son noyau repose l'identité du passage (son propriétaire), pas celle du décideur.
        kernel = self._k(approval.thread_id)
        return kernel.run_turn(TurnInput(thread_id=approval.thread_id, user_text=block,
                                         channel=approval.channel, sender=approval.decided_by))


__all__ = ["CHILD_GONE", "ApprovalExecutor"]
