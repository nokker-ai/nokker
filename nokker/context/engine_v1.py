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

from nokker.context.engine import ContextEngineV0, date_block, estimate_tokens, render_injected
from nokker.context.imported import IMPORT_SOURCE, render_imported
from nokker.context.reply_language import turn_line
from nokker.context.manager import RECALL_TOOL, ContextManager
from nokker.core.ports import Assembled, Recall, ToolSpec
from nokker.core.turn import Tier, ToolResult, Turn
from nokker.memory.provenance import recall_line
from nokker.redact.markers import CONSIGNE as MASKED_CONSIGNE
from nokker.redact.markers import consigne_needed


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

        as_message = mgr.budgets.memory_as_message
        # `recall_context` est-il dans le catalogue du fil ? `core()` exclut déjà les outils retirés (`deny_tools`) et ne
        # le contient pas sans source `RecallContextSource` : sinon l'invitation de la mémoire nommerait un outil absent.
        recall_ok = any(s.name == RECALL_TOOL for s in core_tools)
        memory_text = (mgr.frozen_memory_block(turn.thread_id, recall_ok) if as_message
                       else mgr.memory_block(turn.thread_id, recall_ok))
        index_block = self.tools_index_block(loaded_tools)
        project_text = self.project_block(turn)
        plan_text = self.plan_block(turn)
        # Du plus stable au moins stable (cache de préfixe) : préfixe, index d'outils, profil, mémoire de projet (change
        # quand une consigne du dépôt change ou qu'une règle est notée, au tour suivant), puis la mémoire de session,
        # qui change à la clôture d'un épisode.
        system_parts = self.system_parts(index_block)
        if project_text:
            system_parts.append(project_text)
        if plan_text:  # consignes héritées du plan (X18) : changent quand la carte ou un ancêtre change
            system_parts.append(plan_text)
        if memory_text and not as_message:
            system_parts.append(memory_text)
        messages.append({"role": "system", "content": "\n\n".join(system_parts)})
        if memory_text and as_message:
            # Append-only : le message système (préfixe, index, profil, projet) ne change plus à la clôture d'un épisode ;
            # le résumé est un message `user` de contexte juste après lui, figé jusqu'à la clôture suivante.
            messages.append({"role": "user", "content": memory_text})

        # Couche 4 — l'épisode courant, append-only, chaque tour avec son en-tête d'origine ; les résultats délestés
        # par un stub (état de vue persisté).
        kept = self.images_kept(history, turn)
        for past in history:
            messages.append(self.user_message(past, self.past_user(past, mgr.replay_head(past, st), strip_recall=self.strip_past_recall), kept))
            for m in past.messages:
                d = m.to_openai(thinking=True)
                if m.role == "tool":
                    stub = mgr.shed_stub(past.index, m.tool_call_id, st)
                    if stub is not None:
                        d["content"] = stub
                messages.append(d)

        # Couche 5 — en-tête du tour (même format que V0 + épisodes pertinents après la date), figé par tour.
        def head_parts() -> list[str]:
            parts = [date_block(self.now_fn(), turn)]
            env = self.self_context_for(turn)
            if env:  # où tourne le fil, canal, machines liées : juste après la date, avant les épisodes pertinents
                parts.append(env)
            if self.reply_language or self.reply_language_tail:
                # Ligne de langue du tour (`context.reply_language`), comme V0 (même fonction) : figée avec l'en-tête du
                # tour (append-only), seulement sur un vrai signal de langue. Sans drapeau de langue : en-tête d'avant, à
                # l'octet près (V0 l'écrit sans drapeau ; V1 la garde derrière eux pour ne rien changer ailleurs).
                line = turn_line(turn)
                if line:
                    parts.append(line)
            relevant = mgr.relevant_block(turn.thread_id, turn.input.text, exclude=mgr.layer3_ids(turn.thread_id))
            if relevant:
                parts.append(relevant)
            own = [r for r in recall if getattr(r, "source", "") != IMPORT_SOURCE]
            imported = [r for r in recall if getattr(r, "source", "") == IMPORT_SOURCE]
            if own:
                # Ligne d'avant inchangée ; fait de tiers : marqueur de source + inline() (nokker/memory/provenance.py).
                rappel = "\n".join(recall_line(r, self.recall_text_limit) for r in own)
                parts.append(f"[Rappel mémoire]\n{rappel}\n[/Rappel]")
            if imported:  # lot 30 d : données importées, bloc à part (nokker/context/imported.py)
                parts.append(render_imported(imported, self.recall_text_limit))
            notes = self.feature_notes_block(turn)
            if notes:  # drapeau `feature_notes` : après le rappel mémoire, avant le contexte reçu
                parts.append(notes)
            # Blocs de contexte de l'hôte (`context_blocks`) : seuls les en-têtes ENCORE rejoués comptent comme visibles.
            parts.extend(self.host_context_parts(
                turn, [h.head for h in reversed(history) if h.head and mgr.replay_head(h, st)]))
            if turn.injected:
                parts.append(render_injected(turn.injected))
            if consigne_needed(turn, history):
                # Valeurs masquées sur l'appareil (docs/LOKKAL-PHONE.md §4.3.2) : une fois par fil, au premier tour qui
                # porte un marqueur, juste avant les pièces jointes.
                parts.append(MASKED_CONSIGNE)
            if turn.attached:
                # Pièces jointes texte/dossier/url (docs/CONTEXT-FORMAT.md) : en dernier, juste avant le message.
                parts.append(turn.attached)
            return parts

        head = self.turn_head(turn, head_parts)
        messages.append(self.user_message(turn, self.past_user(turn), kept))
        messages.extend(m.to_openai(thinking=True) for m in turn.messages)

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
        if plan_text:
            layer_tokens["plan"] = estimate_tokens(plan_text)
        return Assembled(messages=messages, tools=tools_payload, prefix_sha256=self._prefix_sha256,
                         layer_tokens=layer_tokens)

    def admit_result(self, result: ToolResult) -> ToolResult:
        return super().admit_result(result)  # sans fil connu (appel hors boucle) : comportement V0


__all__ = ["ContextEngineV1"]
