"""Connexions Postgres du noyau (docs/TENANCY.md §4) : le locataire courant devient `app.tenant_id`, que lisent les
politiques RLS de `migrations/006_tenant_rls.sql`.

- `connect(dsn)` : connexion courte, `app.tenant_id` posé à l'ouverture. Hors d'un `tenant_scope` : `NoTenant`.
- `TenantConn` : connexion longue des stores (un seul objet pour tous les locataires). Le locataire est reposé à
  CHAQUE usage (`get()`) : figé à l'ouverture, il servirait les données du premier locataire à tous les autres.
- `connect_tenant(dsn, tenant)` : `connect` pour un locataire nommé, hors de son scope (webhook, relais, opérateur).
- `connect_key(dsn, clé)` : sans locataire, ouvre la seule ligne qui porte cette clé (empreinte de jeton, code) sur les
  tables cherchées avant le locataire (migration 058, docs/TENANCY.md §4).
- `connect_admin(dsn)` : sans locataire. Migrations (avec le DSN propriétaire, `owner_dsn`) et, avec le DSN du noyau,
  les tables hors RLS (`tenants`, tables d'authentification de `nokker/approvals`). Une requête de contenu par cette
  connexion ne voit rien : `app.tenant_id` absent, la politique est fausse.
- Délai de connexion : `connect_timeout` = 10 s par défaut sur toute ouverture (une base qui ne répond pas ne bloque plus
  le fil jusqu'au délai TCP). Réglable par l'argument `connect_timeout=` ou `NOKKER_PG_CONNECT_TIMEOUT` (secondes) ;
  inchangé si le DSN le porte déjà (ou `PGCONNECT_TIMEOUT`).
- `migrate(dsn, *sqls)` : migrations idempotentes en propriétaire. `NOKKER_PG_OWNER_DSN` s'il est posé (bêta OVH : le pod
  ne tient que `nokker_app`, le job d'init migre), sinon le DSN donné (canari : un seul rôle, propriétaire). Sans les
  droits, log et on continue : le job d'init s'en est chargé.
"""
from __future__ import annotations

import logging
import os
import re
import threading
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql as sql_lib
from psycopg.pq import TransactionStatus
from psycopg.conninfo import conninfo_to_dict

from nokker.tenancy.context import INTERNAL_TENANT, NoTenant, current_tenant

logger = logging.getLogger("nokker.store.conn")

OWNER_DSN_ENV = "NOKKER_PG_OWNER_DSN"
RLS_MIGRATION = Path(__file__).parent / "migrations" / "006_tenant_rls.sql"
LOCK_TIMEOUT_ENV = "NOKKER_MIGRATE_LOCK_TIMEOUT"
_PG_DURATION = re.compile(r"^\d{1,6}\s*(ms|s|min)?$")
_MIGRATION_LOCK = 727006  # verrou consultatif : deux processus qui migrent en même temps ne se marchent pas dessus
CONNECT_TIMEOUT_ENV = "NOKKER_PG_CONNECT_TIMEOUT"
DEFAULT_CONNECT_TIMEOUT = 10
_SET_TENANT = "SELECT set_config('app.tenant_id', %s, false)"


def _open(dsn: str, kw: dict[str, Any]) -> psycopg.Connection:
    """`psycopg.connect` avec un `connect_timeout` par défaut. Un argument `connect_timeout`, un DSN qui le porte déjà
    ou `PGCONNECT_TIMEOUT` (libpq) gardent la main : rien n'est ajouté."""
    if "connect_timeout" not in kw and "PGCONNECT_TIMEOUT" not in os.environ:
        try:
            has_dsn = "connect_timeout" in conninfo_to_dict(dsn or "")
        except psycopg.ProgrammingError:
            has_dsn = True  # DSN illisible : psycopg le dira, on n'y touche pas
        if not has_dsn:
            try:
                delay = int(float(os.environ.get(CONNECT_TIMEOUT_ENV, "")))
            except ValueError:
                delay = DEFAULT_CONNECT_TIMEOUT
            kw = {**kw, "connect_timeout": delay if delay > 0 else DEFAULT_CONNECT_TIMEOUT}
    return psycopg.connect(dsn, **kw)


