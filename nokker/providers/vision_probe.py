"""Vision `auto` d'un palier (docs/LOKKAL.md §7.6) : le modèle lit-il vraiment les images ?

Pour un point d'accès de l'utilisateur ou un modèle local, on ne sait pas d'avance. `vision: auto` pose ici une SONDE
unique : au premier tour qui porte une image (jamais en tâche de fond, jamais sans image), une petite PNG d'une couleur
tirée au hasard part au modèle avec la question « de quelle couleur ? ». Il voit s'il nomme CETTE couleur et aucune
autre ; il ne voit pas s'il en nomme une autre, plusieurs ou aucune, ou si l'amont refuse la requête (4xx). Le
résultat certain est gardé dans l'état local (`<état>/vision-probes.json`, par point d'accès et modèle, jamais la clé) et
resservi ensuite, redémarrage compris. Sans certitude (réseau, 5xx, 429, authentification) : texte seul pour ce tour,
rien n'est gardé, pas de nouvel essai avant `RETRY_AFTER_S`.
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import struct
import threading
import time
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("nokker.providers.vision_probe")

SESSION_KEY = "nokker-vision-probe"
RETRY_AFTER_S = 600.0
MAX_TOKENS = 64
# Couleur → pixel ; et les mots qui la nomment (anglais, français).
COLORS: dict[str, tuple[int, int, int]] = {"red": (255, 0, 0), "green": (0, 160, 0), "blue": (0, 0, 255),
                                           "yellow": (255, 230, 0), "black": (0, 0, 0)}
WORDS: dict[str, tuple[str, ...]] = {"red": ("red", "rouge"), "green": ("green", "vert", "verte"),
                                     "blue": ("blue", "bleu", "bleue"), "yellow": ("yellow", "jaune"),
                                     "black": ("black", "noir", "noire")}
PROMPT = ("What is the colour of this square image? Answer with one English word.")
_DEFINITIVE = re.compile(r"\bHTTP (400|404|413|415|422)\b")

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()
_file_guard = threading.Lock()


def probe_image(rng: random.Random | None = None) -> tuple[str, bytes]:
    """(couleur, PNG 8×8 de cette couleur), tirée au hasard : un modèle qui devine sans voir a une chance sur 5."""
    color = (rng or random).choice(sorted(COLORS))
    w = h = 8
    raw = b"".join(b"\x00" + bytes(COLORS[color]) * w for _ in range(h))

    def chunk(t: bytes, d: bytes) -> bytes:
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    return color, (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                   + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def names_color(reply: str, color: str) -> bool:
    """La réponse nomme `color`, et aucune autre couleur de la liste."""
    text = (reply or "").lower()
    named = {c for c, words in WORDS.items() if any(re.search(rf"\b{w}\b", text) for w in words)}
    return named == {color}


class VisionProbe:
    """La sonde d'UN point d'accès et modèle. `check(provider)` → (vision, sondé à l'instant)."""

    def __init__(self, base_url: str, model: str, path: str | os.PathLike[str] | None = None) -> None:
        self.key = f"{(base_url or '').rstrip('/')}|{model}"
        self.model = model
        self.path = Path(path) if path is not None else None
        self._known: bool | None = None
        self._failed_at: float | None = None
        self.last_reason = ""

    def _lock(self) -> threading.Lock:
        with _locks_guard:
            return _locks.setdefault(self.key, threading.Lock())

    def _read(self) -> dict[str, Any]:
        if self.path is None:
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def known(self) -> bool | None:
        if self._known is None:
            entry = self._read().get(self.key)
            if isinstance(entry, dict) and isinstance(entry.get("vision"), bool):
                self._known = entry["vision"]
        return self._known

    def _remember(self, vision: bool, reason: str) -> None:
        self._known = vision
        if self.path is None:
            return
        try:
            with _file_guard:
                data = self._read()
                data[self.key] = {"vision": vision, "model": self.model, "reason": reason[:200],
                                  "checked_at": datetime.now(timezone.utc).isoformat()}
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
                os.replace(tmp, self.path)
        except OSError:
            logger.warning("résultat de la sonde de vision non gardé (%s)", self.path, exc_info=True)

    def check(self, provider: Any) -> tuple[bool, bool]:
        """Vision connue → elle, sans appel. Sinon UNE sonde (un seul appel à la fois par point d'accès et modèle)."""
        known = self.known()
        if known is not None:
            return known, False
        with self._lock():
            known = self.known()
            if known is not None:
                return known, False
            if self._failed_at is not None and time.monotonic() - self._failed_at < RETRY_AFTER_S:
                return False, False
            return self._probe(provider), True

    def _probe(self, provider: Any) -> bool:
        import base64

        color, data = probe_image()
        url = "data:image/png;base64," + base64.b64encode(data).decode("ascii")
        messages = [{"role": "user", "content": [{"type": "text", "text": PROMPT},
                                                 {"type": "image_url", "image_url": {"url": url}}]}]
        try:
            out = provider.complete(messages, [], reasoning=False, session_key=SESSION_KEY, max_tokens=MAX_TOKENS)
        except Exception as e:  # noqa: BLE001 — une sonde ne casse jamais un tour : texte seul
            text = str(e)
            if _DEFINITIVE.search(text):
                self.last_reason = f"l'amont refuse les images ({text[:120]})"
                self._remember(False, self.last_reason)
            else:
                self._failed_at = time.monotonic()
                self.last_reason = f"sonde sans réponse, texte seul pour l'instant ({type(e).__name__}: {text[:120]})"
            logger.info("sonde de vision %s : %s", self.key, self.last_reason)
            return False
        reply = str(getattr(getattr(out, "message", None), "content", "") or "")
        ok = names_color(reply, color)
        self.last_reason = (f"image {color} reconnue" if ok else f"image {color}, réponse « {reply[:80]} »")
        self._remember(ok, self.last_reason)
        logger.info("sonde de vision %s : %s", self.key, self.last_reason)
        return ok


__all__ = ["COLORS", "RETRY_AFTER_S", "SESSION_KEY", "VisionProbe", "names_color", "probe_image"]
