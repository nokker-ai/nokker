"""Types du chantier « never clear » (docs/NEVER-CLEAR.md §7) : épisodes, réservoir, résumés de jour, état de vue,
et les ports de stockage / de résumé. Aucun import d'un autre paquet nokker hors `core.turn` (pas de cycle)."""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Any, Protocol

CLOSED_REASONS = frozenset({"watermark", "silence", "marker", "topic", "manual"})
RESERVOIR_KINDS = frozenset({"tool_result", "episode_export", "document"})


def new_episode_id() -> str:
    return "ep_" + secrets.token_hex(6)


def new_reservoir_id() -> str:
    return "rs_" + secrets.token_hex(6)


def new_day_id() -> str:
    return "dy_" + secrets.token_hex(6)


def estimate_tokens(text: str) -> int:
    """`len // 4` — même estimation que `context/engine.py` (documentée là), pas un vrai tokenizer."""
    return max(0, len(text) // 4)


@dataclass
class Episode:
    """Suite contiguë de tours d'un fil. Ouvert tant que `closed_at is None` (`last_turn` = dernier tour connu)."""

    id: str
    thread_id: str
    seq: int  # numéro d'ordre dans le fil (0, 1, 2…)
    first_turn: int
    last_turn: int | None = None
    title: str = ""
    summary: str = ""
    keywords: list[str] = field(default_factory=list)
    raw_tokens: int = 0  # estimation des tokens bruts des tours de l'épisode
    summary_tokens: int = 0
    opened_at: float = 0.0
    closed_at: float | None = None
    closed_reason: str | None = None
    day: str | None = None  # AAAA-MM-JJ (Europe/Zurich) de la clôture
    turn_ids_sha256: str = ""  # signature des tours résumés (« résumés signés », PLAN §2.4)
    refined: bool = False  # True quand le résumé LLM a remplacé le résumé extractif
    bubble_id: str | None = None
    version: int = 1

    @property
    def is_open(self) -> bool:
        return self.closed_at is None

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "thread_id": self.thread_id, "seq": self.seq, "first_turn": self.first_turn,
                "last_turn": self.last_turn, "title": self.title, "summary": self.summary,
                "keywords": list(self.keywords), "raw_tokens": self.raw_tokens,
                "summary_tokens": self.summary_tokens, "opened_at": self.opened_at, "closed_at": self.closed_at,
                "closed_reason": self.closed_reason, "day": self.day, "turn_ids_sha256": self.turn_ids_sha256,
                "refined": self.refined, "bubble_id": self.bubble_id, "version": self.version}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Episode:
        return cls(id=d["id"], thread_id=d["thread_id"], seq=d["seq"], first_turn=d["first_turn"],
                   last_turn=d.get("last_turn"), title=d.get("title", ""), summary=d.get("summary", ""),
                   keywords=list(d.get("keywords", [])), raw_tokens=d.get("raw_tokens", 0),
                   summary_tokens=d.get("summary_tokens", 0), opened_at=d.get("opened_at", 0.0),
                   closed_at=d.get("closed_at"), closed_reason=d.get("closed_reason"), day=d.get("day"),
                   turn_ids_sha256=d.get("turn_ids_sha256", ""), refined=d.get("refined", False),
                   bubble_id=d.get("bubble_id"), version=d.get("version", 1))