def connect(dsn: str, **kw: Any) -> psycopg.Connection:
    """`psycopg.connect` (mêmes kwargs) + `app.tenant_id` = locataire courant, pour toute la session."""
    tenant = current_tenant()  # NoTenant AVANT d'ouvrir quoi que ce soit
    conn = _open(dsn, kw)
    try:
        conn.execute(_SET_TENANT, (tenant,))
        if not conn.autocommit:
            conn.commit()  # set_config dans une transaction annulée serait annulé avec elle
    except BaseException:
        conn.close()
        raise
    return conn


_SET_KEY = "SELECT set_config('app.lookup_key', %s, false)"


def connect_tenant(dsn: str, tenant: str, **kw: Any) -> psycopg.Connection:
    """`connect` pour un locataire NOMMÉ (la ligne qu'on écrit ou relit porte son locataire, hors de son scope :
    webhook, relais, outil d'opérateur). Locataire vide : `NoTenant`, jamais de connexion sans locataire."""
    if not tenant:
        raise NoTenant("locataire vide")
    conn = _open(dsn, kw)
    try:
        conn.execute(_SET_TENANT, (tenant,))
        if not conn.autocommit:
            conn.commit()
    except BaseException:
        conn.close()
        raise
    return conn


def connect_key(dsn: str, key: str, **kw: Any) -> psycopg.Connection:
    """Connexion SANS locataire qui ouvre la seule ligne portant `key` (empreinte de jeton, code, identifiant présenté)
    sur les tables cherchées avant de connaître le locataire (migration 058, politiques `key_lookup_*`). Clé vide :
    aucune ligne (la politique lit NULL)."""
    conn = _open(dsn, kw)
    try:
        conn.execute(_SET_KEY, (key or "",))
        if not conn.autocommit:
            conn.commit()
    except BaseException:
        conn.close()
        raise
    return conn


def set_lookup_key(conn: psycopg.Connection, key: str, *, local: bool = False) -> None:
    """Pose (ou change) la clé de lecture d'une connexion ouverte ; `local` : pour la transaction en cours seulement."""
    conn.execute("SELECT set_config('app.lookup_key', %s, %s)", (key or "", local))


def connect_admin(dsn: str, **kw: Any) -> psycopg.Connection:
    """Connexion sans locataire : migrations, administration, tables hors RLS. Jamais pour du contenu."""
    return _open(dsn, kw)


class TenantConn:
    """Connexion longue d'un store partagé par tous les locataires (autocommit). `get()` repose `app.tenant_id` =
    locataire courant avant de rendre la connexion, sans cache. L'appelant tient le verrou du store pendant l'usage.

    Reconnexion (Postgres managé qui redémarre, terminaison par un administrateur, coupure réseau) : UNIQUEMENT dans
    `get()`, à l'acquisition. Le seul ordre rejoué est le `set_config` du locataire, sans effet d'écriture : s'il échoue
    sur une connexion morte, on rouvre UNE connexion, on repose le locataire, on la rend. Une erreur survenue plus tard,
    pendant l'usage de la connexion (ordre en autocommit, transaction explicite en cours), remonte telle quelle et n'est
    JAMAIS rejouée : l'ordre a pu atteindre le serveur. La connexion cassée n'est pas gardée : le `get()` suivant en
    rouvre une. Une seule tentative par `get()` ; si elle échoue, son erreur remonte. Hors de `get()`, ne garder aucune
    référence à la connexion rendue (un autre fil peut avoir reconnecté) : toujours repasser par `get()`."""

    def __init__(self, dsn: str, **kw: Any) -> None:
        self._dsn = dsn
        self._kw = {**kw, "autocommit": True}
        self._lock = threading.RLock()  # reconnexion : un seul fil rouvre, les autres prennent la nouvelle
        self._shut = False
        self._conn = _open(dsn, self._kw)

    def get(self) -> psycopg.Connection:
        arm = self._arm()  # NoTenant AVANT toute E/S (TenantConn) ; rien à poser pour AdminConn
        with self._lock:
            conn = self._conn
            if self._shut:
                conn.execute(*arm)  # fermée par close() : l'erreur d'origine, pas de résurrection
                return conn
            if conn.closed or conn.broken:
                return self._reconnect(arm)  # morte depuis une erreur déjà remontée : rien n'est rejoué
            in_tx = conn.info.transaction_status != TransactionStatus.IDLE
            try:
                conn.execute(*arm)
                return conn
            except (psycopg.OperationalError, psycopg.InterfaceError):
                if in_tx or not (conn.closed or conn.broken):
                    raise  # en milieu de transaction, ou erreur d'une connexion vivante : jamais de rejeu
                return self._reconnect(arm)

    def _arm(self) -> tuple[str, tuple]:
        """Premier ordre de chaque `get()` (le seul qui peut être rejoué : sans écriture)."""
        return _SET_TENANT, (current_tenant(),)

    def _reconnect(self, arm: tuple[str, tuple]) -> psycopg.Connection:
        new = _open(self._dsn, self._kw)
        try:
            new.execute(*arm)
        except BaseException:
            new.close()
            raise
        old, self._conn = self._conn, new
        try:
            old.close()
        except Exception:  # noqa: BLE001 — déjà morte
            pass
        logger.info("connexion Postgres rétablie")
        return new

    def close(self) -> None:
        with self._lock:
            self._shut = True
            self._conn.close()


