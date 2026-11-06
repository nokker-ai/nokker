"""nokker/tools/card_snapshots.py — point de retour PAR CARTE d'une écriture approuvée par le relecteur automatique
(docs/APPROVALS.md §8, « Révocation et restauration »).

Les points de retour git (`nokker/tools/checkpoints.py`) sont pris une fois par tour et dépendent de la bibliothèque
amont : ils ne disent pas l'état d'avant l'outil d'UNE carte. Ici, juste avant qu'une écriture approuvée
automatiquement (`write_file`, `patch`) s'exécute, l'enveloppe des points de retour copie chaque fichier visé (ou note
« fichier absent ») dans un dossier hors du bac à sable, puis note l'empreinte de chaque fichier APRÈS l'outil.

- **Limites** : fichiers sous la racine du dossier de travail seulement (chemin résolu, lien symbolique refusé),
  `max_file_bytes` par fichier et `max_total_bytes` par carte.
- **Pas de point, pas d'approbation automatique** : AVANT de relire une carte `write_file`/`patch`, le noyau appelle
  `preflight` ; une capture impossible (hors limites, aucun magasin pour le fil) rend la carte à l'humain, par règle,
  sans appel au modèle. Si la capture échoue quand même au moment de l'outil (fichier changé entre-temps), le point
  existe avec `available: false` et la raison.
- **Proposition** (`offer`) : `{available, checkpoint_id, files, reason?}`, rendu dans la réponse de révocation.
- **Application** (`apply`) : seulement sur demande explicite (`POST /v1/approvals/{id}/auto-review/rollback`), jamais
  à la révocation. Un fichier modifié depuis l'écriture (empreinte différente de celle d'après l'outil) n'est PAS
  écrasé : il est rendu en conflit. Une seule application par carte.
- **Rétention** : les points de plus de 30 jours (fenêtre de révocation) sont purgés à la création d'un nouveau.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger("nokker.tools.card_snapshots")

SNAPSHOT_TOOLS = ("write_file", "patch")
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024
RETENTION_S = 30 * 86400.0
MANIFEST = "manifest.json"
_ID = re.compile(r"^[A-Za-z0-9_-]{1,96}$")
_V4A = re.compile(r"^\*\*\* (?:(?:Update|Add|Delete) File|Move to): (.+)$", re.MULTILINE)


class RollbackUnavailable(RuntimeError):
    """Restauration impossible (point absent, incomplet, déjà appliqué) : le message est pour l'utilisateur."""


def touched_paths(name: str, args: dict[str, Any]) -> list[str]:
    """Chemins (vus par l'outil) qu'un `write_file` ou un `patch` peut écrire, créer, supprimer ou déplacer."""
    if name == "patch" and args.get("mode") == "patch":
        return list(dict.fromkeys(m.group(1).strip() for m in _V4A.finditer(str(args.get("patch") or ""))))
    if name in SNAPSHOT_TOOLS and args.get("path"):
        return [str(args["path"])]
    return []


def _sha(path: Path) -> str | None:
    """Empreinte du fichier, None s'il n'existe pas."""
    if not path.exists() and not path.is_symlink():
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _inside(root: Path, host: str) -> Path:
    """Chemin hôte sous `root` (dossiers parents résolus, dernier élément jamais un lien), ou ValueError."""
    real_root = Path(os.path.realpath(root))
    p = Path(os.path.abspath(host))
    parent = Path(os.path.realpath(p.parent))
    if parent != real_root and real_root not in parent.parents:
        raise ValueError("hors du dossier de travail")
    full = parent / p.name
    if full.is_symlink():
        raise ValueError("lien symbolique")
    if full.exists() and not full.is_file():
        raise ValueError("pas un fichier ordinaire")
    return full


