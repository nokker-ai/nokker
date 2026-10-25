"""Stockage du SSO (drapeau `sso`) : `MemorySsoStore` (dev, tests) et `PgSsoStore` (tables de
`store/migrations/032_sso.sql`), même contrat. Les DROITS vivent dans nokker/sso/service.py, pas ici.

Sous RLS (locataire courant, `nokker/store/conn.py::connect`) : connexions, domaines, liens, adhésions tenues,
journal. Hors RLS (lus avant de connaître le locataire, connexion du même rôle `nokker_app` sans `app.tenant_id`) :
`sso_routes`, `sso_domain_claims`, `sso_flows`, `sso_identities`. Le lien d'un projet se lit aussi par ses membres
d'autres locataires : `binding(actor, …)` pose `app.account_id` comme `PgTeamStore`.
"""
from __future__ import annotations

import copy
import threading
from dataclasses import dataclass, field
from typing import Any, Protocol

from nokker.sso.model import Binding, Connection, Domain, Flow, Identity
from nokker.tenancy.context import current_tenant_or_none


class SsoStore(Protocol):
    def create_connection(self, c: Connection) -> None: ...
    def connection(self, cid: str) -> Connection | None: ...
    def connections_of(self, owner_account: str) -> list[Connection]: ...
    def save_connection(self, c: Connection) -> None: ...
    def delete_connection(self, c: Connection) -> None: ...
    def route(self, cid: str) -> str | None: ...
    def domains(self, cid: str) -> list[Domain]: ...
    def put_domain(self, d: Domain) -> None: ...
    def delete_domain(self, c: Connection, domain: str) -> bool: ...
    def claim_domain(self, d: Domain, now: float) -> bool: ...
    def claim_for(self, domain: str) -> tuple[str, str] | None: ...
    def binding(self, actor: Any, project_id: str) -> Binding | None: ...
    def bindings_of(self, cid: str) -> list[Binding]: ...
    def put_binding(self, b: Binding) -> None: ...
    def delete_binding(self, project_id: str) -> bool: ...
    def is_managed(self, project_id: str, account_id: str) -> bool: ...
    def set_managed(self, project_id: str, tenant_id: str, account_id: str, cid: str, now: float) -> None: ...
    def unset_managed(self, project_id: str, account_id: str) -> None: ...
    def put_flow(self, f: Flow) -> None: ...
    def take_flow(self, state_sha256: str) -> Flow | None: ...
    def identity(self, issuer: str, sub: str) -> Identity | None: ...
    def identities_of(self, account_id: str) -> list[Identity]: ...
    def put_identity(self, i: Identity) -> None: ...
    def last_login(self, cid: str, account_id: str) -> float | None: ...
    def add_audit(self, row: dict[str, Any]) -> None: ...
    def audit_of(self, cid: str, limit: int) -> list[dict[str, Any]]: ...


def _mine(tenant_id: str) -> bool:
    tid = current_tenant_or_none()
    return tid is not None and tid == tenant_id


