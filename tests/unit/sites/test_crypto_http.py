"""Saisie scellée (Python et sealed.js croisés), routes de l'écran, intégration au noyau."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from nokker.sites.crypto import MachineKeyring, SealedError, seal_value
from nokker.sites.http import SitesHttp
from nokker.sites.requests import RequestError

from tests.unit.sites.helpers import CANARY_PASSWORD, OWNER, connect_ok, make_stack

ROOT = Path(__file__).resolve().parents[3]


def test_sealed_roundtrip_single_use_and_expiry():
    t = [1000.0]
    ring = MachineKeyring(clock=lambda: t[0])
    jwk = ring.public_key("rq_1", 60)
    sealed = seal_value(jwk, "rq_1", CANARY_PASSWORD)
    assert CANARY_PASSWORD not in json.dumps(sealed)
    assert ring.open("rq_1", sealed) == CANARY_PASSWORD
    with pytest.raises(SealedError):
        ring.open("rq_1", sealed)  # usage unique
    jwk2 = ring.public_key("rq_2", 60)
    t[0] += 61
    with pytest.raises(SealedError):
        ring.open("rq_2", seal_value(jwk2, "rq_2", "x"))
    jwk3 = ring.public_key("rq_3", 60)
    with pytest.raises(SealedError):
        ring.open("rq_3", seal_value(jwk3, "rq_other", "x"))  # lié à SA demande


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent : test croisé JS sauté")
def test_sealed_js_matches_python(tmp_path):
    ring = MachineKeyring()
    jwk = ring.public_key("rq_js", 60)
    script = tmp_path / "t.mjs"
    script.write_text(f"""import {{ sealValue }} from {json.dumps(str(ROOT / 'nokker/sites/sealed.js'))};
const out = await sealValue({json.dumps(jwk)}, "rq_js", {json.dumps(CANARY_PASSWORD)});
console.log(JSON.stringify(out));""")
    sealed = json.loads(subprocess.run(["node", str(script)], capture_output=True, text=True, check=True).stdout)
    assert ring.open("rq_js", sealed) == CANARY_PASSWORD


def test_routes_list_add_consent_answer_no_plain_secret(tmp_path):
    st = make_stack(tmp=tmp_path)
    http = SitesHttp(st.service, lambda: OWNER)
    code, body = http.handle("GET", "/v1/local/sites", None)
    assert code == 200 and body["sites"] == [] and {c["site"] for c in body["catalog"]} >= {"linkedin"}
    code, body = http.handle("POST", "/v1/local/sites", {"site": "LinkedIn", "username": "a@b.c",
                                                          "password": "x"})
    assert code == 400 and body["error"]["code"] == "no_plain_secret"
    code, body = http.handle("POST", "/v1/local/sites", {"site": "LinkedIn", "username": "alex@example.org",
                                                          "label": "LinkedIn pro"})
    assert code == 201 and body["label"] == "LinkedIn pro"
    cid = body["id"]
    req = None
    for _ in range(300):
        code, card = http.handle("GET", f"/v1/local/sites/{cid}", None)
        if card.get("request"):
            req = card["request"]
            break
        import time

        time.sleep(0.01)
    assert req and req["kind"] == "password"
    code, pub = http.handle("GET", f"/v1/local/sites/requests/{req['id']}", None)
    assert code == 200 and pub["machine_key"]["crv"] == "P-256"
    code, body = http.handle("POST", f"/v1/local/sites/requests/{req['id']}/answer", {"value": CANARY_PASSWORD})
    assert code == 400 and body["error"]["code"] == "no_plain_secret"
    code, _ = http.handle("POST", f"/v1/local/sites/requests/{req['id']}/answer",
                          {"sealed": seal_value(pub["machine_key"], req["id"], CANARY_PASSWORD)})
    assert code == 204
    assert st.service.wait_flow(cid, 5).status == "connected"
    code, card = http.handle("GET", f"/v1/local/sites/{cid}", None)
    assert card["state"] == "connected" and card["state_text"] == "Connecté ✓"
    assert card["credentials"]["stored"] is True and CANARY_PASSWORD not in json.dumps(card)
    code, _ = http.handle("POST", f"/v1/local/sites/requests/{req['id']}/answer", {"sealed": {}})
    assert code in (404, 409, 410)
    code, card = http.handle("PATCH", f"/v1/local/sites/{cid}", {"label": "LI pro"})
    assert code == 200 and card["label"] == "LI pro"
    code, _ = http.handle("DELETE", f"/v1/local/sites/{cid}?forget_credentials=true", None)
    assert code == 200 and st.store.get(cid) is None and not st.vault.values


def test_routes_consent_required_and_owner_only(tmp_path):
    st = make_stack(tmp=tmp_path)
    cid = connect_ok(st)
    http = SitesHttp(st.service, lambda: "camille")
    code, body = http.handle("POST", "/v1/local/sites", {"site": "LinkedIn", "username": "c@x.org"})
    assert code == 409 and body["error"]["code"] == "consent_required"
    code, _ = http.handle("GET", f"/v1/local/sites/{cid}", None)
    assert code == 404  # pas partagé : invisible
    conn = st.store.get(cid)
    conn.grants["camille"] = "read"
    st.store.put(conn)
    code, card = http.handle("GET", f"/v1/local/sites/{cid}", None)
    assert code == 200 and "credentials" not in card and card["access"] == "read"
    code, _ = http.handle("DELETE", f"/v1/local/sites/{cid}", None)
    assert code == 403


def test_request_answer_by_other_account_refused(tmp_path):
    st = make_stack(tmp=tmp_path)
    req = st.requests.open("st_x", OWNER, "LinkedIn", "password")
    with pytest.raises(RequestError):
        st.requests.answer_sealed(req.id, seal_value(req.machine_key, req.id, "x"), "camille")


def test_kernel_integration_effects_family_web_fetch_provenance():
    from nokker.core.turn import Effect
    from nokker.qa.provenance import SOURCES
    from nokker.tools.effects import DEFAULT_EFFECTS
    from nokker.tools.select import FAMILIES
    from nokker.tools.web import WebToolSource

    assert DEFAULT_EFFECTS["sites_connect"] is Effect.WRITE and DEFAULT_EFFECTS["sites_list"] is Effect.READ
    fam = {f.label: f for f in FAMILIES}
    sites, bulles = fam["Sites connectés"], fam["Bulles"]
    for yes in ("regarde mes notifications linkedin", "mon insta", "ouvre x.com", "publier sur facebook",
                "inviter sur linkedin"):
        assert sites.match.search(yes), yes
    for no in ("mes messages", "publier", "inviter Paul", "X a dit quoi ?", "le prix de X"):
        assert not sites.match.search(no), no
    assert not bulles.match.search("linkedin") and not bulles.match.search("instagram")
    assert "site" in SOURCES
    web = WebToolSource(connected_site=lambda u: "LinkedIn" if "linkedin.com" in u else None)
    from nokker.core.ports import ToolContext

    r = web.call("web_fetch", {"urls": ["https://www.linkedin.com/in/x/"]}, ToolContext("t", 0, "api"))
    web.close()
    assert r.is_error and r.content == ("site_connected : « LinkedIn » est un site connecté ; pour ses pages, utilise "
                                        "sites_read site=LinkedIn.")


def test_bubble_show_rule_in_description():
    import inspect

    from nokker.bubbles import tools as bt

    src = inspect.getsource(bt)
    assert "Lire un profil ou un post \"\n                     \"d'un site connecté = sites_read" in src or \
        "d'un site connecté = sites_read ; montrer une page publique quelconque = bubble_show." in src
