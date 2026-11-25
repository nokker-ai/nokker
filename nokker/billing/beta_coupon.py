"""Coupon bêta PAR COMPTE : « Solo offert 6 mois » (C16, décision du 15.11.2026 ; migration 088, docs/LOKKAL.md).

Remplace le coupon global `billing_coupon`, qui partait avec CHAQUE Checkout. Règles :
- **Éligibilité manuelle**, posée par l'opérateur À L'INVITATION (console, `POST /admin/v1/applications/<id>/invite`
  avec `beta_coupon: true`) : une ligne par invitation front1. Le compte né de cette invitation (`accounts.invitation_id`)
  est le seul à pouvoir s'en servir : rien ne se transmet d'un compte à l'autre. Pour un compte déjà créé (avant C16, ou
  invitation non cochée), l'opérateur la pose après coup : `POST /admin/v1/accounts/<id>/beta-coupon` (clé
  `account:<id>`, idempotent, journalisé). Un compte n'a jamais deux offres : celle de son invitation passe d'abord.
- **Offre Solo seulement**, 6 mois (durée portée par le coupon Stripe `billing_beta_coupon`, `duration: repeating`).
  Une autre formule, même pour un compte éligible, part au plein tarif ; l'attribution reste intacte.
- **Usage unique** : au Checkout Solo, l'attribution est RÉSERVÉE au nom de la session (`metadata.beta_grant`) ; elle
  est CONSOMMÉE au webhook `checkout.session.completed` de cette session, une seule fois (verrou sur la ligne ; un
  événement rejoué, ou un autre événement de la même session, ne consomme rien de plus). `checkout.session.expired`
  rend la réservation. Une nouvelle demande de Checkout reprend la réservation et fait expirer l'ancienne session
  chez Stripe (au mieux).
- **Compte non éligible** : plein tarif, et une raison lisible (`beta_coupon` + `message` dans la réponse du Checkout,
  `beta_offer` dans `GET /v1/billing/plan`).
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

PLAN = "solo"
MONTHS = 6
STATES = ("granted", "reserved", "consumed", "revoked")
# Raisons rendues au client (champ `beta_coupon` du Checkout) : `applied` = remise posée sur cette session.
REASONS = ("applied", "not_eligible", "other_plan", "used", "unavailable")

_MESSAGES = {
    "fr": {
        "applied": "Solo offert 6 mois : la remise est appliquée au paiement.",
        "not_eligible": "Ton compte n'a pas l'offre bêta « Solo offert 6 mois » : l'abonnement est au plein tarif.",
        "other_plan": "L'offre bêta (6 mois offerts) ne vaut que sur {solo} : sur {plan}, tu paies le tarif normal{price}.",
        "used": "L'offre bêta de ton compte a déjà servi : l'abonnement est au plein tarif.",
        "unavailable": "L'offre bêta n'est pas disponible pour le moment : l'abonnement est au plein tarif.",
    },
    "en": {
        "applied": "Solo free for 6 months: the discount is applied at checkout.",
        "not_eligible": "Your account doesn't have the beta offer \"Solo free for 6 months\": you pay the full price.",
        "other_plan": "The beta offer (6 months free) only applies to {solo}: on {plan}, you pay the regular price{price}.",
        "used": "Your account's beta offer has already been used: you pay the full price.",
        "unavailable": "The beta offer isn't available right now: you pay the full price.",
    },
}


ACCOUNT_PREFIX = "account:"


def account_key(account_id: str) -> str:
    """Clé d'une attribution posée APRÈS la création du compte (console, `POST /admin/v1/accounts/<id>/beta-coupon`) :
    les comptes nés avant C16, ou d'une invitation non cochée. Même table, même cycle, même usage unique."""
    return f"{ACCOUNT_PREFIX}{account_id}"


