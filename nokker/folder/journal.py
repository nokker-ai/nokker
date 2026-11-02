"""nokker/folder/journal.py — point de retour EXACT d'un passage du mode dossier (docs/FOLDER-MODE.md §4).

Pourquoi pas les points de retour git des sessions de code (`nokker/tools/checkpoints.py`) : ils ignorent ce que le
`.gitignore` exclut, les dossiers `build/`, `node_modules/`… et tout fichier de plus de 10 Mo — un dossier de bureau
(PDF scannés, photos) y perdrait justement ses gros fichiers. Ici le point de retour est fait pour ce cas :

- **Instantané** (`manifest.json`), pris avant la PREMIÈRE écriture du passage : chaque fichier des deux zones
  (`dossier`, `sortie`) avec sa taille et son mtime en ns, et la liste des dossiers. Rien n'est recopié d'avance.
- **Copie avant écriture** : avant qu'un fichier existant soit remplacé, son contenu est copié (mtime compris) dans
  `blobs/<sha256>` ; un déplacement ou une mise à la corbeille est un `rename`, sans copie.
- **Journal** (`journal.jsonl`) : une ligne par geste (`create`, `overwrite`, `move`, `mkdir`), avec l'empreinte du
  fichier APRÈS le geste.
- **Retour arrière** : le journal rejoué à l'envers. Un fichier créé par le passage n'est jamais effacé : il part dans
  `annules/` (hors des zones). Un fichier modifié par l'utilisateur depuis (empreinte différente) n'est pas touché :
  il est rendu en conflit, sauf `force`. Puis l'état est comparé à l'instantané (`exact`).

Rien ici n'appelle de modèle ni de réseau ; aucun chemin n'est suivi à travers un lien symbolique.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TRASH_DIR = ".corbeille-nokker"  # dans le dossier : la corbeille d'un passage = TRASH_DIR/<run_id>/<chemin>
_CHUNK = 1 << 20


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def scan(root: str | Path) -> dict[str, Any]:
    """`{files: {rel: [taille, mtime_ns]}, dirs: [rel…]}` d'une zone, sans suivre les liens (un lien est noté comme
    fichier de taille -1 : il doit rester tel quel)."""
    root = str(root)
    files: dict[str, list[int]] = {}
    dirs: list[str] = []
    if not os.path.isdir(root):
        return {"files": files, "dirs": dirs, "exists": False}
    dir_mtimes: dict[str, int] = {".": os.lstat(root).st_mtime_ns}
    for cur, dnames, fnames in os.walk(root, followlinks=False):
        rel_cur = os.path.relpath(cur, root)
        for d in list(dnames):
            full = os.path.join(cur, d)
            rel = os.path.normpath(os.path.join(rel_cur, d))
            if os.path.islink(full):
                dnames.remove(d)
                files[rel] = [-1, 0]
                continue
            dirs.append(rel)
            dir_mtimes[rel] = os.lstat(full).st_mtime_ns
        for n in fnames:
            full = os.path.join(cur, n)
            rel = os.path.normpath(os.path.join(rel_cur, n))
            st = os.lstat(full)
            files[rel] = [-1, 0] if os.path.islink(full) else [st.st_size, st.st_mtime_ns]
    return {"files": files, "dirs": sorted(dirs), "exists": True, "dir_mtimes": dir_mtimes}


@dataclass
class Journal:
    """Le point de retour d'UN passage : `<état>/folder/<run_id>/{manifest.json, journal.jsonl, blobs/, annules/}`."""

    run_dir: Path
    zones: dict[str, str]  # zone -> chemin hôte (realpath) : {"dossier": …, "sortie": …}
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        self.run_dir = Path(self.run_dir)
        (self.run_dir / "blobs").mkdir(parents=True, exist_ok=True)

    # ── instantané ──
    @property
    def manifest_path(self) -> Path:
        return self.run_dir / "manifest.json"

    def has_snapshot(self) -> bool:
        return self.manifest_path.is_file()

    def snapshot(self) -> dict[str, Any]:
        """Prend l'instantané (une fois : un second appel rend le premier)."""
        with self._lock:
            if self.manifest_path.is_file():
                return json.loads(self.manifest_path.read_text(encoding="utf-8"))
            data = {"taken_at": time.time(), "zones": {z: scan(p) for z, p in self.zones.items()}}
            tmp = self.manifest_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.manifest_path)
            return data

    # ── journal ──
    def _append(self, entry: dict[str, Any]) -> None:
        entry = {"at": time.time(), **entry}
        with self._lock, open(self.run_dir / "journal.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def entries(self) -> list[dict[str, Any]]:
        p = self.run_dir / "journal.jsonl"
        if not p.is_file():
            return []
        return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]

    def keep_original(self, full: str) -> str:
        """Copie le contenu actuel de `full` dans `blobs/` (mtime compris) ; rend son sha256."""
        sha = sha256_file(full)
        blob = self.run_dir / "blobs" / sha
        if not blob.exists():
            tmp = blob.with_suffix(".tmp")
            shutil.copy2(full, tmp)
            os.replace(tmp, blob)
        return sha

    def note_mkdir(self, zone: str, rel: str) -> None:
        self._append({"op": "mkdir", "zone": zone, "path": rel})

    def note_write(self, zone: str, rel: str, before_sha: str | None, after_sha: str) -> None:
        if before_sha is None:
            self._append({"op": "create", "zone": zone, "path": rel, "sha_after": after_sha})
        else:
            self._append({"op": "overwrite", "zone": zone, "path": rel, "blob": before_sha, "sha_after": after_sha})

    def note_move(self, zone: str, src: str, dst: str, sha: str | None, trash: bool = False) -> None:
        self._append({"op": "move", "zone": zone, "path": src, "to": dst, "sha": sha, "trash": trash})

    # ── retour arrière ──
    def _full(self, zone: str, rel: str) -> str:
        return os.path.normpath(os.path.join(self.zones[zone], rel))

    def _park(self, full: str, zone: str, rel: str) -> str:
        """Range un fichier créé par le passage dans `annules/` (jamais effacé)."""
        dest = self.run_dir / "annules" / zone / rel
        n = 1
        while dest.exists():
            dest = self.run_dir / "annules" / zone / f"{rel}.{n}"
            n += 1
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(full, dest)
        return str(dest)

    def _same(self, full: str, sha: str | None) -> bool:
        if not os.path.isfile(full) or os.path.islink(full):
            return False
        return sha is None or sha256_file(full) == sha

    def restore(self, force: bool = False) -> dict[str, Any]:
        """Rejoue le journal à l'envers. Rend `{restored, conflicts, parked, exact, differences}`."""
        restored: list[str] = []
        conflicts: list[dict[str, str]] = []
        parked: list[str] = []
        for e in reversed(self.entries()):
            op, zone, rel = e["op"], e["zone"], e["path"]
            label = f"{zone}/{rel}"
            full = self._full(zone, rel)
            if op == "mkdir":
                try:
                    os.rmdir(full)  # vide seulement : sinon il contient autre chose que le passage, on le garde
                except OSError:
                    pass
                continue
            if op == "create":
                if not os.path.lexists(full):
                    continue
                if not self._same(full, e["sha_after"]) and not force:
                    conflicts.append({"path": label, "why": "modifié depuis le passage"})
                    continue
                parked.append(self._park(full, zone, rel))
                restored.append(label)
            elif op == "overwrite":
                if os.path.lexists(full) and not self._same(full, e["sha_after"]) and not force:
                    conflicts.append({"path": label, "why": "modifié depuis le passage"})
                    continue
                if os.path.lexists(full):
                    parked.append(self._park(full, zone, rel))
                os.makedirs(os.path.dirname(full), exist_ok=True)
                tmp = full + ".nokker-retour"
                shutil.copy2(self.run_dir / "blobs" / e["blob"], tmp)
                os.replace(tmp, full)
                restored.append(label)
            elif op == "move":
                dst = self._full(zone, e["to"])
                if os.path.lexists(full):
                    conflicts.append({"path": label, "why": "un fichier occupe déjà l'emplacement d'origine"})
                    continue
                if not self._same(dst, e.get("sha")) and not force:
                    conflicts.append({"path": f"{zone}/{e['to']}", "why": "absent ou modifié depuis le passage"})
                    continue
                os.makedirs(os.path.dirname(full) or self.zones[zone], exist_ok=True)
                os.rename(dst, full)
                restored.append(label)
                if e.get("trash"):  # ranger les dossiers vides de la corbeille de CE passage
                    self._prune_empty(os.path.dirname(dst), self._full(zone, TRASH_DIR))
        self._restore_dir_mtimes()
        diff = self.compare()
        return {"restored": restored, "conflicts": conflicts, "parked": parked, "exact": not diff and not conflicts,
                "differences": diff}

    def _restore_dir_mtimes(self) -> None:
        """Un renommage change la date des dossiers parents : on leur rend celle de l'instantané (au mieux)."""
        if not self.manifest_path.is_file():
            return
        snap = json.loads(self.manifest_path.read_text(encoding="utf-8"))["zones"]
        for zone, before in snap.items():
            for rel, mtime in sorted((before.get("dir_mtimes") or {}).items(), key=lambda kv: -kv[0].count("/")):
                full = os.path.normpath(os.path.join(self.zones[zone], rel))
                if os.path.isdir(full) and not os.path.islink(full):
                    try:
                        os.utime(full, ns=(os.lstat(full).st_atime_ns, mtime))
                    except OSError:
                        pass

    @staticmethod
    def _prune_empty(path: str, stop: str) -> None:
        stop = os.path.normpath(stop)
        path = os.path.normpath(path)
        while path.startswith(stop):
            try:
                os.rmdir(path)
            except OSError:
                return
            if path == stop:
                return
            path = os.path.dirname(path)

    def compare(self) -> list[str]:
        """Écarts entre l'état actuel des zones et l'instantané (chemins, taille, mtime) ; [] = identique."""
        if not self.manifest_path.is_file():
            return []
        snap = json.loads(self.manifest_path.read_text(encoding="utf-8"))["zones"]
        out: list[str] = []
        for zone, before in snap.items():
            now = scan(self.zones[zone])
            if before.get("exists", True) != now.get("exists", True):
                out.append(f"{zone}/ : " + ("absent" if before.get("exists", True) else "en plus"))
            for rel in sorted(set(before["files"]) | set(now["files"])):
                a, b = before["files"].get(rel), now["files"].get(rel)
                if a != b:
                    out.append(f"{zone}/{rel} : " + ("absent" if b is None else "en plus" if a is None else "changé"))
            for rel in sorted(set(before["dirs"]) ^ set(now["dirs"])):
                out.append(f"{zone}/{rel}/ : " + ("absent" if rel in before["dirs"] else "en plus"))
        return out


__all__ = ["TRASH_DIR", "Journal", "scan", "sha256_file"]
