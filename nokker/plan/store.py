"""Stockage du plan : mémoire (rangé par locataire courant) ou Postgres (`migrations/029_plan_operate.sql`, RLS forcée
par `tenant_id`). Les règles (hiérarchie, droits, avancement) vivent dans `service.Plan`."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from nokker.plan.types import Card, Proposal
from nokker.tenancy.context import current_tenant


class BasePlan:
    def put(self, c: Card) -> None: raise NotImplementedError
    def get(self, card_id: str) -> Card | None: raise NotImplementedError
    def all(self) -> list[Card]: raise NotImplementedError
    def delete(self, card_id: str) -> None: raise NotImplementedError
    def put_proposal(self, p: Proposal) -> None: raise NotImplementedError
    def get_proposal(self, proposal_id: str) -> Proposal | None: raise NotImplementedError
    def proposals(self, card_id: str | None = None) -> list[Proposal]: raise NotImplementedError
    def brief_agent(self, owner: str) -> str | None: raise NotImplementedError
    def set_brief(self, owner: str, agent_id: str | None, at: float) -> None: raise NotImplementedError
    def brief_owner(self, agent_id: str) -> str | None: raise NotImplementedError


@dataclass
class MemoryPlan(BasePlan):
    _cards: dict[str, dict[str, Card]] = field(default_factory=dict)
    _props: dict[str, dict[str, Proposal]] = field(default_factory=dict)
    _briefs: dict[str, dict[str, str]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _t(self, d: dict[str, Any]) -> Any:
        return d.setdefault(current_tenant(), {})

    def put(self, c: Card) -> None:
        with self._lock:
            self._t(self._cards)[c.id] = c

    def get(self, card_id: str) -> Card | None:
        return self._t(self._cards).get(card_id)

    def all(self) -> list[Card]:
        return list(self._t(self._cards).values())

    def delete(self, card_id: str) -> None:
        with self._lock:
            self._t(self._cards).pop(card_id, None)

    def put_proposal(self, p: Proposal) -> None:
        with self._lock:
            self._t(self._props)[p.id] = p

    def get_proposal(self, proposal_id: str) -> Proposal | None:
        return self._t(self._props).get(proposal_id)

    def proposals(self, card_id: str | None = None) -> list[Proposal]:
        return [p for p in self._t(self._props).values() if card_id is None or p.card_id == card_id]

    def brief_agent(self, owner: str) -> str | None:
        return self._t(self._briefs).get(owner)

    def set_brief(self, owner: str, agent_id: str | None, at: float) -> None:
        with self._lock:
            d = self._t(self._briefs)
            if agent_id is None:
                d.pop(owner, None)
            else:
                d[owner] = agent_id

    def brief_owner(self, agent_id: str) -> str | None:
        return next((o for o, a in self._t(self._briefs).items() if a == agent_id), None)


MIGRATION = Path(__file__).resolve().parent.parent / "store" / "migrations" / "029_plan_operate.sql"
_CARD_COLS = [f.name for f in fields(Card)]
_PROP_COLS = [f.name for f in fields(Proposal)]
_CARD_JSON = {"constraints", "decisions", "agents", "threads"}
_PROP_JSON = {"children", "created"}


@dataclass
class PgPlan(BasePlan):
    dsn: str = ""
    migrate_now: bool = True

    def __post_init__(self) -> None:
        from nokker.store.conn import TenantConn, migrate

        if not self.dsn:
            raise ValueError("PgPlan requiert un DSN Postgres")
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

    def _exec(self, sql: str, args: tuple | list = ()) -> None:
        with self._lock, self._db.get().cursor() as cur:
            cur.execute(sql, args)

    @staticmethod
    def _upsert(table: str, cols: list[str]) -> str:
        sets = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c not in ("id", "created_at", "created_by"))
        return (f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) "
                f"ON CONFLICT (id) DO UPDATE SET {sets}")

    @staticmethod
    def _vals(obj: Any, cols: list[str], js: set[str]) -> list[Any]:
        from psycopg.types.json import Jsonb

        return [Jsonb(getattr(obj, c)) if c in js else getattr(obj, c) for c in cols]

    @staticmethod
    def _card(r: tuple) -> Card:
        d = dict(zip(_CARD_COLS, r, strict=True))
        for k in _CARD_JSON:
            d[k] = list(d[k] or [])
        return Card(**d)

    @staticmethod
    def _prop(r: tuple) -> Proposal:
        d = dict(zip(_PROP_COLS, r, strict=True))
        for k in _PROP_JSON:
            d[k] = list(d[k] or [])
        return Proposal(**d)

    def put(self, c: Card) -> None:
        self._exec(self._upsert("nokker_plan_cards", _CARD_COLS), self._vals(c, _CARD_COLS, _CARD_JSON))

    def get(self, card_id: str) -> Card | None:
        rows = self._all(f"SELECT {', '.join(_CARD_COLS)} FROM nokker_plan_cards WHERE id = %s", (card_id,))
        return self._card(rows[0]) if rows else None

    def all(self) -> list[Card]:
        return [self._card(r) for r in self._all(f"SELECT {', '.join(_CARD_COLS)} FROM nokker_plan_cards")]

    def delete(self, card_id: str) -> None:
        self._exec("DELETE FROM nokker_plan_cards WHERE id = %s", (card_id,))

    def put_proposal(self, p: Proposal) -> None:
        self._exec(self._upsert("nokker_plan_proposals", _PROP_COLS), self._vals(p, _PROP_COLS, _PROP_JSON))

    def get_proposal(self, proposal_id: str) -> Proposal | None:
        rows = self._all(f"SELECT {', '.join(_PROP_COLS)} FROM nokker_plan_proposals WHERE id = %s", (proposal_id,))
        return self._prop(rows[0]) if rows else None

    def proposals(self, card_id: str | None = None) -> list[Proposal]:
        sql = f"SELECT {', '.join(_PROP_COLS)} FROM nokker_plan_proposals"
        rows = self._all(sql + " WHERE card_id = %s", (card_id,)) if card_id else self._all(sql)
        return [self._prop(r) for r in rows]

    def brief_agent(self, owner: str) -> str | None:
        rows = self._all("SELECT agent_id FROM nokker_plan_briefs WHERE owner = %s", (owner,))
        return rows[0][0] if rows else None

    def set_brief(self, owner: str, agent_id: str | None, at: float) -> None:
        if agent_id is None:
            self._exec("DELETE FROM nokker_plan_briefs WHERE owner = %s", (owner,))
            return
        self._exec("INSERT INTO nokker_plan_briefs (owner, agent_id, created_at) VALUES (%s, %s, %s) "
                   "ON CONFLICT (tenant_id, owner) DO UPDATE SET agent_id = EXCLUDED.agent_id", (owner, agent_id, at))

    def brief_owner(self, agent_id: str) -> str | None:
        rows = self._all("SELECT owner FROM nokker_plan_briefs WHERE agent_id = %s", (agent_id,))
        return rows[0][0] if rows else None


__all__ = ["BasePlan", "MemoryPlan", "PgPlan"]
