"""Écarts remontés par l'écran (nokker-front, 05.11) : consents, mineur, fonction éteinte, passation, annuler,
answer_here, signal, lien sécurisé, code faux."""
from __future__ import annotations

import json
import threading
import time

from nokker.local.api import LocalApi
from nokker.sites.http import SitesHttp
from nokker.sites.model import STATE_TEXT

from tests.unit.sites.helpers import CANARY_CODE, CANARY_PASSWORD, OWNER, make_stack


def _wait_state(st, cid, state, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        c = st.store.get(cid)
        if c is not None and c.state.value == state:
            return c
        time.sleep(0.01)
    raise AssertionError(f"{cid} jamais en {state} ({st.store.get(cid).state.value})")


def test_list_has_consents_and_enabled(tmp_path):
    st = make_stack(tmp=tmp_path)
    code, body = SitesHttp(st.service, lambda: OWNER).handle("GET", "/v1/local/sites", None)
    assert code == 200 and body["enabled"] is True and body["consents"] == ["fakesocial", "linkedin"]


def test_minor_gets_403_everywhere(tmp_path):
    st = make_stack(tmp=tmp_path)
    http = SitesHttp(st.service, lambda: "kid", minor=lambda a: a == "kid")
    for method, path in (("GET", "/v1/local/sites"), ("POST", "/v1/local/sites"), ("GET", "/v1/local/sites/st_x")):
        code, body = http.handle(method, path, {})
        assert code == 403 and body["error"]["code"] == "minor"


def test_disabled_feature_is_readable_not_a_silent_404(tmp_path):
    root = tmp_path / "p"
    (root / "a").mkdir(parents=True)
    api = LocalApi.from_config({"token_path": str(tmp_path / "tok"), "code": {"roots": [str(root)], "backend": "local"}},
                               port=8742)
    code, body = api.handle("GET", "/v1/local/sites", f"Bearer {api.token}", {})
    assert code == 200 and body["enabled"] is False and body["reason"] == "disabled"
    code, body = api.handle("POST", "/v1/local/sites", f"Bearer {api.token}", {})
    assert code == 409 and body["enabled"] is False


def test_signal_code_and_state_text_rules(tmp_path):
    st = make_stack(tmp=tmp_path)
    http = SitesHttp(st.service, lambda: OWNER)
    code, card = http.handle("POST", "/v1/local/sites", {"site": "LinkedIn", "username": "alex@example.org"})
    cid = card["id"]
    c = _wait_state(st, cid, "needs_password")
    code, card = http.handle("GET", f"/v1/local/sites/{cid}", None)
    assert card["signal"] == "" and card["state_text"] == "Mot de passe demandé dans l'application Nokker"
    for text in STATE_TEXT.values():
        assert "Session" not in text and "CAPTCHA" not in text
    st.requests.cancel(card["request"]["id"])
    flow = st.service.wait_flow(cid, 5)
    assert flow.reason == "password_timeout"
    code, card = http.handle("GET", f"/v1/local/sites/{cid}", None)
    assert card["signal"] == "password_timeout" and card["state_text"] == ("Connexion abandonnée : mot de passe non "
                                                                          "donné à temps")
    assert c is not None


def test_cancel_a_connection_in_progress(tmp_path):
    st = make_stack(tmp=tmp_path)
    http = SitesHttp(st.service, lambda: OWNER)
    code, card = http.handle("POST", "/v1/local/sites", {"site": "LinkedIn", "username": "alex@example.org"})
    cid = card["id"]
    _wait_state(st, cid, "needs_password")
    code, body = http.handle("POST", f"/v1/local/sites/{cid}/cancel", {})
    assert code == 202
    assert st.service.wait_flow(cid, 5).reason == "cancelled"
    code, card = http.handle("GET", f"/v1/local/sites/{cid}", None)
    assert card["signal"] == "cancelled" and "request" not in card
    code, body = http.handle("POST", f"/v1/local/sites/{cid}/cancel", {})
    assert code == 409


def test_answer_here_delays_the_telegram_request(tmp_path):
    st = make_stack(tmp=tmp_path)
    http = SitesHttp(st.service, lambda: OWNER)
    code, card = http.handle("POST", "/v1/local/sites", {"site": "LinkedIn", "username": "alex@example.org",
                                                          "answer_here": True})
    cid = card["id"]
    _wait_state(st, cid, "needs_password")
    assert not [n for n in st.notes if n[1] == "request"]  # rien n'est parti sur Telegram
    st.answer("password", CANARY_PASSWORD)
    assert st.service.wait_flow(cid, 5).status == "connected"
    assert not [n for n in st.notes if n[1] == "request"]


def test_needs_you_handoff_url_single_use_owner_only(tmp_path):
    st = make_stack(mfa="captcha", tmp=tmp_path)
    st.service.handoff_wait_s = 60.0
    http = SitesHttp(st.service, lambda: OWNER)
    code, card = http.handle("POST", "/v1/local/sites", {"site": "LinkedIn", "username": "alex@example.org"})
    cid = card["id"]
    _wait_state(st, cid, "needs_password")
    st.answer("password", CANARY_PASSWORD)
    _wait_state(st, cid, "needs_you")
    code, card = http.handle("GET", f"/v1/local/sites/{cid}", None)
    url = card["handoff_url"]
    assert url.startswith("https://app.nokker.ai/sites/h/") and card["signal"] == "captcha"
    token = url.rsplit("/", 1)[1]
    other = SitesHttp(st.service, lambda: "camille")
    assert other.handle("POST", f"/v1/local/sites/handoff/{token}", {})[0] == 410  # pas à elle
    code, body = http.handle("POST", f"/v1/local/sites/handoff/{token}", {})
    assert code == 200 and body["screen_url"].endswith(f"/sites/screen/{cid}")
    assert http.handle("POST", f"/v1/local/sites/handoff/{token}", {})[0] == 410  # usage unique
    st.server.captcha_solved.add("alex@example.org")
    assert st.service.wait_flow(cid, 5).status == "connected"


def test_handoff_url_exists_as_soon_as_needs_you_is_visible(tmp_path):
    """Course du 07.11 (test ci-dessus instable) : l'état `needs_you` était publié AVANT la création du lien de passation ;
    une lecture de la carte à cet instant n'avait pas de `handoff_url`. Vérifié au moment même où l'état change."""
    st = make_stack(mfa="captcha", tmp=tmp_path)
    st.service.handoff_wait_s = 60.0
    seen = []
    orig = st.service._flow_set

    def spy(conn, state, *a, **kw):
        out = orig(conn, state, *a, **kw)
        if getattr(state, "value", state) == "needs_you":
            seen.append(st.service.handoff_url(conn))
        return out

    st.service._flow_set = spy
    http = SitesHttp(st.service, lambda: OWNER)
    code, card = http.handle("POST", "/v1/local/sites", {"site": "LinkedIn", "username": "alex@example.org"})
    cid = card["id"]
    _wait_state(st, cid, "needs_password")
    st.answer("password", CANARY_PASSWORD)
    _wait_state(st, cid, "needs_you")
    assert seen and seen[0] is not None and seen[0].startswith("https://app.nokker.ai/sites/h/")
    st.server.captcha_solved.add("alex@example.org")
    assert st.service.wait_flow(cid, 5).status == "connected"


def test_secure_link_format(tmp_path):
    st = make_stack(tmp=tmp_path)
    st.service.secure_link = lambda rid: f"{st.service.origin}/sites/r/{rid}"
    st.service.origin = "https://alex.nokker.app"
    r = st.source.call("sites_connect", {"site": "LinkedIn", "username": "alex@example.org"}, st.ctx())
    link = json.loads(r.content)["secure_link"]
    assert link.startswith("https://alex.nokker.app/sites/r/rq_")


def test_wrong_code_reopens_one_code_request_then_gives_up(tmp_path):
    st = make_stack(mfa="code", tmp=tmp_path)
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", st.source.call(
        "sites_connect", {"site": "LinkedIn", "username": "alex@example.org"}, st.ctx())))
    t.start()
    st.answer("password", CANARY_PASSWORD)
    t.join(5)
    cid = json.loads(out["r"].content)["id"]
    first = st.answer("code", "111111")
    second = st.answer("code", "222222")  # le site a refusé : UNE nouvelle demande `code`
    assert first.id != second.id
    assert any(ev == "wrong_code" for _, ev, _ in st.notes)
    flow = st.service.wait_flow(cid, 5)
    assert flow.status == "failed" and flow.reason == "code_rejected"
    assert CANARY_CODE not in json.dumps(st.vault.values)


def test_wrong_code_then_right_code_connects(tmp_path):
    st = make_stack(mfa="code", tmp=tmp_path)
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", st.source.call(
        "sites_connect", {"site": "LinkedIn", "username": "alex@example.org"}, st.ctx())))
    t.start()
    st.answer("password", CANARY_PASSWORD)
    t.join(5)
    cid = json.loads(out["r"].content)["id"]
    st.answer("code", "111111")
    st.answer("code", CANARY_CODE)
    assert st.service.wait_flow(cid, 5).status == "connected"
