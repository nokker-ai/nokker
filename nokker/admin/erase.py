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

Les images jointes (X7) vivent hors base, dans le magasin d'images (`images_root`) : le dossier du locataire y est
supprimé après la validation de la transaction. Même règle pour les sessions de code (bloc `code`) : espaces de travail,
points de retour, confiances des hooks, identifiants de forge du coffre. L'index sémantique de session (bloc
`context_corthexis` : notes sous `root_dir` et base CortHeXis, nokker/context/corthexis_erase.py) est effacé, lui,
AVANT la validation, d'après les fils du locataire lus dans la transaction : s'il échoue, rien n'est validé et tout se
rejoue. Tout chemin d'effacement passe la même portée : `erase_account(dsn, …, **erase_scope(config))`.

Hors base, à faire par l'opérateur (le compte rendu le rappelle) : inscrire la ligne au journal des effacements
(coffre de l'opérateur : identifiant de locataire et date, rien d'autre), et retirer la candidature et
l'invitation chez front1 (Suisse), qui ne sont pas dans cette base. Les sauvegardes ne sont pas modifiées : les données
en sortent à l'expiration de la rétention (30 jours).
"""
from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import psycopg

from nokker.billing.plans import ACTIVE_STATUSES
from nokker.tenancy.context import INTERNAL_TENANT

# Tables sans colonne de rattachement, connues et volontairement hors effacement : comptes d'opérateurs, journal
# d'administration, candidatures historiques (elles vivent chez front1), nonces (traités à part, par scope_id).
UNSCOPED_OK = {"operators", "admin_audit", "beta_applications", "used_nonces", "schema_migrations",
               "lokkal_link_requests",  # flux d'appareil Lokkal : 10 min, aucune donnée de compte
               "billing_events",  # idempotence des webhooks Stripe : identifiant et type d'événement, aucune donnée de compte
               "lokkal_relay_used_jti"}  # jti des jetons de pages (035) : empreintes, 10 min au plus, aucun compte
_LAST = ("accounts", "tenants")  # supprimées après tout le reste, dans cet ordre
# Équipe (X17, migration 023) : un compte membre d'un projet d'un AUTRE locataire y a des lignes au locataire du projet.
# Elles partent avec lui : la transaction pose aussi `app.account_id` / `app.account_email`, que les politiques d'équipe
# ouvrent à ce seul compte (ses adhésions, les invitations qui lui sont adressées) ; le reste du projet ne bouge pas.
_CROSS_TENANT = {
    "team_access": ("tenant_id = %s OR account_id = %s", "acc"),
    "team_members": ("tenant_id = %s OR account_id = %s", "acc"),
    "team_invites": ("tenant_id = %s OR invitee_account = %s OR (email <> '' AND email = %s)", "acc+mail"),
    # SSO (032) : les adhésions que le SSO tient pour ce compte dans les projets d'autres locataires (politique
    # `sso_self_erase`) ; ses identités (issuer, sub) partent par `account_id`.
    "sso_managed": ("tenant_id = %s OR account_id = %s", "acc"),
    # Foyer (039) : son adhésion au foyer d'un autre (politique `household_self_leave`, mineur compris pendant
    # l'effacement) et, pour un profil mineur, les attestations de son représentant qui le nomment (par son adresse,
    # `household_minor_erase`). Son profil mineur (`account_minors`, hors RLS) part par `account_id`.
    "household_members": ("tenant_id = %s OR account_id = %s", "acc"),
    "parental_consents": ("tenant_id = %s OR (minor_email <> '' AND minor_email = %s)", "mail"),
}


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


def erase_scope(config: Any) -> dict[str, Any]:
    """Ce qui vit hors de la base du noyau, d'après la configuration du serveur : à passer à `erase_account` par TOUT
    chemin d'effacement (CLI, file des suppressions demandées par les personnes), pour qu'aucun n'en oublie."""
    return {"images_root": config.images_root() or None, "code": config.code,
            "session_memory": dict(config.context_corthexis) if config.context_corthexis else None}


