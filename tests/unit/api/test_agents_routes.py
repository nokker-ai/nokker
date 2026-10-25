"""Routes Crew du serveur (docs/BUBBLES.md §9.5) — vraies requêtes HTTP sur le vrai Handler, le vrai noyau et le moteur
Crew (faux modèle) : création, PATCH qui désarme, arm (200, ou 202 avec la carte FORTE décidée par la route locale de
Lokkal), disarm, run, runs paginés, cancel, DELETE, webhook signé, événements `/v1/events`, `origin` des fils."""
from __future__ import annotations

import hashlib
import hmac
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from nokker.api.events import EventBus
from nokker.api.server import make_handler
from nokker.local.api import LOCAL_OWNER, LocalApi
from tests.unit.agents.conftest import make_world


@pytest.fixture
def srv(tmp_path):
    w = make_world(tmp_path, [("text", "fini")] * 20)
    bus = EventBus()
    w.agents.publish = bus.publish
    w.bubbles.publish = bus.publish
    root = tmp_path / "projets"
    root.mkdir()
    api = LocalApi.from_config({"token_path": str(tmp_path / "tok"), "code": {"roots": [str(root)],
                                                                             "backend": "local"}}, port=8742)
    keys = {"key-alice": "alice", "key-bruno": "bruno", api.token: LOCAL_OWNER}
    handler = make_handler(w.kernel, keys, None, approvals=w.approvals, events=bus, hub=w.bubbles, agents=w.agents,
                           known_users=frozenset({"alice", "bruno"}), local_api=api)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    api.allowed_hosts = frozenset({f"127.0.0.1:{port}"})
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    yield w, port, bus, api
    httpd.shutdown()
    th.join(timeout=5)


def call(port, method, path, body=None, key="key-alice", headers=None, raw=None):
    h = {"Content-Type": "application/json", **(headers or {})}
    if key:
        h["Authorization"] = f"Bearer {key}"
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            out = resp.read()
            return resp.status, (json.loads(out) if out else None)
    except urllib.error.HTTPError as e:
        out = e.read()
        return e.code, (json.loads(out) if out else None)


def _events(bus, kind):
    return [e.data for e in bus._buffer if e.event_type == kind]


def test_create_patch_arm_disarm_and_delete(srv):
    w, port, bus, _ = srv
    st, a = call(port, "POST", "/v1/agents", {"name": "Matin", "role": "veille", "instructions": "Résume la veille.",
                                              "trigger": {"kind": "schedule", "cron": "0 7 * * 1-5",
                                                          "tz": "Europe/Zurich"},
                                              "budget": {"max_calls": 20, "max_cost": 1.5}, "project": None})
    assert st == 201 and a["status"] == "paused" and a["next_run_at"] is None and a["placement"] is None
    assert a["budget"] == {"max_calls": 20, "max_cost": 1.5, "currency": "EUR"} and a["last_run"] is None
    st, armed = call(port, "POST", f"/v1/agents/{a['id']}/arm")
    assert st == 200 and armed["status"] == "armed" and armed["next_run_at"]
    st, same = call(port, "PATCH", f"/v1/agents/{a['id']}", {"name": "Matin 2"})
    assert st == 200 and same["status"] == "armed" and same["name"] == "Matin 2"
    st, paused = call(port, "PATCH", f"/v1/agents/{a['id']}", {"trigger": {"kind": "manual"}})
    assert st == 200 and paused["status"] == "paused" and paused["next_run_at"] is None  # §9.5 : désarmé
    assert call(port, "POST", f"/v1/agents/{a['id']}/arm")[1]["status"] == "armed"
    assert call(port, "POST", f"/v1/agents/{a['id']}/disarm")[1]["status"] == "paused"
    assert call(port, "GET", f"/v1/agents/{a['id']}")[1]["name"] == "Matin 2"
    assert call(port, "DELETE", f"/v1/agents/{a['id']}") == (204, None)
    assert call(port, "GET", f"/v1/agents/{a['id']}")[0] == 404
    assert [e["id"] for e in _events(bus, "agent.deleted")] == [a["id"]]
    assert {e["agent"]["status"] for e in _events(bus, "agent.updated")} >= {"armed", "paused"}


