"""Pack Emploi (073) sur un vrai Postgres, connexion `nokker_app`. Ignoré sans `NOKKER_TEST_PG_DSN`.
Profil emploi et versions locales cloisonnés par locataire ; un profil par compte ; versions effacées avec le profil."""
from __future__ import annotations

import pytest

from tests.integration.test_rls import A, B, pg, requires_pg  # noqa: F401 — fixtures (base neuve, rôles, migrations)

pytestmark = pytest.mark.integration


@requires_pg
def test_pg_jobs_store_tenant_isolated(pg):  # noqa: F811
    from nokker.jobs.store import JobsDoc, JobsProfile, PgJobsStore, new_id
    from nokker.tenancy.context import tenant_scope

    st = PgJobsStore(pg["app"])
    with tenant_scope(A):
        p = st.save_profile(JobsProfile(id=new_id("em"), account_id="us_pgjobs", tenant_id=A, active=True,
                                        partner_account_id="pa1", base_cv_id="cv1"))
        d = st.add_doc(JobsDoc(id=new_id("cvl"), profile_id=p.id, kind="cv", parent=f"{p.id}|cv:cv1",
                               data={"document": {"summary": "x"}}, pdf=b"%PDF", pdf_sha256="ab"))
        assert st.profile_of("us_pgjobs").partner_account_id == "pa1" and st.doc(d.id).pdf == b"%PDF"
        assert st.partner_accounts() == ["pa1"]
    with tenant_scope(B):
        assert st.profile_of("us_pgjobs") is None and st.doc(d.id) is None and st.partner_accounts() == []
    with tenant_scope(A):
        st.delete_profile(p.id)
        assert st.doc(d.id) is None


def _account(su: str, tenant: str, account_id: str) -> None:
    import psycopg

    from tests.integration.test_erase import _dummy_row

    with psycopg.connect(su, autocommit=True) as conn:
        conn.execute("INSERT INTO tenants (id) VALUES (%s) ON CONFLICT DO NOTHING", (tenant,))
        cols, vals = _dummy_row(conn, "accounts")
        row = {**dict(zip(cols, vals)), "id": account_id, "tenant_id": tenant, "email": f"{account_id}@recette.test"}
        conn.execute(f'INSERT INTO accounts ({", ".join(row)}) VALUES ({", ".join(["%s"] * len(row))})',
                     list(row.values()))


def _seed_jobs(app: str, tenant: str, account_id: str, partner: str) -> None:
    from nokker.jobs.store import JobsDoc, JobsProfile, PgJobsStore, new_id
    from nokker.tenancy.context import tenant_scope

    st = PgJobsStore(app)
    with tenant_scope(tenant):
        p = st.save_profile(JobsProfile(id=new_id("em"), account_id=account_id, tenant_id=tenant, active=True,
                                        partner_account_id=partner, base_cv_id="cv1"))
        st.add_doc(JobsDoc(id=new_id("lt"), profile_id=p.id, kind="letter", parent="", data={"text": "x"}))


@requires_pg
def test_erasing_an_account_with_job_data_erases_the_partner_account_first(pg, monkeypatch):  # noqa: F811
    """Effacement Nokker = effacement chez le service emploi (docs/JOBS-PACK.md §13.3), avant la validation ; en échec
    chez le service, rien n'est effacé ici. Pack éteint : tables du pack effacées avec le compte, sans appel au service."""
    import nokker.jobs as jobs_mod
    from nokker.admin.erase import EraseError, erase_account
    from tests.unit.jobs.fake_partner import AUDIENCE, PRIVATE_PEM, FakePartner

    fake = FakePartner()
    fake.accounts.update({"pa_erase_c": {}})
    orig = jobs_mod.partner_eraser
    monkeypatch.setattr(jobs_mod, "partner_eraser",
                        lambda dsn, block, resolve=None, transport=None: orig(dsn, block, lambda r: PRIVATE_PEM, fake))
    block = {"enabled": True, "base_url": fake.base, "private_key": "secret:K", "audience": AUDIENCE}
    c, acc_c = "tn_jobserasec01", "us_jobserasec01"
    _account(pg["su"], c, acc_c)
    _seed_jobs(pg["app"], c, acc_c, "pa_erase_c")

    fake.fail["/accounts/"] = (503, "unavailable")  # service en panne : rien n'est validé
    with pytest.raises(EraseError, match="service emploi injoignable") as err:
        erase_account(pg["app"], account_id=acc_c, apply=True, confirm=acc_c, jobs_pack=block)
    assert "journal" not in str(err.value)
    import psycopg

    with psycopg.connect(pg["su"]) as conn:
        assert conn.execute("SELECT count(*) FROM jobs_profiles WHERE tenant_id = %s", (c,)).fetchone()[0] == 1
    fake.fail.clear()
    r = erase_account(pg["app"], account_id=acc_c, apply=True, confirm=acc_c, jobs_pack=block)
    assert r.applied and r.deleted["service emploi : comptes partenaires"] == 1 and "pa_erase_c" not in fake.accounts
    assert r.deleted["jobs_profiles"] == 1 and r.deleted["jobs_docs"] == 1
    with psycopg.connect(pg["su"]) as conn:
        for t in ("jobs_profiles", "jobs_docs"):
            assert conn.execute(f"SELECT count(*) FROM {t} WHERE tenant_id = %s", (c,)).fetchone()[0] == 0

    d, acc_d = "tn_jobserased01", "us_jobserased01"  # pack éteint : effacement des tables, aucun appel
    _account(pg["su"], d, acc_d)
    _seed_jobs(pg["app"], d, acc_d, "pa_erase_d")
    calls = len(fake.calls)
    r = erase_account(pg["app"], account_id=acc_d, apply=True, confirm=acc_d)
    assert r.deleted["jobs_docs"] == 1 and len(fake.calls) == calls
