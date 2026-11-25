"""Ce que les outils d'UN tour ont réellement fait, lu dans les messages du tour : base commune des gardes de fin de tour
(`note_claims` : « c'est noté », `action_claims` : « c'est fait »). Un résultat compte comme abouti s'il n'est ni un refus
(`Refusé : …`), ni une carte en attente (`EN ATTENTE D'APPROBATION HUMAINE…`, appel sauté, annulé), ni un échec d'outil
(`failed_ids`, posé par la boucle d'après `ToolResult.is_error`)."""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from nokker.core import model_texts as mt
from nokker.core.ports import call_effect
from nokker.core.refusal import is_refusal
from nokker.core.turn import Effect, Message

# Outils classés READ qui emportent pourtant un effet hors du fil (`tools/effects.py::LEAVES_THREAD`) : jamais « lecture ».
EFFECT_DESPITE_READ = frozenset({"session_send", "agent_draft", "memory_note"})
# Marques d'une carte en attente, d'un appel sauté ou annulé : dans chacune des quatre langues du tour.
_PENDING = (*mt.marks(mt.PENDING_BARE, head=" ("), *mt.PENDING_SKIPPED_MARK.values(), *mt.CALL_CANCELLED.values())
# Fin d'un refus de `memory_note` (nokker/context/project_memory.py::FAILED_MARK) : un échec, pas un succès.
_NOTE_FAILED = "rien n'a été noté"  # français ; les quatre langues : `_NOTE_FAILED_MARKS`
_NOTE_FAILED_MARKS = tuple(mt.NOTE_NOT_WRITTEN_MARK.values())


@dataclass
class Outcomes:
    called: int = 0  # appels d'outil du tour
    done: int = 0  # outils à effet aboutis
    pending: int = 0  # cartes posées (en attente de la personne) ou appels non exécutés
    refused: int = 0  # refus des portes
    failed: int = 0  # échecs d'outil
    read_only: int = 0  # lectures abouties
    note_ok: int = 0  # `memory_note` abouti, en attente de validation ou déjà noté
    note_failed: int = 0

    @property
    def effect_or_pending(self) -> bool:
        return bool(self.done or self.pending)

    def reason(self) -> str:
        """Pourquoi rien n'est fait : `pending` | `refused` | `failed` | `none` (clés de `person_texts.ACTION_WHY`)."""
        return "pending" if self.pending else "refused" if self.refused else "failed" if self.failed else "none"


@dataclass
class CallOutcome:
    """Issue d'UN appel d'outil du tour (`call_outcomes`) : `state` = done | pending | refused | failed | read."""

    name: str
    effect: Effect
    state: str
    arguments: dict[str, Any]


def _classify(m: Message, calls: dict[str, Any], spec_of: Callable[[str], Any] | None, failed_ids: set[str]) -> CallOutcome:
    name, content = m.name or "", m.content or ""
    tc = calls.get(m.tool_call_id or "")
    args = tc.arguments if tc is not None else {}
    effect = Effect.WRITE
    if spec_of is not None:
        effect = call_effect(spec_of(name), args)
    if is_refusal(content):
        return CallOutcome(name, effect, "refused", args)
    if content.startswith(_PENDING):
        return CallOutcome(name, effect, "pending", args)
    if m.tool_call_id in failed_ids or (name == "memory_note" and any(k in content for k in _NOTE_FAILED_MARKS)):
        return CallOutcome(name, effect, "failed", args)
    if effect is not Effect.READ or name in EFFECT_DESPITE_READ or spec_of is None:
        return CallOutcome(name, effect, "done", args)
    return CallOutcome(name, effect, "read", args)


def call_outcomes(messages: list[Message], spec_of: Callable[[str], Any] | None = None,
                  failed_ids: Iterable[str] = ()) -> list[CallOutcome]:
    """Une issue PAR appel d'outil du tour, dans l'ordre (la garde « c'est fait » juge action par action)."""
    failed_ids = set(failed_ids)
    calls = {tc.id: tc for m in messages if m.role == "assistant" for tc in m.tool_calls}
    return [_classify(m, calls, spec_of, failed_ids) for m in messages if m.role == "tool"]


def read_outcomes(messages: list[Message], spec_of: Callable[[str], Any] | None = None,
                  failed_ids: Iterable[str] = ()) -> Outcomes:
    """`spec_of(nom)` rend la spec d'un outil (effet) ; sans elle, tout outil abouti compte comme à effet (prudent pour
    une garde qui ne doit pas corriger à tort)."""
    failed_ids = set(failed_ids)
    calls = {tc.id: tc for m in messages if m.role == "assistant" for tc in m.tool_calls}
    out = Outcomes()
    for m in messages:
        if m.role != "tool":
            continue
        out.called += 1
        name, content = m.name or "", m.content or ""
        if name == "memory_note":
            if any(m in content for m in _NOTE_FAILED_MARKS):
                out.note_failed += 1
            else:
                out.note_ok += 1
        if is_refusal(content):
            out.refused += 1
        elif content.startswith(_PENDING):
            out.pending += 1
        elif m.tool_call_id in failed_ids or (name == "memory_note" and any(m in content for m in _NOTE_FAILED_MARKS)):
            out.failed += 1
        else:
            tc = calls.get(m.tool_call_id or "")
            effect = Effect.WRITE
            if spec_of is not None:
                effect = call_effect(spec_of(name), tc.arguments if tc is not None else {})
            if effect is not Effect.READ or name in EFFECT_DESPITE_READ or spec_of is None:
                out.done += 1
            else:
                out.read_only += 1
    return out
