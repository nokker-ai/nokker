"""Document CV (`cv_document.v1`, docs/JOBS-PACK.md §13.6) : édition structurée, diff, faits nouveaux, contrôle du titulaire.

Tout est déterministe et sans réseau. L'édition ne prend que des opérations sur des chemins du document
(`experience[2].bullets[1]`) ; le texte de l'offre n'y entre jamais.
"""
from __future__ import annotations

import copy
import re
import unicodedata
from typing import Any

_SEG = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)((?:\[\d+\])*)")
_IDX = re.compile(r"\[(\d+)\]")
MAX_OPS = 40


class EditError(ValueError):
    """`missing` : la section ou l'élément visé n'existe pas dans le document."""

    def __init__(self, message: str, missing: bool = False) -> None:
        super().__init__(message)
        self.missing = missing


def _parse(path: str) -> list[str | int]:
    out: list[str | int] = []
    for part in str(path or "").split("."):
        m = _SEG.fullmatch(part.strip())
        if not m:
            raise EditError(f"chemin illisible : {path!r}")
        out.append(m.group(1))
        out.extend(int(i) for i in _IDX.findall(m.group(2)))
    if not out:
        raise EditError("chemin vide")
    return out


def _walk(doc: Any, keys: list[str | int]) -> Any:
    cur = doc
    for k in keys:
        if isinstance(k, int):
            if not isinstance(cur, list) or not 0 <= k < len(cur):
                raise EditError(f"index hors du document : {k}", missing=True)
            cur = cur[k]
        else:
            if not isinstance(cur, dict) or k not in cur:
                raise EditError(f"champ absent du document : {k}", missing=True)
            cur = cur[k]
    return cur


def apply_ops(document: dict[str, Any], ops: list[dict[str, Any]]) -> dict[str, Any]:
    """Nouvelle version du document ; l'original n'est jamais modifié. `set` (champ ou élément existant, ou ajout en
    fin de liste par l'index `len`), `remove` (champ ou élément), `move` (élément de liste vers l'index `to`)."""
    if not isinstance(ops, list) or not ops or len(ops) > MAX_OPS:
        raise EditError(f"de 1 à {MAX_OPS} opérations")
    doc = copy.deepcopy(document)
    for op in ops:
        if not isinstance(op, dict):
            raise EditError("opération illisible")
        kind, keys = op.get("op"), _parse(str(op.get("path") or ""))
        parent, last = _walk(doc, keys[:-1]), keys[-1]
        if kind == "set":
            value = op.get("value")
            if not isinstance(value, (str, int, float, list, dict)) or value in ("",):
                raise EditError("valeur vide ou illisible")
            if isinstance(last, int):
                if not isinstance(parent, list) or not 0 <= last <= len(parent):
                    raise EditError(f"index hors du document : {last}", missing=True)
                if last == len(parent):
                    parent.append(value)
                else:
                    parent[last] = value
            else:
                if not isinstance(parent, dict):
                    raise EditError(f"champ sur une liste : {last}")
                parent[last] = value
        elif kind == "remove":
            _walk(parent, [last])
            if isinstance(last, int):
                parent.pop(last)
            else:
                del parent[last]
        elif kind == "move":
            if not isinstance(last, int) or not isinstance(parent, list):
                raise EditError("move ne vise qu'un élément de liste")
            to = op.get("to")
            if not isinstance(to, int) or not 0 <= to < len(parent):
                raise EditError("move : index `to` hors de la liste")
            item = _walk(parent, [last])
            parent.pop(last)
            parent.insert(to, item)
        else:
            raise EditError(f"opération inconnue : {kind!r}")
    return doc


def flatten(value: Any, prefix: str = "") -> dict[str, str]:
    """Feuilles texte du document, par chemin."""
    out: dict[str, str] = {}
    if isinstance(value, dict):
        for k, v in value.items():
            if k in ("photo_url", "photo"):
                continue
            out.update(flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            out.update(flatten(v, f"{prefix}[{i}]"))
    elif value not in (None, ""):
        out[prefix] = str(value)
    return out


def diff(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """Lignes changées : « + chemin : texte », « - chemin : texte », « ~ chemin : avant → après »."""
    a, b = flatten(before), flatten(after)
    lines = []
    for k in sorted(set(a) | set(b)):
        if k not in b:
            lines.append(f"- {k} : {a[k]}")
        elif k not in a:
            lines.append(f"+ {k} : {b[k]}")
        elif a[k] != b[k]:
            lines.append(f"~ {k} : {a[k]} → {b[k]}")
    return lines


def _fold(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)).lower()


# Un « fait » : nombre (dates, pourcentages, montants), mot à majuscule intérieure ou sigle (AWS, Kubernetes, ESIEA),
# mot avec chiffre (ISO27001). Les mots courants en tête de phrase ne comptent pas.
_TOKEN = re.compile(r"[\w][\w+#./-]*[\w+#%]|\d+%?|\w")
_COMMON = frozenset(_fold(w) for w in (
    "le la les un une des de du et en au aux pour par sur dans avec chez à the a an of and in for with on at to by "
    "je nous vous il elle ils elles madame monsieur bonjour cordialement objet my our your".split()))


def facts(text: str) -> set[str]:
    out = set()
    for tok in _TOKEN.findall(text or ""):
        t = tok.strip(".-/")
        if not t:
            continue
        folded = _fold(t)
        if folded in _COMMON:
            continue
        if any(ch.isdigit() for ch in t) or (t[0].isupper() and len(t) > 1) or t.isupper() and len(t) > 1:
            out.add(folded)
    return out


def _strong(t: str) -> bool:
    """Un fait même en tête de phrase : nombre, sigle, majuscule intérieure (« PostgreSQL »)."""
    return any(ch.isdigit() for ch in t) or (t.isupper() and len(t) > 1) or any(ch.isupper() for ch in t[1:])


def new_facts(text: str, sources: list[str]) -> list[str]:
    """Faits présents dans `text` et absents de toutes les `sources` (CV de base, et texte de l'offre pour une
    lettre). Un mot à simple majuscule en tête de phrase ou de ligne n'est pas un fait (« Votre », « Six ») ; plus loin
    dans la phrase, c'est un nom propre. Montrés à la personne, jamais refusés en silence."""
    known: set[str] = set()
    for s in sources:
        known |= {_fold(t) for t in _TOKEN.findall(s or "")}
    seen, out = set(), []
    text = text or ""
    for m in _TOKEN.finditer(text):
        t = m.group(0).strip(".-/")
        f = _fold(t)
        if not t or f not in facts(t) or f in known or f in seen:
            continue
        before = text[:m.start()].rstrip(" \t\u00a0«\"'(")
        if not _strong(t) and (not before or before[-1] in ".!?:;\n,"):
            continue
        seen.add(f)
        out.append(t)
    return out


def owner_matches(name_on_cv: str, member_name: str) -> bool:
    """Le prénom lu sur le CV (premier mot) figure-t-il dans le nom du membre (partie locale de son adresse) ? Prudent :
    un CV d'enfant au même nom de famille ne passe pas ; un doute coûte une question, jamais un rattachement."""
    words = [w for w in re.split(r"[^a-z]+", _fold(name_on_cv)) if len(w) >= 2]
    member = re.sub(r"[^a-z]", "", _fold(member_name))
    return bool(words) and bool(member) and words[0] in member


__all__ = ["EditError", "apply_ops", "diff", "facts", "flatten", "new_facts", "owner_matches"]
