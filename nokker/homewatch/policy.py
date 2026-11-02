"""Droits des membres du foyer sur la veille (docs/HOME-WATCH.md §3), déterministes, sans modèle.

Cinq capacités : `cameras` (résumés et vignettes en local, notifications d'incident), `direct` (flux en direct, carte
forte), `reseau` (qui est sur le Wi-Fi, appareils inconnus), `nas` (disques, volumes, sauvegardes), `actions`
(actions nommées sous carte forte : pause d'un appareil, relance d'une sauvegarde). Un membre est une identité de tour
(`Turn.sender`) associée à un rôle ; une identité inconnue prend `default_role` (« guest » : aucun droit). Un profil
mineur d'un foyer (teen/child) prend toujours le rôle du même nom, quelle que soit la config.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

CAPS = ("cameras", "direct", "reseau", "nas", "actions")
DEFAULT_ROLES: dict[str, list[str]] = {
    "adult": list(CAPS),
    "teen": [],
    "child": [],
    "guest": [],
}
MINOR_ROLES = {"teen": "teen", "child": "child"}


@dataclass
class WatchPolicy:
    roles: dict[str, frozenset[str]] = field(default_factory=lambda: {k: frozenset(v) for k, v in
                                                                      DEFAULT_ROLES.items()})
    members: dict[str, str] = field(default_factory=dict)
    default_role: str = "guest"
    profile_of: Callable[[str | None], str | None] | None = None

    @classmethod
    def from_config(cls, raw: dict[str, Any] | None) -> WatchPolicy:
        raw = raw or {}
        roles: dict[str, frozenset[str]] = {k: frozenset(v) for k, v in DEFAULT_ROLES.items()}
        for name, caps in (raw.get("roles") or {}).items():
            caps = [caps] if isinstance(caps, str) else list(caps or [])
            unknown = sorted(set(map(str, caps)) - set(CAPS) - {"*"})
            if unknown:
                raise ValueError(f"home_watch.roles.{name} : capacité(s) inconnue(s) {', '.join(unknown)}")
            roles[str(name)] = frozenset(CAPS) if "*" in caps else frozenset(map(str, caps))
        default_role = str(raw.get("default_role") or "guest")
        if default_role not in roles:
            raise ValueError(f"home_watch.default_role inconnu : {default_role}")
        members = {str(k): str(v) for k, v in (raw.get("members") or {}).items()}
        bad = sorted({r for r in members.values() if r not in roles})
        if bad:
            raise ValueError(f"home_watch.members : rôle(s) inconnu(s) {', '.join(bad)}")
        return cls(roles=roles, members=members, default_role=default_role)

    def role_of(self, sender: str | None) -> str:
        if self.profile_of is not None and sender:
            try:
                profile = self.profile_of(sender)
            except Exception:  # noqa: BLE001 — foyer illisible : le rôle le plus restreint
                return "child"
            if profile in MINOR_ROLES:
                return MINOR_ROLES[profile]
        return self.members.get(sender or "", self.default_role)

    def can(self, sender: str | None, cap: str) -> bool:
        return cap in self.roles.get(self.role_of(sender), frozenset())

    def any_right(self, sender: str | None) -> bool:
        return bool(self.roles.get(self.role_of(sender)))

    def recipients(self, cap: str = "cameras") -> list[str]:
        """Membres déclarés qui ont `cap` (destinataires d'une notification d'incident)."""
        return sorted(m for m in self.members if self.can(m, cap))


__all__ = ["CAPS", "DEFAULT_ROLES", "WatchPolicy"]
