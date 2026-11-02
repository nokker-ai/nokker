"""Console opérateur sur la bêta OVH : comptes, usage et drapeaux (lecture seule, aucune conversation).

Ce module ne lit QUE des identifiants et des agrégats : comptes (`accounts`, `tenants`), compteurs de tours
(`tenant_usage`, RLS : une connexion, un locataire à la fois via `app.tenant_id`), palier (`subscriptions`, RLS aussi).
Jamais `turns`, `threads` ni aucun contenu. Les routes qui l'appellent exigent un rôle owner/ops.
"""
from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from nokker.billing.plans import ACTIVE_STATUSES, plans_from_config
from nokker.tenancy.limits import DEFAULT_LIMITS, ZURICH, day_window, week_window

LIMIT_KEYS = ("turns_per_day", "turns_per_week", "hard_turns_per_day", "deep_per_day")
MAX_DAYS = 31


class Overview(Protocol):
    def accounts(self, limit: int = 500) -> list[dict[str, Any]]: ...

    def plans(self) -> dict[str, str]: ...

    def usage_rows(self, days: int, now: datetime) -> tuple[list[dict[str, Any]], dict[str, str]]:
        """(lignes tenant_usage des `days` derniers jours, palier actif par locataire)."""
        ...


def _iso(v: Any) -> Any:
    return v.isoformat() if hasattr(v, "isoformat") else v


class PgOverview:
    """Postgres du noyau. `tenants` et `accounts` sont hors RLS ; `tenant_usage` et `subscriptions` se lisent un
    locataire à la fois (la politique elle-même interdit de voir une ligne d'un autre)."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def _conn(self):
        import psycopg
        from psycopg.rows import dict_row

        return psycopg.connect(self.dsn, row_factory=dict_row, autocommit=True)

    def accounts(self, limit: int = 500) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT a.id, a.tenant_id, a.email, a.lang, a.status, a.created_at, t.status AS tenant_status "
                "FROM accounts a JOIN tenants t ON t.id = a.tenant_id ORDER BY a.created_at DESC, a.id LIMIT %s",
                (limit,)).fetchall()
        return [dict(r) for r in rows]

    def plans(self) -> dict[str, str]:
        return self._read(0, datetime.now(UTC), usage=False)[1]

    def usage_rows(self, days: int, now: datetime) -> tuple[list[dict[str, Any]], dict[str, str]]:
        return self._read(days, now, usage=True)

    def _read(self, days: int, now: datetime, *, usage: bool) -> tuple[list[dict[str, Any]], dict[str, str]]:
        since = (now.astimezone(UTC) - timedelta(days=days + 7)).date()  # +7 : la ligne du lundi de la semaine en cours
        out: list[dict[str, Any]] = []
        plans: dict[str, str] = {}
        with self._conn() as conn:
            tenants = [r["id"] for r in conn.execute("SELECT id FROM tenants ORDER BY id").fetchall()]
            has_subs = conn.execute("SELECT to_regclass('subscriptions') AS r").fetchone()["r"] is not None
            for tid in tenants:
                conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tid,))
                if usage:
                    for r in conn.execute("SELECT tenant_id, account, day, turns, deep, week_turns FROM tenant_usage "
                                          "WHERE day >= %s ORDER BY day", (since,)).fetchall():
                        out.append(dict(r))
                if has_subs:
                    sub = conn.execute("SELECT plan FROM subscriptions WHERE status = ANY(%s) "
                                       "ORDER BY updated_at DESC LIMIT 1", (sorted(ACTIVE_STATUSES),)).fetchone()
                    if sub:
                        plans[tid] = sub["plan"]
            conn.execute("SELECT set_config('app.tenant_id', '', false)")
        return out, plans


@dataclasses.dataclass
class MemoryOverview:
    """Dev et tests : mêmes sorties, données injectées."""

    account_rows: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    usage: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    plans_by_tenant: dict[str, str] = dataclasses.field(default_factory=dict)

    def accounts(self, limit: int = 500) -> list[dict[str, Any]]:
        return [dict(a) for a in self.account_rows[:limit]]

    def plans(self) -> dict[str, str]:
        return dict(self.plans_by_tenant)

    def usage_rows(self, days: int, now: datetime) -> tuple[list[dict[str, Any]], dict[str, str]]:
        return [dict(r) for r in self.usage], dict(self.plans_by_tenant)


def mask_email(email: str) -> str:
    local, _, domain = (email or "").partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"


def accounts_view(rows: list[dict[str, Any]], plans: dict[str, str], role: str) -> dict[str, Any]:
    """Comptes de la bêta : identifiants, statut, langue, palier. `lecteur` ne voit jamais l'adresse en clair."""
    items = []
    for r in rows:
        mail = str(r.get("email", ""))
        items.append({"id": r["id"], "tenant_id": r["tenant_id"], "email": mask_email(mail) if role == "lecteur" else mail,
                      "lang": r.get("lang", ""), "status": r.get("status", ""),
                      "tenant_status": r.get("tenant_status", ""), "created_at": _iso(r.get("created_at")),
                      "plan": plans.get(r["tenant_id"])})
    by_status: dict[str, int] = {}
    for i in items:
        by_status[i["status"]] = by_status.get(i["status"], 0) + 1
    return {"total": len(items), "by_status": by_status,
            "by_plan": _count(i["plan"] or "bêta (sans abonnement)" for i in items), "items": items}


