"""Effacement d'un locataire dans l'index sémantique de session (`context_corthexis`, docs/NEVER-CLEAR.md §11) — appelé
par `nokker erase` (nokker/admin/erase.py), qui fournit les fils du locataire lus dans la base du noyau.

L'index range chaque fil dans un PROJET CortHeXis `s-<hash du fil>` (`corthexis_index.project_of`) : un dossier
`<root_dir>/s-…` de notes `ep-….md` (résumé et messages de l'utilisateur) et, si la base CortHeXis est posée
(`CORTHEXIS_DATABASE_URL`, ou `database_url_env`), les lignes de ces notes : `notes` (texte), `chunks` (morceaux et
embeddings, par génération), `note_versions` (historique des textes), `links`, `note_level_floor`, `note_access` et
`recall_log` (requêtes). Un nom de projet ne dit rien du locataire : ce sont ses fils qui les désignent.

Ordre : la base CortHeXis d'abord, dans UNE transaction (validée seulement avec `apply`), puis les dossiers. Une base
injoignable ou en erreur lève `SessionMemoryEraseError` AVANT de toucher un fichier ; l'appelant annule alors
l'effacement de la base du noyau, qui reste rejouable (ses fils servent à retrouver les notes). À blanc : la
transaction est annulée et les fichiers seulement comptés. Un lien symbolique à la place d'un dossier est retiré, jamais
suivi. Les sorties ne portent que des nombres : ni compte, ni adresse, ni nom de projet, ni DSN.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from nokker.context.corthexis_index import project_of

logger = logging.getLogger("nokker.context.corthexis_erase")

DEFAULT_ROOT = "/var/lib/nokker/session-memory"
PROJECT_RE = re.compile(r"s-[0-9a-f]{16}")
FILES_KEY = "corthexis: notes de session (fichiers)"
# Tables de CortHeXis (2.1.2) rattachées à un projet, hors `notes` / `chunks` (traitées à part : compteurs, IDF).
PROJECT_TABLES = ("note_versions", "links", "note_level_floor", "note_access", "recall_log")


class SessionMemoryEraseError(RuntimeError):
    """La base CortHeXis n'a pas pu être effacée : rien n'est effacé (ni fichier, ni ligne)."""


def session_projects(thread_ids: Iterable[str]) -> list[str]:
    return sorted({project_of(t) for t in thread_ids if t})


def _database_url(settings: Mapping[str, Any], environ: Mapping[str, str]) -> str:
    return str(environ.get(str(settings.get("database_url_env") or "CORTHEXIS_DATABASE_URL"), "") or "")


def _erase_db(dsn: str, projects: list[str], apply: bool) -> dict[str, int]:
    import psycopg

    counts: dict[str, int] = {}
    with psycopg.connect(dsn, connect_timeout=10) as conn:
        present = {t for t in ("notes", "chunks", "lex_df", "index_generations", *PROJECT_TABLES)
                   if conn.execute("SELECT to_regclass(%s)", (t,)).fetchone()[0]}
        if "notes" in present:
            notes = conn.execute("SELECT id, lex_tokens FROM notes WHERE project = ANY(%s) FOR UPDATE",
                                 (projects,)).fetchall()
            ids = [r[0] for r in notes]
            if "chunks" in present:
                per_gen = Counter(r[0] for r in conn.execute(
                    "DELETE FROM chunks WHERE note_id = ANY(%s) RETURNING generation_id", (ids,)).fetchall())
                counts["corthexis: chunks"] = sum(per_gen.values())
                if "index_generations" in present:
                    for gid, n in sorted(per_gen.items()):
                        conn.execute("UPDATE index_generations SET chunk_count = chunk_count - %s WHERE id = %s",
                                     (n, gid))
            counts["corthexis: notes"] = conn.execute("DELETE FROM notes WHERE id = ANY(%s)", (ids,)).rowcount
            if "lex_df" in present and notes:  # IDF : même règle que Store._delete_note_row de CortHeXis
                delta = Counter(t for r in notes for t in (r[1] or []))
                delta[""] += len(notes)
                toks = sorted(delta)
                conn.execute("UPDATE lex_df SET df = df - x.d FROM unnest(%s::text[], %s::int[]) AS x(t, d) "
                             "WHERE lex_df.token = x.t", (toks, [delta[t] for t in toks]))
                conn.execute("DELETE FROM lex_df WHERE token = ANY(%s) AND df <= 0", (toks,))
        for t in PROJECT_TABLES:
            if t in present:
                counts[f"corthexis: {t}"] = conn.execute(f"DELETE FROM {t} WHERE project = ANY(%s)",
                                                         (projects,)).rowcount
        if apply:
            conn.commit()
        else:
            conn.rollback()
    return counts


def _remove(path: Path) -> None:
    if path.is_symlink():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _erase_files(root: Path, projects: list[str], apply: bool) -> int:
    n = 0
    for name in projects:
        d = root / name
        if not PROJECT_RE.fullmatch(name) or not (d.is_symlink() or d.exists()):
            continue
        if not d.is_symlink() and d.is_dir():
            n += sum(1 for p in d.iterdir() if p.suffix == ".md" and not p.is_symlink())
        if apply:
            _remove(d)
    return n


def erase_session_memory(settings: Mapping[str, Any] | None, thread_ids: Iterable[str], *, apply: bool,
                         environ: Mapping[str, str] | None = None) -> dict[str, int]:
    """{libellé: nombre} pour les fils `thread_ids` d'un locataire ; {} sans bloc `context_corthexis`. Le bloc peut être
    éteint (`enabled: false`) : des notes restent peut-être d'avant, elles partent aussi."""
    if not settings or not isinstance(settings, Mapping):
        return {}
    projects = session_projects(thread_ids)
    root = Path(os.path.realpath(str(settings.get("root_dir") or DEFAULT_ROOT)))
    counts: dict[str, int] = {}
    dsn = _database_url(settings, os.environ if environ is None else environ)
    if dsn and projects:
        try:
            counts.update(_erase_db(dsn, projects, apply))
        except Exception as e:  # noqa: BLE001 — le message ne reprend pas l'exception : elle peut citer le DSN
            logger.warning("effacement de l'index de session CortHeXis en échec (%s)", type(e).__name__)
            raise SessionMemoryEraseError(f"base de l'index de session CortHeXis injoignable ou en erreur "
                                          f"({type(e).__name__}) : rien n'est effacé, à rejouer") from None
    counts[FILES_KEY] = _erase_files(root, projects, apply)
    return counts


__all__ = ["DEFAULT_ROOT", "FILES_KEY", "SessionMemoryEraseError", "erase_session_memory", "session_projects"]
