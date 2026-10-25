"""Stockage des sources d'alerte, des signatures reçues et des incidents : mémoire (rangé par locataire courant) ou
Postgres (`migrations/029_plan_operate.sql`, RLS forcée par `tenant_id`). Les règles vivent dans `service.Operate`."""
from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from nokker.incidents.types import LIVE_STATES, AlertSource, Incident
from nokker.tenancy.context import current_tenant


class BaseIncidents:
    # sources
    def put_source(self, s: AlertSource) -> None: raise NotImplementedError
    def get_source(self, source_id: str) -> AlertSource | None: raise NotImplementedError
    def list_sources(self) -> list[AlertSource]: raise NotImplementedError
    def delete_source(self, source_id: str) -> None: raise NotImplementedError
    def tenant_of_source(self, source_id: str) -> str | None:
        """Locataire d'une source, sans identité (route signée) ; None = à chercher locataire par locataire."""
        return None

    # anti-rejeu
    def seen(self, source_id: str, sig: str, expires_at: float, now: float) -> bool:
        """True si `sig` a déjà été reçue pour cette source (rejeu) ; sinon la note et rend False."""
        raise NotImplementedError

    # incidents
    def put(self, i: Incident) -> None: raise NotImplementedError
    def get(self, incident_id: str) -> Incident | None: raise NotImplementedError
    def list(self, state: str | None = None) -> list[Incident]: raise NotImplementedError
    def live_by_fingerprint(self, source_id: str, fingerprint: str) -> Incident | None: raise NotImplementedError
    def by_run(self, run_id: str) -> Incident | None: raise NotImplementedError


