"""Connexions Postgres du noyau (docs/TENANCY.md §4) : le locataire courant devient `app.tenant_id`, que lisent les
politiques RLS de `migrations/006_tenant_rls.sql`.

- `connect(dsn)` : connexion courte, `app.tenant_id` posé à l'ouverture. Hors d'un `tenant_scope` : `NoTenant`.
- `TenantConn` : connexion longue des stores (un seul objet pour tous les locataires). Le locataire est reposé à
  CHAQUE usage (`get()`) : figé à l'ouverture, il servirait les données du premier locataire à tous les autres.
- `connect_admin(dsn)` : sans locataire. Migrations (avec le DSN propriétaire, `owner_dsn`) et, avec le DSN du noyau,
  les tables hors RLS (`tenants`, tables d'authentification de `nokker/approvals`). Une requête de contenu par cette
  connexion ne voit rien : `app.tenant_id` absent, la politique est fausse.
- `migrate(dsn, *sqls)` : migrations idempotentes en propriétaire. `NOKKER_PG_OWNER_DSN` s'il est posé (bêta OVH : le pod
  ne tient que `nokker_app`, le job d'init migre), sinon le DSN donné (canari : un seul rôle, propriétaire). Sans les
  droits, log et on continue : le job d'init s'en est chargé.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import psycopg

from nokker.tenancy.context import INTERNAL_TENANT, current_tenant

logger = logging.getLogger("nokker.store.conn")

OWNER_DSN_ENV = "NOKKER_PG_OWNER_DSN"
RLS_MIGRATION = Path(__file__).parent / "migrations" / "006_tenant_rls.sql"
_MIGRATION_LOCK = 727006  # verrou consultatif : deux processus qui migrent en même temps ne se marchent pas dessus
_SET_TENANT = "SELECT set_config('app.tenant_id', %s, false)"


def connect(dsn: str, **kw: Any) -> psycopg.Connection:
    """`psycopg.connect` (mêmes kwargs) + `app.tenant_id` = locataire courant, pour toute la session."""
    tenant = current_tenant()  # NoTenant AVANT d'ouvrir quoi que ce soit
    conn = psycopg.connect(dsn, **kw)
    try:
        conn.execute(_SET_TENANT, (tenant,))
        if not conn.autocommit:
            conn.commit()  # set_config dans une transaction annulée serait annulé avec elle
    except BaseException:
        conn.close()
        raise
    return conn


def connect_admin(dsn: str, **kw: Any) -> psycopg.Connection:
    """Connexion sans locataire : migrations, administration, tables hors RLS. Jamais pour du contenu."""
    return psycopg.connect(dsn, **kw)


class TenantConn:
    """Connexion longue d'un store partagé par tous les locataires (autocommit). `get()` repose `app.tenant_id` =
    locataire courant avant de rendre la connexion, sans cache. L'appelant tient le verrou du store pendant l'usage."""

    def __init__(self, dsn: str, **kw: Any) -> None:
        self._conn = psycopg.connect(dsn, **{**kw, "autocommit": True})

    def get(self) -> psycopg.Connection:
        self._conn.execute(_SET_TENANT, (current_tenant(),))
        return self._conn

    def close(self) -> None:
        self._conn.close()


def owner_dsn(dsn: str) -> str:
    return os.environ.get(OWNER_DSN_ENV) or dsn


def migrate(dsn: str, *sqls: str, rls: bool = True, strict: bool = False) -> bool:
    """Applique `sqls` (idempotents) dans l'ordre, puis la RLS (`006`, si `rls`) : une table de contenu n'existe
    jamais sans sa politique, même créée par un store avant `apply_migrations`. Renvoie False si le rôle n'a pas les
    droits (sauf `strict`, qui lève : job d'init)."""
    if rls:
        sqls = (*sqls, RLS_MIGRATION.read_text(encoding="utf-8"))
    try:
        with connect_admin(owner_dsn(dsn), autocommit=True) as conn:
            conn.execute("SELECT pg_advisory_lock(%s)", (_MIGRATION_LOCK,))
            try:
                for sql in sqls:
                    conn.execute(sql)  # un fichier = une transaction implicite (protocole simple)
            finally:
                conn.execute("SELECT pg_advisory_unlock(%s)", (_MIGRATION_LOCK,))
    except psycopg.errors.InsufficientPrivilege as e:
        if strict:
            raise
        logger.warning("migrations non appliquées (rôle sans droits, %s absent ?) : %s — le job d'init doit les "
                       "avoir faites", OWNER_DSN_ENV, str(e).splitlines()[0])
        return False
    return True


def known_tenants(dsn: str) -> list[str]:
    """Locataires connus (`tenants`, hors RLS) + l'interne : les tâches de fond passent sur chacun dans son
    `tenant_scope`, jamais par une valeur joker dans la politique."""
    with connect_admin(dsn) as conn:
        if conn.execute("SELECT to_regclass('tenants')").fetchone()[0] is None:
            return [INTERNAL_TENANT]
        rows = conn.execute("SELECT id FROM tenants ORDER BY id").fetchall()
    return [INTERNAL_TENANT, *(r[0] for r in rows if r[0] != INTERNAL_TENANT)]


__all__ = ["OWNER_DSN_ENV", "RLS_MIGRATION", "TenantConn", "connect", "connect_admin", "known_tenants", "migrate",
           "owner_dsn"]


def role_bypasses_rls(dsn: str) -> bool:
    """Le rôle de `dsn` échappe-t-il à la RLS ? Un superutilisateur ou un rôle BYPASSRLS ignore toute politique, même
    FORCE (vu sur la copie du canari le 21.10.2026 : rôle `nokker` superutilisateur → un testeur y voyait tout)."""
    with psycopg.connect(dsn) as conn:
        row = conn.execute("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user").fetchone()
    return bool(row and row[0])
