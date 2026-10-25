"""`PgPackStore` — packs installés par les comptes de nokker.ai (`nokker_pack_installs`, migration 036), sous RLS : chaque
requête porte le locataire (`app.tenant_id`), même interface que `MemoryPackStore`."""
from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb

from nokker.webpacks.service import PackInstall

_COLS = "account_id, pack, version, skills, templates, machine, bind, proposed, installed_at"


def _tq(dsn: str, tenant: str, sql: str, params: tuple = ()) -> list[tuple]:
    from nokker.store.conn import connect
    from nokker.tenancy.context import tenant_scope

    with tenant_scope(tenant), connect(dsn, autocommit=True) as conn:
        cur = conn.execute(sql, params)
        return cur.fetchall() if cur.description else []


def _row(r: tuple) -> PackInstall:
    account, pack, version, skills, templates, machine, bind, proposed, installed_at = r
    return PackInstall(account=account, pack=pack, version=int(version), skills=list(skills or []),
                       templates=list(templates or []), machine=dict(machine or {}), bind=dict(bind or {}),
                       proposed=list(proposed or []), installed_at=float(installed_at))


class PgPackStore:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def get(self, tenant: str, account: str, pack: str) -> PackInstall | None:
        rows = _tq(self.dsn, tenant, f"SELECT {_COLS} FROM nokker_pack_installs WHERE account_id = %s AND pack = %s",
                   (account, pack))
        return _row(rows[0]) if rows else None

    def list(self, tenant: str, account: str) -> list[PackInstall]:
        return [_row(r) for r in _tq(self.dsn, tenant, f"SELECT {_COLS} FROM nokker_pack_installs WHERE account_id = %s "
                                                       "ORDER BY pack", (account,))]

    def put(self, tenant: str, rec: PackInstall) -> None:
        vals: tuple[Any, ...] = (rec.account, rec.pack, rec.version, Jsonb(rec.skills), Jsonb(rec.templates),
                                 Jsonb(rec.machine), Jsonb(rec.bind), Jsonb(rec.proposed), rec.installed_at)
        _tq(self.dsn, tenant,
            f"INSERT INTO nokker_pack_installs ({_COLS}) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (tenant_id, account_id, pack) DO UPDATE SET version = EXCLUDED.version, "
            "skills = EXCLUDED.skills, templates = EXCLUDED.templates, machine = EXCLUDED.machine, "
            "bind = EXCLUDED.bind, proposed = EXCLUDED.proposed, installed_at = EXCLUDED.installed_at", vals)

    def delete(self, tenant: str, account: str, pack: str) -> bool:
        return bool(_tq(self.dsn, tenant, "DELETE FROM nokker_pack_installs WHERE account_id = %s AND pack = %s "
                                          "RETURNING pack", (account, pack)))


__all__ = ["PgPackStore"]
