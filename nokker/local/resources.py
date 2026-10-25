"""Inventaire des ressources `local` d'une machine Lokkal (lot X21, docs/LOKKAL.md §6.7 et §7.16), par NOM seulement.

Une session web qui a besoin d'une ressource `local` (un dépôt, un hôte ssh, un connecteur posé dans Lokkal, un
serveur MCP de la machine) ne la reçoit jamais : elle propose de passer le relais à la machine qui la déclare. Pour
choisir cette machine, chacune publie ce qu'elle a, **par nom seulement** : jamais un chemin complet, jamais un contenu,
jamais un identifiant, jamais un nom de secret (le coffre local n'est pas inventorié).

- **dépôts** (`repos`) : dossiers git (`.git` dossier ou fichier de worktree) à la racine déclarée ou jusqu'à deux
  niveaux dessous ; le nom est le NOM DU DOSSIER (deux dépôts de même nom : le premier dans l'ordre des racines). Le
  chemin reste ici : `resolve("repo", nom)` le retrouve à l'ouverture de la session déléguée ;
- **hôtes** (`hosts`) : les noms de `hosts.yaml` (lot X11 : on ne lit QUE les noms, quelle que soit la forme du reste) ;
- **connecteurs** (`connectors`) : la nature des connecteurs posés dans Lokkal (`mail`, `calendar`) ;
- **mcp** : les noms des serveurs MCP déclarés dans la config de la machine.

Envoi : avec la charge (X5, `LoadReporter`), à `POST /v1/gateway/machine/resources`, quand l'inventaire change et au
moins toutes les 10 min. `clean_resources` est le nettoyage côté nokker.ai : un nom qui ressemble à un chemin (« / »,
« \\ », « ~ ») ou qui dépasse la forme permise est jeté, la machine n'est pas crue sur ce qu'elle envoie.
"""
from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("nokker.local.resources")

RESOURCES_VERSION = 1
KINDS = ("repo", "host", "connector", "mcp")
FIELD = {"repo": "repos", "host": "hosts", "connector": "connectors", "mcp": "mcp"}
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@+-]{0,63}$")  # pas de « / » : un nom, jamais un chemin
MAX_PER_KIND = 200
MAX_SCANNED_DIRS = 4000  # borne du parcours des racines (une racine montée peut être un $HOME entier)


def _is_repo(d: Path) -> bool:
    g = d / ".git"
    return g.is_dir() or g.is_file()


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def scan_repos(roots: list[str], depth: int = 2) -> dict[str, str]:
    """{nom : chemin} des dépôts git sous les racines (la racine elle-même et jusqu'à `depth` niveaux) ; dossiers
    cachés et liens qui sortent de la racine ignorés."""
    found: dict[str, str] = {}
    budget = MAX_SCANNED_DIRS
    for raw in roots:
        root = Path(raw)
        if not root.is_dir():
            continue
        level = [root]
        for d in range(depth + 1):
            nxt: list[Path] = []
            for p in level:
                budget -= 1
                if budget < 0:
                    logger.info("inventaire : parcours des racines coupé à %d dossiers", MAX_SCANNED_DIRS)
                    return found
                if _is_repo(p):
                    if NAME.match(p.name) and p.name not in found:
                        found[p.name] = str(p)
                    continue  # pas de dépôt dans un dépôt (sous-modules, vendored)
                if d == depth:
                    continue
                try:
                    kids = sorted(c for c in p.iterdir() if c.is_dir() and not c.name.startswith("."))
                except OSError:
                    continue
                nxt.extend(c for c in kids if not c.is_symlink() or _under(c, root))
            level = nxt
    return found


def host_names(path: Path | None) -> list[str]:
    """Noms des hôtes de `hosts.yaml` (X11) : `hosts: {nom: …}`, `hosts: [{name: …}]` ou un dictionnaire de noms à la
    racine. Rien d'autre n'est lu ; fichier absent ou illisible : aucun hôte."""
    if path is None or not path.is_file():
        return []
    import yaml

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        logger.info("inventaire : %s illisible, aucun hôte publié", path.name)
        return []
    hosts = data.get("hosts", data) if isinstance(data, dict) else data
    if isinstance(hosts, dict):
        names = [str(k) for k in hosts]
    elif isinstance(hosts, list):
        names = [str(h.get("name") if isinstance(h, dict) else h) for h in hosts]
    else:
        names = []
    return [n for n in names if NAME.match(n)]


@dataclass
class Inventory:
    """Ce que la machine déclare, et la résolution d'un nom en dossier de session (qui ne sort jamais d'ici)."""

    roots: list[str]
    hosts_path: Path | None = None
    connectors: Callable[[], list[str]] | None = None  # natures des connecteurs posés dans Lokkal
    mcp: Callable[[], list[str]] | None = None  # noms des serveurs MCP de la machine
    _repos: dict[str, str] = field(default_factory=dict, init=False, repr=False)

    def _names(self, fn: Callable[[], list[str]] | None) -> list[str]:
        if fn is None:
            return []
        try:
            return [n for n in (str(x) for x in fn()) if NAME.match(n)]
        except Exception:  # noqa: BLE001 — un magasin en panne ne bloque pas l'inventaire
            logger.debug("inventaire : source en échec", exc_info=True)
            return []

    def collect(self) -> dict[str, Any]:
        self._repos = scan_repos(self.roots)
        return {"v": RESOURCES_VERSION,
                "repos": sorted(self._repos)[:MAX_PER_KIND],
                "hosts": sorted(set(host_names(self.hosts_path)))[:MAX_PER_KIND],
                "connectors": sorted(set(self._names(self.connectors)))[:MAX_PER_KIND],
                "mcp": sorted(set(self._names(self.mcp)))[:MAX_PER_KIND]}

    def declares(self, kind: str, name: str) -> bool:
        if kind not in FIELD:
            return False
        return name in self.collect()[FIELD[kind]]

    def resolve(self, kind: str, name: str) -> str | None:
        """Dossier de la session déléguée pour ce besoin : le dépôt nommé ; pour un hôte, un connecteur ou un serveur
        MCP déclarés, la première racine (la session y travaille, la ressource passe par ses outils). None si la
        machine ne déclare pas la ressource."""
        if not self.declares(kind, name):
            return None
        if kind == "repo":
            return self._repos.get(name)
        return next((r for r in self.roots if os.path.isdir(r)), None)


def clean_resources(raw: Any) -> dict[str, Any] | None:
    """L'inventaire reçu d'une machine, réduit aux noms permis et bornés ; None si ce n'est pas un objet. Côté
    nokker.ai : un nom qui ressemble à un chemin ou à autre chose qu'un nom est jeté."""
    if not isinstance(raw, dict):
        return None
    out: dict[str, Any] = {"v": RESOURCES_VERSION}
    for f in FIELD.values():
        items = raw.get(f) if isinstance(raw.get(f), list) else []
        names = sorted({str(x) for x in items if isinstance(x, str) and NAME.match(x)})
        out[f] = names[:MAX_PER_KIND]
    return out


__all__ = ["FIELD", "KINDS", "NAME", "RESOURCES_VERSION", "Inventory", "clean_resources", "host_names", "scan_repos"]