class CardSnapshots:
    """Points de retour par carte. `root_of(thread_id)` : racine hôte du dossier de travail du fil ;
    `dir_of(root)` : dossier de stockage pour cette racine, HORS de tout montage du bac à sable."""

    def __init__(self, root_of: Callable[[str], Path | None], dir_of: Callable[[Path], Path], *,
                 max_file_bytes: int = MAX_FILE_BYTES, max_total_bytes: int = MAX_TOTAL_BYTES,
                 retention_s: float = RETENTION_S, clock: Callable[[], float] = time.time,
                 mount: Callable[[], str] | None = None) -> None:
        self.root_of, self.dir_of = root_of, dir_of
        self.mount = mount or (lambda: "/workspace")  # point de montage vu par l'outil (posé par l'enveloppe)
        self.max_file_bytes, self.max_total_bytes = max_file_bytes, max_total_bytes
        self.retention_s, self.clock = retention_s, clock
        self._mu = threading.Lock()

    # ── stockage ──
    def _card_dir(self, root: Path, approval_id: str) -> Path:
        if not _ID.match(approval_id or ""):
            raise ValueError("identifiant de carte invalide")
        return Path(self.dir_of(root)) / approval_id

    @staticmethod
    def _write_manifest(d: Path, data: dict[str, Any]) -> None:
        tmp = d / (MANIFEST + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, d / MANIFEST)

    def _load(self, approval_id: str, thread_id: str) -> tuple[Path, Path, dict[str, Any]] | None:
        root = self.root_of(thread_id)
        if root is None:
            return None
        d = self._card_dir(Path(root), approval_id)
        try:
            data = json.loads((d / MANIFEST).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("thread_id") != thread_id \
                or data.get("root") != os.path.realpath(root):
            return None
        return Path(root), d, data

    def _prune(self, base: Path) -> None:
        limit = self.clock() - self.retention_s
        try:
            for d in base.iterdir():
                if d.is_dir() and not d.is_symlink() and d.stat().st_mtime < limit:
                    shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass

    # ── capture ──
    def _plan(self, root: Path, hosts: list[str]) -> list[tuple[Path, str, int | None]]:
        """(chemin, relatif, taille ou None si absent) de chaque fichier visé ; ValueError si hors limites."""
        if not hosts:
            raise ValueError("aucun fichier visé")
        real_root = Path(os.path.realpath(root))
        out, total = [], 0
        for host in hosts:
            full = _inside(root, host)
            rel = str(full.relative_to(real_root))
            size = full.stat().st_size if full.exists() else None
            total += size or 0
            if size is not None and (size > self.max_file_bytes or total > self.max_total_bytes):
                raise ValueError(f"{rel} : trop grand pour un point de retour")
            out.append((full, rel, size))
        return out

    def check(self, root: Path, tool: str, args: dict[str, Any]) -> str | None:
        """Raison pour laquelle un point de retour serait impossible pour cet appel, ou None."""
        from nokker.tools.checkpoints import host_path

        try:
            self.dir_of(root)
            self._plan(root, [host_path(root, p, self.mount()) for p in touched_paths(tool, args)])
        except (OSError, ValueError) as e:
            return str(e) if isinstance(e, ValueError) else type(e).__name__
        return None

    def before(self, approval_id: str, thread_id: str, root: Path, tool: str, hosts: list[str]) -> None:
        """Copie des fichiers visés AVANT l'outil. Ne lève jamais : un échec donne un point `available: false`."""
        try:
            d = self._card_dir(root, approval_id)
            d.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._prune(d.parent)
            if d.exists():
                shutil.rmtree(d)
            (d / "blobs").mkdir(mode=0o700, parents=True)
        except (OSError, ValueError):
            logger.warning("point de retour de la carte %s non créé", approval_id, exc_info=True)
            return
        data: dict[str, Any] = {"approval_id": approval_id, "thread_id": thread_id, "tool": tool,
                                "root": os.path.realpath(root), "created_at": self.clock(), "available": True,
                                "files": []}
        try:
            for full, rel, size in self._plan(root, hosts):
                entry: dict[str, Any] = {"path": rel, "existed": size is not None}
                if entry["existed"]:
                    entry["sha_before"] = sha = _sha(full)
                    entry["mode"] = full.stat().st_mode & 0o7777
                    blob = d / "blobs" / str(sha)
                    if not blob.exists():
                        shutil.copyfile(full, blob)
                data["files"].append(entry)
        except (OSError, ValueError) as e:
            data.update(available=False, reason=str(e) if isinstance(e, ValueError) else type(e).__name__)
        try:
            self._write_manifest(d, data)
        except OSError:
            logger.warning("manifeste du point de la carte %s non écrit", approval_id, exc_info=True)

    def after(self, approval_id: str, root: Path) -> None:
        """Empreinte de chaque fichier APRÈS l'outil (détection d'une modification ultérieure)."""
        try:
            d = self._card_dir(root, approval_id)
            data = json.loads((d / MANIFEST).read_text(encoding="utf-8"))
            real_root = Path(data["root"])
            for f in data["files"]:
                p = real_root / f["path"]
                f["sha_after"] = None if p.is_symlink() else _sha(p)
            data["after_at"] = self.clock()
            self._write_manifest(d, data)
        except (OSError, ValueError, KeyError, TypeError):
            logger.warning("empreintes après l'outil de la carte %s non notées", approval_id, exc_info=True)

    # ── proposition et application ──
    def offer(self, approval_id: str, thread_id: str) -> dict[str, Any] | None:
        """`{available, checkpoint_id, files, reason?, applied_at?}`, ou None si cette carte n'a pas de point ici."""
        found = self._load(approval_id, thread_id)
        if found is None:
            return None
        _root, _d, data = found
        out: dict[str, Any] = {"available": bool(data.get("available")) and "after_at" in data
                               and not data.get("applied_at"),
                               "checkpoint_id": approval_id, "files": [f["path"] for f in data.get("files", [])],
                               "applied": bool(data.get("applied_at"))}
        if not data.get("available"):
            out["reason"] = data.get("reason") or "point incomplet"
        elif "after_at" not in data:
            out["reason"] = "écriture non terminée"
        if data.get("applied_at"):
            applied = data.get("applied") or {}
            out.update(reason="restauration déjà appliquée", applied_at=data["applied_at"],
                       restored=list(applied.get("restored") or []), conflicts=list(applied.get("conflicts") or []))
        return out

    def apply(self, approval_id: str, thread_id: str) -> dict[str, Any]:
        """Remet chaque fichier dans son état d'avant l'outil, sauf ceux modifiés depuis (conflits). Lève
        `RollbackUnavailable` si le point manque, est incomplet ou a déjà servi."""
        with self._mu:
            found = self._load(approval_id, thread_id)
            if found is None:
                raise RollbackUnavailable("aucun point de retour pour cette carte")
            root, d, data = found
            offer = self.offer(approval_id, thread_id) or {}
            if not offer.get("available"):
                raise RollbackUnavailable(offer.get("reason") or "point de retour indisponible")
            restored, conflicts = [], []
            for f in data["files"]:
                rel = f["path"]
                try:
                    full = _inside(root, str(Path(data["root"]) / rel))
                    if _sha(full) != f.get("sha_after"):
                        conflicts.append(rel)
                        continue
                    if f["existed"]:
                        full.parent.mkdir(parents=True, exist_ok=True)
                        tmp = full.with_name(f".{full.name}.nokker-restore")
                        shutil.copyfile(d / "blobs" / f["sha_before"], tmp)
                        os.chmod(tmp, int(f.get("mode") or 0o644))
                        os.replace(tmp, full)
                    elif full.exists():
                        full.unlink()
                    restored.append(rel)
                except (OSError, ValueError):
                    logger.warning("restauration de %s (carte %s) en échec", rel, approval_id, exc_info=True)
                    conflicts.append(rel)
            data["applied_at"] = self.clock()
            data["applied"] = {"restored": restored, "conflicts": conflicts}
            self._write_manifest(d, data)
        return {"checkpoint_id": approval_id, "restored": restored, "conflicts": conflicts}


def preflight(stores: list[Any], thread_id: str, tool: str, args: dict[str, Any]) -> str | None:
    """Avant d'approuver automatiquement une écriture de fichier : None si un magasin du fil peut en prendre le point de
    retour, sinon la raison (rangée sur la carte, jamais dans un texte vu par le modèle)."""
    for s in stores or ():
        try:
            root = s.root_of(thread_id)
        except Exception:  # noqa: BLE001 — fil inconnu de ce magasin
            root = None
        if root is not None:
            why = s.check(Path(root), tool, args)
            return None if why is None else f"point de retour impossible ({why})"
    return "aucun point de retour possible pour ce fil"


def find_offer(stores: list[Any], approval_id: str, thread_id: str) -> tuple[Any, dict[str, Any]] | None:
    """Premier magasin qui connaît cette carte : (magasin, proposition)."""
    for s in stores or ():
        try:
            o = s.offer(approval_id, thread_id)
        except (OSError, ValueError, PermissionError):
            continue
        if o is not None:
            return s, o
    return None


__all__ = ["CardSnapshots", "MAX_FILE_BYTES", "MAX_TOTAL_BYTES", "RollbackUnavailable", "SNAPSHOT_TOOLS",
           "find_offer", "preflight", "touched_paths"]
