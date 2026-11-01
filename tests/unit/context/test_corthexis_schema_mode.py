"""`corthexis_schema: managed | external` (nokker/context/corthexis_tenancy.py, docs/MEMORY-TENANCY.md §5.1) : mode,
démarrage du pod (vérification au lieu d'`ensure`), `nokker migrate` (DSN propriétaire). Aucun Postgres : les appels à
la base sont remplacés ; le vrai comportement est dans tests/integration/test_corthexis_tenancy_pg.py."""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from nokker.config import NokkerConfig
from nokker.context import corthexis_tenancy as ct


@pytest.fixture(autouse=True)
def _reset_mode():
    yield
    ct.set_mode(ct.MANAGED)
    ct.set_strict(False)


def test_mode_defaults_to_managed_and_rejects_typos():
    assert NokkerConfig().corthexis_schema == "managed"
    assert ct.set_mode(None) == ct.MANAGED
    assert ct.set_mode(" External ") == ct.EXTERNAL and ct.schema_mode() == ct.EXTERNAL
    with pytest.raises(ValueError):
        ct.set_mode("externl")
    assert ct.schema_mode() == ct.EXTERNAL  # une valeur refusée ne change rien


def test_unknown_mode_stops_the_pod():
    from nokker.api.server import apply_corthexis_schema

    with pytest.raises(SystemExit, match="corthexis_schema inconnu"):
        apply_corthexis_schema(NokkerConfig(corthexis_schema="owner"))


def _ctx_config(**kw) -> NokkerConfig:
    return NokkerConfig(context_enabled=True, require_rls=True,
                        context_corthexis={"enabled": True, "root_dir": kw.pop("root")}, **kw)


def test_external_pod_verifies_instead_of_ensure_and_cuts_the_index(monkeypatch, tmp_path, caplog):
    from nokker.api import server as srv

    monkeypatch.setenv("CORTHEXIS_DATABASE_URL", "postgresql://app@db/corthexis")
    monkeypatch.setenv("CORTHEXIS_EMBED_URLS", "http://embed:8080")
    monkeypatch.setattr(ct, "ensure", lambda *a, **k: pytest.fail("ensure appelée en mode external"))
    monkeypatch.setattr(ct, "verify", lambda dsn: ["notes : appartient au rôle du pod"])
    caplog.set_level(logging.INFO)
    mgr = srv.build_context_manager(_ctx_config(root=str(tmp_path), corthexis_schema="external"), store=None)
    assert getattr(mgr, "semantic", None) is None  # index de session coupé (rappel lexical)
    err = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert err and "appartient au rôle du pod" in err[0].getMessage() and "index de session coupé" in err[0].getMessage()


def test_external_pod_serves_a_conforming_database(monkeypatch, tmp_path):
    from nokker.api import server as srv

    monkeypatch.setenv("CORTHEXIS_DATABASE_URL", "postgresql://app@db/corthexis")
    monkeypatch.setenv("CORTHEXIS_EMBED_URLS", "http://embed:8080")
    monkeypatch.setattr(ct, "ensure", lambda *a, **k: pytest.fail("ensure appelée en mode external"))
    monkeypatch.setattr(ct, "verify", lambda dsn: [])
    mgr = srv.build_context_manager(_ctx_config(root=str(tmp_path), corthexis_schema="external"), store=None)
    assert mgr.semantic is not None


def test_external_pod_unreachable_database_is_not_served(monkeypatch, tmp_path, caplog):
    from nokker.api import server as srv

    def boom(dsn):
        raise OSError("connection refused for postgresql://app:secret@db/corthexis")

    monkeypatch.setenv("CORTHEXIS_DATABASE_URL", "postgresql://app:secret@db/corthexis")
    monkeypatch.setattr(ct, "verify", boom)
    caplog.set_level(logging.INFO)
    mgr = srv.build_context_manager(_ctx_config(root=str(tmp_path), corthexis_schema="external"), store=None)
    assert getattr(mgr, "semantic", None) is None
    assert "secret" not in caplog.text and "base injoignable" in caplog.text


def test_managed_pod_still_runs_ensure(monkeypatch, tmp_path):
    from nokker.api import server as srv

    calls = []
    monkeypatch.setenv("CORTHEXIS_DATABASE_URL", "postgresql://app@db/corthexis")
    monkeypatch.setattr(ct, "ensure", lambda dsn, pt=None, **k: calls.append(dsn) or True)
    monkeypatch.setattr(ct, "verify", lambda dsn: pytest.fail("verify appelée en mode managed"))
    mgr = srv.build_context_manager(_ctx_config(root=str(tmp_path)), store=None)
    assert calls == ["postgresql://app@db/corthexis"] and mgr.semantic is not None


class _FakeStore:
    def __init__(self):
        self.dsn, self.pool = "postgresql://app@db/corthexis", SimpleNamespace(connection=None)
        self.create_generation = self.build_index = None


