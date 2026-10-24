"""Transcription lisible et bornée d'un épisode (relecture `recall_context`, export vers un autre fil).
Les tours stockés sont déjà caviardés par les invariants ; l'appelant repasse tout par son scrubber."""
from __future__ import annotations

import json

from nokker.context.text import clip
from nokker.core.turn import Turn


def render_episode(turns: list[Turn], max_chars: int, *, tool_result_chars: int = 400) -> str:
    lines: list[str] = []
    for t in turns:
        if t.injected:
            lines.append(f"[T{t.index}] Contexte reçu : " + clip(" ".join(t.injected), 300))
        lines.append(f"[T{t.index}] Utilisateur : {clip(t.input.text, 1200)}")
        for m in t.messages:
            if m.role == "assistant":
                for tc in m.tool_calls:
                    args = json.dumps(tc.arguments, ensure_ascii=False, sort_keys=True) if tc.arguments else ""
                    lines.append(f"[T{t.index}] Outil {tc.name}({clip(args, 160)})")
            elif m.role == "tool":
                lines.append(f"[T{t.index}] → {m.name or 'outil'} : {clip(m.content, tool_result_chars)}")
        if t.reply:
            lines.append(f"[T{t.index}] Réponse : {clip(t.reply, 1200)}")
    text = "\n".join(lines)
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 40)].rstrip() + f"\n[… transcription tronquée à {max_chars} car.]"


__all__ = ["render_episode"]
