"""« Ton Nokker » (V6) sur Lokkal : pas de compte nokker.ai, le titulaire est le propriétaire de la machine
(`local-owner`). Prouve, par le VRAI serveur : jeton local sur la machine → 200 (lecture, choix gardé, catalogue) ;
par la passerelle → 403 ; jeton faux ou absent, ou d'un membre → 401. Le cloud (comptes nokker.ai) reste couvert par
`test_avatar_speech.py::test_persona_routes`, inchangé."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from nokker import persona as persona_mod
from tests.unit.voice.test_avatar_speech import PERSONA


def _req(port, path, *, token=None, method="GET", body=None, headers=None):
    h = {"Content-Type": "application/json", **(headers or {})}
    if token:
        h["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, headers=h,
                                 data=json.dumps(body).encode() if body is not None else None)
    req.add_unredirected_header("Host", "127.0.0.1:8742")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


@pytest.fixture
def serve():
    from nokker.api.server import make_handler
    from tests.unit.api.test_server import make_kernel

    started = []
    persona_mod.configure(persona_mod.from_config(PERSONA, {}, None))

    def run(keys, local_api=None):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(make_kernel(), keys, local_api=local_api))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        started.append(httpd)
        return httpd.server_address[1]

    yield run
    for h in started:
        h.shutdown()
    persona_mod.configure(None)


def test_lokkal_owner_on_the_machine_has_a_persona(serve, tmp_path):
    from nokker.local.api import LOCAL_OWNER
    from tests.unit.test_local_api import _api

    api = _api(tmp_path)
    port = serve({api.token: LOCAL_OWNER}, local_api=api)
    st, cat = _req(port, "/v1/persona/catalog", token=api.token)
    assert st == 200 and cat["defaults"]["voice_id"] == "nina"
    st, p = _req(port, "/v1/persona", token=api.token)
    assert st == 200 and p["defaulted"] is True and p["avatar"]["id"] == "nina"
    st, p = _req(port, "/v1/persona", token=api.token, method="PUT", body={"name": "Ada"})
    assert st == 200 and p["name"] == "Ada"
    st, p = _req(port, "/v1/persona", token=api.token)
    assert st == 200 and p["name"] == "Ada" and p["defaulted"] is False  # gardé pour le titulaire local
    assert persona_mod.service().get(LOCAL_OWNER)["name"] == "Ada"
    st, out = _req(port, "/v1/persona", token=api.token, method="PUT", body={"voice_id": "zz"})
    assert st == 400 and out["error"]["code"] == "bad_voice"  # mêmes catalogues fermés qu'au cloud


def test_lokkal_refuses_the_gateway_and_strangers(serve, tmp_path):
    from nokker.local.api import LOCAL_OWNER
    from tests.unit.test_local_api import _api

    api = _api(tmp_path)
    port = serve({api.token: LOCAL_OWNER}, local_api=api)
    st, _ = _req(port, "/v1/persona", token=api.token, headers={"X-Nokker-Relay": "1"})
    assert st == 403  # par la passerelle : jamais (jeton local refusé en amont, ou persona_on_machine)
    st, _ = _req(port, "/v1/persona", token=api.token, method="PUT", body={"name": "Eve"},
                 headers={"X-Nokker-Relay": "1"})
    assert st == 403 and persona_mod.service().get(LOCAL_OWNER)["defaulted"] is True
    assert _req(port, "/v1/persona", token="faux")[0] == 401
    assert _req(port, "/v1/persona")[0] == 401
    assert _req(port, "/v1/persona", token="us_membre_du_foyer")[0] == 401
