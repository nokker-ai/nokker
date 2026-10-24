"""Rendu compact d'une bulle pour le contexte d'un fil (docs/BUBBLES.md §3).

`render_for_context` produit le bloc injecté au PROCHAIN tour du fil d'arrivée :
`[Bulle reçue de « <fil source> » — <kind> « <titre> »] <résumé ≤ 600 caractères de state>`.
Le résumé est borné (`SUMMARY_MAX_CHARS`) et passe par le scrubber des invariants (`scrub`, en pratique
`DeterministicInvariants.scrub_output`) : une bulle ne fait jamais entrer un secret dans un autre fil."""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from nokker.bubbles.types import Bubble

SUMMARY_MAX_CHARS = 600


def _clip(text: str, n: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: max(0, n - 1)] + "…"


def summarize_state(kind: str, state: dict[str, Any]) -> str:
    s = state or {}
    if kind == "search":
        parts = [f"recherche « {s.get('query', '')} »"]
        for i, r in enumerate((s.get("results") or [])[:5], 1):
            if isinstance(r, dict):
                parts.append(f"{i}. {r.get('title') or r.get('url', '')} ({r.get('site') or r.get('url', '')})")
        return " ; ".join(parts)
    if kind == "monitor":
        head = f"{s.get('target', '')} {s.get('metric', '')} = {s.get('value', '')}{s.get('unit', '') or ''}"
        if s.get("max") is not None:
            head += f" / {s['max']}{s.get('unit', '') or ''}"
        items = [f"{i.get('label')}: {i.get('value')}" for i in (s.get("items") or [])[:6] if isinstance(i, dict)]
        return head + (" ; " + " ; ".join(items) if items else "")
    if kind == "timer":
        return f"minuteur « {s.get('label', '')} » jusqu'à {s.get('ends_at', '')}"
    if kind == "script":
        tail = " | ".join(str(x) for x in (s.get("log_tail") or [])[-5:])
        code = f" (code {s['exit_code']})" if s.get("exit_code") is not None else ""
        return f"$ {s.get('command', '')}{code} {tail}".strip()
    if kind == "media":
        return f"{s.get('mime', '')} {s.get('url', '')}".strip()
    if kind == "notify":
        return f"{s.get('from_title', '')} : {s.get('text', '')}"
    if kind == "agent_draft":
        return f"agent {s.get('name', '')} ({s.get('role', '')}, {s.get('model', '')})"
    if kind == "approval":
        return f"approbation {s.get('approval_id', '')}"
    if kind == "episode":
        return (f"épisode « {s.get('title', '')} » ({s.get('turns', '?')} tours, {s.get('day', '?')}) : "
                f"{s.get('summary', '')}")
    return json.dumps(s, ensure_ascii=False, sort_keys=True)


def render_for_context(bubble: Bubble, from_title: str, scrub: Callable[[str], str] | None = None) -> str:
    summary = _clip(summarize_state(bubble.kind, bubble.state), SUMMARY_MAX_CHARS)
    if scrub is not None:
        summary = _clip(scrub(summary), SUMMARY_MAX_CHARS)
    head = f"[Bulle reçue de « {_clip(from_title, 80)} » — {bubble.kind} « {_clip(bubble.title, 80)} »]"
    if scrub is not None:
        head = scrub(head)
    return f"{head} {summary}".strip()


def render_sent(bubble: Bubble, to_title: str) -> str:
    return f"[Bulle envoyée vers « {_clip(to_title, 80)} » — {bubble.kind} « {_clip(bubble.title, 80)} »]"


def render_session_message(from_title: str, text: str, scrub: Callable[[str], str] | None = None, *,
                           reply: bool = False, max_chars: int = 2000) -> str:
    body = _clip(text, max_chars)
    if scrub is not None:
        body = scrub(body)
    tag = "Réponse de la session" if reply else "Message de la session"
    return f"[{tag} « {_clip(from_title, 80)} »] {body}"


__all__ = ["SUMMARY_MAX_CHARS", "render_for_context", "render_sent", "render_session_message",
           "summarize_state"]
