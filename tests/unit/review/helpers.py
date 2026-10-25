"""Aides des tests de `nokker review` : transport scripté du juge, réponse OpenAI-compatible."""
from __future__ import annotations

import json


def model_reply(obj) -> dict:
    """Réponse OpenAI-compatible dont le contenu est `obj` (dict → JSON, str telle quelle)."""
    content = obj if isinstance(obj, str) else json.dumps(obj)
    return {"model": "fake-judge", "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20}}


class ScriptedTransport:
    """Transport du juge sans réseau : `reply(body)` rend le contenu ; garde chaque corps envoyé."""

    def __init__(self, reply):
        self.reply = reply
        self.bodies: list[dict] = []

    def post(self, body):
        self.bodies.append(body)
        out = self.reply(body)
        if isinstance(out, Exception):
            raise out
        return model_reply(out)

    def sent_text(self) -> str:
        return "\n".join(m["content"] for b in self.bodies for m in b["messages"])
