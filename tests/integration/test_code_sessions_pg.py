"""Sessions de code multi-utilisateur (docs/LOKKAL.md §7.28) en base : vrai Postgres jetable, rôles de
deploy/postgres/roles.sql, store connecté en `nokker_app` (ni propriétaire, ni BYPASSRLS). Ignoré sans
`NOKKER_TEST_PG_DSN`.

Prouve : les sessions d'un locataire n'existent pas pour un autre (par le store ET en SQL brut), une écriture qui force
le locataire d'un autre est refusée, sans locataire on ne lit rien ; deux utilisateurs d'un même locataire ne se voient
pas par le store ; le plafond compte les sessions vivantes ; une session survit au redémarrage du store ; jamais de
valeur d'identifiant en base (le NOM seulement)."""
from __future__ import annotations

import os

import pytest

from tests.integration.test_rls import A, B, pg, pod_env, requires_pg  # noqa: F401 — fixtures (base neuve, rôles)

pytestmark = pytest.mark.integration


def _session(sid: str, tenant: str, user: str, **kw):
    from nokker.code_sessions.store import CodeSession

    return CodeSession(id=sid, tenant_id=tenant, user_id=user, thread_id=f"{tenant}:{sid}",
                       root_rel=f"{tenant}/{user}/{sid}", mode="accept_edits", created_at=1.0, last_used_at=1.0, **kw)


@requires_pg
def test_code_sessions_are_isolated_by_rls(pg, pod_env):  # noqa: F811
    import psycopg

    from nokker.code_sessions.store import PgCodeStore
    from nokker.store.conn import TenantConn, connect
    from nokker.tenancy.context import NoTenant, tenant_scope

    store = PgCodeStore(pg["app"], migrate_now=False)
    try:
        ua, ua2, ub = "us_aaaaaaaaaaa1", "us_aaaaaaaaaaa2", "us_bbbbbbbbbbb1"
        with tenant_scope(A):
            store.put(_session("cs_aaaaaaaaaaaaaaa1", A, ua, repo_url="https://git.example.org/a.git",
                               credential="forge-bot"))
            store.put(_session("cs_aaaaaaaaaaaaaaa2", A, ua2))
            store.put(_session("cs_aaaaaaaaaaaaaaa3", A, ua, status="closed"))
        with tenant_scope(B):
            store.put(_session("cs_bbbbbbbbbbbbbbb1", B, ub))
        with tenant_scope(A):
            assert [s.id for s in store.list_user(ua)] == ["cs_aaaaaaaaaaaaaaa1"]  # vivantes seulement
            assert [s.id for s in store.list_user(ua, live_only=False)] == ["cs_aaaaaaaaaaaaaaa1",
                                                                           "cs_aaaaaaaaaaaaaaa3"]
            assert [s.id for s in store.list_user(ua2)] == ["cs_aaaaaaaaaaaaaaa2"]
            assert store.count_live(ua) == 1
            assert store.get("cs_bbbbbbbbbbbbbbb1") is None and store.by_thread(f"{B}:cs_bbbbbbbbbbbbbbb1") is None
            assert store.list_user(ub) == []
            s = store.by_thread(f"{A}:cs_aaaaaaaaaaaaaaa1")
            assert s.credential == "forge-bot" and s.repo_url == "https://git.example.org/a.git"
            assert store.update("cs_aaaaaaaaaaaaaaa1", status="idle").status == "idle"
        with tenant_scope(B):
            assert [s.id for s in store.list_user(ub)] == ["cs_bbbbbbbbbbbbbbb1"]
            assert store.get("cs_aaaaaaaaaaaaaaa1") is None
            assert store.update("cs_aaaaaaaaaaaaaaa1", status="closed") is None  # rien à toucher chez A
        for tid, other in ((A, B), (B, A)):
            with tenant_scope(tid), connect(pg["app"]) as conn:
                assert conn.execute("SELECT count(*) FROM nokker_code_sessions WHERE tenant_id <> %s",
                                    (tid,)).fetchone()[0] == 0
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    conn.execute("INSERT INTO nokker_code_sessions (id, tenant_id, user_id, thread_id, root_rel, "
                                 "created_at) VALUES ('cs_forced0000000000', %s, 'us_x', 'x', 'x', 1)", (other,))
        with pytest.raises(NoTenant):
            TenantConn(pg["app"]).get()
        # « redémarrage » : un autre store, même base
        again = PgCodeStore(pg["app"], migrate_now=False)
        try:
            with tenant_scope(A):
                assert again.get("cs_aaaaaaaaaaaaaaa1").status == "idle"
        finally:
            again.close()
        with psycopg.connect(pg["su"]) as su:
            cols = {r[0] for r in su.execute("SELECT column_name FROM information_schema.columns "
                                              "WHERE table_name = 'nokker_code_sessions'").fetchall()}
        assert not cols & {"token", "secret", "password", "value"}
    finally:
        store.close()


