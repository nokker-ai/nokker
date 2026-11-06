"""Notes de fonctions (drapeau `feature_notes`, défaut éteint) : une note par fonction du catalogue
(`nokker/guide/catalog.py`, source unique), au format standard, générée à l'identique à chaque démarrage et versionnée
par le commit du noyau. Le modèle en reçoit 0 à 3 dans un bloc à part de l'en-tête du tour (`[Fonctions]`), filtrées
par les drapeaux du compte et le statut mineur, dans un budget de caractères ; `feature_get(id)` relit une note.

Une ligne vide dans le catalogue n'est pas rendue (fonction pas encore décrite : rien n'est inventé). Textes NOUVEAUX,
vus par le modèle : tous dans `ALL_NOTE_TEXTS` (relus avant allumage)."""
from __future__ import annotations

import logging
import re
import threading
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from nokker.guide.catalog import CATALOG, CLOUD, LOKKAL, Feature, flag_on

logger = logging.getLogger("nokker.guide.notes")

# ── Textes vus par le modèle ──
NOTE_HEADER = "[Fonction] {id} · version {sha}"
LINE = "{label} : {value}"
EXAMPLE = "« {example} »"
LABELS = (
    ("Quoi", "purpose_fr"),
    ("Quand", "when_fr"),
    ("Qui fait", "who"),
    ("Comment", "tools_how"),
    ("Jamais", "never"),
    ("La personne voit", "sees_fr"),
    ("Exemple", "example_fr"),
)
BLOCK_OPEN, BLOCK_CLOSE = "[Fonctions]", "[/Fonctions]"
FEATURE_GET = "feature_get"
FEATURE_GET_DESC = ("Relit la note d'une fonction de Nokker (quoi, quand, qui fait, comment, jamais) par son "
                    "identifiant, tel qu'il apparaît dans un bloc [Fonctions]. Lecture seule.")
FEATURE_GET_UNKNOWN = "fonction inconnue ou indisponible pour ce compte : {id}"

ALL_NOTE_TEXTS: tuple[str, ...] = (NOTE_HEADER, LINE, EXAMPLE,
                                   *(label for label, _ in LABELS), BLOCK_OPEN, BLOCK_CLOSE, FEATURE_GET_DESC,
                                   FEATURE_GET_UNKNOWN)

MAX_NOTES = 3
BUDGET_CHARS = 2400  # bloc entier, cadre compris ; une note qui ne tient pas est omise, jamais coupée

_BY_ID = {f.id: f for f in CATALOG}


def render_note(f: Feature, sha: str) -> str:
    """Note standard d'une fonction : en-tête, puis les lignes renseignées dans l'ordre de `LABELS`."""
    lines = [NOTE_HEADER.format(id=f.id, sha=sha or "?")]
    for label, attr in LABELS:
        value: Any = getattr(f, attr)
        if attr == "who":
            value = ", ".join(value)
        elif attr == "example_fr" and value:
            value = EXAMPLE.format(example=value)
        if value:
            lines.append(LINE.format(label=label, value=value))
    return "\n".join(lines)


def generate(sha: str) -> Mapping[str, str]:
    """Toutes les notes, par identifiant : le contenu de la collection en lecture seule (régénérée au démarrage)."""
    return MappingProxyType({f.id: render_note(f, sha) for f in CATALOG})


def allowed(f: Feature, config: Any, *, target: str, minor: bool) -> bool:
    """Même règle que le statut `on`/`setup` du Guide : cible de l'installation, tous les drapeaux allumés, et pas
    une fonction réservée aux adultes pour un profil mineur."""
    if target not in f.targets or not all(flag_on(config, fl) for fl in f.flags):
        return False
    return not (f.adult_only and minor)


def note_for(feature_id: str, config: Any, *, target: str, minor: bool, sha: str) -> str | None:
    """Note d'UNE fonction si elle est permise à ce compte (`feature_get`), sinon None."""
    f = _BY_ID.get(feature_id)
    if f is None or not allowed(f, config, target=target, minor=minor):
        return None
    return render_note(f, sha)


def block(ids: Iterable[str], config: Any, *, target: str, minor: bool, sha: str, max_notes: int = MAX_NOTES,
          budget: int = BUDGET_CHARS) -> str:
    """Bloc `[Fonctions]` des fonctions choisies (dans l'ordre reçu, sans doublon), filtrées ; '' si rien ne passe."""
    notes: list[str] = []
    used = len(BLOCK_OPEN) + len(BLOCK_CLOSE) + 2
    seen: set[str] = set()
    for fid in ids:
        if len(notes) >= max_notes or fid in seen:
            continue
        seen.add(fid)
        note = note_for(fid, config, target=target, minor=minor, sha=sha)
        if note is None or used + len(note) + 1 > budget:
            continue
        notes.append(note)
        used += len(note) + 1
    if not notes:
        return ""
    return "\n".join((BLOCK_OPEN, *notes, BLOCK_CLOSE))


def undescribed() -> list[str]:
    """Fonctions dont la note n'a que les lignes du catalogue d'origine (qui fait / comment / jamais à écrire)."""
    return [f.id for f in CATALOG if not (f.who and f.tools_how)]


# ── Choix des notes pour un message (phase 2) : embeddings du service CortHeXis, sinon score lexical local ──
MIN_COSINE = 0.5  # similarité minimale message ↔ note (embeddings)
MIN_HITS = 2  # mots distincts partagés au minimum (repli lexical)
_WORD = re.compile(r"[a-z0-9_]+")
_STOP = frozenset("""avec dans pour plus mais tout tous toute toutes cette cela elle elles nous vous leur leurs sont
etre fait faire peut quoi quand comme sans sous entre aussi bien encore deja tres
""".split())


