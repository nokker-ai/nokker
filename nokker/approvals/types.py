"""Types annexes du chantier Approbations fortes — appareils et sessions (docs/CLIENT-API.md). Pas dans
`core/ports.py` : contrairement à `Approval`/`Challenge`/`Proof` (consommés par `core/loop.py::Kernel`),
`Device` et `Session` ne traversent jamais la boucle — ils sont propres à l'authentification côté API
(`nokker/api/server.py`) et au cycle d'enrôlement (`docs/APPROVALS.md` §3)."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Device:
    """Un appareil enrôlé : clé publique ECDSA P-256 (SPKI, base64) + statut. `code` : identifiant COURT
    (6 caractères hex), pour l'enrôlement humain (`python -m nokker devices approve <code>`) — distinct
    de `id` (UUID complet, utilisé dans les URLs/`Proof.device_id`)."""

    id: str
    code: str
    user: str
    name: str
    public_key_spki: str  # SPKI DER, base64 standard — JAMAIS une clé privée
    status: str = "pending"  # pending | approved | revoked
    created_at: float = 0.0
    approved_at: float | None = None
    approved_via: str | None = None  # "totp" | "cli" | "invitation:<id>"
    telegram_user_id: str | None = None  # si l'appareil est la Mini App — recoupé avec initData.user.id


@dataclass
class Session:
    """Jeton de session émis après une connexion prouvée (signature de défi par la clé de l'appareil,
    `POST /v1/sessions`) — porte l'utilisateur VÉRIFIÉ (docs/CLIENT-API.md §Authentification)."""

    token: str
    user: str
    device_id: str
    created_at: float
    expires_at: float


__all__ = ["Device", "Session"]
