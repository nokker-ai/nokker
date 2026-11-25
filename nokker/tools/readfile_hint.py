"""Indice de chemins quand `read_file` (et `patch` en mode replace) vise un fichier qui n'existe pas (`tools/code.py`).

Mesuré sur Nina 70B (acte 4b, 5 fois sur 5) : `search_files(".")`, puis `read_file` sur un chemin INVENTÉ (`tests/test_main.py`, `tests/test.js`), erreur,
et le modèle s'arrête. L'amont renvoie `similar_files` (5 noms proches DANS LE MÊME DOSSIER), vide quand le dossier n'existe pas : c'est le cas ici. Cette
aide ajoute à l'erreur une liste BORNÉE des entrées réelles : le dossier du chemin demandé s'il existe, sinon la racine du dossier de travail.

Bornes : 20 noms au plus, tri stable (alphabétique), dossiers suffixés « / », jamais hors du dossier de travail (le chemin a déjà été confiné par
`CodeToolSource._inside`), pas de fichiers cachés (point), ignorés (`node_modules`, `__pycache__`…) ni secrets (`.env*`, clés, jetons, identifiants). Les noms
passent par `qa/untrusted.inline` (un nom de fichier est un contenu de tiers).

TEXTES VUS PAR LE MODÈLE (à relire et geler) : `HINT_SAME_DIR`, `HINT_ROOT`, `MORE`, `EMPTY`, `SEPARATOR`."""
from __future__ import annotations

import fnmatch

from nokker.core import model_texts as mt
from nokker.qa.untrusted import inline

MAX_NAMES = 20
HINT_SAME_DIR = mt.HINT_SAME_DIR["fr"]  # figés ; langue du tour : `model_texts`
HINT_ROOT = mt.HINT_ROOT["fr"]
MORE = mt.HINT_MORE["fr"]
EMPTY = mt.HINT_EMPTY["fr"]
SEPARATOR = ", "

_IGNORED = ("node_modules", "__pycache__", "venv", "env", "dist", "build", "target", "vendor")
_SECRET = ("*.pem", "*.key", "*.p12", "*.pfx", "*.keystore", "id_rsa*", "id_ed25519*", "*secret*", "*token*", "*credential*", "*password*", "*.kdbx")


def visible(names: list[str]) -> list[str]:
    """Filtre (cachés, ignorés, secrets) puis tri stable. Les dossiers portent déjà leur « / » final."""
    out = []
    for n in names:
        base = n.rstrip("/")
        low = base.lower()
        if not base or base.startswith(".") or base in _IGNORED or low.endswith((".pyc", ".pyo")):
            continue
        if any(fnmatch.fnmatch(low, p) for p in _SECRET):
            continue
        out.append(n)
    return sorted(out)


def hint_text(names: list[str], *, same_dir: bool, where: str) -> str:
    """Le texte d'aide. `names` = entrées réelles déjà filtrées par `visible` (vide : « (aucune) »)."""
    shown = [inline(n, 80) for n in names[:MAX_NAMES]]
    listing = SEPARATOR.join(shown) if shown else mt.pick(mt.HINT_EMPTY)
    if len(names) > MAX_NAMES:
        listing += mt.pick(mt.HINT_MORE).format(n=len(names) - MAX_NAMES)
    return mt.pick(mt.HINT_SAME_DIR if same_dir else mt.HINT_ROOT).format(where=inline(where, 80), names=listing)


__all__ = ["EMPTY", "HINT_ROOT", "HINT_SAME_DIR", "MAX_NAMES", "MORE", "SEPARATOR", "hint_text", "visible"]