@dataclass
class ReservoirItem:
    """Contenu complet rangé hors fenêtre (résultat d'outil long, export d'épisode…). Propre à UN fil."""

    id: str
    thread_id: str
    kind: str  # tool_result | episode_export | document
    name: str = ""  # nom de l'outil, titre de l'épisode…
    content: str = ""  # déjà caviardé par les invariants — JAMAIS de secret ici
    preview: str = ""  # extrait court (≤ ~500 car.)
    tokens: int = 0
    turn_index: int | None = None
    call_id: str | None = None
    created_at: float = 0.0
    origin: dict[str, Any] | None = None  # ex. {"thread_id", "episode_id"} pour un export

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "thread_id": self.thread_id, "kind": self.kind, "name": self.name,
                "content": self.content, "preview": self.preview, "tokens": self.tokens,
                "turn_index": self.turn_index, "call_id": self.call_id, "created_at": self.created_at,
                "origin": self.origin}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ReservoirItem:
        return cls(id=d["id"], thread_id=d["thread_id"], kind=d["kind"], name=d.get("name", ""),
                   content=d.get("content", ""), preview=d.get("preview", ""), tokens=d.get("tokens", 0),
                   turn_index=d.get("turn_index"), call_id=d.get("call_id"), created_at=d.get("created_at", 0.0),
                   origin=d.get("origin"))


@dataclass
class DaySummary:
    """Résumé d'une journée d'épisodes clos (hiérarchie tour → épisode → jour)."""

    id: str
    thread_id: str
    day: str  # AAAA-MM-JJ
    summary: str = ""
    episode_ids: list[str] = field(default_factory=list)
    tokens: int = 0
    created_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "thread_id": self.thread_id, "day": self.day, "summary": self.summary,
                "episode_ids": list(self.episode_ids), "tokens": self.tokens, "created_at": self.created_at}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> DaySummary:
        return cls(id=d["id"], thread_id=d["thread_id"], day=d["day"], summary=d.get("summary", ""),
                   episode_ids=list(d.get("episode_ids", [])), tokens=d.get("tokens", 0),
                   created_at=d.get("created_at", 0.0))


@dataclass
class ViewState:
    """État persisté de la vue d'un fil : ce qui est dans la fenêtre. Stable entre deux événements (cache amont)."""

    thread_id: str
    episode_id: str | None = None  # épisode courant (ouvert)
    base_turn: int = 0  # premier tour dont le contenu brut est dans la fenêtre
    last_turn_at: float = 0.0  # horodatage de fin du dernier tour (silence)
    last_turn_index: int = -1
    tokens_since_compaction: int = 0
    compaction_version: int = 0
    # Résultats d'outils délestés de la fenêtre : clé « <tour>:<call_id> » -> TEXTE du stub rendu à la place du
    # résultat (il contient l'id de réservoir `rs_…`) ; stocké tel quel pour ne jamais relire le réservoir au rendu.
    shed: dict[str, str] = field(default_factory=dict)
    pinned_episodes: list[str] = field(default_factory=list)
    updated_at: float = 0.0
    # En-têtes de tour (`Turn.head` : date, rappel, épisodes pertinents) délestés pour les tours d'index inférieur :
    # rejoués tels qu'envoyés tant qu'ils sont récents (cache de préfixe entre tours), retirés au délestage seulement
    # (événement discret, comme les stubs), jamais à chaque tour.
    heads_shed_before: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"thread_id": self.thread_id, "episode_id": self.episode_id, "base_turn": self.base_turn,
                "last_turn_at": self.last_turn_at, "last_turn_index": self.last_turn_index,
                "tokens_since_compaction": self.tokens_since_compaction,
                "compaction_version": self.compaction_version, "shed": dict(self.shed),
                "pinned_episodes": list(self.pinned_episodes), "updated_at": self.updated_at,
                "heads_shed_before": self.heads_shed_before}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ViewState:
        return cls(thread_id=d["thread_id"], episode_id=d.get("episode_id"), base_turn=d.get("base_turn", 0),
                   last_turn_at=d.get("last_turn_at", 0.0), last_turn_index=d.get("last_turn_index", -1),
                   tokens_since_compaction=d.get("tokens_since_compaction", 0),
                   compaction_version=d.get("compaction_version", 0), shed=dict(d.get("shed", {})),
                   pinned_episodes=list(d.get("pinned_episodes", [])), updated_at=d.get("updated_at", 0.0),
                   heads_shed_before=d.get("heads_shed_before", 0))


