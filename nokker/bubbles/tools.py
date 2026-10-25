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
        "instructions": {"type": "string", "description": "la mission, message de chaque passage"},
        "trigger": {"type": "object", "description": "{kind: manual} | {kind: schedule, cron: \"0 7 * * 1-5\", "
                    "tz: \"Europe/Zurich\"} | {kind: once, at: ISO 8601} | {kind: event, event: "
                    "\"webhook:<id>\" | \"alert:<nom>\"}",
                    "properties": {"kind": {"type": "string", "enum": ["manual", "schedule", "once", "event"]}}},
        "secrets": {"type": "array", "items": {"type": "string"}, "description": "NOMS de secrets du coffre "
                    "(jamais une valeur)"},
        "budget": {"type": "object", "description": "{max_calls, max_cost, currency} par passage"},
        "auto_approve": {"type": "array", "items": {"type": "string"}, "description": "règles outil(motif) "
                         "levées sans carte pendant un passage ; une écriture demandera une carte forte à l'armement"},
    },
    "required": ["name", "role"],
}

# Sans moteur d'agents (la bêta : `AgentsHub.engine` absent), rien ne tourne à une échéance ni sur un événement, et un
# agent qui lève une règle reste au repos : le modèle ne voit que le déclencheur manuel (revue du train du 2.11, §0.4).
_AGENT_DRAFT_PARAMS_NO_ENGINE = {
    "type": "object",
    "properties": {
        **{k: v for k, v in _AGENT_DRAFT_PARAMS["properties"].items() if k not in ("trigger", "auto_approve")},
        "trigger": {"type": "object", "description": "{kind: manual} seulement sur cette instance (pas de moteur "
                    "d'agents) : aucun passage à heure fixe, à une date ou sur un événement",
                    "properties": {"kind": {"type": "string", "enum": ["manual"]}}},
    },
    "required": ["name", "role"],
}
_AGENT_DRAFT_DESC = ("Propose une carte de création d'agent (Crew) dans le fil courant ; l'utilisateur l'active ensuite "
                     "(ne crée AUCUN agent par lui-même).")
_AGENT_DRAFT_DESC_NO_ENGINE = (_AGENT_DRAFT_DESC + " Cette instance n'a pas de moteur d'agents : déclencheur manuel "
                               "seulement, l'agent ne passe ni à heure fixe, ni à une date, ni sur un événement. Ne "
                               "propose pas de passage programmé ; si l'utilisateur en demande un, dis-lui que ce "
                               "n'est pas disponible ici.")
NO_ENGINE_DRAFT = ("carte refusée : pas de moteur d'agents sur cette instance, les agents programmés (heure fixe, date, "
                   "événement) ne sont pas disponibles. Propose la carte avec trigger {\"kind\": \"manual\"}, et dis-le "
                   "à l'utilisateur.")


def agents_engine(hub: BubbleHub) -> bool:
    """Le moteur des agents tourne-t-il sur cette instance ? (même condition que `AgentsHub._check_engine`)"""
    agents = getattr(hub, "agents_hub", None)
    return agents is not None and getattr(agents, "engine", None) is not None


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


def propose_agent_draft(hub: BubbleHub, ctx: ToolContext, arguments: dict[str, Any],
                        extra_state: dict[str, Any] | None = None, *, scheduled: bool = True) -> ToolResult:
    """Pose une bulle `agent_draft` dans le fil du tour (§9.4/§9.5) : ne crée AUCUN agent. `extra_state` : champs
    de la carte venus d'un pack — `pack` et `template` (information, ignorés à l'activation) et `needs` (X15), que
    l'activation passe à l'agent pour le placement. Partagé par l'outil `agent_draft` et par `pack_install` (lot
    X16). `scheduled` faux (pas de moteur) :
    un déclencheur autre que `manual` est refusé tout de suite, avec la consigne pour le modèle."""
    try:
        fill = float(arguments.get("fill", 1.0))
    except (TypeError, ValueError):
        fill = 1.0
    fill = max(0.0, min(1.0, fill))
    state: dict[str, Any] = {"name": str(arguments.get("name", "")), "role": str(arguments.get("role", "")),
                             "model": str(arguments.get("model", "")),
                             "tools": [str(t) for t in (arguments.get("tools") or [])], "fill": fill}
    # §9.5 : ce que l'activation créera, validé tout de suite (le modèle corrige un cron faux, un secret donné en
    # valeur, une écriture sur un déclencheur d'événement) — mêmes règles que POST /v1/agents.
    from nokker.agents.spec import norm_budget, norm_rules, norm_secrets, norm_trigger
    from nokker.agents.types import AgentError

    try:
        state.update({"instructions": str(arguments.get("instructions") or "")[:20_000],
                      "trigger": norm_trigger(arguments.get("trigger")),
                      "secrets": norm_secrets(arguments.get("secrets")),
                      "budget": norm_budget(arguments.get("budget")),
                      "auto_approve": norm_rules(arguments.get("auto_approve"))})
    except AgentError as e:
        return ToolResult(call_id="", name="agent_draft", content=f"carte refusée : {e}", is_error=True)
    if not scheduled and state["trigger"].get("kind") != "manual":
        return ToolResult(call_id="", name="agent_draft", content=NO_ENGINE_DRAFT, is_error=True)
    for key, value in (extra_state or {}).items():
        state.setdefault(key, value)  # jamais par-dessus un champ d'agent
    b = hub.create_bubble(ctx.thread_id, ctx.sender, kind="agent_draft", title=state["name"] or "Nouvel agent",
                          state=state, actions=list(_AGENT_DRAFT_ACTIONS))
    return ToolResult(call_id="", name="agent_draft", content=json.dumps({"bubble_id": b.id}, ensure_ascii=False))


@dataclass
class BubbleToolSource:
    hub: BubbleHub
    # `bubble_show` (19.10.2026) : carte d'aperçu (OpenGraph par HTTP, service `nokker/previews/`), cliquable vers la
    # page — pas de navigateur (décision produit : pas de ressource par client, ce qui est montré doit être cliquable).
    previews: Any | None = None

    def specs(self) -> list[ToolSpec]:
        engine = agents_engine(self.hub)
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
            # Schéma et description selon l'instance (moteur d'agents ou non), relus à chaque `specs()`.
            ToolSpec(name="agent_draft", description=_AGENT_DRAFT_DESC if engine else _AGENT_DRAFT_DESC_NO_ENGINE,
                     parameters=_AGENT_DRAFT_PARAMS if engine else _AGENT_DRAFT_PARAMS_NO_ENGINE, effect=Effect.READ,
                     source="builtin"),
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
                return propose_agent_draft(self.hub, ctx, arguments, scheduled=agents_engine(self.hub))
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


__all__ = ["NO_ENGINE_DRAFT", "BubbleToolSource", "agents_engine", "propose_agent_draft"]