def _words(text: str) -> set[str]:
    flat = unicodedata.normalize("NFKD", text.lower()).encode("ascii", "ignore").decode()
    return {w for w in _WORD.findall(flat) if len(w) >= 4 and w not in _STOP}


def _body(f: Feature, sha: str) -> str:
    """Texte d'une note sans son en-tête (indexé pour le choix)."""
    return render_note(f, sha).split("\n", 1)[-1]


def _lexicon(f: Feature) -> set[str]:
    return _words(" ".join((f.name_fr, f.purpose_fr, f.when_fr, f.example_fr, *f.signals)))


@dataclass
class FeatureNotes:
    """Notes en mémoire (`generate`, versionnées par le commit du noyau) et leur choix pour un message : embeddings
    CortHeXis (`embedder` : `embed_docs`, `embed_query`) calculés une fois au démarrage, sinon score lexical local.
    Rien n'est jamais écrit dans CortHeXis."""

    config: Any
    sha: str
    embedder: Any = None
    min_cosine: float = MIN_COSINE
    min_hits: int = MIN_HITS
    max_notes: int = MAX_NOTES
    budget: int = BUDGET_CHARS
    _vecs: dict[str, list[float]] = field(default_factory=dict, init=False, repr=False)
    _lex: dict[str, set[str]] = field(default_factory=dict, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        self.notes = generate(self.sha)
        self._lex = {f.id: _lexicon(f) for f in CATALOG}

    @property
    def target(self) -> str:
        return LOKKAL if getattr(self.config, "local", None) is not None else CLOUD

    def warm(self) -> bool:
        """Embeddings des notes (au démarrage, une fois). Échec ou pas de service : repli lexical, jamais d'erreur."""
        if self.embedder is None:
            return False
        try:
            ids = [f.id for f in CATALOG]
            vecs = self.embedder.embed_docs([_body(f, self.sha) for f in CATALOG])
            if len(vecs) != len(ids):
                raise ValueError(f"{len(vecs)} vecteurs pour {len(ids)} notes")
            with self._lock:
                self._vecs = dict(zip(ids, vecs, strict=True))
            return True
        except Exception:  # noqa: BLE001 — le service d'embed ne casse jamais un démarrage
            logger.warning("notes de fonctions : embeddings indisponibles, repli lexical", exc_info=True)
            return False

    def ranked(self, message: str) -> list[str]:
        """Identifiants des notes pertinentes pour ce message, du plus au moins proche (au-dessus du seuil)."""
        text = (message or "").strip()
        if not text:
            return []
        with self._lock:
            vecs = dict(self._vecs)
        if vecs:
            try:
                from nokker.memory.tenant_long import cosine

                q = self.embedder.embed_query(text)
                scored = [(c, fid) for fid, v in vecs.items() if (c := cosine(q, v)) is not None and c >= self.min_cosine]
                return [fid for _c, fid in sorted(scored, key=lambda x: -x[0])]
            except Exception:  # noqa: BLE001 — repli lexical pour ce tour
                logger.warning("notes de fonctions : embed du message indisponible, repli lexical", exc_info=True)
        words = _words(text)
        hits = [(len(words & lex), fid) for fid, lex in self._lex.items()]
        return [fid for n, fid in sorted(hits, key=lambda x: -x[0]) if n >= self.min_hits]

    def block_for(self, message: str, *, minor: bool) -> str:
        """Bloc `[Fonctions]` du tour, filtré (cible, drapeaux, mineur) et borné ; '' si rien ne passe."""
        return block(self.ranked(message), self.config, target=self.target, minor=minor, sha=self.sha,
                     max_notes=self.max_notes, budget=self.budget)

    def get(self, feature_id: str, *, minor: bool) -> str | None:
        return note_for(feature_id, self.config, target=self.target, minor=minor, sha=self.sha)


@dataclass
class FeatureGetSource:
    """Outil `feature_get(id)` (READ, dans l'index) : relit une note permise à ce compte."""

    notes: FeatureNotes
    minor: Callable[[], bool] = lambda: False
    source_name: str = "builtin:feature_notes"

    def specs(self) -> list[Any]:
        from nokker.core.ports import ToolSpec
        from nokker.core.turn import Effect

        params = {"type": "object", "required": ["id"],
                  "properties": {"id": {"type": "string", "enum": [f.id for f in CATALOG]}}}
        return [ToolSpec(name=FEATURE_GET, description=FEATURE_GET_DESC, parameters=params, effect=Effect.READ,
                         source=self.source_name)]

    def call(self, name: str, arguments: dict[str, Any], ctx: Any) -> Any:
        from nokker.core.turn import ToolResult

        fid = str((arguments or {}).get("id") or "").strip()[:64]
        try:
            minor = bool(self.minor())
        except Exception:  # noqa: BLE001 — profil illisible : le plus prudent
            minor = True
        note = self.notes.get(fid, minor=minor) if name == FEATURE_GET else None
        if note is None:
            return ToolResult(call_id="", name=name, content=FEATURE_GET_UNKNOWN.format(id=fid), is_error=True)
        return ToolResult(call_id="", name=name, content=note)


__all__ = ["ALL_NOTE_TEXTS", "FeatureGetSource", "FeatureNotes", "BLOCK_CLOSE", "BLOCK_OPEN", "BUDGET_CHARS", "FEATURE_GET", "FEATURE_GET_DESC",
           "FEATURE_GET_UNKNOWN", "MAX_NOTES", "NOTE_HEADER", "allowed", "block", "generate", "note_for",
           "render_note", "undescribed"]
