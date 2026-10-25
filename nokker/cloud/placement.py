"""Placement d'une session ou d'un agent sur une machine du compte (lot X15, docs/LOKKAL.md §7.19), côté nokker.ai.

UNE seule logique, pure (aucun appel réseau, aucune horloge propre), pour les deux usages :
  - la délégation web → Lokkal par besoin (X21, `nokker/cloud/handoff.py::choose`) ;
  - les passages des agents placés `auto` ou sur une machine nommée (X15, `nokker/cloud/crew.py`).

**Éligible** = en ligne (charge reçue il y a 180 s au plus, X5), qui DÉCLARE chaque ressource demandée dans son
inventaire par noms (X21 : dépôts, hôtes, connecteurs, serveurs MCP) et qui a au moins un GPU si `gpu` est demandé
(vidéo, inférence locale).

**Classement** des éligibles : RAM libre d'abord (la plus grande, par pas de 256 Mio : deux mesures à quelques Mio près
sont égales, sinon le bruit de la mesure déciderait seul), puis charge CPU relative (`load1 / cpu.count`, la plus
petite), puis tours actifs (le moins), puis le nom et l'id (un ordre stable : deux appels identiques choisissent la
même machine).

**Aucune éligible** : `machine` vaut None et `reason` dit pourquoi, lisible par l'utilisateur ; `fallback` est la
machine la plus récemment vue parmi celles qui ont les ressources et le GPU (hors ligne) : la demande l'attendra.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

FRESH_S = 180  # charge plus vieille : machine éteinte, hors ligne ou ancienne version (docs/LOKKAL.md §7.2)
RAM_STEP_MB = 256
KINDS = ("repos", "hosts", "connectors", "mcp")  # natures de l'inventaire (docs/LOKKAL.md §7.16 point 3)
NEED_KINDS = {"repo": "repos", "host": "hosts", "connector": "connectors", "mcp": "mcp"}  # X21 : besoin au singulier
SINGULAR = {v: k for k, v in NEED_KINDS.items()}
LABEL = {"repos": "dépôt", "hosts": "hôte", "connectors": "connecteur", "mcp": "serveur MCP"}
MAX_PER_KIND = 20
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@+-]{0,63}$")  # même règle que l'inventaire (`clean_resources`)


class NeedsError(ValueError):
    """Champ `needs` illisible (400)."""


@dataclass(frozen=True)
class Needs:
    """Ce qu'une session ou un agent exige de sa machine, par NOMS (jamais un chemin)."""

    repos: tuple[str, ...] = ()
    hosts: tuple[str, ...] = ()
    connectors: tuple[str, ...] = ()
    mcp: tuple[str, ...] = ()
    gpu: bool = False

    @classmethod
    def of(cls, kind: str, name: str) -> Needs:
        """Un besoin X21 (`{kind: repo|host|connector|mcp, name}`)."""
        f = NEED_KINDS.get(kind)
        return cls(**{f: (name,)}) if f else cls()

    def resources(self) -> list[tuple[str, str]]:
        return [(k, n) for k in KINDS for n in getattr(self, k)]

    def local_only(self) -> bool:
        """Exige une ressource qui n'existe que sur une machine (le web n'a ni dépôt, ni hôte, ni GPU)."""
        return bool(self.resources()) or self.gpu

    def to_json(self) -> dict[str, Any] | None:
        if not self.resources() and not self.gpu:
            return None
        return {**{k: list(getattr(self, k)) for k in KINDS}, "gpu": self.gpu}


def parse_needs(raw: Any) -> Needs:
    """`needs` d'un agent : `null`, ou `{repos?, hosts?, connectors?, mcp?: [noms], gpu?: bool}`. NeedsError sinon."""
    if raw is None:
        return Needs()
    if not isinstance(raw, dict):
        raise NeedsError("needs doit être un objet {repos, hosts, connectors, mcp, gpu} ou null")
    unknown = sorted(set(raw) - {*KINDS, "gpu"})
    if unknown:
        raise NeedsError(f"needs : champ inconnu {unknown[0]!r} (repos, hosts, connectors, mcp, gpu)")
    out: dict[str, Any] = {}
    for k in KINDS:
        items = raw.get(k)
        if items is None:
            continue
        if not isinstance(items, list):
            raise NeedsError(f"needs.{k} : liste de noms attendue")
        names: list[str] = []
        for item in items:
            name = str(item or "").strip()
            if not _NAME.match(name):
                raise NeedsError(f"needs.{k} : nom invalide {name[:40]!r} (un NOM tel que la machine le déclare, "
                                 "jamais un chemin)")
            if name not in names:
                names.append(name)
        if len(names) > MAX_PER_KIND:
            raise NeedsError(f"needs.{k} : {MAX_PER_KIND} noms au plus")
        out[k] = tuple(names)
    gpu = raw.get("gpu", False)
    if not isinstance(gpu, bool):
        raise NeedsError("needs.gpu : vrai ou faux")
    return Needs(**out, gpu=gpu)


def needs_of(raw: Any) -> Needs:
    """Comme `parse_needs`, sans lever : un champ stocké illisible ne demande rien (il a été validé à l'écriture)."""
    try:
        return parse_needs(raw)
    except NeedsError:
        return Needs()


# ─────────────────────────────── une machine ───────────────────────────────


