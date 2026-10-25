"""Résumeur LLM d'un épisode, avec GARDE ANTI-INVENTION (docs/NEVER-CLEAR.md §5).

Chemin hors-critique (consolidation) : le modèle reformule l'épisode ; tout nombre, identifiant technique ou nom
propre du résumé doit figurer dans la transcription source, sinon un réessai (violations listées dans le prompt)
puis, en dernier recours, le résumeur déterministe de repli. Jamais d'exception vers l'appelant."""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from typing import Any

from nokker.context.types import Summarizer, SummaryResult

log = logging.getLogger("nokker.context.summarize_llm")

SESSION_KEY = "nokker-summarizer"

# ───────────────────────────── transcription bornée ─────────────────────────────

_ARGS_MAX = 160  # arguments d'outil abrégés
_RESULT_MAX = 400  # résultat d'outil tronqué
_TEXT_MAX = 1500  # texte utilisateur / réponse (avant partage du budget global)


def _cut(text: str, limit: int) -> str:
    text = " ".join(str(text).split()) if limit <= _ARGS_MAX else str(text).strip()
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


def _args_str(arguments: Any, raw: str = "") -> str:
    try:
        s = json.dumps(arguments, ensure_ascii=False) if arguments else (raw or "")
    except (TypeError, ValueError):
        s = raw or str(arguments)
    return _cut(s, _ARGS_MAX)


def _render_turn(turn: Any, cap: int) -> str:
    """Un tour en texte : `[T<index>]` puis utilisateur, appels d'outils, résultats, réponse."""
    lines = [f"[T{getattr(turn, 'index', '?')}]"]
    inp = getattr(turn, "input", None)
    user = getattr(inp, "text", "") if inp is not None else ""
    if user:
        lines.append("Utilisateur : " + _cut(user, min(_TEXT_MAX, cap)))
    last_assistant = ""
    for m in getattr(turn, "messages", None) or []:
        role = getattr(m, "role", "")
        content = getattr(m, "content", "") or ""
        if role == "assistant":
            if content:
                lines.append("Assistant : " + _cut(content, min(_TEXT_MAX, cap)))
                last_assistant = content
            for tc in getattr(m, "tool_calls", None) or []:
                lines.append(f"Outil appelé : {tc.name}({_args_str(tc.arguments, getattr(tc, 'raw_arguments', ''))})")
        elif role == "tool":
            name = getattr(m, "name", None) or "outil"
            lines.append(f"Résultat {name} : " + _cut(content, min(_RESULT_MAX, cap)))
    reply = getattr(turn, "reply", "") or ""
    if reply and reply.strip() != last_assistant.strip():
        lines.append("Assistant : " + _cut(reply, min(_TEXT_MAX, cap)))
    return "\n".join(lines)


def render_transcript(turns: list[Any], max_chars: int) -> str:
    """Transcription lisible et BORNÉE (≤ `max_chars`) : budget partagé entre les tours, chaque élément tronqué."""
    if not turns or max_chars <= 0:
        return ""
    cap = max(200, max_chars // len(turns))
    out = "\n\n".join(_render_turn(t, cap) for t in turns)
    if len(out) > max_chars:
        out = out[: max_chars - 1].rstrip() + "…"
    return out


# ───────────────────────────── garde anti-invention ─────────────────────────────

_SEP_SPACES = "   '’"
_THOUSANDS = re.compile(rf"(?<=\d)[{_SEP_SPACES}](?=\d{{3}}(?!\d))")
_NUMBER = re.compile(r"(?<![A-Za-z_\d])\d+(?:[.,:/\-]\d+)*")

_IDENT_PATTERNS = [
    re.compile(r"\b(?:rs|ep|th|bu|dy)_[A-Za-z0-9]+", re.IGNORECASE),
    re.compile(r"\bT\d+\b"),
    re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),
    re.compile(r"https?://[^\s<>\"')\]]+"),
    re.compile(r"\b[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*\.[a-z]{2,}\b", re.IGNORECASE),  # hôtes / noms de domaine
    re.compile(r"(?<![\w/])(?:~|\.{1,2})?/[\w.\-]+(?:/[\w.\-]+)*"),  # chemins absolus / relatifs
    re.compile(r"\b[\w.\-]+(?:/[\w.\-]+)+\.[A-Za-z0-9]+\b"),  # dossier/fichier.ext
]
_PROPER = re.compile(r"[A-ZÀ-ÖØ-Ý][A-Za-zÀ-ÖØ-öø-ÿ'’\-]{2,}")
_SENTENCE_START = re.compile(r"(?:^\s*|[.!?…:;]\s+|\n\s*(?:[-*•]\s*)?|^\s*[-*•]\s+|[\"«(\[]\s*)$")


