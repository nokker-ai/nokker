"""`memory_note` côté nokker.ai : le modèle propose de retenir ce que la personne vient de lui demander de retenir ; la
note attend dans la quarantaine (`nokker/learning/pending.py`, drapeau `memory_pending`) et n'entre dans AUCUN contexte
avant l'approbation de l'humain dans la vue d'apprentissage. Réglage `tenant_long_memory_settings.note_tool` (défaut
false : ni outil ni texte de plus). Portée user seulement : jamais le foyer, jamais un projet.

Garde-fous codés : la citation (`quote`) doit se trouver dans les mots de la personne de CE tour (origine « personne »,
≥ 8 caractères, après normalisation) ; secrets caviardés ; sous-agent refusé ; mineur refusé (`allowed`). À
l'approbation, la note s'écrit par `LongMemory.write` (provenance `assistant` : dérivée des mots de la personne, sans
contenu de tiers). Les textes vus par le modèle sont ICI, et seulement ici (`ALL_MODEL_TEXTS`, empreinte gelée par la
relecture des textes du modèle) ; le résultat d'attente et les deux erreurs de forme sont ceux de Lokkal, mot pour mot."""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from nokker.context.project_memory import (
    KINDS,
    MAX_TEXT,
    MAX_TITLE,
    MIN_QUOTE,
    NOT_WRITTEN,
    PENDING_RESULT,
    SCRUBBED,
    STORAGE_ERROR,
)
from nokker.core import model_texts as mt
from nokker.core.ports import ToolContext, ToolResult, ToolSpec
from nokker.core.turn import Effect
from nokker.memory.provenance import ASSISTANT
from nokker.memory.tenant_long import LongMemory, LongMemoryError, _norm

logger = logging.getLogger("nokker.memory.long_note")

TOOL = "memory_note"
SOURCE = "tenant:long-note"
ORIGIN = "memory_note"

# Vus par le MODÈLE (relus et gelés avant le câblage).
DESCRIPTION = ("Retient pour les prochaines conversations ce que l'utilisateur vient de te demander de retenir : une "
               "règle, une préférence, un fait sur lui. Une idée par note, courte, sans secret. Elle n'entre en "
               "mémoire qu'après sa relecture par l'utilisateur.")
KIND_DESCRIPTION = ("rule : règle durable ; preference : préférence ; feedback : correction ; fact : fait dit par "
                    "l'utilisateur")
TITLE_DESCRIPTION = f"titre court (≤ {MAX_TITLE} car.)"
TEXT_DESCRIPTION = f"la note (≤ {MAX_TEXT} car.)"
QUOTE_DESCRIPTION = f"ses mots de CE message, tels quels (au moins {MIN_QUOTE} caractères)"
ERR_KIND = f"kind ∈ {KINDS}"
ERR_SIZE = f"title (≤ {MAX_TITLE} car.) et text (≤ {MAX_TEXT} car.) requis : une note par idée, courte"
ALREADY = "déjà noté : {name}"  # = résultat de Lokkal (`ProjectMemory.note`), mot pour mot
ERR_QUOTE = ("cite dans `quote` les mots de l'utilisateur de ce message (au moins "
             f"{MIN_QUOTE} caractères, tels quels)")
PARAMETERS = {"type": "object", "properties": {
    "kind": {"type": "string", "enum": list(KINDS), "description": KIND_DESCRIPTION},
    "title": {"type": "string", "description": TITLE_DESCRIPTION},
    "text": {"type": "string", "description": TEXT_DESCRIPTION},
    "quote": {"type": "string", "description": QUOTE_DESCRIPTION}},
    "required": ["kind", "title", "text", "quote"]}

ALL_MODEL_TEXTS = (DESCRIPTION, KIND_DESCRIPTION, TITLE_DESCRIPTION, TEXT_DESCRIPTION, QUOTE_DESCRIPTION, ERR_KIND,
                   ERR_SIZE, ERR_QUOTE, PENDING_RESULT, ALREADY, NOT_WRITTEN, STORAGE_ERROR, SCRUBBED)


