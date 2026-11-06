"""Console opérateur sur la bêta OVH : comptes, usage, drapeaux ; candidatures coupées (elles restent en Suisse) ;
fail closed sans AUD."""
from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from nokker.admin.access import AccessDenied, AccessVerifier
from nokker.admin.overview import MemoryOverview, accounts_view, usage_view
from nokker.admin.server import serve_admin
from nokker.admin.store import MemoryAdminStore
from tests.unit.admin.test_admin import AUD, TEAM, claims, get, jwt, verifier

ROOT = Path(__file__).resolve().parents[3]
NOW = datetime(2026, 11, 4, 15, 0, tzinfo=UTC)  # mercredi : jour de quota du 04.11, semaine du lundi 02.11
ACCOUNTS = [
    {"id": "us_aaaaaaaaaaaa", "tenant_id": "tn_aaaaaaaaaaaa", "email": "lea@example.org", "lang": "fr", "status": "active",
     "tenant_status": "active", "created_at": datetime(2026, 11, 2, tzinfo=UTC)},
    {"id": "us_bbbbbbbbbbbb", "tenant_id": "tn_bbbbbbbbbbbb", "email": "sam@example.ch", "lang": "en", "status": "suspended",
     "tenant_status": "suspended", "created_at": datetime(2026, 11, 3, tzinfo=UTC)}]
USAGE = [
    {"tenant_id": "tn_aaaaaaaaaaaa", "account": "us_aaaaaaaaaaaa", "day": date(2026, 11, 4), "turns": 45, "deep": 2, "week_turns": 0},
    {"tenant_id": "tn_aaaaaaaaaaaa", "account": "us_aaaaaaaaaaaa", "day": date(2026, 11, 2), "turns": 0, "deep": 0, "week_turns": 210},
    {"tenant_id": "tn_bbbbbbbbbbbb", "account": "us_bbbbbbbbbbbb", "day": date(2026, 11, 3), "turns": 7, "deep": 0, "week_turns": 0},
]


@pytest.fixture()
def beta():
    store = MemoryAdminStore()
    store.seed_operators({"alice@example.ch": "owner", "bruno@example.ch": "ops", "lire@example.ch": "lecteur"}, "system:config")
    httpd = serve_admin(store, verifier(), "127.0.0.1", 0, None,
                        overview=MemoryOverview(ACCOUNTS, USAGE, {"tn_aaaaaaaaaaaa": "solo"}), candidatures=False,
                        candidatures_console="admin.nokker.ai", limits={"mode": "soft", "turns_per_day": 40,
                                                                        "turns_per_week": 200},
                        flags=["admin_enabled", "billing_enabled"])
    yield store, f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def test_usage_view_aggregates_with_caps_and_plan():
    v = usage_view(USAGE, {"tn_aaaaaaaaaaaa": "solo"}, {"mode": "soft", "turns_per_day": 40, "turns_per_week": 200},
                   {}, NOW, 7)
    by = {i["account"]: i for i in v["items"]}
    a, b = by["us_aaaaaaaaaaaa"], by["us_bbbbbbbbbbbb"]
    assert (a["turns_today"], a["turns_week"], a["plan"]) == (45, 210, "solo")
    assert a["cap_day"] == 40 and a["over_day"]  # palier solo = CGU §8 (05.11) : 40/jour, 200/semaine
    assert a["cap_week"] == 200 and a["over_week"]
    assert b["turns_today"] == 0 and b["plan"] is None and b["cap_day"] == 40
    assert v["by_day"]["2026-11-04"] == {"turns": 45, "deep": 2, "accounts": 1}
    assert v["totals"]["turns_today"] == 45


def test_accounts_view_masks_email_for_lecteur():
    assert accounts_view(ACCOUNTS, {}, "ops")["items"][0]["email"] == "lea@example.org"
    assert accounts_view(ACCOUNTS, {}, "lecteur")["items"][0]["email"] == "l***@example.org"