def message(reason: str, lang: str, *, solo: str = "Solo", plan: str = "", price: dict[str, Any] | None = None) -> str:
    """Texte de la raison. `other_plan` nomme la formule demandée et son prix (`{amount, currency, interval}` lu sur
    Stripe, voir `Billing.price`) ; sans prix lisible, la parenthèse disparaît (jamais un prix inventé)."""
    en = lang == "en"
    text = _MESSAGES["en" if en else "fr"][reason]
    if reason != "other_plan":
        return text
    return text.format(solo=solo, plan=plan or "?", price=f" ({price_label(price, en)})" if price else "")


def price_label(price: dict[str, Any], en: bool) -> str:
    """49 CHF/mois · 9,50 CHF/mois · 49 CHF/month · 9.50 CHF/month (montant tel que Stripe le donne)."""
    amount = price["amount"]
    num = f"{amount}" if isinstance(amount, int) else f"{amount:.2f}"
    if not en:
        num = num.replace(".", ",")
    per = {"month": ("mois", "month"), "year": ("an", "year")}.get(str(price.get("interval") or ""))
    return f"{num} {price['currency']}" + (f"/{per[1] if en else per[0]}" if per else "")


class GrantStore(Protocol):
    def grant(self, invitation_id: str, by: str, now: datetime) -> bool:
        """Pose l'éligibilité (False si l'invitation en a déjà une : rien ne change)."""
        ...

    def get(self, invitation_id: str) -> dict[str, Any] | None: ...

    def claim(self, invitation_id: str, until: datetime, now: datetime) -> tuple[bool, str | None]:
        """Réserve l'attribution (`granted`, ou `reserved` déjà là : reprise). Rend (réservée, session précédente)."""
        ...

    def attach(self, invitation_id: str, session: str) -> None: ...

    def unclaim(self, invitation_id: str) -> None:
        """Rend une réservation sans session (Stripe n'a pas créé la session)."""
        ...

    def release(self, invitation_id: str, session: str) -> bool:
        """`reserved` par CETTE session → `granted` (session expirée chez Stripe)."""
        ...

    def consume(self, invitation_id: str, session: str, event_id: str, now: datetime) -> str:
        """Atomique (verrou sur la ligne) : `consumed` (cette fois-ci), `already` (déjà consommée : rien ne change),
        `revoked` ou `unknown`."""
        ...


