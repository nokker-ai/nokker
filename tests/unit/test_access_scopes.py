"""Portées des sessions d'accès (docs/KIOSK.md §3) : `kiosk` ne lit que sa vue ; `present` parle et affiche, mais ne
gère rien, ne TRANCHE JAMAIS une carte, n'appelle aucun outil, n'exporte ni n'efface ; 12 h fixes ; révocation par
portée à l'arrêt du lanceur. Refus dès `parse_request` (403 scope_<portée>), vrai serveur HTTP en boucle locale."""
from __future__ import annotations

import pytest

from nokker.local.access_sessions import PRESENT_TTL_S, SESSION_TTL_S, AccessError, scope_allows
from tests.unit.test_access_sessions import call, served, svc  # noqa: F401 — fixture partagée


@pytest.mark.parametrize("method,path,ok", [
    ("GET", "/v1/local/kiosk", True), ("GET", "/v1/local/kiosk/", True), ("GET", "/healthz", True),
    ("GET", "/v1/capabilities", True), ("POST", "/v1/local/kiosk", False), ("GET", "/v1/threads", False),
    ("GET", "/v1/approvals", False), ("POST", "/v1/chat/completions", False), ("GET", "/v1/local/setup", False),
])
def test_kiosk_is_a_strict_get_whitelist(method, path, ok):
    assert scope_allows("kiosk", method, path) is ok


@pytest.mark.parametrize("method,path,ok", [
    ("GET", "/v1/threads", True), ("POST", "/v1/chat/completions", True), ("GET", "/v1/events", True),
    ("GET", "/v1/approvals", True), ("GET", "/v1/local/approvals", True),  # une carte se lit…
    ("POST", "/v1/local/approvals/ap_1/decide", False), ("POST", "/v1/approvals/ap_1/decision", False),  # …ne se tranche pas
    ("PATCH", "/v1/approvals/ap_1", False), ("DELETE", "/v1/devices/dv_1", False),
    ("POST", "/v1/browser/sessions/bs_12345678/narrations/1/speech/timed", True),
    ("POST", "/v1/local/session-code", False), ("GET", "/v1/local/access-sessions", False),
    ("GET", "/v1/local/setup", False), ("POST", "/v1/local/setup/done", False), ("GET", "/v1/local/accord", False),
    ("PUT", "/v1/local/settings", False), ("GET", "/v1/connectors", False), ("POST", "/v1/local/tools/call", False),
    ("GET", "/v1/threads/th_1/export", False), ("POST", "/v1/local/erase", False), ("POST", "/v1/account/erase", False),
])
def test_present_talks_and_shows_but_never_decides(method, path, ok):
    assert scope_allows("present", method, path) is ok


def test_unknown_scope_allows_nothing_and_owner_everything():
    assert scope_allows("pirate", "GET", "/healthz") is False
    assert scope_allows("owner", "POST", "/v1/local/approvals/x/decide") is True


def test_scope_travels_from_code_to_session(tmp_path):
    s, c = svc(tmp_path)
    with pytest.raises(AccessError) as e:
        s.mint("pirate")
    assert (e.value.status, e.value.code) == (400, "bad_scope")
    m = s.mint("kiosk")
    assert m["scope"] == "kiosk"
    out = s.exchange(m["code"])
    assert out["scope"] == "kiosk" and out["expires_in"] == SESSION_TTL_S
    assert s.lookup(out["token"])[1] == "kiosk"
    assert s.mint()["scope"] == "owner"


def test_present_lasts_twelve_hours_fixed(tmp_path):
    s, c = svc(tmp_path)
    out = s.exchange(s.mint("present")["code"])
    assert out["expires_in"] == PRESENT_TTL_S
    c.t += PRESENT_TTL_S - 120
    assert s.lookup(out["token"])[1] == "present"  # utilisée : l'échéance ne bouge PAS
    c.t += 121
    assert s.lookup(out["token"]) is None


def test_revoke_scope_only_touches_that_scope(tmp_path):
    s, _ = svc(tmp_path)
    k = s.exchange(s.mint("kiosk")["code"])["token"]
    p = s.exchange(s.mint("present")["code"])["token"]
    o = s.exchange(s.mint()["code"])["token"]
    assert s.revoke_scope("kiosk") == 1
    assert s.lookup(k) is None and s.lookup(p) and s.lookup(o)
    with pytest.raises(AccessError):
        s.revoke_scope("pirate")


def _token(api, port, scope):
    code = call(port, "POST", "/v1/local/session-code", token=api.token, body={"scope": scope})[1]["code"]
    st, out = call(port, "POST", "/v1/local/session-code/exchange", body={"code": code})
    assert st == 200 and out["scope"] == scope
    return out["token"]