@requires_pg
def test_service_on_postgres_two_tenants(pg, pod_env, tmp_path):  # noqa: F811
    from nokker.code_sessions.config import CodeConfig
    from nokker.code_sessions.service import CodeError, CodeSessions
    from nokker.code_sessions.store import PgCodeStore
    from nokker.tenancy.context import tenant_scope

    cfg = CodeConfig.from_config({"enabled": True, "backend": "docker", "workspace_root": str(tmp_path),
                                  "max_sessions_per_user": 1}, lokkal=False)
    svc = CodeSessions(cfg=cfg, store=PgCodeStore(pg["app"], migrate_now=False))
    try:
        with tenant_scope(A):
            a = svc.create("us_aaaaaaaaaaa9", {})
            with pytest.raises(CodeError):
                svc.create("us_aaaaaaaaaaa9", {})  # plafond, compté en base
        with tenant_scope(B):
            b = svc.create("us_aaaaaaaaaaa9", {})  # même identifiant de compte dans un autre locataire : distinct
            assert [x["id"] for x in svc.list("us_aaaaaaaaaaa9")["sessions"]] == [b["id"]]
            with pytest.raises(CodeError):
                svc.get("us_aaaaaaaaaaa9", a["id"])
        assert (tmp_path / A / "us_aaaaaaaaaaa9" / a["id"]).is_dir()
        assert (tmp_path / B / "us_aaaaaaaaaaa9" / b["id"]).is_dir()
    finally:
        svc.close_all()
        svc.store.close()


@requires_pg
def test_nokker_erase_removes_code_sessions_workspaces_checkpoints_and_credentials(pg, pod_env, tmp_path,  # noqa: F811
                                                                                  monkeypatch):
    import psycopg
    from cryptography.fernet import Fernet

    from nokker.admin.erase import erase_account
    from nokker.code_sessions.config import CodeConfig
    from nokker.code_sessions.secrets import TenantSecrets
    from nokker.code_sessions.service import CodeSessions
    from nokker.code_sessions.store import PgCodeStore
    from nokker.tenancy.context import tenant_scope
    from tests.integration.test_erase import _dummy_row

    monkeypatch.delenv("NOKKER_SECRETS_PROVIDER", raising=False)
    monkeypatch.setenv("NOKKER_CODE_IT_KEY", Fernet.generate_key().decode())
    acc = {A: "us_aaaaaaaaaaaa", B: "us_bbbbbbbbbbbb"}
    with psycopg.connect(pg["su"], autocommit=True) as conn:
        for tid, aid in acc.items():
            cols, vals = _dummy_row(conn, "accounts")
            row = {**dict(zip(cols, vals)), "id": aid, "tenant_id": tid, "email": f"{aid}@recette.test"}
            conn.execute(f'INSERT INTO accounts ({", ".join(row)}) VALUES ({", ".join(["%s"] * len(row))}) '
                         "ON CONFLICT DO NOTHING", list(row.values()))
    raw = {"enabled": True, "backend": "docker", "workspace_root": str(tmp_path / "ws"),
           "secrets": {"provider": "file", "file": {"key_env": "NOKKER_CODE_IT_KEY"}}}
    (tmp_path / "ws").mkdir()
    cfg = CodeConfig.from_config(raw, lokkal=False)
    svc = CodeSessions(cfg=cfg, store=PgCodeStore(pg["app"], migrate_now=False),
                       secrets=TenantSecrets.from_config(cfg.secrets, os.path.join(cfg.state_dir, "secrets")))
    made = {}
    try:
        for tid, aid in acc.items():
            with tenant_scope(tid):
                svc.put_credential("forge-bot", f"tok-{tid}")
                s = svc.create(aid, {})
                made[tid] = s["id"]
                ck = svc.checkpoint_home(tid, aid, s["id"])
                (ck / "checkpoints" / "store").mkdir(parents=True)
    finally:
        svc.close_all()
        svc.store.close()
    with psycopg.connect(pg["su"]) as su:  # la base du module porte aussi les sessions des tests précédents
        before = dict(su.execute("SELECT tenant_id, count(*) FROM nokker_code_sessions GROUP BY 1").fetchall())
    dry = erase_account(pg["app"], account_id=acc[A], code=raw)
    assert not dry.applied and dry.deleted["nokker_code_sessions"] == before[A] >= 1
    assert dry.deleted["code: espaces de travail"] == 1 and dry.deleted["code: points de retour"] == 1
    assert dry.deleted["code: identifiants de forge"] == 1 and (tmp_path / "ws" / A).is_dir()
    done = erase_account(pg["app"], account_id=acc[A], apply=True, confirm=acc[A], code=raw)
    assert done.applied and done.deleted["code: espaces de travail"] == 1
    state = tmp_path / "ws" / ".nokker-state"
    assert not (tmp_path / "ws" / A).exists() and not (state / A).exists()
    assert (tmp_path / "ws" / B / acc[B] / made[B]).is_dir() and (state / B).is_dir()
    vault = TenantSecrets.from_config(raw["secrets"], state / "secrets")
    assert vault.for_tenant(A).list() == [] and vault.for_tenant(B).get("forge-bot") == f"tok-{B}"
    with psycopg.connect(pg["su"]) as su:
        left = dict(su.execute("SELECT tenant_id, count(*) FROM nokker_code_sessions GROUP BY 1").fetchall())
    assert left == {B: before[B]}
