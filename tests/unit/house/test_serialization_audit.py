"""Audit de la vue client de la salle Maison (docs/HOUSE-ROOM.md §6) : ce qu'un membre reçoit par GET /v1/house et
GET /v1/house/messages ne contient JAMAIS le compte (`account_id`) ni le locataire (`tenant_id`) d'un autre membre, et
les cartes d'un message (`approvals`) ne sont montrées qu'à son auteur (`mine: true`). `RoomMessage.to_json` le fait
(nokker/house/store.py) ; ce test le garantit, par la route HTTP comme par la sérialisation directe."""
from __future__ import annotations

import json
from types import SimpleNamespace

from nokker.house import http as house_http
from nokker.house.store import RoomMessage
from nokker.tenancy.context import tenant_scope
from tests.unit.house.conftest import make_world, messages, post


class _H:
    """Faux gestionnaire HTTP : garde la dernière réponse JSON."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.status: int | None = None
        self.body: object = None

    def _json(self, status: int, body: object) -> None:
        self.status, self.body = status, body


def _get(w, who, path: str):
    h = _H(path)
    with tenant_scope(who.tenant_id):
        assert house_http.handle(h, "GET", path, SimpleNamespace(account_id=who.account_id)) is True
    assert h.status == 200, h.body
    return h.body


def _strings(obj) -> list[str]:
    """Toutes les clés et valeurs (chaînes) d'une réponse JSON, à toute profondeur."""
    if isinstance(obj, dict):
        return [s for k, v in obj.items() for s in (str(k), *_strings(v))]
    if isinstance(obj, list):
        return [s for v in obj for s in _strings(v)]
    return [obj] if isinstance(obj, str) else []


def _ids_of_others(w, who) -> set[str]:
    members = (w.pay, w.adult, w.teen, w.child)
    return {x for m in members if m.account_id != who.account_id for x in (m.account_id, m.tenant_id)} - {
        who.account_id, who.tenant_id}


def test_house_views_never_carry_another_members_account_or_tenant(tmp_path):
    w = make_world(tmp_path)
    house_http.configure(w.room)
    try:
        w.brain.plan["maison"] = [("tool", "maison_fort", {}), ("text", "Il faut confirmer.")]
        post(w, w.adult, "@maison déverrouille la porte")  # message d'agent qui porte une carte de bob
        post(w, w.pay, "bonjour la maison")
        post(w, w.teen, "@veille du nouveau ?")
        for who in (w.pay, w.adult, w.teen, w.child):
            others = _ids_of_others(w, who)
            for body in (_get(w, who, "/v1/house"), _get(w, who, "/v1/house/messages")):
                blob = json.dumps(body, ensure_ascii=False)
                leaked = [x for x in others if x in blob]
                assert not leaked, (who.email, leaked)
                assert not any(x in s for s in _strings(body) for x in others)
    finally:
        house_http.configure(None)


def test_approvals_only_in_the_authors_view(tmp_path):
    w = make_world(tmp_path)
    house_http.configure(w.room)
    try:
        w.brain.plan["maison"] = [("tool", "maison_fort", {}), ("text", "Il faut confirmer.")]
        out = post(w, w.adult, "@maison déverrouille la porte")
        assert out["reply"]["approvals"] and out["reply"]["mine"] is True
        for who in (w.pay, w.adult, w.teen):
            rows = _get(w, who, "/v1/house/messages")["messages"]
            for r in rows:
                assert ("approvals" in r) == (r["mine"] is True and r["id"] == out["reply"]["id"]), (who.email, r)
            assert rows == messages(w, who)
    finally:
        house_http.configure(None)


def test_to_json_for_another_member_drops_ids_and_cards():
    m = RoomMessage(id=7, household_id="hh_1", tenant_id="t_payer_secret", kind="agent", text="ok", at=1.0,
                    author_account="acc_bob_secret", author_label="membre B (adulte)", agent="maison",
                    origin={"kind": "room", "channel": "web", "thread_id": "th_secret", "tenant_id": "t_payer_secret"},
                    approvals=["card_1"])
    other = m.to_json("acc_alice")
    blob = json.dumps(other)
    assert "acc_bob_secret" not in blob and "t_payer_secret" not in blob and "th_secret" not in blob
    assert other["mine"] is False and "approvals" not in other
    assert m.to_json(None)["mine"] is False and "approvals" not in m.to_json(None)
    mine = m.to_json("acc_bob_secret")
    assert mine["mine"] is True and mine["approvals"] == ["card_1"]
    assert "t_payer_secret" not in json.dumps(mine)
