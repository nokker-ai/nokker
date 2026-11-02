"""Boucle d'agent du noyau — l'ordre du tour de PLAN-P1 §2.3, et rien d'autre.

La boucle ne prend aucune décision de fond : elle appelle les ports dans l'ordre et applique la décision
la plus sévère. La sûreté est dans `Invariants` + `Gates` (pilier b), le choix du palier dans `Policy`
(pilier c), l'assemblage dans `ContextEngine` (pilier a). Issue de harness.py (banc P0 : 18/14/17 sur 20).

Outils natifs du noyau (jamais délégués à une source) :
  - `escalate_to_human` : exécuté par l'Escalator, marque le tour escaladé ;
  - `tool_find`         : charge des outils non cœur pour la suite du tour (schémas à la demande).
"""
from __future__ import annotations

import hashlib
import inspect
import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from nokker.tools.select import relevant_tools
from nokker.core.ports import (
    Approvals, ContextEngine, Escalator, Gates, Invariants, Ledger, MemoryPort, Policy, Provider, Recall,
    RouteDecision, RouteState, Store, StreamInterrupted, ToolContext, ToolRegistry, ToolSpec, TurnObserver,
    TurnStream, call_effect,
)
from nokker.core.controls import STEER_MAX_CHARS, Steer, TurnControls
from nokker.core.live_input import LiveToolInputs
from nokker.core.stream import ScrubbedDeltas, holdback_for
from nokker.core.turn_lock import TurnLocks
from nokker.images.render import has_images, materialize
from nokker.core.turn import (
    Decision, Effect, Gate, ImageRef, InputCheck, LLMCall, Message, Tier, ToolCall, ToolResult, Turn, TurnInput, Verdict,
)
from nokker.ledger.attribution import merge_tags, spend_tag
from nokker.tenancy.context import submit_in_context, tenant_of_thread

if TYPE_CHECKING:
    from nokker.tenancy.limits import TenantLimits

ESCALATE_TOOL = "escalate_to_human"
# Canal d'un appel d'outil fait par le propriétaire, hors tour (POST /v1/local/tools/call, docs/LOKKAL.md §7.4.4) : sa
# carte éventuelle ne relance pas la boucle après décision.
DIRECT_CHANNEL = "local-direct"
NOTE_CHARS = 600  # début du résultat repris dans la ligne « [hors tour] » du tour suivant
FIND_TOOL = "tool_find"
MEMORY_TOOLS = frozenset({"memory_search", "memory_get"})
CLARIFY_TOOL = "clarify"
# Ajoutée au dernier appel quand la limite d'appels est atteinte (jamais gardée dans l'historique).
WRAP_UP_NOTE = ("[Limite d'appels d'outils atteinte pour ce tour] Réponds maintenant, sans outil, avec ce que tu as "
                "déjà trouvé ; si une partie manque, dis-le simplement et propose de continuer.")
BUDGET_NOTE = ("[noyau] Il te reste {left} appels sur {total} pour ce tour. Termine maintenant : supprime les fichiers "
               "temporaires que tu as créés (scripts d'essai, reproductions), lance les tests concernés une dernière "
               "fois, puis conclus.")
# Pause et steer (docs/CLIENT-API.md §Pause et steer), derrière `Kernel.steer_enabled` : messages `user` ajoutés au tour
# au point de contrôle qui précède un appel modèle, gardés dans l'historique (format : docs/CONTEXT-FORMAT.md).
STEER_BLOCK = "[Message de l'utilisateur pendant le tour]\n{text}\n[/Message]"
RESUME_NOTE = "[noyau] Le tour reprend après une pause demandée par l'utilisateur : des fichiers ont pu changer entre-temps."
STALL_NOTE = ("[noyau] Tu viens de relancer `{name}` avec les mêmes arguments et le même résultat : ça ne fait pas "
              "avancer. Change d'approche (relis le code concerné, teste une autre hypothèse) ou conclus.")

logger = logging.getLogger("nokker.core.loop")

# Rappel du tour lancé en parallèle de la préparation du contexte (20.10.2026) : mesuré sur le canari, le MCP
# `sokkan-memory` répond en 1,4-1,5 s à chaud (dont ~1,1 s de reranker), 2,1 s session froide, pics à 8 s.
_RECALL_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="nokker-recall")


class TurnCancelled(Exception):
    """Annulation reçue PENDANT un appel modèle en flux (D6, e2e du 1.11) : levée par le relais des morceaux
    (`Kernel._complete`) au premier morceau qui arrive après `cancel()`. Elle remonte à travers le provider — qui sort
    de son `with client.stream(...)`, donc ferme la réponse HTTP de l'amont — jusqu'à la boucle, qui finit le tour sur
    « Annulé à ta demande. ». Jamais prise pour une panne d'amont (pas de repli de palier, pas d'escalade)."""


CANCEL_REPLY = "Annulé à ta demande."

# Motif d'une conclusion forcée, tel qu'il s'affiche avec les remarques de la relecture (`_final_review`).
_FORCED_WHY = {"loop:max_calls": "le tour a atteint sa limite d'appels", "loop:stalled": "le tour tournait en rond"}


def worst(verdicts: list[Verdict]) -> Decision:
    return max((v.decision for v in verdicts), key=lambda d: d.severity, default=Decision.ALLOW)


def error_signature(result: ToolResult) -> str:
    return f"{result.name}:{result.content[:120]}"


_VOLATILE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:s|ms|sec|seconds)\b|0x[0-9a-f]{6,}|\b\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\b",
                       re.IGNORECASE)


def stall_signature(call: ToolCall, result: ToolResult) -> str:
    """Signature d'un appel d'outil et de son résultat, sans ce qui change d'une exécution à l'autre (durées
    « in 0.53s », adresses 0x…, heures) : deux pytest identiques sur un code inchangé ont la même signature."""
    args = json.dumps(call.arguments, ensure_ascii=False, sort_keys=True, default=str)
    body = hashlib.sha1(_VOLATILE.sub("", result.content or "").encode("utf-8", "replace")).hexdigest()
    return f"{call.name}|{args}|{body}"


def _short_error(e: BaseException | None) -> str:
    """Motif lisible d'une panne d'amont : jamais une page HTML d'erreur recopiée (vu en réel 19.10 : le
    message d'escalade montrait le `<!DOCTYPE html>` d'un 502 du Router)."""
    text = str(e) if e is not None else "erreur inconnue"
    low = text.lower()
    cut = min((i for i in (low.find("<!doctype"), low.find("<html")) if i >= 0), default=-1)
    if cut >= 0:
        text = text[:cut].rstrip(" :") + " (page d'erreur HTML)"
    text = " ".join(text.split())
    return text if len(text) <= 200 else text[:199] + "…"


def prompt_text(assembled) -> str:
    """Le prompt tel qu'un template de chat le rend, à l'ordre près : message système, schémas d'outils (Qwen les
    rend dans le bloc système, donc juste après lui), puis le reste des messages. Sert à mesurer le préfixe
    réutilisable d'un appel à l'autre — pas à construire la requête."""
    msgs = [json.dumps(m, ensure_ascii=False, sort_keys=True) for m in assembled.messages]
    tools = json.dumps(assembled.tools, ensure_ascii=False, sort_keys=True)
    return "\n".join([*msgs[:1], tools, *msgs[1:]])


def reusable_tokens(previous: str | None, current: str, prompt_tokens: int) -> int | None:
    """Tokens du prompt `current` identiques en tête à `previous`, au prorata des caractères (le tokenizer de
    l'amont n'est pas connu du noyau). None sans appel précédent."""
    if previous is None or not current:
        return None
    n = 0
    for a, b in zip(previous, current):
        if a != b:
            break
        n += 1
    return round(prompt_tokens * n / len(current))


def accepts_on_delta(provider: object, keyword: str = "on_delta") -> bool:
    """Vrai si `provider.complete` DÉCLARE le mot-clé `on_delta` (streaming, `core/ports.py::Provider`), ou `keyword`
    (`on_tool_delta`, écriture en direct X22). Pas de `**kwargs` accepté comme preuve : un faux provider ou un Mock ne
    doit pas recevoir un argument qu'il ne comprend pas."""
    try:
        return keyword in inspect.signature(provider.complete).parameters  # type: ignore[attr-defined]
    except (TypeError, ValueError, AttributeError):
        return False


