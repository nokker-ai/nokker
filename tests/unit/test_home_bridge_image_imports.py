"""Image home-bridge (deploy/lokkal/home-bridge/Dockerfile) : chaque module `nokker.*` importé AU NIVEAU MODULE par un
fichier copié dans l'image est lui-même copié. Sinon le contrôle d'import du Dockerfile casse tout build Lokkal (vu le
18.11 : `from nokker import bg_stop` ajouté à homebridge/server.py sans COPY, ImportError au build de rc.9.37).
Statique : AST seulement, sans docker."""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "deploy/lokkal/home-bridge/Dockerfile"


def copied_paths() -> set[Path]:
    """Chemins `nokker/…` copiés par `COPY nokker/<x> /opt/home/nokker/<x>` (fichier ou dossier)."""
    out = set()
    for m in re.finditer(r"^COPY\s+(nokker/\S*)\s+/opt/home/\S+", DOCKERFILE.read_text(encoding="utf-8"), re.M):
        out.add(ROOT / m.group(1).rstrip("/"))
    return out


def copied_files() -> list[Path]:
    files = []
    for p in copied_paths():
        files += sorted(p.rglob("*.py")) if p.is_dir() else [p]
    return files


def is_copied(module: str, copied: set[Path]) -> bool:
    rel = module.split(".")
    cand = [ROOT.joinpath(*rel).with_suffix(".py"), ROOT.joinpath(*rel)]
    return any(c == p or p in c.parents for c in cand for p in copied)


def module_level_imports(path: Path) -> set[str]:
    """Modules `nokker.*` importés au niveau MODULE (hors fonctions et classes ; `if TYPE_CHECKING` exclu)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    mods: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.If) and "TYPE_CHECKING" in ast.unparse(node.test):
            continue
        for n in ([node] if not isinstance(node, (ast.If, ast.Try)) else ast.walk(node)):
            if isinstance(n, ast.Import):
                mods |= {a.name for a in n.names if a.name.startswith("nokker")}
            elif isinstance(n, ast.ImportFrom) and n.level == 0 and (n.module or "").startswith("nokker"):
                if n.module == "nokker":  # from nokker import bg_stop → module nokker.bg_stop (ou attribut du paquet)
                    mods |= {f"nokker.{a.name}" for a in n.names}
                else:
                    mods.add(n.module)
    return mods


def test_dockerfile_copies_something():
    files = copied_files()
    assert any(f.name == "server.py" and f.parent.name == "homebridge" for f in files) and len(files) > 10


def entrypoints() -> set[str]:
    """Modules que l'image charge : le contrôle d'import du Dockerfile (`RUN python -c "import …"`) et le module de
    l'ENTRYPOINT (`python -m <m>`, donc aussi `<m>.__main__`)."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    mods = set()
    for m in re.finditer(r'python -c "import ([^"]+)"', text):
        mods |= {x.strip() for x in m.group(1).split(",") if x.strip().startswith("nokker")}
    for m in re.finditer(r'"-m",\s*"(nokker[\w.]*)"', text):
        mods |= {m.group(1), m.group(1) + ".__main__"}
    return mods


def module_file(mod: str) -> Path | None:
    rel = mod.split(".")
    for c in (ROOT.joinpath(*rel).with_suffix(".py"), ROOT.joinpath(*rel) / "__init__.py"):
        if c.is_file():
            return c
    return None


def test_entrypoints_found():
    assert {"nokker.homebridge.server", "nokker.homebridge"} <= entrypoints()


def test_every_module_level_nokker_import_reached_by_the_image_is_copied():
    """Fermeture des imports de NIVEAU MODULE depuis les points d'entrée de l'image : chaque module `nokker.*`
    atteint doit être copié par le Dockerfile (un import dans une fonction n'est pas suivi : il ne tourne pas au
    chargement, et la passerelle n'appelle pas ces chemins)."""
    copied = copied_paths()
    todo, seen, missing = list(entrypoints()), set(), {}
    while todo:
        mod = todo.pop()
        if mod in seen or mod == "nokker":
            continue
        seen.add(mod)
        parents = [".".join(mod.split(".")[:i]) for i in range(2, len(mod.split(".")))]  # paquets intermédiaires
        todo += [p for p in parents if p not in seen]
        f = module_file(mod)
        if f is None:
            continue  # attribut d'un paquet (from nokker.x import NOM) : couvert par le paquet
        if not is_copied(mod, copied):
            missing.setdefault(mod, []).append("atteint depuis les points d'entrée")
            continue
        todo += sorted(module_level_imports(f) - seen)
    assert not missing, f"modules chargés par l'image home-bridge mais absents de son Dockerfile : {missing}"
