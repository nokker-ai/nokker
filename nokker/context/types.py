"""Types du chantier « never clear » (docs/NEVER-CLEAR.md §7) : épisodes, réservoir, résumés de jour, état de vue,
et les ports de stockage / de résumé. Aucun import d'un autre paquet nokker hors `core.turn` (pas de cycle)."""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Any, Protocol

from nokker.context.tokens import estimate_tokens

CLOSED_REASONS = frozenset({"watermark", "silence", "marker", "topic", "manual"})
RESERVOIR_KINDS = frozenset({"tool_result", "episode_export", "document"})


def new_episode_id() -> str:
    return "ep_" + secrets.token_hex(6)


def new_reservoir_id() -> str:
    return "rs_" + secrets.token_hex(6)


def new_day_id() -> str:
    return "dy_" + secrets.token_hex(6)


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
    # Provenance du résumé (nokker/memory/provenance.py), posée à la clôture d'après les tours ; un épisode d'avant la
    # provenance reste `unknown_legacy` tant que sa trace ne montre pas un contenu de tiers.
    provenance: str = "unknown_legacy"

    @property
    def is_open(self) -> bool:
        return self.closed_at is None

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "thread_id": self.thread_id, "seq": self.seq, "first_turn": self.first_turn,
                "last_turn": self.last_turn, "title": self.title, "summary": self.summary,
                "keywords": list(self.keywords), "raw_tokens": self.raw_tokens,
                "summary_tokens": self.summary_tokens, "opened_at": self.opened_at, "closed_at": self.closed_at,
                "closed_reason": self.closed_reason, "day": self.day, "turn_ids_sha256": self.turn_ids_sha256,
                "refined": self.refined, "bubble_id": self.bubble_id, "version": self.version,
                "provenance": self.provenance}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Episode:
        return cls(id=d["id"], thread_id=d["thread_id"], seq=d["seq"], first_turn=d["first_turn"],
                   last_turn=d.get("last_turn"), title=d.get("title", ""), summary=d.get("summary", ""),
                   keywords=list(d.get("keywords", [])), raw_tokens=d.get("raw_tokens", 0),
                   summary_tokens=d.get("summary_tokens", 0), opened_at=d.get("opened_at", 0.0),
                   closed_at=d.get("closed_at"), closed_reason=d.get("closed_reason"), day=d.get("day"),
                   turn_ids_sha256=d.get("turn_ids_sha256", ""), refined=d.get("refined", False),
                   bubble_id=d.get("bubble_id"), version=d.get("version", 1),
                   provenance=d.get("provenance") or "unknown_legacy")


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
    """Budgets et seuils (jetons estimés par `context/tokens.estimate_tokens`, calibré sur le tokenizer réel). Les valeurs sont celles de PLAN §2.4 ajustées pour des fenêtres
    ≥ 128k : le plafond coûte du cache, pas de la place ; tout est configurable (`NokkerConfig.context`)."""

    episodes_budget: int = 1500  # couche 3 : résumés des épisodes clos
    current_budget: int = 12000  # couche 4 : épisode courant — seuil DUR (clôture)
    soft_ratio: float = 0.70  # seuil souple (délestage) = soft_ratio × current_budget
    compaction_step: int = 8000  # jetons à ajouter avant un nouveau délestage RÉTROACTIF (`retroactive_shed`) ; était 3 000
    keep_last_turns_raw: int = 3  # jamais délester les N derniers tours
    shed_min_tokens: int = 300  # on ne délègue au réservoir que les résultats ≥ N tokens
    reservoir_min_tokens: int = 8000  # à l'admission : au-dessus, résultat rangé + extrait seulement
    reservoir_excerpt_chars: int = 1200
    carry_turns: int = 2  # tours de transition conservés après une clôture
    # Plafond EN JETONS du report : au plus `carry_max_ratio × current_budget` (forme réduite si besoin, moins de tours si
    # elle dépasse encore). 1,0 ou plus = ancien comportement (jamais de plafond). Maximum documenté : 1,0.
    carry_max_ratio: float = 0.3
    recent_episodes_k: int = 2  # derniers épisodes clos toujours en couche 3
    relevant_episodes_k: int = 3  # épisodes pertinents ajoutés au bloc du tour
    relevant_min_score: float = 0.15  # seuil de pertinence lexicale (normalisé 0..1)
    silence_gap_s: float = 6 * 3600.0
    topic_shift_enabled: bool = False
    topic_shift_threshold: float = 0.08  # similarité lexicale en dessous de laquelle le sujet a changé
    topic_shift_min_turns: int = 4
    export_max_chars: int = 20000  # transcription d'un épisode exportée vers un autre fil
    # Correctifs NEVER-CLEAR §14 (bench/keys, défauts 2-4) : (1) consignes durables en couche 3 (section
    # « Consignes durables… », seulement si l'utilisateur en a donné), (2) extrait retrouvé (`_hit_body`) dans
    # `recall_context(query)` et `[Épisodes pertinents]` au lieu du résumé coupé à 200 caractères — c'est ce qui laisse
    # voir la ligne « Éléments clés » et la dernière valeur d'un résumé —, (3) mots outils hors des requêtes lexicales.
    # ALLUMÉ par défaut depuis le lot compact-cache (le gel des textes vus par le modèle est levé, contrat de format
    # docs/CONTEXT-FORMAT.md) ; faux = exactement ce que le modèle voyait avant.
    never_clear_fixes_enabled: bool = True
    # APPEND-ONLY / CACHE (lot compact-cache) : la fenêtre envoyée ne change entre deux événements que par AJOUT. Chaque
    # réécriture d'un message ancien invalide tout le cache aval (réécrit au tarif d'écriture 1 h).
    #  - `shed_heads` : au délestage, retirer l'en-tête de date/rappel (`Turn.head`) des anciens tours. Il réécrit
    #    messages[0] : tout le cache est perdu. Défaut FAUX (en-têtes gardés, figés avec leur tour) ; vrai = ancien.
    #  - `retroactive_shed` : `compact()` raccourcit en cours d'épisode des résultats déjà envoyés (stub). Défaut FAUX :
    #    la forme d'un résultat est décidée à son ARRIVÉE (`admit_result_in`) et n'est plus jamais réécrite ; seule la
    #    clôture d'épisode (et le bootstrap d'un historique importé) retire du contenu. Vrai = ancien comportement
    #    (alors `compaction_step` / `soft_ratio` règlent la rareté des réécritures).
    #  - `admit_turn_budget` : jetons de résultats « moyens » (shed_min_tokens ≤ taille < reservoir_min_tokens) gardés en
    #    CLAIR par tour ; au-delà de ce budget de tour, le résultat entre en forme finale stub + extrait. 0 = tous les
    #    résultats moyens entrent en stub + extrait ; très grand = tous en clair.
    #  - `memory_as_message` : le résumé des épisodes clos (`[Mémoire de la session]`) est un message `user` inséré juste
    #    après le message système et figé entre deux clôtures, au lieu d'être le dernier bloc du système (que chaque
    #    clôture réécrivait : préfixe hôte + invariants relus au tarif d'écriture). Faux = ancien emplacement.
    shed_heads: bool = False
    retroactive_shed: bool = False
    admit_turn_budget: int = 100_000
    memory_as_message: bool = True
    # Fil existant (historique importé, fils d'avant le chantier) : au PREMIER contact, l'épisode courant subit le même
    # délestage que `compact` (résultats d'outils ≥ shed_min_tokens hors des keep_last_turns_raw derniers tours → stub +
    # réservoir), une fois. Éteint : le 1er appel part avec tous les résultats d'outils en clair.
    bootstrap_compact: bool = True
    # Ratio auto-calibré par palier (`tokens.TokenCalibrator`) : multiplie la taille estimée de la fenêtre par le rapport
    # jetons réels (`prompt_tokens` de l'amont) / estimés, borné [1,0 ; 2,5], moyenne mobile en mémoire du processus.
    # ALLUMÉ par défaut (banc décisions du 22.11 : l'estimateur sous-estime encore de ~15 % — `len // 4` de 36 % — sur le 27B ;
    # un délestage décidé sur l'estimation brute part trop tard). Coût assumé : la fenêtre cesse d'être une fonction pure du
    # journal (un redémarrage repart de 1,0 jusqu'à la première observation). Retour arrière : `calibrate_tokens: false`.
    calibrate_tokens: bool = True
    # Lot compact-cache, points 4-5 (tous rétablissables) :
    #  - `summary_key_items` / `summary_key_chars` : ligne « Éléments clés » (noms de fichiers, identifiants, valeurs avec unité,
    #    « (remplace …) » après une révision) dans le résumé extractif d'un épisode, ≤ summary_key_chars (max 600) ;
    #  - `direct_recall_hint` : invitation de `[Mémoire de la session]` plus directe (cherche d'abord avec recall_context) ;
    #  - `revision_markers` : « on passe de X à Y », « au lieu de », « désormais »… comptent comme un revirement (en/de/it
    #    aussi), y compris dans l'épisode OUVERT pour `[Épisodes pertinents]` et `recall_context` ;
    #  - `recall_recent_first` : `recall_context(query)` trie du plus récent au plus ancien et date les extraits du réservoir.
    summary_key_items: bool = True
    summary_key_chars: int = 300
    direct_recall_hint: bool = True
    revision_markers: bool = True
    recall_recent_first: bool = True
    # Plafond de jetons cumulés des RECHERCHES `recall_context(query)` d'un même tour (leurres : au-delà, plus de relance, le noyau
    # répond `context.recall_nothing_found`). 0 = pas de plafond.
    recall_budget_tokens: int = 12_000
    durable_instructions_k: int = 5  # consignes durables gardées en couche 3 (les plus récentes encore en vigueur)

    # Maxima des réglages numériques du lot compact-cache (`from_config` ramène une valeur trop grande au maximum, une valeur
    # illisible au défaut ; les booléens illisibles retombent sur le défaut).
    MAXIMA = {"compaction_step": 1_000_000, "admit_turn_budget": 100_000, "summary_key_chars": 600}
    BOOLS = ("shed_heads", "retroactive_shed", "memory_as_message", "bootstrap_compact", "calibrate_tokens",
             "summary_key_items", "direct_recall_hint", "revision_markers", "recall_recent_first",
             "never_clear_fixes_enabled")

    @classmethod
    def from_config(cls, raw: dict[str, Any] | None) -> ContextBudgets:
        """`context_budgets` de la configuration → budgets : clés inconnues ignorées ; numériques du lot compact-cache bornés
        (`MAXIMA`), booléens stricts (« false » en chaîne ne vaut pas vrai)."""
        known = set(cls.__dataclass_fields__)
        out: dict[str, Any] = {}
        defaults = cls()
        for k, v in (raw or {}).items():
            if k not in known:
                continue
            if k in cls.BOOLS:
                out[k] = v if isinstance(v, bool) else (v.strip().lower() in ("1", "true", "yes", "on")
                                                        if isinstance(v, str) else getattr(defaults, k))
            elif k == "carry_max_ratio":  # 0..1 ; illisible = défaut ; au-delà de 1 = 1 (ancien comportement)
                try:
                    r = float(v)
                except (TypeError, ValueError):
                    r = defaults.carry_max_ratio
                out[k] = 0.0 if r != r else max(0.0, min(r, 1.0))  # NaN → 0 (aucun report)
            elif k in cls.MAXIMA:
                try:
                    n = int(v)
                except (TypeError, ValueError):
                    n = getattr(defaults, k)
                out[k] = max(0 if k != "compaction_step" else 1, min(n, cls.MAXIMA[k]))
            else:
                out[k] = v
        return cls(**out)

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