def _count(keys) -> dict[str, int]:
    out: dict[str, int] = {}
    for k in keys:
        out[k] = out.get(k, 0) + 1
    return out


def usage_view(rows: list[dict[str, Any]], plans: dict[str, str], limits_cfg: dict[str, Any] | None,
               billing_plans: dict[str, dict[str, int]] | None, now: datetime, days: int) -> dict[str, Any]:
    """Tours par compte (jour et semaine de quota), plafonds effectifs (config + palier), tours par jour.

    Même découpage que `nokker.tenancy.limits` : la ligne du jour (12:00 UTC) porte `turns` et `deep`, la ligne du
    lundi de la semaine (06:00 UTC) porte `week_turns`."""
    cfg = {**DEFAULT_LIMITS, **(limits_cfg or {})}
    catalogue = plans_from_config(billing_plans)
    (d0, d1), (w0, _w1) = day_window(now), week_window(now)
    today, monday = d0.date(), w0.date()
    accounts: dict[tuple[str, str], dict[str, Any]] = {}
    by_day: dict[str, dict[str, Any]] = {}
    for r in rows:
        key = (r["tenant_id"], r["account"])
        acc = accounts.setdefault(key, {"account": r["account"], "tenant_id": r["tenant_id"], "turns_today": 0,
                                         "deep_today": 0, "turns_week": 0})
        day = r["day"]
        if day == today:
            acc["turns_today"] += int(r.get("turns") or 0)
            acc["deep_today"] += int(r.get("deep") or 0)
        if day == monday:
            acc["turns_week"] += int(r.get("week_turns") or 0)
        t = int(r.get("turns") or 0)
        if t:
            slot = by_day.setdefault(day.isoformat(), {"turns": 0, "deep": 0, "accounts": 0})
            slot["turns"] += t
            slot["deep"] += int(r.get("deep") or 0)
            slot["accounts"] += 1
    items = []
    for acc in accounts.values():
        plan = plans.get(acc["tenant_id"])
        caps = {k: cfg.get(k) for k in LIMIT_KEYS}
        if plan in catalogue:
            caps.update({k: v for k, v in catalogue[plan].caps.items() if k in LIMIT_KEYS})
        cap_day, cap_week = caps["turns_per_day"], caps["turns_per_week"]
        hard = caps["hard_turns_per_day"] if cfg["mode"] == "soft" else cap_day
        items.append({**acc, "plan": plan, "cap_day": cap_day, "cap_week": cap_week, "hard_cap": hard,
                      "over_day": cap_day is not None and acc["turns_today"] > cap_day,
                      "over_week": cap_week is not None and acc["turns_week"] > cap_week})
    items.sort(key=lambda i: (-i["turns_today"], i["account"]))
    return {"tz": "Europe/Zurich", "mode": cfg["mode"], "limits": {k: cfg.get(k) for k in LIMIT_KEYS},
            "day": today.isoformat(), "resets_at": d1.astimezone(ZURICH).isoformat(), "week_start": monday.isoformat(),
            "days": days, "items": items, "by_day": dict(sorted(by_day.items())),
            "totals": {"accounts": len(items), "turns_today": sum(i["turns_today"] for i in items),
                       "over_day": sum(1 for i in items if i["over_day"]),
                       "over_week": sum(1 for i in items if i["over_week"])}}


def enabled_flags(config: Any) -> list[str]:
    """Drapeaux allumés de la config : champs booléens vrais, et blocs (`{enabled: true}`) allumés. Les NOMS seulement."""
    out = []
    for f in dataclasses.fields(config):
        v = getattr(config, f.name)
        if v is True or isinstance(v, dict) and v.get("enabled") is True:
            out.append(f.name)
    return sorted(out)


def now_utc() -> datetime:
    return datetime.now(UTC)


__all__ = ["MAX_DAYS", "MemoryOverview", "Overview", "PgOverview", "accounts_view", "enabled_flags", "mask_email",
           "usage_view"]
