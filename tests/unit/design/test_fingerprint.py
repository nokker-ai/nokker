"""Empreinte des textes de V8 vus par le modèle, figée à la relecture : pack design (tous ses fichiers), description et
paramètres de `design_check`, et nokker/design/floor.py (messages des constats). Un changement doit repasser par la
relecture, puis mettre à jour FROZEN."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from nokker.pages.tools import _CHECK_SPEC

ROOT = Path(__file__).resolve().parents[3]
FROZEN = "926da289bfff3264e66b8be714094fb327673f2e39595ebdfeba14ab45054244"


def fingerprint() -> str:
    h = hashlib.sha256()
    pack = ROOT / "nokker" / "packs" / "design"
    for p in sorted(x for x in pack.rglob("*") if x.is_file() and "__pycache__" not in x.parts):
        h.update(p.relative_to(ROOT).as_posix().encode() + b"\0" + p.read_bytes() + b"\0")
    h.update(_CHECK_SPEC.description.encode() + b"\0")
    h.update(json.dumps(_CHECK_SPEC.parameters, sort_keys=True, ensure_ascii=False).encode() + b"\0")
    h.update((ROOT / "nokker" / "design" / "floor.py").read_bytes())
    return h.hexdigest()


def test_reviewed_texts_are_frozen():
    assert fingerprint() == FROZEN, f"textes V8 changés : relecture, puis FROZEN = {fingerprint()!r}"