@dataclass
class ContextBudgets:
    """Budgets et seuils (tokens estimés `len // 4`). Les valeurs sont celles de PLAN §2.4 ajustées pour des fenêtres
    ≥ 128k : le plafond coûte du cache, pas de la place ; tout est configurable (`NokkerConfig.context`)."""

    episodes_budget: int = 1500  # couche 3 : résumés des épisodes clos
    current_budget: int = 12000  # couche 4 : épisode courant — seuil DUR (clôture)
    soft_ratio: float = 0.70  # seuil souple (délestage) = soft_ratio × current_budget
    compaction_step: int = 3000  # tokens bruts à ajouter avant un nouveau délestage
    keep_last_turns_raw: int = 3  # jamais délester les N derniers tours
    shed_min_tokens: int = 300  # on ne délègue au réservoir que les résultats ≥ N tokens
    reservoir_min_tokens: int = 1000  # à l'admission : au-dessus, résultat rangé + extrait seulement
    reservoir_excerpt_chars: int = 1200
    carry_turns: int = 2  # tours de transition conservés bruts après une clôture
    recent_episodes_k: int = 2  # derniers épisodes clos toujours en couche 3
    relevant_episodes_k: int = 3  # épisodes pertinents ajoutés au bloc du tour
    relevant_min_score: float = 0.15  # seuil de pertinence lexicale (normalisé 0..1)
    silence_gap_s: float = 6 * 3600.0
    topic_shift_enabled: bool = False
    topic_shift_threshold: float = 0.08  # similarité lexicale en dessous de laquelle le sujet a changé
    topic_shift_min_turns: int = 4
    export_max_chars: int = 20000  # transcription d'un épisode exportée vers un autre fil

    @property
    def soft_budget(self) -> int:
        return int(self.current_budget * self.soft_ratio)


class EpisodeStore(Protocol):
    """Stockage des épisodes et des résumés de jour. Un backend par processus ; verrou interne."""

    def current(self, thread_id: str) -> Episode | None: ...

    def get(self, episode_id: str) -> Episode | None: ...

    def list(self, thread_id: str, *, closed_only: bool = False) -> list[Episode]: ...  # ordre `seq` croissant

    def save(self, episode: Episode) -> None: ...  # insert ou update (version++ à l'update)

    def next_seq(self, thread_id: str) -> int: ...

    def get_day(self, thread_id: str, day: str) -> DaySummary | None: ...

    def list_days(self, thread_id: str) -> list[DaySummary]: ...

    def save_day(self, day: DaySummary) -> None: ...

    def threads_with_open_episode(self) -> list[str]: ...

    def get_state(self, thread_id: str) -> ViewState | None: ...

    def save_state(self, state: ViewState) -> None: ...


class ReservoirStore(Protocol):
    def put(self, item: ReservoirItem) -> None: ...

    def get(self, item_id: str) -> ReservoirItem | None: ...

    def list(self, thread_id: str, *, kind: str | None = None, limit: int = 200) -> list[ReservoirItem]: ...

    def find(self, thread_id: str, turn_index: int, call_id: str) -> ReservoirItem | None: ...


@dataclass
class SummaryResult:
    title: str
    summary: str
    keywords: list[str] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)  # faits durables candidats → quarantaine (MemoryPort.propose)
    model: str | None = None


class Summarizer(Protocol):
    """Résume un épisode. `turns` : tours bruts (`core.turn.Turn`) ; `previous` : résumé existant (raffinement)."""

    def summarize(self, turns: list[Any], previous: str | None = None) -> SummaryResult: ...


__all__ = [
    "CLOSED_REASONS",
    "RESERVOIR_KINDS",
    "ContextBudgets",
    "DaySummary",
    "Episode",
    "EpisodeStore",
    "ReservoirItem",
    "ReservoirStore",
    "Summarizer",
    "SummaryResult",
    "ViewState",
    "estimate_tokens",
    "new_day_id",
    "new_episode_id",
    "new_reservoir_id",
]
