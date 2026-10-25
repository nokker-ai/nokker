"""Routes /v1/household* (nokker/household/http.py) : drapeau éteint = 404 comme avant ; contrat de §7.29 ; erreurs
`{"error": {"code", "message"}}` ; la vue ne porte jamais de contenu."""
from __future__ import annotations

import json

import pytest

from nokker.household import http as household_http
from nokker.household.model import Settings, consent_text
from nokker.household.service import Household
from nokker.household.store import MemoryHouseholdStore
from nokker.team.service import Team
from nokker.tenancy.context import tenant_scope
from nokker.tenancy.store import MemoryTenancy


class H:
    def __init__(self, path: str, body=None) -> None:
        self.path, self.body, self.out = path, body or {}, None
        self.headers: dict = {}
        self.client_address = ("198.51.100.4", 0)

    def _read_json_body(self):
        return self.body

    def _json(self, status, payload):
        self.out = (status, json.loads(json.dumps(payload)))


@pytest.fixture
def w():
    tenancy = MemoryTenancy()
    pay, mem, kid = (tenancy.create_account(f"{n}@example.com") for n in ("payeur", "membre", "ado"))
    hh = Household(team=Team(store=MemoryHouseholdStore(), tenancy=tenancy), settings=Settings(minors=True))
    household_http.configure(hh)
    yield hh, pay, mem, kid
    household_http.configure(None)


def call(who, method, path, body=None):
    h = H(path, body)
    with tenant_scope(who.tenant_id):
        assert household_http.handle(h, method, path, who)
    return h.out


def test_flag_off_means_no_route():
    household_http.configure(None)
    assert household_http.handle(H("/v1/household"), "GET", "/v1/household", object()) is False
    assert not household_http.enabled()


def test_full_flow_through_the_routes(w):
    _hh, pay, mem, kid = w
    assert call(pay, "GET", "/v1/household")[0] == 404
    status, view = call(pay, "POST", "/v1/household", {"name": "Maison"})
    assert status == 201 and view["you"]["payer"] is True
    status, inv = call(pay, "POST", "/v1/household/invitations", {"email": mem.email})
    assert status == 201 and inv["profile"] == "adult"
    status, got = call(mem, "GET", "/v1/household/invitations/received")
    assert [i["id"] for i in got["invitations"]] == [inv["id"]]
    status, view = call(mem, "POST", f"/v1/household/invitations/{inv['id']}/accept")
    assert status == 200 and view["you"] == {"account_id": mem.account_id, "payer": False, "profile": "adult"}
    status, line = call(pay, "PUT", f"/v1/household/members/{mem.account_id}/cap", {"turns_per_day": 10})
    assert (status, line["cap"]) == (200, {"turns_per_day": 10, "source": "custom"})
    status, err = call(mem, "PUT", f"/v1/household/members/{mem.account_id}/cap", {"turns_per_day": 99})
    assert (status, err["error"]["code"]) == (403, "forbidden")
    status, out = call(mem, "PUT", "/v1/household/notes/courses", {"kind": "fact", "title": "Courses",
                                                                    "text": "Le marché est le samedi matin."})
    assert status == 200 and out["accepted"] == 1 and out["note"]["name"] == "courses"
    assert call(pay, "GET", "/v1/household/notes")[1]["notes"][0]["text"] == "Le marché est le samedi matin."
    status, doc = call(pay, "GET", "/v1/household/consent?lang=en")
    assert doc["version"] == consent_text("en").sha256 and doc["lang"] == "en" and "guardian" in doc["text"]
    status, inv2 = call(pay, "POST", "/v1/household/invitations", {
        "email": kid.email, "profile": "teen",
        "attestation": {"version": consent_text("fr").sha256, "lang": "fr", "legal_guardian": True}})
    assert status == 201 and inv2["profile"] == "teen"
    call(kid, "POST", f"/v1/household/invitations/{inv2['id']}/accept")
    status, line = call(pay, "DELETE", f"/v1/household/members/{kid.account_id}/consent")
    assert line["status"] == "suspended"
    assert call(pay, "GET", "/v1/household/consents")[1]["consents"][0]["active"] is False
    status, pol = call(pay, "PUT", "/v1/household/policy", {"minor_connectors": ["calendar"]})
    assert pol == {"minor_connectors": ["calendar"], "minor_agents": []}
    assert call(pay, "GET", "/v1/household/approvals")[0] == 404  # cartes : seulement avec les approbations
    assert call(mem, "DELETE", f"/v1/household/members/{mem.account_id}") == (200, {"removed": True})
    assert call(pay, "DELETE", "/v1/household") == (200, {"deleted": True})


def test_view_never_carries_content(w):
    _hh, pay, mem, _ = w
    call(pay, "POST", "/v1/household", {})
    inv = call(pay, "POST", "/v1/household/invitations", {"email": mem.email})[1]
    call(mem, "POST", f"/v1/household/invitations/{inv['id']}/accept")
    call(mem, "PUT", "/v1/household/notes/secret-de-famille", {"kind": "fact", "title": "Clé",
                                                              "text": "La clé est sous le pot de fleurs."})
    view = json.dumps(call(pay, "GET", "/v1/household")[1])
    assert "pot de fleurs" not in view and "secret-de-famille" not in view


def test_unknown_path_and_rate_limit(w):
    _hh, pay, *_ = w
    assert call(pay, "GET", "/v1/household/nimporte")[0] == 404
    call(pay, "POST", "/v1/household", {})
    for _ in range(household_http.RATE_MAX - 1):
        call(pay, "PUT", "/v1/household/policy", {})
    assert call(pay, "PUT", "/v1/household/policy", {})[0] == 429
