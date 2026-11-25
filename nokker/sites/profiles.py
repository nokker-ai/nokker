"""Sites connectés — profils Chrome chiffrés au repos (docs/CONNECTED-SITES.md §5.2).

Une archive par connecteur, AES-256-GCM sous une clé dérivée de la clé maître (`crypto.seal_profile`) ; une nouvelle
génération à chaque sauvegarde, l'ancienne effacée. Jamais exportée par une route ; effacée à la déconnexion.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from nokker.sites.crypto import open_profile, seal_profile


class ProfileStore:
    def __init__(self, master: bytes, root: str | Path | None = None) -> None:
        self._master = master
        self.root = Path(root) if root else None
        self._mem: dict[str, tuple[int, bytes]] = {}
        self._lock = threading.Lock()

    def _path(self, account: str, cid: str) -> Path:
        import hashlib

        fp = hashlib.sha256(account.encode("utf-8")).hexdigest()[:16]
        return self.root / fp / f"{cid}.enc"  # type: ignore[operator]

    def save(self, account: str, cid: str, archive: bytes) -> int:
        with self._lock:
            gen = self._generation(account, cid) + 1
            blob = seal_profile(self._master, account, cid, gen, archive)
            if self.root is None:
                self._mem[cid] = (gen, blob)
            else:
                p = self._path(account, cid)
                p.parent.mkdir(parents=True, exist_ok=True)
                tmp = p.with_suffix(".tmp")
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "wb") as f:
                    f.write(json.dumps({"gen": gen}).encode() + b"\n" + blob)
                os.replace(tmp, p)
            return gen

    def _generation(self, account: str, cid: str) -> int:
        if self.root is None:
            return self._mem.get(cid, (0, b""))[0]
        p = self._path(account, cid)
        if not p.exists():
            return 0
        return int(json.loads(p.read_bytes().split(b"\n", 1)[0])["gen"])

    def load(self, account: str, cid: str) -> bytes | None:
        with self._lock:
            if self.root is None:
                if cid not in self._mem:
                    return None
                gen, blob = self._mem[cid]
            else:
                p = self._path(account, cid)
                if not p.exists():
                    return None
                head, blob = p.read_bytes().split(b"\n", 1)
                gen = int(json.loads(head)["gen"])
            return open_profile(self._master, account, cid, gen, blob)

    def raw(self, account: str, cid: str) -> bytes | None:  # tests : l'archive telle qu'au repos
        if self.root is None:
            return self._mem.get(cid, (0, None))[1]
        p = self._path(account, cid)
        return p.read_bytes() if p.exists() else None

    def delete(self, account: str, cid: str) -> None:
        with self._lock:
            self._mem.pop(cid, None)
            if self.root is not None:
                p = self._path(account, cid)
                if p.exists():
                    p.unlink()


__all__ = ["ProfileStore"]