def test_guard_store_external_never_ensures_and_strict_refuses(monkeypatch):
    monkeypatch.setattr(ct, "ensure", lambda *a, **k: pytest.fail("ensure appelée en mode external"))
    monkeypatch.setattr(ct, "verify", lambda dsn: ["chunks : RLS non forcée"])
    ct.set_mode(ct.EXTERNAL)
    ct.set_strict(True)
    with pytest.raises(RuntimeError, match="RLS non forcée"):
        ct.guard_store(_FakeStore())


def test_guard_store_external_routes_generations_to_owner_functions(monkeypatch):
    pytest.importorskip("corthexis.store", reason="paquet corthexis absent")
    monkeypatch.setattr(ct, "verify", lambda dsn: [])
    ct.set_mode(ct.EXTERNAL)
    ct.set_strict(True)
    st = ct.guard_store(_FakeStore())
    assert st._nokker_tenant_guard and isinstance(st.pool, ct._TenantPool)
    assert st.create_generation.__qualname__.startswith("_external_generations")
    assert st.build_index.__qualname__.startswith("_external_generations")


# ─────────────── nokker migrate ───────────────

def _migrate(monkeypatch, capsys, cfg: NokkerConfig, env: dict[str, str]):
    import nokker.cli as cli_mod

    seen: dict = {}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(cli_mod, "load_config", lambda path=None: cfg)
    monkeypatch.setattr(cli_mod, "apply_migrations", lambda dsn, strict: seen.setdefault("core", (dsn, strict)))

    def migrate_schema(owner, **kw):
        seen["cx"] = (owner, kw)
        return {"applied": [1, 6], "app_role": kw["app_role"], "backup": True}

    monkeypatch.setattr(ct, "migrate_schema", migrate_schema)
    rc = cli_mod._cmd_migrate(SimpleNamespace(config="x.yaml"))
    return rc, seen, capsys.readouterr()


def test_migrate_runs_the_corthexis_owner_path(monkeypatch, capsys):
    cfg = NokkerConfig(corthexis_schema="external", context_corthexis={"enabled": True})
    rc, seen, out = _migrate(monkeypatch, capsys, cfg, {"NOKKER_PG_DSN": "postgresql://owner@db/nokker",
                                                         "CORTHEXIS_OWNER_DATABASE_URL": "postgresql://owner@db/cx"})
    assert rc == 0 and seen["core"] == ("postgresql://owner@db/nokker", True)
    owner, kw = seen["cx"]
    assert owner == "postgresql://owner@db/cx" and kw["app_role"] == "nokker_app" and callable(kw["project_tenants"])
    assert "base CortHeXis migrée (2 migration(s) du paquet, droits de nokker_app, sauvegarde)" in out.out
    assert "owner@db" not in out.out + out.err


def test_migrate_custom_env_and_role(monkeypatch, capsys):
    cfg = NokkerConfig(context_corthexis={"enabled": True, "owner_database_url_env": "CX_OWNER",
                                          "app_role": "core_rw"})
    rc, seen, _ = _migrate(monkeypatch, capsys, cfg, {"NOKKER_PG_DSN": "postgresql://o@db/n",
                                                       "CX_OWNER": "postgresql://o@db/cx"})
    assert rc == 0 and seen["cx"][0] == "postgresql://o@db/cx" and seen["cx"][1]["app_role"] == "core_rw"


def test_migrate_without_owner_dsn_leaves_corthexis_alone(monkeypatch, capsys):
    monkeypatch.delenv("CORTHEXIS_OWNER_DATABASE_URL", raising=False)
    rc, seen, out = _migrate(monkeypatch, capsys, NokkerConfig(context_corthexis={"enabled": True}),
                             {"NOKKER_PG_DSN": "postgresql://o@db/n"})
    assert rc == 0 and "cx" not in seen and out.err == ""
    cfg = NokkerConfig(corthexis_schema="external", context_corthexis={"enabled": True})
    rc, seen, out = _migrate(monkeypatch, capsys, cfg, {"NOKKER_PG_DSN": "postgresql://o@db/n"})
    assert rc == 0 and "cx" not in seen and "sans CORTHEXIS_OWNER_DATABASE_URL" in out.err


def test_migrate_rejects_an_unknown_mode(monkeypatch, capsys):
    cfg = NokkerConfig(corthexis_schema="externl", context_corthexis={"enabled": True})
    rc, seen, out = _migrate(monkeypatch, capsys, cfg, {"NOKKER_PG_DSN": "postgresql://o@db/n",
                                                         "CORTHEXIS_OWNER_DATABASE_URL": "postgresql://o@db/cx"})
    assert rc == 1 and "cx" not in seen and "corthexis_schema inconnu" in out.err


def test_backup_grants_skip_a_missing_role():
    class Con:
        def __init__(self):
            self.sql = []

        def execute(self, sql, params=()):
            self.sql.append(sql)
            return SimpleNamespace(fetchone=lambda: (False,))

    con = Con()
    assert ct._grant_backup(con, "nokker_backup", strict=True) is False
    assert len(con.sql) == 1  # seulement la lecture de pg_roles
