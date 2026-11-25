"""Rendu compact d'un snapshot agent-desktop avant le modèle (docs/DESKTOP.md §2.3) — pur, sans dépendance.

Une ligne par élément utile, indentée par profondeur : `eN rôle « nom » [états] = valeur`. Les refs perdent leur
préfixe de snapshot (`@s3fe…:e12` → `e12`, le `snap_id` est donné une fois en tête). Retirés avant le comptage : les
sous-arbres `hidden` (doublons cachés de GTK3 et LibreOffice relevés au spike) et les conteneurs sans nom ni ref (leurs
enfants remontent). Un champ secret n'a jamais de valeur. On ne coupe jamais en silence : le rendu dit combien
d'éléments il montre sur combien, et l'appelant décide (palier `--skeleton`, puis refus)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from nokker.desktop.policy import is_secret_field

VALUE_MAX = 200
NAME_MAX = 120
SHOWN_STATES = ("focused", "checked", "selected", "disabled", "expanded", "collapsed", "secure", "offscreen",
                "modal", "editable", "required", "invalid")
_REF = re.compile(r"^@([A-Za-z0-9]{4,40}):(e[0-9]{1,6})$")
_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f  ]+")


def short_ref(ref_id: object) -> tuple[str, str] | None:
    """(`snap_id`, `eN`) d'une ref amont qualifiée, ou None."""
    m = _REF.match(str(ref_id or ""))
    return (m.group(1), m.group(2)) if m else None


def one_line(value: object, limit: int) -> str:
    """Sur une ligne, sans caractère de contrôle, bornée (… au bout si coupée)."""
    s = _CTRL.sub(" ", str(value if value is not None else "")).strip()
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


@dataclass
class Element:
    ref: str            # eN
    role: str
    name: str
    states: tuple[str, ...]
    secret: bool


@dataclass
class Rendered:
    snap_id: str
    text: str           # lignes rendues (avant masquage et clôture : c'est le nœud qui masque)
    shown: int          # éléments à ref rendus
    total: int          # éléments à ref visibles (hors sous-arbres cachés)
    cut: bool           # vrai si `shown < total` (plafond atteint)
    skeleton: bool
    refs: dict[str, Element] = field(default_factory=dict)  # ref → élément, pour le contrôle `expect` du nœud


def render(envelope: dict[str, Any], cap_bytes: int, *, skeleton: bool = False) -> Rendered:
    """Rend `data.tree` d'une enveloppe `snapshot`. Les lignes s'arrêtent au plafond (en octets UTF-8) ; `refs` garde
    TOUS les éléments visibles (le nœud contrôle une action même sur un élément qui n'a pas tenu dans le plafond)."""
    data = envelope.get("data") or {}
    snap_id = str(data.get("snapshot_id") or "")
    lines: list[str] = []
    refs: dict[str, Element] = {}
    size = 0
    shown = 0
    cut = False

    def walk(node: dict[str, Any], depth: int) -> None:
        nonlocal size, shown, cut
        states = tuple(str(s).lower() for s in node.get("states") or ())
        if "hidden" in states:
            return
        role = one_line(node.get("role"), 40)
        name = one_line(node.get("name"), NAME_MAX)
        sr = short_ref(node.get("ref_id"))
        next_depth = depth
        if sr is not None or name:
            secret = is_secret_field(role, name, states)
            parts = [sr[1]] if sr else []
            parts.append(role or "?")
            if name:
                parts.append(f"« {name} »")
            st = [s for s in states if s in SHOWN_STATES]
            if secret and "secure" not in st:
                st.append("secure")
            if st:
                parts.append("[" + " ".join(st) + "]")
            if node.get("children_count") and not node.get("children"):
                parts.append(f"(+{int(node['children_count'])})")
            value = node.get("value")
            if not secret and value not in (None, "") and not isinstance(value, (dict, list)):
                parts.append("= " + one_line(value, VALUE_MAX))
            line = "  " * depth + " ".join(parts)
            if sr is not None:
                refs[sr[1]] = Element(ref=sr[1], role=role, name=name, states=states, secret=secret)
            n = len(line.encode("utf-8")) + 1
            if not cut and size + n <= cap_bytes:
                lines.append(line)
                size += n
                if sr is not None:
                    shown += 1
            else:
                cut = True
            next_depth = depth + 1
        for child in node.get("children") or ():
            if isinstance(child, dict):
                walk(child, next_depth)

    tree = data.get("tree")
    if isinstance(tree, dict):
        walk(tree, 0)
    return Rendered(snap_id=snap_id, text="\n".join(lines), shown=shown, total=len(refs), cut=cut,
                    skeleton=skeleton, refs=refs)


__all__ = ["Element", "Rendered", "one_line", "render", "short_ref"]
