"""Règles d'approbation suggérées (docs/APPROVALS.md §7).

À partir de l'historique des cartes d'un compte, propose « toujours autoriser X » quand la même règle candidate (celle
que la décision locale proposait déjà, `nokker.local.approvals.suggested_rules`) a couvert au moins `threshold` cartes
approuvées dans la fenêtre, sans AUCUN refus. Ce n'est qu'une proposition : rien ne change tant que le propriétaire ne
l'accepte pas, et l'acceptation passe par le mécanisme de règles existant (règle `allow` des réglages de l'utilisateur,
docs/LOKKAL.md §7.4.3, portée `always`), avec sa provenance.

Jamais proposées, quel que soit l'historique : carte forte (destructif, plancher de la session), `SEND_BULK` (toujours
refusé, jamais une carte), effet destructif, plancher prod (`ops:prod*`), publication de page, profil mineur, connecteur
relayé et tour venu du web. Ces familles restent une carte à chaque fois.

Rien ici n'est visible par le modèle : ni outil, ni bloc de contexte ; seules des routes du propriétaire le lisent.
"""
from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from nokker.core.ports import Approval

DEFAULT_THRESHOLD = 5
DEFAULT_WINDOW_DAYS = 30
MAX_SUGGESTIONS = 20

APPROVED = frozenset({"approved", "executed", "failed"})  # « failed » : approuvée, l'outil a échoué ensuite
REJECTED = "rejected"

# Familles jamais proposées (le test `test_forbidden_families_never_suggested` les parcourt toutes).
FORBIDDEN_EFFECTS = frozenset({"destructive", "send_bulk"})
FORBIDDEN_RULES = frozenset({
    "pages:publish",                       # sortie vers le public : clé d'appareil à chaque fois
    "household:guardian",                  # profil mineur : son représentant décide chaque carte
    "permissions:relayed_connector",       # connecteur relayé : une carte par appel
    "permissions:relayed_strong",          # tour venu du web : carte forte au téléphone
    "permissions:local_connector_strong",  # boîte mail locale : envoi, corbeille
    "permissions:go_sandbox_only",
    "permissions:hardline",
})
FORBIDDEN_TOOLS = frozenset({"ssh_trust"})  # premier contact : une carte par clé d'hôte


def forbidden(card: Approval) -> bool:
    """Une carte dont la famille n'est JAMAIS transformée en règle durable."""
    rule = card.rule or ""
    if card.strength == "strong":
        return True
    if rule.startswith("effect:") and rule.split(":")[1] in FORBIDDEN_EFFECTS:
        return True
    if rule == "ops:prod" or rule.startswith("ops:prod:"):
        return True
    if rule in FORBIDDEN_RULES or card.tool_name in FORBIDDEN_TOOLS:
        return True
    return "send_bulk" in rule


@dataclass(frozen=True)
class Settings:
    enabled: bool = False
    threshold: int = DEFAULT_THRESHOLD  # cartes approuvées couvertes, au moins
    window_days: float = DEFAULT_WINDOW_DAYS

    @classmethod
    def from_config(cls, raw: dict[str, Any] | None) -> Settings:
        raw = raw or {}
        return cls(enabled=bool(raw.get("enabled", False)),
                   threshold=max(2, int(raw.get("threshold", DEFAULT_THRESHOLD))),
                   window_days=max(1.0, float(raw.get("window_days", DEFAULT_WINDOW_DAYS))))

    @property
    def window_s(self) -> float:
        return self.window_days * 86400.0


@dataclass
class Suggestion:
    id: str
    rule: str
    tool: str
    approved: int  # cartes approuvées couvertes dans la fenêtre
    approval_ids: list[str] = field(default_factory=list)  # les plus récentes d'abord, 10 au plus
    first_at: float = 0.0
    last_at: float = 0.0

    def to_json(self) -> dict[str, Any]:

        def iso(t: float) -> str:
            return datetime.fromtimestamp(t, tz=UTC).isoformat()

        return {"id": self.id, "rule": self.rule, "tool": self.tool, "approved": self.approved, "rejected": 0,
                "approval_ids": self.approval_ids, "first_at": iso(self.first_at), "last_at": iso(self.last_at)}


def suggestion_id(tenant: str, user: str, rule: str) -> str:
    """Stable pour (compte, utilisateur, règle) : un rejet masque la même proposition aux calculs suivants."""
    return "sg_" + hashlib.sha256(f"{tenant}\x00{user}\x00{rule}".encode()).hexdigest()[:20]


def _candidates(card: Approval) -> list[str]:
    from nokker.local.approvals import suggested_rules

    try:
        return suggested_rules(card.tool_name, card.arguments or {}, card.strength, card.rule)
    except Exception:  # noqa: BLE001 — arguments inattendus : pas de proposition plutôt qu'une route en échec
        return []


def compute(history: Iterable[Approval], *, tenant: str, user: str, settings: Settings, now: float,
            hidden: Iterable[str] = (), existing: Iterable[str] = ()) -> list[Suggestion]:
    """`history` : cartes du compte (déjà filtrées par locataire), tous statuts. Seules comptent celles décidées par
    `user` dans la fenêtre. `hidden` : ids rejetés ou acceptés ; `existing` : règles `allow` déjà en place."""
    since = now - settings.window_s
    hidden_ids, existing_rules = set(hidden), set(existing)
    approved: dict[str, list[Approval]] = {}
    refused: set[str] = set()
    for card in history:
        if card.decided_by != user or (card.decided_at or card.created_at) < since:
            continue
        cands = _candidates(card)
        if card.status == REJECTED:
            refused.update(cands)  # un refus bloque toute règle qui aurait couvert cette carte
            continue
        if card.status not in APPROVED or forbidden(card):
            continue
        for rule in cands:
            approved.setdefault(rule, []).append(card)

    out: list[Suggestion] = []
    covered: dict[frozenset[str], str] = {}  # ensemble de cartes → règle la plus étroite déjà retenue
    # La plus étroite d'abord (`suggested_rules` les rend dans cet ordre ; la plus longue liste de cartes ensuite).
    for rule in sorted(approved, key=lambda r: ("*" in r, len(approved[r]), r)):
        cards = approved[rule]
        if rule in refused or rule in existing_rules or len(cards) < settings.threshold:
            continue
        key = frozenset(c.id for c in cards)
        if key in covered:  # une règle plus large qui ne couvre rien de plus : pas de preuve en plus
            continue
        sid = suggestion_id(tenant, user, rule)
        covered[key] = rule
        if sid in hidden_ids:
            continue
        times = sorted((c.decided_at or c.created_at) for c in cards)
        recent = sorted(cards, key=lambda c: c.decided_at or c.created_at, reverse=True)[:10]
        out.append(Suggestion(id=sid, rule=rule, tool=cards[0].tool_name, approved=len(cards),
                              approval_ids=[c.id for c in recent], first_at=times[0], last_at=times[-1]))
    out.sort(key=lambda s: (-s.approved, s.rule))
    return out[:MAX_SUGGESTIONS]


__all__ = ["DEFAULT_THRESHOLD", "DEFAULT_WINDOW_DAYS", "Settings", "Suggestion", "compute", "forbidden",
           "suggestion_id"]
