"""Stockage des sessions de code : mémoire (rangé par locataire courant) ou Postgres (`migrations/038_code_sessions.sql`,
RLS forcée par `tenant_id`). Toute lecture se fait dans le locataire courant ; l'utilisateur est filtré par l'appelant
(`service.CodeSessions`)."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any

from nokker.tenancy.context import current_tenant

MIGRATION = Path(__file__).resolve().parents[1] / "store" / "migrations" / "038_code_sessions.sql"
LIVE = ("active", "idle")


@dataclass(frozen=True)
class CodeSession:
    id: str
    tenant_id: str
    user_id: str
    thread_id: str
    root_rel: str
    mode: str
    status: str = "active"
    repo_url: str | None = None
    repo_ref: str | None = None
    credential: str | None = None  # NOM de l'identifiant de forge au coffre, jamais sa valeur
    created_at: float = 0.0
    last_used_at: float = 0.0
    closed_at: float | None = None


_COLS = [f.name for f in fields(CodeSession)]


class BaseCodeStore:
    def put(self, s: CodeSession) -> None: raise NotImplementedError
    def get(self, session_id: str) -> CodeSession | None: raise NotImplementedError
    def by_thread(self, thread_id: str) -> CodeSession | None: raise NotImplementedError
    def list_user(self, user_id: str, live_only: bool = True) -> list[CodeSession]: raise NotImplementedError
    def delete(self, session_id: str) -> None: raise NotImplementedError

    def count_live(self, user_id: str) -> int:
        return len(self.list_user(user_id, live_only=True))

    def update(self, session_id: str, **changes: Any) -> CodeSession | None:
        s = self.get(session_id)
        if s is None:
            return None
        s = replace(s, **changes)
        self.put(s)
        return s

    def close(self) -> None:
        return None


@dataclass
class MemoryCodeStore(BaseCodeStore):
    _rows: dict[str, dict[str, CodeSession]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _t(self) -> dict[str, CodeSession]:
        return self._rows.setdefault(current_tenant(), {})

    def put(self, s: CodeSession) -> None:
        t = self._t()
        if s.tenant_id != current_tenant():
            raise PermissionError("session d'un autre locataire")
        with self._lock:
            t[s.id] = s

    def get(self, session_id: str) -> CodeSession | None:
        return self._t().get(session_id)

    def by_thread(self, thread_id: str) -> CodeSession | None:
        return next((s for s in list(self._t().values()) if s.thread_id == thread_id), None)

    def list_user(self, user_id: str, live_only: bool = True) -> list[CodeSession]:
        out = [s for s in list(self._t().values()) if s.user_id == user_id and (not live_only or s.status in LIVE)]
        return sorted(out, key=lambda s: (s.created_at, s.id))

    def delete(self, session_id: str) -> None:
        with self._lock:
            self._t().pop(session_id, None)


@dataclass
class PgCodeStore(BaseCodeStore):
    dsn: str = ""
    migrate_now: bool = True

    def __post_init__(self) -> None:
        from nokker.store.conn import TenantConn, migrate

        if not self.dsn:
            raise ValueError("PgCodeStore requiert un DSN Postgres")
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

    def _row(self, r: tuple) -> CodeSession:
        return CodeSession(**dict(zip(_COLS, r, strict=True)))

    def put(self, s: CodeSession) -> None:
        sets = ", ".join(f"{c} = EXCLUDED.{c}" for c in _COLS if c not in ("id", "tenant_id", "created_at"))
        self._all(f"INSERT INTO nokker_code_sessions ({', '.join(_COLS)}) VALUES ({', '.join(['%s'] * len(_COLS))}) "
                  f"ON CONFLICT (id) DO UPDATE SET {sets}", [getattr(s, c) for c in _COLS])

    def get(self, session_id: str) -> CodeSession | None:
        rows = self._all(f"SELECT {', '.join(_COLS)} FROM nokker_code_sessions WHERE id = %s", (session_id,))
        return self._row(rows[0]) if rows else None

    def by_thread(self, thread_id: str) -> CodeSession | None:
        rows = self._all(f"SELECT {', '.join(_COLS)} FROM nokker_code_sessions WHERE thread_id = %s", (thread_id,))
        return self._row(rows[0]) if rows else None

    def list_user(self, user_id: str, live_only: bool = True) -> list[CodeSession]:
        cond = " AND status IN ('active', 'idle')" if live_only else ""
        rows = self._all(f"SELECT {', '.join(_COLS)} FROM nokker_code_sessions WHERE user_id = %s{cond} "
                         "ORDER BY created_at, id", (user_id,))
        return [self._row(r) for r in rows]

    def delete(self, session_id: str) -> None:
        self._all("DELETE FROM nokker_code_sessions WHERE id = %s", (session_id,))


__all__ = ["BaseCodeStore", "CodeSession", "LIVE", "MemoryCodeStore", "PgCodeStore"]
