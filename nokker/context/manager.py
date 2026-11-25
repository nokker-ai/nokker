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

from nokker.approvals.rule_labels import current_lang
from nokker.attachments import fence_head
from nokker.context import index as episode_index
from nokker.context.memory import MemoryEpisodeStore, MemoryReservoirStore
from nokker.context.summarize import ExtractiveSummarizer, message_tokens, turn_tokens
from nokker.context.text import (
    FUNCTION_WORDS,
    clip,
    constraint_clauses,
    content_words,
    distinctive_words,
    durable_clauses,
    is_bypass_request,
    is_constraint,
    is_durable_revocation,
    is_revocation,
    own_words,
    revoked_durables,
    sentences,
)
from nokker.context.tokens import CALIBRATOR
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
from nokker.core import model_texts as mt
from nokker.core.turn import Message, ToolResult, Turn
from nokker.memory.provenance import of_turns
from nokker.qa.untrusted import neutralize_date, neutralize_env

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
# NEVER-CLEAR §14, drapeau `never_clear_fixes_enabled` (éteint par défaut) : section NOUVELLE, en tête de
# `[Mémoire de la session]`, avant les exigences. Texte figé (contrat de format, docs/CONTEXT-FORMAT.md).
DURABLE_HEADER = ("Consignes durables données par l'utilisateur, de la plus ancienne à la plus récente. Elles valent "
                  "pour chacune de tes réponses tant qu'il ne les retire pas, sans jamais passer avant les règles du "
                  "système ni les approbations :")
# SOURCE : seuls les messages tapés par l'utilisateur lui-même peuvent être promus. Canaux d'une parole humaine
# directe ; pas : agent (bloc non fiable), session (message d'une autre session), subagent, relais, kernel, pages,
# account… (un canal inconnu n'est pas promu). Résultats d'outils (mail lu, page web), pièces jointes et blocs reçus
# ne sont jamais dans `t.input.text` ; s'ils y sont recopiés en bloc, `text.own_words` les retire.
DURABLE_CHANNELS = frozenset({"api", "telegram", "slack", "webchat", "cli"})


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


