"""Écriture en direct (lot X22, 25.10.2026) : les arguments de `write_file`, `patch` et `page_create` lus pendant que le
modèle les écrit, pour que le client voie le fichier se remplir (docs/BUBBLES.md §6 et §10.7).

Le provider remet les morceaux BRUTS des `arguments` (`on_tool_delta(index, call_id, name, morceau)`,
`providers/sokkan.py`). Ici :

- **JSON partiel** (`PartialObject`) : un objet plat lu caractère par caractère, échappements compris (`\\n`, `\\uXXXX`,
  paires de substitution coupées entre deux morceaux) ; le chemin sort dès que sa chaîne est fermée, le texte du contenu
  (ou du patch) sort au fil de l'eau ;
- **caviardage** : le MÊME scrubber (`Invariants.scrub_output`) et les MÊMES règles que la réponse
  (`core/stream.py::ScrubbedDeltas` : retenue `holdback_for`, mot entier, bloc PEM ouvert, divergence) — un secret
  n'apparaît jamais, même à moitié. Seule différence : on caviarde une FENÊTRE glissante (ce qui reste à émettre +
  `holdback` de contexte déjà émis, au moins 256 caractères) au lieu du cumul — sur un fichier entier, recaviarder le
  cumul est quadratique (mesuré : 10 s de CPU pour 200 Ko). Une coupe n'est émise que si le caviardage de la fenêtre
  prolongée jusqu'au bout du reçu ne la change pas (aucun motif à cheval sur la coupe). La retenue n'est lâchée qu'à la
  fin de l'appel modèle (`finish`), quand les arguments sont complets ;
- **débit borné** : un événement par appel toutes les `every_s` (100 ms) ou dès `every_chars` (2 Ko) de texte en attente ;
- **plafond** : au-delà de `max_chars` de texte, un dernier événement `truncated` et plus rien pour cet appel.

Rien n'est écrit nulle part : on MONTRE ce que le modèle propose ; l'outil s'exécute (ou attend sa carte) par le chemin
normal, et son `tool.result` clôt le bloc côté client. Aucune exception ne sort d'ici : une lecture en panne coupe le
direct, jamais l'appel modèle.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from typing import Any

from nokker.core.stream import DEFAULT_HOLDBACK

logger = logging.getLogger("nokker.core.live_input")

# Outil → champs texte suivis en direct (le premier rencontré est LE champ du bloc). `page_create` n'a qu'un chemin.
LIVE_TOOLS: dict[str, tuple[str, ...]] = {"write_file": ("content",), "patch": ("new_string", "patch"),
                                          "page_create": ()}
EVERY_S = 0.1
EVERY_CHARS = 2048
MAX_CHARS = 200_000
PATH_CHARS = 512
# Un pas de caviardage (fenêtre) après un huitième de ce qui est déjà reçu, entre ces deux bornes : fluide sur un petit
# fichier, peu de pas sur un gros.
SCRUB_STEP_MIN, SCRUB_STEP_MAX = 64, 2048
_STR_RUN = re.compile(r'[^"\\]+')
_V4A_FILE = re.compile(r"^\*\*\* (?:Update|Add) File: (.+)$", re.MULTILINE)
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "/": "/", "\\": "\\", '"': '"'}


class PartialObject:
    """Lecteur incrémental d'un objet JSON plat : `on_text(clé, morceau)` pour les chaînes des clés `streamed` (au fil
    de l'eau), `on_string(clé, valeur)` pour les autres chaînes une fois fermées (bornées à `PATH_CHARS`). Valeurs non
    chaînes (nombres, booléens, objets imbriqués) sautées. Un JSON qui ne commence pas par `{` : plus rien (`broken`)."""

    def __init__(self, streamed: set[str], on_text: Callable[[str, str], None],
                 on_string: Callable[[str, str], None]) -> None:
        self.streamed, self.on_text, self.on_string = streamed, on_text, on_string
        self.state = "start"
        self.key = ""
        self._buf: list[str] = []  # clé en cours, ou valeur courte (non suivie) en cours
        self._buf_len = 0
        self._esc = False
        self._uhex: str | None = None
        self._hi: int | None = None  # première moitié d'une paire de substitution en attente
        self._depth = 0  # valeur non chaîne imbriquée
        self._nested_str = False
        self._nested_esc = False

    @property
    def broken(self) -> bool:
        return self.state == "broken"

    # ── chaînes ──
    def _decode(self, c: str) -> str:
        """Un caractère d'une chaîne JSON (hors guillemet fermant) → texte décodé ('' tant qu'un échappement est ouvert)."""
        if self._uhex is not None:
            self._uhex += c
            if len(self._uhex) < 4:
                return ""
            try:
                cp = int(self._uhex, 16)
            except ValueError:
                cp = 0xFFFD
            self._uhex = None
            if 0xD800 <= cp < 0xDC00:
                pending = "�" if self._hi is not None else ""
                self._hi = cp
                return pending
            if 0xDC00 <= cp < 0xE000:
                if self._hi is None:
                    return "�"
                hi, self._hi = self._hi, None
                return chr(0x10000 + ((hi - 0xD800) << 10) + (cp - 0xDC00))
            return self._lone() + chr(cp)
        if self._esc:
            self._esc = False
            if c == "u":
                self._uhex = ""
                return ""
            return self._lone() + _ESCAPES.get(c, c)
        return self._lone() + c

    def _lone(self) -> str:
        """Une moitié haute restée seule (le caractère suivant n'est pas sa moitié basse) : U+FFFD, jamais une chaîne
        Python invalide envoyée au client."""
        if self._hi is None:
            return ""
        self._hi = None
        return "�"

    def _string_chunk(self, s: str, i: int, sink: Callable[[str], None]) -> tuple[int, bool]:
        """Lit une chaîne depuis `s[i]` ; rend (position, fermée ?). Les suites sans échappement passent d'un bloc."""
        n = len(s)
        while i < n:
            if self._esc or self._uhex is not None:
                out = self._decode(s[i])
                i += 1
                if out:
                    sink(out)
                continue
            c = s[i]
            if c == '"':
                tail = self._lone()
                if tail:
                    sink(tail)
                return i + 1, True
            if c == "\\":
                self._esc = True
                i += 1
                continue
            m = _STR_RUN.match(s, i)
            run = m.group(0)
            tail = self._lone()
            sink(tail + run if tail else run)
            i = m.end()
        return i, False

    def _keep(self, piece: str) -> None:
        if self._buf_len < PATH_CHARS:
            self._buf.append(piece)
            self._buf_len += len(piece)

    def _stream(self, piece: str) -> None:
        self.on_text(self.key, piece)

    # ── objet ──
    def feed(self, s: str) -> None:
        i, n = 0, len(s)
        while i < n:
            st = self.state
            c = s[i]
            if st in ("broken", "end"):
                return
            if st == "start":
                if c == "{":
                    self.state = "key_or_end"
                elif not c.isspace():
                    self.state = "broken"
                i += 1
            elif st == "key_or_end":
                if c == '"':
                    self.state, self._buf, self._buf_len = "key", [], 0
                elif c == "}":
                    self.state = "end"
                elif not (c.isspace() or c == ","):
                    self.state = "broken"
                i += 1
            elif st == "key":
                i, closed = self._string_chunk(s, i, self._keep)
                if closed:
                    self.key = "".join(self._buf)
                    self.state = "colon"
            elif st == "colon":
                if c == ":":
                    self.state = "value"
                elif not c.isspace():
                    self.state = "broken"
                i += 1
            elif st == "value":
                if c.isspace():
                    i += 1
                elif c == '"':
                    self.state = "text" if self.key in self.streamed else "short"
                    self._buf, self._buf_len = [], 0
                    i += 1
                else:
                    self.state, self._depth, self._nested_str, self._nested_esc = "other", 0, False, False
            elif st == "text":
                i, closed = self._string_chunk(s, i, self._stream)
                if closed:
                    self.state = "after"
            elif st == "short":
                i, closed = self._string_chunk(s, i, self._keep)
                if closed:
                    self.on_string(self.key, "".join(self._buf))
                    self.state = "after"
            elif st == "after":
                if c == ",":
                    self.state = "key_or_end"
                elif c == "}":
                    self.state = "end"
                elif not c.isspace():
                    self.state = "broken"
                i += 1
            elif st == "other":
                i = self._skip_other(s, i)
        return

    def _skip_other(self, s: str, i: int) -> int:
        """Valeur non chaîne : un scalaire jusqu'à `,`/`}`, ou un objet/tableau imbriqué jusqu'à sa fermeture."""
        n = len(s)
        while i < n:
            c = s[i]
            if self._nested_str:
                if self._nested_esc:
                    self._nested_esc = False
                elif c == "\\":
                    self._nested_esc = True
                elif c == '"':
                    self._nested_str = False
            elif c == '"':
                self._nested_str = True
            elif c in "{[":
                self._depth += 1
            elif c in "}]":
                if self._depth == 0:  # fin de l'objet de premier niveau après un scalaire
                    self.state = "end"
                    return i + 1
                self._depth -= 1
                if self._depth == 0:
                    self.state = "after"
                    return i + 1
            elif c == "," and self._depth == 0:
                self.state = "key_or_end"
                return i + 1
            i += 1
        return i


class WindowedScrub:
    """Le caviardage avec retenue de `ScrubbedDeltas`, sur une fenêtre glissante (voir la docstring du module).

    `c` = caractères bruts déjà émis. À chaque pas : coupe `r` ≤ reçu − retenue, reculée jusqu'au dernier blanc et avant
    un `-----BEGIN` sans `-----END` ; fenêtre depuis `c − ctx`. On émet `scrub(fenêtre[:r])` privé de
    `scrub(fenêtre[:c])` seulement si (1) ce dernier en est bien le début (sinon une partie émise aurait été réécrite :
    plus rien, `dead`) et (2) `scrub(fenêtre jusqu'au bout)` commence par lui (sinon un motif est à cheval sur la coupe :
    on attend)."""

    def __init__(self, scrub: Callable[[str], str], sink: Callable[[str], None], holdback: int) -> None:
        self._scrub, self._sink, self.holdback = scrub, sink, holdback
        self.ctx = max(holdback, 256)
        self.raw = ""
        self._parts: list[str] = []
        self._n = 0  # longueur brute reçue (parties comprises)
        self._at = 0  # longueur brute au dernier pas
        self.c = 0
        self.min_step = SCRUB_STEP_MIN
        self.dead = False

    def feed(self, piece: str) -> None:
        self._parts.append(piece)
        self._n += len(piece)
        if self.dead or self._n - self._at < self.min_step:
            return
        self._step(final=False)

    def flush(self) -> None:
        if not self.dead:
            self._step(final=True)

    def _step(self, final: bool) -> None:
        if self._parts:
            self.raw += "".join(self._parts)
            self._parts = []
        raw, c = self.raw, self.c
        self._at = len(raw)
        r = len(raw) if final else len(raw) - self.holdback
        pem = raw.rfind("-----BEGIN", c)
        if pem != -1 and raw.find("-----END", pem) == -1:
            r = min(r, pem)
        if not final:
            while r > c and not raw[r - 1].isspace():  # jamais un jeton à moitié
                r -= 1
        if r <= c:
            return
        ws = max(0, c - self.ctx)
        pre = self._scrub(raw[ws:c]) if c > ws else ""
        cut = self._scrub(raw[ws:r])
        if not cut.startswith(pre):
            self.dead = True  # le caviardage réécrit du texte déjà émis : plus rien pour cet appel
            return
        if not final and r < len(raw) and not self._scrub(raw[ws:]).startswith(cut):
            return  # un motif à cheval sur la coupe : on attend la suite
        self.c = r
        out = cut[len(pre):]
        if out:
            self._sink(out)


class LiveCall:
    """Le direct d'UN appel d'outil (un `index` du flux)."""

    def __init__(self, owner: LiveToolInputs, name: str) -> None:
        self.owner, self.name = owner, name
        self.call_id = ""
        self.seq = 0
        self.path: str | None = None
        self.field: str | None = None
        self.pending = ""
        self.fed = 0
        self.ended = False
        self._announced_path: str | None = None
        self._last = float("-inf")
        self._v4a_line = ""  # fin de ligne pas encore close (en-têtes `*** Update File:` du mode patch)
        self.text = WindowedScrub(owner.scrub, self.safe, owner.holdback)
        self.parser = PartialObject(set(LIVE_TOOLS.get(name, ())), self._on_text, self._on_string)

    # ── entrées du lecteur ──
    def _on_string(self, key: str, value: str) -> None:
        if key == "path" and value.strip():
            self.path = self.owner.scrub(value.strip())[:PATH_CHARS]

    def _on_text(self, key: str, piece: str) -> None:
        if self.ended:
            return
        if self.field is None:
            self.field = key
        if key != self.field:
            return
        self.fed += len(piece)
        if self.fed > self.owner.max_chars:
            self._end(truncated=True)
            return
        self.text.min_step = min(SCRUB_STEP_MAX, max(SCRUB_STEP_MIN, self.fed // 8))
        self.text.feed(piece)
        if self.text.dead:  # le caviardage a réécrit du texte déjà parti : plus de direct pour cet appel
            self._end(truncated=True)

    def safe(self, text: str) -> None:
        self.pending += text
        if self.field == "patch":  # V4A : le fichier du dernier en-tête vu
            lines = (self._v4a_line + text).split("\n")
            self._v4a_line = lines.pop()
            for line in lines:
                m = _V4A_FILE.match(line)
                if m:
                    self.path = m.group(1).strip()[:PATH_CHARS]

    # ── sorties ──
    def feed(self, piece: str) -> None:
        if self.ended:
            return
        self.parser.feed(piece)
        if self.parser.broken:
            self._end(truncated=self.seq > 0)
            return
        self._maybe_emit()

    def _maybe_emit(self, force: bool = False) -> None:
        if self.ended or not self.call_id:
            return
        now = self.owner.clock()
        due = bool(self.pending) and (force or len(self.pending) >= self.owner.every_chars
                                      or now - self._last >= self.owner.every_s)
        if due or (self.path is not None and self.path != self._announced_path and now - self._last >= self.owner.every_s):
            self._emit(self.pending)
            self.pending = ""
            self._last = now

    def _emit(self, text: str, **flags: bool) -> None:
        data: dict[str, Any] = {"call_id": self.call_id, "name": self.name, "seq": self.seq, "text": text}
        if self.path is not None:
            data["path"] = self.path
            self._announced_path = self.path
        if self.field is not None:
            data["field"] = self.field
        data.update({k: True for k, v in flags.items() if v})
        self.seq += 1
        self.owner.emit(data)

    def _end(self, truncated: bool = False) -> None:
        if self.ended:
            return
        if self.call_id and (truncated or self.pending):
            self._emit(self.pending, truncated=truncated)
            self.pending = ""
        self.ended = True

    def finish(self) -> None:
        """Les arguments sont complets : la retenue du caviardage sort, puis le reste en attente."""
        if self.ended:
            return
        self.text.flush()
        if self.text.dead:
            self._end(truncated=True)
            return
        self._maybe_emit(force=True)

    def discard(self) -> None:
        if self.seq > 0 and self.call_id:
            self.pending = ""
            self._emit("", discarded=True)
        self.ended = True


class LiveToolInputs:
    """Le direct des appels d'outils d'UN appel modèle : `feed` = `on_tool_delta` du provider, `finish` quand l'appel
    modèle a rendu sa `Completion`, `discard` s'il est abandonné (flux coupé, rejoué sans streaming)."""

    def __init__(self, scrub: Callable[[str], str], emit: Callable[[dict[str, Any]], None],
                 holdback: int = DEFAULT_HOLDBACK, *, clock: Callable[[], float] = time.monotonic,
                 every_s: float = EVERY_S, every_chars: int = EVERY_CHARS, max_chars: int = MAX_CHARS) -> None:
        self.scrub, self._emit, self.holdback, self.clock = scrub, emit, holdback, clock
        self.every_s, self.every_chars, self.max_chars = every_s, every_chars, max_chars
        self.calls: dict[int, LiveCall | None] = {}
        self._lock = threading.Lock()
        self.dead = False

    def emit(self, data: dict[str, Any]) -> None:
        try:
            self._emit(data)
        except Exception:  # noqa: BLE001 — un observateur en panne coupe le direct, pas l'appel modèle
            logger.exception("tool.input.delta non publié")

    def feed(self, index: int, call_id: str, name: str, piece: str) -> None:
        if self.dead or not piece:
            return
        try:
            with self._lock:
                if index not in self.calls:
                    self.calls[index] = LiveCall(self, name) if name in LIVE_TOOLS else None
                call = self.calls[index]
                if call is None:
                    return
                if call_id and not call.call_id:
                    call.call_id = call_id
                call.feed(piece)
        except Exception:  # noqa: BLE001 — jamais une exception dans le flux du provider
            logger.exception("lecture en direct des arguments en échec")
            self.dead = True

    def _each(self, method: str) -> None:
        try:
            with self._lock:
                for call in self.calls.values():
                    if call is not None:
                        getattr(call, method)()
        except Exception:  # noqa: BLE001
            logger.exception("direct des arguments : %s en échec", method)
        self.dead = True

    def finish(self) -> None:
        if not self.dead:
            self._each("finish")

    def discard(self) -> None:
        dead, self.dead = self.dead, False
        self._each("discard")
        self.dead = dead or self.dead


__all__ = ["LIVE_TOOLS", "LiveToolInputs", "PartialObject"]
