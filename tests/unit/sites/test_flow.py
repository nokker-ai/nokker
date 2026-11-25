"""Flux de connexion et gestes de lecture sur le site simulé (docs/CONNECTED-SITES.md §4.2, §4.4, §6, §13)."""
from __future__ import annotations

import json
import threading

from nokker.sites.credentials import REASON_EXPIRED
from nokker.sites.fakesocial import FAKESOCIAL
from nokker.sites.model import State

from tests.unit.sites.helpers import CANARY_CODE, CANARY_PASSWORD, OWNER, connect_ok, make_stack


def _connect_async(st, args):
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", st.source.call("sites_connect", args, st.ctx())))
    t.start()
    return t, out


def test_first_connection_password_by_secure_request_then_vault(tmp_path):
    st = make_stack(tmp=tmp_path)
    cid = connect_ok(st)
    conn = st.store.get(cid)
    assert conn.label == "LinkedIn" and conn.default and conn.account_name == "Alex Martin"
    assert st.vault.values[f"sites.{cid}.password"] == CANARY_PASSWORD  # enregistré après succès
    assert st.store.credential_reads() == []  # la valeur venait de la demande : aucune lecture du coffre


def test_valid_session_never_reads_the_vault(tmp_path):
    st = make_stack(tmp=tmp_path)
    cid = connect_ok(st)
    st.vault.gets.clear()
    for _ in range(3):
        r = st.source.call("sites_list", {"kind": "notifications"}, st.ctx())
        assert not r.is_error, r.content
        r = st.source.call("sites_list", {"kind": "messages"}, st.ctx())
        assert not r.is_error, r.content
    r = st.source.call("sites_search", {"kind": "people", "query": "camille"}, st.ctx())
    assert not r.is_error, r.content
    assert [n for n in st.vault.gets if n.startswith("sites.")] == []
    assert st.store.credential_reads(cid) == []
    assert all(r.kind not in ("password", "code") for r in st.requests._by_id.values() if not r.answered)


def test_expired_session_reads_vault_once_with_reason_and_resumes(tmp_path):
    st = make_stack(tmp=tmp_path)
    cid = connect_ok(st)
    st.server.expire_all()
    r = st.source.call("sites_list", {"kind": "notifications"}, st.ctx())
    assert not r.is_error, r.content
    reads = st.store.credential_reads(cid)
    assert len(reads) == 1 and reads[0]["reason"] == REASON_EXPIRED
    assert st.store.get(cid).last_auto_login_at is not None
    r = st.source.call("sites_list", {"kind": "messages"}, st.ctx())
    assert not r.is_error and len(st.store.credential_reads(cid)) == 1


def test_two_factor_code_by_request_never_stored(tmp_path):
    st = make_stack(mfa="code", tmp=tmp_path)
    t, out = _connect_async(st, {"site": "LinkedIn", "username": "alex@example.org"})
    st.answer("password", CANARY_PASSWORD)
    t.join(5)
    st.answer("code", CANARY_CODE)
    cid = json.loads(out["r"].content)["id"]
    assert st.service.wait_flow(cid, 5).status == "connected"
    assert CANARY_CODE not in json.dumps(st.vault.values)


def test_code_not_given_in_time_abandons_cleanly(tmp_path):
    st = make_stack(mfa="code", tmp=tmp_path)
    import nokker.sites.requests as rq

    old = dict(rq.TTL_S)
    rq.TTL_S["code"] = 0.3
    try:
        t, out = _connect_async(st, {"site": "LinkedIn", "username": "alex@example.org"})
        st.answer("password", CANARY_PASSWORD)
        t.join(5)
        cid = json.loads(out["r"].content)["id"]
        flow = st.service.wait_flow(cid, 5)
        assert flow.status == "failed" and "code" in flow.reason
        assert st.store.get(cid).last_ok_at is None
    finally:
        rq.TTL_S.update(old)


def test_app_approval_then_done(tmp_path):
    st = make_stack(mfa="app", tmp=tmp_path)
    t, out = _connect_async(st, {"site": "LinkedIn", "username": "alex@example.org"})
    st.answer("password", CANARY_PASSWORD)
    t.join(5)
    st.server.app_approved.add("alex@example.org")
    st.answer("app_approval", "")
    cid = json.loads(out["r"].content)["id"]
    assert st.service.wait_flow(cid, 5).status == "connected"


def test_magic_link_fetched_from_mailbox_never_shown(tmp_path):
    st = make_stack(skin=FAKESOCIAL, mfa="link", tmp=tmp_path)
    r = st.source.call("sites_connect", {"site": "FakeSocial", "username": "alex@example.org"}, st.ctx())
    res = json.loads(r.content)
    flow = st.service.wait_flow(res["id"], 5)
    assert flow.status == "connected", flow
    link = st.server.outbox[-1]["link"]
    assert link not in r.content and link not in st.everything()
    assert st.mail  # la boîte a été lue par le noyau, pas par le modèle