@dataclass
class MemoryIncidents(BaseIncidents):
    _sources: dict[str, dict[str, AlertSource]] = field(default_factory=dict)
    _seen: dict[str, dict[tuple[str, str], float]] = field(default_factory=dict)
    _incidents: dict[str, dict[str, Incident]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _t(self, d: dict[str, Any]) -> Any:
        return d.setdefault(current_tenant(), {})

    def put_source(self, s: AlertSource) -> None:
        with self._lock:
            self._t(self._sources)[s.id] = s

    def get_source(self, source_id: str) -> AlertSource | None:
        return self._t(self._sources).get(source_id)

    def list_sources(self) -> list[AlertSource]:
        return sorted(self._t(self._sources).values(), key=lambda s: s.created_at)

    def delete_source(self, source_id: str) -> None:
        with self._lock:
            self._t(self._sources).pop(source_id, None)

    def tenant_of_source(self, source_id: str) -> str | None:
        return next((tid for tid, d in self._sources.items() if source_id in d), None)

    def seen(self, source_id: str, sig: str, expires_at: float, now: float) -> bool:
        with self._lock:
            d = self._t(self._seen)
            for k in [k for k, exp in d.items() if exp < now]:
                del d[k]
            if (source_id, sig) in d:
                return True
            d[(source_id, sig)] = expires_at
            return False

    def put(self, i: Incident) -> None:
        with self._lock:
            self._t(self._incidents)[i.id] = i

    def get(self, incident_id: str) -> Incident | None:
        return self._t(self._incidents).get(incident_id)

    def list(self, state: str | None = None) -> list[Incident]:
        return [i for i in self._t(self._incidents).values() if state is None or i.state == state]

    def live_by_fingerprint(self, source_id: str, fingerprint: str) -> Incident | None:
        return next((i for i in self._t(self._incidents).values() if i.source_id == source_id
                     and i.fingerprint == fingerprint and i.state in LIVE_STATES), None)

    def by_run(self, run_id: str) -> Incident | None:
        return next((i for i in self._t(self._incidents).values()
                     if any(r.get("run_id") == run_id for r in i.runs)), None)


MIGRATION = Path(__file__).resolve().parent.parent / "store" / "migrations" / "029_plan_operate.sql"
_INC_COLS = [f.name for f in fields(Incident)]
_SRC_COLS = [f.name for f in fields(AlertSource)]
_JSON = {"labels", "runs", "timeline"}


@dataclass
class PgIncidents(BaseIncidents):
    dsn: str = ""
    migrate_now: bool = True
    clock: Callable[[], float] | None = None

    def __post_init__(self) -> None:
        from nokker.store.conn import TenantConn, migrate

        if not self.dsn:
            raise ValueError("PgIncidents requiert un DSN Postgres")
        if self.migrate_now:
            migrate(self.dsn, MIGRATION.read_text(encoding="utf-8"))
        self._db = TenantConn(self.dsn)
        self._lock = threading.Lock()

    def close(self) -> None:
        self._db.close()

    def _all(self, sql: str, args: tuple | list = ()) -> list[tuple]:
        with self._lock, self._db.get().cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchall() if cur.description else []

    def _exec(self, sql: str, args: tuple | list = ()) -> int:
        with self._lock, self._db.get().cursor() as cur:
            cur.execute(sql, args)
            return cur.rowcount

    @staticmethod
    def _upsert(table: str, cols: list[str]) -> str:
        sets = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c not in ("id", "created_at", "created_by"))
        return (f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) "
                f"ON CONFLICT (id) DO UPDATE SET {sets}")

    # sources
    def put_source(self, s: AlertSource) -> None:
        self._exec(self._upsert("nokker_alert_sources", _SRC_COLS), [getattr(s, c) for c in _SRC_COLS])

    def get_source(self, source_id: str) -> AlertSource | None:
        rows = self._all(f"SELECT {', '.join(_SRC_COLS)} FROM nokker_alert_sources WHERE id = %s", (source_id,))
        return AlertSource(**dict(zip(_SRC_COLS, rows[0], strict=True))) if rows else None

    def list_sources(self) -> list[AlertSource]:
        rows = self._all(f"SELECT {', '.join(_SRC_COLS)} FROM nokker_alert_sources ORDER BY created_at")
        return [AlertSource(**dict(zip(_SRC_COLS, r, strict=True))) for r in rows]

    def delete_source(self, source_id: str) -> None:
        self._exec("DELETE FROM nokker_alert_sources WHERE id = %s", (source_id,))
        self._exec("DELETE FROM nokker_alert_seen WHERE source_id = %s", (source_id,))

    def seen(self, source_id: str, sig: str, expires_at: float, now: float) -> bool:
        self._exec("DELETE FROM nokker_alert_seen WHERE expires_at < %s", (now,))
        n = self._exec("INSERT INTO nokker_alert_seen (source_id, sig, expires_at) VALUES (%s, %s, %s) "
                       "ON CONFLICT DO NOTHING", (source_id, sig, expires_at))
        return n == 0

    # incidents
    def _row(self, r: tuple) -> Incident:
        d = dict(zip(_INC_COLS, r, strict=True))
        d["labels"] = dict(d["labels"] or {})
        d["runs"] = list(d["runs"] or [])
        d["timeline"] = list(d["timeline"] or [])
        return Incident(**d)

    def put(self, i: Incident) -> None:
        from psycopg.types.json import Jsonb

        vals = [Jsonb(getattr(i, c)) if c in _JSON else getattr(i, c) for c in _INC_COLS]
        self._exec(self._upsert("nokker_incidents", _INC_COLS), vals)

    def get(self, incident_id: str) -> Incident | None:
        rows = self._all(f"SELECT {', '.join(_INC_COLS)} FROM nokker_incidents WHERE id = %s", (incident_id,))
        return self._row(rows[0]) if rows else None

    def list(self, state: str | None = None) -> list[Incident]:
        if state is None:
            rows = self._all(f"SELECT {', '.join(_INC_COLS)} FROM nokker_incidents")
        else:
            rows = self._all(f"SELECT {', '.join(_INC_COLS)} FROM nokker_incidents WHERE state = %s", (state,))
        return [self._row(r) for r in rows]

    def live_by_fingerprint(self, source_id: str, fingerprint: str) -> Incident | None:
        rows = self._all(f"SELECT {', '.join(_INC_COLS)} FROM nokker_incidents WHERE source_id = %s AND "
                         "fingerprint = %s AND state <> 'resolved' LIMIT 1", (source_id, fingerprint))
        return self._row(rows[0]) if rows else None

    def by_run(self, run_id: str) -> Incident | None:
        from psycopg.types.json import Jsonb

        rows = self._all(f"SELECT {', '.join(_INC_COLS)} FROM nokker_incidents WHERE runs @> %s LIMIT 1",
                         (Jsonb([{"run_id": run_id}]),))
        return self._row(rows[0]) if rows else None


__all__ = ["BaseIncidents", "MemoryIncidents", "PgIncidents"]