def _strip_trailing(s: str) -> str:
    return s.rstrip(".,;:!?)]»\"'")


def _identifiers(text: str) -> list[str]:
    found: list[str] = []
    for pat in _IDENT_PATTERNS:
        for m in pat.finditer(text):
            tok = _strip_trailing(m.group(0))
            if len(tok) >= 2:
                found.append(tok.lower())
    return found


def _num_keys(token: str) -> set[str]:
    """Formes canoniques d'un nombre : séparateur décimal unifié, zéros de tête/queue retirés ; « 1,234 » est
    ambigu (milliers ou décimale), donc accepté sous les deux lectures."""
    parts = re.split(r"[.,:/\-]", token)
    seps = re.findall(r"[.,:/\-]", token)
    ints = [str(int(p)) for p in parts]
    keys = {"|".join(ints)}
    if len(parts) == 2 and seps[0] in ".,":
        dec = parts[1].rstrip("0")
        keys = {f"{int(parts[0])}.{dec}" if dec else str(int(parts[0]))}
        keys.add("|".join(ints))
        if len(parts[1]) == 3:
            keys.add(str(int(parts[0] + parts[1])))
    elif len(parts) > 2 and all(s in ".," for s in seps) and all(len(p) == 3 for p in parts[1:]):
        keys.add(str(int("".join(parts))))
    return keys


def _numbers(text: str) -> list[tuple[str, set[str]]]:
    for pat in _IDENT_PATTERNS:  # un identifiant n'est pas un nombre (ses chiffres sont vérifiés comme identifiant)
        text = pat.sub(" ", text)
    text = _THOUSANDS.sub("", text)
    out: list[tuple[str, set[str]]] = []
    for m in _NUMBER.finditer(text):
        tok = m.group(0)
        try:
            out.append((tok, _num_keys(tok)))
        except ValueError:
            continue
    return out


def _proper_nouns(text: str) -> list[str]:
    for pat in _IDENT_PATTERNS:
        text = pat.sub(" ", text)
    found: list[str] = []
    for m in _PROPER.finditer(text):
        if _SENTENCE_START.search(text[: m.start()]) is not None:
            continue
        found.append(m.group(0))
    return found


def grounded_violations(summary: str, source_text: str) -> list[str]:
    """Éléments du résumé ABSENTS de la source (nombres, identifiants techniques, noms propres). Liste vide =
    fidèle. Comparaison sans casse ; nombres normalisés (milliers, virgule/point, zéros de tête)."""
    low_source = source_text.lower()
    violations: list[str] = []
    seen: set[str] = set()

    def add(v: str) -> None:
        if v.lower() not in seen:
            seen.add(v.lower())
            violations.append(v)

    src_keys: set[str] = set()
    for _tok, keys in _numbers(source_text):
        src_keys |= keys
    for tok, keys in _numbers(summary):
        if not (keys & src_keys):
            add(tok)
    for ident in _identifiers(summary):
        if ident not in low_source:
            add(ident)
    for noun in _proper_nouns(summary):
        if not re.search(rf"(?<![\w]){re.escape(noun.lower())}(?![\w])", low_source):
            add(noun)
    return violations


# ───────────────────────────── résumeur LLM ─────────────────────────────

_SYSTEM = (
    "Tu résumes un épisode de conversation entre un utilisateur et un agent, en français, de façon factuelle.\n"
    "RÈGLE ABSOLUE : n'invente RIEN. Chaque nombre, nom propre et identifiant (rs_…, ep_…, th_…, bu_…, hôte, chemin, "
    "adresse mail, URL) de ta sortie doit figurer tel quel dans la transcription. Pas de date, de chiffre ni de nom "
    "déduit ou arrondi. Un résumé court et vrai vaut mieux qu'un résumé riche et faux.\n"
    "Sortie : UN SEUL objet JSON, sans texte autour : "
    '{"title": "titre court", "summary": "résumé en quelques phrases", "keywords": ["mot-clé", …], '
    '"facts": ["fait durable", …]}.\n'
    "« facts » = uniquement des faits DURABLES et vérifiables (préférence de l'utilisateur, décision prise, "
    "identifiant, configuration) formulés en une phrase autonome ; liste vide s'il n'y en a pas."
)