def test_validation_errors_are_400_with_a_message(srv):
    _, port, _, _ = srv
    for body in ({"name": "x", "role": "y", "trigger": {"kind": "schedule", "cron": "nope"}},
                 {"name": "x", "role": "y", "secrets": ["sk-live-abcdef0123456789"]},
                 {"name": "x", "role": "y", "placement": {"machine": "auto"}},
                 {"name": "x", "role": "y", "trigger": {"kind": "event", "event": "alert:x"},
                  "auto_approve": ["note_write"]}):
        st, out = call(port, "POST", "/v1/agents", body)
        assert st == 400 and out["error"]["message"], body
    assert call(port, "POST", "/v1/agents", {"name": "x", "role": "y"}, key=None)[0] == 401


def test_run_runs_pagination_and_events(srv):
    w, port, bus, _ = srv
    a = call(port, "POST", "/v1/agents", {"name": "A", "role": "r"})[1]
    ids = []
    for _ in range(3):
        w.clock.t += 60
        st, out = call(port, "POST", f"/v1/agents/{a['id']}/run")
        assert st == 202
        ids.append(out["run_id"])
    st, page = call(port, "GET", f"/v1/agents/{a['id']}/runs?limit=2")
    assert st == 200 and [r["run_id"] for r in page["runs"]] == ids[::-1][:2] and page["next_before"] == ids[1]
    st, rest = call(port, "GET", f"/v1/agents/{a['id']}/runs?limit=2&before={page['next_before']}")
    assert [r["run_id"] for r in rest["runs"]] == [ids[0]] and rest["next_before"] is None
    r = page["runs"][0]
    assert r["status"] == "succeeded" and r["trigger"] == "manual" and r["cost"]["currency"] == "EUR"
    assert set(r) >= {"run_id", "agent_id", "trigger", "scheduled_for", "started_at", "ended_at", "status",
                      "thread_id", "cost", "summary"}
    runs_ev = [e for e in _events(bus, "agent.run") if e["run"]["run_id"] == ids[0]]
    assert [e["run"]["status"] for e in runs_ev] == ["queued", "running", "succeeded"]
    # le fil du passage porte son origine, dans la liste et en détail
    st, threads = call(port, "GET", "/v1/threads")
    origins = [t["origin"] for t in threads if "origin" in t]
    assert {o["run_id"] for o in origins} == set(ids) and all(o["kind"] == "agent" for o in origins)
    st, one = call(port, "GET", f"/v1/threads/{r['thread_id']}")
    assert one["thread"]["origin"] == {"kind": "agent", "agent_id": a["id"], "run_id": ids[2]}
    # cancel d'un passage fini : rendu tel quel
    st, same = call(port, "POST", f"/v1/agents/{a['id']}/runs/{ids[0]}/cancel")
    assert st == 200 and same["status"] == "succeeded"
    assert call(port, "POST", f"/v1/agents/{a['id']}/runs/ar_{'0' * 16}/cancel")[0] == 404


