"""Sessions d'accès du web local (nokker/local/access_sessions.py) : un code court (usage unique, 60 s, au plus 5) échangé
SANS Authorization contre un jeton de session distinct, haché, 30 jours glissants, révocable ; droits du propriétaire
sauf émettre des codes et gérer les sessions ; tout refusé par la passerelle, jeton local compris (défense en
profondeur). Horloge à la main pour la classe ; vrai serveur HTTP en boucle locale pour les routes."""
from __future__ import annotations

import json
import stat
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from nokker.local.access_sessions import (
    CODE_TTL_S,
    CODES_MAX,
    FAILS_MAX,
    SESSION_TTL_S,
    AccessError,
    AccessSessions,
)
from tests.unit.test_local_api import _api


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


def svc(tmp_path):
    c = Clock()
    return AccessSessions(tmp_path / "access-sessions.json", clock=c), c


# ───────────────────────────── classe ─────────────────────────────

def test_code_exchanged_once_for_a_distinct_hashed_token(tmp_path):
    s, _ = svc(tmp_path)
    m = s.mint()
    assert m["expires_in"] == CODE_TTL_S and len(m["code"]) >= 22  # 128 bits en base64url
    out = s.exchange(m["code"], "Mozilla/5.0 " + "x" * 200)
    assert out["expires_in"] == SESSION_TTL_S and len(out["token"]) >= 43  # 256 bits
    raw = (tmp_path / "access-sessions.json").read_text()
    assert out["token"] not in raw and m["code"] not in raw  # haché, jamais en clair
    assert stat.S_IMODE((tmp_path / "access-sessions.json").stat().st_mode) == 0o600
    sid = s.verify(out["token"])
    assert sid and sid.startswith("as_")
    row = s.list()[0]
    assert set(row) == {"id", "created_at", "last_used_at", "expires_at", "agent", "scope"} and len(row["agent"]) == 60
    assert row["scope"] == "owner"
    with pytest.raises(AccessError) as e:
        s.exchange(m["code"])
    assert (e.value.status, e.value.code) == (410, "code_used")


def test_code_expires_after_60_s_and_unknown_code_says_expired(tmp_path):
    s, c = svc(tmp_path)
    code = s.mint()["code"]
    c.t += CODE_TTL_S + 1
    with pytest.raises(AccessError) as e:
        s.exchange(code)
    assert (e.value.status, e.value.code) == (410, "code_expired")
    with pytest.raises(AccessError) as e:
        s.exchange("A" * 22)  # inconnu : même réponse, rien n'est dit sur les codes vivants
    assert e.value.code == "code_expired"
    with pytest.raises(AccessError) as e:
        s.exchange("pas un code !")
    assert (e.value.status, e.value.code) == (400, "bad_code")


def test_at_most_five_live_codes(tmp_path):
    s, c = svc(tmp_path)
    for _ in range(CODES_MAX):
        s.mint()
    with pytest.raises(AccessError) as e:
        s.mint()
    assert (e.value.status, e.value.code) == (429, "too_many_codes")
    c.t += CODE_TTL_S + 1
    s.mint()  # les codes morts ne comptent plus


def test_ten_failed_exchanges_per_minute_then_429(tmp_path):
    s, c = svc(tmp_path)
    good = s.mint()["code"]
    for _ in range(FAILS_MAX):
        with pytest.raises(AccessError):
            s.exchange("B" * 22)
    with pytest.raises(AccessError) as e:
        s.exchange(good)  # même le bon code attend la fin de la fenêtre
    assert (e.value.status, e.value.code) == (429, "rate_limited")
    c.t += 61  # la fenêtre passe (le premier code a expiré entre-temps : 60 s) ; un code neuf passe
    assert s.exchange(s.mint()["code"])["token"]


def test_sliding_30_days_and_revocation(tmp_path):
    s, c = svc(tmp_path)
    tok = s.exchange(s.mint()["code"])["token"]
    c.t += SESSION_TTL_S - 10
    assert s.verify(tok)  # utilisé avant l'échéance : repoussée de 30 jours
    c.t += SESSION_TTL_S - 10
    assert s.verify(tok)
    c.t += SESSION_TTL_S + 1  # 30 jours sans usage : morte
    assert s.verify(tok) is None and s.list() == []
    tok2 = s.exchange(s.mint()["code"])["token"]
    sid = s.verify(tok2)
    assert s.revoke(sid) is True and s.verify(tok2) is None and s.revoke(sid) is False


def test_sessions_survive_a_restart_and_garbage_is_ignored(tmp_path):
    s, c = svc(tmp_path)
    tok = s.exchange(s.mint()["code"])["token"]
    again = AccessSessions(tmp_path / "access-sessions.json", clock=c)
    assert again.verify(tok)
    (tmp_path / "access-sessions.json").write_text("{pas du json")
    assert AccessSessions(tmp_path / "access-sessions.json", clock=c).verify(tok) is None