def _build_prompt(transcript: str, previous: str | None, violations: list[str] | None) -> list[dict[str, Any]]:
    user = ""
    if previous:
        user += f"Résumé précédent à améliorer (ancre ; reste fidèle à la transcription) :\n{previous}\n\n"
    user += f"Transcription de l'épisode :\n{transcript}\n"
    msgs: list[dict[str, Any]] = [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}]
    if violations:
        msgs.append({"role": "user", "content": (
            "Ta réponse précédente contenait des éléments ABSENTS de la transcription : "
            + ", ".join(repr(v) for v in violations)
            + ". Refais le JSON sans aucun de ces éléments (supprime-les ou reformule sans les citer).")})
    return msgs


def _parse_json(text: str) -> dict[str, Any] | None:
    """JSON tolérant : brut, bloc ```json, ou premier objet trouvé dans du texte."""
    candidates = [text.strip()]
    candidates += re.findall(r"```(?:json)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    for cand in candidates:
        try:
            obj = json.loads(cand)
            if isinstance(obj, dict):
                return obj
        except ValueError:
            pass
    dec = json.JSONDecoder()
    for m in re.finditer(r"\{", text):
        try:
            obj, _ = dec.raw_decode(text[m.start():])
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _str_list(v: Any) -> list[str]:
    return [s.strip() for s in v if isinstance(s, str) and s.strip()] if isinstance(v, list) else []


class LLMSummarizer:
    """`complete` : un `Provider` (méthode `complete`) ou une fonction `(messages) -> str` (tests)."""

    def __init__(self, complete: Any, fallback: Summarizer, max_tokens: int = 400, max_source_chars: int = 24000,
                 retries: int = 1) -> None:
        self._complete = complete
        self.fallback = fallback
        self.max_tokens = max_tokens
        self.max_source_chars = max_source_chars
        self.retries = max(0, retries)

    def _call(self, messages: list[dict[str, Any]]) -> tuple[str, str | None]:
        from nokker.qa.secrets import redact_messages

        # La transcription part en rôle `user` : réponses et arguments d'outils du modèle n'y sont pas passés par une porte
        # de caviardage (lot fix-batch-1, filet du fil).
        messages = redact_messages(messages)
        c = self._complete
        if hasattr(c, "complete"):
            comp = c.complete(messages, [], reasoning=False, session_key=SESSION_KEY, max_tokens=self.max_tokens)
            return (getattr(comp.message, "content", "") or ""), getattr(comp, "model", None)
        if callable(c):
            res: Callable[..., Any] | Any = c(messages)
            if isinstance(res, str):
                return res, None
            msg = getattr(res, "message", None)
            return (getattr(msg, "content", "") or ""), getattr(res, "model", None)
        raise TypeError("complete doit être un Provider ou une fonction (messages) -> str")

    def summarize(self, turns: list[Any], previous: str | None = None) -> SummaryResult:
        try:
            res = self._summarize(turns, previous)
        except Exception as e:  # noqa: BLE001 — jamais d'exception vers l'appelant : repli déterministe
            log.warning("résumé LLM échoué (%s: %s) — repli extractif", type(e).__name__, e)
            res = None
        return res if res is not None else self.fallback.summarize(turns, previous)

    def _summarize(self, turns: list[Any], previous: str | None) -> SummaryResult | None:
        transcript = render_transcript(turns, self.max_source_chars)
        if not transcript:
            return None
        source = transcript + ("\n" + previous if previous else "")
        violations: list[str] | None = None
        for attempt in range(self.retries + 1):
            text, model = self._call(_build_prompt(transcript, previous, violations))
            obj = _parse_json(text)
            if obj is None:
                log.warning("résumé LLM : JSON invalide — repli extractif")
                return None
            summary = obj.get("summary")
            summary = summary.strip() if isinstance(summary, str) else ""
            if not summary:
                log.warning("résumé LLM vide — repli extractif")
                return None
            if len(summary) > len(transcript):
                log.warning("résumé LLM plus long que la source — repli extractif")
                return None
            violations = grounded_violations(summary, source)
            if violations:
                log.warning("résumé LLM : %d élément(s) non fondé(s) (essai %d) : %s", len(violations), attempt + 1,
                            violations[:8])
                continue
            title = obj.get("title") if isinstance(obj.get("title"), str) else ""
            title = title.strip()
            if not title or grounded_violations(title, source):
                title = summary[:60].rstrip()
            facts = [f for f in _str_list(obj.get("facts")) if not grounded_violations(f, source)]
            return SummaryResult(title=title, summary=summary, keywords=_str_list(obj.get("keywords")),
                                 facts=facts, model=model)
        return None


__all__ = ["LLMSummarizer", "grounded_violations", "render_transcript"]
