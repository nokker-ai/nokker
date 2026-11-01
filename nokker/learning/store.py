"""Stockage de l'apprentissage (docs/LEARNING.md §5) : versions des skills appris, notes en attente de validation,
journal. Trois implémentations de la même interface :

  - `MemoryLearningStore` : en mémoire (tests, instance sans base) ;
  - `FileLearningStore`   : Nokker Lokkal, fichiers JSON sous l'état du noyau (`<racine>/<compte>/…`) ;
  - `PgLearningStore`     : nokker.ai, tables sous RLS (`nokker/learning/pg.py`, migration 046).

Tout est rangé par COMPTE (et, en mémoire et en base, dans le locataire courant `current_tenant()`) : deux
comptes ne lisent jamais les lignes l'un de l'autre, même avec le même nom de skill. Rien n'est jamais détruit : une
version annulée reste dans l'historique, une note refusée reste au journal (texte compris : c'est la trace)."""
from __future__ import annotations

import json
import os
import re
import threading
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from nokker.tenancy.context import current_tenant_or_none

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
_SAFE = re.compile(r"[^A-Za-z0-9_.:-]")
PENDING_STATUSES = ("pending", "approved", "rejected")
JOURNAL_KINDS = ("skill.created", "skill.updated", "skill.reverted", "skill.disabled", "memory.pending",
                 "memory.approved", "memory.rejected")


class LearningConflict(Exception):
    """Écriture concurrente (même version d'un skill posée deux fois) : rien n'a été écrit."""


@dataclass
class SkillVersion:
    name: str
    version: int
    content: str  # SKILL.md complet (frontmatter + corps) ; vide pour une désactivation
    sha256: str
    op: str  # create | update | revert | disable
    active: bool = True  # False : skill désactivé à cette version (retiré du catalogue, historique gardé)
    reverted_to: int | None = None
    thread_id: str = ""
    turn_index: int | None = None
    actor: str = "agent"  # agent (carte approuvée) | owner (geste de l'humain : route, appel direct)
    approval_id: str | None = None
    at: str = ""

    def to_json(self, content: bool = True) -> dict[str, Any]:
        d = asdict(self)
        if not content:
            d.pop("content")
        return d


@dataclass
class PendingNote:
    id: str
    source: str  # memory_note | propose
    kind: str
    title: str
    text: str
    scope: str = "project"
    derived_from: str = "agent"
    project: str = ""  # projet de la mémoire de projet (Lokkal) au moment de la proposition
    links: list[str] = field(default_factory=list)
    thread_id: str = ""
    turn_index: int | None = None
    at: str = ""
    status: str = "pending"
    decided_at: str | None = None
    decided_by: str | None = None
    result: str | None = None  # nom de la note écrite à l'approbation

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class JournalEntry:
    kind: str
    subject: str  # nom du skill, ou id de la note
    at: str
    actor: str = "agent"
    version: int | None = None
    thread_id: str = ""
    turn_index: int | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: f"lj_{uuid.uuid4().hex[:16]}")

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def _tenant() -> str:
    return current_tenant_or_none() or "local"


def _from(cls: type, d: dict[str, Any]) -> Any:
    keys = cls.__dataclass_fields__  # type: ignore[attr-defined]
    return cls(**{k: v for k, v in d.items() if k in keys})


class MemoryLearningStore:
    """En mémoire, rangé par (locataire, compte)."""

    def __init__(self) -> None:
        self._skills: dict[tuple[str, str], dict[str, list[SkillVersion]]] = {}
        self._pending: dict[tuple[str, str], dict[str, PendingNote]] = {}
        self._journal: dict[tuple[str, str], list[JournalEntry]] = {}
        self._lock = threading.RLock()

    # ── skills ──
    def skill_versions(self, account: str, name: str) -> list[SkillVersion]:
        with self._lock:
            return list(self._skills.get((_tenant(), account), {}).get(name, []))

    def skill_names(self, account: str) -> list[str]:
        with self._lock:
            return sorted(self._skills.get((_tenant(), account), {}))

    def add_skill_version(self, account: str, v: SkillVersion) -> SkillVersion:
        with self._lock:
            versions = self._skills.setdefault((_tenant(), account), {}).setdefault(v.name, [])
            if v.version != len(versions) + 1:
                raise LearningConflict(f"{v.name} : version {v.version} attendue {len(versions) + 1}")
            versions.append(v)
            return v

    # ── notes en attente ──
    def put_pending(self, account: str, note: PendingNote) -> None:
        with self._lock:
            self._pending.setdefault((_tenant(), account), {})[note.id] = note

    def get_pending(self, account: str, note_id: str) -> PendingNote | None:
        with self._lock:
            return self._pending.get((_tenant(), account), {}).get(note_id)

    def list_pending(self, account: str, status: str | None = "pending") -> list[PendingNote]:
        with self._lock:
            notes = self._pending.get((_tenant(), account), {}).values()
            return sorted((n for n in notes if status is None or n.status == status), key=lambda n: n.at)

    def decide_pending(self, account: str, note_id: str, status: str, by: str, at: str,
                       result: str | None = None) -> PendingNote | None:
        """Passe une note `pending` à `approved|rejected` ; None si elle n'existe pas ou n'est plus en attente."""
        with self._lock:
            n = self._pending.get((_tenant(), account), {}).get(note_id)
            if n is None or n.status != "pending":
                return None
            n.status, n.decided_by, n.decided_at, n.result = status, by, at, result
            return n

    # ── journal ──
    def journal_append(self, account: str, entry: JournalEntry) -> None:
        with self._lock:
            self._journal.setdefault((_tenant(), account), []).append(entry)

    def journal(self, account: str, limit: int = 200) -> list[JournalEntry]:
        with self._lock:
            return list(reversed(self._journal.get((_tenant(), account), [])))[:limit]