def test_arm_with_a_write_rule_returns_202_and_the_strong_card_decided_locally_arms(srv):
    w, port, _, api = srv
    a = call(port, "POST", "/v1/agents", {"name": "Notes", "role": "scribe",
                                          "auto_approve": ["note_write(notes/*)"]})[1]
    assert a["status"] == "paused"
    st, out = call(port, "POST", f"/v1/agents/{a['id']}/arm")
    assert st == 202 and out["approval_id"] and out["agent"]["arm_approval_id"] == out["approval_id"]
    listed = call(port, "GET", "/v1/local/approvals", key=api.token)[1]["approvals"]
    card = next(c for c in listed if c["id"] == out["approval_id"])
    assert card["strength"] == "strong" and card["tool_name"] == "agent_arm"
    st, decided = call(port, "POST", f"/v1/local/approvals/{card['id']}/decide", {"decision": "approve"},
                       key=api.token)
    # decide répond tout de suite (LOKKAL §7.4.9) ; le geste du noyau (armement) s'applique en fond, dans la file du fil
    assert st in (200, 202) and decided["status"] in ("approved", "executed")  # geste court : parfois déjà appliqué
    import time
    for _ in range(100):
        if call(port, "GET", f"/v1/agents/{a['id']}")[1]["status"] == "armed":
            break
        time.sleep(0.05)
    assert call(port, "GET", f"/v1/agents/{a['id']}")[1]["status"] == "armed"
    # changer les règles désarme et la carte d'avant ne vaut plus pour les nouvelles
    st, p = call(port, "PATCH", f"/v1/agents/{a['id']}", {"auto_approve": ["note_write"]})
    assert p["status"] == "paused"
    assert call(port, "POST", f"/v1/agents/{a['id']}/arm")[0] == 202


def test_run_while_running_is_409(srv):
    w, port, _, _ = srv
    w.model.steps[:0] = [("tool", "note_write", {}), ("text", "attente")]
    a = call(port, "POST", "/v1/agents", {"name": "A", "role": "r"})[1]
    assert call(port, "POST", f"/v1/agents/{a['id']}/run")[0] == 202
    assert call(port, "GET", f"/v1/agents/{a['id']}")[1]["status"] == "running"
    st, out = call(port, "POST", f"/v1/agents/{a['id']}/run")
    assert st == 409 and "déjà en cours" in out["error"]["message"]


def test_signed_webhook_route(srv):
    w, port, _, _ = srv
    a = call(port, "POST", "/v1/agents", {"name": "PR", "role": "relecture",
                                          "trigger": {"kind": "event", "event": "webhook:pr"}})[1]
    call(port, "POST", f"/v1/agents/{a['id']}/arm")
    st, sec = call(port, "POST", f"/v1/agents/{a['id']}/webhook-secret")
    assert st == 200 and sec["url"] == f"/v1/agents/{a['id']}/webhooks/pr" and sec["header"] == "X-Nokker-Signature"
    assert sec["secret"] not in json.dumps(call(port, "GET", f"/v1/agents/{a['id']}")[1])
    body = b'{"action": "opened"}'
    t = str(int(w.clock()))
    sig = f"t={t},v1=" + hmac.new(sec["secret"].encode(), t.encode() + b"." + body, hashlib.sha256).hexdigest()
    hdr = {"X-Nokker-Signature": sig}
    st, out = call(port, "POST", sec["url"], key=None, headers=hdr, raw=body)
    assert st == 202 and out["status"] in ("queued", "succeeded")
    assert call(port, "POST", sec["url"], key=None, headers=hdr, raw=body)[0] == 409  # rejeu
    assert call(port, "POST", sec["url"], key=None, headers={"X-Nokker-Signature": "t=1,v1=00"}, raw=body)[0] == 401
    assert call(port, "POST", f"/v1/agents/{a['id']}/webhooks/autre", key=None, headers=hdr, raw=body)[0] == 404


def test_agents_of_another_tenant_do_not_exist(srv):
    w, port, _, _ = srv
    a = call(port, "POST", "/v1/agents", {"name": "A", "role": "r"})[1]
    stored = w.agents.store.get(a["id"])
    stored.created_by = "us_inconnu"  # agent d'un compte testeur : hors du locataire interne
    w.agents.store.save(stored)
    assert call(port, "GET", f"/v1/agents/{a['id']}")[0] == 404
    assert call(port, "POST", f"/v1/agents/{a['id']}/run")[0] == 404
    assert a["id"] not in [x["id"] for x in call(port, "GET", "/v1/agents")[1]]


