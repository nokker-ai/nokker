"""Source d'alertes authentifiée par jeton Bearer (lot fix-batch-1, défaut 4 ; docs/LOKKAL.md §7.18.1).

Alertmanager ne sait pas signer un corps en HMAC, seulement poser `Authorization` (`http_config.authorization`). Sur
option explicite à la création (`auth: bearer`), une source accepte un jeton Bearer de 32 octets ou plus, comparé en
temps constant et rangé haché ; HMAC reste le défaut, et une source HMAC n'accepte jamais de Bearer.
"""
from __future__ import annotations

import io
import json

import pytest

from nokker.incidents import http as incidents_http
from nokker.incidents.service import sign
from nokker.incidents.types import IncidentError, source_to_json
from tests.unit.incidents.test_operate import _scope, am, setup  # noqa: F401 — _scope : fixture autouse


def bearer_source(op, name="alertmanager"):
    return op.create_source({"name": name, "format": "alertmanager", "auth": "bearer"}, "alice")


def raw(body=None) -> bytes:
    return json.dumps(body or am()).encode()


def test_bearer_source_is_explicit_and_hmac_stays_the_default(tmp_path):
    w, op, hmac_src, key, _ = setup(tmp_path)
    assert source_to_json(hmac_src)["auth"] == "hmac"
    src, token = bearer_source(op)
    assert source_to_json(src)["auth"] == "bearer" and source_to_json(src)["has_secret"]
    assert len(token.split("_", 1)[1]) >= 43  # 32 octets aléatoires ou plus (base64 URL)
    stored = op.store.get_source(src.id).key
    assert token not in stored and stored.startswith("sha256:")  # rangé haché, jamais en clair
    with pytest.raises(IncidentError):
        op.create_source({"name": "x", "format": "alertmanager", "auth": "basic"}, "alice")


def test_bearer_token_opens_an_incident_and_bad_tokens_are_refused(tmp_path):
    w, op, _, _, a = setup(tmp_path, [("text", "vu")])
    src, token = bearer_source(op)
    body = raw()
    assert op.receive(src.id, body, None, authorization=f"Bearer {token}")[0] == 202
    assert op.receive(src.id, body, None, authorization=None)[0] == 401
    assert op.receive(src.id, body, None, authorization=f"Bearer {token[:-1]}x")[0] == 401
    assert op.receive(src.id, body, None, authorization="Bearer court")[0] == 401
    assert op.receive(src.id, body, None, authorization=token)[0] == 401  # sans le schéma
    # une signature HMAC ne remplace pas le jeton d'une source Bearer
    assert op.receive(src.id, body, sign(token, body, w.clock()), authorization=None)[0] == 401
    assert op.receive(src.id, body, None, authorization=f"bearer {token}")[0] == 202  # schéma insensible à la casse


def test_hmac_source_never_accepts_a_bearer_token(tmp_path):
    w, op, src, key, _ = setup(tmp_path)
    body = raw()
    assert op.receive(src.id, body, None, authorization=f"Bearer {key}")[0] == 401
    assert op.receive(src.id, body, sign(key, body, w.clock()), authorization=f"Bearer {key}")[0] == 202


def test_replayed_bearer_alert_only_adds_an_occurrence(tmp_path):
    """Le rejeu n'est pas bloqué en Bearer (pas d'horodatage signé) : sans danger, il ne fait qu'ajouter une
    occurrence à l'incident vivant (même empreinte), sans second passage d'agent."""
    w, op, _, _, a = setup(tmp_path, [("text", "vu"), ("text", "vu encore")])
    src, token = bearer_source(op)
    body = raw()
    first = op.receive(src.id, body, None, authorization=f"Bearer {token}")
    again = op.receive(src.id, body, None, authorization=f"Bearer {token}")
    assert first[0] == again[0] == 202 and again[1]["alerts"][0]["result"] == "repeated"
    [inc] = op.store.list()
    assert inc.occurrences == 2 and len(w.runs(a.id)) == 1


def test_rotation_keeps_the_mode_and_invalidates_the_old_token(tmp_path):
    w, op, _, _, _ = setup(tmp_path)
    src, token = bearer_source(op)
    src2, new = op.rotate_secret(src.id)
    assert source_to_json(src2)["auth"] == "bearer" and new != token
    assert op.receive(src.id, raw(), None, authorization=f"Bearer {token}")[0] == 401
    assert op.receive(src.id, raw(), None, authorization=f"Bearer {new}")[0] == 202


class FakeHandler:
    def __init__(self, body: bytes, headers: dict[str, str]):
        self.headers = {"Content-Length": str(len(body)), **headers}
        self.rfile = io.BytesIO(body)
        self.sent = None

    def _json(self, status, payload):
        self.sent = (status, payload)


def test_http_route_passes_the_authorization_header(tmp_path):
    w, op, _, _, _ = setup(tmp_path)
    src, token = bearer_source(op)
    incidents_http.configure(op)
    try:
        h = FakeHandler(raw(), {"Authorization": f"Bearer {token}"})
        assert incidents_http.handle(h, "POST", f"/v1/alerts/{src.id}") and h.sent[0] == 202
        h = FakeHandler(raw(), {})
        incidents_http.handle(h, "POST", f"/v1/alerts/{src.id}")
        assert h.sent[0] == 401 and "Bearer" in h.sent[1]["error"]["message"]
    finally:
        incidents_http.configure(None)
