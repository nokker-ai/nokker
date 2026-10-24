"""Effacement d'un compte testeur et retrait d'un appareil (docs/BACKUP-DRP.md §6, conditions §7) — outil d'opérateur.

`nokker erase --email <adresse>` (ou `--account us_…`) supprime de la base vive TOUT ce qui appartient au compte et à son
locataire ; `nokker devices remove <code>` retire un seul appareil (ses sessions, ses nonces de connexion, son identité).
Les deux tournent À BLANC par défaut : les suppressions sont exécutées dans une transaction puis annulées, et le compte
rendu donne le nombre exact de lignes par table. `--apply` (avec `--confirm <account_id>` pour l'effacement) valide.

Ce qui est effacé est lu dans le catalogue, pas dans une liste figée : une table ajoutée plus tard est couverte si elle
porte `tenant_id`, `account_id` ou `"user"` (tables d'authentification de nokker/approvals, `"user"` = account_id).
Une table qui ne porte aucune de ces colonnes est listée « non concernée » dans le compte rendu, pour qu'on la regarde.
Les tables sous RLS ne sont visibles qu'avec `app.tenant_id` : il est posé sur le locataire effacé pour la transaction,
ce qui interdit aussi, par la politique elle-même, de toucher une ligne d'un autre locataire. `turns` est append-only :
son trigger n'accepte un DELETE que si `nokker.erasing_tenant` désigne le locataire de la ligne (migration 013).

Hors base, à faire par l'opérateur (le compte rendu le rappelle) : inscrire la ligne au journal des effacements
(coffre de l'opérateur : identifiant de locataire et date, rien d'autre), et retirer la candidature et
l'invitation chez front1 (Suisse), qui ne sont pas dans cette base. Les sauvegardes ne sont pas modifiées : les données
en sortent à l'expiration de la rétention (30 jours).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import psycopg

from nokker.tenancy.context import INTERNAL_TENANT

# Tables sans colonne de rattachement, connues et volontairement hors effacement : comptes d'opérateurs, journal
# d'administration, candidatures historiques (elles vivent chez front1), nonces (traités à part, par scope_id).
UNSCOPED_OK = {"operators", "admin_audit", "beta_applications", "used_nonces", "schema_migrations",
               "lokkal_link_requests"}  # flux d'appareil Lokkal : 10 min, aucune donnée de compte
_LAST = ("accounts", "tenants")  # supprimées après tout le reste, dans cet ordre


class EraseError(Exception):
    """Refus : compte introuvable, locataire interne, locataire partagé, confirmation absente ou fausse."""


@dataclass
class Report:
    account_id: str
    tenant_id: str
    email: str
    devices: list[dict[str, Any]]
    deleted: dict[str, int] = field(default_factory=dict)
    unscoped: list[str] = field(default_factory=list)
    applied: bool = False

    def journal_line(self) -> str:
        return json.dumps({"tenant_id": self.tenant_id,
                           "erased_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})

    def render(self) -> str:
        head = "EFFACÉ" if self.applied else "À BLANC (rien n'est validé ; --apply --confirm <account_id> pour effacer)"
        lines = [f"{head} — compte {self.account_id} <{self.email}>, locataire {self.tenant_id}",
                 f"appareils : {', '.join(d['code'] + ' ' + repr(d['name']) for d in self.devices) or 'aucun'}"]
        lines += [f"  {t:<28} {n}" for t, n in self.deleted.items() if n]
        lines.append(f"  (tables sans ligne du compte : {', '.join(t for t, n in self.deleted.items() if not n) or '—'})")
        if self.unscoped:
            lines.append(f"  NON CONCERNÉES, à regarder : {', '.join(self.unscoped)}")
        if self.applied:
            lines += ["À faire hors base :",
                      f"  1. journal des effacements (coffre de l'opérateur) : {self.journal_line()}",
                      "  2. candidature et invitation chez front1 (Suisse), si elles doivent partir aussi"]
        return "\n".join(lines)


def _columns(conn: psycopg.Connection) -> dict[str, set[str]]:
    rows = conn.execute("SELECT table_name, column_name FROM information_schema.columns "
                        "WHERE table_schema = current_schema()").fetchall()
    tables = {r[0] for r in conn.execute("SELECT table_name FROM information_schema.tables "
                                         "WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'")}
    cols: dict[str, set[str]] = {t: set() for t in tables}
    for t, c in rows:
        if t in cols:
            cols[t].add(c)
    return cols


def _children_first(conn: psycopg.Connection, tables: list[str]) -> list[str]:
    """Ordre de suppression : une table qui en référence une autre passe avant elle (clés étrangères)."""
    refs: dict[str, set[str]] = {t: set() for t in tables}
    for child, parent in conn.execute(
            "SELECT c.conrelid::regclass::text, c.confrelid::regclass::text FROM pg_constraint c "
            "WHERE c.contype = 'f' AND c.connamespace = current_schema()::regnamespace").fetchall():
        child, parent = child.strip('"'), parent.strip('"')
        if child in refs and parent in refs and child != parent:
            refs[parent].add(child)  # parent attend ses enfants
    out: list[str] = []
    seen: set[str] = set()

    def visit(t: str, stack: frozenset[str]) -> None:
        if t in seen or t in stack:
            return
        for c in sorted(refs[t]):
            visit(c, stack | {t})
        seen.add(t)
        out.append(t)

    for t in sorted(tables):
        visit(t, frozenset())
    return out


def _find_account(conn: psycopg.Connection, email: str | None, account_id: str | None) -> tuple[str, str, str]:
    if account_id:
        row = conn.execute("SELECT id, tenant_id, email FROM accounts WHERE id = %s", (account_id,)).fetchone()
    else:
        row = conn.execute("SELECT id, tenant_id, email FROM accounts WHERE email = %s",
                           ((email or "").strip().lower(),)).fetchone()
    if row is None:
        raise EraseError(f"aucun compte pour {account_id or email!r}")
    return row[0], row[1], row[2]


def _devices(conn: psycopg.Connection, account_id: str) -> list[dict[str, Any]]:
    return [{"id": r[0], "code": r[1], "name": r[2], "status": r[3]} for r in conn.execute(
        'SELECT id, code, name, status FROM devices WHERE "user" = %s ORDER BY created_at', (account_id,))]


def erase_account(dsn: str, *, email: str | None = None, account_id: str | None = None,
                  apply: bool = False, confirm: str | None = None) -> Report:
    with psycopg.connect(dsn) as conn:
        acc, tenant, mail = _find_account(conn, email, account_id)
        if tenant == INTERNAL_TENANT:
            raise EraseError(f"{acc} appartient au locataire interne {INTERNAL_TENANT} : refusé")
        others = conn.execute("SELECT count(*) FROM accounts WHERE tenant_id = %s AND id <> %s",
                              (tenant, acc)).fetchone()[0]
        if others:
            raise EraseError(f"le locataire {tenant} porte {others} autre(s) compte(s) : effacement du locataire refusé")
        if apply and confirm != acc:
            raise EraseError(f"--apply exige --confirm {acc}")
        report = Report(account_id=acc, tenant_id=tenant, email=mail, devices=_devices(conn, acc))

        conn.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant,))  # RLS : ce locataire, cette transaction
        conn.execute("SELECT set_config('nokker.erasing_tenant', %s, true)", (tenant,))  # turns : migration 013
        cols = _columns(conn)
        plan: dict[str, tuple[str, tuple]] = {}
        for t, cs in cols.items():
            if t in _LAST:
                continue
            if "tenant_id" in cs:
                plan[t] = ("tenant_id = %s", (tenant,))
            elif "account_id" in cs:
                plan[t] = ("account_id = %s", (acc,))
            elif "user" in cs:
                plan[t] = ('"user" = %s', (acc,))
            elif t not in UNSCOPED_OK:
                report.unscoped.append(t)
        if "used_nonces" in cols:  # défis de connexion des appareils du compte
            n = conn.execute("DELETE FROM used_nonces WHERE scope_id = ANY(%s)",
                             ([f"login:{d['id']}" for d in report.devices],)).rowcount
            report.deleted["used_nonces"] = n
        for t in _children_first(conn, list(plan)):
            where, params = plan[t]
            report.deleted[t] = conn.execute(f'DELETE FROM "{t}" WHERE {where}', params).rowcount
        report.deleted["accounts"] = conn.execute("DELETE FROM accounts WHERE id = %s", (acc,)).rowcount
        report.deleted["tenants"] = conn.execute("DELETE FROM tenants WHERE id = %s", (tenant,)).rowcount
        if apply:
            conn.commit()
            report.applied = True
        else:
            conn.rollback()
    report.unscoped.sort()
    return report


def remove_device(dsn: str, code: str, *, apply: bool = False) -> str:
    """Retire UN appareil (par son code court) : sessions, nonces de connexion, identité `device`, l'appareil."""
    with psycopg.connect(dsn) as conn:
        row = conn.execute('SELECT id, "user", name, status FROM devices WHERE code = %s',
                           (code.strip().lower(),)).fetchone()
        if row is None:
            raise EraseError(f"aucun appareil de code {code!r}")
        dev, user, name, status = row
        n_sess = conn.execute("DELETE FROM sessions WHERE device_id = %s", (dev,)).rowcount
        n_nonce = conn.execute("DELETE FROM used_nonces WHERE scope_id = %s", (f"login:{dev}",)).rowcount
        n_id = conn.execute("DELETE FROM account_identities WHERE kind = 'device' AND value = %s", (dev,)).rowcount
        conn.execute("DELETE FROM devices WHERE id = %s", (dev,))
        left = conn.execute('SELECT count(*) FROM devices WHERE "user" = %s', (user,)).fetchone()[0]
        if apply:
            conn.commit()
        else:
            conn.rollback()
    head = "RETIRÉ" if apply else "À BLANC (--apply pour retirer)"
    return (f"{head} — appareil {dev} code {code} {name!r} ({status}) du compte {user} : {n_sess} session(s), "
            f"{n_nonce} nonce(s), {n_id} identité ; il reste {left} appareil(s) au compte")


__all__ = ["EraseError", "Report", "erase_account", "remove_device"]