def test_magic_link_without_mailbox_is_asked_by_request(tmp_path):
    st = make_stack(skin=FAKESOCIAL, mfa="link", mail_links=False, tmp=tmp_path)
    t, out = _connect_async(st, {"site": "FakeSocial", "username": "alex@example.org"})
    t.join(5)
    res = json.loads(out["r"].content)
    assert res["status"] == "needs_link" and "secure_link" in res
    st.answer("link", st.server.outbox[-1]["link"])
    assert st.service.wait_flow(res["id"], 5).status == "connected"


def test_captcha_goes_to_handoff_and_resumes(tmp_path):
    st = make_stack(mfa="captcha", tmp=tmp_path)
    t, out = _connect_async(st, {"site": "LinkedIn", "username": "alex@example.org"})
    st.answer("password", CANARY_PASSWORD)
    t.join(5)
    cid = json.loads(out["r"].content)["id"]
    st.server.captcha_solved.add("alex@example.org")  # la personne fait l'étape en VNC
    assert st.service.wait_flow(cid, 5).status == "connected"
    assert any(ev == "handoff" for _, ev, _ in st.notes)


def test_bad_password_suspends_without_retry(tmp_path):
    st = make_stack(tmp=tmp_path)
    t, out = _connect_async(st, {"site": "LinkedIn", "username": "alex@example.org"})
    st.answer("password", "Faux-Mot2Passe!")
    t.join(5)
    cid = json.loads(out["r"].content)["id"]
    assert st.service.wait_flow(cid, 5).status == "suspended"
    assert f"sites.{cid}.password" not in st.vault.values  # un mot de passe refusé n'est pas rangé
    tries = [x for x in st.server.typed if x == "Faux-Mot2Passe!"]
    assert len(tries) == 1
    r = st.source.call("sites_list", {"kind": "notifications"}, st.ctx())
    assert r.is_error and r.content.startswith("site_suspended")


def test_unusual_activity_pauses_and_blocks_next_gestures(tmp_path):
    st = make_stack(tmp=tmp_path)
    connect_ok(st)
    st.server.unusual = True
    r = st.source.call("sites_list", {"kind": "notifications"}, st.ctx())
    assert r.is_error and r.content.startswith("site_paused")
    st.server.unusual = False
    views = st.server.page_views
    r = st.source.call("sites_list", {"kind": "notifications"}, st.ctx())
    assert r.is_error and r.content.startswith("site_paused") and st.server.page_views == views


def test_quota_on_searches(tmp_path):
    st = make_stack(tmp=tmp_path)
    connect_ok(st)
    codes = []
    for _ in range(8):
        r = st.source.call("sites_search", {"kind": "people", "query": "martin"}, st.ctx())
        codes.append(r.reason if r.is_error else "ok")
    assert codes[:6] == ["ok"] * 6 and codes[6] == "quota"


def test_one_driver_per_connector_busy(tmp_path):
    st = make_stack(tmp=tmp_path)
    cid = connect_ok(st)
    gov = st.service.gov(st.store.get(cid))
    with gov.driver(cid):
        r = st.source.call("sites_list", {"kind": "notifications"}, st.ctx())
    assert r.is_error and r.content.startswith("busy") and "propose de réessayer plus tard" in r.content


def test_two_accounts_same_site_labels_and_resolution(tmp_path):
    st = make_stack(tmp=tmp_path)
    connect_ok(st)
    from nokker.sites.fakesocial import Account

    st.server.accounts["pro@example.org"] = Account("pro@example.org", "Pro-Pass!9921", "Alex Pro")
    t, out = _connect_async(st, {"site": "LinkedIn", "username": "pro@example.org", "label": "LinkedIn pro"})
    st.answer("password", "Pro-Pass!9921")
    t.join(5)
    pro = json.loads(out["r"].content)["id"]
    assert st.service.wait_flow(pro, 5).status == "connected"
    r = st.source.call("sites_list", {"kind": "notifications"}, st.ctx())
    assert r.is_error and r.content.startswith("argument_invalide : plusieurs sites accessibles")
    r = st.source.call("sites_list", {"kind": "notifications", "site": "linkedin PRO"}, st.ctx())
    assert not r.is_error and "« LinkedIn pro »" in r.content
    assert [c.label for c in st.store.of(OWNER)] == ["LinkedIn", "LinkedIn pro"]


