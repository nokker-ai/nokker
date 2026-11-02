"""Versions des variantes appliquées (docs/SELF-IMPROVE.md §6) : chaque application est une version COMPLÈTE du calque de
sa cible, rien n'est jamais effacé, et revenir en arrière crée une version de plus (comme les skills appris,
docs/LEARNING.md §2.6).

Fichiers JSON 0600 sous `<état>/improve/<cible>/v0001.json` : {target, version, overlay, sha256, op (apply | revert),
reverted_to, actor (carte | owner), approval_id, proposal_id, scores_before, scores_after, at}. Version 0 = calque vide
(rien d'appliqué). Les calques `tools` et `router` sont ceux de l'INSTANCE (locataire interne, l'opérateur) : aucune
donnée de compte n'y entre (la réflexion ne lit que les traces internes, `traces.py`). Les skills appris ne passent pas
ici : leurs versions vivent dans le magasin de l'apprentissage (`nokker/learning`)."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nokker.improve.overlay import sha256_of

STORED_TARGETS = ("tools", "router")


@dataclass
class OverlayVersion:
    target: str
    version: int
    overlay: dict[str, Any]
    sha256: str
    op: str  # apply | revert
    actor: str = "carte"  # carte (approuvée) | owner (geste de l'opérateur : `nokker improve revert`)
    reverted_to: int | None = None
    approval_id: str | None = None
    proposal_id: str | None = None
    scores_before: dict[str, Any] = field(default_factory=dict)
    scores_after: dict[str, Any] = field(default_factory=dict)
    at: str = ""

    def to_json(self, overlay: bool = True) -> dict[str, Any]:
        d = asdict(self)
        if not overlay:
            d.pop("overlay")
        return d


def default_root(config: Any) -> Path:
    settings = getattr(config, "self_improve_settings", None) or {}
    if settings.get("state_dir"):
        return Path(str(settings["state_dir"])).expanduser()
    local = getattr(config, "local", None) or {}
    if local.get("state_dir"):
        return Path(str(local["state_dir"])).expanduser() / "improve"
    return Path("~/.config/nokker/improve").expanduser()


class ImproveStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    @classmethod
    def from_config(cls, config: Any) -> ImproveStore:
        return cls(default_root(config))

    def _dir(self, target: str) -> Path:
        if target not in STORED_TARGETS:
            raise ValueError(f"cible sans calque versionné : {target!r}")
        return self.root / target

    def versions(self, target: str) -> list[OverlayVersion]:
        d = self._dir(target)
        if not d.is_dir():
            return []
        out = []
        for p in sorted(d.glob("v*.json")):
            with open(p, encoding="utf-8") as f:
                out.append(OverlayVersion(**json.load(f)))
        return out

    def current(self, target: str) -> OverlayVersion | None:
        v = self.versions(target)
        return v[-1] if v else None

    def active(self, target: str) -> dict[str, Any]:
        cur = self.current(target)
        return dict(cur.overlay) if cur is not None else {}

    def add(self, target: str, overlay: dict[str, Any], *, op: str = "apply", actor: str = "carte",
            reverted_to: int | None = None, approval_id: str | None = None, proposal_id: str | None = None,
            scores_before: dict[str, Any] | None = None, scores_after: dict[str, Any] | None = None) -> OverlayVersion:
        d = self._dir(target)
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
        cur = self.current(target)
        v = OverlayVersion(target=target, version=(cur.version + 1) if cur else 1, overlay=dict(overlay),
                           sha256=sha256_of(overlay), op=op, actor=actor, reverted_to=reverted_to,
                           approval_id=approval_id, proposal_id=proposal_id, scores_before=dict(scores_before or {}),
                           scores_after=dict(scores_after or {}),
                           at=datetime.now(UTC).isoformat(timespec="seconds"))
        path = d / f"v{v.version:04d}.json"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)  # jamais deux fois la même version
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(v.to_json(), f, ensure_ascii=False, indent=1, sort_keys=True)
        return v

    def revert(self, target: str, version: int, actor: str = "owner") -> OverlayVersion:
        """Nouvelle version au calque de `version` ; 0 = calque vide (plus rien d'appliqué)."""
        if version == 0:
            overlay: dict[str, Any] = {}
        else:
            got = next((v for v in self.versions(target) if v.version == version), None)
            if got is None:
                raise LookupError(f"{target} : version {version} inconnue")
            overlay = got.overlay
        return self.add(target, overlay, op="revert", actor=actor, reverted_to=version)


__all__ = ["STORED_TARGETS", "ImproveStore", "OverlayVersion", "default_root"]
