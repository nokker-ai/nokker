"""Statuts de présence et leurs libellés (vue seulement : rien de ce module n'est vu par le modèle)."""
from __future__ import annotations

WORKING, AWAITING, BLOCKED, PAUSED, IDLE = "working", "awaiting_approval", "blocked", "paused", "idle"
STATUSES = (AWAITING, BLOCKED, WORKING, PAUSED, IDLE)  # ordre d'affichage : ce qui t'attend d'abord
ACTIVITY_CHARS = 140


def flat(text: str | None, n: int = ACTIVITY_CHARS) -> str | None:
    t = " ".join((text or "").split())
    if not t:
        return None
    return t if len(t) <= n else t[: n - 1] + "…"


def rank(status: str) -> int:
    return STATUSES.index(status) if status in STATUSES else len(STATUSES)