# ───────────────────────────── routes HTTP ─────────────────────────────

def _serve(api):
    from nokker.api.server import make_handler
    from nokker.local.api import LOCAL_OWNER
    from tests.unit.api.test_server import make_kernel

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(make_kernel("ok"), {api.token: LOCAL_OWNER},
                                                               local_api=api))
    port = httpd.server_address[1]
    api.allowed_hosts = frozenset({f"127.0.0.1:{port}"})
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port


def call(port, method, path, *, token=None, body=None, relay=False):
    headers = {"Content-Type": "application/json", "User-Agent": "test-agent/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if relay:
        headers["X-Nokker-Relay"] = "1"
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, headers=headers,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


@pytest.fixture
def served(tmp_path):
    api = _api(tmp_path)
    api.access_sessions = AccessSessions(tmp_path / "access-sessions.json")
    httpd, port = _serve(api)
    yield api, port
    httpd.shutdown()


def test_full_flow_mint_exchange_use_list_revoke(served):
    api, port = served
    st, m = call(port, "POST", "/v1/local/session-code", token=api.token)
    assert st == 200 and m["expires_in"] == 60
    st, out = call(port, "POST", "/v1/local/session-code/exchange", body={"code": m["code"]})  # SANS Authorization
    assert st == 200 and out["token"] != api.token
    tok = out["token"]
    assert call(port, "GET", "/v1/local/sessions", token=tok)[0] == 200  # droits du propriétaire sur /v1/local/*
    assert call(port, "GET", "/v1/approvals", token=tok)[0] != 401  # et hors de /v1/local (identité propriétaire)
    st, rows = call(port, "GET", "/v1/local/access-sessions", token=api.token)
    assert st == 200 and rows["sessions"][0]["agent"] == "test-agent/1.0"
    sid = rows["sessions"][0]["id"]
    assert call(port, "DELETE", f"/v1/local/access-sessions/{sid}", token=api.token) == (200, {"id": sid, "revoked": True})
    assert call(port, "GET", "/v1/local/sessions", token=tok)[0] == 401  # révoquée : plus rien


def test_session_token_cannot_mint_nor_manage_sessions(served):
    api, port = served
    tok = call(port, "POST", "/v1/local/session-code/exchange",
               body={"code": call(port, "POST", "/v1/local/session-code", token=api.token)[1]["code"]})[1]["token"]
    assert call(port, "POST", "/v1/local/session-code", token=tok)[0] == 401
    assert call(port, "GET", "/v1/local/access-sessions", token=tok)[0] == 401
    sid = call(port, "GET", "/v1/local/access-sessions", token=api.token)[1]["sessions"][0]["id"]
    assert call(port, "DELETE", f"/v1/local/access-sessions/{sid}", token=tok)[0] == 401


def test_everything_refused_through_the_relay(served):
    api, port = served
    code = call(port, "POST", "/v1/local/session-code", token=api.token)[1]["code"]
    tok = call(port, "POST", "/v1/local/session-code/exchange", body={"code": code})[1]["token"]
    code2 = call(port, "POST", "/v1/local/session-code", token=api.token)[1]["code"]
    assert call(port, "POST", "/v1/local/session-code", token=api.token, relay=True)[0] == 403
    assert call(port, "POST", "/v1/local/session-code/exchange", body={"code": code2}, relay=True)[0] == 403
    refused = (401, 403)
    assert call(port, "GET", "/v1/local/sessions", token=tok, relay=True)[0] in refused  # session : jamais par le relais
    assert call(port, "GET", "/v1/approvals", token=tok, relay=True)[0] in refused
    assert call(port, "GET", "/v1/local/sessions", token=api.token, relay=True)[0] in refused  # jeton local non plus
    assert call(port, "GET", "/v1/approvals", token=api.token, relay=True)[0] in refused  # défense en profondeur
    assert call(port, "GET", "/v1/approvals", token=api.token)[0] != 401  # sur la machine : inchangé


def test_wrong_and_reused_codes_over_http(served):
    api, port = served
    code = call(port, "POST", "/v1/local/session-code", token=api.token)[1]["code"]
    assert call(port, "POST", "/v1/local/session-code/exchange", body={"code": code})[0] == 200
    st, err = call(port, "POST", "/v1/local/session-code/exchange", body={"code": code})
    assert st == 410 and err["error"]["code"] == "code_used" and "token" not in err
    st, err = call(port, "POST", "/v1/local/session-code/exchange", body={"code": "x"})
    assert st == 400 and err["error"]["code"] == "bad_code"
