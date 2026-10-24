"""Outils du modèle pour les bulles (docs/BUBBLES.md §2/§4) — effet READ (`tools/effects.py`), périmètre
borné par le hub : `bubble_update` ne touche qu'une bulle du FIL COURANT ; `session_send` n'écrit qu'à un
fil dont l'utilisateur du tour (`ToolContext.sender`, vérifié) est membre. Un tour lancé par `ask`
(canal `session`) ne peut pas relancer `ask` : pas de ping-pong entre sessions."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from nokker.bubbles.hub import SESSION_CHANNEL, BubbleHub
from nokker.bubbles.types import BubbleError
from nokker.core.ports import ToolContext, ToolSpec
from nokker.core.turn import Effect, ToolResult
from nokker.threads.types import ThreadError

_BUBBLE_UPDATE_PARAMS = {
    "type": "object",
    "properties": {
        "bubble_id": {"type": "string", "description": "id de la bulle (bu_…) du fil courant"},
        "title": {"type": "string"},
        "state": {"type": "object", "description": "champs d'état à modifier (fusionnés avec l'état existant)"},
        "status": {"type": "string", "enum": ["idle", "working", "done", "failed"]},
    },
    "required": ["bubble_id"],
}

_SESSION_SEND_PARAMS = {
    "type": "object",
    "properties": {
        "to_thread": {"type": "string", "description": "id du fil (session) destinataire"},
        "text": {"type": "string", "description": "le message"},
        "ask": {"type": "boolean", "description": "true = la session destinataire répond (tour en tâche de fond)"},
    },
    "required": ["to_thread", "text"],
}

_AGENT_DRAFT_PARAMS = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "nom de l'agent proposé"},
        "role": {"type": "string", "description": "rôle de l'agent proposé"},
        "model": {"type": "string", "description": "modèle/palier prévu"},
        "tools": {"type": "array", "items": {"type": "string"}, "description": "outils prévus"},
        "fill": {"type": "number", "description": "0..1, avancement du brouillon (défaut 1)"},
    },
    "required": ["name", "role"],
}

_BUBBLE_SHOW_PARAMS = {
    "type": "object",
    "properties": {
        "url": {"type": "string", "description": "page à montrer (profil, article, site…), URL complète https://"},
        "title": {"type": "string", "description": "titre court de la bulle (ex. « Instagram @jdupont »)"},
    },
    "required": ["url"],
}

logger = logging.getLogger("nokker.bubbles.tools")

_AGENT_DRAFT_ACTIONS = [{"id": "activate", "label": "Activer", "primary": True}]


@dataclass
class BubbleToolSource:
    hub: BubbleHub
    # `bubble_show` (19.10.2026) : carte d'aperçu (OpenGraph par HTTP, service `nokker/previews/`), cliquable vers la
    # page — pas de navigateur (Nick : pas de ressource par client, ce qui est montré doit être cliquable).
    previews: Any | None = None

    def specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(name="bubble_show", description="MONTRE une page à l'utilisateur dans une nouvelle bulle (carte "
                     "d'aperçu cliquable : image, titre, description) : profil Instagram/LinkedIn, article, site. À "
                     "utiliser dès qu'on te demande d'afficher / montrer quelque chose. Pseudo connu → URL directe "
                     "(https://www.instagram.com/<pseudo>/, https://www.linkedin.com/in/<id>/), pas une recherche. "
                     "Le noyau remplit la carte : n'ajoute ni bubble_update ni navigation derrière.", parameters=_BUBBLE_SHOW_PARAMS,
                     effect=Effect.READ, source="builtin"),
            ToolSpec(name="bubble_update", description="Met à jour une bulle EXISTANTE du fil courant (bubble_id bu_… "
                     "reçu d'un outil) ; pour en créer une, `bubble_show`.",
                     parameters=_BUBBLE_UPDATE_PARAMS, effect=Effect.READ, source="builtin"),
            ToolSpec(name="session_send", description="Écrit à une autre session (fil) de l'utilisateur ; "
                     "ask=true pour qu'elle réponde.", parameters=_SESSION_SEND_PARAMS, effect=Effect.READ,
                     source="builtin"),
            ToolSpec(name="agent_draft", description="Propose une carte de création d'agent (Crew) dans le "
                     "fil courant ; l'utilisateur l'active ensuite (ne crée AUCUN agent par lui-même).",
                     parameters=_AGENT_DRAFT_PARAMS, effect=Effect.READ, source="builtin"),
        ]

    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            if name == "bubble_update":
                state = arguments.get("state")
                if state is not None and not isinstance(state, dict):
                    raise BubbleError("state doit être un objet")
                if state is not None:
                    # Fusion, jamais remplacement : vu en réel le 19.10, le modèle a écrasé l'url et la capture
                    # d'une bulle `bubble_show` avec un chemin de fichier local.
                    current = self.hub.bubbles.get(str(arguments.get("bubble_id", "")))
                    if current is not None:
                        state = {**current.state, **state}
                b = self.hub.tool_bubble_update(ctx.thread_id, str(arguments.get("bubble_id", "")),
                                                title=arguments.get("title"), state=state,
                                                status=arguments.get("status"))
                return ToolResult(call_id="", name=name, content=json.dumps(
                    {"bubble_id": b.id, "version": b.version, "status": b.status}, ensure_ascii=False))
            if name == "bubble_show":
                return self._show(arguments, ctx)
            if name == "session_send":
                out = self.hub.send_message(ctx.thread_id, str(arguments.get("to_thread", "")),
                                            str(arguments.get("text", "")), ask=bool(arguments.get("ask", False)),
                                            sender=ctx.sender, allow_ask=ctx.channel != SESSION_CHANNEL)
                return ToolResult(call_id="", name=name, content=json.dumps(out, ensure_ascii=False))
            if name == "agent_draft":
                try:
                    fill = float(arguments.get("fill", 1.0))
                except (TypeError, ValueError):
                    fill = 1.0
                fill = max(0.0, min(1.0, fill))
                state = {"name": str(arguments.get("name", "")), "role": str(arguments.get("role", "")),
                         "model": str(arguments.get("model", "")),
                         "tools": [str(t) for t in (arguments.get("tools") or [])], "fill": fill}
                b = self.hub.create_bubble(ctx.thread_id, ctx.sender, kind="agent_draft",
                                           title=state["name"] or "Nouvel agent", state=state,
                                           actions=list(_AGENT_DRAFT_ACTIONS))
                return ToolResult(call_id="", name=name, content=json.dumps(
                    {"bubble_id": b.id}, ensure_ascii=False))
        except (BubbleError, ThreadError) as e:
            return ToolResult(call_id="", name=name, content=f"refusé : {type(e).__name__} {e}", is_error=True)
        return ToolResult(call_id="", name=name, content=f"outil inconnu : {name}", is_error=True)

    def _show(self, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        import re

        raw = str(arguments.get("url") or "")
        m = re.search(r"https?://\S+", raw)
        if not m:
            return ToolResult(call_id="", name="bubble_show", content="URL https:// requise.", is_error=True)
        url = m.group(0).rstrip(").,;")
        title = str(arguments.get("title") or "").strip() or url
        b = self.hub.create_bubble(ctx.thread_id, ctx.sender, kind="media", title=title[:200],
                                   state={"url": url, "mime": "text/html", "poster": None, "preview": None},
                                   status="working")

        def fill() -> None:
            state: dict[str, Any] = {"url": url, "mime": "text/html", "poster": None, "preview": None}
            if self.previews is not None:
                try:
                    p = self.previews.preview(url)
                    if not p.get("error"):
                        state["preview"] = p
                        if title == url and p.get("title"):
                            self.hub.update_from_kernel(b.id, title=str(p["title"])[:200])
                except Exception:  # noqa: BLE001
                    logger.info("bubble_show %s : aperçu indisponible", url)
            self.hub.update_from_kernel(b.id, state=state, status="done" if state["preview"] else "failed")

        self.hub.spawn(fill)
        return ToolResult(call_id="", name="bubble_show", content=json.dumps(
            {"bubble_id": b.id, "shown": url,
             "note": "bulle affichée chez l'utilisateur ; le noyau y pose l'aperçu lui-même. Rien d'autre à faire "
                     "sur cette bulle : réponds en une phrase, sans décrire une page que tu n'as pas lue."},
            ensure_ascii=False))


__all__ = ["BubbleToolSource"]
