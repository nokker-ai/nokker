"""Magasin des images jointes (X7) : les octets sous l'état, jamais dans le tour.

Disposition, par locataire (le locataire est celui du fil, `tenancy.context.tenant_of_thread`) :

  <root>/<locataire>/blobs/<sha[:2]>/<sha256>         octets, 0600, écrits une fois (tmp + rename)
  <root>/<locataire>/refs/<clé du fil>/<sha256>       marqueur vide : « ce fil référence cette image »

- **Dédupliqué par sha256 dans un locataire** (jamais entre locataires : un locataire ne doit pas pouvoir apprendre
  qu'un autre détient une image, et l'effacement d'un compte ne doit rien laisser derrière lui) ;
- **lecture par fil** : `get(thread_id, sha)` ne rend des octets que si le fil référence l'image ;
- **effacement** : `erase_thread` retire les marqueurs du fil puis les octets que plus aucun fil ne référence ;
  `erase_tenant` retire tout le dossier du locataire (branché sur `nokker erase`, nokker/admin/erase.py).
La clé du fil est un hachage de son identifiant (qui peut contenir « : » ou « / ») : rien de lisible sur le disque.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path

from nokker.core.turn import ImageRef
from nokker.images.validate import RawImage, sniff_media_type
from nokker.tenancy.context import tenant_of_thread

_SHA = re.compile(r"[0-9a-f]{64}")
_TENANT = re.compile(r"tn_[A-Za-z0-9_-]{1,64}")


def thread_key(thread_id: str) -> str:
    return hashlib.sha256(thread_id.encode("utf-8")).hexdigest()[:32]


@dataclass
class ImageStore:
    root: Path
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        self.root = Path(self.root).expanduser()

    # ───────── chemins ─────────

    def _tenant_dir(self, tenant: str) -> Path:
        if not _TENANT.fullmatch(tenant):
            raise ValueError(f"locataire invalide : {tenant[:40]!r}")
        return self.root / tenant

    def _blob(self, tenant: str, sha: str) -> Path:
        return self._tenant_dir(tenant) / "blobs" / sha[:2] / sha

    def _refs(self, tenant: str, thread_id: str) -> Path:
        return self._tenant_dir(tenant) / "refs" / thread_key(thread_id)

    # ───────── écriture ─────────

    def put(self, thread_id: str, image: RawImage) -> ImageRef:
        """Range les octets (une seule copie par sha256 dans le locataire) et note que `thread_id` les référence."""
        sha = hashlib.sha256(image.data).hexdigest()
        tenant = tenant_of_thread(thread_id)
        blob, refs = self._blob(tenant, sha), self._refs(tenant, thread_id)
        with self._lock:
            if not blob.is_file():
                blob.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                tmp = blob.with_name(f".{sha}.{os.getpid()}.{threading.get_ident()}.tmp")
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "wb") as f:
                    f.write(image.data)
                os.replace(tmp, blob)
            refs.mkdir(parents=True, exist_ok=True, mode=0o700)
            (refs / sha).touch(mode=0o600, exist_ok=True)
        return ImageRef(sha256=sha, media_type=image.media_type, size=len(image.data), name=image.name)

    # ───────── lecture ─────────

    def get(self, thread_id: str, sha: str) -> bytes | None:
        """Octets de l'image si CE fil la référence (sinon None : effacée, autre fil, autre locataire)."""
        if not _SHA.fullmatch(sha or ""):
            return None
        tenant = tenant_of_thread(thread_id)
        try:
            if not (self._refs(tenant, thread_id) / sha).is_file():
                return None
            return self._blob(tenant, sha).read_bytes()
        except (OSError, ValueError):
            return None

    def media_type(self, thread_id: str, sha: str) -> str | None:
        data = self.get(thread_id, sha)
        return sniff_media_type(data) if data is not None else None

    def thread_images(self, thread_id: str) -> list[str]:
        refs = self._refs(tenant_of_thread(thread_id), thread_id)
        return sorted(p.name for p in refs.iterdir() if _SHA.fullmatch(p.name)) if refs.is_dir() else []

    # ───────── effacement ─────────

    def erase_thread(self, thread_id: str) -> int:
        """Retire les références du fil et les octets que plus aucun autre fil du locataire ne référence. Rend le
        nombre d'images (octets) supprimées."""
        tenant = tenant_of_thread(thread_id)
        refs = self._refs(tenant, thread_id)
        gone = 0
        with self._lock:
            if not refs.is_dir():
                return 0
            shas = [p.name for p in refs.iterdir() if _SHA.fullmatch(p.name)]
            shutil.rmtree(refs, ignore_errors=True)
            refs_root = self._tenant_dir(tenant) / "refs"
            for sha in shas:
                if any((d / sha).exists() for d in refs_root.iterdir() if d.is_dir()):
                    continue  # encore référencée par un autre fil du locataire
                try:
                    self._blob(tenant, sha).unlink()
                    gone += 1
                except FileNotFoundError:
                    pass
        return gone

    def erase_tenant(self, tenant: str) -> int:
        """Tout le dossier du locataire (effacement d'un compte). Rend le nombre d'images supprimées."""
        d = self._tenant_dir(tenant)
        with self._lock:
            if not d.is_dir():
                return 0
            n = sum(1 for p in (d / "blobs").glob("*/*") if _SHA.fullmatch(p.name))
            shutil.rmtree(d)
        return n

    def count_tenant(self, tenant: str) -> int:
        d = self._tenant_dir(tenant) / "blobs"
        return sum(1 for p in d.glob("*/*") if _SHA.fullmatch(p.name)) if d.is_dir() else 0


__all__ = ["ImageStore", "thread_key"]