def test_kiosk_token_over_http(served):  # noqa: F811
    api, port = served
    tok = _token(api, port, "kiosk")
    st, body = call(port, "GET", "/v1/threads", token=tok)
    assert st == 403 and body["error"]["code"] == "scope_kiosk"
    assert call(port, "GET", "/v1/local/sessions", token=tok)[0] == 403
    assert call(port, "GET", "/v1/local/kiosk", token=tok)[0] != 403  # sa vue (404 ici : kiosque non configuré)
    assert call(port, "GET", "/v1/capabilities", token=tok)[0] == 200


def test_present_token_over_http_never_decides(served):  # noqa: F811
    api, port = served
    tok = _token(api, port, "present")
    assert call(port, "GET", "/v1/local/sessions", token=tok)[0] == 200  # le propriétaire, pour parler et afficher
    assert call(port, "GET", "/v1/approvals", token=tok)[0] not in (401, 403)
    for method, path in (("POST", "/v1/local/approvals/ap_x/decide"), ("POST", "/v1/approvals/ap_x/decision"),
                         ("POST", "/v1/local/session-code"), ("GET", "/v1/local/access-sessions"),
                         ("POST", "/v1/local/tools/call")):
        st, body = call(port, method, path, token=tok, body={})
        assert st == 403 and body["error"]["code"] == "scope_present", (method, path)


def test_launcher_revokes_by_scope_with_the_local_token_only(served):  # noqa: F811
    api, port = served
    k = _token(api, port, "kiosk")
    assert call(port, "DELETE", "/v1/local/access-sessions/scope/kiosk", token=k)[0] == 403  # pas par lui-même
    assert call(port, "DELETE", "/v1/local/access-sessions/scope/kiosk", token=api.token) == \
        (200, {"scope": "kiosk", "revoked": 1})
    assert call(port, "GET", "/v1/capabilities", token=k)[0] == 401
    assert call(port, "DELETE", "/v1/local/access-sessions/scope/pirate", token=api.token)[0] == 400


def test_exchange_with_the_wrong_expect_consumes_nothing_and_creates_no_session(tmp_path):
    s, _ = svc(tmp_path)
    owner = s.mint()["code"]
    with pytest.raises(AccessError) as e:
        s.exchange(owner, expect="kiosk")  # code owner ouvert sur /kiosk
    assert (e.value.status, e.value.code) == (409, "wrong_scope")
    assert s.list() == []  # aucune session orpheline
    assert s.exchange(owner, expect="owner")["scope"] == "owner"  # le code sert encore, une fois, à la bonne page
    k = s.mint("kiosk")["code"]
    assert s.exchange(k, expect="kiosk")["scope"] == "kiosk"
    with pytest.raises(AccessError) as e:
        s.exchange(s.mint("kiosk")["code"], expect="pirate")
    assert (e.value.status, e.value.code) == (400, "bad_scope")
    assert s.exchange(s.mint("present")["code"])["scope"] == "present"  # sans expect : inchangé


def test_wrong_expect_is_not_a_counted_failure(tmp_path):
    from nokker.local.access_sessions import FAILS_MAX

    s, _ = svc(tmp_path)
    code = s.mint()["code"]
    for _ in range(FAILS_MAX + 3):
        with pytest.raises(AccessError) as e:
            s.exchange(code, expect="kiosk")
        assert e.value.code == "wrong_scope"
    assert s.exchange(code)["token"]  # pas de 429 : un mauvais `expect` ne dit rien des codes vivants


def test_used_or_expired_code_says_410_before_409(tmp_path):
    from nokker.local.access_sessions import CODE_TTL_S

    s, c = svc(tmp_path)
    code = s.mint()["code"]
    s.exchange(code)
    with pytest.raises(AccessError) as e:
        s.exchange(code, expect="kiosk")
    assert (e.value.status, e.value.code) == (410, "code_used")
    late = s.mint()["code"]
    c.t += CODE_TTL_S + 1
    with pytest.raises(AccessError) as e:
        s.exchange(late, expect="kiosk")
    assert (e.value.status, e.value.code) == (410, "code_expired")


def test_expect_over_http(served):  # noqa: F811
    api, port = served
    code = call(port, "POST", "/v1/local/session-code", token=api.token, body={})[1]["code"]
    st, body = call(port, "POST", "/v1/local/session-code/exchange", body={"code": code, "expect": "kiosk"})
    assert st == 409 and body["error"]["code"] == "wrong_scope" and "token" not in body
    assert call(port, "GET", "/v1/local/access-sessions", token=api.token)[1]["sessions"] == []
    st, body = call(port, "POST", "/v1/local/session-code/exchange", body={"code": code, "expect": "owner"})
    assert st == 200 and body["scope"] == "owner"