@dataclass
class MemoryGrants:
    rows: dict[str, dict[str, Any]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def grant(self, invitation_id: str, by: str, now: datetime) -> bool:
        with self._lock:
            if invitation_id in self.rows:
                return False
            self.rows[invitation_id] = {"invitation_id": invitation_id, "plan": PLAN, "months": MONTHS,
                                        "state": "granted", "checkout_session": None, "reserved_until": None,
                                        "consumed_event": None, "consumed_at": None, "granted_by": by,
                                        "granted_at": now}
            return True

    def get(self, invitation_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.rows.get(invitation_id)
            return dict(row) if row else None

    def claim(self, invitation_id: str, until: datetime, now: datetime) -> tuple[bool, str | None]:
        with self._lock:
            row = self.rows.get(invitation_id)
            if row is None or row["state"] not in ("granted", "reserved"):
                return False, None
            prev = row["checkout_session"] if row["state"] == "reserved" else None
            row.update(state="reserved", checkout_session=None, reserved_until=until)
            return True, prev

    def attach(self, invitation_id: str, session: str) -> None:
        with self._lock:
            row = self.rows.get(invitation_id)
            if row is not None and row["state"] == "reserved":
                row["checkout_session"] = session

    def unclaim(self, invitation_id: str) -> None:
        with self._lock:
            row = self.rows.get(invitation_id)
            if row is not None and row["state"] == "reserved" and row["checkout_session"] is None:
                row.update(state="granted", reserved_until=None)

    def release(self, invitation_id: str, session: str) -> bool:
        with self._lock:
            row = self.rows.get(invitation_id)
            if row is None or row["state"] != "reserved" or row["checkout_session"] != session:
                return False
            row.update(state="granted", checkout_session=None, reserved_until=None)
            return True

    def consume(self, invitation_id: str, session: str, event_id: str, now: datetime) -> str:
        with self._lock:
            row = self.rows.get(invitation_id)
            if row is None:
                return "unknown"
            if row["state"] == "consumed":
                return "already"
            if row["state"] == "revoked":
                return "revoked"
            row.update(state="consumed", checkout_session=session, consumed_event=event_id, consumed_at=now,
                       reserved_until=None)
            return "consumed"


class PgGrants:
    """Table hors RLS (088) : connexion d'administration, comme `vouchers`."""

    COLS = ("invitation_id, plan, months, state, checkout_session, reserved_until, consumed_event, consumed_at, "
            "granted_by, granted_at")

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def _conn(self, autocommit: bool = True):
        from psycopg.rows import dict_row

        from nokker.store.conn import connect_admin

        return connect_admin(self.dsn, row_factory=dict_row, autocommit=autocommit)

    def grant(self, invitation_id: str, by: str, now: datetime) -> bool:
        with self._conn() as conn:
            return conn.execute("INSERT INTO beta_coupon_grants (invitation_id, granted_by, granted_at) VALUES (%s,%s,%s) "
                                "ON CONFLICT DO NOTHING RETURNING 1", (invitation_id, by, now)).fetchone() is not None

    def get(self, invitation_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            return conn.execute(f"SELECT {self.COLS} FROM beta_coupon_grants WHERE invitation_id = %s",
                                (invitation_id,)).fetchone()

    def claim(self, invitation_id: str, until: datetime, now: datetime) -> tuple[bool, str | None]:
        with self._conn(autocommit=False) as conn, conn.transaction():
            row = conn.execute("SELECT state, checkout_session FROM beta_coupon_grants WHERE invitation_id = %s FOR UPDATE",
                               (invitation_id,)).fetchone()
            if row is None or row["state"] not in ("granted", "reserved"):
                return False, None
            conn.execute("UPDATE beta_coupon_grants SET state = 'reserved', checkout_session = NULL, reserved_until = %s, "
                         "updated_at = %s WHERE invitation_id = %s", (until, now, invitation_id))
            return True, row["checkout_session"] if row["state"] == "reserved" else None

    def attach(self, invitation_id: str, session: str) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE beta_coupon_grants SET checkout_session = %s, updated_at = now() "
                         "WHERE invitation_id = %s AND state = 'reserved'", (session, invitation_id))

    def unclaim(self, invitation_id: str) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE beta_coupon_grants SET state = 'granted', reserved_until = NULL, updated_at = now() "
                         "WHERE invitation_id = %s AND state = 'reserved' AND checkout_session IS NULL", (invitation_id,))

    def release(self, invitation_id: str, session: str) -> bool:
        with self._conn() as conn:
            return conn.execute(
                "UPDATE beta_coupon_grants SET state = 'granted', checkout_session = NULL, reserved_until = NULL, "
                "updated_at = now() WHERE invitation_id = %s AND state = 'reserved' AND checkout_session = %s RETURNING 1",
                (invitation_id, session)).fetchone() is not None

    def consume(self, invitation_id: str, session: str, event_id: str, now: datetime) -> str:
        with self._conn(autocommit=False) as conn, conn.transaction():
            row = conn.execute("SELECT state FROM beta_coupon_grants WHERE invitation_id = %s FOR UPDATE",
                               (invitation_id,)).fetchone()
            if row is None:
                return "unknown"
            if row["state"] in ("consumed", "revoked"):
                return "already" if row["state"] == "consumed" else "revoked"
            conn.execute("UPDATE beta_coupon_grants SET state = 'consumed', checkout_session = %s, consumed_event = %s, "
                         "consumed_at = %s, reserved_until = NULL, updated_at = %s WHERE invitation_id = %s",
                         (session, event_id, now, now, invitation_id))
            return "consumed"


__all__ = ["ACCOUNT_PREFIX", "MONTHS", "PLAN", "account_key", "REASONS", "GrantStore", "MemoryGrants", "PgGrants", "message"]
