"""Mémoire de session sur le VRAI moteur CortHeXis (docs/NEVER-CLEAR.md §11).

Chaque fil est un PROJET CortHeXis (`s-<hash>`, un dossier = un projet, scope strict : une recherche ne sort jamais du
fil) ; chaque épisode clos devient une NOTE (`ep-<id>.md`, frontmatter + résumé + messages de l'utilisateur) que
l'indexeur de CortHeXis découpe, plonge (embeddings, profil `leger|standard|gpu`) et range dans pgvector ; la recherche
est hybride (vecteurs + lexical) et peut être reclassée (reranker). On bénéficie sans le réécrire de ce que CortHeXis
apporte : projets et niveaux de classification, journal d'accès, historique des notes, génération d'index versionnée.

Le paquet `corthexis` est importé PARESSEUSEMENT (comme `memory/corthexis.py`) : un déploiement sans ce mode n'a besoin ni
de lui ni de pgvector. L'indexation tourne dans un fil de fond (jamais sur le chemin d'un tour) ; une recherche avant la
fin de l'indexation d'un épisode ne le voit pas encore — le repli lexical du gestionnaire couvre l'intervalle."""
from __future__ import annotations

import hashlib
import logging
import os
import queue
import threading
import time
from pathlib import Path

from nokker.context.semantic import SemanticHit
from nokker.context.types import Episode

logger = logging.getLogger("nokker.context.corthexis_index")

DEFAULT_MIN_SCORE = 0.15  # score hybride (profil sans reranker) — mesuré : pertinent 0,21-0,34, hors sujet ≤ 0,12
DEFAULT_MIN_RERANK = 0.35  # score du reranker (profils standard/gpu) — mesuré : pertinent 0,84, mauvais ≈ 0,02
RELATIVE_FLOOR = 0.6  # un résultat doit valoir au moins 60 % du meilleur
# Une indexation à la fois dans le processus, tous index confondus (mémoire de session, mémoire de projet) : au premier
# démarrage, sans génération d'index active, deux passes simultanées en créeraient chacune une, et la seconde activée
# rendrait invisibles les notes de la première jusqu'à leur prochaine réindexation (X1b).
_INDEX_LOCK = threading.Lock()


def project_of(thread_id: str) -> str:
    """Nom de projet CortHeXis valide (`[a-z0-9][a-z0-9-]*`) et stable pour un fil."""
    return "s-" + hashlib.sha1(thread_id.encode("utf-8")).hexdigest()[:16]


def note_name(episode_id: str) -> str:
    return "ep-" + episode_id.removeprefix("ep_")


def episode_id_of(name: str) -> str:
    return "ep_" + name.removeprefix("ep-")


def render_note(ep: Episode, body: str) -> str:
    desc = " ".join(f"{ep.title} — {ep.summary}".split())[:300].replace('"', "'")
    modified = (ep.day or "")[:10]
    head = ["---", f"name: {note_name(ep.id)}", f'description: "{desc}"', "type: project"]
    if modified:
        head += ["metadata:", f"  modified: {modified}"]
    head.append("---")
    return "\n".join(head) + f"\n# {ep.title}\n\n{body}\n"


