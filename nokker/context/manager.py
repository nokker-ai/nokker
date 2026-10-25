"""`ContextManager` — la fenêtre comme VUE calculée (docs/NEVER-CLEAR.md). Il orchestre épisodes, réservoir, délestage et
rappel épisodique ; `engine_v1.ContextEngineV1` en est la façade `core.ports.ContextEngine`.

Cycle d'un tour vu du gestionnaire :
  before_turn  → état de vue (amorçage d'un fil existant), frontières d'entrée (`/new`, silence, sujet) ;
  history_window / assemble → ce que le modèle voit (épisode courant brut + mémoire de session + épisodes pertinents) ;
  admit_result_in → un résultat long entre au réservoir, le contexte ne reçoit qu'un extrait ;
  after_turn   → comptage, délestage (compaction) ou clôture d'épisode au seuil.

Le journal (`turns`) n'est JAMAIS modifié ; tout ce qui bouge (épisodes, état de vue, réservoir) est à part."""
from __future__ import annotations

import hashlib
import logging
import math
import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from nokker.context import index as episode_index
from nokker.context.memory import MemoryEpisodeStore, MemoryReservoirStore
from nokker.context.summarize import ExtractiveSummarizer, message_chars, turn_tokens
from nokker.context.text import (
    clip,
    constraint_clauses,
    content_words,
    distinctive_words,
    is_constraint,
    is_revocation,
    sentences,
)
from nokker.context.transcript import render_episode
from nokker.context.types import (
    ContextBudgets,
    Episode,
    EpisodeStore,
    ReservoirItem,
    ReservoirStore,
    Summarizer,
    ViewState,
    estimate_tokens,
    new_episode_id,
    new_reservoir_id,
)
from nokker.core.turn import ToolResult, Turn

logger = logging.getLogger("nokker.context.manager")
ZURICH = ZoneInfo("Europe/Zurich")
STUB_PREVIEW_CHARS = 200
MARKER_NEW = "/new"
REVOCATIONS_K = 6  # revirements gardés en couche 3 (les plus récents)
REVOCATION_HEADER = ("Revirements de la session, du plus ancien au plus récent : chacun REMPLACE ce qui avait été décidé "
                     "avant lui sur le même sujet, et fait foi sur tout épisode ou message plus ancien (puis le dépôt, "
                     "git log) :")
CONSTRAINTS_K = 8  # exigences de tâche gardées en couche 3 (les plus récentes)
CONSTRAINT_HEADER = ("Exigences posées pendant les tâches, du plus ancien au plus récent : elles restent en vigueur tant "
                     "qu'un revirement plus récent ne les annule pas. Le code qui les porte peut sembler inutilisé dans "
                     "le dépôt (il sert des appelants extérieurs) : avant de supprimer, renommer ou nettoyer, vérifie "
                     "que tu ne les casses pas :")


def conclusion_of(turn: Turn) -> str:
    """Dernier texte du modèle dans le tour (sa conclusion), sinon la réponse rendue (qu'une porte a pu remplacer)."""
    for m in reversed(turn.messages):
        if m.role == "assistant" and (m.content or "").strip():
            return m.content
    return turn.reply or ""


def revocation_sentences(text: str) -> str:
    """Les seules phrases de revirement d'un message (« Reviens au point-virgule. »), le message entier à défaut."""
    picked = [x for x in sentences(text) if is_revocation(x)]
    return " ".join(picked) if picked else " ".join((text or "").split())


def shed_key(turn_index: int, call_id: str) -> str:
    return f"{turn_index}:{call_id}"


def day_of(ts: float) -> str:
    return datetime.fromtimestamp(ts, ZURICH).strftime("%Y-%m-%d")


def stub_text(item_id: str, name: str, tokens: int, preview: str) -> str:
    return (f"[résultat de {name or 'outil'} ({tokens} tokens) déplacé hors fenêtre — extrait : "
            f"« {clip(preview, STUB_PREVIEW_CHARS)} » — recall_context(\"{item_id}\") pour le relire]")


