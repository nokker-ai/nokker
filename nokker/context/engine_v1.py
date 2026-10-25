"""Context Engine v1 (`core.ports.ContextEngine`) — A-1/A-4 (docs/NEVER-CLEAR.md). Sous-classe de `ContextEngineV0`
(laissé INTACT : rétrocompatibilité des tests et du canari) ; il ajoute :

  couche 3  la mémoire de session (`[Mémoire de la session]`), dans le message `system` UNIQUE, après le profil ;
  couche 4  l'épisode courant seulement (la boucle lui passe `history_window`, pas tout le journal), les résultats
            délestés rendus en stub ;
  couche 5  `[Épisodes pertinents]` (rappel épisodique) entre la date et le rappel CortHeXis ;
  réservoir à l'admission d'un résultat long (`admit_result_in`).

Le préfixe (couche 1) reste indépendant du palier, du modèle et de l'heure : le hash est celui de V0."""
from __future__ import annotations

from dataclasses import dataclass, field

from nokker.context.engine import ContextEngineV0, estimate_tokens, format_now_fr, render_injected
from nokker.context.manager import ContextManager
from nokker.core.ports import Assembled, Recall, ToolSpec
from nokker.core.turn import Tier, ToolResult, Turn


@dataclass
class ContextEngineV1(ContextEngineV0):
    manager: ContextManager | None = field(default=None)

    # ───────── crochets optionnels appelés par `core/loop.py` (via getattr) ─────────

    def history_window(self, thread_id: str) -> list[Turn]:
        return self.manager.history_window(thread_id)

    def before_turn(self, thread_id: str, text: str) -> str | None:
        return self.manager.before_turn(thread_id, text)

    def new_episode(self, thread_id: str, reason: str = "marker") -> str | None:
        return self.manager.new_episode(thread_id, reason)

    def after_turn(self, turn: Turn) -> None:
        self.manager.after_turn(turn)

    def admit_result_in(self, thread_id: str, turn_index: int, result: ToolResult) -> ToolResult:
        return self.manager.admit_result_in(thread_id, turn_index, result)

    # ───────── assemblage ─────────

    def assemble(self, turn: Turn, history: list[Turn], recall: list[Recall], loaded_tools: list[ToolSpec],
                 tier: Tier) -> Assembled:
        mgr = self.manager
        assert mgr is not None, "ContextEngineV1 sans manager"
        core_tools = self.tools.core()
        st = mgr.state(turn.thread_id)
        messages: list[dict] = []

        memory_text = mgr.memory_block(turn.thread_id)
        index_block = self.tools_index_block()
        project_text = self.project_block(turn)
        # Du plus stable au moins stable (cache de préfixe) : préfixe, index d'outils, profil, mémoire de projet (change
        # quand une consigne du dépôt change ou qu'une règle est notée, au tour suivant), puis la mémoire de session,
        # qui change à la clôture d'un épisode.
        system_parts = self.system_parts(index_block)
        if project_text:
            system_parts.append(project_text)
        if memory_text:
            system_parts.append(memory_text)
        messages.append({"role": "system", "content": "\n\n".join(system_parts)})

        # Couche 4 — l'épisode courant, append-only, chaque tour avec son en-tête d'origine ; les résultats délestés
        # par un stub (état de vue persisté).
        kept = self.images_kept(history, turn)
        for past in history:
            messages.append(self.user_message(past, self.past_user(past, mgr.replay_head(past, st)), kept))
            for m in past.messages:
                d = m.to_openai()
                if m.role == "tool":
                    stub = mgr.shed_stub(past.index, m.tool_call_id, st)
                    if stub is not None:
                        d["content"] = stub
                messages.append(d)

        # Couche 5 — en-tête du tour (même format que V0 + épisodes pertinents après la date), figé par tour.
        def head_parts() -> list[str]:
            parts = [f"[Date et heure actuelles]\n{format_now_fr(self.now_fn())}\n[/Date]"]
            relevant = mgr.relevant_block(turn.thread_id, turn.input.text, exclude=mgr.layer3_ids(turn.thread_id))
            if relevant:
                parts.append(relevant)
            if recall:
                rappel = "\n".join(
                    f"- [{r.note}] (score {r.score:.2f}) {r.text[:self.recall_text_limit]}" for r in recall)
                parts.append(f"[Rappel mémoire]\n{rappel}\n[/Rappel]")
            if turn.injected:
                parts.append(render_injected(turn.injected))
            return parts

        head = self.turn_head(turn, head_parts)
        messages.append(self.user_message(turn, self.past_user(turn), kept))
        messages.extend(m.to_openai() for m in turn.messages)

        tools_payload = [s.to_openai() for s in core_tools] + [s.to_openai() for s in loaded_tools]
        layer_tokens = {
            "prefix": estimate_tokens(self._prefix_text),
            "tools_index": estimate_tokens(index_block),
            "profile": estimate_tokens(self._profile_text),
            "episodes": estimate_tokens(memory_text),
            "current": sum(mgr._turn_tokens(p, st) for p in history),
            "turn_block": estimate_tokens("\n\n".join([head, turn.input.text])),
        }
        if self.project_memory is not None:
            layer_tokens["project"] = estimate_tokens(project_text)
        return Assembled(messages=messages, tools=tools_payload, prefix_sha256=self._prefix_sha256,
                         layer_tokens=layer_tokens)

    def admit_result(self, result: ToolResult) -> ToolResult:
        return super().admit_result(result)  # sans fil connu (appel hors boucle) : comportement V0


__all__ = ["ContextEngineV1"]