def erase_account(dsn: str, *, email: str | None = None, account_id: str | None = None,
                  apply: bool = False, confirm: str | None = None, images_root: str | None = None,
                  code: dict[str, Any] | None = None, session_memory: dict[str, Any] | None = None,
                  before_commit: Callable[[Report], None] | None = None) -> Report:
    """`images_root` (X7, `NokkerConfig.images_root()`) : le magasin des images jointes ; le dossier du locataire y est
    supprimé APRÈS la validation de la base (à blanc : seulement compté). `code` (`NokkerConfig.code`, sessions de
    code multi-utilisateur, docs/LOKKAL.md §7.28) : espaces de travail, points de retour, confiances des hooks et
    identifiants de forge du locataire, effacés eux aussi après la validation (nokker/code_sessions/erase.py).
    `session_memory` (`NokkerConfig.context_corthexis`) : l'index sémantique de session des fils du locataire, effacé
    AVANT la validation (nokker/context/corthexis_erase.py) ; en échec, `EraseError` et rien n'est validé.
    `before_commit(report)` (validé seulement) : appelé dans la transaction, les suppressions faites, AVANT l'index de
    session et la validation — le journal des effacements de la file (`ErasureJournal`) ; s'il lève, `EraseError` et
    rien n'est effacé (le compte reste dans la file)."""
    report = _erase_db(dsn, email=email, account_id=account_id, apply=apply, confirm=confirm,
                       session_memory=session_memory, before_commit=before_commit)
    if code:
        from nokker.code_sessions.erase import erase_code_tenant

        report.deleted.update(erase_code_tenant(code, report.tenant_id, apply=report.applied))
    if images_root:
        from pathlib import Path

        from nokker.images.store import ImageStore

        store = ImageStore(Path(images_root))
        report.deleted["images (fichiers)"] = (store.erase_tenant(report.tenant_id) if report.applied
                                               else store.count_tenant(report.tenant_id))
    return report


def _tenant_threads(conn: psycopg.Connection, cols: dict[str, set[str]], tenant: str) -> set[str]:
    """Tous les fils du locataire que la base connaît encore (tours, épisodes, fils permanents…), sous-agents compris :
    un fil dont les épisodes ont disparu garde ses tours (append-only), donc ses notes d'index se retrouvent."""
    threads: set[str] = set()
    for t, cs in sorted(cols.items()):
        if "tenant_id" not in cs:
            continue
        col = "thread_id" if "thread_id" in cs else "id" if t == "nokker_threads" else None
        if col:
            threads.update(r[0] for r in conn.execute(
                f'SELECT DISTINCT "{col}" FROM "{t}" WHERE tenant_id = %s', (tenant,)).fetchall() if r[0])
    return threads


def _erase_db(dsn: str, *, email: str | None, account_id: str | None, apply: bool, confirm: str | None,
              session_memory: dict[str, Any] | None = None,
              before_commit: Callable[[Report], None] | None = None) -> Report:
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
        if conn.execute("SELECT to_regclass('subscriptions')").fetchone()[0]:
            conn.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant,))
            live = conn.execute("SELECT count(*) FROM subscriptions WHERE tenant_id = %s AND status = ANY(%s)",
                                (tenant, sorted(ACTIVE_STATUSES))).fetchone()[0]
            if live:  # sinon Stripe continue de facturer un compte effacé ici
                raise EraseError(f"{acc} a un abonnement actif : le résilier d'abord (Billing.cancel_for)")
        report = Report(account_id=acc, tenant_id=tenant, email=mail, devices=_devices(conn, acc))

        conn.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant,))  # RLS : ce locataire, cette transaction
        conn.execute("SELECT set_config('nokker.erasing_tenant', %s, true)", (tenant,))  # turns : migration 013
        conn.execute("SELECT set_config('app.account_id', %s, true), set_config('app.account_email', %s, true)",
                     (acc, mail))  # équipe : ses adhésions aux projets d'autres locataires (023)
        cols = _columns(conn)
        threads = _tenant_threads(conn, cols, tenant) if session_memory else set()
        plan: dict[str, tuple[str, tuple]] = {}
        for t, cs in cols.items():
            if t in _LAST:
                continue
            if t in _CROSS_TENANT:
                where, extra = _CROSS_TENANT[t]
                plan[t] = (where, (tenant, acc, mail) if extra == "acc+mail" else (tenant, mail) if extra == "mail"
                           else (tenant, acc))
            elif "tenant_id" in cs:
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
        if apply and before_commit is not None:  # journal écrit (fsync) avant tout effacement hors base et la validation
            try:
                before_commit(report)
            except Exception as e:  # noqa: BLE001 — la transaction est annulée, le compte reste dans la file
                raise EraseError(f"journal des effacements : écriture impossible ({type(e).__name__})") from None
        if session_memory:  # avant la validation : en échec, l'exception annule la transaction (tout se rejoue)
            from nokker.context.corthexis_erase import SessionMemoryEraseError, erase_session_memory

            try:
                report.deleted.update(erase_session_memory(session_memory, threads, apply=apply))
            except SessionMemoryEraseError as e:
                raise EraseError(str(e)) from None
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


def pending_deletions(dsn: str) -> list[str]:
    """Comptes dont la suppression a été accordée par la personne (carte forte, nokker/account/delete.py), dans
    l'ordre des demandes : `nokker erase --pending` les efface un par un."""
    with psycopg.connect(dsn) as conn:
        if conn.execute("SELECT to_regclass('account_deletions')").fetchone()[0] is None:
            return []
        return [r[0] for r in conn.execute("SELECT account_id FROM account_deletions ORDER BY requested_at")]


