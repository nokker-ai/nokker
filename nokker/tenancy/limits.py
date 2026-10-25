"""Quotas des testeurs (docs/TENANCY.md §5) : par compte (`Turn.sender`), des tours sur deux fenêtres, et des appels
Deep. Le locataire interne n'a aucun quota.

Offre Solo (décision produit du 22.10.2026) : 40 tours par jour et 200 par semaine. Le jour repart à 12:00 UTC, la
semaine le lundi à 06:00 UTC. Les deux fenêtres ne s'alignent pas : chacune a son compteur (`turns` sur la date du
début du jour, `week_turns` sur la date du lundi), jamais une somme de jours.

- Mode `soft` (la bêta : « on compte, on prévient, on ne coupe pas ») : 40/200 ne coupent rien ; le tour qui dépasse
  l'un des deux reçoit UNE fois un avertissement en fin de réponse (`notice`). Seul `hard_turns_per_day` (150, garde
  contre l'emballement de coût) refuse le tour.
- Mode `hard` (offre payante) : le tour au-delà du jour ou de la semaine est refusé.
- Refus : vérifié AVANT le tour (`core/loop.py::Kernel.run_turn`, juste après le contrôle d'entrée), réponse
  `reply(verdict)` sans appel modèle ni rappel, tour gardé dans l'historique, verdict `limits:…` journalisé.
- Deep : `LimitedPolicy` enveloppe la politique ; un appel Deep au-delà du plafond reste sur Ship (motif au ledger).

Compteurs : `tenant_usage` (migrations 007 et 011, RLS) si un DSN est configuré, en mémoire sinon. L'incrément est
atomique et borné (`take`) : deux tours simultanés ne dépassent jamais un plafond qui coupe.
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from nokker.core.ports import Policy, RouteDecision, RouteState
from nokker.core.turn import Decision, Gate, Tier, Verdict
from nokker.tenancy.context import INTERNAL_TENANT, current_tenant

ZURICH = ZoneInfo("Europe/Zurich")
DAY_RESET_UTC_HOUR = 12
WEEK_RESET_UTC_HOUR = 6  # le lundi
DEFAULT_LIMITS: dict[str, Any] = {"mode": "soft", "turns_per_day": 40, "turns_per_week": 200,
                                  "hard_turns_per_day": 150, "deep_per_day": 20}
KINDS = ("turns", "deep", "week_turns")
UNBOUNDED = 2**31 - 1

LIMIT_REPLY_DAY = (
    "Limite du jour atteinte : {n} messages par jour et par compte. Le compteur repart à {at} (heure suisse) ; ta "
    "conversation est gardée, tu pourras la reprendre là où elle s'est arrêtée.\n\n"
    "Daily limit reached: {n} messages per day and per account. The counter resets at {at} (Swiss time); your "
    "conversation is kept and you can pick it up where you left off.")
LIMIT_REPLY_WEEK = (
    "Limite de la semaine atteinte : {n} messages par semaine et par compte. Le compteur repart lundi à {at} (heure "
    "suisse) ; ta conversation est gardée.\n\n"
    "Weekly limit reached: {n} messages per week and per account. The counter resets on Monday at {at} (Swiss time); "
    "your conversation is kept.")
NOTICE_DAY = (
    "— Tu viens de dépasser les {n} messages du jour prévus par l'offre Solo. Pendant la bêta, rien n'est coupé : on "
    "compte pour régler l'offre. Le compteur repart à {at} (heure suisse).\n"
    "— You just passed the {n} daily messages of the Solo plan. During the beta nothing is cut: we count to tune the "
    "plan. The counter resets at {at} (Swiss time).")
NOTICE_WEEK = (
    "— Tu viens de dépasser les {n} messages de la semaine prévus par l'offre Solo. Pendant la bêta, rien n'est "
    "coupé : on compte pour régler l'offre. Le compteur repart lundi à {at} (heure suisse).\n"
    "— You just passed the {n} weekly messages of the Solo plan. During the beta nothing is cut: we count to tune "
    "the plan. The counter resets on Monday at {at} (Swiss time).")


def day_window(now: datetime) -> tuple[datetime, datetime]:
    """[début, fin) du jour de quota contenant `now` : de 12:00 UTC à 12:00 UTC."""
    t = now.astimezone(UTC)
    start = t.replace(hour=DAY_RESET_UTC_HOUR, minute=0, second=0, microsecond=0)
    if t < start:
        start -= timedelta(days=1)
    return start, start + timedelta(days=1)


def week_window(now: datetime) -> tuple[datetime, datetime]:
    """[début, fin) de la semaine de quota contenant `now` : du lundi 06:00 UTC au lundi suivant 06:00 UTC."""
    t = now.astimezone(UTC)
    start = (t - timedelta(days=t.weekday())).replace(hour=WEEK_RESET_UTC_HOUR, minute=0, second=0, microsecond=0)
    if t < start:
        start -= timedelta(days=7)
    return start, start + timedelta(days=7)


def _swiss_hour(at: datetime) -> str:
    return at.astimezone(ZURICH).strftime("%H:%M")


class UsageCounter(Protocol):
    def take(self, tenant_id: str, account: str, day: date, kind: str, cap: int) -> bool:
        """Compte une unité de `kind` si le compteur de `day` est sous `cap` ; False (rien compté) sinon."""
        ...

    def give_back(self, tenant_id: str, account: str, day: date, kind: str) -> None:
        """Rend une unité prise par `take` (tour refusé par une autre fenêtre)."""
        ...

    def read(self, tenant_id: str, account: str, day: date, kind: str) -> int: ...


class MemoryUsage:
    """Compteurs en mémoire (dev, tests, noyau sans Postgres) : remis à zéro au redémarrage."""

    def __init__(self) -> None:
        self._counts: dict[tuple[str, str, date, str], int] = {}
        self._lock = threading.Lock()

    def take(self, tenant_id: str, account: str, day: date, kind: str, cap: int) -> bool:
        key = (tenant_id, account, day, kind)
        with self._lock:
            n = self._counts.get(key, 0)
            if n >= cap:
                return False
            self._counts[key] = n + 1
            return True

    def give_back(self, tenant_id: str, account: str, day: date, kind: str) -> None:
        key = (tenant_id, account, day, kind)
        with self._lock:
            self._counts[key] = max(0, self._counts.get(key, 0) - 1)

    def read(self, tenant_id: str, account: str, day: date, kind: str) -> int:
        with self._lock:
            return self._counts.get((tenant_id, account, day, kind), 0)


class PgUsage:
    """Table `tenant_usage` (migrations 007, 011). Une connexion par opération, `app.tenant_id` posé avant toute
    requête : la RLS forcée filtre la table sur le locataire courant, et la ligne porte explicitement ce locataire."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    @staticmethod
    def _kind(kind: str) -> str:
        if kind not in KINDS:
            raise ValueError(f"compteur inconnu : {kind!r}")
        return kind  # colonne tirée de KINDS (liste fermée), jamais d'une entrée

    def _conn(self, tenant_id: str):
        import psycopg

        # TODO intégration p1/t-rls : passer par nokker/store/conn.py::connect (pose déjà app.tenant_id).
        conn = psycopg.connect(self.dsn, autocommit=True)
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant_id,))
        return conn

    def take(self, tenant_id: str, account: str, day: date, kind: str, cap: int) -> bool:
        k = self._kind(kind)
        if cap <= 0:
            return False
        sql = (f"INSERT INTO tenant_usage AS u (tenant_id, account, day, {k}) VALUES (%s, %s, %s, 1) "
               f"ON CONFLICT (tenant_id, account, day) DO UPDATE SET {k} = u.{k} + 1 WHERE u.{k} < %s "
               f"RETURNING u.{k}")
        with self._conn(tenant_id) as conn:
            row = conn.execute(sql, (tenant_id, account, day, cap)).fetchone()
        return row is not None

    def give_back(self, tenant_id: str, account: str, day: date, kind: str) -> None:
        k = self._kind(kind)
        with self._conn(tenant_id) as conn:
            conn.execute(f"UPDATE tenant_usage SET {k} = GREATEST({k} - 1, 0) "
                         "WHERE tenant_id = %s AND account = %s AND day = %s", (tenant_id, account, day))

    def read(self, tenant_id: str, account: str, day: date, kind: str) -> int:
        k = self._kind(kind)
        with self._conn(tenant_id) as conn:
            row = conn.execute(f"SELECT {k} FROM tenant_usage WHERE tenant_id = %s AND account = %s AND day = %s",
                               (tenant_id, account, day)).fetchone()
        return int(row[0]) if row else 0


