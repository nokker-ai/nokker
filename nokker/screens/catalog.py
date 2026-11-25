"""Catalogue de composants des écrans du mur (lot V7, docs/SCREENS.md).

Le modèle ne produit JAMAIS de HTML, de CSS ni de script : il produit une description (JSON) faite des seuls composants
ci-dessous, que le noyau valide ici puis rend lui-même (`render.py`). Tout ce qui n'est pas dans le catalogue est
refusé, avec un message court qui dit quoi corriger.
"""
from __future__ import annotations

import re
from typing import Any

THEMES = ("dark", "light")
LAYOUTS = ("stack", "columns")
TONES = ("info", "warn")
MAX_BLOCKS = 16          # au premier niveau
MAX_NODES = 40           # en tout, groupes compris
MAX_GROUP = 6
MAX_ITEMS = 10
TITLE_MAX = 60

# type → {champ: (sorte, borne)} ; sorte : str | bool | items | checks | blocks | enum:<valeurs>
FIELDS: dict[str, dict[str, tuple[str, Any]]] = {
    "heading": {"text": ("str", 80)},
    "text": {"text": ("str", 300)},
    "list": {"title": ("str?", 60), "items": ("items", 80)},
    "checklist": {"title": ("str?", 60), "items": ("checks", 80)},
    "stat": {"label": ("str", 40), "value": ("str", 12)},
    "note": {"text": ("str", 200), "tone": ("enum?", TONES)},
    "clock": {},
    "next_event": {},
    "divider": {},
    "group": {"title": ("str?", 60), "blocks": ("blocks", MAX_GROUP)},
}
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f​-‏ -‮⁦-⁩]")


class SpecError(ValueError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def _clean(s: str) -> str:
    return _CTRL.sub("", s).strip()


def validate(spec: Any) -> dict[str, Any]:
    """Description normalisée (champs inconnus refusés, textes nettoyés) ; SpecError avec la liste des problèmes."""
    problems: list[str] = []
    if not isinstance(spec, dict):
        raise SpecError(["l'écran doit être un objet {title, theme?, layout?, blocks}"])
    extra = set(spec) - {"title", "theme", "layout", "blocks"}
    if extra:
        problems.append(f"champs inconnus : {', '.join(sorted(extra))}")
    title = spec.get("title")
    if not isinstance(title, str) or not _clean(title) or len(_clean(title)) > TITLE_MAX:
        problems.append(f"title : texte de 1 à {TITLE_MAX} caractères")
    theme = spec.get("theme", "dark")
    if theme not in THEMES:
        problems.append("theme : dark ou light")
    layout = spec.get("layout", "stack")
    if layout not in LAYOUTS:
        problems.append("layout : stack ou columns")
    count = [0]
    blocks = _blocks(spec.get("blocks"), "blocks", MAX_BLOCKS, problems, count, nested=False)
    if count[0] > MAX_NODES:
        problems.append(f"trop de composants : {count[0]} (au plus {MAX_NODES})")
    if layout == "columns" and blocks and not all(b["type"] == "group" for b in blocks):
        problems.append("layout columns : le premier niveau n'a que des group (une colonne chacun)")
    if layout == "columns" and len(blocks) > 3:
        problems.append("layout columns : trois colonnes au plus")
    if problems:
        raise SpecError(problems[:12])
    return {"title": _clean(title), "theme": theme, "layout": layout, "blocks": blocks}


def _blocks(raw: Any, where: str, limit: int, problems: list[str], count: list[int], nested: bool) -> list[dict]:
    if not isinstance(raw, list) or not raw:
        problems.append(f"{where} : liste de 1 à {limit} composants")
        return []
    if len(raw) > limit:
        problems.append(f"{where} : {len(raw)} composants (au plus {limit})")
    out = []
    for i, b in enumerate(raw[:limit]):
        at = f"{where}[{i}]"
        count[0] += 1
        if not isinstance(b, dict) or b.get("type") not in FIELDS:
            problems.append(f"{at} : type inconnu (permis : {', '.join(FIELDS)})")
            continue
        kind = b["type"]
        if kind == "group" and nested:
            problems.append(f"{at} : un group ne contient pas de group")
            continue
        spec = FIELDS[kind]
        extra = set(b) - set(spec) - {"type"}
        if extra:
            problems.append(f"{at} ({kind}) : champs inconnus : {', '.join(sorted(extra))}")
        norm: dict[str, Any] = {"type": kind}
        for name, (sort, bound) in spec.items():
            v = b.get(name)
            optional = sort.endswith("?")
            sort = sort.rstrip("?")
            if v is None and optional:
                continue
            if sort == "str":
                if not isinstance(v, str) or not _clean(v) or len(_clean(v)) > bound:
                    problems.append(f"{at} ({kind}) : {name} = texte de 1 à {bound} caractères")
                else:
                    norm[name] = _clean(v)
            elif sort == "enum":
                if v not in bound:
                    problems.append(f"{at} ({kind}) : {name} = {' ou '.join(bound)}")
                else:
                    norm[name] = v
            elif sort == "items":
                if (not isinstance(v, list) or not v or len(v) > MAX_ITEMS
                        or not all(isinstance(x, str) and _clean(x) and len(_clean(x)) <= bound for x in v)):
                    problems.append(f"{at} ({kind}) : items = 1 à {MAX_ITEMS} textes de {bound} caractères au plus")
                else:
                    norm[name] = [_clean(x) for x in v]
            elif sort == "checks":
                ok = isinstance(v, list) and 0 < len(v) <= MAX_ITEMS and all(
                    isinstance(x, dict) and set(x) <= {"text", "done"} and isinstance(x.get("text"), str)
                    and _clean(x["text"]) and len(_clean(x["text"])) <= bound and isinstance(x.get("done", False), bool)
                    for x in v)
                if not ok:
                    problems.append(f"{at} ({kind}) : items = 1 à {MAX_ITEMS} {{text, done}} (text de {bound} "
                                    "caractères au plus, done vrai ou faux)")
                else:
                    norm[name] = [{"text": _clean(x["text"]), "done": bool(x.get("done", False))} for x in v]
            elif sort == "blocks":
                norm[name] = _blocks(v, f"{at}.blocks", bound, problems, count, nested=True)
        out.append(norm)
    return out


__all__ = ["FIELDS", "LAYOUTS", "MAX_BLOCKS", "MAX_NODES", "SpecError", "THEMES", "validate"]