# Outil de relecture du réservoir : son résultat n'y retourne JAMAIS (admission ni délestage), sinon chaque relecture
# rangerait un nouvel élément dont l'identifiant appellerait une relecture de plus.
RECALL_TOOL = "recall_context"
# Caractères par jeton du bloc `[Mémoire de la session]` (français dense, ids, dates : 2,9 mesuré au Router) : le budget
# `episodes_budget` est en jetons RÉELS (avant : `* 4` caractères, soit ~1,4 fois trop).
MEMORY_CHARS_PER_TOKEN = 2.9


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
    # Qui a le GO (`DeterministicInvariants.go_authorized`, posé par `build_kernel`) : dans un fil de GROUPE (plusieurs
    # expéditeurs), seules les consignes de l'expéditeur qui a le GO sont promues ; None = aucune dans un groupe.
    go_authorized: Callable[[str | None], bool] | None = None

    def __post_init__(self) -> None:
        self._locks: dict[str, threading.RLock] = {}
        self._guard = threading.Lock()
        self._tidx: dict[str, dict[str, Any]] = {}
        for attr, val in (("key_items", self.budgets.summary_key_items),
                          ("key_items_chars", min(600, max(0, int(self.budgets.summary_key_chars))))):
            if hasattr(self.summarizer, attr):  # le résumeur extractif par défaut (et ceux qui le prolongent)
                setattr(self.summarizer, attr, val)
        self._admitted: dict[tuple[str, int], int] = {}  # (fil, tour) -> jetons de résultats moyens gardés en clair
        self._mem_snap: dict[str, tuple[tuple, str]] = {}  # fil -> (clé d'événement, bloc `[Mémoire de la session]` figé)
        self._rel_cache: dict[tuple[str, str], list] = {}  # une recherche par (fil, requête) : assemble() repasse à chaque appel

    # ───────────────────────────── utilitaires ─────────────────────────────

    def _is_rev(self, text: str) -> bool:
        """`text.is_revocation` avec le réglage `revision_markers` (révisions de valeur incluses ou non)."""
        return is_revocation(text, self.budgets.revision_markers)

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

    def _turn_tokens(self, turn: Turn, st: ViewState | None = None, shed: dict[str, str] | None = None) -> int:
        """Tokens estimés d'un tour tel que le modèle le VOIT (les résultats délestés comptent pour leur stub, l'en-tête
        figé pour sa longueur tant qu'il n'est pas délesté). `shed` : stubs supplémentaires à supposer (le report de
        clôture mesure la forme réduite avant de la décider)."""
        if st is None:
            st = self.state(turn.thread_id)
        n = estimate_tokens(turn.input.text or "")
        if turn.head and turn.index >= st.heads_shed_before:
            n += estimate_tokens(fence_head(turn.head))  # contient déjà le contexte reçu ; porté comme au rejeu (r46)
        else:
            n += sum(estimate_tokens(b) for b in turn.injected)
        for m in turn.messages:
            k = shed_key(turn.index, m.tool_call_id or "")
            stub = ((shed or {}).get(k) or st.shed.get(k)) if m.role == "tool" else None
            n += estimate_tokens(stub) if stub is not None else message_tokens(m)
        return n

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
        if self.budgets.bootstrap_compact:
            # Historique importé / fil d'avant ce chantier : l'épisode courant garde ses résultats d'outils EN CLAIR
            # (`compact` ne tourne qu'en fin de tour). Même règle de délestage, UNE FOIS, avant le tout premier appel :
            # événement discret et persisté comme les autres (aucun cache à casser : rien n'a encore été envoyé).
            self._shed(thread_id, st)
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
            res = ExtractiveSummarizer(key_items=self.budgets.summary_key_items,
                                       key_items_chars=min(600, max(0, int(self.budgets.summary_key_chars)))).summarize(turns)
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
        # Provenance du résumé (et des faits qu'on en tirera) : tiers dès qu'un tour porte un contenu de tiers.
        ep.provenance = of_turns(turns)

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
        st.base_turn = self._carry_from(thread_id, st, ep, turns)
        st.shed = {k: v for k, v in st.shed.items() if int(k.split(":", 1)[0]) >= st.base_turn}
        st.tokens_since_compaction = 0
        st.compaction_version += 1
        st.updated_at = now
        self.episodes.save_state(st)
        return ep.id

    def _reduced_stubs(self, t: Turn, st: ViewState, item_id: str | None = None) -> dict[str, tuple[Message, str]]:
        """Résultats d'outils d'un tour qu'un délestage remplacerait par un stub (≥ `shed_min_tokens`, pas déjà délestés,
        pas `recall_context`) : clé de délestage → (message, stub). Sans `item_id`, le stub est mesuré avec un
        identifiant factice de même longueur (rien n'est rangé au réservoir)."""
        out: dict[str, tuple[Message, str]] = {}
        for m in t.messages:
            if m.role != "tool" or not m.tool_call_id or m.name == RECALL_TOOL:
                continue
            key = shed_key(t.index, m.tool_call_id)
            tokens = estimate_tokens(m.content)
            if key in st.shed or tokens < self.budgets.shed_min_tokens:
                continue
            found = self.reservoir.find(t.thread_id, t.index, m.tool_call_id)
            rid = found.id if found is not None else (item_id or "rs_000000000000")
            preview = found.preview if found is not None else clip(self._clean(m.content), 500)
            out[key] = (m, stub_text(rid, m.name or "", (found.tokens if found is not None else 0) or tokens, preview))
        return out

    def _carry_from(self, thread_id: str, st: ViewState, ep: Episode, turns: list[Turn]) -> int:
        """Premier tour de la fenêtre qui suit une clôture : les `carry_turns` derniers tours, SOUS UN PLAFOND EN JETONS
        (`carry_max_ratio × current_budget`, bloc `[Mémoire de la session]` compris : report + bloc ≤ plafond). Sans plafond, des tours lourds reportés remplissent presque tout le budget et
        la clôture revient tous les 2 tours (mesuré sur un vrai Claude). Au-delà du plafond, les tours reportés passent à
        leur forme réduite (résultats d'outils en stub + extrait + `rs_…`, sans texte nouveau : le même stub qu'un
        délestage) puis, si ça dépasse encore, les plus anciens ne sont pas reportés. La fenêtre est réécrite par la
        clôture de toute façon : ce raccourcissement ne coûte pas un octet de cache de plus. `carry_max_ratio` ≥ 1 = ancien."""
        b = self.budgets
        first = max(ep.first_turn, ep.last_turn - b.carry_turns + 1)
        cand = [t for t in turns if t.index >= first]
        if b.carry_max_ratio >= 1.0 or not cand:
            return first
        # Le bloc `[Mémoire de la session]` (message de tête, écrit au même coup que le report) compte dans le plafond : on
        # réduit le REPORT, jamais le bloc. L'épisode est déjà clos et enregistré : le bloc est celui qui va être figé.
        mem = estimate_tokens(self.memory_block(thread_id)) if b.memory_as_message else 0
        cap = max(0, int(b.carry_max_ratio * b.current_budget) - mem)
        if sum(self._turn_tokens(t, st) for t in cand) <= cap:
            return first
        used, kept = 0, []
        for t in reversed(cand):  # du plus récent au plus ancien : la suite doit rester contiguë
            reduced = self._reduced_stubs(t, st)
            n = self._turn_tokens(t, st, {k: v[1] for k, v in reduced.items()})
            if used + n > cap:
                break
            used += n
            kept.append(t)
        for t in kept:
            for key, (m, _) in self._reduced_stubs(t, st).items():
                item = self.reservoir.find(thread_id, t.index, m.tool_call_id or "")
                if item is None:
                    item = ReservoirItem(id=new_reservoir_id(), thread_id=thread_id, kind="tool_result", name=m.name or "",
                                         content=self._clean(m.content), preview=clip(self._clean(m.content), 500),
                                         tokens=estimate_tokens(m.content), turn_index=t.index,
                                         call_id=m.tool_call_id, created_at=self.clock())
                    self.reservoir.put(item)
                st.shed[key] = stub_text(item.id, item.name, item.tokens or estimate_tokens(m.content), item.preview)
        return kept[-1].index if kept else ep.last_turn + 1

    def _semantic_body(self, ep: Episode, turns: list[Turn]) -> str:
        """Corps de la note CortHeXis d'un épisode : résumé + TOUS les messages de l'utilisateur (bornés), UN PAR
        PARAGRAPHE — l'indexeur découpe sur les paragraphes : un message par morceau, pour qu'un fait du milieu d'un
        épisode de bavardage ne soit pas noyé dans l'embedding d'un morceau de remplissage (mesuré, §11)."""
        lines = [f"[T{t.index}] {clip(t.input.text, 320)}"
                 + (f" → conclusion : {clip(conclusion_of(t), 240)}" if self._is_rev(t.input.text) else "")
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
        n = sum(self._turn_tokens(t, st) for t in self._history_from(thread_id, st.base_turn))
        if self.budgets.calibrate_tokens:  # ratio réel/estimé du palier (`tokens.CALIBRATOR`), 1,0 sans observation
            n = round(n * CALIBRATOR.factor())
        return n

    def _enforce(self, thread_id: str) -> None:
        b = self.budgets
        st = self.state(thread_id)
        wt = self.window_tokens(thread_id)
        if b.retroactive_shed and wt >= b.soft_budget and (st.tokens_since_compaction >= b.compaction_step
                                                            or wt >= b.current_budget):
            self.compact(thread_id)
            wt = self.window_tokens(thread_id)
        if wt >= b.current_budget:
            self._close(thread_id, "watermark")

    def compact(self, thread_id: str) -> int:
        """Délestage : les gros résultats d'outils ANCIENS (hors des `keep_last_turns_raw` derniers tours) vont au
        réservoir et sont remplacés par un stub. Événement discret, persisté : la vue reste append-only entre deux.
        Renvoie le nombre de résultats délestés."""
        with self._lock(thread_id):
            return self._shed(thread_id, self.state(thread_id))

    def _shed(self, thread_id: str, st: ViewState) -> int:
        """Corps de `compact`, sur un état DÉJÀ obtenu (le bootstrap n'a pas encore le sien dans le magasin)."""
        b = self.budgets
        turns = self._history_from(thread_id, st.base_turn)
        candidates = turns[: max(0, len(turns) - b.keep_last_turns_raw)]
        n = 0
        for t in candidates:
            for m in t.messages:
                if m.role != "tool" or not m.tool_call_id or m.name == RECALL_TOOL:
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
        if candidates and b.shed_heads:  # les en-têtes des mêmes tours anciens partent au même événement
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
        if result.name == RECALL_TOOL:  # déjà borné par `max_chars` : en clair dans la fenêtre, jamais réadmis
            return result
        b = self.budgets
        tokens = estimate_tokens(result.content)
        if tokens < b.reservoir_min_tokens:
            # Résultat « moyen » : sa forme FINALE est décidée maintenant (jamais réécrite ensuite, `retroactive_shed`
            # faux) selon un budget par tour ; au-delà, stub + extrait comme un gros résultat. Un extrait qui ne
            # raccourcit presque rien (< 30 %) n'a pas de sens : le résultat reste en clair.
            if b.retroactive_shed or tokens < b.shed_min_tokens:
                return result
            if len(self._admitted) > 256:
                self._admitted.clear()
            key = (thread_id, turn_index)
            used = self._admitted.get(key, 0)
            if used + tokens <= b.admit_turn_budget:
                self._admitted[key] = used + tokens
                return result
            if len(result.content) * 0.7 <= b.reservoir_excerpt_chars:  # l'extrait ne raccourcirait presque rien
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

    def _fmt_found(self, ep: Episode, body: str) -> str:
        """`_fmt` dont le corps est un extrait retrouvé (résumé coupé à 200 caractères à défaut)."""
        if not body:
            return self._fmt(ep, 200)
        n = (ep.last_turn - ep.first_turn + 1) if ep.last_turn is not None else 0
        return f"- {ep.id} (#{ep.seq + 1}, {ep.day or '?'}, {n} tours) « {ep.title} » — {body}"

    def memory_block(self, thread_id: str, recall_available: bool = True) -> str:
        """Couche 3 : résumés des derniers épisodes clos + épisodes épinglés + index des plus anciens. Ne change qu'à
        la clôture d'un épisode ou à un (dés)épinglage : le préfixe de cache reste valide entre-temps.

        `recall_available` : `recall_context` est-il dans le catalogue du fil ? (l'appelant le sait : `deny_tools`, source
        d'outils absente). Faux : la balise `[Mémoire de la session]` et le contenu restent, SANS la ligne d'invitation
        (elle nommerait un outil absent)."""
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
        budget_chars = int(b.episodes_budget * MEMORY_CHARS_PER_TOKEN)
        if not recall_available:
            header = "[Mémoire de la session]"
        elif b.direct_recall_hint:  # invitation directe, dans la langue du tour (`model_texts.SESSION_MEMORY_HINT`)
            header = "[Mémoire de la session]\n" + mt.pick(mt.SESSION_MEMORY_HINT, current_lang())
        else:
            header = ("[Mémoire de la session]\n(La fenêtre ne montre que l'épisode courant : pour toute information absente, "
                      "appelle recall_context(query) AVANT de répondre que tu ne sais pas.)")
        index_line = self._index_line(rest, thread_id) if rest else ""
        durs = self._durable_lines(thread_id, int(budget_chars * 0.15))  # [] drapeau éteint
        cons = self._constraint_lines(thread_id, int(budget_chars * 0.25))
        revs = self._revocation_lines(thread_id, int(budget_chars * 0.4))
        fixed = (len(header) + len(index_line) + sum(len(x) for x in day_lines)
                 + sum(len(x) + 1 for x in (*durs, *cons, *revs)) + 40)
        room = max(0, budget_chars - fixed)
        cap = max(100, room // max(1, len(chosen)) - 90) if chosen else 0
        lines = [self._fmt(e, cap) for e in chosen]
        # Exigences puis revirements juste après l'en-tête (la coupe de sûreté ci-dessous ne peut pas les emporter),
        # dans cet ordre : un revirement plus récent l'emporte sur une exigence.
        text = neutralize_date(neutralize_env("\n".join([header, *durs, *cons, *revs, *day_lines, *lines,
                                                      *([index_line] if index_line else []), "[/Mémoire]"])))
        return text if len(text) <= budget_chars + 200 else text[: budget_chars + 150] + "\n[/Mémoire]"

    def frozen_memory_block(self, thread_id: str, recall_available: bool = True) -> str:
        """`memory_block` FIGÉ entre deux événements de la mémoire de session (clôture d'un épisode, épinglage) : le texte
        envoyé au modèle ne change pas quand un résumé est raffiné, qu'un résumé de jour est (re)calculé ou qu'un index
        bouge, il n'est donc jamais réécrit dans la requête (cache). En mémoire du processus : après un redémarrage il
        est recalculé une fois (les résumés raffinés entrent alors).

        `recall_available` n'entre PAS dans la clé du figement : sa valeur est prise au figement et le bloc ne change pas
        si la disponibilité varie ensuite (cache de préfixe intact ; `deny_tools` est posé à l'ouverture de la session,
        en pratique constant sur la vie du fil)."""
        with self._lock(thread_id):
            st = self.state(thread_id)
            key = (tuple(e.id for e in self.episodes.list(thread_id, closed_only=True)), tuple(st.pinned_episodes))
            hit = self._mem_snap.get(thread_id)
            if hit is not None and hit[0] == key:
                return hit[1]
            text = self.memory_block(thread_id, recall_available)
            self._mem_snap[thread_id] = (key, text)
            return text

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

    def open_revisions(self, thread_id: str) -> list[tuple[Turn, Episode]]:
        """Les tours de revirement ou de RÉVISION DE VALEUR (« on passe de 1e-3 à 2e-4 ») de l'épisode OUVERT, du plus ancien
        au plus récent : lus dans la fenêtre à chaque appel (jamais dans le bloc figé de couche 3). Ils disent que ce qu'un
        épisode clos affirme a changé depuis. Vide si `revision_markers` est faux."""
        if not self.budgets.revision_markers:
            return []
        ep = self.episodes.current(thread_id)
        if ep is None or ep.last_turn is None:
            return []
        return [(t, ep) for t in self._history_from(thread_id, ep.first_turn)
                if t.index <= ep.last_turn and self._is_rev(t.input.text or "")]

    def constraints(self, thread_id: str) -> list[tuple[Turn, Episode]]:
        """Les tours qui posent une exigence de préservation (`text.is_constraint`, hors revirements) dans les
        épisodes clos, du plus ancien au plus récent."""
        self._turn_docs(thread_id)
        return list(self._tidx.get(thread_id, {}).get("cons", []))

    def durable_instructions(self, thread_id: str) -> list[tuple[Turn, Episode, str]]:
        """Les consignes DURABLES (`text.durable_clauses`) des épisodes clos encore en vigueur — celles qu'aucune
        révocation plus récente (`text.is_durable_revocation` : « arrête de… », « oublie cette règle », « plus besoin
        de… ») n'a retirées — avec leurs seules phrases de consigne, du plus ancien au plus récent ; les
        `durable_instructions_k` plus récentes. Lues dans CE fil seulement (un fil = un compte) : ni un autre compte ni
        un membre du foyer ne les voient."""
        self._turn_docs(thread_id)
        idx = self._tidx.get(thread_id, {})
        # Fil de groupe (plusieurs expéditeurs) : seul celui qui a le GO compte (jugé au rendu : un fil devient un
        # groupe quand un 2e expéditeur parle, les consignes déjà indexées sont alors refiltrées).
        if len(idx.get("senders", ())) > 1:
            go = self.go_authorized
            def ok(t):
                return go is not None and bool(go(t.sender))
        else:
            def ok(t):
                return True
        events = sorted([(t.index, "d", t, ep, txt) for t, ep, txt in idx.get("durs", []) if ok(t)]
                        + [(t.index, "r", t, ep, "") for t, ep in idx.get("drevs", []) if ok(t)],
                        key=lambda x: x[0])
        active: list[tuple[Turn, Episode, str]] = []
        for _, kind, t, ep, txt in events:
            if kind == "d":
                active.append((t, ep, txt))
            elif active:
                gone = revoked_durables(t.input.text or "", [x[2] for x in active])
                active = [x for i, x in enumerate(active) if i not in gone]
        k = max(0, int(self.budgets.durable_instructions_k))
        return active[-k:] if k and self.budgets.never_clear_fixes_enabled else []

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

    def _durable_lines(self, thread_id: str, budget_chars: int) -> list[str]:
        """Couche 3, drapeau `never_clear_fixes_enabled` : les consignes DURABLES données à l'assistante encore en
        vigueur (« termine chacune de tes réponses par … », NEVER-CLEAR §14), une ligne par tour, la seule phrase qui
        porte la consigne, réextraite du message CAVIARDÉ (un secret à cheval sur deux propositions ne ressort pas par
        la découpe) puis recaviardée. Les plus récentes d'abord gardées, rendues dans l'ordre chronologique."""
        if not self.budgets.never_clear_fixes_enabled:
            return []
        out: list[str] = []
        used = len(DURABLE_HEADER) + 1
        for t, ep, txt in reversed(self.durable_instructions(thread_id)):
            body = durable_clauses(self._clean(t.input.text or "")) or txt
            line = f"- T{t.index} ({ep.id}) « {clip(self._clean(body), 220)} »"
            if used + len(line) + 1 > budget_chars:
                break
            out.append(line)
            used += len(line) + 1
        return [DURABLE_HEADER, *reversed(out)] if out else []

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
                                    exclude=exclude, extra_stop=self._qstop)
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
            best = self._hit_body(e, query, sem.get(e.id))
            span = f"T{e.first_turn}-T{e.last_turn}" if e.last_turn is not None else f"T{e.first_turn}"
            lines.append(f"- {e.id} (#{e.seq + 1}, {span}, {e.day or '?'}, {n} tours) « {clip(e.title, 60)} » — "
                         f"{best or clip(e.summary, 300)} (pertinence {score:.2f})")
        revs = self.revocations(thread_id) + self.open_revisions(thread_id)  # + celles de l'épisode ouvert (dans la fenêtre)
        rev_ids = {t.index for t, _ in revs}
        if found:
            lines.append("Messages retrouvés :")
            lines.extend(self._fmt_turn_hit(sc, t, ep) + (f" — {mt.pick(mt.RECALL_REVISION, current_lang())}"
                                                          if t.index in rev_ids else "") for sc, t, ep in found)
        oldest = min([e.first_turn for e, _ in hits] + [t.index for _, t, _ in found])
        shown = {t.index for _, t, _ in found}
        later = [(t, ep) for t, ep in revs if t.index > oldest and t.index not in shown][-4:]
        if later:  # le bloc renvoie lui-même aux revirements plus récents que ses extraits (détail en couche 3)
            lines.append("Plus récent que ces extraits — revirement(s) qui REMPLACE(NT) ce qui précède sur le même "
                         "sujet et fait foi : " + " ; ".join(
                             f"T{t.index} « {clip(self._clean(revocation_sentences(t.input.text)), 160)} »"
                             for t, _ in later))
        return neutralize_date(neutralize_env("[Épisodes pertinents]\n" + "\n".join(lines) + "\n[/Épisodes]"))

    def _hit_body(self, e: Episode, query: str, hit: Any = None) -> str:
        """Corps d'un épisode retrouvé, commun au bloc `[Épisodes pertinents]` et à `recall_context(query)` : l'extrait
        que le MOTEUR sémantique a jugé le plus proche d'abord (il porte le sens : une paraphrase sans mot commun ne
        recoupe ni le titre ni le résumé), puis les lignes du résumé qui recoupent la requête ; « » si ni l'un ni
        l'autre. Drapeau `never_clear_fixes_enabled` allumé : recaviardé (l'index est déjà caviardé à l'écriture :
        défense en profondeur) et mots outils hors des termes de requête ; éteint : exactement le corps d'avant."""
        on = self.budgets.never_clear_fixes_enabled
        lex = " | ".join(clip(ln, 220) for ln, _ in episode_index.best_lines(e, query, 2, extra_stop=self._qstop))
        if hit is None or not getattr(hit, "text", ""):
            sem_text = ""
        else:
            sem_text = clip(self._clean(hit.text), 280) if on else clip(hit.text, 280)
        body = " | ".join(x for x in (sem_text, lex) if x)
        return self._clean(body) if on else body

    @property
    def _qstop(self) -> frozenset[str]:
        """Mots retirés EN PLUS des termes d'une requête lexicale : les mots outils (`text.FUNCTION_WORDS`), drapeau
        `never_clear_fixes_enabled` allumé seulement (éteint : sélection d'avant, gel du format)."""
        return FUNCTION_WORDS if self.budgets.never_clear_fixes_enabled else frozenset()

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
            hits = episode_index.search(closed, query, k=5, min_score=0.05, extra_stop=self._qstop)
            if self.budgets.never_clear_fixes_enabled:
                sem: dict[str, Any] = {}
                for h in self._semantic_hits(thread_id, query, 5):
                    sem.setdefault(h.episode_id, h)
                # Titre puis corps, comme avant ; le corps est l'EXTRAIT retrouvé (`_hit_body`, même plafond et même
                # caviardage que `[Épisodes pertinents]`), le résumé coupé à 200 caractères seulement à défaut.
                lines = [f"{self._fmt_found(e, self._hit_body(e, query, sem.get(e.id)))} (pertinence {s:.2f})"
                         for e, s in hits]
                for eid, h in sem.items():
                    ep = self.episodes.get(eid)
                    if ep is not None and ep.thread_id == thread_id and not any(ep.id in ln for ln in lines):
                        lines.append(f"{self._fmt_found(ep, self._hit_body(ep, query, h))} (sémantique {h.score:.2f})")
            else:  # drapeau éteint : titre + résumé coupé, exactement comme avant (gel du format)
                lines = [f"{self._fmt(e, 200)} (pertinence {s:.2f})" for e, s in hits]
                for h in self._semantic_hits(thread_id, query, 5):
                    ep = self.episodes.get(h.episode_id)
                    if ep is not None and ep.thread_id == thread_id and not any(ep.id in ln for ln in lines):
                        lines.append(f"{self._fmt(ep, 200)} (sémantique {h.score:.2f})")
            if not self.budgets.recall_recent_first:  # ancien ordre : épisodes, messages, réservoir (par pertinence)
                lines += self.search_turns(thread_id, query, k=5)
                q = set(content_words(query)) - self._qstop
                for it in self.reservoir.list(thread_id, limit=200):
                    hay = set(content_words(f"{it.name} {it.preview}"))
                    if q and len(q & hay) / len(q) >= 0.5:
                        lines.append(f"- {it.id} ({it.kind}, {it.tokens} tokens) {it.name} — {clip(it.preview, 160)}")
                return "\n".join(lines[:8]) if lines else "Rien de pertinent trouvé dans la mémoire de ce fil."
            return self._recall_recent_first(thread_id, query, lines)
        return "recall_context : donner `ref` (ep_…, rs_…, dy_…) ou `query`."

    def _recall_recent_first(self, thread_id: str, query: str, episode_lines: list[str]) -> str:
        """`recall_context(query)` rendu du plus RÉCENT au plus ancien : quand une valeur a été révisée, la dernière version
        est la première lue. Les 8 meilleures lignes (même sélection que l'ancien ordre : épisodes, messages retrouvés,
        réservoir, puis les révisions de l'épisode ouvert) sont triées par numéro de tour décroissant ; les extraits du
        réservoir portent leur jour."""
        keyed: list[tuple[int, str]] = []
        by_id = {e.id: e for e in self.episodes.list(thread_id, closed_only=True)}
        for ln in episode_lines:
            eid = next((i for i in by_id if i in ln), "")
            ep = by_id.get(eid)
            keyed.append(((ep.last_turn if ep is not None and ep.last_turn is not None else -1), ln))
        for sc, t, ep in self._scored_turns(thread_id, query, 5, 0.34):
            keyed.append((t.index, self._fmt_turn_hit(sc, t, ep)
                          + (f" — {mt.pick(mt.RECALL_REVISION, current_lang())}" if self._is_rev(t.input.text or "") else "")))
        q = set(content_words(query)) - self._qstop
        for it in self.reservoir.list(thread_id, limit=200):
            hay = set(content_words(f"{it.name} {it.preview}"))
            if q and len(q & hay) / len(q) >= 0.5:
                day = day_of(it.created_at) if it.created_at else "?"
                keyed.append((it.turn_index if it.turn_index is not None else -1,
                              f"- {it.id} ({it.kind}, {it.tokens} tokens, {day}) {it.name} — {clip(it.preview, 160)}"))
        terms = set(episode_index.query_terms(query, extra_stop=self._qstop))
        recent: list[tuple[int, str]] = []
        for t, ep in self.open_revisions(thread_id)[::-1]:  # l'épisode ouvert est dans la fenêtre : ses révisions seules comptent
            text = t.input.text or ""
            if terms & (set(content_words(text)) | set(distinctive_words(text))):
                recent.append((t.index, f"- T{t.index} ({ep.id}, en cours) « {clip(text, 220)} » — "
                                        f"{mt.pick(mt.RECALL_REVISION, current_lang())}"))
        recent = recent[:2]  # jamais coupées par le plafond de 8 lignes
        top = keyed[:max(0, 8 - len(recent))] + recent
        if not top:
            return "Rien de pertinent trouvé dans la mémoire de ce fil."
        return "\n".join(ln for _, ln in sorted(top, key=lambda p: -p[0]))

    def _turn_docs(self, thread_id: str) -> list[tuple[Turn, set[str], Episode]]:
        """Index INCRÉMENTAL (en mémoire du processus) des messages utilisateur des épisodes clos : un tour n'est lu
        qu'une fois ; l'épisode courant est exclu (déjà dans la fenêtre). Reconstruit à froid après un redémarrage."""
        with self._lock(thread_id):
            st = self.state(thread_id)
            idx = self._tidx.setdefault(thread_id, {"upto": -1, "docs": [], "revs": [], "cons": [], "durs": [],
                                                    "drevs": []})
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
                    # consigne durable donnée à l'assistante (NEVER-CLEAR §14), drapeau allumé seulement ; gardée
                    # caviardée (la ligne de couche 3 est réextraite du message caviardé au rendu)
                    nc = self.budgets.never_clear_fixes_enabled
                    human = nc and t.channel in DURABLE_CHANNELS and not is_bypass_request(own_words(text))
                    if human and t.sender:
                        idx.setdefault("senders", set()).add(t.sender)
                    durable = durable_clauses(text) if human else ""
                    if durable:
                        idx["durs"].append((t, ep, self._clean(durable)))
                    elif human and is_durable_revocation(text):
                        idx["drevs"].append((t, ep))
                    # « Désormais, … doit rester importable » est une EXIGENCE (puis un marqueur de révision par hasard) :
                    # elle reste une exigence ; seul un revirement classique, ou une révision sans exigence, est un revirement.
                    revision_only = not is_revocation(text, False)
                    if self._is_rev(text) and not (revision_only and is_constraint(text)):
                        # Un revirement s'exprime par référence (« reviens au point-virgule », « annule ce
                        # changement-là ») : la conclusion du modèle, qui nomme ce qui a été annulé, porte le
                        # vocabulaire qui manque au message pour qu'une question sur le sujet le retrouve.
                        text = f"{text}\n{self._clean(conclusion_of(t))}"
                        idx["revs"].append((t, ep))
                    elif is_constraint(text) and not durable:  # le « … doit rester » d'un revirement est déjà
                        idx["cons"].append((t, ep))                   # dans sa section, une consigne durable aussi
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
        terms = episode_index.query_terms(query, extra_stop=self._qstop)
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
