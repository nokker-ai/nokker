"""Lecture d'un diff unifié (git ou `diff -u`) pour `nokker review` (lot X13, docs/LOKKAL.md §7.20).

Le diff est une DONNÉE NON FIABLE (invariant 3) : on le découpe, on le numérote, on ne l'interprète jamais. Chaque
ligne garde son numéro dans l'ancien fichier (côté gauche, `LEFT`) et/ou dans le nouveau (côté droit, `RIGHT`) : ce
sont les seules positions où un commentaire peut être posé (`FileDiff.positions`). Un numéro de ligne qui n'y est pas
est halluciné et jeté avant tout envoi.

Les lignes d'un bloc sont comptées par l'en-tête `@@ -a,b +c,d @@` : une ligne retirée qui commence par « -- » (« ---
x ») reste une ligne du bloc, pas un nouvel en-tête de fichier.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

LEFT, RIGHT = "LEFT", "RIGHT"

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")
_GIT_HEADER = re.compile(r"^diff --git (\"?a/.*?\"?) (\"?b/.*\"?)$")
_ESC = {"n": "\n", "t": "\t", '"': '"', "\\": "\\", "a": "\a", "b": "\b", "f": "\f", "r": "\r", "v": "\v"}


@dataclass
class Line:
    kind: str  # "+" ajoutée, "-" retirée, " " contexte
    text: str
    old: int | None  # numéro dans l'ancien fichier (retirée ou contexte)
    new: int | None  # numéro dans le nouveau fichier (ajoutée ou contexte)


@dataclass
class Hunk:
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    section: str = ""
    lines: list[Line] = field(default_factory=list)


@dataclass
class FileDiff:
    old_path: str | None  # None : fichier ajouté
    new_path: str | None  # None : fichier supprimé
    status: str = "modified"  # added | deleted | renamed | modified
    binary: bool = False
    hunks: list[Hunk] = field(default_factory=list)

    @property
    def path(self) -> str:
        """Chemin d'un commentaire (celui de la PR) : le nouveau, sauf pour un fichier supprimé."""
        return self.new_path or self.old_path or ""

    def positions(self) -> set[tuple[str, int]]:
        """(côté, ligne) commentables : RIGHT = ajoutée ou contexte (numéro du nouveau fichier), LEFT = retirée ou
        contexte (numéro de l'ancien)."""
        out: set[tuple[str, int]] = set()
        for h in self.hunks:
            for ln in h.lines:
                if ln.new is not None:
                    out.add((RIGHT, ln.new))
                if ln.old is not None:
                    out.add((LEFT, ln.old))
        return out

    def added(self) -> list[Line]:
        return [ln for h in self.hunks for ln in h.lines if ln.kind == "+"]


def _unquote(p: str) -> str:
    """Chemin cité par git (`"a/caf\\303\\251.py"`) : échappements C, octets en octal (UTF-8)."""
    p = p.strip()
    if not (len(p) >= 2 and p[0] == p[-1] == '"'):
        return p
    raw = bytearray()
    for i, m in enumerate(re.split(r"(\\[0-7]{3}|\\.)", p[1:-1])):
        if i % 2 == 0:
            raw += m.encode("utf-8", "surrogateescape")
        elif re.fullmatch(r"\\[0-7]{3}", m):
            raw.append(int(m[1:], 8) & 0xFF)
        else:
            raw += _ESC.get(m[1], m[1]).encode("utf-8")
    return raw.decode("utf-8", "replace")


def _strip_prefix(p: str | None, prefix: str) -> str | None:
    if p is None:
        return None
    p = _unquote(p.split("\t", 1)[0] if not p.startswith('"') else p)
    if p == "/dev/null":
        return None
    return p.removeprefix(prefix)


def parse_diff(text: str) -> list[FileDiff]:
    """Les fichiers d'un diff unifié, dans l'ordre. Tolère un diff `git` (en-têtes, renommages, binaires) et un diff
    `diff -u` (seulement `---`/`+++`). Une ligne qu'on ne comprend pas hors d'un bloc est ignorée."""
    files: list[FileDiff] = []
    cur: FileDiff | None = None
    hunk: Hunk | None = None
    left = right = 0  # lignes restantes du bloc en cours (anciennes, nouvelles)
    old_no = new_no = 0
    lines = (text or "").replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines):
        raw = lines[i]
        i += 1
        if hunk is not None and (left > 0 or right > 0):
            tag, body = (raw[:1], raw[1:]) if raw else (" ", "")  # ligne de contexte vide (espace final coupé)
            if tag == "\\":
                continue  # « \ No newline at end of file »
            if tag == "+" and right > 0:
                hunk.lines.append(Line("+", body, None, new_no))
                new_no += 1
                right -= 1
                continue
            if tag == "-" and left > 0:
                hunk.lines.append(Line("-", body, old_no, None))
                old_no += 1
                left -= 1
                continue
            if tag == " " and left > 0 and right > 0:
                hunk.lines.append(Line(" ", body, old_no, new_no))
                old_no += 1
                new_no += 1
                left -= 1
                right -= 1
                continue
            hunk = None  # bloc mal compté : on retombe sur la lecture des en-têtes
            left = right = 0
        if raw.startswith("\\"):
            continue
        m = _GIT_HEADER.match(raw)
        if m:
            cur = FileDiff(old_path=_strip_prefix(m.group(1), "a/"), new_path=_strip_prefix(m.group(2), "b/"))
            files.append(cur)
            hunk = None
            continue
        if raw.startswith("--- ") and i < len(lines) and lines[i].startswith("+++ "):
            old = _strip_prefix(raw[4:], "a/")
            new = _strip_prefix(lines[i][4:], "b/")
            i += 1
            if cur is None or cur.hunks:  # diff -u sans en-tête git : un fichier par paire ---/+++
                cur = FileDiff(old_path=old, new_path=new)
                files.append(cur)
            else:
                cur.old_path, cur.new_path = old, new
            if old is None:
                cur.status = "added"
            elif new is None:
                cur.status = "deleted"
            hunk = None
            continue
        if cur is None:
            continue
        if raw.startswith("new file mode"):
            cur.status, cur.old_path = "added", None
        elif raw.startswith("deleted file mode"):
            cur.status, cur.new_path = "deleted", None
        elif raw.startswith("rename from "):
            cur.status, cur.old_path = "renamed", _unquote(raw[len("rename from "):])
        elif raw.startswith("rename to "):
            cur.status, cur.new_path = "renamed", _unquote(raw[len("rename to "):])
        elif raw.startswith(("Binary files ", "GIT binary patch")):
            cur.binary = True
        else:
            hm = _HUNK.match(raw)
            if hm:
                a, b, c, d = hm.group(1), hm.group(2), hm.group(3), hm.group(4)
                hunk = Hunk(int(a), int(b) if b is not None else 1, int(c), int(d) if d is not None else 1,
                            section=hm.group(5).strip())
                cur.hunks.append(hunk)
                old_no, new_no = hunk.old_start, hunk.new_start
                left, right = hunk.old_len, hunk.new_len
    return [f for f in files if f.path]


__all__ = ["LEFT", "RIGHT", "FileDiff", "Hunk", "Line", "parse_diff"]
