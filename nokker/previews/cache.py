"""nokker/previews/cache.py — cache des aperçus (TTL mémoire) et cache disque borné des images.

Deux cycles de vie différents (docs/BUBBLES.md §5) : l'APERÇU (JSON) expire à 24 h, mais une IMAGE
déjà téléchargée reste servable (`GET /v1/previews/img/<sha256>`) même après l'expiration de
l'aperçu qui la référence — elle n'a pas de TTL, seulement une borne de taille TOTALE (éviction la
plus ancienne d'abord, par un COMPTEUR d'écriture — pas le `mtime` du fichier : vu en test, deux
écritures rapprochées peuvent recevoir le MÊME `mtime` selon le système de fichiers, ce qui rendrait
l'ordre d'éviction non déterministe).
"""
from __future__ import annotations

import hashlib
import hmac
import itertools
import json
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# Images signées (docs/BUBBLES.md §9.3) : un `<img src=…>` ne pose jamais d'en-tête `Authorization`,
# donc `GET /v1/previews/img/<sha>` accepte AUSSI une signature `?exp=<epoch>&sig=<hmac tronqué>` en
# plus de l'auth normale (voir `nokker/api/previews_routes.py::_handle_image`). TTL fixe, indépendant
# du TTL de l'aperçu JSON (24 h aussi, mais posé séparément à chaque signature — voir `sign_image`).
IMG_SIGN_TTL_S = 86400.0
_SIG_HEX_LEN = 32  # 16 octets (128 bits) — tronqué, largement suffisant pour une URL d'image signée


def sign_image(sha: str, secret: bytes, *, now: float | None = None, ttl_s: float = IMG_SIGN_TTL_S
               ) -> tuple[str, str]:
    """`(exp, sig)` pour `/v1/previews/img/<sha>?exp=<exp>&sig=<sig>`. `sig` = HMAC-SHA256 de
    `f"{sha}.{exp}"` (clé = secret du noyau), tronqué à `_SIG_HEX_LEN` caractères hexadécimaux."""
    now = time.time() if now is None else now
    exp = str(int(now + ttl_s))
    sig = hmac.new(secret, f"{sha}.{exp}".encode("ascii"), hashlib.sha256).hexdigest()[:_SIG_HEX_LEN]
    return exp, sig


def verify_image_signature(sha: str, exp: str, sig: str, secret: bytes, *, now: float | None = None) -> bool:
    """`True` si `sig` est une signature valide ET non expirée pour `sha`. Jamais `==` sur la
    signature (`hmac.compare_digest`, résistant au timing)."""
    now = time.time() if now is None else now
    try:
        exp_i = int(exp)
    except (TypeError, ValueError):
        return False
    if exp_i < now:
        return False
    expected = hmac.new(secret, f"{sha}.{exp}".encode("ascii"), hashlib.sha256).hexdigest()[:_SIG_HEX_LEN]
    return hmac.compare_digest(expected, sig or "")


@dataclass
class TtlCache:
    """LRU + TTL générique (même logique que `tools/web.py::_QueryCache`, dupliquée à dessein :
    ce module ne dépend pas des internes privés de `tools/web.py`)."""

    maxsize: int = 512
    ttl_s: float = 86400.0
    _store: OrderedDict[str, tuple[float, Any]] = field(default_factory=OrderedDict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def get(self, key: str, now: float | None = None) -> Any:
        now = time.monotonic() if now is None else now
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            ts, value = entry
            if now - ts > self.ttl_s:
                del self._store[key]
                return None
            self._store.move_to_end(key)
            return value

    def set(self, key: str, value: Any, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            self._store[key] = (now, value)
            self._store.move_to_end(key)
            while len(self._store) > self.maxsize:
                self._store.popitem(last=False)

    def __len__(self) -> int:
        return len(self._store)


def is_valid_sha256(value: str) -> bool:
    return bool(_SHA256_RE.match(value))


@dataclass
class ImageDiskCache:
    """Cache disque borné, clé = sha256 hex des OCTETS de l'image (déduplication : la même image
    référencée par deux pages n'est stockée qu'une fois). Pas de TTL — seulement une borne de
    taille TOTALE (`max_bytes`), éviction la plus ancienne d'abord (compteur d'écriture, pas mtime)
    quand on la dépasse.

    Deux fichiers par entrée : `<sha>.bin` (octets) et `<sha>.json` (métadonnées : content_type,
    size, fetched_at) — pas de base de données pour un cache qui doit survivre à un redémarrage
    sans migration."""

    directory: Path
    max_bytes: int = 200 * 1024 * 1024
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # Compteur d'écriture en mémoire (PAS le `mtime` du fichier, voir docstring de la classe) — défini
    # par instance, PAS partagé entre instances/process : ordonne correctement les écritures de CE
    # process ; une entrée écrite par un process antérieur (avant un redémarrage) porte un `seq` plus
    # petit par construction (relu depuis son `.json`, jamais régénéré), donc reste bien « plus
    # ancienne » que tout ce qu'écrit le nouveau process — l'ordre relatif reste correct même à travers
    # un redémarrage, tant qu'on ne compare jamais deux `seq` de deux process DIFFÉRENTS encore en vie
    # en même temps (cas qui ne se produit pas ici : un seul process sert `/v1/previews`).
    _seq: itertools.count = field(default_factory=itertools.count, repr=False)

    def __post_init__(self) -> None:
        self.directory = Path(self.directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def _bin_path(self, sha: str) -> Path:
        return self.directory / f"{sha}.bin"

    def _meta_path(self, sha: str) -> Path:
        return self.directory / f"{sha}.json"

    def _write_meta(self, sha: str, content_type: str, size: int) -> None:
        self._meta_path(sha).write_text(
            json.dumps({"content_type": content_type, "size": size, "fetched_at": time.time(),
                       "seq": next(self._seq)}),
            encoding="utf-8",
        )

    def put(self, data: bytes, content_type: str) -> str:
        sha = hashlib.sha256(data).hexdigest()
        with self._lock:
            bin_path = self._bin_path(sha)
            if not bin_path.exists():
                bin_path.write_bytes(data)
            self._write_meta(sha, content_type, len(data))  # neuf ou ré-écrit : fait remonter l'entrée
            self._evict_if_needed()
        return sha

    def get(self, sha: str) -> tuple[bytes, str] | None:
        if not is_valid_sha256(sha):
            return None
        bin_path, meta_path = self._bin_path(sha), self._meta_path(sha)
        try:
            data = bin_path.read_bytes()
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data, str(meta.get("content_type") or "application/octet-stream")

    def _read_seq(self, bin_path: Path) -> float:
        try:
            meta = json.loads((self.directory / f"{bin_path.stem}.json").read_text(encoding="utf-8"))
            if "seq" in meta:
                return float(meta["seq"])
        except (OSError, json.JSONDecodeError, ValueError):
            pass
        return bin_path.stat().st_mtime  # repli : entrée sans `seq` (format antérieur)

    def _evict_if_needed(self) -> None:
        entries = sorted(self.directory.glob("*.bin"), key=self._read_seq)
        total = sum(p.stat().st_size for p in entries)
        i = 0
        while total > self.max_bytes and i < len(entries):
            victim = entries[i]
            try:
                size = victim.stat().st_size
                victim.unlink(missing_ok=True)
                (self.directory / f"{victim.stem}.json").unlink(missing_ok=True)
                total -= size
            except OSError:
                pass
            i += 1


__all__ = ["IMG_SIGN_TTL_S", "ImageDiskCache", "TtlCache", "is_valid_sha256", "sign_image", "verify_image_signature"]