def test_mail_trigger_is_refused_at_creation_and_patch_with_a_clear_message(srv):
    """Revue de cohérence de la doc (25.10) : `mail:<filtre>` n'a pas de producteur (le connecteur mail n'a pas de
    relève) ; un agent créé avec ce déclencheur ne tournerait jamais. Refusé en 400, création et PATCH."""
    _, port, _, _ = srv
    mail = {"kind": "event", "event": "mail:is:unread"}
    st, out = call(port, "POST", "/v1/agents", {"name": "Tri", "role": "courrier", "trigger": mail})
    assert st == 400 and "mail:" in out["error"]["message"] and "webhook:" in out["error"]["message"]
    assert out["error"]["code"] == "no_mail_trigger"  # code stable pour les écrans, le message reste en clair
    st, a = call(port, "POST", "/v1/agents", {"name": "Tri", "role": "courrier"})
    assert st == 201
    st, out = call(port, "PATCH", f"/v1/agents/{a['id']}", {"trigger": mail})
    assert st == 400 and "mail:" in out["error"]["message"] and out["error"]["code"] == "no_mail_trigger"
    st, out = call(port, "POST", "/v1/agents", {"name": "x"})  # erreur sans code : pas de champ `code`
    assert st == 400 and "code" not in out["error"]
    assert call(port, "GET", f"/v1/agents/{a['id']}")[1]["trigger"] == {"kind": "manual"}


def test_mail_trigger_with_the_watch_is_accepted_and_get_shows_its_state(srv, monkeypatch):
    """Relève du courrier branchée : 201, filtre normalisé, `mail_watch` dans GET (liste et agent) ; filtre hors du
    sous-ensemble : 400 avec la raison. L'identifiant de la boîte n'apparaît dans aucune réponse."""
    from nokker.agents.mailwatch import MailWatcher
    from nokker.connectors.crypto import SecretBox
    from nokker.connectors.provider import ConnectorProvider
    from nokker.connectors.store import MemoryConnectorStore

    w, port, _, _ = srv
    store = MemoryConnectorStore(SecretBox(SecretBox.generate_key()))
    store.put("acc-1", "mail", {"preset": "custom", "address": "agent@corp.test", "imap_host": "imap.corp.test",
                                "imap_port": 993, "smtp_host": "smtp.corp.test", "smtp_port": 465}, "pw-route-0099")
    w.engine.mail_watch = MailWatcher(engine=w.engine, mailbox=ConnectorProvider(store, lambda t: "acc-1").mail_for,
                                      clock=w.clock)
    st, out = call(port, "POST", "/v1/agents", {"name": "Tri", "role": "courrier",
                                                "trigger": {"kind": "event", "event": "mail:in:Archive"}})
    assert st == 400 and "INBOX" in out["error"]["message"] and out["error"]["code"] == "bad_mail_filter"
    st, a = call(port, "POST", "/v1/agents", {"name": "Tri", "role": "courrier",
                                              "trigger": {"kind": "event", "event": "mail:subject:Facture  is:unread"}})
    assert st == 201 and a["trigger"]["event"] == "mail:subject:Facture is:unread"
    assert a["mail_watch"]["state"] == "off"  # créé au repos
    st, armed = call(port, "POST", f"/v1/agents/{a['id']}/arm")
    assert st == 200 and armed["mail_watch"]["state"] == "waiting"
    got = call(port, "GET", f"/v1/agents/{a['id']}")[1]
    assert got["mail_watch"]["folder"] == "INBOX" and got["mail_watch"]["poll_s"] == 120
    listed = next(x for x in call(port, "GET", "/v1/agents")[1] if x["id"] == a["id"])
    assert listed["mail_watch"]["state"] == "waiting"
    other = call(port, "POST", "/v1/agents", {"name": "M", "role": "r"})[1]
    assert "mail_watch" not in other
    assert "pw-route-0099" not in json.dumps([got, listed])