class LongNoteTool:
    """`ToolSource` des testeurs : `memory_note` vers la quarantaine. Effet READ comme `agent_draft` : rien n'est écrit en
    mémoire ici et rien n'entre dans un contexte ; la validation de la personne (vue d'apprentissage) EST la carte, une
    seconde carte d'approbation dans le chat doublerait la première."""

    def __init__(self, service: LongMemory, pending: Any, *, scrub: Callable[[str], str] | None = None,
                 allowed: Callable[[str], bool] | None = None) -> None:
        self.service, self.pending, self.scrub, self.allowed = service, pending, scrub, allowed
        self._specs = [ToolSpec(name=TOOL, description=DESCRIPTION, parameters=PARAMETERS, effect=Effect.READ,
                                core=False, source=SOURCE)]

    def available_for(self, tenant_id: str) -> bool:
        if self.service.for_tenant(tenant_id) is None:
            return False
        return self.allowed(tenant_id) if self.allowed is not None else True

    def specs(self) -> list[ToolSpec]:
        return list(self._specs)

    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        def res(content: str, error: bool = False) -> ToolResult:
            # B5 : un refus le dit (« rien n'a été noté »), comme `memory_note` de Lokkal, mot pour mot.
            return ToolResult(call_id="", name=TOOL, content=f"{content} — {mt.pick(mt.NOTE_NOT_WRITTEN)}" if error else content,
                              is_error=error)

        if name != TOOL:
            return ToolResult(call_id="", name=name, content=mt.pick(mt.UNKNOWN_TOOL_BARE).format(name=name), is_error=True)
        from nokker.tenancy.context import current_tenant

        if self.service.for_tenant(current_tenant()) is None or (
                self.allowed is not None and not self.allowed(current_tenant())):
            return ToolResult(call_id="", name=name, content=mt.pick(mt.UNKNOWN_TOOL_BARE).format(name=name), is_error=True)
        args = arguments or {}
        kind = str(args.get("kind") or "")
        title = " ".join(str(args.get("title") or "").split())
        text = str(args.get("text") or "").strip()
        if kind not in KINDS:
            return res(ERR_KIND, True)
        if not title or not text or len(title) > MAX_TITLE or len(text) > MAX_TEXT:
            return res(mt.pick(mt.MEMORY_SIZE_REQUIRED).format(max_title=MAX_TITLE, max_text=MAX_TEXT), True)
        turn = ctx.turn
        said = ""
        if turn is not None and "/sa-" not in ctx.thread_id and not getattr(turn.input, "origin", ""):
            said = _norm(turn.input.text)  # les mots de la PERSONNE, pas le bloc d'une carte ni un bloc injecté
        quote = _norm(str(args.get("quote") or ""))
        if len(quote) < MIN_QUOTE or quote not in said:
            return res(mt.pick(mt.MEMORY_QUOTE_REQUIRED).format(min_quote=MIN_QUOTE), True)
        raw = (title, text)
        if self.scrub is not None:
            title, text = self.scrub(title), self.scrub(text)
        scrubbed = f" — {mt.pick(mt.NOTE_SCRUBBED)}" if (title, text) != raw else ""
        from nokker.learning.pending import PendingError

        account = self.pending.account_of_thread(ctx.thread_id) if hasattr(self.pending, "account_of_thread") else None
        try:
            same = self.service.equivalent_note(account, text) if account else None
        except Exception:  # noqa: BLE001 — sans la lecture, la note part en attente : la personne tranchera
            logger.warning("memory_note : recherche d'une note équivalente en échec", exc_info=True)
            same = None
        if same is not None:  # T6 : déjà active en mémoire, rien à faire relire
            return res(mt.pick(mt.NOTE_ALREADY).format(name=same.name))
        try:
            held = self.pending.hold_note(thread_id=ctx.thread_id, turn_index=ctx.turn_index, kind=kind, scope="user",
                                          title=title, text=text, derived_from="user", project="", links=[])
        except PendingError as e:
            return res(e.message, True)
        except Exception:  # noqa: BLE001 — quarantaine en panne : le modèle doit savoir que rien n'attend
            logger.warning("memory_note : mise en attente en échec", exc_info=True)
            return res(mt.pick(mt.NOTE_STORAGE_ERROR), True)
        return res(mt.pick(mt.NOTE_PENDING).format(id=held.id) + scrubbed)


def commit_note_for(service: LongMemory, pending: Any) -> Callable[[Any], str | None]:
    """`PendingMemory.commit_note` côté nokker.ai : la note approuvée s'écrit en mémoire longue de SON compte."""
    from nokker.learning.pending import PendingError

    def commit(note: Any) -> str | None:
        account = pending.account_of_thread(note.thread_id)
        if not account:
            raise PendingError(403, "no_account", "pas de compte pour ce fil")
        try:
            out = service.write(account, scope="user", kind=note.kind, title=note.title, text=note.text,
                                audience="all", origin=ORIGIN, provenance=ASSISTANT)
        except LongMemoryError as e:  # la note reste en attente, la personne voit le motif
            raise PendingError(e.status, e.code, e.message) from None
        return str((out.get("note") or {}).get("name") or "") or None

    return commit


def texts_sha256() -> str:
    """Empreinte des textes vus par le modèle (gel : `tests/unit/memory/test_long_note.py`)."""
    import hashlib
    import json

    return hashlib.sha256(json.dumps([DESCRIPTION, PARAMETERS, ERR_KIND, ERR_SIZE, ERR_QUOTE, PENDING_RESULT, ALREADY, NOT_WRITTEN,
                                      STORAGE_ERROR, SCRUBBED],
                                     ensure_ascii=False, sort_keys=True).encode()).hexdigest()


__all__ = ["ALL_MODEL_TEXTS", "LongNoteTool", "commit_note_for", "texts_sha256"]