# Verrou consultatif de SESSION (Postgres) : un seul passage de la file à la fois, sur toute l'instance (CronJob relancé
# pendant qu'un passage tourne, opérateur à la main). Pris par `pg_try_advisory_lock` : jamais d'attente, le second
# passage ne fait rien. Le noyau ne le prend jamais : il n'écrit dans la file qu'à l'accord, sur un compte déjà
# suspendu, et la ligne de la file part dans la transaction d'effacement de son compte.
PENDING_LOCK_KEY = "nokker:erase-pending"


@contextmanager
def _pending_lock(dsn: str) -> Iterator[bool]:
    """Rend True si ce passage tient le verrou (relâché à la sortie, ou à la fermeture de la connexion si le processus
    meurt), False si un autre passage le tient déjà."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        got = conn.execute("SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", (PENDING_LOCK_KEY,)).fetchone()[0]
        try:
            yield bool(got)
        finally:
            if got:
                conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (PENDING_LOCK_KEY,))


class JournalUnusable(Exception):
    """`--journal` inutilisable (dossier absent, non inscriptible, fichier impossible à ouvrir) : refus avant tout."""


class ErasureJournal:
    """Journal des effacements de la file, hors base (`nokker erase --pending --apply --journal PATH`) : une ligne
    `{"tenant_id", "erased_at"}` par compte effacé — le locataire seulement, jamais l'adresse ni le compte `us_…`.
    C'est lui que lit le rejeu après une restauration (docs/BACKUP-DRP.md §6). Fichier créé en 0600, ouvert en ajout
    seul (O_APPEND) ; chaque ligne est suivie d'un fsync, avant la validation de la transaction d'effacement."""

    def __init__(self, fd: int) -> None:
        self.fd = fd

    @staticmethod
    def check(path: str) -> None:
        parent = os.path.dirname(os.path.abspath(path)) or "/"
        if not os.path.isdir(parent):
            raise JournalUnusable(f"journal : dossier absent ({parent})")
        if not os.access(parent, os.W_OK | os.X_OK):
            raise JournalUnusable(f"journal : dossier non inscriptible ({parent})")
        if os.path.isdir(path):
            raise JournalUnusable(f"journal : {path} est un dossier")

    @classmethod
    def open(cls, path: str) -> ErasureJournal:
        cls.check(path)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_CLOEXEC", 0), 0o600)
        except OSError as e:
            raise JournalUnusable(f"journal : ouverture impossible ({e.strerror})") from None
        return cls(fd)

    def append(self, tenant_id: str) -> None:
        line = json.dumps({"tenant_id": tenant_id,
                           "erased_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}) + "\n"
        data = line.encode("utf-8")
        if os.write(self.fd, data) != len(data):
            raise OSError("écriture incomplète du journal")
        os.fsync(self.fd)

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def __enter__(self) -> ErasureJournal:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


@dataclass
class PendingRun:
    """Compte rendu d'un passage de la file : des nombres, jamais une identité."""
    applied: bool
    locked: bool = False  # un autre passage tenait le verrou : rien n'a été fait
    erased: int = 0
    refused: int = 0
    gone: int = 0  # sortis de la file entre la lecture et l'effacement (effacés par un autre chemin)
    at: str = field(default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))

    def journal_line(self) -> str:
        """LA ligne du journal des effacements pour ce passage : date et nombres seulement (ni adresse, ni compte, ni
        locataire — ces sorties sont gardées sans fin, bucket verrouillé)."""
        return json.dumps({"erased_at": self.at, "erased": self.erased, "refused": self.refused})


def erase_pending(dsn: str, *, apply: bool = False, scope: dict[str, Any] | None = None,
                  journal: ErasureJournal | None = None) -> PendingRun:
    """Vide la file des suppressions accordées (`account_deletions`) : chaque compte passe par `erase_account` avec la
    portée COMPLÈTE hors base (`scope` = `erase_scope(config)` : images, sessions de code, index de session), l'accord
    de la personne valant confirmation. Sous verrou consultatif : deux passages simultanés n'en font qu'un.
    Idempotent : un compte effacé quitte la file dans la même transaction ; un refus (abonnement encore actif, index
    CortHeXis injoignable…) laisse le compte dans la file, rejoué au passage suivant. `journal` : la ligne du
    locataire y est écrite avant la validation de chaque effacement ; écriture impossible → ce compte est refusé."""
    run = PendingRun(applied=apply)
    before = (lambda report: journal.append(report.tenant_id)) if journal is not None and apply else None
    with _pending_lock(dsn) as held:
        if not held:
            run.locked = True
            return run
        for acc in pending_deletions(dsn):
            try:
                erase_account(dsn, account_id=acc, apply=apply, confirm=acc, before_commit=before, **(scope or {}))
            except EraseError:
                if acc in pending_deletions(dsn):
                    run.refused += 1
                else:
                    run.gone += 1
                continue
            run.erased += 1
    return run


__all__ = ["PENDING_LOCK_KEY", "EraseError", "ErasureJournal", "JournalUnusable", "PendingRun", "Report", "erase_account", "erase_pending", "erase_scope",
           "pending_deletions", "remove_device"]
