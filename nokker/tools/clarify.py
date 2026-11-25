"""Question à choix : `clarify` enrichi d'options (drapeau `host_clarify_options`, docs/NOKKAN-HOST-API.md §1).

Ce n'est PAS une carte d'approbation : une carte est la porte d'un effet (échec fermé, GO attribué, preuve). Une
question ne porte aucun effet, n'autorise rien et n'expire pas en refus : c'est la réponse du tour, rendue en boutons
là où le client sait le faire (Telegram, web), en texte numéroté ailleurs. Une option « Autre (à préciser) » est
TOUJOURS ajoutée par le noyau, jamais par le modèle.

Drapeau éteint (défaut) : rien d'ici n'est branché ; le schéma et le résultat de `clarify` restent ceux de
`nokker/tools/registry.py` (test `tests/unit/tools/test_clarify_options.py`).

Trois morceaux :
- `options_entry()` : le schéma enrichi et la fonction de l'outil (textes vus par le modèle : docs/nokkan-host-api-textes.md) ;
- `QuestionBoard` : les questions en attente, en mémoire du processus (une par fil : une nouvelle remplace l'ancienne,
  un message de la personne sur le fil ferme la question) ;
- `Question.view()` : la forme servie aux clients (`question.asked`, `GET /v1/local/questions`, `nokker.question`).
"""
from __future__ import annotations

import logging
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from nokker.core.ports import ToolContext, ToolSpec
from nokker.core.turn import Effect, ToolResult

logger = logging.getLogger("nokker.tools.clarify")

# ─────────────── textes vus par le modèle (gelés : docs/nokkan-host-api-textes.md) ───────────────
# La description de l'outil est celle d'aujourd'hui, mot pour mot (aucun texte créé là où l'existant suffit).
CLARIFY_DESCRIPTION = "Pose une seule question fermée quand il manque une information qui change l'action."
OPTIONS_DESCRIPTION = ("Réponses proposées, 2 à 6, courtes et distinctes. Ne pas y mettre « Autre » : il est "
                       "toujours ajouté.")
OTHER_LABEL = "Autre (à préciser)"
# ──────────────────────────────────────────────────────────────────────────────────────────────

OTHER_ID = "other"
MAX_OPTIONS = 6
MAX_LABEL_CHARS = 80
MAX_QUESTION_CHARS = 1000
MAX_ANSWER_CHARS = 4000
EMPTY_QUESTION = "(question vide)"  # même texte que `registry._clarify`
_OTHER_WORDS = ("autre", "other")


def options_parameters() -> dict[str, Any]:
    return {"type": "object", "properties": {
        "question": {"type": "string"},
        "options": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": MAX_OPTIONS,
                    "description": OPTIONS_DESCRIPTION}}, "required": ["question"]}


def normalize(args: dict[str, Any]) -> tuple[str, list[str]]:
    """(question, libellés) depuis les arguments du modèle : libellés en texte, bornés, sans doublon, sans « Autre »
    (ajouté par le noyau), six au plus. Des options illisibles (pas une liste) : aucune, la question reste posée."""
    question = str(args.get("question", "")).strip()[:MAX_QUESTION_CHARS] or EMPTY_QUESTION
    raw = args.get("options")
    labels: list[str] = []
    seen: set[str] = set()
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict):  # tolérance : {"label": …} au lieu d'une chaîne
            item = item.get("label") or item.get("text") or ""
        if not isinstance(item, (str, int, float)) or isinstance(item, bool):
            continue
        label = " ".join(str(item).split())[:MAX_LABEL_CHARS]
        key = label.casefold()
        if not label or key in seen or key.startswith(_OTHER_WORDS):
            continue
        seen.add(key)
        labels.append(label)
        if len(labels) == MAX_OPTIONS:
            break
    return question, labels


def render_text(question: str, labels: list[str]) -> str:
    """Repli en texte (réponse du tour, gardée dans le fil) : la question, puis les options numérotées et « Autre ».
    Sans option : la question seule, exactement comme le `clarify` d'origine."""
    if not labels:
        return question
    lines = [f"{i}. {label}" for i, label in enumerate([*labels, OTHER_LABEL], start=1)]
    return question + "\n\n" + "\n".join(lines)


def draft(args: dict[str, Any]) -> dict[str, Any]:
    question, labels = normalize(args)
    return {"question": question, "options": [{"id": str(i), "label": label} for i, label in enumerate(labels, 1)]}


