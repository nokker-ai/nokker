"""Vouchers sur un vrai Postgres (migration 067) : connexion applicative `nokker_app`, réservation concurrente d'un
code unique, reprise d'une réservation d'invitation, bilan, fin d'essai dans `subscriptions`. Ignoré sans
`NOKKER_TEST_PG_DSN`."""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

from nokker.billing.store import PgBilling
from nokker.billing.voucher import PgVouchers, Vouchers
from nokker.tenancy.context import tenant_scope
from tests.integration.test_rls import pg, pod_env, requires_pg  # noqa: F401 — fixtures (base neuve, rôles, migrations)

NOW = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)


def _v(dsn, grants_invite=False):
    return Vouchers(PgVouchers(dsn), "sel-de-test-0123456789", clock=lambda: NOW.timestamp(), grants_invite=grants_invite)


@requires_pg
def test_single_code_is_reserved_once_under_concurrency(pg, pod_env):  # noqa: F811
    v = _v(pg["app"])
    (code,) = v.create(campaign="flyers", kind="single", count=1, expires="2027-06-30", actor="ops@x")
    results, barrier = [], threading.Barrier(8)

    def go(i):
        barrier.wait()
        try:
            results.append(("ok", v.reserve(code, f"tn_{i:012d}", f"p{i}@example.org", "solo")[0]))
        except Exception as e:  # noqa: BLE001
            results.append(("ko", getattr(e, "code", repr(e))))

    threads = [threading.Thread(target=go, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(r[0] for r in results) == ["ko"] * 7 + ["ok"]
    assert {r[1] for r in results if r[0] == "ko"} == {"code_exhausted"}


@requires_pg
def test_one_trial_per_person_and_expiry_frees(pg, pod_env):  # noqa: F811
    v = _v(pg["app"])
    v.create(campaign="radio", kind="shared", code="RADIO26", expires="2027-06-30", actor="ops@x")
    v.create(campaign="tele", kind="shared", code="TELE26", expires="2027-06-30", actor="ops@x")
    uid, end = v.reserve("radio26", "tn_aaaaaaaaaaaa", "a@example.org", "solo")
    assert end > NOW and v.check("TELE26", "tn_bbbbbbbbbbbb", "A+x@example.org") == "code_used"
    assert v.check("TELE26", "tn_aaaaaaaaaaaa", "z@example.org") == "code_used"
    v.store.update(uid, reserved_until=NOW - timedelta(seconds=1))
    assert v.check("TELE26", "tn_aaaaaaaaaaaa", "a@example.org") is None
    assert v.store.expire_reservations(NOW) == 1
    rep = v.report()
    assert {c["campaign"]: c["expired"] for c in rep["campaigns"]} == {"radio": 1, "tele": 0, "flyers": 0}


@requires_pg
def test_invite_reservation_is_taken_over_by_the_first_checkout(pg, pod_env):  # noqa: F811
    v = _v(pg["app"], grants_invite=True)
    (code,) = v.create(campaign="flyers", kind="single", count=1, expires="2027-06-30", actor="ops@x")
    uid = v.invite(code, "new@example.org")
    assert v.invite(code, "New+1@example.org") == uid
    assert v.check(code, "tn_xxxxxxxxxxxx", "other@example.org") == "code_exhausted"
    assert v.reserve(code, "tn_nnnnnnnnnnnn", "new@example.org", "famille")[0] == uid
    use = v.store.by_session("") or v.store.trialing()  # aucune session encore : on relit par l'id
    row = PgVouchers(pg["app"])._conn().execute("SELECT plan, tenant_key FROM voucher_uses WHERE id = %s", (uid,)).fetchone()
    assert row["plan"] == "famille" and not row["tenant_key"].startswith("pending:") and use is not None


@requires_pg
def test_no_address_or_tenant_in_clear_and_trial_end_on_subscriptions(pg, pod_env):  # noqa: F811
    import psycopg

    v = _v(pg["app"])
    v.create(campaign="radio", kind="shared", code="RADIOSECRET", expires="2027-06-30", actor="ops@x")
    v.reserve("RADIOSECRET", "tn_secretsecret", "secret@example.org", "solo")
    with psycopg.connect(pg["su"]) as conn:
        dump = str(conn.execute("SELECT * FROM voucher_uses").fetchall())
    assert "secret@example.org" not in dump and "tn_secretsecret" not in dump
    b = PgBilling(pg["app"])
    with tenant_scope("tn_trialtrial01"):
        assert b.upsert_subscription({"stripe_subscription": "sub_t", "stripe_customer": "cus_t", "plan": "solo",
                                      "status": "trialing", "current_period_end": NOW, "cancel_at_period_end": False,
                                      "last_event_at": NOW, "trial_end": NOW + timedelta(days=30)})
    assert b.subscription("tn_trialtrial01")["trial_end"] == NOW + timedelta(days=30)