class CorthexisSemanticIndex:
    def __init__(self, root_dir: str | Path, *, database_url: str = "", embed_urls: str = "", profile: str = "leger",
                 rerank_url: str = "", min_score: float = DEFAULT_MIN_SCORE, min_rerank: float = DEFAULT_MIN_RERANK,
                 deep: bool = False, chunk_target: int = 300) -> None:
        self.root = Path(root_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.min_score, self.min_rerank, self.deep = min_score, min_rerank, deep
        self.winner_floor, self.winner_ratio = 0.10, 1.4
        self.chunk_target = chunk_target  # petits morceaux : un message ou deux par embedding (défaut CortHeXis : 1200)
        for env_name, value in (("CORTHEXIS_DATABASE_URL", database_url), ("CORTHEXIS_EMBED_URLS", embed_urls),
                                ("CORTHEXIS_MEMORY_PROFILE", profile), ("CORTHEXIS_RERANK_URL", rerank_url)):
            if value:
                os.environ.setdefault(env_name, value)
        self._q: queue.Queue[str | None] = queue.Queue()
        self._pending: set[str] = set()
        self._lock = threading.Lock()
        self._idle = threading.Event()
        self._idle.set()
        self._worker: threading.Thread | None = None
        self._failures = 0
        self._disabled_until = 0.0

    # ───────────── écriture ─────────────

    def index_episode(self, ep: Episode, body: str) -> None:
        project = project_of(ep.thread_id)
        d = self.root / project
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{note_name(ep.id)}.md"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(render_note(ep, body), encoding="utf-8")
        tmp.replace(path)  # atomique : l'indexeur ne lit jamais un fichier à moitié écrit
        self._enqueue(project)

    def enqueue_project(self, project: str) -> None:
        """Réindexe en fond le dossier `root/<projet>` (notes écrites par l'appelant : mémoire de projet, X1)."""
        self._enqueue(project)

    def _enqueue(self, project: str) -> None:
        with self._lock:
            if project not in self._pending:
                self._pending.add(project)
                self._idle.clear()
                self._q.put(project)
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._run, name="corthexis-session-index", daemon=True)
                self._worker.start()

    def _run(self) -> None:
        while True:
            try:
                project = self._q.get(timeout=5)
            except queue.Empty:
                with self._lock:
                    if not self._pending:
                        return
                continue
            with self._lock:
                self._pending.discard(project)
            try:
                self._index_project(project)
            except Exception:
                logger.warning("indexation du projet %s en échec", project, exc_info=True)
            finally:
                with self._lock:
                    if not self._pending:
                        self._idle.set()

    def _index_project(self, project: str) -> None:
        from corthexis import service as cx_service
        from corthexis.indexer import IndexConfig, Indexer

        cfg = IndexConfig.from_env(memory_dir=self.root / project, project=project, normalize=False,
                                   write_index=False, chunk_target=self.chunk_target)
        with _INDEX_LOCK:
            rep = Indexer(cx_service.get_store(), cx_service.embedder(), cfg).run()
        for w in getattr(rep, "warnings", []) or []:
            logger.warning("corthexis(session %s): %s", project, w)

    def flush(self, timeout: float = 30.0) -> bool:
        return self._idle.wait(timeout)

    # ───────────── lecture ─────────────

    def search(self, thread_id: str, query: str, k: int = 5) -> list[SemanticHit]:
        if not query.strip() or time.monotonic() < self._disabled_until:
            return []
        project = project_of(thread_id)
        if not (self.root / project).is_dir():
            return []
        try:
            from corthexis import service as cx_service

            rows = cx_service.search(query, top_k=max(k, 8), deep=self.deep, projects=(project,))
            self._failures = 0
        except Exception:
            self._failures += 1
            if self._failures >= 3:
                self._disabled_until = time.monotonic() + 60.0  # disjoncteur : ne pas ajouter de latence à chaque tour
                logger.warning("corthexis(session): recherche indisponible, coupée 60 s", exc_info=True)
            return []
        hits: list[SemanticHit] = []
        for row in rows:
            if not isinstance(row, dict) or "error" in row or "info" in row:
                continue
            name = str(row.get("note_name") or "")
            if not name.startswith("ep-"):
                continue
            rerank = row.get("rerank")
            hits.append(SemanticHit(episode_id=episode_id_of(name), score=float(row.get("score") or 0.0),
                                    text=str(row.get("snippet") or ""), rerank=float(rerank) if rerank is not None
                                    else None))
        if not hits:
            return []
        hits.sort(key=lambda h: -h.score)
        second = hits[1].score if len(hits) > 1 else 0.0
        kept: list[SemanticHit] = []
        for i, h in enumerate(hits):
            strong = h.rerank is not None and h.rerank >= self.min_rerank  # le reranker est sûr de lui
            absolute = h.score >= self.min_score and h.score >= RELATIVE_FLOOR * hits[0].score
            # Un vainqueur NET (≥ 1,4× le suivant) compte même sous le seuil absolu : sur de vraies notes (résumé +
            # messages), les scores sont bas et le reranker sévère ; ce qui distingue un bon résultat du bruit, c'est
            # l'écart (mesuré : 0,16 contre 0,09 pour le bon, 0,06/0,05/0,05 sans vainqueur sur un hors-sujet).
            winner = i == 0 and h.score >= self.winner_floor and h.score >= self.winner_ratio * second
            if strong or absolute or winner:
                kept.append(h)
        for h in kept:
            if h.rerank is not None and h.rerank >= self.min_rerank:
                h.score = max(h.score, h.rerank)
        return kept[:k]

    def search_rows(self, projects: tuple[str, ...], query: str, k: int = 8) -> list[dict]:
        """Lignes brutes de CortHeXis dans ces projets (mémoire de projet, X1 : le seuil est chez l'appelant) ; même
        disjoncteur que `search`. Jamais d'exception."""
        if not query.strip() or time.monotonic() < self._disabled_until:
            return []
        projects = tuple(p for p in projects if (self.root / p).is_dir())
        if not projects:
            return []
        try:
            from corthexis import service as cx_service

            rows = cx_service.search(query, top_k=k, deep=self.deep, projects=projects)
            self._failures = 0
        except Exception:
            self._failures += 1
            if self._failures >= 3:
                self._disabled_until = time.monotonic() + 60.0
                logger.warning("corthexis(projet): recherche indisponible, coupée 60 s", exc_info=True)
            return []
        return [r for r in rows if isinstance(r, dict) and "error" not in r and "info" not in r]

    def close(self) -> None:
        try:
            from corthexis import service as cx_service

            cx_service.reset()
        except Exception:
            logger.debug("corthexis(session): reset ignoré", exc_info=True)


__all__ = ["DEFAULT_MIN_RERANK", "DEFAULT_MIN_SCORE", "CorthexisSemanticIndex", "episode_id_of", "note_name", "project_of", "render_note"]
