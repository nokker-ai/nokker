"""Backends Postgres (psycopg 3) de `EpisodeStore` et `ReservoirStore` — tables de `migrations/002_context.sql`.
Même contrat que `nokker/context/memory.py` (le test `tests/unit/context/test_store_contract.py` les parcourt)."""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from nokker.context.types import DaySummary, Episode, ReservoirItem, ViewState
from nokker.store.conn import TenantConn, migrate

MIGRATION_SQL = (Path(__file__).parent.parent / "store" / "migrations" / "002_context.sql").read_text(
    encoding="utf-8")

_EP_COLS = ("id, thread_id, seq, first_turn, last_turn, title, summary, keywords, raw_tokens, summary_tokens, "
            "opened_at, closed_at, closed_reason, day, turn_ids_sha256, refined, bubble_id, version")
_DAY_COLS = "id, thread_id, day, summary, episode_ids, tokens, created_at"
_ST_COLS = ("thread_id, episode_id, base_turn, last_turn_at, last_turn_index, tokens_since_compaction, "
            "compaction_version, shed, pinned_episodes, updated_at, heads_shed_before")
_RS_COLS = "id, thread_id, kind, name, content, preview, tokens, turn_index, call_id, created_at, origin"


def _episode(r: tuple) -> Episode:
    (eid, thread_id, seq, first_turn, last_turn, title, summary, keywords, raw_tokens, summary_tokens, opened_at,
     closed_at, closed_reason, day, sha, refined, bubble_id, version) = r
    return Episode(id=eid, thread_id=thread_id, seq=seq, first_turn=first_turn, last_turn=last_turn, title=title,
                   summary=summary, keywords=list(keywords or []), raw_tokens=raw_tokens,
                   summary_tokens=summary_tokens, opened_at=opened_at, closed_at=closed_at,
                   closed_reason=closed_reason, day=day, turn_ids_sha256=sha, refined=refined,
                   bubble_id=bubble_id, version=version)


def _day(r: tuple) -> DaySummary:
    did, thread_id, day, summary, episode_ids, tokens, created_at = r
    return DaySummary(id=did, thread_id=thread_id, day=day, summary=summary, episode_ids=list(episode_ids or []),
                      tokens=tokens, created_at=created_at)


def _state(r: tuple) -> ViewState:
    (thread_id, episode_id, base_turn, last_turn_at, last_turn_index, tokens_since, comp_version, shed, pinned,
     updated_at, heads_shed_before) = r
    return ViewState(thread_id=thread_id, episode_id=episode_id, base_turn=base_turn, last_turn_at=last_turn_at,
                     last_turn_index=last_turn_index, tokens_since_compaction=tokens_since,
                     compaction_version=comp_version, shed=dict(shed or {}), pinned_episodes=list(pinned or []),
                     updated_at=updated_at, heads_shed_before=heads_shed_before or 0)


def _item(r: tuple) -> ReservoirItem:
    (iid, thread_id, kind, name, content, preview, tokens, turn_index, call_id, created_at, origin) = r
    return ReservoirItem(id=iid, thread_id=thread_id, kind=kind, name=name, content=content, preview=preview,
                         tokens=tokens, turn_index=turn_index, call_id=call_id, created_at=created_at,
                         origin=dict(origin) if origin is not None else None)


class _PgBase:
    def __init__(self, dsn: str) -> None:
        if not dsn:
            raise ValueError(f"{type(self).__name__} requiert un DSN Postgres")
        self.dsn = dsn
        migrate(dsn, MIGRATION_SQL)  # en propriétaire sous verrou consultatif, puis la RLS (nokker/store/conn.py)
        self._db = TenantConn(dsn)
        self._lock = threading.Lock()

    @property
    def _conn(self) -> Any:
        return self._db.get()  # app.tenant_id = locataire courant, reposé à chaque usage (NoTenant hors scope)

    def close(self) -> None:
        self._db.close()

    def _one(self, sql: str, params: tuple) -> tuple | None:
        with self._lock, self._conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()

    def _all(self, sql: str, params: tuple) -> list[tuple]:
        with self._lock, self._conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()