@dataclass
class MemorySsoStore:
    conns: dict[str, Connection] = field(default_factory=dict)
    routes: dict[str, str] = field(default_factory=dict)
    doms: dict[tuple[str, str], Domain] = field(default_factory=dict)
    claims: dict[str, tuple[str, str]] = field(default_factory=dict)
    binds: dict[str, Binding] = field(default_factory=dict)
    managed: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    flows: dict[str, Flow] = field(default_factory=dict)
    idents: dict[tuple[str, str], Identity] = field(default_factory=dict)
    audit: list[dict[str, Any]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def create_connection(self, c: Connection) -> None:
        with self._lock:
            self.conns[c.id] = copy.deepcopy(c)
            self.routes[c.id] = c.tenant_id

    def connection(self, cid: str) -> Connection | None:
        with self._lock:
            c = self.conns.get(cid)
            return copy.deepcopy(c) if c is not None and _mine(c.tenant_id) else None

    def connections_of(self, owner_account: str) -> list[Connection]:
        with self._lock:
            return sorted((copy.deepcopy(c) for c in self.conns.values()
                           if c.owner_account == owner_account and _mine(c.tenant_id)), key=lambda c: c.created_at)

    def save_connection(self, c: Connection) -> None:
        with self._lock:
            if c.id in self.conns and _mine(self.conns[c.id].tenant_id):
                self.conns[c.id] = copy.deepcopy(c)

    def delete_connection(self, c: Connection) -> None:
        with self._lock:
            self.conns.pop(c.id, None)
            self.routes.pop(c.id, None)
            for k in [k for k in self.doms if k[0] == c.id]:
                del self.doms[k]
            for k in [k for k, v in self.claims.items() if v[0] == c.id]:
                del self.claims[k]
            for k in [k for k, b in self.binds.items() if b.connection_id == c.id]:
                del self.binds[k]
            for k in [k for k, v in self.managed.items() if v["connection_id"] == c.id]:
                del self.managed[k]

    def route(self, cid: str) -> str | None:
        with self._lock:
            return self.routes.get(cid)

    def domains(self, cid: str) -> list[Domain]:
        with self._lock:
            return sorted((copy.copy(d) for (c, _), d in self.doms.items() if c == cid and _mine(d.tenant_id)),
                          key=lambda d: d.domain)

    def put_domain(self, d: Domain) -> None:
        with self._lock:
            self.doms[(d.connection_id, d.domain)] = copy.copy(d)

    def delete_domain(self, c: Connection, domain: str) -> bool:
        with self._lock:
            if self.claims.get(domain, ("",))[0] == c.id:
                del self.claims[domain]
            return self.doms.pop((c.id, domain), None) is not None

    def claim_domain(self, d: Domain, now: float) -> bool:
        with self._lock:
            cur = self.claims.get(d.domain)
            if cur is not None and cur[0] != d.connection_id:
                return False
            self.claims[d.domain] = (d.connection_id, d.tenant_id)
            self.doms[(d.connection_id, d.domain)].verified_at = now
            return True

    def claim_for(self, domain: str) -> tuple[str, str] | None:
        with self._lock:
            return self.claims.get(domain)

    def binding(self, actor: Any, project_id: str) -> Binding | None:
        with self._lock:
            b = self.binds.get(project_id)
            return copy.deepcopy(b) if b else None

    def bindings_of(self, cid: str) -> list[Binding]:
        with self._lock:
            return sorted((copy.deepcopy(b) for b in self.binds.values() if b.connection_id == cid and _mine(b.tenant_id)),
                          key=lambda b: b.project_id)

    def put_binding(self, b: Binding) -> None:
        with self._lock:
            self.binds[b.project_id] = copy.deepcopy(b)

    def delete_binding(self, project_id: str) -> bool:
        with self._lock:
            for k in [k for k in self.managed if k[0] == project_id]:
                del self.managed[k]
            return self.binds.pop(project_id, None) is not None

    def is_managed(self, project_id: str, account_id: str) -> bool:
        with self._lock:
            return (project_id, account_id) in self.managed

    def set_managed(self, project_id: str, tenant_id: str, account_id: str, cid: str, now: float) -> None:
        with self._lock:
            self.managed.setdefault((project_id, account_id), {"tenant_id": tenant_id, "connection_id": cid,
                                                               "since": now})

    def unset_managed(self, project_id: str, account_id: str) -> None:
        with self._lock:
            self.managed.pop((project_id, account_id), None)

    def put_flow(self, f: Flow) -> None:
        with self._lock:
            self.flows[f.state_sha256] = copy.copy(f)

    def take_flow(self, state_sha256: str) -> Flow | None:
        with self._lock:
            return self.flows.pop(state_sha256, None)

    def identity(self, issuer: str, sub: str) -> Identity | None:
        with self._lock:
            i = self.idents.get((issuer, sub))
            return copy.copy(i) if i else None

    def identities_of(self, account_id: str) -> list[Identity]:
        with self._lock:
            return [copy.copy(i) for i in self.idents.values() if i.account_id == account_id]

    def put_identity(self, i: Identity) -> None:
        with self._lock:
            self.idents[(i.issuer, i.sub)] = copy.copy(i)

    def last_login(self, cid: str, account_id: str) -> float | None:
        with self._lock:
            ts = [i.last_login_at for i in self.idents.values() if i.account_id == account_id and i.connection_id == cid]
            return max(ts) if ts else None

    def add_audit(self, row: dict[str, Any]) -> None:
        with self._lock:
            self.audit.append({**copy.deepcopy(row), "tenant_id": current_tenant_or_none()})

    def audit_of(self, cid: str, limit: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = [{k: v for k, v in r.items() if k != "tenant_id"} for r in self.audit
                    if r["connection_id"] == cid and _mine(r["tenant_id"] or "")]
            return copy.deepcopy(rows[-limit:])


# ─────────────────────────────── Postgres ───────────────────────────────

_C_COLS = ("connection_id, tenant_id, owner_account, name, issuer, client_id, secret_name, groups_claim, token_auth, "
           "created_at, updated_at")
_B_COLS = "project_id, tenant_id, connection_id, mappings, required, max_age_s, updated_by, updated_at"
_F_COLS = ("state_sha256, connection_id, tenant_id, nonce, code_verifier, public_key_spki, device_name, lang, "
           "created_at, expires_at")
_I_COLS = "issuer, sub, account_id, connection_id, email, created_at, last_login_at"


def _conn_row(r: dict[str, Any]) -> Connection:
    return Connection(id=r["connection_id"], tenant_id=r["tenant_id"], owner_account=r["owner_account"],
                      name=r["name"], issuer=r["issuer"], client_id=r["client_id"], secret_name=r["secret_name"],
                      groups_claim=r["groups_claim"], token_auth=r["token_auth"], created_at=r["created_at"], updated_at=r["updated_at"])


def _bind_row(r: dict[str, Any]) -> Binding:
    return Binding(project_id=r["project_id"], tenant_id=r["tenant_id"], connection_id=r["connection_id"],
                   mappings=list(r["mappings"] or []), required=bool(r["required"]), max_age_s=int(r["max_age_s"]),
                   updated_by=r["updated_by"],
                   updated_at=r["updated_at"])


class PgSsoStore:
    """Une connexion courte par opération. Tables sous RLS : `connect` (locataire courant). Tables hors RLS : même
    rôle, sans locataire (`psycopg.connect`), comme les sessions et les appareils (`PgApprovals`)."""

    def __init__(self, dsn: str, *, migrate_now: bool = True) -> None:
        if not dsn:
            raise ValueError("PgSsoStore requiert un DSN Postgres")
        self.dsn = dsn
        if migrate_now:
            from pathlib import Path

            from nokker.store.conn import migrate

            sql = (Path(__file__).parent.parent / "store" / "migrations" / "032_sso.sql").read_text(encoding="utf-8")
            migrate(dsn, sql)

    def _rls(self, actor: Any = None):
        from psycopg.rows import dict_row

        from nokker.store.conn import connect

        conn = connect(self.dsn, row_factory=dict_row)
        if actor is not None:
            try:
                conn.execute("SELECT set_config('app.account_id', %s, false), set_config('app.account_email', %s, false)",
                             (actor.account_id, (actor.email or "").lower()))
                conn.commit()
            except BaseException:
                conn.close()
                raise
        return conn

    def _plain(self):
        import psycopg
        from psycopg.rows import dict_row

        return psycopg.connect(self.dsn, row_factory=dict_row)

    # ── connexions ──
    def create_connection(self, c: Connection) -> None:
        with self._rls() as db:
            db.execute(f"INSERT INTO sso_connections ({_C_COLS}) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                       (c.id, c.tenant_id, c.owner_account, c.name, c.issuer, c.client_id, c.secret_name,
                        c.groups_claim, c.token_auth, c.created_at, c.updated_at))
        with self._plain() as db:
            db.execute("INSERT INTO sso_routes (connection_id, tenant_id) VALUES (%s, %s)", (c.id, c.tenant_id))

    def connection(self, cid: str) -> Connection | None:
        with self._rls() as db:
            r = db.execute(f"SELECT {_C_COLS} FROM sso_connections WHERE connection_id = %s", (cid,)).fetchone()
        return _conn_row(r) if r else None

    def connections_of(self, owner_account: str) -> list[Connection]:
        with self._rls() as db:
            rows = db.execute(f"SELECT {_C_COLS} FROM sso_connections WHERE owner_account = %s ORDER BY created_at",
                              (owner_account,)).fetchall()
        return [_conn_row(r) for r in rows]

    def save_connection(self, c: Connection) -> None:
        with self._rls() as db:
            db.execute("UPDATE sso_connections SET name=%s, issuer=%s, client_id=%s, secret_name=%s, groups_claim=%s, "
                       "token_auth=%s, updated_at=%s WHERE connection_id=%s AND tenant_id=%s",
                       (c.name, c.issuer, c.client_id, c.secret_name, c.groups_claim, c.token_auth, c.updated_at,
                        c.id, c.tenant_id))

    def delete_connection(self, c: Connection) -> None:
        with self._rls() as db:
            exists = db.execute("SELECT 1 FROM sso_connections WHERE connection_id = %s AND tenant_id = %s",
                                (c.id, c.tenant_id)).fetchone()
            if not exists:
                return
            pids = [r["project_id"] for r in db.execute(
                "SELECT project_id FROM sso_bindings WHERE connection_id = %s AND tenant_id = %s",
                (c.id, c.tenant_id)).fetchall()]
            db.execute("DELETE FROM sso_managed WHERE connection_id = %s AND tenant_id = %s", (c.id, c.tenant_id))
            if pids:
                db.execute("DELETE FROM sso_managed WHERE project_id = ANY(%s) AND tenant_id = %s", (pids, c.tenant_id))
            for t in ("sso_bindings", "sso_domains", "sso_connections"):
                db.execute(f"DELETE FROM {t} WHERE connection_id = %s AND tenant_id = %s", (c.id, c.tenant_id))
        with self._plain() as db:
            for t in ("sso_domain_claims", "sso_routes", "sso_flows"):
                db.execute(f"DELETE FROM {t} WHERE connection_id = %s AND tenant_id = %s", (c.id, c.tenant_id))

    def route(self, cid: str) -> str | None:
        with self._plain() as db:
            r = db.execute("SELECT tenant_id FROM sso_routes WHERE connection_id = %s", (cid,)).fetchone()
        return r["tenant_id"] if r else None

    # ── domaines ──
    def domains(self, cid: str) -> list[Domain]:
        with self._rls() as db:
            rows = db.execute("SELECT connection_id, tenant_id, domain, token, verified_at, created_at FROM sso_domains "
                              "WHERE connection_id = %s ORDER BY domain", (cid,)).fetchall()
        return [Domain(**r) for r in rows]

    def put_domain(self, d: Domain) -> None:
        with self._rls() as db:
            db.execute("INSERT INTO sso_domains (connection_id, tenant_id, domain, token, verified_at, created_at) "
                       "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (connection_id, domain) DO NOTHING",
                       (d.connection_id, d.tenant_id, d.domain, d.token, d.verified_at, d.created_at))

    def delete_domain(self, c: Connection, domain: str) -> bool:
        with self._rls() as db:
            n = db.execute("DELETE FROM sso_domains WHERE connection_id = %s AND tenant_id = %s AND domain = %s",
                           (c.id, c.tenant_id, domain)).rowcount
        with self._plain() as db:
            db.execute("DELETE FROM sso_domain_claims WHERE domain = %s AND connection_id = %s AND tenant_id = %s",
                       (domain, c.id, c.tenant_id))
        return n > 0

    def claim_domain(self, d: Domain, now: float) -> bool:
        with self._plain() as db:
            db.execute("INSERT INTO sso_domain_claims (domain, connection_id, tenant_id, claimed_at) VALUES (%s,%s,%s,%s) "
                       "ON CONFLICT (domain) DO NOTHING", (d.domain, d.connection_id, d.tenant_id, now))
            r = db.execute("SELECT connection_id FROM sso_domain_claims WHERE domain = %s", (d.domain,)).fetchone()
        if r is None or r["connection_id"] != d.connection_id:
            return False
        with self._rls() as db:
            db.execute("UPDATE sso_domains SET verified_at = %s WHERE connection_id = %s AND tenant_id = %s "
                       "AND domain = %s", (now, d.connection_id, d.tenant_id, d.domain))
        return True

    def claim_for(self, domain: str) -> tuple[str, str] | None:
        with self._plain() as db:
            r = db.execute("SELECT connection_id, tenant_id FROM sso_domain_claims WHERE domain = %s",
                           (domain,)).fetchone()
        return (r["connection_id"], r["tenant_id"]) if r else None

    # ── liens projet ↔ connexion ──
    def binding(self, actor: Any, project_id: str) -> Binding | None:
        with self._rls(actor) as db:
            r = db.execute(f"SELECT {_B_COLS} FROM sso_bindings WHERE project_id = %s AND tenant_id = "
                           "(SELECT tenant_id FROM team_projects WHERE project_id = %s)",
                           (project_id, project_id)).fetchone()
        return _bind_row(r) if r else None

    def bindings_of(self, cid: str) -> list[Binding]:
        with self._rls() as db:
            rows = db.execute(f"SELECT {_B_COLS} FROM sso_bindings WHERE connection_id = %s ORDER BY project_id",
                              (cid,)).fetchall()
        return [_bind_row(r) for r in rows]

    def put_binding(self, b: Binding) -> None:
        from psycopg.types.json import Jsonb

        with self._rls() as db:
            db.execute(f"INSERT INTO sso_bindings ({_B_COLS}) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (project_id) "
                       "DO UPDATE SET connection_id = EXCLUDED.connection_id, mappings = EXCLUDED.mappings, "
                       "required = EXCLUDED.required, max_age_s = EXCLUDED.max_age_s, "
                       "updated_by = EXCLUDED.updated_by, updated_at = EXCLUDED.updated_at",
                       (b.project_id, b.tenant_id, b.connection_id, Jsonb(b.mappings), b.required, b.max_age_s,
                        b.updated_by, b.updated_at))

    def delete_binding(self, project_id: str) -> bool:
        with self._rls() as db:
            db.execute("DELETE FROM sso_managed WHERE project_id = %s", (project_id,))
            return db.execute("DELETE FROM sso_bindings WHERE project_id = %s", (project_id,)).rowcount > 0

    # ── adhésions tenues par le SSO (locataire du projet) ──
    def is_managed(self, project_id: str, account_id: str) -> bool:
        with self._rls() as db:
            return db.execute("SELECT 1 FROM sso_managed WHERE project_id = %s AND account_id = %s",
                              (project_id, account_id)).fetchone() is not None

    def set_managed(self, project_id: str, tenant_id: str, account_id: str, cid: str, now: float) -> None:
        with self._rls() as db:
            db.execute("INSERT INTO sso_managed (project_id, tenant_id, account_id, connection_id, since) "
                       "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (project_id, account_id) DO NOTHING",
                       (project_id, tenant_id, account_id, cid, now))

    def unset_managed(self, project_id: str, account_id: str) -> None:
        with self._rls() as db:
            db.execute("DELETE FROM sso_managed WHERE project_id = %s AND account_id = %s", (project_id, account_id))

    # ── connexions en cours (hors RLS, usage unique) ──
    def put_flow(self, f: Flow) -> None:
        with self._plain() as db:
            db.execute("DELETE FROM sso_flows WHERE expires_at < %s", (f.created_at - 3600,))
            db.execute(f"INSERT INTO sso_flows ({_F_COLS}) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                       (f.state_sha256, f.connection_id, f.tenant_id, f.nonce, f.code_verifier, f.public_key_spki,
                        f.device_name, f.lang, f.created_at, f.expires_at))

    def take_flow(self, state_sha256: str) -> Flow | None:
        with self._plain() as db:  # DELETE … RETURNING : deux callbacks simultanés, un seul gagne
            r = db.execute(f"DELETE FROM sso_flows WHERE state_sha256 = %s RETURNING {_F_COLS}",
                           (state_sha256,)).fetchone()
        return Flow(**r) if r else None

    # ── identités (hors RLS) ──
    def identity(self, issuer: str, sub: str) -> Identity | None:
        with self._plain() as db:
            r = db.execute(f"SELECT {_I_COLS} FROM sso_identities WHERE issuer = %s AND sub = %s",
                           (issuer, sub)).fetchone()
        return Identity(**r) if r else None

    def identities_of(self, account_id: str) -> list[Identity]:
        with self._plain() as db:
            rows = db.execute(f"SELECT {_I_COLS} FROM sso_identities WHERE account_id = %s", (account_id,)).fetchall()
        return [Identity(**r) for r in rows]

    def put_identity(self, i: Identity) -> None:
        with self._plain() as db:
            db.execute(f"INSERT INTO sso_identities ({_I_COLS}) VALUES (%s,%s,%s,%s,%s,%s,%s) "
                       "ON CONFLICT (issuer, sub) DO UPDATE SET connection_id = EXCLUDED.connection_id, "
                       "email = EXCLUDED.email, last_login_at = EXCLUDED.last_login_at "
                       "WHERE sso_identities.account_id = EXCLUDED.account_id",
                       (i.issuer, i.sub, i.account_id, i.connection_id, i.email, i.created_at, i.last_login_at))

    def last_login(self, cid: str, account_id: str) -> float | None:
        with self._plain() as db:
            r = db.execute("SELECT max(last_login_at) AS t FROM sso_identities WHERE connection_id = %s "
                           "AND account_id = %s", (cid, account_id)).fetchone()
        return r["t"] if r and r["t"] is not None else None

    # ── journal (locataire de la connexion) ──
    def add_audit(self, row: dict[str, Any]) -> None:
        from psycopg.types.json import Jsonb

        with self._rls() as db:
            db.execute("INSERT INTO sso_audit (connection_id, at, event, actor, email, project_id, detail) "
                       "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                       (row["connection_id"], row["at"], row["event"], row.get("actor", ""), row.get("email", ""),
                        row.get("project_id", ""), Jsonb(row.get("detail") or {})))

    def audit_of(self, cid: str, limit: int) -> list[dict[str, Any]]:
        with self._rls() as db:
            rows = db.execute("SELECT connection_id, at, event, actor, email, project_id, detail FROM sso_audit "
                              "WHERE connection_id = %s ORDER BY id DESC LIMIT %s", (cid, limit)).fetchall()
        return [dict(r) for r in reversed(rows)]


__all__ = ["MemorySsoStore", "PgSsoStore", "SsoStore"]