class AdminConn(TenantConn):
    """Même reconnexion que `TenantConn` pour une connexion longue SANS locataire (tables d'authentification, hors RLS) :
    le premier ordre de chaque `get()` est un `SELECT 1` (aucune écriture, rien de rejoué d'autre)."""

    def _arm(self) -> tuple[str, tuple]:
        return "SELECT 1", ()


def owner_dsn(dsn: str) -> str:
    return os.environ.get(OWNER_DSN_ENV) or dsn


_warned_unmigrated = False


def migrate(dsn: str, *sqls: str, rls: bool = True, strict: bool = False) -> bool:
    """Applique `sqls` (idempotents) dans l'ordre, puis la RLS (`006`, si `rls`) : une table de contenu n'existe
    jamais sans sa politique, même créée par un store avant `apply_migrations`. Renvoie False si le rôle n'a pas les
    droits (sauf `strict`, qui lève : job d'init)."""
    if rls:
        sqls = (*sqls, RLS_MIGRATION.read_text(encoding="utf-8"))
    try:
        with connect_admin(owner_dsn(dsn), autocommit=True) as conn:
            # Job de déploiement sur une base en service : `NOKKER_MIGRATE_LOCK_TIMEOUT` (p. ex. « 5s ») borne l'attente
            # d'un verrou (ACCESS EXCLUSIVE d'un ALTER) ; dépassé, le job échoue et se relance, au lieu de bloquer
            # derrière lui toutes les requêtes de la bêta. Vide = attente sans limite (comportement d'avant).
            lock_timeout = os.environ.get(LOCK_TIMEOUT_ENV, "").strip()
            if lock_timeout:
                if not _PG_DURATION.match(lock_timeout):
                    raise ValueError(f"{LOCK_TIMEOUT_ENV} illisible : {lock_timeout!r} (ex. 5s, 500ms, 1min)")
                conn.execute(sql_lib.SQL("SET lock_timeout = {}").format(sql_lib.Literal(lock_timeout)))
            conn.execute("SELECT pg_advisory_lock(%s)", (_MIGRATION_LOCK,))
            try:
                for sql in sqls:
                    conn.execute(sql)  # un fichier = une transaction implicite (protocole simple)
            finally:
                conn.execute("SELECT pg_advisory_unlock(%s)", (_MIGRATION_LOCK,))
    except psycopg.errors.InsufficientPrivilege as e:
        if strict:
            raise
        # Une fois par processus : chaque store appelle `migrate` au démarrage, la même cause se répétait par store.
        global _warned_unmigrated
        log = logger.debug if _warned_unmigrated else logger.warning
        _warned_unmigrated = True
        log("migrations non appliquées (rôle sans droits, %s absent ?) : %s — le job d'init doit les avoir faites",
            OWNER_DSN_ENV, str(e).splitlines()[0])
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


__all__ = ["AdminConn", "OWNER_DSN_ENV", "RLS_MIGRATION", "TenantConn", "connect", "connect_admin", "connect_key", "connect_tenant",
           "known_tenants", "migrate", "owner_dsn", "set_lookup_key"]


def role_bypasses_rls(dsn: str) -> bool:
    """Le rôle de `dsn` échappe-t-il à la RLS ? Un superutilisateur ou un rôle BYPASSRLS ignore toute politique, même
    FORCE (vu sur la copie du canari le 21.10.2026 : rôle `nokker` superutilisateur → un testeur y voyait tout)."""
    with _open(dsn, {}) as conn:
        row = conn.execute("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user").fetchone()
    return bool(row and row[0])