def test_ops_reads_accounts_usage_flags_and_it_is_audited(beta):
    store, base = beta
    status, body = get(base, "/admin/v1/accounts", email="bruno@example.ch")
    assert status == 200 and json.loads(body)["total"] == 2
    status, body = get(base, "/admin/v1/usage?days=3", email="bruno@example.ch")
    assert status == 200 and json.loads(body)["days"] == 3
    assert json.loads(get(base, "/admin/v1/flags", email="bruno@example.ch")[1])["items"] == ["admin_enabled", "billing_enabled"]
    actions = [(j["actor"], j["action"]) for j in store.journal]
    assert ("bruno@example.ch", "accounts.list") in actions and ("bruno@example.ch", "usage.list") in actions


@pytest.mark.parametrize("path", ["/admin/v1/accounts", "/admin/v1/usage", "/admin/v1/flags", "/admin/v1/stats"])
def test_beta_routes_need_token_and_operator_role(beta, path):
    _, base = beta
    assert get(base, path)[0] == 401
    assert get(base, path, email="inconnu@example.ch")[0] == 403
    assert get(base, path, email="lire@example.ch")[0] == 403  # lecteur : pas d'accès aux comptes ni à l'usage


def test_no_conversation_content_in_responses(beta):
    _, base = beta
    for path in ("/admin/v1/accounts", "/admin/v1/usage", "/admin/v1/stats"):
        for key in ("content", "text", "thread", "message", "turn_text"):
            assert f'"{key}"' not in get(base, path, email="alice@example.ch")[1].decode()


def test_candidatures_disabled_everywhere(beta):
    store, base = beta
    for path in ("/admin/v1/applications", "/admin/v1/applications/1", "/admin/v1/applications/1/invitations",
                 "/admin/v1/applications/1/invite/preview"):
        status, body = get(base, path, email="alice@example.ch")
        assert status == 404 and json.loads(body)["error"] == "candidatures_disabled", path
        assert "admin.nokker.ai" in json.loads(body)["detail"]
    me = json.loads(get(base, "/admin/v1/me", email="alice@example.ch")[1])
    assert me["candidatures"] is False and me["candidatures_note"] == "candidatures : console Suisse (admin.nokker.ai)"
    stats = json.loads(get(base, "/admin/v1/stats", email="alice@example.ch")[1])  # agrégats de la bêta, pas de candidatures
    assert stats["accounts"] == 2 and "by_profil" not in stats
    from urllib import request as rq
    req = rq.Request(base + "/admin/v1/applications/1/invite", data=b"{}", method="POST",
                     headers={"Cf-Access-Jwt-Assertion": jwt(claims("alice@example.ch")), "X-Nokker-Console": "1"})
    from urllib import error as er
    with pytest.raises(er.HTTPError) as e:
        rq.urlopen(req, timeout=5)
    assert e.value.code == 404 and store.application(1) is None
    assert not [j for j in store.journal if j["action"].startswith(("application.", "invitation."))]


def test_ui_page_is_served_with_candidatures_note(beta):
    _, base = beta
    status, body = get(base, "/admin/", email="alice@example.ch")
    assert status == 200 and b"candidatures_note" in body and b"/admin/v1/usage" in body


def test_fail_closed_without_audience(monkeypatch):
    """Sans valeur d'AUD (variable absente), même un jeton parfaitement signé est refusé partout sauf /healthz."""
    store = MemoryAdminStore()
    store.seed_operators({"alice@example.ch": "owner"}, "system:config")
    v = AccessVerifier(team_domain=TEAM, audience="", fetch=verifier().fetch)
    httpd = serve_admin(store, v, "127.0.0.1", 0, None, overview=MemoryOverview(ACCOUNTS, USAGE), candidatures=False)
    try:
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        good = jwt(claims("alice@example.ch", aud=[AUD]))
        for path in ("/admin/", "/admin/v1/me", "/admin/v1/accounts", "/admin/v1/usage", "/admin/v1/audit"):
            assert get(base, path, token=good)[0] == 401, path
        assert get(base, "/admin/healthz")[0] == 200
    finally:
        httpd.shutdown()


def test_fail_closed_without_team_domain():
    v = AccessVerifier(team_domain="", audience=AUD, fetch=verifier().fetch)
    with pytest.raises(AccessDenied):
        v.verify(jwt(claims()))
