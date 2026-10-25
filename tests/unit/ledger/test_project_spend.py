"""Attribution d'un appel au projet d'équipe (docs/LOKKAL.md §7.15, budget) : le ledger reste le seul compteur ; un
appel fait pendant un tour rattaché à un projet porte le projet, son locataire et le compte qui a dépensé."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from nokker.ledger.attribution import SpendTag, current_tag, spend_tag
from nokker.ledger.costs import InMemoryLedger
from tests.unit.ledger.test_costs import make_call

TAG = SpendTag(project_id="pj_0123456789ab", project_tenant="tn_aaaaaaaaaaaa", account="us_member")


def test_tag_scope_nests_and_none_keeps_the_outer_tag():
    assert current_tag() is None
    with spend_tag(TAG):
        assert current_tag() == TAG
        with spend_tag(None):  # un sous-agent dont le fil ne résout rien garde le projet du tour qui l'a lancé
            assert current_tag() == TAG
    assert current_tag() is None


def test_calls_made_under_a_tag_carry_the_project_and_the_account():
    now = datetime(2026, 10, 25, 9, 0, tzinfo=UTC)
    ledger = InMemoryLedger(clock=lambda: now)
    ledger.record("th_free", 0, make_call())
    with spend_tag(TAG):
        ledger.record("th_proj", 0, make_call())
        ledger.record("th_proj/sa-1", 0, make_call())
    plain = ledger.fetch_calls(thread_id="th_free")[0]
    assert plain.project_id is None and plain.account is None
    got = ledger.fetch_project_calls(TAG.project_id, TAG.project_tenant, since=now - timedelta(days=1),
                                     actor="us_member")
    assert sorted(r.thread_id for r in got) == ["th_proj", "th_proj/sa-1"]
    assert {(r.project_id, r.account) for r in got} == {(TAG.project_id, "us_member")}
    # même identifiant de projet, autre locataire : rien (un projet est défini par son id ET son locataire)
    assert ledger.fetch_project_calls(TAG.project_id, "tn_bbbbbbbbbbbb", since=now - timedelta(days=1),
                                      actor="us_member") == []
    # fenêtre : rien avant `since`
    assert ledger.fetch_project_calls(TAG.project_id, TAG.project_tenant, since=now + timedelta(seconds=1),
                                      actor="us_member") == []
