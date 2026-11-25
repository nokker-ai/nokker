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
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from nokker.approvals.crypto import args_sha256
from nokker.approvals.rule_labels import lang_scope
from nokker.core import model_texts as mt
from nokker.account.delete import ACCOUNT_CHANNEL
from nokker.core.loop import DIRECT_CHANNEL, Kernel
from nokker.tools.mcp_apps import APP_CHANNEL
from nokker.tenancy.context import tenant_of_thread, tenant_scope
from nokker.core.ports import Approval, ToolContext
from nokker.core.turn import APPROVAL_CONTINUE, ToolResult, Turn, TurnInput

logger = logging.getLogger("nokker.approvals.executor")

SUBAGENT_MARK = "/sa-"  # fil d'un sous-agent : `<fil parent>/sa-<id>` (nokker/tools/delegate.py)
CHILD_GONE = ("sous-agent introuvable (terminé, annulé ou redémarré) : carte refusée, jamais exécutée avec les outils "
              "du parent")
RESULT_CHAR_LIMIT = 6000  # un résultat très long n'a pas sa place entière dans le bloc du tour (A-1 réservoir à part)


FAILED_ISSUE = mt.CARD_FAILED_ISSUE["fr"]  # figés (relecture des textes du 15.11.2026) ; autres langues : `model_texts`
# Consigne finale quand le code de sortie est ≠ 0 : remplace la consigne ordinaire ; elle garde « ni sous une autre forme » et
# « Continue la tâche », sans contradiction.
FAILED_INSTRUCTION = mt.CARD_FAILED_INSTRUCTION["fr"]


def _approbation_block(approval: Approval, *, outcome: str, exit_code: int | None = None,
                       lang: str | None = None) -> str:
    # Code de sortie ≠ 0 (champ structuré du résultat d'outil, jamais deviné dans le texte) : l'échec est dit. 0 ou
    # absent : bloc identique octet pour octet à l'ancien (cache de préfixe des cas réussis). `lang` : langue du tour
    # (le texte que lit le modèle suit la langue de la personne) ; sans elle, celle du contexte, sinon le français.
    failed = exit_code is not None and exit_code != 0
    def p(table: dict[str, str]) -> str:
        return mt.pick(table, lang)

    lines = [
        p(mt.APPROVAL_OPEN),
        p(mt.CARD_TOOL).format(name=approval.tool_name),
        *([p(mt.CARD_COMMAND).format(command=approval.arguments["command"])] if approval.tool_name in ("terminal", "process_start")
          and approval.arguments.get("command") else []),
        p(mt.CARD_DECISION).format(status=approval.status),
        p(mt.CARD_DECIDER).format(who=approval.decided_by),
        p(mt.CARD_VIA).format(via=approval.decided_via),
        *([p(mt.CARD_FAILED_ISSUE).format(code=exit_code)] if failed else []),
        p(mt.CARD_RESULT).format(outcome=outcome),
        # Banc CLI de Lokkal (23.10) : sans cette ligne, le modèle redemandait la même commande après l'avoir obtenue.
        p(mt.CARD_FAILED_INSTRUCTION) if failed else p(mt.CARD_INSTRUCTION),
        p(mt.APPROVAL_CLOSE),
    ]
    return "\n".join(lines)


@dataclass
class _Ran:
    """Ce que rend l'appel d'un outil approuvé : la carte à jour, le bloc `[Approbation]`, le contenu et l'état."""

    approval: Approval
    block: str
    content: str
    is_error: bool
    ms: int | None = None
    result: ToolResult | None = None  # résultat caviardé de l'outil (None : empreinte refusée, outil qui a levé)


@dataclass
class InlineOutcome:
    """Décision prise PENDANT le tour (`ApprovalExecutor.settle_in_turn`) : `kind` = `executed` | `failed` (l'outil a tourné ;
    `result` quand il a rendu quelque chose) | `rejected` (refus, aucun outil)."""

    kind: str
    approval: Approval
    block: str
    content: str = ""
    result: ToolResult | None = None