@dataclass
class ContextManager:
    store: Any  # core.ports.Store (history / history_from)
    episodes: EpisodeStore = field(default_factory=MemoryEpisodeStore)
    reservoir: ReservoirStore = field(default_factory=MemoryReservoirStore)
    summarizer: Summarizer = field(default_factory=ExtractiveSummarizer)
    budgets: ContextBudgets = field(default_factory=ContextBudgets)
    scrub: Callable[[str], str] | None = None  # invariants.scrub_output : rien ne rentre non caviardé
    clock: Callable[[], float] = time.time
    # Appelé à la clôture d'un épisode ; renvoie l'id de la bulle `episode` créée (ou None).
    on_episode_closed: Callable[[Episode], str | None] | None = None
    # Recherche sémantique (moteur CortHeXis, `corthexis_index.py`) ; None = lexical seulement.
    semantic: Any = None

    def __post_init__(self) -> None:
        self._locks: dict[str, threading.RLock] = {}
        self._guard = threading.Lock()
        self._tidx: dict[str, dict[str, Any]] = {}
        self._rel_cache: dict[tuple[str, str], list] = {}  # une recherche par (fil, requête) : assemble() repasse à chaque appel

    # ───────────────────────────── utilitaires ─────────────────────────────

    def _lock(self, thread_id: str) -> threading.RLock:
        with self._guard:
            return self._locks.setdefault(thread_id, threading.RLock())

    def _clean(self, text: str) -> str:
        return self.scrub(text) if self.scrub is not None else text

    def _history_from(self, thread_id: str, first: int) -> list[Turn]:
        fn = getattr(self.store, "history_from", None)
        if callable(fn):
            return fn(thread_id, first)
        return [t for t in self.store.history(thread_id) if t.index >= first]

    def _turn_tokens(self, turn: Turn, st: ViewState | None = None) -> int:
        """Tokens estimés d'un tour tel que le modèle le VOIT (les résultats délestés comptent pour leur stub, l'en-tête
        figé pour sa longueur tant qu'il n'est pas délesté)."""
        if st is None:
            st = self.state(turn.thread_id)
        chars = len(turn.input.text or "")
        if turn.head and turn.index >= st.heads_shed_before:
            chars += len(turn.head)  # contient déjà le contexte reçu
        else:
            chars += sum(len(b) for b in turn.injected)
        for m in turn.messages:
            stub = st.shed.get(shed_key(turn.index, m.tool_call_id or "")) if m.role == "tool" else None
            chars += len(stub) if stub is not None else message_chars(m)
        return max(0, chars // 4)

    def replay_head(self, turn: Turn, st: ViewState) -> bool:
        """Vrai si l'historique rejoue l'en-tête figé de ce tour (non délesté)."""
        return bool(turn.head) and turn.index >= st.heads_shed_before

    # ───────────────────────────── état de vue ─────────────────────────────

    def state(self, thread_id: str) -> ViewState:
        with self._lock(thread_id):
            st = self.episodes.get_state(thread_id)
            if st is None:
                st = self._bootstrap(thread_id)
            return st

    def _bootstrap(self, thread_id: str) -> ViewState:
        """Premier contact avec un fil : s'il a déjà un historique (canari, fils d'avant ce chantier), on le découpe en
        épisodes par le seuil de tokens (les tours n'ont pas d'horodatage) ; le dernier segment reste l'épisode ouvert.
        Aucune bulle n'est créée pour ces épisodes hérités (pas de rafale d'un fil de 300 tours)."""
        now = self.clock()
        history = self.store.history(thread_id)
        st = ViewState(thread_id=thread_id, last_turn_at=now, updated_at=now)
        if not history:
            self.episodes.save_state(st)
            return st
        st.last_turn_index = history[-1].index
        seq = self.episodes.next_seq(thread_id)
        start, acc, seg = history[0].index, 0, []
        for i, t in enumerate(history):
            seg.append(t)
            acc += turn_tokens(t)
            last = i == len(history) - 1
            if acc >= self.budgets.current_budget and not last:
                ep = Episode(id=new_episode_id(), thread_id=thread_id, seq=seq, first_turn=start,
                             last_turn=t.index, opened_at=now)
                self._finish_episode(ep, seg, "watermark", now)
                self.episodes.save(ep)
                self._index_semantic(ep, seg)
                seq += 1
                start, acc, seg = t.index + 1, 0, []
        opened = Episode(id=new_episode_id(), thread_id=thread_id, seq=seq, first_turn=start,
                         last_turn=history[-1].index, raw_tokens=acc, opened_at=now)
        self.episodes.save(opened)
        st.episode_id, st.base_turn = opened.id, start
        self.episodes.save_state(st)
        return st

    # ───────────────────────────── frontières ─────────────────────────────

    def before_turn(self, thread_id: str, text: str) -> str | None:
        """À l'entrée d'un tour, AVANT la lecture de l'historique : crée l'état de vue si besoin et applique les
        frontières décidables sur l'entrée. Renvoie la raison si un épisode a été clos."""
        with self._lock(thread_id):
            st = self.state(thread_id)
            ep = self.episodes.current(thread_id)
            if ep is None or ep.last_turn is None:
                return None
            b = self.budgets
            if (text or "").strip().lower() == MARKER_NEW:
                return "marker" if self._close(thread_id, "marker") else None
            if st.last_turn_at and self.clock() - st.last_turn_at >= b.silence_gap_s:
                return "silence" if self._close(thread_id, "silence") else None
            if b.topic_shift_enabled and self._topic_shifted(thread_id, st, ep, text):
                return "topic" if self._close(thread_id, "topic") else None
            return None

    def _topic_shifted(self, thread_id: str, st: ViewState, ep: Episode, text: str) -> bool:
        new = set(content_words(text, 4))
        if len(new) < 3 or ep.last_turn is None or ep.last_turn - ep.first_turn + 1 < self.budgets.topic_shift_min_turns:
            return False
        recent = " ".join(f"{t.input.text} {t.reply}" for t in self._history_from(thread_id, st.base_turn))
        window = set(content_words(recent, 4))
        if not window:
            return False
        sim = len(new & window) / len(new | window)
        return sim < self.budgets.topic_shift_threshold

    def new_episode(self, thread_id: str, reason: str = "manual") -> str | None:
        """Clôt l'épisode courant (s'il a des tours) — `/new`, API, consolidation. Rien n'est perdu."""
        with self._lock(thread_id):
            self.state(thread_id)
            return self._close(thread_id, reason)

    def _finish_episode(self, ep: Episode, turns: list[Turn], reason: str, now: float) -> None:
        try:
            res = self.summarizer.summarize(turns)
        except Exception:
            logger.exception("résumeur en échec — repli extractif")
            res = ExtractiveSummarizer().summarize(turns)
        ep.title = self._clean(res.title)[:120]
        ep.summary = self._clean(res.summary)
        ep.keywords = [self._clean(k) for k in res.keywords][:16]
        ep.raw_tokens = sum(turn_tokens(t) for t in turns)
        ep.summary_tokens = estimate_tokens(ep.summary)
        ep.closed_at, ep.closed_reason, ep.day = now, reason, day_of(now)
        ep.last_turn = turns[-1].index if turns else ep.last_turn
        ep.turn_ids_sha256 = hashlib.sha256(
            "|".join(f"{ep.thread_id}:{t.index}" for t in turns).encode()).hexdigest()
        ep.refined = res.model is not None

    def _close(self, thread_id: str, reason: str) -> str | None:
        ep = self.episodes.current(thread_id)
        st = self.state(thread_id)
        if ep is None or ep.last_turn is None:
            return None
        now = self.clock()
        turns = [t for t in self._history_from(thread_id, ep.first_turn) if t.index <= ep.last_turn]
        self._finish_episode(ep, turns, reason, now)
        self.episodes.save(ep)
        if self.on_episode_closed is not None:
            try:
                bubble_id = self.on_episode_closed(ep)
            except Exception:
                logger.exception("bulle d'épisode non créée (%s)", ep.id)
                bubble_id = None
            if bubble_id:
                ep.bubble_id = bubble_id
                self.episodes.save(ep)
        self._index_semantic(ep, turns)
        st.episode_id = None
        st.base_turn = max(ep.first_turn, ep.last_turn - self.budgets.carry_turns + 1)
        st.shed = {k: v for k, v in st.shed.items() if int(k.split(":", 1)[0]) >= st.base_turn}
        st.tokens_since_compaction = 0
        st.compaction_version += 1
        st.updated_at = now
        self.episodes.save_state(st)
        return ep.id

    def _semantic_body(self, ep: Episode, turns: list[Turn]) -> str:
        """Corps de la note CortHeXis d'un épisode : résumé + TOUS les messages de l'utilisateur (bornés), UN PAR
        PARAGRAPHE — l'indexeur découpe sur les paragraphes : un message par morceau, pour qu'un fait du milieu d'un
        épisode de bavardage ne soit pas noyé dans l'embedding d'un morceau de remplissage (mesuré, §11)."""
        lines = [f"[T{t.index}] {clip(t.input.text, 320)}"
                 + (f" → conclusion : {clip(conclusion_of(t), 240)}" if is_revocation(t.input.text) else "")
                 for t in turns if (t.input.text or "").strip()]
        text = "\n\n".join(lines)
        return self._clean(f"{ep.summary}\n\n## Messages de l'utilisateur\n\n{text[:12000]}")

    def _index_semantic(self, ep: Episode, turns: list[Turn]) -> None:
        if self.semantic is None:
            return
        try:
            self.semantic.index_episode(ep, self._semantic_body(ep, turns))
        except Exception:
            logger.warning("indexation sémantique de %s en échec", ep.id, exc_info=True)

    def reindex_episode(self, episode_id: str) -> bool:
        """Ré-indexe un épisode clos (résumé raffiné, changement de moteur)."""
        ep = self.episodes.get(episode_id)
        if ep is None or ep.closed_at is None or self.semantic is None:
            return False
        last = ep.last_turn if ep.last_turn is not None else ep.first_turn
        turns = [t for t in self._history_from(ep.thread_id, ep.first_turn) if t.index <= last]
        self._index_semantic(ep, turns)
        return True

    def reindex_thread(self, thread_id: str) -> int:
        n = 0
        for ep in self.episodes.list(thread_id, closed_only=True):
            n += 1 if self.reindex_episode(ep.id) else 0
        return n

    def _semantic_hits(self, thread_id: str, query: str, k: int) -> list:
        if self.semantic is None:
            return []
        key = (thread_id, query)
        if key not in self._rel_cache:
            if len(self._rel_cache) > 64:
                self._rel_cache.clear()
            try:
                self._rel_cache[key] = self.semantic.search(thread_id, query, k)
            except Exception:
                logger.warning("recherche sémantique en échec", exc_info=True)
                self._rel_cache[key] = []
        return self._rel_cache[key]

    # ───────────────────────────── fin de tour : comptage, délestage, seuil ─────────────────────────────

    def after_turn(self, turn: Turn) -> None:
        thread_id = turn.thread_id
        with self._lock(thread_id):
            fresh = self.episodes.get_state(thread_id) is None
            st = self.state(thread_id)  # amorce (le tour est déjà au journal : l'amorçage l'a compté)
            if not fresh:
                now = self.clock()
                ep = self.episodes.current(thread_id)
                tt = self._turn_tokens(turn, st)
                if ep is None:
                    ep = Episode(id=new_episode_id(), thread_id=thread_id, seq=self.episodes.next_seq(thread_id),
                                 first_turn=turn.index, opened_at=now)
                ep.last_turn = turn.index
                ep.raw_tokens += tt
                self.episodes.save(ep)
                st.episode_id, st.last_turn_index, st.last_turn_at = ep.id, turn.index, now
                st.tokens_since_compaction += tt
                st.updated_at = now
                self.episodes.save_state(st)
            self._enforce(thread_id)

    def window_tokens(self, thread_id: str) -> int:
        st = self.state(thread_id)
        return sum(self._turn_tokens(t, st) for t in self._history_from(thread_id, st.base_turn))

    def _enforce(self, thread_id: str) -> None:
        b = self.budgets
        st = self.state(thread_id)
        wt = self.window_tokens(thread_id)
        if wt >= b.soft_budget and (st.tokens_since_compaction >= b.compaction_step or wt >= b.current_budget):
            self.compact(thread_id)
            wt = self.window_tokens(thread_id)
        if wt >= b.current_budget:
            self._close(thread_id, "watermark")

    def compact(self, thread_id: str) -> int:
        """Délestage : les gros résultats d'outils ANCIENS (hors des `keep_last_turns_raw` derniers tours) vont au
        réservoir et sont remplacés par un stub. Événement discret, persisté : la vue reste append-only entre deux.
        Renvoie le nombre de résultats délestés."""
        with self._lock(thread_id):
            st = self.state(thread_id)
            b = self.budgets
            turns = self._history_from(thread_id, st.base_turn)
            candidates = turns[: max(0, len(turns) - b.keep_last_turns_raw)]
            n = 0
            for t in candidates:
                for m in t.messages:
                    if m.role != "tool" or not m.tool_call_id:
                        continue
                    key = shed_key(t.index, m.tool_call_id)
                    tokens = estimate_tokens(m.content)
                    if key in st.shed or tokens < b.shed_min_tokens:
                        continue
                    item = self.reservoir.find(thread_id, t.index, m.tool_call_id)
                    if item is None:
                        item = ReservoirItem(id=new_reservoir_id(), thread_id=thread_id, kind="tool_result",
                                             name=m.name or "", content=self._clean(m.content),
                                             preview=clip(self._clean(m.content), 500), tokens=tokens,
                                             turn_index=t.index, call_id=m.tool_call_id, created_at=self.clock())
                        self.reservoir.put(item)
                    st.shed[key] = stub_text(item.id, item.name, item.tokens or tokens, item.preview)
                    n += 1
            if candidates:  # les en-têtes des mêmes tours anciens partent au même événement
                st.heads_shed_before = max(st.heads_shed_before, candidates[-1].index + 1)
            st.compaction_version += 1
            st.tokens_since_compaction = 0
            st.updated_at = self.clock()
            self.episodes.save_state(st)
            return n

    # ───────────────────────────── ce que le modèle voit ─────────────────────────────

    def history_window(self, thread_id: str) -> list[Turn]:
        """Les tours bruts de la fenêtre (épisode courant + tours de transition) — jamais tout le journal."""
        with self._lock(thread_id):
            st = self.state(thread_id)
            return self._history_from(thread_id, st.base_turn)

    def shed_stub(self, turn_index: int, call_id: str | None, st: ViewState) -> str | None:
        return st.shed.get(shed_key(turn_index, call_id or "")) if call_id else None

    def admit_result_in(self, thread_id: str, turn_index: int, result: ToolResult) -> ToolResult:
        """Admission : un résultat de plus de `reservoir_min_tokens` est rangé EN ENTIER au réservoir ; le contexte ne
        garde qu'un extrait et l'identifiant (le résultat est déjà caviardé par l'appelant)."""
        tokens = estimate_tokens(result.content)
        if tokens < self.budgets.reservoir_min_tokens:
            return result
        item = ReservoirItem(id=new_reservoir_id(), thread_id=thread_id, kind="tool_result", name=result.name,
                             content=self._clean(result.content), preview=clip(self._clean(result.content), 500),
                             tokens=tokens, turn_index=turn_index, call_id=result.call_id or None,
                             created_at=self.clock())
        self.reservoir.put(item)
        excerpt = result.content[: self.budgets.reservoir_excerpt_chars]
        content = (f"{excerpt}\n[… {tokens} tokens au total, extrait ci-dessus — résultat complet : "
                   f"recall_context(\"{item.id}\")]")
        return ToolResult(call_id=result.call_id, name=result.name, content=content, is_error=result.is_error,
                          reservoir_id=item.id, secret_handles=result.secret_handles)

    # ───────────────────────────── mémoire de session (couche 3) et épisodes pertinents (bloc 5) ──────────────

    @staticmethod
    def _fmt(ep: Episode, summary_chars: int) -> str:
        n = (ep.last_turn - ep.first_turn + 1) if ep.last_turn is not None else 0
        return (f"- {ep.id} (#{ep.seq + 1}, {ep.day or '?'}, {n} tours) « {ep.title} » — "
                f"{clip(ep.summary, summary_chars)}")

    def memory_block(self, thread_id: str) -> str:
        """Couche 3 : résumés des derniers épisodes clos + épisodes épinglés + index des plus anciens. Ne change qu'à
        la clôture d'un épisode ou à un (dés)épinglage : le préfixe de cache reste valide entre-temps."""
        b = self.budgets
        closed = self.episodes.list(thread_id, closed_only=True)
        if not closed:
            return ""
        st = self.state(thread_id)
        pinned = {e for e in st.pinned_episodes}
        recent = closed[-b.recent_episodes_k:] if b.recent_episodes_k > 0 else []
        chosen = [e for e in closed if e.id in pinned or e in recent or e.id in {r.id for r in recent}]
        older = [e for e in closed if e.id not in {c.id for c in chosen}]
        # Hiérarchie : les jours entièrement composés d'épisodes « plus anciens » passent par leur résumé de jour
        # (le plus récent d'abord, 3 au plus) ; le reste est signalé par une ligne d'index.
        older_ids = {e.id for e in older}
        day_lines: list[str] = []
        covered: set[str] = set()
        for d in sorted(self.episodes.list_days(thread_id), key=lambda d: d.day, reverse=True):
            if len(day_lines) >= 3:
                break
            if d.episode_ids and all(i in older_ids for i in d.episode_ids):
                day_lines.append(f"- {d.id} ({d.day}, {len(d.episode_ids)} épisodes) {clip(d.summary, 260)}")
                covered.update(d.episode_ids)
        day_lines.reverse()
        rest = [e for e in older if e.id not in covered]
        budget_chars = b.episodes_budget * 4
        header = ("[Mémoire de la session]\n(La fenêtre ne montre que l'épisode courant : pour toute information absente, "
                  "appelle recall_context(query) AVANT de répondre que tu ne sais pas.)")
        index_line = self._index_line(rest, thread_id) if rest else ""
        cons = self._constraint_lines(thread_id, int(budget_chars * 0.25))
        revs = self._revocation_lines(thread_id, int(budget_chars * 0.4))
        fixed = (len(header) + len(index_line) + sum(len(x) for x in day_lines)
                 + sum(len(x) + 1 for x in (*cons, *revs)) + 40)
        room = max(0, budget_chars - fixed)
        cap = max(100, room // max(1, len(chosen)) - 90) if chosen else 0
        lines = [self._fmt(e, cap) for e in chosen]
        # Exigences puis revirements juste après l'en-tête (la coupe de sûreté ci-dessous ne peut pas les emporter),
        # dans cet ordre : un revirement plus récent l'emporte sur une exigence.
        text = "\n".join([header, *cons, *revs, *day_lines, *lines, *([index_line] if index_line else []),
                          "[/Mémoire]"])
        return text if len(text) <= budget_chars + 200 else text[: budget_chars + 150] + "\n[/Mémoire]"

    def revocations(self, thread_id: str) -> list[tuple[Turn, Episode]]:
        """Les tours de revirement (`text.is_revocation`) des épisodes clos, du plus ancien au plus récent."""
        self._turn_docs(thread_id)
        return list(self._tidx.get(thread_id, {}).get("revs", []))

    def _revocation_lines(self, thread_id: str, budget_chars: int) -> list[str]:
        """Couche 3 : les revirements EN VIGUEUR, avec la conclusion du modèle (c'est elle qui nomme ce qui a été annulé
        quand l'utilisateur parle par référence : « annule ce changement-là »). Toujours présents tant que le fil
        dure : une annulation ne partage pas le vocabulaire de ce qu'elle annule, la recherche ne la remonte pas
        (banc X9, §5.1). Les plus récents d'abord gardés, rendus dans l'ordre chronologique."""
        out: list[str] = []
        used = len(REVOCATION_HEADER) + 1
        for t, ep in reversed(self.revocations(thread_id)[-REVOCATIONS_K:]):
            concl = clip(self._clean(conclusion_of(t)), 160)
            line = (f"- T{t.index} ({ep.id}) « {clip(self._clean(t.input.text), 200)} »"
                    + (f" → conclusion : « {concl} »" if concl else ""))
            if used + len(line) + 1 > budget_chars:
                break
            out.append(line)
            used += len(line) + 1
        return [REVOCATION_HEADER, *reversed(out)] if out else []

    def constraints(self, thread_id: str) -> list[tuple[Turn, Episode]]:
        """Les tours qui posent une exigence de préservation (`text.is_constraint`, hors revirements) dans les
        épisodes clos, du plus ancien au plus récent."""
        self._turn_docs(thread_id)
        return list(self._tidx.get(thread_id, {}).get("cons", []))

    def _constraint_lines(self, thread_id: str, budget_chars: int) -> list[str]:
        """Couche 3 : les exigences posées au fil des tâches (banc X9, RESULTS.md §10.3 : « réexporte total_ht … pour
        ne rien casser chez les appelants », perdue 35 tours plus tard par un « nettoyage : imports inutilisés » qui ne
        partage aucun mot avec elle). Même raison que les revirements : la recherche ne remonte pas une exigence pour
        une demande qui ne la nomme pas. Seules les propositions qui la portent ; les plus récentes d'abord gardées,
        rendues dans l'ordre chronologique."""
        out: list[str] = []
        used = len(CONSTRAINT_HEADER) + 1
        for t, ep in reversed(self.constraints(thread_id)[-CONSTRAINTS_K:]):
            line = f"- T{t.index} ({ep.id}) « {clip(self._clean(constraint_clauses(t.input.text)), 220)} »"
            if used + len(line) + 1 > budget_chars:
                break
            out.append(line)
            used += len(line) + 1
        return [CONSTRAINT_HEADER, *reversed(out)] if out else []

    def _index_line(self, older: list[Episode], thread_id: str) -> str:
        counts: Counter[str] = Counter()
        for e in older:
            counts.update(k.lower() for k in e.keywords)
        top = ", ".join(w for w, _ in counts.most_common(10))
        days = [e.day for e in older if e.day]
        span = f" du {min(days)} au {max(days)}" if days else ""
        return (f"- {len(older)} épisode(s) plus ancien(s){span} — sujets : {top or 'n/a'} — "
                "recall_context(query) pour les retrouver, recall_context(id) pour les relire")

    def relevant_block(self, thread_id: str, query: str, exclude: set[str] | None = None) -> str:
        """Bloc du tour : les épisodes clos les plus pertinents au message courant (lexical), hors ceux déjà en
        couche 3. Vide si aucun ne passe le seuil de score."""
        b = self.budgets
        if b.relevant_episodes_k <= 0:
            return ""
        closed = self.episodes.list(thread_id, closed_only=True)
        hits = episode_index.search(closed, query, k=b.relevant_episodes_k, min_score=b.relevant_min_score,
                                    exclude=exclude)
        sem = {h.episode_id: h for h in self._semantic_hits(thread_id, query, b.relevant_episodes_k)
               if not (exclude and h.episode_id in exclude)}
        if sem:  # fusion : un épisode retrouvé par le sens entre même si le lexical l'a manqué (« backups » ↔ « sauvegarde »)
            by_id = {e.id: e for e in closed}
            merged = {e.id: (e, sc) for e, sc in hits}
            for eid, h in sem.items():
                if eid in by_id:
                    prev = merged.get(eid, (by_id[eid], 0.0))[1]
                    merged[eid] = (by_id[eid], max(prev, h.score))
            hits = sorted(merged.values(), key=lambda p: (-p[1], -p[0].seq))[:b.relevant_episodes_k]
        found = self._scored_turns(thread_id, query, k=3, min_score=0.5)
        if not hits and not found:
            return ""
        # Ordre CHRONOLOGIQUE (et plus par score) : entre deux extraits qui se contredisent, le modèle doit voir lequel
        # est le plus récent (banc X9, §5.1 : la virgule du T73 passait après le point-virgule du T42, sans son
        # annulation du T76).
        hits = sorted(hits, key=lambda p: p[0].seq)
        found = sorted(found, key=lambda f: f[1].index)
        lines = []
        for e, score in hits:
            n = (e.last_turn - e.first_turn + 1) if e.last_turn is not None else 0
            lex = " | ".join(clip(ln, 220) for ln, _ in episode_index.best_lines(e, query, 2))
            # L'extrait que le MOTEUR a jugé le plus proche passe d'abord (il porte le sens), les lignes lexicales après.
            sem_text = clip(sem[e.id].text, 280) if e.id in sem and sem[e.id].text else ""
            best = " | ".join(x for x in (sem_text, lex) if x)
            span = f"T{e.first_turn}-T{e.last_turn}" if e.last_turn is not None else f"T{e.first_turn}"
            lines.append(f"- {e.id} (#{e.seq + 1}, {span}, {e.day or '?'}, {n} tours) « {clip(e.title, 60)} » — "
                         f"{best or clip(e.summary, 300)} (pertinence {score:.2f})")
        revs = self.revocations(thread_id)
        rev_ids = {t.index for t, _ in revs}
        if found:
            lines.append("Messages retrouvés :")
            lines.extend(self._fmt_turn_hit(sc, t, ep) + (" — REVIREMENT : remplace ce qui précède sur ce sujet"
                                                          if t.index in rev_ids else "") for sc, t, ep in found)
        oldest = min([e.first_turn for e, _ in hits] + [t.index for _, t, _ in found])
        shown = {t.index for _, t, _ in found}
        later = [(t, ep) for t, ep in revs if t.index > oldest and t.index not in shown][-4:]
        if later:  # le bloc renvoie lui-même aux revirements plus récents que ses extraits (détail en couche 3)
            lines.append("Plus récent que ces extraits — revirement(s) qui REMPLACE(NT) ce qui précède sur le même "
                         "sujet et fait foi : " + " ; ".join(
                             f"T{t.index} « {clip(self._clean(revocation_sentences(t.input.text)), 160)} »"
                             for t, _ in later))
        return "[Épisodes pertinents]\n" + "\n".join(lines) + "\n[/Épisodes]"

    def layer3_ids(self, thread_id: str) -> set[str]:
        b = self.budgets
        closed = self.episodes.list(thread_id, closed_only=True)
        st = self.state(thread_id)
        recent = closed[-b.recent_episodes_k:] if b.recent_episodes_k > 0 else []
        return {e.id for e in recent} | set(st.pinned_episodes)

    # ───────────────────────────── épinglage, export, relecture ─────────────────────────────

    def pin(self, thread_id: str, episode_id: str) -> bool:
        with self._lock(thread_id):
            ep = self.episodes.get(episode_id)
            if ep is None or ep.thread_id != thread_id or ep.closed_at is None:
                return False
            st = self.state(thread_id)
            if episode_id not in st.pinned_episodes:
                st.pinned_episodes.append(episode_id)
                st.compaction_version += 1
                self.episodes.save_state(st)
            return True

    def unpin(self, thread_id: str, episode_id: str) -> bool:
        with self._lock(thread_id):
            st = self.state(thread_id)
            if episode_id in st.pinned_episodes:
                st.pinned_episodes.remove(episode_id)
                st.compaction_version += 1
                self.episodes.save_state(st)
                return True
            return False

    def transcript(self, episode: Episode, max_chars: int) -> str:
        last = episode.last_turn if episode.last_turn is not None else episode.first_turn
        turns = [t for t in self._history_from(episode.thread_id, episode.first_turn) if t.index <= last]
        return self._clean(render_episode(turns, max_chars))

    def export_episode(self, episode_id: str, to_thread: str) -> ReservoirItem | None:
        """Copie la transcription (caviardée) d'un épisode dans le RÉSERVOIR d'un autre fil (transfert d'une bulle
        `episode`). Aucune lecture inter-fils : le fil d'arrivée n'a que sa copie."""
        ep = self.episodes.get(episode_id)
        if ep is None or ep.closed_at is None:
            return None
        text = self.transcript(ep, self.budgets.export_max_chars)
        item = ReservoirItem(id=new_reservoir_id(), thread_id=to_thread, kind="episode_export", name=ep.title,
                             content=text, preview=clip(ep.summary, 500), tokens=estimate_tokens(text),
                             created_at=self.clock(), origin={"thread_id": ep.thread_id, "episode_id": ep.id})
        self.reservoir.put(item)
        return item

    def recall(self, thread_id: str, *, ref: str | None = None, query: str | None = None, part: str = "auto",
               max_chars: int = 3000, offset: int = 0) -> str:
        """Relecture à la demande (`recall_context`) — LIMITÉE au fil appelant."""
        max_chars = max(200, min(int(max_chars or 3000), 8000))
        offset = max(0, int(offset or 0))
        if ref:
            ref = ref.strip()
            if ref.startswith("ep_"):
                ep = self.episodes.get(ref)
                if ep is None or ep.thread_id != thread_id:
                    return f"Épisode inconnu dans ce fil : {ref}"
                if part == "raw":
                    return self._page(self.transcript(ep, 10 ** 6), offset, max_chars, f"épisode {ref}")
                head = self._fmt(ep, 4000)
                return f"{head}\n(transcription complète : recall_context(\"{ref}\", part=\"raw\"))"
            if ref.startswith("rs_"):
                item = self.reservoir.get(ref)
                if item is None or item.thread_id != thread_id:
                    return f"Élément inconnu dans ce fil : {ref}"
                return self._page(item.content, offset, max_chars, f"{item.kind} {ref}")
            if ref.startswith("dy_"):
                for d in self.episodes.list_days(thread_id):
                    if d.id == ref:
                        return f"- {d.id} ({d.day}, {len(d.episode_ids)} épisodes) {d.summary}"
                return f"Résumé de jour inconnu dans ce fil : {ref}"
            return f"Identifiant non reconnu : {ref} (attendu ep_…, rs_… ou dy_…)"
        if query:
            closed = self.episodes.list(thread_id, closed_only=True)
            hits = episode_index.search(closed, query, k=5, min_score=0.05)
            lines = [f"{self._fmt(e, 200)} (pertinence {s:.2f})" for e, s in hits]
            for h in self._semantic_hits(thread_id, query, 5):
                ep = self.episodes.get(h.episode_id)
                if ep is not None and ep.thread_id == thread_id and not any(ep.id in ln for ln in lines):
                    lines.append(f"{self._fmt(ep, 200)} (sémantique {h.score:.2f})")
            lines += self.search_turns(thread_id, query, k=5)
            q = set(content_words(query))
            for it in self.reservoir.list(thread_id, limit=200):
                hay = set(content_words(f"{it.name} {it.preview}"))
                if q and len(q & hay) / len(q) >= 0.5:
                    lines.append(f"- {it.id} ({it.kind}, {it.tokens} tokens) {it.name} — {clip(it.preview, 160)}")
            return "\n".join(lines[:8]) if lines else "Rien de pertinent trouvé dans la mémoire de ce fil."
        return "recall_context : donner `ref` (ep_…, rs_…, dy_…) ou `query`."

    def _turn_docs(self, thread_id: str) -> list[tuple[Turn, set[str], Episode]]:
        """Index INCRÉMENTAL (en mémoire du processus) des messages utilisateur des épisodes clos : un tour n'est lu
        qu'une fois ; l'épisode courant est exclu (déjà dans la fenêtre). Reconstruit à froid après un redémarrage."""
        with self._lock(thread_id):
            st = self.state(thread_id)
            idx = self._tidx.setdefault(thread_id, {"upto": -1, "docs": [], "revs": [], "cons": []})
            closed = [e for e in self.episodes.list(thread_id, closed_only=True)]
            if not closed or st.last_turn_index < 0:
                return []
            spans = [(e.first_turn, e.last_turn if e.last_turn is not None else e.first_turn, e) for e in closed]
            top = max(hi for _, hi, _ in spans)
            if top > idx["upto"]:
                start = max(idx["upto"] + 1, st.last_turn_index - 20000, 0)
                for t in self._history_from(thread_id, start):
                    ep = next((e for lo, hi, e in spans if lo <= t.index <= hi), None)
                    if ep is None:
                        continue
                    text = t.input.text or ""
                    if is_revocation(text):
                        # Un revirement s'exprime par référence (« reviens au point-virgule », « annule ce
                        # changement-là ») : la conclusion du modèle, qui nomme ce qui a été annulé, porte le
                        # vocabulaire qui manque au message pour qu'une question sur le sujet le retrouve.
                        text = f"{text}\n{self._clean(conclusion_of(t))}"
                        idx["revs"].append((t, ep))
                    elif is_constraint(text):  # le « … doit rester » d'un revirement est déjà dans sa section
                        idx["cons"].append((t, ep))
                    toks = set(content_words(text)) | set(distinctive_words(text))
                    idx["docs"].append((t, toks, ep))
                idx["upto"] = top
            return idx["docs"]

    def search_turns(self, thread_id: str, query: str, k: int = 5, min_score: float = 0.34) -> list[str]:
        """Recherche au niveau des MESSAGES de l'utilisateur dans les épisodes clos : filet de sécurité quand le résumé
        n'a pas gardé un fait (idf calculé sur les messages du fil)."""
        return [self._fmt_turn_hit(sc, t, ep) for sc, t, ep in self._scored_turns(thread_id, query, k, min_score)]

    @staticmethod
    def _fmt_turn_hit(score: float, t: Turn, ep: Episode) -> str:
        return f"- T{t.index} ({ep.id}, {ep.day or '?'}) « {clip(t.input.text, 220)} » (pertinence {score:.2f})"

    def _scored_turns(self, thread_id: str, query: str, k: int, min_score: float) -> list[tuple[float, Turn, Episode]]:
        terms = episode_index.query_terms(query)
        docs = self._turn_docs(thread_id) if terms else []
        if not docs:
            return []
        n = len(docs)
        df = Counter(x for _, toks, _ in docs for x in toks if x in terms)
        terms = [x for x in terms if df.get(x, 0) > 0]  # un terme absent du fil ne départage rien
        if not terms:
            return []
        idf = {x: math.log(1 + (n + 1) / (1 + df[x])) for x in terms}
        total = sum(idf.values()) or 1.0
        scored = []
        rare = max(2, int(0.02 * n))  # un terme seul n'est un indice que s'il est RARE dans les messages du fil
        for t, toks, ep in docs:
            hit = [x for x in terms if x in toks]
            mass = sum(idf[x] for x in hit)
            if not hit or (len(hit) == 1 and df[hit[0]] > rare):
                continue
            if mass >= episode_index.MIN_MASS and mass / total >= min_score:
                scored.append((mass / total, t, ep))
        return sorted(scored, key=lambda s: (-s[0], -s[1].index))[:k]

    @staticmethod
    def _page(text: str, offset: int, max_chars: int, label: str) -> str:
        chunk = text[offset: offset + max_chars]
        end = offset + len(chunk)
        tail = "" if end >= len(text) else f"\n[… suite : offset={end} sur {len(text)} car.]"
        return f"[{label}, caractères {offset}-{end} sur {len(text)}]\n{chunk}{tail}"


__all__ = ["MARKER_NEW", "ContextManager", "conclusion_of", "day_of", "revocation_sentences", "shed_key", "stub_text"]