class PgEpisodeStore(_PgBase):
    def current(self, thread_id: str) -> Episode | None:
        r = self._one(f"SELECT {_EP_COLS} FROM nokker_episodes WHERE thread_id = %s AND closed_at IS NULL "
                      "ORDER BY seq DESC LIMIT 1", (thread_id,))
        return _episode(r) if r else None

    def get(self, episode_id: str) -> Episode | None:
        r = self._one(f"SELECT {_EP_COLS} FROM nokker_episodes WHERE id = %s", (episode_id,))
        return _episode(r) if r else None

    def list(self, thread_id: str, *, closed_only: bool = False) -> list[Episode]:
        where = " AND closed_at IS NOT NULL" if closed_only else ""
        rows = self._all(f"SELECT {_EP_COLS} FROM nokker_episodes WHERE thread_id = %s{where} ORDER BY seq, id",
                         (thread_id,))
        return [_episode(r) for r in rows]

    def save(self, episode: Episode) -> None:
        vals = (episode.id, episode.thread_id, episode.seq, episode.first_turn, episode.last_turn, episode.title,
                episode.summary, Jsonb(list(episode.keywords)), episode.raw_tokens, episode.summary_tokens,
                episode.opened_at, episode.closed_at, episode.closed_reason, episode.day,
                episode.turn_ids_sha256, episode.refined, episode.bubble_id, episode.version)
        sets = ", ".join(f"{c} = EXCLUDED.{c}" for c in _EP_COLS.split(", ")[1:-1])
        # insert : version telle que fournie ; update : version de la ligne + 1 (reportée sur l'objet, comme en mémoire)
        r = self._one(f"INSERT INTO nokker_episodes ({_EP_COLS}) VALUES ({', '.join(['%s'] * 18)}) "
                      f"ON CONFLICT (id) DO UPDATE SET {sets}, version = nokker_episodes.version + 1 "
                      "RETURNING version", vals)
        if r:
            episode.version = r[0]

    def next_seq(self, thread_id: str) -> int:
        r = self._one("SELECT COALESCE(MAX(seq) + 1, 0) FROM nokker_episodes WHERE thread_id = %s", (thread_id,))
        return int(r[0]) if r else 0

    def get_day(self, thread_id: str, day: str) -> DaySummary | None:
        r = self._one(f"SELECT {_DAY_COLS} FROM nokker_day_summaries WHERE thread_id = %s AND day = %s",
                      (thread_id, day))
        return _day(r) if r else None

    def list_days(self, thread_id: str) -> list[DaySummary]:
        rows = self._all(f"SELECT {_DAY_COLS} FROM nokker_day_summaries WHERE thread_id = %s ORDER BY day",
                         (thread_id,))
        return [_day(r) for r in rows]

    def save_day(self, day: DaySummary) -> None:
        self._one("INSERT INTO nokker_day_summaries (id, thread_id, day, summary, episode_ids, tokens, created_at) "
                  "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (thread_id, day) DO UPDATE SET "
                  "id = EXCLUDED.id, summary = EXCLUDED.summary, episode_ids = EXCLUDED.episode_ids, "
                  "tokens = EXCLUDED.tokens, created_at = EXCLUDED.created_at RETURNING id",
                  (day.id, day.thread_id, day.day, day.summary, Jsonb(list(day.episode_ids)), day.tokens,
                   day.created_at))

    def threads_with_open_episode(self) -> list[str]:
        rows = self._all("SELECT thread_id FROM nokker_episodes WHERE closed_at IS NULL "
                         "GROUP BY thread_id ORDER BY thread_id COLLATE \"C\"", ())
        return [r[0] for r in rows]

    def get_state(self, thread_id: str) -> ViewState | None:
        r = self._one(f"SELECT {_ST_COLS} FROM nokker_view_state WHERE thread_id = %s", (thread_id,))
        return _state(r) if r else None

    def save_state(self, state: ViewState) -> None:
        cols = _ST_COLS.split(", ")
        sets = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols[1:])
        self._one(f"INSERT INTO nokker_view_state ({_ST_COLS}) VALUES ({', '.join(['%s'] * 11)}) "
                  f"ON CONFLICT (thread_id) DO UPDATE SET {sets} RETURNING thread_id",
                  (state.thread_id, state.episode_id, state.base_turn, state.last_turn_at, state.last_turn_index,
                   state.tokens_since_compaction, state.compaction_version, Jsonb(dict(state.shed)),
                   Jsonb(list(state.pinned_episodes)), state.updated_at, state.heads_shed_before))


class PgReservoirStore(_PgBase):
    def put(self, item: ReservoirItem) -> None:
        cols = _RS_COLS.split(", ")
        sets = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols[1:])
        self._one(f"INSERT INTO nokker_reservoir ({_RS_COLS}) VALUES ({', '.join(['%s'] * 11)}) "
                  f"ON CONFLICT (id) DO UPDATE SET {sets} RETURNING id",
                  (item.id, item.thread_id, item.kind, item.name, item.content, item.preview, item.tokens,
                   item.turn_index, item.call_id, item.created_at,
                   Jsonb(item.origin) if item.origin is not None else None))

    def get(self, item_id: str) -> ReservoirItem | None:
        r = self._one(f"SELECT {_RS_COLS} FROM nokker_reservoir WHERE id = %s", (item_id,))
        return _item(r) if r else None

    def list(self, thread_id: str, *, kind: str | None = None, limit: int = 200) -> list[ReservoirItem]:
        if limit <= 0:
            return []
        # les `limit` DERNIERS (created_at, id), rendus en ordre croissant — comme `items[-limit:]` en mémoire
        # (id en collation « C » : même tri que Python)
        kind_sql, params = ("", (thread_id,)) if kind is None else (" AND kind = %s", (thread_id, kind))
        rows = self._all(f"SELECT {_RS_COLS} FROM (SELECT {_RS_COLS} FROM nokker_reservoir WHERE thread_id = %s"
                         f"{kind_sql} ORDER BY created_at DESC, id COLLATE \"C\" DESC LIMIT %s) t "
                         "ORDER BY created_at, id COLLATE \"C\"", (*params, limit))
        return [_item(r) for r in rows]

    def find(self, thread_id: str, turn_index: int, call_id: str) -> ReservoirItem | None:
        r = self._one(f"SELECT {_RS_COLS} FROM nokker_reservoir WHERE thread_id = %s AND turn_index = %s "
                      "AND call_id = %s ORDER BY created_at, id COLLATE \"C\" LIMIT 1",
                      (thread_id, turn_index, call_id))
        return _item(r) if r else None


__all__ = ["PgEpisodeStore", "PgReservoirStore"]