def test_profile_at_rest_is_encrypted_and_bound_to_its_generation(tmp_path):
    st = make_stack(tmp=tmp_path)
    cid = connect_ok(st)
    raw = st.profiles.raw(OWNER, cid)
    assert b"session" not in raw
    session = st.browsers[-1].cookies["session"]
    assert session.encode() not in raw
    import pytest

    from nokker.sites.crypto import open_profile

    blob = raw.split(b"\n", 1)[1]
    with pytest.raises(Exception):
        open_profile(b"k" * 32, OWNER, cid, 999, blob)
    with pytest.raises(Exception):
        open_profile(b"k" * 32, "camille", cid, 1, blob)


def test_state_events_for_the_screen(tmp_path):
    st = make_stack(tmp=tmp_path)
    seen = []
    conn = st.service.add(OWNER, "LinkedIn", "alex@example.org")
    st.service.subscribe(conn.id, lambda ev: seen.append(ev["state"]))
    st.service.connect(conn, wait_s=0.5)
    st.answer("password", CANARY_PASSWORD)
    st.service.wait_flow(conn.id, 5)
    assert seen[0] == "connecting" and "needs_password" in seen and seen[-1] == "connected"
    assert State.CONNECTED.value == "connected"


def _paused(tmp_path):
    st = make_stack(tmp=tmp_path)
    cid = connect_ok(st)
    st.server.unusual = True
    assert st.source.call("sites_list", {"kind": "notifications"}, st.ctx()).content.startswith("site_paused")
    assert st.store.get(cid).state.value == "paused"
    return st, cid


def test_lift_pause_back_to_connected_when_session_valid(tmp_path):
    """Levée par l'utilisateur : une page d'accueil vérifie la session → `connected`, signal effacé, lecture reprise."""
    from nokker.sites.http import SitesHttp

    st, cid = _paused(tmp_path)
    st.server.unusual = False
    views = st.server.page_views
    status, card = SitesHttp(st.service, lambda: OWNER).handle("DELETE", f"/v1/local/sites/{cid}/pause", None)
    st.service.wait_flow(cid, 5)
    c = st.store.get(cid)
    assert status == 200 and c.state.value == "connected" and c.signal == "" and c.paused_until is None
    assert st.server.page_views == views + 1  # une seule page
    r = st.source.call("sites_list", {"kind": "notifications"}, st.ctx())
    assert not r.is_error and "untrusted-data" in r.content


def test_lift_pause_session_gone_is_expired(tmp_path):
    st, cid = _paused(tmp_path)
    st.server.unusual = False
    st.server.expire_all()
    st.service.lift_pause(st.store.get(cid), wait_s=5)
    assert st.store.get(cid).state.value == "expired"


def test_lift_pause_site_still_restricted_pauses_again(tmp_path):
    st, cid = _paused(tmp_path)
    st.service.lift_pause(st.store.get(cid), wait_s=5)
    c = st.store.get(cid)
    assert c.state.value == "paused" and c.signal == "unusual_activity" and c.paused_until
    assert [n for n in st.notes if n[1] == "paused"][-1][2] == {"signal": "unusual_activity"}


def test_lift_pause_not_paused_touches_nothing(tmp_path):
    st = make_stack(tmp=tmp_path)
    cid = connect_ok(st)
    views = st.server.page_views
    st.service.lift_pause(st.store.get(cid), wait_s=1)
    assert st.store.get(cid).state.value == "connected" and st.server.page_views == views


def test_needs_link_is_never_returned_before_its_request(tmp_path):
    """Course du 06.11 (vue sous charge) : « needs_link » sortait avant l'ouverture de la demande, d'où « une demande
    est partie … : . » sans lien. L'ouverture est BLOQUÉE par un événement : pendant ce temps, sites_connect ne rend
    jamais needs_link ; une fois la demande ouverte, needs_link vient avec son lien."""
    st = make_stack(skin=FAKESOCIAL, mfa="link", mail_links=False, tmp=tmp_path)
    entered, release = threading.Event(), threading.Event()
    real_open = st.requests.open

    def gated_open(*a, **kw):
        entered.set()
        assert release.wait(10)
        return real_open(*a, **kw)

    st.requests.open = gated_open
    t, out = _connect_async(st, {"site": "FakeSocial", "username": "alex@example.org"})
    assert entered.wait(10)  # le flux est exactement entre « lien envoyé » et l'ouverture de la demande
    t.join(10)
    first = json.loads(out["r"].content)
    assert first["status"] == "connecting" and "secure_link" not in first and ": ." not in first["message"]
    opened = threading.Event()
    st.service.subscribe(first["id"], lambda ev: ev.get("request") and opened.set())  # demande posée avec l'état
    release.set()
    assert opened.wait(10)
    again = json.loads(st.source.call("sites_connect", {"site": first["site"]}, st.ctx()).content)
    assert again["status"] == "needs_link" and again["secure_link"].endswith(st.requests.open_for(first["id"]).id)
    assert again["secure_link"] in again["message"]