def is_online(m: Any, now: float) -> bool:
    """Charge fraîche (X5), et pas de fermeture de tunnel signalée par le relais depuis cette charge."""
    load_at = getattr(m, "load_at", None)
    if load_at is None or now - load_at > FRESH_S:
        return False
    offline_at = getattr(m, "offline_at", None)
    return offline_at is None or offline_at < load_at


def gpu_count(m: Any) -> int:
    load = getattr(m, "load", None)
    gpus = load.get("gpus") if isinstance(load, dict) else None
    return len(gpus) if isinstance(gpus, list) else 0


def _declared(m: Any, kind: str) -> list[str]:
    res = getattr(m, "resources", None)
    return list(res.get(kind) or []) if isinstance(res, dict) else []


def lacks(m: Any, needs: Needs) -> list[dict[str, Any]]:
    """Ce qui manque à `m` pour `needs` (ressources par nom, GPU), indépendamment de son état en ligne. Une machine
    sans inventaire n'a rien ; une machine sans charge connue n'a pas de GPU connu."""
    out: list[dict[str, Any]] = []
    for kind, name in needs.resources():
        if name not in _declared(m, kind):
            out.append({"kind": SINGULAR[kind], "name": name,
                        "message": f"{LABEL[kind]} « {name} » absent de « {m.name} »"})
    if needs.gpu and gpu_count(m) == 0:
        out.append({"kind": "gpu", "message": f"pas de GPU sur « {m.name} »"
                    + (" (charge inconnue)" if not isinstance(getattr(m, "load", None), dict) else "")})
    return out


def cpu_rel(m: Any) -> float:
    cpu = (m.load or {}).get("cpu") or {} if isinstance(getattr(m, "load", None), dict) else {}
    count = cpu.get("count") or 1
    return float(cpu.get("load1") or 0) / float(count) if count else 0.0


def ram_free_mb(m: Any) -> int:
    ram = (m.load or {}).get("ram") or {} if isinstance(getattr(m, "load", None), dict) else {}
    return int(ram.get("free_mb") or 0)


def rank_key(m: Any) -> tuple:
    """Clé de tri (la plus petite gagne) : RAM libre (par pas de 256 Mio, décroissante), CPU relatif, tours actifs,
    nom, id."""
    active = int(((m.load or {}).get("active_turns") or 0) if isinstance(getattr(m, "load", None), dict) else 0)
    return (-(ram_free_mb(m) // RAM_STEP_MB), round(cpu_rel(m), 3), active, str(m.name), str(m.id))


def _age(m: Any, now: float) -> str:
    seen = getattr(m, "load_at", None) or getattr(m, "last_seen_at", None)
    if seen is None:
        return "jamais vue en ligne"
    minutes = max(1, int((now - seen) // 60))
    return f"hors ligne depuis {minutes} min" if minutes < 120 else f"hors ligne depuis {minutes // 60} h"


# ─────────────────────────────── le choix ───────────────────────────────


@dataclass
class Placement:
    machine: Any | None  # la machine choisie (en ligne, éligible), ou None
    ranked: list[Any] = field(default_factory=list)  # toutes les éligibles en ligne, dans l'ordre du classement
    fallback: Any | None = None  # aucune en ligne : la plus récemment vue de celles qui ont tout (la demande attend)
    reason: str | None = None  # pourquoi aucune n'est choisie (None si `machine`)
    lacking: dict[str, list[dict[str, Any]]] = field(default_factory=dict)  # machine_id → ce qui lui manque

    @property
    def online(self) -> bool:
        return self.machine is not None


def place(machines: list[Any], needs: Needs, now: float, *, only: str | None = None) -> Placement:
    """La machine d'une session ou d'un agent parmi `machines` (celles du compte, avec charge et inventaire :
    `LinkService.machines_with_load`). `only` : la machine nommée seulement (`placement: {machine: "mc_…"}`)."""
    pool = [m for m in machines if only is None or m.id == only]
    if not machines:
        return Placement(None, reason="aucune machine liée à ce compte : installer Nokker Lokkal et la relier "
                                      "(app.nokker.ai/link)")
    if not pool:
        return Placement(None, reason=f"machine {only} inconnue ou déliée de ce compte")
    lacking = {m.id: lacks(m, needs) for m in pool}
    have = [m for m in pool if not lacking[m.id]]
    online = sorted((m for m in have if is_online(m, now)), key=rank_key)
    if online:
        return Placement(online[0], ranked=online, lacking=lacking)
    if have:
        fallback = max(have, key=lambda m: (m.load_at or 0, m.last_seen_at or 0, m.id))
        if only is not None:
            reason = f"« {fallback.name} » {_age(fallback, now)} : le passage attend qu'elle revienne"
        else:
            names = ", ".join(f"« {m.name} » ({_age(m, now)})" for m in sorted(have, key=lambda m: m.name))
            reason = f"aucune machine éligible en ligne : {names}"
        return Placement(None, fallback=fallback, reason=reason, lacking=lacking)
    detail = "; ".join(x["message"] for m in sorted(pool, key=lambda m: m.name) for x in lacking[m.id])
    return Placement(None, reason=f"aucune machine n'a ce qu'il faut : {detail}", lacking=lacking)


__all__ = [
    "FRESH_S",
    "KINDS",
    "LABEL",
    "NEED_KINDS",
    "RAM_STEP_MB",
    "Needs",
    "NeedsError",
    "Placement",
    "cpu_rel",
    "gpu_count",
    "is_online",
    "lacks",
    "needs_of",
    "parse_needs",
    "place",
    "ram_free_mb",
    "rank_key",
]