@dataclass
class Kernel:
    store: Store
    context: ContextEngine
    memory: MemoryPort
    invariants: Invariants
    gates: Gates
    escalator: Escalator
    policy: Policy
    providers: dict[Tier, Provider]
    tools: ToolRegistry
    ledger: Ledger
    # P1 (18.10.2026) : port optionnel (docs/APPROVALS.md). `None` = comportement inchangé (la carte
    # reste seulement dans `turn.pending_approvals`, aucun test existant ne casse). Branché, `_tool_step`
    # pose une VRAIE carte d'approbation (défi/signature/TOTP) sur `Decision.HITL`.
    approvals: Approvals | None = None
    max_calls: int = 8
    # Plafond de jetons de sortie par appel modèle (None = défaut du provider, 2048). Session de code (Nokker Lokkal) :
    # 16384, comme l'agent de référence du banc G1 ; à 2048, le 397B était coupé en pleine réflexion (L01, 23.10).
    max_tokens: int | None = None
    # Réponse coupée par la limite (finish_reason « length ») SANS appel d'outil : ce n'est pas une réponse, le
    # noyau demande de continuer, au plus ce nombre de fois par tour.
    truncation_retries: int = 2
    # Longue tâche (session de code) : à cette fraction de `max_calls`, le noyau rappelle au modèle qu'il doit conclure
    # (ranger, tester, répondre) ; None = jamais. Banc SWE-bench du 24.10 : 12 tâches sur 31 allaient jusqu'au plafond
    # sans conclure, avec 59 % des jetons du banc.
    budget_notice_at: float | None = None
    # Stagnation : un même appel d'outil (nom + arguments) qui rend le même résultat (durées et adresses ignorées)
    # compte comme une répétition. À la première, une note demande de changer d'approche ; à `stall_limit`
    # répétitions dans le tour, le noyau conclut sans outils (règle `loop:stalled`). None = pas de détection.
    stall_limit: int | None = None
    # MCP Apps (drapeau `mcp_apps`, docs/MCP-NEXT.md) : `nokker.tools.mcp_apps.AppRegistry`, posé par `build_kernel` ;
    # None = aucune app enregistrée, aucun `tool.app`, `app_tool_call` refuse.
    mcp_apps: Any = None
    # Fils dont le tour en cours doit s'arrêter (POST /v1/threads/{id}/cancel, client terminal) : vérifié avant chaque
    # appel modèle et chaque outil ; le tour finit sur « annulé », sans erreur d'amont.
    _cancelled: set[str] = field(default_factory=set, init=False, repr=False)
    # Appels à effet faits hors tour par le propriétaire : une ligne chacun, ajoutée au contexte reçu du prochain tour
    # du fil (sinon le modèle continuerait sur des fichiers restaurés sans le savoir).
    _owner_notes: dict[str, list[str]] = field(default_factory=dict, init=False, repr=False)
    _notes_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def cancel(self, thread_id: str) -> None:
        self._cancelled.add(thread_id)
        self.controls.poke()  # un tour en pause se réveille et finit sur « annulé »

    # ─────────────── pause, reprise, steer (core/controls.py) ───────────────

    def pause(self, thread_id: str) -> None:
        self.controls.pause(thread_id)

    def resume(self, thread_id: str) -> bool:
        return self.controls.resume(thread_id)

    def pause_account(self, tenant: str, account: str) -> None:
        self.controls.pause_account(tenant, account)

    def resume_account(self, tenant: str, account: str) -> bool:
        return self.controls.resume_account(tenant, account)

    def steer(self, thread_id: str, text: str) -> Steer | None:
        """Un message pour le tour en cours du fil, consommé avant son prochain appel modèle. None : steer éteint
        (`steer_enabled`), ou aucun tour ne tourne sur le fil. Caviardé comme un message (invariants d'entrée)."""
        if not self.steer_enabled:
            return None
        return self.controls.steer(thread_id, self.invariants.check_input(text[:STEER_MAX_CHARS]).text)

    def _observe_opt(self, method: str, *args) -> None:
        """Méthode d'observateur OPTIONNELLE (événements de pause et de steer) : appelée seulement si elle existe."""
        if self.observer is not None and hasattr(self.observer, method):
            self._observe(method, *args)

    def _pause_point(self, turn: Turn, at: str, tool: str | None = None) -> bool:
        """Point de contrôle de la boucle : en pause, le tour attend ici (rien à moitié fait) jusqu'à la reprise ou
        l'annulation. Rend vrai si le tour a attendu."""
        tenant = tenant_of_thread(turn.thread_id)
        if turn.thread_id in self._cancelled or not self.controls.is_paused(turn.thread_id, tenant, turn.sender):
            return False
        where = {"at": at, **({"tool": tool} if tool else {})}
        self._observe_opt("turn_paused", turn, dict(where))
        t0 = time.monotonic()
        self.controls.wait(turn.thread_id, tenant, turn.sender, lambda: turn.thread_id in self._cancelled)
        ms = int((time.monotonic() - t0) * 1000)
        cancelled = turn.thread_id in self._cancelled
        self._log_verdicts(turn, [Verdict(gate=Gate.SWITCH, decision=Decision.ALLOW, layer=0, rule="loop:paused",
                                          reason=f"pause avant {'l’outil ' + tool if tool else 'l’appel modèle'}, "
                                                 f"{ms / 1000:.1f} s{' (annulé pendant la pause)' if cancelled else ''}")])
        if not cancelled:
            self._observe_opt("turn_resumed", turn, {**where, "paused_ms": ms})
        return True

    def _take_steers(self, turn: Turn, resumed: bool) -> None:
        """Avant un appel modèle : mention de reprise (si le tour a déjà commencé) puis steers en attente, dans l'ordre
        d'arrivée. Steer éteint : rien n'est ajouté (ce que voit le modèle ne change pas)."""
        if not self.steer_enabled:
            return
        if resumed and turn.messages:
            turn.messages.append(Message(role="user", content=RESUME_NOTE))
        for s in self.controls.take_steers(turn.thread_id):
            turn.messages.append(Message(role="user", content=STEER_BLOCK.format(text=s.text)))
            self._observe_opt("turn_steered", turn, {"steer_id": s.id})

    def cancel_requested(self, thread_id: str) -> bool:
        """Le tour du fil doit s'arrêter (sans consommer la demande : la boucle la lira après l'outil en cours)."""
        return thread_id in self._cancelled

    def note_owner_call(self, thread_id: str, name: str, arguments: dict, result: ToolResult) -> None:
        args = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
        head = (result.content or "")[:NOTE_CHARS]
        self.add_owner_note(thread_id, f"[hors tour] le propriétaire a appelé {name} {args} : "
                                       f"{'ÉCHEC — ' if result.is_error else ''}{head}")

    def add_owner_note(self, thread_id: str, line: str) -> None:
        with self._notes_lock:
            self._owner_notes.setdefault(thread_id, []).append(self.invariants.scrub_output(line))

    def direct_call(self, thread_id: str, name: str, arguments: dict, sender: str | None = None) -> ToolResult | str:
        """Un appel d'outil du propriétaire, sans tour modèle (docs/LOKKAL.md §7.4.4) : mêmes portes que la boucle
        (effet de CET appel, règles, invariants, caviardage). Rend le résultat, ou l'id de la carte si l'appel attend
        une approbation (le résultat arrive alors par l'exécuteur de cartes). Un refus est un résultat `is_error`."""
        spec = self.tools.spec(name)
        tc = ToolCall(id=f"direct-{time.monotonic_ns()}", name=name, arguments=arguments)
        turn = Turn(thread_id=thread_id, index=self.store.next_index(thread_id), input=InputCheck(text=""),
                    channel=DIRECT_CHANNEL, sender=sender)
        verdicts = self.gates.action(tc, spec, turn)
        decision = worst(verdicts)
        rule = next((v for v in verdicts if v.decision is decision), None)
        why = f"{rule.rule}: {rule.reason}" if rule else decision.value
        if decision is Decision.ALLOW:
            ctx = ToolContext(thread_id=thread_id, turn_index=turn.index, channel=DIRECT_CHANNEL, sender=sender)
            result = self.invariants.redact_result(self.tools.call(name, arguments, ctx), spec)
            if call_effect(spec, arguments) is not Effect.READ:
                self.note_owner_call(thread_id, name, arguments, result)
            return result
        if decision is Decision.HITL and self.approvals is not None:
            return self.approvals.request(turn, tc, rule, self._card_advice(turn, tc, rule)).id
        return ToolResult(call_id=tc.id, name=name, is_error=True, content=f"refusé par le noyau ({why})",
                          reason=rule.code if rule is not None and decision is Decision.DENY else None)

    def app_tool_call(self, thread_id: str, server: str, name: str, arguments: dict,
                      sender: str | None = None) -> ToolResult | str:
        """Un `tools/call` demandé par une app MCP affichée (docs/MCP-NEXT.md §4) : mêmes portes que la boucle et que
        `direct_call` (effet de CET appel, règles, invariants, carte prouvée), même caviardage. Contrairement à
        `direct_call`, rien n'entre dans le contexte du modèle (ni note « hors tour », ni relance de tour après une
        carte) : l'app n'écrit jamais dans la fenêtre du modèle. Rend le résultat, ou l'id de la carte."""
        from nokker.tools.mcp_apps import APP_CHANNEL

        reg = self.mcp_apps
        spec = reg.spec(server, name) if reg is not None else None
        tc = ToolCall(id=f"app-{time.monotonic_ns()}", name=name, arguments=arguments)
        if spec is None:
            return ToolResult(call_id=tc.id, name=name, is_error=True, content=f"outil inconnu : {name}")
        turn = Turn(thread_id=thread_id, index=self.store.next_index(thread_id), input=InputCheck(text=""),
                    channel=APP_CHANNEL, sender=sender)
        verdicts = self.gates.action(tc, spec, turn)
        decision = worst(verdicts)
        rule = next((v for v in verdicts if v.decision is decision), None)
        if decision is Decision.ALLOW:
            ctx = ToolContext(thread_id=thread_id, turn_index=turn.index, channel=APP_CHANNEL, sender=sender)
            result = self.invariants.redact_result(reg.call(server, name, arguments, ctx), spec)
            result.app = None  # une app n'en ouvre pas une autre (v1)
            return result
        if decision is Decision.HITL and self.approvals is not None:
            return self.approvals.request(turn, tc, rule, self._card_advice(turn, tc, rule)).id
        why = f"{rule.rule}: {rule.reason}" if rule else decision.value
        return ToolResult(call_id=tc.id, name=name, is_error=True, content=f"refusé par le noyau ({why})",
                          reason=rule.code if rule is not None and decision is Decision.DENY else None)

    def script_turn(self, ctx: ToolContext) -> Turn:
        """Le tour d'un appel d'outil fait par un script `execute_code` quand l'appel ne vient pas d'un tour (carte
        d'`execute_code` approuvée puis exécutée par `approvals/executor.py`) : même fil, même index, même canal, le
        décideur comme expéditeur — comme `direct_call`."""
        if isinstance(ctx.turn, Turn):
            return ctx.turn
        return Turn(thread_id=ctx.thread_id, index=ctx.turn_index, input=InputCheck(text=""), channel=ctx.channel,
                    sender=ctx.sender)

    def script_tool_call(self, name: str, arguments: dict, turn: Turn, ctx: ToolContext) -> dict[str, Any]:
        """Un appel d'outil venu du script d'`execute_code` (nokker/tools/execute_code.py) : les MÊMES portes que
        `_tool_step` (effet de CET appel, règles, invariants, carte prouvée, escalade), le MÊME caviardage
        (`redact_result`), sur le tour qui a lancé le script. Rend `{status, content[, approval_id]}` pour le script :
        `ok` | `error` (l'outil a échoué) | `pending` (carte posée, rien d'exécuté : le script ne l'attend pas) |
        `refused` (règle, mode plan, escalade). Ni cache de lecture, ni réservoir : le bloc d'`execute_code` est
        borné par lui-même et passe ensuite par l'admission ordinaire du tour."""
        spec = self.tools.spec(name)
        if spec is None:
            return {"status": "error", "content": f"outil inconnu : {name}"}
        tc = ToolCall(id=f"xc-{time.monotonic_ns()}", name=name, arguments=arguments)
        verdicts = self.gates.action(tc, spec, turn)
        if self.provenance is not None:  # un script qui lit un mail puis envoie : même politique que la boucle
            verdicts = self.provenance.check(tc, spec, turn) + verdicts
        self._log_verdicts(turn, verdicts)
        decision = worst(verdicts)
        if decision is Decision.ALLOW:
            inner = ToolContext(thread_id=ctx.thread_id, turn_index=ctx.turn_index, channel=ctx.channel,
                                sender=ctx.sender, kernel=self, turn=turn)
            result = self.invariants.redact_result(self.tools.call(name, arguments, inner), spec)
            if self.provenance is not None:
                self.provenance.record(turn, tc, spec, result)
            return {"status": "error" if result.is_error else "ok", "content": result.content or ""}
        rule = next((v for v in verdicts if v.decision is decision), None)
        why = f"{rule.rule}: {rule.reason}" if rule else decision.value
        if decision is Decision.HITL:
            turn.pending_approvals.append(tc)
            if self.approvals is not None:
                approval = self.approvals.request(turn, tc, rule, self._card_advice(turn, tc, rule))
                return {"status": "pending", "approval_id": approval.id,
                        "content": f"EN ATTENTE D'APPROBATION HUMAINE ({why}) [approval:{approval.id}]. "
                                   "L'action n'a pas été exécutée."}
            return {"status": "pending", "content": f"EN ATTENTE D'APPROBATION HUMAINE ({why}). "
                                                    "L'action n'a pas été exécutée."}
        if decision is Decision.ESCALATE:
            text = self.escalator.escalate(turn, why)
            turn.escalated = True
            return {"status": "refused", "content": f"ESCALADÉ À UN HUMAIN ({why}). {text}"}
        return {"status": "refused", "content": f"REFUSÉ par le noyau ({why})."}

    def _card_advice(self, turn: Turn, tc: ToolCall, rule: Verdict | None) -> str | None:
        """Avis du juge sur une carte (X8) : seulement si `advisor` est branché et que la carte est forte ; jamais une
        panne qui bloque la carte."""
        if self.advisor is None or rule is None:
            return None
        try:
            advice = self.advisor.advise(turn, tc.name, tc.arguments, rule.reason, rule.strength)
        except Exception:  # noqa: BLE001 — une carte sans avis reste une carte
            logger.warning("avis de carte en échec", exc_info=True)
            return None
        self._record_judge_call(turn, self.advisor, getattr(self.advisor, "last_model", ""),
                                getattr(self.advisor, "last_usage", None),
                                getattr(self.advisor, "last_latency_s", 0.0), "juge : avis sur une carte forte")
        return advice

    def _auto_review_eligible(self, rule: Verdict | None, tc: ToolCall, effect: Effect) -> bool:
        """La carte qui va être posée passe-t-elle par le relecteur automatique (§8) ? Drapeau, backend capable, et
        hors de la liste fermée (`auto_review.excluded`)."""
        if self.auto_reviewer is None or rule is None or not hasattr(self.approvals, "decide_auto"):
            return False
        from nokker.approvals.auto_review import excluded
        from nokker.core.ports import Approval

        probe = Approval(id="", thread_id="", turn_index=0, channel="", tool_name=tc.name,
                         arguments=dict(tc.arguments or {}), args_sha256="", strength=rule.strength or "standard",
                         rule=rule.rule)
        return excluded(probe, effect.value) is None

    def _auto_review(self, turn: Turn, tc: ToolCall, spec: Any, ctx: ToolContext, approval: Any,
                     effect: Effect) -> ToolResult | None:
        """Relit la carte qui vient d'être posée. Rend le résultat à donner au modèle (outil exécuté, ou refus motivé),
        ou None quand la carte reste à l'humain (escalade, doute, panne) : le chemin habituel d'attente continue."""
        from nokker.approvals.auto_review import REFUSED_RESULT

        try:
            res = self.auto_reviewer.review(turn, approval, call_id=tc.id, approvals=self.approvals)
        except Exception:  # noqa: BLE001 — un relecteur qui tombe laisse la carte à l'humain
            logger.warning("relecteur automatique en échec", exc_info=True)
            res = None
        if res is not None and res.usage:
            self._record_judge_call(turn, self.auto_reviewer, res.model, res.usage, res.latency_s,
                                    "relecteur automatique : carte standard")
        decision = res.decision if res is not None else "escalate"
        reason = res.reason if res is not None else "relecteur en échec : à l'humain"
        try:
            decided = self.approvals.decide_auto(approval.id, decision, reason, model=getattr(res, "model", ""),
                                                 by_rule=bool(getattr(res, "by_rule", True)), effect=effect.value)
        except Exception:  # noqa: BLE001 — carte déjà décidée ailleurs, ou refusée par la liste fermée
            logger.warning("relecteur automatique : décision non appliquée", exc_info=True)
            self.approvals.notify_pending(approval)
            return None
        label = {"approve": "approuvée", "reject": "refusée", "escalate": "laissée à l'humain"}[decision]
        self._log_verdicts(turn, [Verdict(gate=Gate.ACTION, decision=Decision.ALLOW if decision == "approve" else
                                          (Decision.DENY if decision == "reject" else Decision.HITL), layer=2,
                                          rule="approvals:auto_review", reason=f"{label} : {reason}",
                                          judge_model=getattr(res, "model", None) or None,
                                          latency_s=getattr(res, "latency_s", 0.0), subject=tc.id)])
        if decision == "escalate":
            self.approvals.notify_pending(decided)
            return None
        turn.approval_ids.append(approval.id)
        if tc in turn.pending_approvals:  # décidée : plus rien n'attend l'humain pour cet appel
            turn.pending_approvals.remove(tc)
        if decision == "reject":
            return ToolResult(call_id=tc.id, name=tc.name, content=REFUSED_RESULT.format(
                reason=reason, approval_id=approval.id), is_error=False, reason="auto_review_rejected")
        # Approuvée : exécutée DANS le tour, comme une action permise (le modèle voit le résultat de l'outil).
        try:
            result = self.tools.call(tc.name, tc.arguments, ctx)
        except Exception as e:  # noqa: BLE001 — même contrat que l'exécuteur de cartes
            self.approvals.mark_failed(approval.id, f"{type(e).__name__}: {e}"[:500])
            raise
        result.call_id = tc.id
        redacted = self.invariants.redact_result(result, spec)
        if result.is_error:
            self.approvals.mark_failed(approval.id, (redacted.content or "")[:500])
        else:
            self.approvals.mark_executed(approval.id, (redacted.content or "")[:2000])
        suffix = self._observe("tool_executed", turn, tc, spec, redacted)
        admit_in = getattr(self.context, "admit_result_in", None)
        admitted = (admit_in(turn.thread_id, turn.index, redacted) if admit_in is not None
                    else self.context.admit_result(redacted))
        if suffix:
            admitted.content = admitted.content + str(suffix)
        return admitted

    def _record_judge_call(self, turn: Turn, role: Any, model: str, usage: dict | None, latency_s: float,
                           reason: str) -> LLMCall | None:
        """Les jetons du juge comptent au tour (`turn.cost`, X2) et au ledger, comme un appel modèle : palier
        `role.tier` (celui dont le modèle sert de juge), motif « juge : … ». Rend l'appel compté (None sans usage)."""
        if not usage:
            return None
        from nokker.providers.sokkan import _parse_usage

        call = LLMCall(tier=getattr(role, "tier", None) or Tier.SHIP, model=model or getattr(role, "model_name", ""),
                       reasoning=False, prefix_sha256="", layer_tokens={}, usage=_parse_usage(usage),
                       latency_s=latency_s, route_reason=reason)
        turn.calls.append(call)
        try:
            self.ledger.record(turn.thread_id, turn.index, call)
        except Exception:  # noqa: BLE001 — un ledger qui tombe ne coûte pas le tour
            logger.warning("ledger : appel du juge non écrit", exc_info=True)
        return call

    def _run_review(self, turn: Turn, final: bool = False):
        """Une relecture du diff du tour (X8), comptée au tour et tracée (`judge:review`). None : juge en panne ou
        réponse illisible (rien à relire aussi : `reviewer.review` rend None sans diff)."""
        try:
            res = self.reviewer.review(turn)
        except Exception:  # noqa: BLE001 — un juge qui tombe ne relit pas, le tour finit
            logger.warning("relecture en échec", exc_info=True)
            return None
        if res is None:
            return None
        self._record_judge_call(turn, self.reviewer, res.model, res.usage, res.latency_s,
                                "juge : relecture du diff avant « terminé »" if not final
                                else "juge : relecture du diff d'une conclusion forcée")
        verdict = "OK" if res.ok else ("à corriger, non renvoyé à l'auteur (plus d'appel pour corriger), remarques "
                                       "jointes à la réponse" if final else "à corriger")
        self._log_verdicts(turn, [Verdict(
            gate=Gate.SWITCH, decision=Decision.ALLOW if res.ok else Decision.REDO_DEEP, layer=2, rule="judge:review",
            reason=(f"relecture du diff : {verdict}" + ("" if res.ok else f"\n{res.remarks}"))[:1500],
            judge_model=res.model, latency_s=res.latency_s, subject="reply")])
        return res

    def _review(self, turn: Turn, candidate: str, rounds: int, call_index: int) -> str | None:
        """Relecture du diff avant « terminé » (X8). Rend la note à renvoyer à l'auteur, ou None (rien à relire,
        relecture OK, juge muet, ou plus de tour de relecture). Plus assez d'appels pour corriger : relecture finale
        sans renvoi (`_final_review`), faite par l'appelant."""
        if self.reviewer is None or turn.escalated or rounds >= int(getattr(self.reviewer, "rounds", 1)):
            return None
        if call_index + 2 > self.max_calls:
            return None
        res = self._run_review(turn)
        if res is None or res.ok:
            return None
        from nokker.qa.review import REVIEW_NOTE

        return REVIEW_NOTE.format(remarks=res.remarks or "(sans détail)")

    def _final_review(self, turn: Turn, reply: str, why: str) -> str:
        """Relecture d'une réponse qui ne peut plus être corrigée (lot fix-batch-1) : conclusion forcée (plafond
        d'appels, stagnation) ou réponse au dernier appel. UNE relecture, jamais de renvoi à l'auteur — le plafond est
        atteint, un renvoi serait une boucle sans budget ; les remarques sont jointes à la réponse, visibles. Juge muet
        ou en panne sur un tour qui a écrit : la réponse dit que le diff n'a pas été relu. Pas de relecteur, rien
        d'écrit, tour escaladé : la réponse part telle quelle."""
        from nokker.qa.review import FINAL_REVIEW_NOTE, NOT_REVIEWED_NOTE, turn_diff

        if self.reviewer is None or turn.escalated or not reply.strip():
            return reply
        if not turn_diff(turn, int(getattr(self.reviewer, "max_diff_chars", 24000))):
            return reply
        res = self._run_review(turn, final=True)
        if res is None:
            self._log_verdicts(turn, [Verdict(gate=Gate.SWITCH, decision=Decision.ALLOW, layer=2,
                                              rule="judge:review:skipped",
                                              reason=f"diff non relu ({why}) : relecteur muet ou en panne",
                                              subject="reply")])
            return reply + NOT_REVIEWED_NOTE
        if res.ok:
            return reply
        return reply + FINAL_REVIEW_NOTE.format(why=why, remarks=res.remarks or "(sans détail)")

    def _take_cancel(self, thread_id: str) -> bool:
        if thread_id in self._cancelled:
            self._cancelled.discard(thread_id)
            return True
        return False
    # Budget d'appels mémoire explicites par tour (le noyau fait déjà le rappel automatique) : au-delà, le noyau
    # répond sans exécuter. Mesuré au banc (17.10.2026) : identite-meta 9-14 memory_search/memory_get par tour.
    memory_calls_per_turn: int = 3
    # Schémas d'outils envoyés (tools/select.py) : « relevance » = cœurs + outils de la fenêtre + familles demandées par
    # le message (amont sans cache : chaque token payé à chaque appel) ; « all » = tout, sans plafond (amont à cache de
    # préfixe). Remplace preload_all_tools/preload_max_tools (21.10.2026) : avec les 4 outils des bulles, le canari
    # dépassait le plafond de 40 et ne préchargeait plus RIEN (trouvé par le banc de conversations).
    tool_loading: str = "relevance"
    # Outils utilisés dans les N derniers tours de la fenêtre : rechargés pour une relance (« et Lausanne ? »).
    tool_reuse_turns: int = 5
    recall_k: int = 5
    # Attente maximale du rappel, comptée depuis son lancement (début du tour) : au-delà, le tour part sans
    # rappel (verdict `memory:recall_late`) — le modèle garde memory_search s'il en a besoin. Un pic du serveur
    # mémoire ne retarde plus la réponse que de ce budget, pas des 20 s du timeout MCP.
    recall_budget_s: float = 2.5
    # P1 bulles (docs/BUBBLES.md §3/§6) : observateur optionnel (`nokker/bubbles/hub.py::BubbleHub`) —
    # contexte reçu d'autres fils, événements turn.*/tool.*, producteurs de bulles. `None` = inchangé.
    observer: TurnObserver | None = None
    # Magasin des images jointes (X7, `nokker/images/store.py::ImageStore`) : le noyau y lit les octets juste avant
    # l'appel modèle, pour un palier avec vision. `None` = aucune image ne peut être lue (note texte au modèle).
    images: Any = None
    # Pièces jointes texte/dossier/url (`nokker/attachments.py::TextAttachments`), posé quand
    # `text_attachments_enabled` est vrai. `None` = format X7 seul (images), comportement d'avant.
    text_attachments: Any = None
    # Plafonds des testeurs (docs/TENANCY.md §5, `nokker/tenancy/limits.py::TenantLimits`) : vérifiés AVANT le tour,
    # jamais pour le locataire interne. `None` = aucun plafond (tests, bancs).
    limits: TenantLimits | None = None
    # Budget des projets d'équipe (docs/LOKKAL.md §7.15, `nokker/team/budget.py::ProjectBudget`) : un tour rattaché à un
    # projet porte son étiquette au ledger ; au-delà du plafond du projet, refusé AVANT tout appel modèle (comme
    # `limits`). `None` = équipe éteinte, rien n'est attribué.
    project_budget: Any = None
    # Quota commun du foyer (lot X23, `nokker/household/quota.py::HouseholdQuota`) : un tour d'un membre porte
    # l'étiquette du foyer au ledger (même mécanisme que le budget) ; au-delà d'un plafond du foyer ou du membre, refusé
    # AVANT tout appel modèle. `None` = foyer éteint, rien n'est attribué.
    household: Any = None
    # Arrêt propre demandé par l'appelant (passage d'un agent Crew, lot X14 : budget d'appels ou de coût atteint) :
    # appelé avant chaque appel modèle ; un motif (chaîne) termine le tour sur « Arrêté par le noyau : <motif>. »,
    # `turn.error = "stopped"`, sans nouvel appel. None = jamais.
    stop_check: Callable[[Turn], str | None] | None = None
    # Juge allumé (X8, docs/LOKKAL.md §7.17, `nokker/qa/review.py`) : `reviewer` relit le diff du tour avant la réponse
    # (renvoi à l'auteur avec les remarques, `reviewer.rounds` fois au plus) ; `advisor` met un avis et un niveau de
    # risque sur les cartes fortes. `None` = éteints (défaut).
    reviewer: Any = None
    advisor: Any = None
    # Rubrique et objectifs (docs/OUTCOMES-GOAL.md, `nokker/goals/service.py::GoalService`, drapeaux `outcomes_enabled`
    # et `goals_enabled`) : commandes `/goal`, réponse au tour `goal_check`, passes notées par un correcteur séparé
    # dans le tour, objectif noté et enchaîné en fin de tour. `None` = éteints (défaut) : rien ne change.
    goals: Any = None
    # Relecteur automatique des cartes `standard` (docs/APPROVALS.md §8, `nokker/approvals/auto_review.py`,
    # drapeau `auto_review`) : approuve, refuse ou laisse à l'humain une carte de la liste permise. `None` = éteint
    # (défaut) : aucune carte relue, chemin et textes inchangés.
    auto_reviewer: Any = None
    # Traces d'entraînement (docs/TRACES.md, `nokker/traces/recorder.py::TraceRecorder`) : chaque appel modèle (messages
    # exacts envoyés, schémas, suite) gardé à part, écrit à la fin du tour. `None` = rien n'est enregistré (défaut ;
    # `traces_record: false`). Jamais d'effet sur ce que voit le modèle : l'enregistreur copie, et une erreur est avalée.
    traces: Any = None
    # Palier « nina » en ombre (route/nina.py::NinaShadow, `route.nina.mode: shadow`) : le premier appel d'une fraction des
    # tours du locataire interne est doublé en tâche de fond, après la réponse réelle ; rien ne revient dans la boucle.
    nina_shadow: Any = None
    # Invitation de fin de tour (drapeau `learning_nudges`, nokker/learning/nudge.py, docs/LEARNING.md §4) :
    # `after_turn(turn)` → une ligne fixe ajoutée au contexte du PROCHAIN tour du fil, ou None. None = éteinte.
    learning: Any = None
    # Provenance des valeurs (drapeau `provenance`, docs/PROVENANCE.md, `nokker/qa/provenance.py::ProvenanceGuard`) :
    # étiquette ce qui entre dans le tour et les résultats d'outils, et refuse ou soumet à une carte forte une action
    # sensible dont un argument critique ne vient que d'une source non fiable. Partagé par les copies du noyau
    # (`dataclasses.replace`, sous-agents). None = éteint (défaut) : aucun verdict ni texte de plus.
    provenance: Any = None
    # Dernier prompt rendu par fil (`prompt_text`), pour `LLMCall.reusable_tokens` ; borné aux fils récents.
    _last_prompts: OrderedDict = field(default_factory=OrderedDict, repr=False)
    _last_prompts_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    last_prompts_max: int = 256
    # File par fil (docs/LOKKAL.md §7.4.9) : `run_turn` prend le verrou équitable du fil, quel que soit l'appelant (API,
    # Telegram, tour de fond, suite d'une décision, CLI, sous-agents). Le hub et l'exécuteur de cartes partagent ces
    # verrous (`BubbleHub.thread_lock`).
    turn_locks: TurnLocks = field(default_factory=TurnLocks, repr=False)
    # Pause / reprise / steer (`core/controls.py`) : partagés par les copies du noyau (`dataclasses.replace`).
    controls: TurnControls = field(default_factory=TurnControls, repr=False)
    # Steer et mention de reprise vus par le modèle (STEER_BLOCK, RESUME_NOTE) : éteints par défaut, tant que les
    # traces d'entraînement ne les portent pas (docs/CONTEXT-FORMAT.md). La pause, elle, ne change rien au contexte.
    steer_enabled: bool = False
    # Fork de sous-agent (drapeau `delegate_fork`, `tools/delegate.py`, `context/fork.py`) : le noyau garde, par fil, le
    # contexte EXACT (messages assemblés + schémas) du dernier appel modèle, pour qu'un `delegate(fork=true)` du même
    # tour en fasse le préfixe de l'enfant (même préfixe, cache réutilisé). False (défaut) : rien n'est gardé.
    fork_bases: bool = False
    fork_bases_max: int = 64
    # Partagé par les copies du noyau (`dataclasses.replace`), comme `_last_prompts`.
    _fork_bases: OrderedDict = field(default_factory=OrderedDict, repr=False)

    def _observe(self, method: str, *args):
        if self.observer is None:
            return None
        try:
            return getattr(self.observer, method)(*args)
        except Exception:  # noqa: BLE001 — un observateur en panne ne casse jamais un tour
            logger.exception("observateur %s a levé", method)
            return None

    def _await_recall(self, turn: Turn, future, started: float) -> list:
        """Résultat du rappel lancé en tête de tour, attendu au plus `recall_budget_s` depuis son lancement.
        En retard ou en panne : tour sans rappel, jamais bloqué ni cassé (le résultat tardif est abandonné)."""
        if future is None:
            return []
        try:
            recall = future.result(timeout=max(0.0, self.recall_budget_s - (time.monotonic() - started)))
        except FutureTimeout:
            self._log_verdicts(turn, [Verdict(gate=Gate.SWITCH, decision=Decision.ALLOW, layer=0,
                                              rule="memory:recall_late",
                                              reason=f"> {self.recall_budget_s:.1f} s, tour sans rappel")])
            return []
        except Exception:  # noqa: BLE001 — la mémoire en panne ne casse jamais un tour
            logger.exception("memory.recall a levé")
            return []
        if recall:
            # Signal de mesure (demandé P1-S1, 17.10.2026) : un verdict dédié, layer=0 (ni invariant ni
            # juge — un FAIT, pas une décision de sûreté), pour qu'un juge de banc puisse reconnaître
            # qu'un rappel automatique a eu lieu SANS que le modèle ait eu besoin d'appeler memory_search
            # lui-même. Décision ALLOW fixe : ceci ne bloque jamais rien, ça journalise seulement.
            self._log_verdicts(turn, [Verdict(gate=Gate.SWITCH, decision=Decision.ALLOW, layer=0,
                                              rule="memory:recall_used",
                                              reason=f"{len(recall)} note(s), {time.monotonic() - started:.2f} s")])
        # Caviardé comme le contexte reçu (`turn.injected`) : le rappel entre dans `Turn.head`, figé, PERSISTÉ dans
        # `turns` (ajout seul) et rejoué dans l'historique — une valeur du coffre dans une note ne doit jamais y entrer
        # (invariant 5 de docs/NEVER-CLEAR.md ; trouvé en revue le 20.10.2026).
        return [Recall(note=r.note, text=self.invariants.scrub_output(r.text), score=r.score, rerank=r.rerank)
                for r in recall]

    def _record_call(self, turn: Turn, assembled, comp, route: RouteDecision, route_reason: str) -> LLMCall:
        """Un appel modèle au ledger, avec le préfixe réutilisable par rapport au DERNIER prompt envoyé sur ce fil."""
        rendered = prompt_text(assembled)
        with self._last_prompts_lock:  # un tour par thread côté API : plusieurs fils en même temps
            previous = self._last_prompts.get(turn.thread_id)
            self._last_prompts[turn.thread_id] = rendered
            self._last_prompts.move_to_end(turn.thread_id)
            while len(self._last_prompts) > self.last_prompts_max:
                self._last_prompts.popitem(last=False)
        call = LLMCall(tier=route.tier, model=comp.model, reasoning=route.reasoning,
                       prefix_sha256=assembled.prefix_sha256, layer_tokens=assembled.layer_tokens,
                       usage=comp.usage, latency_s=comp.latency_s, route_reason=route_reason,
                       reusable_tokens=reusable_tokens(previous, rendered, comp.usage.prompt))
        turn.calls.append(call)
        self.ledger.record(turn.thread_id, turn.index, call)
        self._trace("answered", turn, comp, call)
        # Une ligne par appel, sans contenu (le fil est haché) : la répartition par palier et par classe se lit dans les
        # journaux du pod, sans accès à la base (mesure du routage du 01.11.2026, impossible avant cette ligne).
        u = comp.usage
        logger.info("route : fil %s tour %d appel %d palier %s modèle %s %d+%d jetons (cache %s) %.2f s — %s",
                    hashlib.sha256(turn.thread_id.encode()).hexdigest()[:8], turn.index, len(turn.calls) - 1,
                    route.tier.value, comp.model, u.prompt, u.completion,
                    u.cached if u.cached_reported else "?", comp.latency_s or 0.0, route_reason[:160])
        return call

    def record_side_call(self, turn: Turn, tier: Tier, comp, reason: str, messages: list[dict],
                         side: str = "advisor", meta: dict[str, Any] | None = None) -> LLMCall:
        """Un appel modèle fait À CÔTÉ de la boucle pendant un tour (outil `advise`, `route/advise.py`) : compté au tour
        (`turn.calls`, donc `turn.cost` et les budgets), écrit au ledger avec son motif, et tracé à part
        (`TraceRecorder.side`, sans toucher au découpage des appels de la boucle)."""
        call = LLMCall(tier=tier, model=comp.model, reasoning=False, prefix_sha256="", layer_tokens={},
                       usage=comp.usage, latency_s=comp.latency_s, route_reason=reason)
        turn.calls.append(call)
        try:
            self.ledger.record(turn.thread_id, turn.index, call)
        except Exception:  # noqa: BLE001 — un ledger qui tombe ne coûte pas le tour
            logger.warning("ledger : appel %s non écrit", side, exc_info=True)
        if self.traces is not None and hasattr(self.traces, "side"):
            self._trace("side", turn, side, messages, comp, call, meta)
        return call

    def fork_base(self, thread_id: str, turn_index: int):
        """Contexte du dernier appel modèle du fil (`fork_bases`), s'il appartient à CE tour et au locataire courant ;
        None sinon. Rend une copie (l'enfant ne peut pas modifier celle du parent)."""
        from nokker.tenancy.context import current_tenant_or_none

        with self._last_prompts_lock:
            got = self._fork_bases.get((current_tenant_or_none(), thread_id))
        if got is None or got[0] != turn_index:
            return None
        _, messages, tools, sha = got
        from nokker.core.ports import Assembled

        return Assembled(messages=json.loads(json.dumps(messages)), tools=json.loads(json.dumps(tools)),
                         prefix_sha256=sha)

    def forked(self, base, session_key: str, child_thread: str) -> "Kernel":
        """Copie du noyau dont le contexte part de `base` (fork, `context/fork.py`) ; affinité de cache sur le fil
        parent (`session_key`). Le préfixe réutilisable du premier appel de l'enfant se mesure contre le dernier prompt
        du parent (`LLMCall.reusable_tokens`)."""
        import dataclasses

        from nokker.context.fork import ForkedContext

        with self._last_prompts_lock:
            self._last_prompts[child_thread] = prompt_text(base)
        return dataclasses.replace(self, context=ForkedContext(inner=self.context, base_messages=base.messages,
                                                               base_tools=base.tools, session_key=session_key))

    def _keep_fork_base(self, turn: Turn | None, assembled) -> None:
        if not self.fork_bases or turn is None:
            return
        from nokker.tenancy.context import current_tenant_or_none

        key = (current_tenant_or_none(), turn.thread_id)
        try:
            entry = (turn.index, json.loads(json.dumps(assembled.messages)), json.loads(json.dumps(assembled.tools)),
                     assembled.prefix_sha256)
        except (TypeError, ValueError):  # contenu non sérialisable (faux contexte d'un test) : pas de fork possible
            return
        with self._last_prompts_lock:
            self._fork_bases[key] = entry
            self._fork_bases.move_to_end(key)
            while len(self._fork_bases) > self.fork_bases_max:
                self._fork_bases.popitem(last=False)

    def _trace(self, hook: str, turn: Turn | None, *args) -> None:
        """Crochet de l'enregistreur de traces (`traces`) : sans effet sur le tour, toute erreur journalisée et avalée."""
        if self.traces is None or turn is None:
            return
        try:
            getattr(self.traces, hook)(turn, *args)
        except Exception:  # noqa: BLE001 — une trace manquée ne casse jamais un tour
            logger.warning("traces : crochet %s en échec", hook, exc_info=True)

    def _conclude(self, turn: Turn, history: list, recall: list, state: RouteState, thread_id: str,
                  stream: TurnStream | None, rule: str, reason: str, reply: str = "") -> str:
        """Fin de boucle sans réponse du modèle (limite d'appels, stagnation) : un dernier appel SANS outils rédige la
        réponse avec ce qui a été trouvé (vu en réel le 20.10.2026 : la bonne page trouvée au 8e appel, puis « boucle
        d'outils non terminée » escaladée ; et le tour suivant répondait à cette question restée orpheline). Le palier
        est redemandé à la politique : un doute (REDO_DEEP) posé au dernier appel envoie la conclusion en Deep. Toujours
        une réponse dans l'historique : jamais de question laissée ouverte pour le tour suivant."""
        route = self.policy.choose(state)
        self._log_verdicts(turn, [Verdict(gate=Gate.SWITCH, decision=Decision.ALLOW, layer=0, rule=rule,
                                          reason=reason)])
        reply = self._wrap_up(turn, history, recall, route, thread_id, stream) if not turn.escalated else reply
        why = _FORCED_WHY.get(rule, rule)
        if not reply:
            turn.error = "max_calls"  # échec : ni conclusion ni escalade motivée
            if turn.failure is None:  # un amont en échec à la conclusion a déjà dit pourquoi (provider_error)
                turn.failure = {"code": "max_calls", "message": reason}
            reply = self.escalator.escalate(turn, "boucle d'outils non terminée")
            turn.escalated = True
            if self.reviewer is not None and self._wrote(turn):
                from nokker.qa.review import NOT_REVIEWED_NOTE

                reply += NOT_REVIEWED_NOTE  # l'humain qui reprend sait que le diff n'a pas été relu
        else:
            # Conclusion forcée : relue une fois, sans renvoi (lot fix-batch-1 ; banc D1, H03 finissait non relue).
            reply = self._final_review(turn, reply, why)
        turn.messages.append(Message(role="assistant", content=reply))
        return reply

    def _wrote(self, turn: Turn) -> bool:
        from nokker.qa.review import turn_diff

        return bool(turn_diff(turn, int(getattr(self.reviewer, "max_diff_chars", 24000))))

    def _wrap_up(self, turn: Turn, history: list, recall: list, route: RouteDecision, thread_id: str,
                 stream: TurnStream | None = None, redo_allowed: bool = True) -> str:
        """Dernier appel sans outils quand la limite d'appels est atteinte : la réponse avec ce qui a été trouvé, ce qui
        manque dit clairement. La porte bascule s'applique comme dans la boucle : doute (REDO_DEEP) hors Deep → une
        relance en Deep ; refus → escalade avec le motif de la porte. Chaîne vide seulement si l'amont échoue ou ne rend
        rien (l'appelant escalade alors)."""
        assembled = self.context.assemble(turn, history, recall, [], route.tier)
        route, assembled = self._fit_context(turn, route, assembled,
                                             lambda t: self.context.assemble(turn, history, recall, [], t))
        assembled.messages.append({"role": "user", "content": WRAP_UP_NOTE})
        assembled.tools = []
        try:
            comp, live = self._complete(self.providers[route.tier], assembled, route, thread_id, stream, turn)
        except TurnCancelled:
            raise
        except Exception as e:  # noqa: BLE001 — l'appelant escalade
            logger.warning("appel de conclusion en échec", exc_info=True)
            turn.failure = {"code": "provider_error", "message": f"appel de conclusion en échec : {_short_error(e)}"}
            return ""
        self._record_call(turn, assembled, comp, route, f"conclusion (limite d'appels) : {route.reason}")
        candidate = comp.message.content or ""
        if not candidate.strip() or comp.message.tool_calls:
            return ""
        verdicts = self.gates.switch(candidate, turn, route.tier)
        self._log_verdicts(turn, verdicts)
        decision = worst(verdicts)
        if (decision is Decision.REDO_DEEP and route.tier is not Tier.DEEP and redo_allowed
                and Tier.DEEP in self.providers):
            if live is not None:
                live.discard()  # la conclusion doutée ne reste pas à l'écran
            deep = RouteDecision(tier=Tier.DEEP, reasoning=True, reason="porte bascule : doute sur la conclusion")
            return self._wrap_up(turn, history, recall, deep, thread_id, stream, redo_allowed=False)
        if decision in (Decision.ESCALATE, Decision.HITL, Decision.DENY):
            reason = "; ".join(f"{v.rule}: {v.reason}" for v in verdicts if v.decision is decision)
            turn.escalated = True
            return self.escalator.escalate(turn, reason)
        return candidate

    def run_turn(self, inp: TurnInput, stream: TurnStream | None = None) -> Turn:
        """Un tour. `stream` (optionnel, P1 19.10.2026) reçoit le texte des appels modèles en direct, caviardé
        avec retenue (`core/stream.py`) — seulement depuis les providers qui savent streamer ; il ne change
        RIEN au tour lui-même : `Turn.reply` reste la réponse qui fait foi.

        Un tour à la fois par fil, dans l'ordre d'arrivée (`turn_locks`) : un tour demandé pendant qu'un autre tourne
        attend sa fin, prend l'index suivant et voit son contexte."""
        with self.turn_locks.get(inp.thread_id):
            # 0. Projet d'équipe : le budget est relu avant le tour ; refusé, le tour s'arrête là (rien n'est compté
            # au plafond du compte). Attribué, chaque appel du tour porte le projet au ledger (`spend_tag`).
            # 0 bis. Foyer (X23) : quota commun et plafond du membre, comptés en tours dans le ledger ; un tour d'un
            # membre porte l'étiquette du foyer, en plus de celle d'un projet d'équipe s'il y en a une.
            house = self.household.check(inp.thread_id, inp.sender) if self.household is not None else None
            if house is not None and house.verdict is not None:
                return self._limited_turn(inp, self.invariants.check_input(inp.user_text), house.verdict,
                                          reply=house.reply)
            ticket = self.project_budget.check(inp.thread_id, inp.sender) if self.project_budget is not None else None
            if ticket is not None and ticket.verdict is not None:
                return self._limited_turn(inp, self.invariants.check_input(inp.user_text), ticket.verdict,
                                          reply=ticket.reply)
            with spend_tag(merge_tags(ticket.tag if ticket is not None else None,
                                      house.tag if house is not None else None)):
                return self._run_turn(inp, stream, ticket, house)

    def _run_turn(self, inp: TurnInput, stream: TurnStream | None = None, budget_ticket: Any = None,
                  household_ticket: Any = None) -> Turn:
        # 1. Entrée : caviardage + drapeaux de domaine AVANT tout enregistrement.
        checked = self.invariants.check_input(inp.user_text)
        # 1 bis. Plafond du compte (testeurs seulement) : au-delà, réponse claire, sans rappel ni appel modèle. Membre
        # d'un foyer : le quota est celui du foyer (0 bis), seule la garde de coût du compte reste (`shared`).
        shared = household_ticket is not None
        over = self.limits.check_turn(inp.sender, shared=shared) if self.limits is not None else None
        if over is not None:
            return self._limited_turn(inp, checked, over)
        # 2 (lancé tôt). Rappel du tour : part maintenant, en parallèle de la préparation du contexte ci-dessous,
        # attendu au point d'usage (`_await_recall`). Le texte est déjà caviardé.
        recall_started = time.monotonic()
        recall_future = None
        if checked.text.strip() and checked.text.strip().lower() != "/new":
            # `recall_for` (optionnel) : un port qui sait quel fil appelle (mémoire de projet des sessions locales, X1).
            recall_for = getattr(self.memory, "recall_for", None)
            recall_future = submit_in_context(_RECALL_POOL, recall_for, inp.thread_id, checked.text, self.recall_k) \
                if recall_for is not None else submit_in_context(_RECALL_POOL, self.memory.recall, checked.text,
                                                                 self.recall_k)
        turn = Turn(thread_id=inp.thread_id, index=self.store.next_index(inp.thread_id), input=checked,
                    channel=inp.channel, sender=inp.sender)
        # Images jointes (X7) : références seules, déjà validées et rangées au magasin par la frontière (API) ; le nom
        # de fichier, seul texte venu du client, passe par le caviardage comme le reste.
        turn.images = [ImageRef(sha256=i.sha256, media_type=i.media_type, size=i.size,
                                name=self.invariants.scrub_output(i.name) if i.name else "") for i in inp.images]
        # Contexte reçu (bulle transférée, message d'une autre session), passé par le scrubber de sortie :
        # il vient d'un autre fil, jamais un secret n'entre par là.
        injected = self._observe("context_for", inp.thread_id) or []
        with self._notes_lock:
            notes = self._owner_notes.pop(inp.thread_id, [])
        turn.injected = notes + [self.invariants.scrub_output(str(b)) for b in injected]
        turn.attached = inp.attached  # déjà caviardé et borné par la frontière (API)
        if self.provenance is not None:
            self.provenance.begin_turn(turn)
        self._observe("turn_started", turn)
        # Never clear (docs/NEVER-CLEAR.md) : crochets OPTIONNELS du Context Engine v1 — frontières d'entrée (`/new`,
        # silence), fenêtre calculée au lieu de tout le journal. Un engine v0 ne les porte pas : comportement inchangé.
        before = getattr(self.context, "before_turn", None)
        if before is not None:
            try:
                before(inp.thread_id, checked.text)
            except Exception:  # la vue du contexte ne casse jamais un tour
                logger.exception("context.before_turn a levé")
        if getattr(self.context, "new_episode", None) is not None and checked.text.strip().lower() == "/new":
            turn.reply = ("Nouvel épisode ouvert — rien n'est perdu : la mémoire de la session reste disponible "
                          "(recall_context).")
            turn.messages.append(Message(role="assistant", content=turn.reply))
            self.store.append(turn)
            self._after_turn(turn)
            self._observe("turn_done", turn)
            return turn
        if self.goals is not None:
            try:
                direct = self.goals.before_turn(self, turn, inp)
            except Exception:  # noqa: BLE001 — les objectifs en panne ne cassent jamais un tour
                logger.warning("objectifs : before_turn en échec", exc_info=True)
                direct = None
            if direct is not None:  # commande `/goal`, ou `goal_check` noté par le correcteur : sans appel modèle
                turn.reply = self.invariants.scrub_output(direct)
                turn.messages.append(Message(role="assistant", content=turn.reply))
                self.store.append(turn)
                self._after_turn(turn)
                self._observe("turn_done", turn)
                return turn
        window = getattr(self.context, "history_window", None)
        history = window(inp.thread_id) if window is not None else self.store.history(inp.thread_id)
        ctx = ToolContext(thread_id=inp.thread_id, turn_index=turn.index, channel=inp.channel, sender=inp.sender,
                          kernel=self, turn=turn)

        # 2. Rappel du tour (bloc 5) : calculé une fois, par le noyau, sans attendre que le modèle y pense (A2).
        recall = self._await_recall(turn, recall_future, recall_started)
        loaded = self._initial_tools(checked.text, history)
        state = RouteState(turn=turn, call_index=0)
        last_errors: list[str] = []
        reply = ""
        # Mémo des résultats d'outils READ déjà obtenus DANS CE TOUR (clé = nom + arguments canoniques).
        # Un effet READ rejoué à l'identique renvoie le résultat déjà admis (réservoir/caviardage déjà
        # appliqués), sans ré-exécuter la source ni compter un nouveau verdict d'action (§_tool_step) —
        # mesuré en réel (P1-S1, 17.10.2026) : `memory_get` rappelé 2x sur la MÊME note, `infra_disk_usage`
        # rappelé plusieurs fois une fois le résultat déjà connu. Jamais pour un effet d'écriture (WRITE,
        # DESTRUCTIVE, SEND_*, SPEND, SECRET) : un doublon là est une DÉCISION du modèle, pas du bruit.
        tool_result_cache: dict[tuple[str, str], ToolResult] = {}
        truncations = 0
        reviews = 0  # renvois de relecture faits ce tour (X8)
        outcome = None  # (objectif, rubrique) d'une tâche notée dans le tour (docs/OUTCOMES-GOAL.md)
        if self.goals is not None:
            try:
                outcome = self.goals.rubric_for_turn(turn, inp)
            except Exception:  # noqa: BLE001
                logger.warning("objectifs : rubrique du tour illisible", exc_info=True)
        outcome_passes = 0  # passes de reprise faites ce tour sur la note du correcteur
        seen_calls: set[str] = set()  # signatures d'appels (stall_signature) déjà vues ce tour
        stalls = 0
        notice_at = (max(1, int(self.max_calls * self.budget_notice_at))
                     if self.budget_notice_at and self.max_calls > 4 else None)

        resumed = False  # le tour a été en pause depuis le dernier appel modèle (mention de reprise)
        dropped: list[Steer] = []
        self.controls.begin(inp.thread_id)
        try:
            self._cancelled.discard(inp.thread_id)  # une annulation d'avant ce tour ne le vise pas
            for call_index in range(self.max_calls):
                state.call_index = call_index
                if call_index == notice_at:
                    turn.messages.append(Message(role="user", content=BUDGET_NOTE.format(
                        left=self.max_calls - call_index, total=self.max_calls)))
                # Point de contrôle avant l'appel modèle : pause, puis annulation (aussi pendant la pause), puis steer.
                resumed = self._pause_point(turn, "model") or resumed
                if self._take_cancel(inp.thread_id):
                    reply = CANCEL_REPLY
                    turn.error = "cancelled"
                    turn.messages.append(Message(role="assistant", content=reply))
                    break
                stop = self.stop_check(turn) if self.stop_check is not None else None
                if stop:
                    reply = f"Arrêté par le noyau : {stop}."
                    turn.error = "stopped"
                    turn.failure = {"code": "stopped", "message": str(stop)}
                    self._log_verdicts(turn, [Verdict(gate=Gate.SWITCH, decision=Decision.ALLOW, layer=0,
                                                      rule="loop:stopped", reason=stop)])
                    turn.messages.append(Message(role="assistant", content=reply))
                    break
                self._take_steers(turn, resumed)
                resumed = False
                # 3. Palier pour CET appel.
                route = self.policy.choose(state)
                state.current_tier = route.tier
                assembled = self.context.assemble(turn, history, recall, loaded, route.tier)
                route, assembled = self._fit_context(turn, route, assembled,
                                                     lambda t: self.context.assemble(turn, history, recall, loaded, t))
                state.current_tier = route.tier
                # 4. Appel modèle, clé de session = fil (affinité de cache, F5).
                # P1-S1 (diag latence, 17.10.2026) : un amont Ship qui n'a pas de repli (catalogue Router,
                # un seul amont servable pour qwen/qwen3.8-27b) peut échouer malgré les réessais internes
                # du provider (budget total dépassé, voir providers/sokkan.py) — un palier qui ne répond
                # jamais ne doit JAMAIS laisser le tour sans réponse : on dégrade proprement (escalade,
                # trace au ledger) au lieu de laisser l'exception remonter en tour vide (`turn.error` sans
                # `turn.reply`, bug trouvé en mesurant ce chantier : l'ancien chemin `except Exception`
                # en fin de fonction ne fixait jamais `reply`).
                comp = None
                live: ScrubbedDeltas | None = None
                failure: Exception | None = None
                for attempt_route in self._routes_with_fallback(route):
                    if attempt_route is not route:
                        # Palier demandé indisponible (vu en réel 19.10 : Deep → HTTP 502 du Router) : on retombe
                        # sur Ship pour CET appel au lieu d'escalader vers l'humain avec une page d'erreur.
                        self._log_verdicts(turn, [Verdict(gate=Gate.SWITCH, decision=Decision.ALLOW, layer=0,
                                                          rule="router:fallback",
                                                          reason=f"palier {route.tier.value} indisponible "
                                                                 f"({_short_error(failure)}) → "
                                                                 f"{attempt_route.tier.value}")])
                        route = attempt_route
                        state.current_tier = route.tier
                        assembled = self.context.assemble(turn, history, recall, loaded, route.tier)
                    try:
                        comp, live = self._complete(self.providers[route.tier], assembled, route, inp.thread_id,
                                                    stream, turn)
                        break
                    except TurnCancelled:
                        raise  # l'utilisateur a annulé : ni repli de palier ni escalade
                    except Exception as e:  # noqa: BLE001 — un amont indisponible n'est pas un bug du noyau
                        failure = e
                if comp is None:
                    reason = f"palier {route.tier.value} indisponible : {_short_error(failure)}"
                    turn.failure = {"code": "tier_unavailable", "message": reason}
                    self._log_verdicts(turn, [Verdict(gate=Gate.SWITCH, decision=Decision.ESCALATE, layer=0,
                                                      rule="router:unavailable", reason=reason)])
                    reply = self.escalator.escalate(turn, reason)
                    turn.escalated = True
                    turn.messages.append(Message(role="assistant", content=reply))
                    break
                if comp.attempts > 1:
                    # Visibilité (pas un filet de sûreté) : l'amont a mis plus d'un essai à répondre —
                    # journalisé au ledger pour qu'un opérateur voie un amont qui se dégrade AVANT qu'il
                    # ne finisse par échouer complètement (cf. ci-dessus).
                    self._log_verdicts(turn, [Verdict(gate=Gate.SWITCH, decision=Decision.ALLOW, layer=0,
                                                      rule="router:retried",
                                                      reason=f"{comp.attempts} tentative(s) palier "
                                                             f"{route.tier.value}")])
                self._record_call(turn, assembled, comp, route, route.reason)
                msg = comp.message

                if msg.tool_calls:
                    if live is not None:
                        live.discard()  # texte d'un appel qui finit en outils : pas la réponse
                    turn.messages.append(Message(role="assistant", content=msg.content, tool_calls=msg.tool_calls))
                    terminal_text = None
                    stall_note: str | None = None
                    for tc in msg.tool_calls:
                        # Point de contrôle avant chaque outil : une pause arrête ici, l'outil part à la reprise.
                        resumed = self._pause_point(turn, "tool", tc.name) or resumed
                        if inp.thread_id in self._cancelled:  # annulé : les appels restants ne partent pas
                            turn.messages.append(Message(role="tool", content="annulé par l'utilisateur",
                                                         tool_call_id=tc.id, name=tc.name))
                            continue
                        self._observe("tool_call", turn, tc)
                        t0 = time.monotonic()
                        result = self._tool_step(tc, turn, ctx, loaded, tool_result_cache)
                        self._observe("tool_result", turn, tc, result, int((time.monotonic() - t0) * 1000))
                        turn.messages.append(Message(role="tool", content=result.content, tool_call_id=tc.id,
                                                     name=tc.name))
                        if tc.name in (ESCALATE_TOOL, CLARIFY_TOOL):
                            # Un humain lit la RÉPONSE du tour, jamais l'argument d'un outil : `clarify`
                            # termine le tour sur sa question, au même principe que `escalate_to_human`
                            # (cf commit 2f8d35c) — sinon le modèle peut très bien répondre, à l'appel
                            # suivant, par un texte de remplissage qui ne redonne jamais la question
                            # (vu en réel : « En attente de ta réponse — j'ai posé la question ci-dessus
                            # via `clarify`. », sans la question elle-même).
                            terminal_text = result.content
                        if result.is_error:
                            state.tool_failures += 1
                            sig = error_signature(result)
                            state.repeated_error = state.repeated_error or sig in last_errors
                            last_errors.append(sig)
                        if self.stall_limit:
                            ssig = stall_signature(tc, result)
                            if ssig in seen_calls:
                                stalls += 1
                                if stalls == 1 or stalls == max(2, self.stall_limit // 2):
                                    stall_note = STALL_NOTE.format(name=tc.name)
                            seen_calls.add(ssig)
                    if terminal_text is not None:
                        reply = terminal_text
                        turn.messages.append(Message(role="assistant", content=reply))
                        break
                    if self.stall_limit and stalls >= self.stall_limit and not turn.escalated:
                        # Le tour ne progresse plus : conclure maintenant plutôt que de brûler le reste du budget.
                        reply = self._conclude(turn, history, recall, state, inp.thread_id, stream, "loop:stalled",
                                               f"{stalls} appels d'outils répétés à l'identique, conclusion sans outils")
                        break
                    if stall_note is not None:
                        turn.messages.append(Message(role="user", content=stall_note))
                    continue

                if getattr(comp, "finish_reason", None) == "length" and truncations < self.truncation_retries:
                    truncations += 1
                    if live is not None:
                        live.discard()
                    turn.messages.append(Message(role="assistant", content=msg.content or ""))
                    turn.messages.append(Message(role="user", content=(
                        "[noyau] Ta réponse a été coupée par la limite de longueur. Continue sans répéter : agis avec "
                        "les outils, ou conclus en quelques lignes.")))
                    continue

                # 7. Porte bascule avant d'envoyer.
                candidate = msg.content or ""
                verdicts = self.gates.switch(candidate, turn, route.tier)
                self._log_verdicts(turn, verdicts)
                decision = worst(verdicts)
                if decision is Decision.REDO_DEEP and route.tier is not Tier.DEEP:
                    # Relance : la réponse candidate n'entre PAS dans l'historique (préfixe et cache intacts).
                    if live is not None:
                        live.discard()
                    state.switch_verdicts = verdicts
                    continue
                if decision in (Decision.ESCALATE, Decision.HITL, Decision.DENY) and not turn.escalated:
                    reason = "; ".join(f"{v.rule}: {v.reason}" for v in verdicts if v.decision is decision)
                    candidate = self.escalator.escalate(turn, reason)
                    turn.escalated = True
                # X8 : le juge relit le diff du tour avant « terminé » ; à corriger → l'auteur reprend avec les
                # remarques (la réponse candidate reste dans l'historique : c'est la sienne, il la complète).
                note = self._review(turn, candidate, reviews, call_index)
                if note is not None:
                    reviews += 1
                    if live is not None:
                        live.discard()
                    turn.messages.append(Message(role="assistant", content=candidate))
                    turn.messages.append(Message(role="user", content=note))
                    continue
                if (self.reviewer is not None and call_index + 2 > self.max_calls
                        and reviews < int(getattr(self.reviewer, "rounds", 1))):
                    # Dernier appel du tour : plus de place pour un renvoi, la relecture se fait quand même (sans renvoi).
                    candidate = self._final_review(turn, candidate, "réponse au dernier appel du tour")
                if outcome is not None and not turn.escalated and candidate.strip():
                    # Rubrique de la tâche : le correcteur note dans sa propre fenêtre ; sous le seuil, une passe de
                    # plus avec son retour (la candidate reste dans l'historique : l'agent la complète).
                    can_retry = call_index + 2 <= self.max_calls and not (
                        self.stop_check is not None and self.stop_check(turn))
                    try:
                        onote, candidate = self.goals.in_turn(self, turn, outcome[0], outcome[1], candidate,
                                                              outcome_passes, can_retry)
                    except Exception:  # noqa: BLE001 — un correcteur en panne ne casse jamais un tour
                        logger.warning("objectifs : note du tour en échec", exc_info=True)
                        onote = None
                    if onote is not None:
                        outcome_passes += 1
                        if live is not None:
                            live.discard()
                        turn.messages.append(Message(role="assistant", content=candidate))
                        turn.messages.append(Message(role="user", content=onote))
                        continue
                    outcome = None
                reply = candidate
                turn.messages.append(Message(role="assistant", content=reply))
                break
            else:
                # Limite d'appels atteinte : conclusion sans outils (`_conclude`).
                state.call_index = self.max_calls
                reply = self._conclude(turn, history, recall, state, inp.thread_id, stream, "loop:max_calls",
                                       f"{self.max_calls} appels, conclusion sans outils", reply)
        except TurnCancelled:
            # Annulé pendant un appel modèle en flux (D6) : la réponse HTTP de l'amont est déjà fermée. Le texte partiel
            # de cet appel n'est PAS gardé (ni dans `messages`, ni dans la réponse) : il n'a pas passé la porte bascule,
            # sa retenue de caviardage n'est pas sortie, et des arguments d'outil à moitié écrits ne font pas un appel.
            # Le client a reçu un reset (`ScrubbedDeltas.discard`), puis la réponse finale « Annulé à ta demande. ».
            self._take_cancel(inp.thread_id)
            reply = CANCEL_REPLY
            turn.error = "cancelled"
            self._log_verdicts(turn, [Verdict(gate=Gate.SWITCH, decision=Decision.ALLOW, layer=0,
                                              rule="loop:cancelled_stream",
                                              reason="annulé pendant un appel modèle en flux, réponse amont fermée")])
            turn.messages.append(Message(role="assistant", content=reply))
        except Exception as e:  # noqa: BLE001 — le tour est enregistré même en échec
            turn.error = f"{type(e).__name__}: {e}"
            turn.failure = {"code": "internal_error", "message": turn.error}
        finally:
            dropped = self.controls.end(inp.thread_id)
        for s in dropped:  # arrivés trop tard (réponse finale déjà en cours) : le client les renvoie en message normal
            self._observe_opt("turn_steer_dropped", turn, {"steer_id": s.id, "text": s.text})

        # 8. Sortie : scrubber sur tout ce qui part.
        turn.reply = self.invariants.scrub_output(reply)
        # 8 bis. Quota dépassé en mode soft : l'avertissement suit la réponse (une fois par fenêtre). Hors de
        # `messages` : le modèle ne le relit pas au tour suivant. Le chunk final du flux porte `turn.reply`.
        try:
            notice = self.limits.notice(inp.sender, shared=shared) if self.limits is not None else None
            if household_ticket is not None and notice is None:
                notice = self.household.notice(household_ticket)  # foyer en mode soft : quota passé, une fois
        except Exception:  # noqa: BLE001 — un avertissement manqué ne casse jamais le tour
            logger.warning("avertissement de quota en échec", exc_info=True)
            notice = None
        if notice:
            turn.reply = f"{turn.reply}\n\n{notice}"
        # 8 ter. Budget du projet : avertissement quand CE tour fait passer 80 % du plafond (une fois).
        if budget_ticket is not None:
            try:
                project_notice = self.project_budget.notice(budget_ticket, turn.calls)
            except Exception:  # noqa: BLE001 — un avertissement manqué ne casse jamais le tour
                logger.warning("avertissement du budget de projet en échec", exc_info=True)
                project_notice = None
            if project_notice:
                turn.reply = f"{turn.reply}\n\n{project_notice}"
        # 9. Fin de tour : append-only. L'extraction de faits → quarantaine arrive avec A-3 (memory.propose).
        self.store.append(turn)
        self._trace("finish", turn)  # traces d'entraînement du tour (docs/TRACES.md), après le tour qui fait foi
        self._after_turn(turn)
        self._observe("turn_done", turn)
        if self.goals is not None:  # objectif du fil : dépense, note, arrêt ou tour suivant (docs/OUTCOMES-GOAL.md)
            try:
                self.goals.after_turn(self, turn, inp)
            except Exception:  # noqa: BLE001 — jamais bloquant
                logger.warning("objectifs : after_turn en échec", exc_info=True)
        # 10. Ressources tenues pendant le tour (onglets du navigateur…) rendues par les sources qui le déclarent.
        tools_done = getattr(self.tools, "turn_done", None)
        if callable(tools_done):
            try:
                tools_done(turn.thread_id)
            except Exception:  # noqa: BLE001
                logger.warning("turn_done des outils en échec", exc_info=True)
        # 11. Hooks `turn_end` (Nokker Lokkal, lot X3, nokker/tools/hooks.py) : seulement si le registre est entouré des
        # hooks ; le tour fini (réponse caviardée) leur est passé, rien ne revient dans le tour.
        ended = getattr(self.tools, "turn_ended", None)
        if callable(ended):
            try:
                ended(turn)
            except Exception:  # noqa: BLE001 — un hook en panne ne casse jamais un tour
                logger.warning("hooks turn_end en échec", exc_info=True)
        # 12. Apprentissage (drapeau `learning_nudges`) : tour réussi et riche en outils → une ligne fixe au tour suivant.
        if self.learning is not None:
            try:
                line = self.learning.after_turn(turn)
            except Exception:  # jamais bloquant
                logger.warning("invitation d'apprentissage en échec", exc_info=True)
                line = None
            if line:
                self.add_owner_note(turn.thread_id, line)
        return turn

    def _limited_turn(self, inp: TurnInput, checked, verdict: Verdict, *, reply: str | None = None) -> Turn:
        """Tour refusé par un plafond (compte, ou budget du projet : `reply` donné) : gardé dans l'historique avec son
        verdict, sans appel modèle."""
        turn = Turn(thread_id=inp.thread_id, index=self.store.next_index(inp.thread_id), input=checked,
                    channel=inp.channel, sender=inp.sender)
        self._observe("turn_started", turn)
        self._log_verdicts(turn, [verdict])
        if reply is None:
            reply = self.limits.reply(verdict) if self.limits is not None else ""
        turn.reply = reply
        turn.messages.append(Message(role="assistant", content=turn.reply))
        self.store.append(turn)
        self._after_turn(turn)
        self._observe("turn_done", turn)
        return turn

    def _after_turn(self, turn: Turn) -> None:
        """Comptage, délestage ou clôture d'épisode (Context Engine v1) — jamais bloquant pour le tour."""
        after = getattr(self.context, "after_turn", None)
        if after is None:
            return
        try:
            after(turn)
        except Exception:
            logger.exception("context.after_turn a levé")

    def _initial_tools(self, text: str, history: list) -> list[ToolSpec]:
        """Schémas non cœur chargés au premier appel du tour (`tool_loading`, voir tools/select.py)."""
        non_core = self.tools.find("", limit=10_000)
        if self.tool_loading == "all":
            return list(non_core)
        used = {c.name for past in history[-self.tool_reuse_turns:] for m in past.messages for c in m.tool_calls}
        return relevant_tools(text, used, non_core)

    # 5-6. Porte action, exécution, réservoir.
    def _tool_step(self, tc: ToolCall, turn: Turn, ctx: ToolContext, loaded: list[ToolSpec],
                   seen: dict[tuple[str, str], ToolResult] | None = None) -> ToolResult:
        if tc.name in MEMORY_TOOLS:
            done = sum(1 for m in turn.messages for c in m.tool_calls if c.name in MEMORY_TOOLS and c.id != tc.id)
            if done >= self.memory_calls_per_turn:
                return ToolResult(call_id=tc.id, name=tc.name, content=(
                    f"Budget mémoire du tour atteint ({self.memory_calls_per_turn} appels) : le rappel automatique et "
                    "les résultats déjà obtenus sont tout ce qu'il y a. Réponds avec, ou dis ce qui manque."))
        if tc.name == FIND_TOOL:
            found = self.tools.find(str(tc.arguments.get("query", "")))
            known = {s.name for s in loaded}
            loaded.extend(s for s in found if s.name not in known)
            listing = [{"name": s.name, "description": s.description} for s in found]
            return ToolResult(call_id=tc.id, name=tc.name, content=json.dumps(listing, ensure_ascii=False))

        if tc.name == ESCALATE_TOOL:
            text = self.escalator.escalate(turn, str(tc.arguments.get("reason", "")))
            turn.escalated = True
            return ToolResult(call_id=tc.id, name=tc.name, content=text)

        spec = self.tools.spec(tc.name)
        if spec is not None and not spec.core and all(s.name != spec.name for s in loaded):
            loaded.append(spec)  # appelé sans schéma : exécuté (portes comprises) ET chargé pour la suite du tour
        effect = call_effect(spec, tc.arguments)
        # Appel identique (même nom, mêmes arguments) déjà fait DANS CE TOUR : pour un effet READ
        # seulement (jamais WRITE/DESTRUCTIVE/SEND_*/SPEND/SECRET — un doublon là est une décision du
        # modèle, pas du bruit), renvoie le résultat déjà obtenu (déjà caviardé/admis) sans ré-exécuter
        # la source ni compter un nouveau verdict d'action — mesuré en réel (P1-S1, 17.10.2026) :
        # `memory_get` et `infra_disk_usage` rappelés plusieurs fois avec les mêmes arguments une fois
        # le résultat déjà connu.
        cache_key = (tc.name, json.dumps(tc.arguments, sort_keys=True, ensure_ascii=False)) if seen is not None \
            else None
        if getattr(spec, "fresh", False):
            cache_key = None  # journal d'un processus, liste des processus : relus à chaque appel
        if effect is Effect.READ and cache_key is not None and cache_key in seen:
            cached = seen[cache_key]
            return ToolResult(call_id=tc.id, name=tc.name,
                              content=cached.content + "\n[déjà appelé ce tour avec les mêmes arguments — "
                                       "résultat identique, pas ré-exécuté]",
                              is_error=cached.is_error, reservoir_id=cached.reservoir_id,
                              secret_handles=cached.secret_handles)

        verdicts = self.gates.action(tc, spec, turn)
        if self.provenance is not None:  # en tête : sa règle est celle que nomme la carte ou le refus
            verdicts = self.provenance.check(tc, spec, turn) + verdicts
        self._log_verdicts(turn, verdicts)
        decision = worst(verdicts)
        if decision is Decision.ALLOW:
            result = self.tools.call(tc.name, tc.arguments, ctx)
            result.call_id = tc.id
            # Caviardage AVANT le contexte et le store : la garantie ne dépend ni du modèle ni de la source d'outils.
            redacted = self.invariants.redact_result(result, spec)
            if self.provenance is not None:
                self.provenance.record(turn, tc, spec, redacted)
            # Producteurs de bulles : le résultat CAVIARDÉ mais complet — `admit_result` peut le ranger au
            # réservoir et n'en garder qu'un extrait (vu sur le canari 18.10 : web_search sans bulle).
            suffix = self._observe("tool_executed", turn, tc, spec, redacted)
            admit_in = getattr(self.context, "admit_result_in", None)
            admitted = (admit_in(turn.thread_id, turn.index, redacted) if admit_in is not None
                        else self.context.admit_result(redacted))
            if suffix:
                admitted.content = admitted.content + str(suffix)
            if effect is Effect.READ and cache_key is not None:
                seen[cache_key] = admitted
            elif seen is not None and effect is not Effect.READ:
                # Un appel à effet a pu changer ce que les lectures renvoient (fichier patché puis relu, événement
                # créé puis listé) : les lectures déjà faites ce tour ne valent plus.
                seen.clear()
            return admitted
        rule = next((v for v in verdicts if v.decision is decision), None)
        why = f"{rule.rule}: {rule.reason}" if rule else decision.value
        if decision is Decision.HITL:
            turn.pending_approvals.append(tc)
            if self.approvals is not None:
                # P1 (18.10.2026) : carte PROUVÉE (docs/APPROVALS.md), pas seulement une mention dans
                # `pending_approvals`. `advice` : avis du juge sur une carte forte (X8, `advisor`), None sinon
                # (accompagne, ne bloque rien).
                auto = self._auto_review_eligible(rule, tc, effect)
                if auto:
                    approval = self.approvals.request(turn, tc, rule, self._card_advice(turn, tc, rule), notify=False)
                    reviewed = self._auto_review(turn, tc, spec, ctx, approval, effect)
                    if reviewed is not None:
                        return reviewed
                else:
                    approval = self.approvals.request(turn, tc, rule, self._card_advice(turn, tc, rule))
                turn.approval_ids.append(approval.id)
                return ToolResult(call_id=tc.id, name=tc.name,
                                  content=f"EN ATTENTE D'APPROBATION HUMAINE ({why}) [approval:{approval.id}]. "
                                          "L'action n'a pas été exécutée. L'utilisateur décide sur la carte : ne "
                                          "rappelle pas l'outil et n'escalade pas ; termine ton tour en disant ce que "
                                          "tu attends. Tu reprendras avec le résultat.")
            return ToolResult(call_id=tc.id, name=tc.name, content=f"EN ATTENTE D'APPROBATION HUMAINE ({why}). "
                              "L'action n'a pas été exécutée.")
        if decision is Decision.ESCALATE:
            text = self.escalator.escalate(turn, why)
            turn.escalated = True
            return ToolResult(call_id=tc.id, name=tc.name, content=f"ESCALADÉ À UN HUMAIN ({why}). {text}")
        return ToolResult(call_id=tc.id, name=tc.name, content=f"REFUSÉ par le noyau ({why}). "
                          "Ne pas réessayer autrement.", is_error=False, reason=rule.code if rule else None)

    def _routes_with_fallback(self, route: RouteDecision) -> list[RouteDecision]:
        """Le palier choisi, puis Ship en repli s'il est différent et configuré (jamais l'inverse : Ship est le
        plancher, son indisponibilité escalade)."""
        if route.tier is Tier.SHIP or Tier.SHIP not in self.providers:
            return [route]
        return [route, RouteDecision(tier=Tier.SHIP, reasoning=route.reasoning,
                                     reason=f"repli : {route.tier.value} indisponible")]

    def _wire_messages(self, provider: Provider, assembled, thread_id: str, turn: Turn | None) -> list[dict]:
        """Messages tels qu'ils partent (X7) : parties `nokker_image` du Context Engine changées en `image_url` (palier
        avec vision) ou en note texte (sans vision). Des images du tour courant non lues : verdict `images:not_read` au
        ledger et événement `attachment.skipped` à l'utilisateur, une fois par tour ; celles des tours précédents,
        sans vision, deviennent leur ligne de délestage, sans rien signaler."""
        vision = bool(getattr(provider, "vision", False))
        probe = getattr(provider, "vision_probe", None)
        if not vision and probe is not None and has_images(assembled.messages):
            # Vision `auto` (§7.6) : UNE sonde, ici seulement (un tour qui porte une image), jamais en fond.
            vision, fresh = probe.check(provider)
            if fresh and turn is not None:
                self._log_verdicts(turn, [Verdict(gate=Gate.SWITCH, decision=Decision.ALLOW, layer=0,
                                                  rule="images:vision_probe",
                                                  reason=f"sonde de vision {getattr(provider, 'model', '?')} : "
                                                         f"{'oui' if vision else 'non'} ({probe.last_reason})")])
        # Images non lues signalées pour CE tour seulement ; celles des tours d'avant sont délestées en une ligne.
        messages, skipped = materialize(assembled.messages, vision=vision, blobs=self.images, thread_id=thread_id,
                                        current_turn=turn.index if turn is not None else None)
        if skipped and turn is not None and not any(v.rule == "images:not_read" for v in turn.verdicts):
            self._log_verdicts(turn, [Verdict(gate=Gate.SWITCH, decision=Decision.ALLOW, layer=0,
                                              rule="images:not_read",
                                              reason=f"{skipped} image(s) non envoyée(s) : modèle sans vision "
                                                     f"({getattr(provider, 'model', '?')})")])
            if self.observer is not None and hasattr(self.observer, "attachment_skipped"):
                self._observe("attachment_skipped", turn, skipped, "no_vision")
        # Filet du fil (lot fix-batch-1) : coffre + motifs forts sur TOUT ce qui part, y compris les blocs qui n'ont pas
        # de porte à eux (plan, mémoire de projet, profil, arguments d'outils rejoués). Un amont qui refuse un secret
        # (Router : `400 secret_detected`) casserait le tour entier ; un texte sans secret part à l'identique.
        redact_context = getattr(self.invariants, "redact_context", None)
        if callable(redact_context):
            messages = redact_context(messages)
        return messages

    def _fit_context(self, turn: Turn, route: RouteDecision, assembled, reassemble: Callable[[Tier], Any]):
        """Un palier qui déclare sa fenêtre (`max_context_tokens`, palier `nina` 32 k) ne reçoit jamais un appel qui n'y
        tient pas : bascule sur Ship (verdict `router:context`), le prompt est réassemblé pour Ship."""
        provider = self.providers.get(route.tier)
        if provider is None or route.tier is Tier.SHIP or Tier.SHIP not in self.providers:
            return route, assembled
        from nokker.route.nina import fits

        ok, need, limit = fits(provider, assembled.messages, assembled.tools, self.max_tokens)
        if ok:
            return route, assembled
        reason = f"{route.reason} ; contexte ~{need} jetons > {limit} ({route.tier.value}) → ship"
        self._log_verdicts(turn, [Verdict(gate=Gate.SWITCH, decision=Decision.ALLOW, layer=0, rule="router:context",
                                          reason=reason)])
        ship = RouteDecision(tier=Tier.SHIP, reasoning=False, reason=reason)
        return ship, reassemble(Tier.SHIP)

    def _shadow(self, turn: Turn | None, route: RouteDecision, messages, tools, comp) -> None:
        """Ombre de Nina (route/nina.py) : premier appel du tour seulement, jamais quand Nina répond déjà ; sans effet."""
        if self.nina_shadow is None or turn is None or turn.calls or route.tier is Tier.NINA:
            return
        try:
            self.nina_shadow.maybe_run(turn, messages, tools, comp, route.tier.value, getattr(comp, "model", ""))
        except Exception:  # noqa: BLE001 — l'ombre ne casse jamais un tour
            logger.warning("nina ombre : en échec", exc_info=True)

    def _complete(self, provider: Provider, assembled, route: RouteDecision, thread_id: str,
                  stream: TurnStream | None, turn: Turn | None = None):
        """Un appel modèle (en streaming si le client l'a demandé et que le provider sait le faire). Un flux
        coupé APRÈS le premier morceau (`StreamInterrupted`, vu en réel 19.10) : reset du client puis UN rejeu
        sans streaming. Toute autre erreur remonte (repli de palier ou escalade chez l'appelant)."""
        messages = self._wire_messages(provider, assembled, thread_id, turn)
        live = None
        tools_live: LiveToolInputs | None = None
        kwargs = {}
        if stream is not None and accepts_on_delta(provider):
            live = ScrubbedDeltas(self.invariants.scrub_output, stream, holdback_for(self.invariants))
            kwargs["on_delta"] = self._cancellable(live.feed, thread_id)
            tools_live = self._live_tool_inputs(provider, assembled, turn)
            if tools_live is not None:
                kwargs["on_tool_delta"] = self._cancellable(tools_live.feed, thread_id)
        budget = {"max_tokens": self.max_tokens} if self.max_tokens else {}
        self._trace("sent", turn, messages, assembled.tools)
        self._keep_fork_base(turn, assembled)
        # Fork (`context/fork.py`) : l'enfant garde l'affinité de cache du fil parent ; sinon, le fil.
        session = getattr(self.context, "session_key", None)
        session = session if isinstance(session, str) and session else thread_id
        try:
            comp = provider.complete(messages, assembled.tools, reasoning=route.reasoning,
                                     session_key=session, **budget, **kwargs)
        except StreamInterrupted as e:
            logger.warning("flux amont interrompu (%s) : rejeu sans streaming", e)
            if live is not None:
                live.discard()
            if tools_live is not None:
                tools_live.discard()
            return provider.complete(messages, assembled.tools, reasoning=route.reasoning,
                                     session_key=session, **budget), None
        except Exception:
            if live is not None:
                live.discard()
            if tools_live is not None:
                tools_live.discard()
            raise
        if tools_live is not None:
            tools_live.finish()  # arguments complets : la retenue du caviardage sort (le texte caviardé en entier)
        self._shadow(turn, route, messages, assembled.tools, comp)
        return comp, live

    def _cancellable(self, feed: Callable[..., None], thread_id: str) -> Callable[..., None]:
        """Relais d'un morceau en flux qui vérifie l'annulation AVANT de le remettre (D6) : annulé, il lève
        `TurnCancelled`, ce qui coupe le flux de l'amont au prochain morceau (texte ou arguments d'outil)."""
        cancelled = self._cancelled

        def fed(*args: Any) -> None:
            if thread_id in cancelled:
                raise TurnCancelled(thread_id)
            feed(*args)

        return fed

    def _live_tool_inputs(self, provider: Provider, assembled, turn: Turn | None) -> LiveToolInputs | None:
        """Écriture en direct (X22, docs/BUBBLES.md §6) : seulement pour un tour streamé, un provider qui remet les
        arguments d'outils au fil de l'eau, un appel qui propose des outils, et un observateur qui publie
        `tool.input.delta`. Le texte passe par le caviardage avec retenue, comme la réponse."""
        if (turn is None or self.observer is None or not assembled.tools
                or not hasattr(self.observer, "tool_input_delta")
                or not accepts_on_delta(provider, "on_tool_delta")):
            return None

        def emit(data: dict) -> None:
            self._observe("tool_input_delta", turn, data)

        return LiveToolInputs(self.invariants.scrub_output, emit, holdback_for(self.invariants))

    def _log_verdicts(self, turn: Turn, verdicts: list[Verdict]) -> None:
        for v in verdicts:
            turn.verdicts.append(v)
            self.ledger.record_verdict(turn.thread_id, turn.index, v)


__all__ = ["CANCEL_REPLY", "ESCALATE_TOOL", "FIND_TOOL", "Gate", "Kernel", "TurnCancelled", "worst"]
