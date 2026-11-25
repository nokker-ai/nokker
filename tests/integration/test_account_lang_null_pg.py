"""`accounts.lang` NULL (migration 089) sur un vrai Postgres, connexion `nokker_app` (sous RLS forcée). Le montage `pg`
crée le schéma par les fichiers de migration (`migration_files()`), comme la production ; la migration 089 est ici
rejouée sur une base qui porte déjà des lignes `fr` (comme avant 089). Ignoré sans `NOKKER_TEST_PG_DSN`."""
from __future__ import annotations

import pytest

from tests.integration.test_rls import pg, requires_pg  # noqa: F401 — fixtures (base neuve, rôles, migrations)

pytestmark = pytest.mark.integration


def _sql(name: str) -> str:
    from nokker.store.pg import migration_files

    return next(p for p in migration_files() if p.name == name).read_text(encoding="utf-8")


@requires_pg
def test_pg_migration_089_nulls_default_fr_keeps_en_is_idempotent_and_set_lang_roundtrips(pg, monkeypatch):  # noqa: F811
    import psycopg

    from nokker.api.server import account_lang_of
    from nokker.store.conn import migrate
    from nokker.tenancy.store import PgTenancy

    monkeypatch.setenv("NOKKER_PG_OWNER_DSN", pg["owner"])
    from nokker.store.pg import migration_files

    names = [p.name for p in migration_files() if p.parent.name == "migrations" and p.parent.parent.name == "store"]
    assert names.index("088_beta_coupon_grants.sql") + 1 == names.index("089_account_lang_null.sql")  # dans l'ordre, sans trou
    nums = sorted(int(n[:3]) for n in names if n[:3].isdigit())
    assert [n for n in nums if n >= 80] == list(range(80, nums[-1] + 1))  # aucun trou de 080 à la dernière
    sql = _sql("089_account_lang_null.sql")
    with psycopg.connect(pg["su"], autocommit=True) as su:
        # état d'AVANT 089 : colonne NOT NULL DEFAULT 'fr', deux comptes fr par défaut, un en
        su.execute("UPDATE accounts SET lang = 'fr' WHERE lang IS NULL")
        su.execute("ALTER TABLE accounts ALTER COLUMN lang SET DEFAULT 'fr'")
        su.execute("ALTER TABLE accounts ALTER COLUMN lang SET NOT NULL")
        su.execute("INSERT INTO tenants (id) VALUES ('tn_l1'), ('tn_l2'), ('tn_l3') ON CONFLICT DO NOTHING")
        for i, lang in ((1, "fr"), (2, "fr"), (3, "en")):
            su.execute("INSERT INTO accounts (id, tenant_id, email, lang) VALUES (%s, %s, %s, %s)",
                       (f"us_old{i}", f"tn_l{i}", f"old{i}@example.org", lang))
        assert su.execute("SELECT relforcerowsecurity FROM pg_class WHERE relname = 'accounts'").fetchone()[0] is True
    assert migrate(pg["owner"], sql, rls=False, strict=True)  # en propriétaire (RLS forcée, NOBYPASSRLS)
    with psycopg.connect(pg["su"], autocommit=True) as su:
        col = su.execute("SELECT is_nullable, column_default FROM information_schema.columns "
                         "WHERE table_name = 'accounts' AND column_name = 'lang'").fetchone()
        assert col[0] == "YES" and col[1] is None
        rows = dict(su.execute("SELECT id, lang FROM accounts WHERE id LIKE 'us_old%'").fetchall())
        assert rows == {"us_old1": None, "us_old2": None, "us_old3": "en"}
        # la RLS forcée est rétablie telle qu'elle était
        assert su.execute("SELECT relforcerowsecurity, relrowsecurity FROM pg_class WHERE relname = 'accounts'").fetchone() == (True, True)

    tenancy = PgTenancy(pg["app"])
    fr_choice = tenancy.set_lang("us_old1", "fr")
    assert fr_choice.lang == "fr"
    # rejouée (redémarrage) : le fr POSÉ n'est pas effacé, la migration ne refait rien
    assert migrate(pg["owner"], sql, rls=False, strict=True) and migrate(pg["owner"], sql, rls=False, strict=True)
    assert tenancy.account("us_old1").lang == "fr" and tenancy.account("us_old2").lang is None
    assert tenancy.account("us_old3").lang == "en"

    lang_of = account_lang_of(tenancy)
    assert lang_of("us_old1") == "fr" and lang_of("us_old2") is None and lang_of("us_old3") == "en"
    assert tenancy.set_lang("us_old1", None).lang is None and lang_of("us_old1") is None
    for code in ("de", "it", "fr", "en"):
        assert tenancy.set_lang("us_old2", code).lang == code and tenancy.account("us_old2").lang == code
    with pytest.raises(ValueError):
        tenancy.set_lang("us_old2", "pt")
    assert tenancy.account("us_old2").lang == "en"  # refus : rien d'écrit
    assert tenancy.set_lang("us_inconnu0000", None) is None

    # création : sans langue (ou « fr » par défaut) = NULL ; en/de/it posés
    a, b, c = (tenancy.create_account("new-a@example.org"), tenancy.create_account("new-b@example.org", "fr"),
               tenancy.create_account("new-c@example.org", "en"))
    assert (a.lang, b.lang, c.lang) == (None, None, "en")
    d, i = tenancy.create_account("new-d@example.org", "de"), tenancy.create_account("new-i@example.org", "it")
    assert (d.lang, i.lang) == ("de", "it") and tenancy.account(d.account_id).lang == "de"
    with psycopg.connect(pg["su"]) as su:
        assert [r[0] for r in su.execute("SELECT lang FROM accounts WHERE id IN (%s,%s) ORDER BY email",
                                         (a.account_id, b.account_id)).fetchall()] == [None, None]
    assert lang_of(a.account_id) is None


@requires_pg
def test_pg_kernel_resolve_lang_distinguishes_explicit_fr_from_unset(pg):  # noqa: F811
    from nokker.api.server import account_lang_of
    from nokker.core.loop import Kernel
    from nokker.tenancy.store import PgTenancy

    tenancy = PgTenancy(pg["app"])
    nul, fr = tenancy.create_account("rl-nul@example.org"), tenancy.create_account("rl-fr@example.org")
    tenancy.set_lang(fr.account_id, "fr")
    k = Kernel.__new__(Kernel)
    k.account_lang = account_lang_of(tenancy)
    k.thread_lang = lambda thread_id: "de"
    assert k.resolve_lang("th", nul.account_id) == "de"
    assert k.resolve_lang("th", fr.account_id) == "fr"
    tenancy.set_lang(fr.account_id, None)
    assert k.resolve_lang("th", fr.account_id) == "de"
