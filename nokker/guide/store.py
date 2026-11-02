"""Compteurs d'usage du Guide (docs/GUIDE.md §4) : par locataire et par CODE de fonction, jamais de contenu. Mémoire
(dev, Lokkal sans Postgres) ou table `nokker_guide_usage` (migration 062) sous la RLS du locataire courant."""
from __future__ import annotations

import threading
import time
from typing import Any

from nokker.tenancy.context import INTERNAL_TENANT, current_tenant_or_none

INTRO_KEY = "guide.intro"


class MemoryUsage:
    def __init__(self, clock: Any = time.time) -> None:
        self._rows: dict[tuple[str, str], dict[str, float]] = {}
        self._lock = threading.Lock()
        self.clock = clock

    @staticmethod
    def _tenant() -> str:
        return current_tenant_or_none() or INTERNAL_TENANT

    def bump(self, feature_id: str) -> None:
        now = self.clock()
        with self._lock:
            row = self._rows.setdefault((self._tenant(), feature_id), {"uses": 0, "first_at": now, "last_at": now})
            row["uses"] += 1
            row["last_at"] = now

    def counts(self) -> dict[str, int]:
        t = self._tenant()
        with self._lock:
            return {f: int(r["uses"]) for (tn, f), r in self._rows.items() if tn == t}


class PgUsage:
    def __init__(self, dsn: str, clock: Any = time.time) -> None:
        self.dsn = dsn
        self.clock = clock

    def _conn(self):
        from nokker.store.conn import connect

        return connect(self.dsn)

    def bump(self, feature_id: str) -> None:
        now = self.clock()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO nokker_guide_usage (feature_id, uses, first_at, last_at) VALUES (%s, 1, %s, %s) "
                "ON CONFLICT (tenant_id, feature_id) DO UPDATE SET uses = nokker_guide_usage.uses + 1, "
                "last_at = EXCLUDED.last_at", (feature_id, now, now))

    def counts(self) -> dict[str, int]:
        with self._conn() as conn:
            rows = conn.execute("SELECT feature_id, uses FROM nokker_guide_usage").fetchall()
        return {str(f): int(n) for f, n in rows}


__all__ = ["INTRO_KEY", "MemoryUsage", "PgUsage"]