def _clarify_options(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    d = draft(args)
    turn = getattr(ctx, "turn", None)
    if turn is not None and hasattr(turn, "question"):
        # Brouillon de la question pour le noyau (`core/loop.py` l'inscrit au tableau et prévient les clients) : posé
        # ICI, donc seulement quand l'outil a vraiment tourné (jamais sur un appel refusé par une porte).
        turn.question = d
    return ToolResult(call_id="", name="clarify",
                      content=render_text(d["question"], [o["label"] for o in d["options"]]))


def options_entry() -> tuple[ToolSpec, Callable[[dict[str, Any], ToolContext], ToolResult]]:
    """(spec, fonction) du `clarify` enrichi : même nom, même description, même effet, un paramètre `options` en plus."""
    spec = ToolSpec(name="clarify", description=CLARIFY_DESCRIPTION, parameters=options_parameters(),
                    effect=Effect.READ, core=True, source="builtin")
    return spec, _clarify_options


class QuestionError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


@dataclass
class Question:
    id: str
    thread_id: str
    turn_index: int
    call_id: str
    question: str
    options: list[dict[str, str]]
    created_at: float
    sender: str | None = None
    channel: str = "api"
    status: str = "pending"  # pending | answered | superseded
    answer: dict[str, Any] | None = None
    answered_at: float | None = None

    def view(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "question_id": self.id, "thread_id": self.thread_id, "turn_index": self.turn_index,
            "turn_id": f"{self.thread_id}:{self.turn_index}", "call_id": self.call_id, "question": self.question,
            "options": [dict(o) for o in self.options], "other": {"id": OTHER_ID, "label": OTHER_LABEL},
            "status": self.status, "created_at": self.created_at}
        if self.answer is not None:
            out["answer"] = dict(self.answer)
            out["answered_at"] = self.answered_at
        return out


@dataclass
class QuestionBoard:
    """Questions posées par `clarify`, en mémoire du processus (perdues au redémarrage : la question reste lisible
    dans le fil, en texte, et se répond par un message)."""

    scrub: Callable[[str], str] | None = None
    # Appelé avec `Question.view()` quand une question se ferme (répondue ou remplacée), hors du verrou : le serveur y
    # branche la publication de `question.answered`. Une panne de ce rappel ne casse ni la réponse ni le tour.
    on_closed: Callable[[dict[str, Any]], None] | None = None
    clock: Callable[[], float] = time.time
    max_kept: int = 500
    _items: OrderedDict = field(default_factory=OrderedDict, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def _clean(self, text: str) -> str:
        return self.scrub(text) if self.scrub is not None else text

    def _closed(self, q: Question | None) -> None:
        if q is None or self.on_closed is None:
            return
        try:
            self.on_closed(q.view())
        except Exception:  # noqa: BLE001
            logger.warning("annonce de la question close en échec", exc_info=True)

    def ask(self, thread_id: str, turn_index: int, call_id: str, d: dict[str, Any], *, sender: str | None = None,
            channel: str = "api") -> tuple[Question, Question | None]:
        """Inscrit la question (texte et libellés caviardés). Rend (question, question remplacée ou None)."""
        options = [{"id": str(o["id"]), "label": self._clean(str(o["label"]))} for o in d.get("options") or []]
        q = Question(id=f"qn_{secrets.token_hex(8)}", thread_id=thread_id, turn_index=turn_index, call_id=call_id,
                     question=self._clean(str(d.get("question") or EMPTY_QUESTION)), options=options,
                     created_at=self.clock(), sender=sender, channel=channel)
        with self._lock:
            old = self._pending_locked(thread_id)
            if old is not None:
                old.status, old.answered_at = "superseded", self.clock()
            self._items[q.id] = q
            while len(self._items) > self.max_kept:
                self._items.popitem(last=False)
        self._closed(old)
        return q, old

    def _pending_locked(self, thread_id: str) -> Question | None:
        return next((q for q in reversed(self._items.values())
                     if q.thread_id == thread_id and q.status == "pending"), None)

    def get(self, question_id: str) -> Question | None:
        with self._lock:
            return self._items.get(question_id)

    def pending(self, thread_id: str | None = None) -> list[Question]:
        with self._lock:
            return [q for q in self._items.values()
                    if q.status == "pending" and (thread_id is None or q.thread_id == thread_id)]

    def answer(self, question_id: str, *, choice: Any = None, text: Any = None, by: str | None = None,
               via: str = "api") -> tuple[Question, str]:
        """Répond à une question en attente : un `choice` (id d'option, ou `other` avec `text`), ou un `text` libre
        seul. Rend (question, texte de la réponse à envoyer comme message de la personne)."""
        if choice is not None and not isinstance(choice, (str, int)) or isinstance(choice, bool):
            raise QuestionError(400, "bad_answer", "choice : identifiant d'option (texte)")
        if text is not None and not isinstance(text, str):
            raise QuestionError(400, "bad_answer", "text : texte attendu")
        free = (text or "").strip()[:MAX_ANSWER_CHARS]
        with self._lock:
            q = self._items.get(question_id)
            if q is None:
                raise QuestionError(404, "unknown_question", "question inconnue")
            if q.status != "pending":
                raise QuestionError(409, "not_pending", f"question déjà close ({q.status})")
            chosen = str(choice) if choice is not None else OTHER_ID
            if chosen == OTHER_ID:
                if not free:
                    raise QuestionError(400, "text_required", "réponse libre : `text` requis")
                label, said = OTHER_LABEL, free
            else:
                opt = next((o for o in q.options if o["id"] == chosen), None)
                if opt is None:
                    raise QuestionError(400, "unknown_option", "option inconnue pour cette question")
                label, said = opt["label"], opt["label"]
            q.status, q.answered_at = "answered", self.clock()
            q.answer = {"choice": chosen, "label": label, "text": free if chosen == OTHER_ID else None,
                        "by": by, "via": via}
        self._closed(q)
        return q, said

    def close_for_turn(self, thread_id: str, text: str) -> Question | None:
        """Un message de la personne arrive sur le fil : il répond à la question en attente (réponse en texte libre,
        sans passer par la route). Rend la question close, ou None."""
        with self._lock:
            q = self._pending_locked(thread_id)
            if q is not None:
                q.status, q.answered_at = "answered", self.clock()
                q.answer = {"choice": OTHER_ID, "label": OTHER_LABEL, "text": self._clean(text)[:MAX_ANSWER_CHARS],
                            "by": None, "via": "message"}
        self._closed(q)
        return q


__all__ = ["CLARIFY_DESCRIPTION", "MAX_OPTIONS", "OPTIONS_DESCRIPTION", "OTHER_ID", "OTHER_LABEL", "Question",
           "QuestionBoard", "QuestionError", "draft", "normalize", "options_entry", "options_parameters",
           "render_text"]