@dataclass
class _Waiter:
    finished: threading.Event = field(default_factory=threading.Event)
    handed: bool = False  # la décision est arrivée par `schedule_decision` : le tour qui attend l'exécute lui-même
    closed: bool = False  # le tour a cessé d'attendre : la suite habituelle prend la décision


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

    def _lang(self, approval: Approval) -> str | None:
        """Langue de la suite d'une carte : celle que le noyau résoudrait pour un tour de ce fil (requête de la décision,
        compte, dernière langue du fil), sinon None (contexte, puis français). Le texte que lit le modèle et les messages
        des outils exécutés suivent cette langue."""
        resolve = getattr(self._k(approval.thread_id) or self.kernel, "resolve_lang", None)
        if not callable(resolve):
            return None
        try:
            return resolve(approval.thread_id, approval.decided_by or None)
        except Exception:  # la langue est un confort : jamais un obstacle à l'exécution
            logger.warning("langue de la carte %s illisible", approval.id, exc_info=True)
            return None

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
    # Carte REFUSÉE (n°27 b) : `on_declined(approval)` publie le `tool.result` de l'appel resté « en attente » avec
    # `status: "refused"`, `reason: "declined"`, avant le tour de suite (le modèle, lui, reçoit le bloc `[Approbation]`).
    on_declined: Callable[[Approval], Any] | None = None
    # Cartes « Mon compte » (canal `account`, nokker/account/delete.py) : une action du noyau, pas un outil du
    # registre (le modèle ne peut pas la demander) ; aucun tour modèle après la décision.
    account_actions: dict[str, Callable[[Approval], str]] = field(default_factory=dict)
    # Une carte ne s'exécute qu'UNE fois : clé (id, empreinte des arguments, décision), prise avant tout effet. Garde
    # contre un 2e ordonnancement de la même carte (décision HTTP + relève, reprise de la suite) ; le statut `executed`
    # / `failed` lu sur la carte couvre le redémarrage du processus.
    _claimed: dict[tuple[str, str, str], None] = field(default_factory=dict, repr=False)
    _claim_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # Tours qui attendent la décision d'une carte qu'ils viennent de poser (`settle_in_turn`) : id de carte -> attente.
    _waiters: dict[str, _Waiter] = field(default_factory=dict, repr=False)

    def _claim(self, approval: Approval) -> bool:
        key = (approval.id, approval.args_sha256, approval.status)
        with self._claim_lock:
            if key in self._claimed:
                return False
            self._claimed[key] = None
            if len(self._claimed) > 5000:  # borne mémoire : on oublie les plus anciennes
                for old in list(self._claimed)[:2500]:
                    self._claimed.pop(old, None)
        return True

    def _already_settled(self, approval: Approval) -> bool:
        """Carte déjà exécutée ou échouée (lue sur le backend) : rien à relancer."""
        get = getattr(self.approvals, "get", None)
        current = get(approval.id) if callable(get) else None
        return current is not None and current.status in ("executing", "executed", "failed", "interrupted")

    def run(self, approval: Approval) -> Turn | None:
        """À appeler APRÈS `approvals.decide()`. `approval.status` doit déjà être `approved` ou
        `rejected` (sinon : erreur de l'appelant, pas de ce module)."""
        # La carte appartient au locataire de son fil (docs/TENANCY.md §3) : l'outil et la suite du tour tournent là, quel
        # que soit le fil d'exécution de l'appelant.
        from nokker.tools.scope import thread_scope

        with tenant_scope(tenant_of_thread(approval.thread_id)), thread_scope(approval.thread_id), \
                lang_scope(self._lang(approval)):
            handler = self.handlers.get(approval.tool_name)
            if handler is not None:
                return self._handle(approval, handler)
            if SUBAGENT_MARK in approval.thread_id and approval.status in ("approved", "rejected") \
                    and self._k(approval.thread_id) is None:
                return self._child_gone(approval)
            if approval.status == "rejected":
                if not self._claim(approval):
                    return None  # même refus ordonnancé deux fois : un seul tour de suite
                return self._continue_with(approval, _approbation_block(
                    approval, outcome=self._rejected_outcome(approval)), "refusé", True, declined=True)
            if approval.status in ("executing", "interrupted"):  # `interrupted` : résultat inconnu, JAMAIS relancée
                logger.warning("carte %s déjà %s : pas de 2e exécution", approval.id, approval.status)
                return None
            if approval.status != "approved":
                raise ValueError(f"ApprovalExecutor.run() appelé sur un statut inattendu : {approval.status!r}")
            if self._already_settled(approval) or not self._claim(approval):
                logger.warning("carte %s déjà exécutée ou en cours : pas de 2e exécution", approval.id)
                return None
            return self._execute_approved(approval)

    def _rejected_outcome(self, approval: Approval) -> str:
        """« refusé par X. » ; avec le motif donné par la personne (donnée de l'hôte, nettoyée, encadrée) s'il y en a un."""
        from nokker.approvals.reasons import clean_reason

        who = approval.decided_by or mt.pick(mt.CARD_UNKNOWN_DECIDER)
        get = getattr(self.approvals, "decision_reason", None)
        scrub = getattr(getattr(self._k(approval.thread_id), "invariants", None), "scrub_output", None)
        try:
            reason = clean_reason(get(approval.id), scrub) if callable(get) else None
        except Exception:  # un motif illisible ne casse pas le refus
            logger.warning("motif du refus de la carte %s illisible", approval.id, exc_info=True)
            reason = None
        if reason:
            return mt.pick(mt.CARD_REJECTED_REASON).format(who=who, reason=reason)
        return mt.pick(mt.CARD_REJECTED).format(who=who)

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
        if approval.status == "interrupted":  # geste du noyau déjà tenté, issue inconnue : jamais deux fois
            logger.warning("carte %s interrompue : geste du noyau non relancé", approval.id)
            return None
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
                approval, outcome=mt.pick(mt.CARD_HASH_CHANGED)), error, True,
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

        ran = self._call_approved(approval)
        return self._continue_with(ran.approval, ran.block, ran.content, ran.is_error, ran=True, ms=ran.ms)

    def _call_approved(self, approval: Approval) -> _Ran:
        """Appelle l'outil d'une carte approuvée et pose `executed` / `failed` sur la carte. Rend le bloc `[Approbation]`
        et le résultat ; ne lance aucun tour de suite (c'est l'appelant qui décide : suite habituelle, ou le tour même).
        La carte reste « en vol » (jamais interrompue par le balayage) jusqu'à la fin, quoi qu'il arrive."""
        try:
            return self._run_approved_tool(approval)
        finally:
            end = getattr(self.approvals, "end_executing", None)
            if callable(end):
                end(approval.id)

    def _run_approved_tool(self, approval: Approval) -> _Ran:
        fresh_hash = args_sha256(approval.arguments)
        if fresh_hash != approval.args_sha256:
            error = "args_sha256 modifiée depuis la demande — exécution refusée"
            approval = self.approvals.mark_failed(approval.id, error)
            logger.error("exécution refusée (args_sha256) : approval=%s", approval.id)
            return _Ran(approval, _approbation_block(
                approval, outcome=mt.pick(mt.CARD_HASH_CHANGED)), error, True)
        mark = getattr(self.approvals, "mark_executing", None)
        if callable(mark):  # statut `executing` visible par l'hôte pendant que l'outil tourne (migration 091)
            try:
                mark(approval.id)  # l'objet `approval` de l'appelant reste tel quel (décideur, statut) : seul le backend change
            except Exception:  # noqa: BLE001 — un statut intermédiaire manqué ne bloque jamais l'exécution
                logger.exception("statut executing de l'approbation %s non posé", approval.id)
        kernel = self._k(approval.thread_id)
        app_server = None
        if approval.channel == APP_CHANNEL and getattr(kernel, "mcp_apps", None) is not None:
            # Carte d'un `tools/call` d'app MCP (docs/MCP-NEXT.md §4) : l'outil peut être réservé aux apps, donc absent
            # du registre du modèle ; le registre des apps sait quel serveur le sert.
            app_server = kernel.mcp_apps.server_of(approval.tool_name)
        spec = (kernel.mcp_apps.spec(app_server, approval.tool_name) if app_server is not None
                else kernel.tools.spec(approval.tool_name))
        ctx = ToolContext(thread_id=approval.thread_id, turn_index=approval.turn_index, channel=approval.channel,
                          sender=approval.decided_by, approval_id=approval.id, kernel=kernel)
        t0 = time.monotonic()
        try:
            result = (kernel.mcp_apps.call(app_server, approval.tool_name, approval.arguments, ctx)
                      if app_server is not None else kernel.tools.call(approval.tool_name, approval.arguments, ctx))
            result = kernel.invariants.redact_result(result, spec)
        except Exception as e:  # noqa: BLE001 — un outil qui plante n'est pas un bug de l'exécuteur
            error = f"{type(e).__name__}: {e}"
            approval = self.approvals.mark_failed(approval.id, error)
            logger.exception("exécution de l'outil approuvé a levé : approval=%s", approval.id)
            return _Ran(approval, _approbation_block(approval, outcome=mt.pick(mt.CARD_FAILED).format(error=error)), error, True,
                        int((time.monotonic() - t0) * 1000))

        ms = int((time.monotonic() - t0) * 1000)
        outcome = result.content
        if len(outcome) > RESULT_CHAR_LIMIT:
            # Début ET fin : la ligne qui compte d'une sortie de tests (« OK », « FAILED ») est à la fin — coupée, le
            # modèle relançait la commande (banc CLI de Lokkal, 23.10).
            head, tail = outcome[:RESULT_CHAR_LIMIT // 3], outcome[-(2 * RESULT_CHAR_LIMIT // 3):]
            outcome = (f"{head}\n" + mt.pick(mt.CARD_TRUNCATED).format(n=len(outcome) - len(head) - len(tail))
                       + f"\n{tail}")
        exit_code = None
        note = getattr(kernel, "note_outcome", None)  # règle de fin `conclude_on_green` : résultat d'un outil sur carte
        if callable(note):
            try:
                note(approval.thread_id, approval.tool_name, approval.arguments, result)
            except Exception:  # une trace de fin de chaîne ne bloque jamais la suite du tour
                logger.exception("note du résultat de l'approbation %s impossible", approval.id)
        if result.is_error:
            approval = self.approvals.mark_failed(approval.id, outcome)
            outcome = mt.pick(mt.CARD_FAILED).format(error=outcome)
        else:
            exit_code = result.exit_code
            approval = self.approvals.mark_executed(approval.id, outcome)
        return _Ran(approval, _approbation_block(approval, outcome=outcome, exit_code=exit_code), result.content,
                    result.is_error, ms, result)

    # ── décision prise PENDANT le tour (`local.approval_wait_s`, docs/HOST-EMBED.md) ──

    def hand_off(self, approval_id: str) -> threading.Event | None:
        """Appelé par `schedule_decision` à une décision : si un tour attend cette carte, il l'exécute lui-même (sous le
        verrou qu'il tient déjà) et rien n'est mis en file derrière lui ; rend l'événement posé à la fin de l'attente.
        None : personne n'attend (ou plus : le tour a fini d'attendre), la suite habituelle s'en charge."""
        with self._claim_lock:
            w = self._waiters.get(approval_id)
            if w is None or w.closed:
                return None
            w.handed = True
            return w.finished

    def settle_in_turn(self, approval: Approval, wait_s: float, cancelled: Callable[[], bool] | None = None, *,
                       poll_s: float = 0.25) -> InlineOutcome | None:
        """Le tour qui vient de poser la carte attend sa décision au plus `wait_s` secondes (`cancelled()` l'interrompt) ;
        décidée, la carte est exécutée ICI (une seule fois : `_claim`, comme la suite habituelle) et le résultat revient au
        tour. `wait_s` = 0 : lit seulement l'état de la carte (filet avant la réponse finale). None : toujours en
        attente (délai, annulation), carte d'un autre canal que le modèle, ou déjà prise par la suite habituelle."""
        get = getattr(self.approvals, "get", None)
        if not callable(get) or approval.channel in (ACCOUNT_CHANNEL, APP_CHANNEL, DIRECT_CHANNEL) \
                or approval.tool_name in self.handlers:
            return None
        waiter = _Waiter()
        with self._claim_lock:
            self._waiters[approval.id] = waiter
        try:
            deadline = time.monotonic() + max(0.0, wait_s)
            cur = get(approval.id)
            while cur is not None and cur.status == "pending":
                if cancelled is not None and cancelled():
                    return None
                if time.monotonic() >= deadline:
                    with self._claim_lock:
                        if not waiter.handed:
                            waiter.closed = True
                            return None
                    deadline = time.monotonic() + 2.0  # décision en route (hand_off) : on la prend, bornée
                time.sleep(max(0.001, min(poll_s, deadline - time.monotonic())))
                cur = get(approval.id)
            if cur is None or cur.status not in ("approved", "rejected"):
                return None  # expirée, retirée, déjà exécutée
            with tenant_scope(tenant_of_thread(cur.thread_id)):
                return self._settle(cur)
        finally:
            with self._claim_lock:
                self._waiters.pop(approval.id, None)
            waiter.finished.set()

    def _settle(self, approval: Approval) -> InlineOutcome | None:
        if approval.status == "rejected":
            if not self._claim(approval):
                return None
            self._track_repeat(approval, "refusé", False)
            return InlineOutcome("rejected", approval, _approbation_block(
                approval, outcome=self._rejected_outcome(approval)), "refusé")
        if self._already_settled(approval) or not self._claim(approval):
            logger.warning("carte %s déjà exécutée ou en cours : pas de 2e exécution", approval.id)
            return None
        from nokker.tools.scope import thread_scope

        with thread_scope(approval.thread_id):
            ran = self._call_approved(approval)
        self._track_repeat(ran.approval, ran.content, True)
        return InlineOutcome("failed" if ran.is_error else "executed", ran.approval, ran.block, ran.content, ran.result)

    def _track_repeat(self, approval: Approval, content: str, ran: bool) -> None:
        """Alimente le détecteur inter-tours (`core/approval_repeat.py`) : carte exécutée = +1 si identique à la
        précédente (outil, arguments, résultat), sinon la chaîne repart de 1 ; carte refusée = remise à zéro."""
        tracker = getattr(self._k(approval.thread_id), "approval_repeat", None)
        if tracker is None:
            return
        if ran:
            tracker.record(approval.thread_id, approval.tool_name, approval.arguments, content)
        else:
            tracker.reset(approval.thread_id)

    def _continue_with(self, approval: Approval, block: str, content: str = "", is_error: bool = False, *,
                       ran: bool = False, ms: int | None = None, declined: bool = False) -> Turn | None:
        if approval.channel == ACCOUNT_CHANNEL:
            return None  # refus d'une carte de compte : rien à faire, aucun tour modèle
        if approval.channel == APP_CHANNEL:
            # Appel d'une app MCP : le résultat va au client (`tool.result` avec `approval_id`), jamais au modèle — ni
            # note « hors tour », ni tour relancé.
            if self.on_direct is not None:
                self.on_direct(approval, content, is_error)
            return None
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
        if declined and self.on_declined is not None:
            try:
                self.on_declined(approval)
            except Exception:  # un événement manqué ne bloque pas la reprise du tour
                logger.exception("publication du refus de l'approbation %s impossible", approval.id)
        self._track_repeat(approval, content, ran)
        # Fil d'un passage d'agent : son noyau repose l'identité du passage (son propriétaire), pas celle du décideur.
        kernel = self._k(approval.thread_id)
        return kernel.run_turn(TurnInput(thread_id=approval.thread_id, user_text=block,
                                         channel=approval.channel, sender=approval.decided_by,
                                         control=APPROVAL_CONTINUE, lang=self._lang(approval)))


__all__ = ["CHILD_GONE", "ApprovalExecutor", "InlineOutcome"]