@dataclass
class TenantLimits:
    counter: UsageCounter
    mode: str = DEFAULT_LIMITS["mode"]
    turns_per_day: int | None = DEFAULT_LIMITS["turns_per_day"]
    turns_per_week: int | None = DEFAULT_LIMITS["turns_per_week"]
    hard_turns_per_day: int | None = DEFAULT_LIMITS["hard_turns_per_day"]
    deep_per_day: int = DEFAULT_LIMITS["deep_per_day"]
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    @classmethod
    def from_config(cls, limits: dict[str, Any] | None, dsn: str = "") -> TenantLimits:
        cfg = {**DEFAULT_LIMITS, **(limits or {})}
        mode = str(cfg["mode"])
        if mode not in ("soft", "hard"):
            raise ValueError(f"tenant_limits.mode inconnu : {mode!r} (soft | hard)")

        def cap(key: str) -> int | None:
            return None if cfg.get(key) is None else int(cfg[key])

        return cls(counter=PgUsage(dsn) if dsn else MemoryUsage(), mode=mode, turns_per_day=cap("turns_per_day"),
                   turns_per_week=cap("turns_per_week"), hard_turns_per_day=cap("hard_turns_per_day"),
                   deep_per_day=int(cfg["deep_per_day"]))

    # ── fenêtres ──────────────────────────────────────────────────────────────────────────────────────────────────
    def _keys(self) -> tuple[tuple[datetime, datetime], tuple[datetime, datetime]]:
        now = self.clock()
        return day_window(now), week_window(now)

    def _day_cap(self) -> int | None:
        """Plafond qui COUPE dans la fenêtre du jour."""
        if self.mode == "soft":
            return self.hard_turns_per_day
        caps = [c for c in (self.turns_per_day, self.hard_turns_per_day) if c is not None]
        return min(caps) if caps else None

    def _week_cap(self) -> int | None:
        return self.turns_per_week if self.mode == "hard" else None

    # ── tour ──────────────────────────────────────────────────────────────────────────────────────────────────────
    def check_turn(self, sender: str | None) -> Verdict | None:
        """None : le tour passe (et il est compté). Sinon le verdict à journaliser ; le tour répond `reply(verdict)`."""
        tid = current_tenant()  # hors scope : NoTenant, jamais un tour sans quota par défaut
        if tid == INTERNAL_TENANT:
            return None
        who = sender or ""
        (day, _), (week, _) = self._keys()
        day_cap, week_cap = self._day_cap(), self._week_cap()
        if not self.counter.take(tid, who, day.date(), "turns", UNBOUNDED if day_cap is None else day_cap):
            soft = self.mode == "soft"
            return Verdict(gate=Gate.SWITCH, decision=Decision.DENY, layer=0,
                           rule="limits:hard_turns_per_day" if soft else "limits:turns_per_day",
                           reason=f"{day_cap} tours par jour atteints pour {sender or '(compte inconnu)'}",
                           subject="reply")
        if not self.counter.take(tid, who, week.date(), "week_turns", UNBOUNDED if week_cap is None else week_cap):
            self.counter.give_back(tid, who, day.date(), "turns")
            return Verdict(gate=Gate.SWITCH, decision=Decision.DENY, layer=0, rule="limits:turns_per_week",
                           reason=f"{week_cap} tours par semaine atteints pour {sender or '(compte inconnu)'}",
                           subject="reply")
        return None

    def reply(self, verdict: Verdict | None = None) -> str:
        (_, day_end), (_, week_end) = self._keys()
        if verdict is not None and verdict.rule == "limits:turns_per_week":
            return LIMIT_REPLY_WEEK.format(n=self.turns_per_week, at=_swiss_hour(week_end))
        return LIMIT_REPLY_DAY.format(n=self._day_cap(), at=_swiss_hour(day_end))

    def notice(self, sender: str | None) -> str | None:
        """Mode soft : avertissement à ajouter à la réponse du tour qui vient de DÉPASSER le quota du jour ou de la
        semaine (une seule fois par fenêtre : le tour n° cap + 1). None sinon, et toujours None en mode hard."""
        tid = current_tenant()
        if self.mode != "soft" or tid == INTERNAL_TENANT:
            return None
        who = sender or ""
        (day, day_end), (week, week_end) = self._keys()
        if self.turns_per_day is not None and \
                self.counter.read(tid, who, day.date(), "turns") == self.turns_per_day + 1:
            return NOTICE_DAY.format(n=self.turns_per_day, at=_swiss_hour(day_end))
        if self.turns_per_week is not None and \
                self.counter.read(tid, who, week.date(), "week_turns") == self.turns_per_week + 1:
            return NOTICE_WEEK.format(n=self.turns_per_week, at=_swiss_hour(week_end))
        return None

    def allow_deep(self, sender: str | None) -> bool:
        tid = current_tenant()
        if tid == INTERNAL_TENANT:
            return True
        (day, _), _ = self._keys()
        return self.counter.take(tid, sender or "", day.date(), "deep", self.deep_per_day)

    # ── lecture (GET /v1/account/usage, docs/CLIENT-API.md) ───────────────────────────────────────────────────────
    def usage(self, sender: str) -> dict[str, Any]:
        tid = current_tenant()
        internal = tid == INTERNAL_TENANT
        (day, day_end), (week, week_end) = self._keys()
        used_day = 0 if internal else self.counter.read(tid, sender, day.date(), "turns")
        used_week = 0 if internal else self.counter.read(tid, sender, week.date(), "week_turns")
        cap_day = None if internal else self.turns_per_day
        cap_week = None if internal else self.turns_per_week
        hard = None if internal else self._day_cap() if self.mode == "hard" else self.hard_turns_per_day
        return {
            "tz": "Europe/Zurich", "mode": self.mode, "plan": None,
            "today": {"day": day.date().isoformat(), "resets_at": day_end.astimezone(ZURICH).isoformat(),
                      "turns": {"used": used_day, "cap": cap_day, "over": cap_day is not None and used_day > cap_day,
                                "hard_cap": hard}},
            "week": {"starts": week.astimezone(ZURICH).isoformat(), "resets_at": week_end.astimezone(ZURICH).isoformat(),
                     "turns": {"used": used_week, "cap": cap_week,
                               "over": cap_week is not None and used_week > cap_week}},
        }


@dataclass
class LimitedPolicy:
    """Enveloppe d'une `Policy` : un choix Deep au-delà du plafond du compte reste sur Ship."""

    inner: Policy
    limits: TenantLimits

    def choose(self, state: RouteState) -> RouteDecision:
        route = self.inner.choose(state)
        if route.tier is not Tier.DEEP or self.limits.allow_deep(state.turn.sender):
            return route
        return RouteDecision(tier=Tier.SHIP, reasoning=False,
                             reason=f"limits:deep_per_day ({self.limits.deep_per_day}/jour atteints), reste sur Ship ; "
                                    f"demandé : {route.reason}")


__all__ = ["DEFAULT_LIMITS", "LIMIT_REPLY_DAY", "LIMIT_REPLY_WEEK", "LimitedPolicy", "MemoryUsage", "PgUsage",
           "TenantLimits", "UsageCounter", "day_window", "week_window"]