class FileLearningStore:
    """Nokker Lokkal : `<root>/<compte>/{skills/<nom>/v0001.json, pending/<id>.json, journal.jsonl}`, écrits
    atomiquement (fichier temporaire puis renommage), 0600. Le dossier est l'état du noyau, jamais un dépôt. Une machine
    Lokkal n'a qu'un propriétaire : pas de locataire dans le chemin (une route, un tour et une carte approuvée tournent
    sous des portées différentes et doivent lire le même dossier)."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser()
        self._lock = threading.RLock()

    def _dir(self, account: str, *parts: str) -> Path:
        d = self.root.joinpath(_SAFE.sub("_", account or "_"), *parts)
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        return d

    @staticmethod
    def _write(path: Path, data: dict[str, Any]) -> None:
        tmp = path.with_suffix(path.suffix + f".{uuid.uuid4().hex[:6]}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        tmp.replace(path)

    @staticmethod
    def _read(path: Path) -> dict[str, Any] | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    # ── skills ──
    def skill_versions(self, account: str, name: str) -> list[SkillVersion]:
        if not NAME_RE.match(name):
            return []
        d = self._dir(account, "skills", name)
        out = [self._read(p) for p in sorted(d.glob("v*.json"))]
        return [_from(SkillVersion, x) for x in out if x is not None]

    def skill_names(self, account: str) -> list[str]:
        d = self._dir(account, "skills")
        return sorted(p.name for p in d.iterdir() if p.is_dir() and NAME_RE.match(p.name) and any(p.glob("v*.json")))

    def add_skill_version(self, account: str, v: SkillVersion) -> SkillVersion:
        if not NAME_RE.match(v.name):
            raise ValueError(f"nom de skill invalide : {v.name!r}")
        with self._lock:
            path = self._dir(account, "skills", v.name) / f"v{v.version:04d}.json"
            if path.exists() or len(self.skill_versions(account, v.name)) != v.version - 1:
                raise LearningConflict(f"{v.name} : version {v.version} déjà posée")
            self._write(path, v.to_json())
            return v

    # ── notes en attente ──
    def put_pending(self, account: str, note: PendingNote) -> None:
        with self._lock:
            self._write(self._dir(account, "pending") / f"{_SAFE.sub('_', note.id)}.json", note.to_json())

    def get_pending(self, account: str, note_id: str) -> PendingNote | None:
        data = self._read(self._dir(account, "pending") / f"{_SAFE.sub('_', note_id)}.json")
        return _from(PendingNote, data) if data is not None and data.get("id") == note_id else None

    def list_pending(self, account: str, status: str | None = "pending") -> list[PendingNote]:
        notes = [self._read(p) for p in self._dir(account, "pending").glob("*.json")]
        out = [_from(PendingNote, n) for n in notes if n is not None]
        return sorted((n for n in out if status is None or n.status == status), key=lambda n: n.at)

    def decide_pending(self, account: str, note_id: str, status: str, by: str, at: str,
                       result: str | None = None) -> PendingNote | None:
        with self._lock:
            n = self.get_pending(account, note_id)
            if n is None or n.status != "pending":
                return None
            n.status, n.decided_by, n.decided_at, n.result = status, by, at, result
            self.put_pending(account, n)
            return n

    # ── journal ──
    def journal_append(self, account: str, entry: JournalEntry) -> None:
        with self._lock:
            path = self._dir(account) / "journal.jsonl"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry.to_json(), ensure_ascii=False) + "\n")

    def journal(self, account: str, limit: int = 200) -> list[JournalEntry]:
        path = self._dir(account) / "journal.jsonl"
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out: list[JournalEntry] = []
        for line in reversed(lines):
            try:
                out.append(_from(JournalEntry, json.loads(line)))
            except (ValueError, TypeError):
                continue
            if len(out) >= limit:
                break
        return out


__all__ = ["JOURNAL_KINDS", "NAME_RE", "PENDING_STATUSES", "FileLearningStore", "JournalEntry", "LearningConflict",
           "MemoryLearningStore", "PendingNote", "SkillVersion"]
