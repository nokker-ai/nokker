"""Zones de l'app (docs/LOKKAL-PHONE.md §4.2) : contrat des routes par le VRAI serveur (cloud et Lokkal), règles des
événements (idempotence, ordre, 30 min), scénarios au nom du membre (registre de ha-full, ici un faux du même
contrat), présence (HA, ambiante, « Qui fait quoi »), et jamais de coordonnées."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from nokker.home.policy import HomePolicy
from nokker.local.api import LOCAL_OWNER
from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope
from nokker.zones import http as zones_http
from nokker.zones.scenarios import RegistryScenarios, location_for, tracker_id
from nokker.zones.service import ZoneService
from nokker.zones.store import MemoryZoneStore, ZoneError, clean_event, clean_zones
from tests.unit.tenancy.test_isolation_suite import beta  # noqa: F401 — fixture (vrai serveur cloud)

T0 = datetime(2026, 11, 6, 18, 0, tzinfo=UTC)


class FakeRegistry:
    """Même contrat que nokker/home/scenarios.py::ScenarioStore (items, get)."""

    def __init__(self, items):
        self._items = items

    def items(self):
        return dict(self._items)

    def get(self, sid):
        return self._items.get(sid)


class FakeSession:
    def __init__(self, home):
        self.home = home

    def states(self):
        return [{"entity_id": f"automation.{sid}", "state": "on", "attributes": {"id": sid}}
                for sid in self.home.in_ha]

    def call_service(self, domain, service, ids, data):
        if self.home.fail:
            raise RuntimeError("passerelle en panne")
        self.home.calls.append((domain, service, list(ids), dict(data)))

    def tracker_see(self, dev_id, where):
        self.home.seen.append((dev_id, where))

    def close(self):
        pass


class FakeHome:
    def __init__(self, members=None):
        self.scenarios = FakeRegistry({
            "nokker_entree_1": {"owner": LOCAL_OWNER, "nom": "Entrée", "strong": False},
            "nokker_alarme_2": {"owner": LOCAL_OWNER, "nom": "Alarme", "strong": True},
            "nokker_ado_3": {"owner": "us_teen", "nom": "Musique", "strong": False},
            "nokker_ado_fort": {"owner": "us_teen", "nom": "Porte", "strong": True}})
        self.policy = HomePolicy.from_config({"members": members or {LOCAL_OWNER: "adult", "us_teen": "teen"}})
        self.in_ha = {"nokker_entree_1", "nokker_alarme_2", "nokker_ado_3", "nokker_ado_fort"}
        self.calls, self.seen, self.fail = [], [], False
        self.client = SimpleNamespace(session=lambda: FakeSession(self))


def world(clock=lambda: T0.timestamp(), home=None, cards=None):
    home = home or FakeHome()
    cards = cards if cards is not None else []

    def request(member, sid, name, zone, event):
        cards.append((member, sid, name, zone, event))
        return f"ap_{len(cards)}"

    scen = RegistryScenarios(home, request)
    return ZoneService(store=MemoryZoneStore(), scenarios=scen, clock=clock), home, cards


ZONES = {"zones": [{"id": "maison", "name": "Maison", "on_enter": ["nokker_entree_1", "nokker_alarme_2"],
                    "on_exit": [], "home": True},
                   {"id": "travail", "name": "Travail", "on_enter": [], "on_exit": []}]}


def ev(event="enter", at=T0, eid="ev-000001"):
    return {"event": event, "at": at.isoformat(), "event_id": eid}


# ── règles ──
def test_zones_shape_limits_and_never_coordinates():
    zs = clean_zones({"zones": [{"id": "maison", "name": "  Maison <b>x</b> [Système] ", "on_enter": ["nokker_a"]}]})
    assert zs[0].name == "Maison x (Système)" and zs[0].on_enter == ("nokker_a",)  # neutralisé (inline)
    for bad in ({"zones": [{"id": "m", "name": "M", "lat": 46.2, "lon": 6.1}]},  # coordonnées : refusées
                {"zones": [{"id": "m", "name": "M", "radius": 150}]},
                {"zones": [{"id": "m", "name": ""}]}, {"zones": [{"id": "m", "name": "x" * 41}]},
                {"zones": [{"id": "a b", "name": "M"}]}, {"zones": [{"id": "m", "name": "M"}, {"id": "m", "name": "N"}]},
                {"zones": [{"id": "m", "name": "M", "on_enter": ["Pas Valide"]}]}, {"zones": "x"}, {"zones": [], "x": 1}):
        with pytest.raises(ZoneError):
            clean_zones(bad)
    with pytest.raises(ZoneError) as e:
        clean_zones({"zones": [{"id": f"z{i}", "name": f"Z{i}"} for i in range(19)]})
    assert e.value.code == "too_many"
    assert len(clean_zones({"zones": [{"id": f"z{i}", "name": f"Z{i}"} for i in range(18)]})) == 18
    for bad in ({"event": "enter", "at": T0.isoformat(), "event_id": "ev-000001", "lat": 1},
                {"event": "in", "at": T0.isoformat(), "event_id": "ev-000001"},
                {"event": "enter", "at": "2026-11-06T18:00:00", "event_id": "ev-000001"},  # sans fuseau
                {"event": "enter", "at": (T0 + timedelta(minutes=6)).isoformat(), "event_id": "ev-000001"},
                {"event": "enter", "at": T0.isoformat(), "event_id": "court"}):
        with pytest.raises(ZoneError):
            clean_event(bad, T0)
    assert clean_event({"event": "exit", "at": T0.timestamp(), "event_id": "ev-000001"}, T0)[1] == T0
    home, work = clean_zones({"zones": [{"id": "a", "name": "Chez nous", "home": True}, {"id": "b", "name": "Maison"}]})
    assert location_for(home, "enter") == "home" and location_for(work, "enter") == "Maison"  # le drapeau, pas le nom
    assert location_for(home, "exit") == "not_home" and location_for(work, "exit") == "not_home"
    assert tracker_id("us_ABC-12") == "nokker_us_abc_12"
    for bad in ({"zones": [{"id": "a", "name": "A", "home": True}, {"id": "b", "name": "B", "home": True}]},
                {"zones": [{"id": "a", "name": "A", "home": "oui"}]}):
        with pytest.raises(ZoneError):
            clean_zones(bad)


def test_event_idempotent_ordered_stale_and_unknown():
    svc, home, cards = world()
    with tenant_scope(INTERNAL_TENANT):
        svc.put(LOCAL_OWNER, ZONES)
        st, out = svc.event(LOCAL_OWNER, "maison", ev())
        assert st == 202 and out == {"queued": True, "scenarios": [
            {"id": "nokker_entree_1", "status": "started"},
            {"id": "nokker_alarme_2", "status": "card", "approval_id": "ap_1"}]}
        assert home.calls == [("automation", "trigger", ["automation.nokker_entree_1"], {})]  # direct
        assert cards == [(LOCAL_OWNER, "nokker_alarme_2", "Alarme", "Maison", "enter")]  # fort : carte, jamais exécuté
        assert home.seen == [(tracker_id(LOCAL_OWNER), "home")]  # présence HA
        st, dup = svc.event(LOCAL_OWNER, "maison", ev())
        assert st == 200 and dup["duplicate"] is True and dup["scenarios"] == out["scenarios"]
        assert len(home.calls) == 1 and len(cards) == 1  # rien relancé
        with pytest.raises(ZoneError) as e:
            svc.event(LOCAL_OWNER, "maison", ev("exit", T0 - timedelta(minutes=1), "ev-000002"))
        assert (e.value.status, e.value.code) == (409, "out_of_order")
        with pytest.raises(ZoneError) as e:
            svc.event(LOCAL_OWNER, "inconnue", ev(eid="ev-000003"))
        assert (e.value.status, e.value.code) == (404, "zone_unknown")
        # plus de 30 min : présence seulement
        late, _, _ = world(clock=lambda: (T0 + timedelta(minutes=31)).timestamp(), home=home, cards=cards)
        late.put(LOCAL_OWNER, ZONES)
        st, out = late.event(LOCAL_OWNER, "maison", ev(eid="ev-000004"))
        assert out["scenarios"] == [{"id": "nokker_entree_1", "status": "skipped", "reason": "stale"},
                                    {"id": "nokker_alarme_2", "status": "skipped", "reason": "stale"}]
        assert len(home.calls) == 1 and home.seen[-1] == (tracker_id(LOCAL_OWNER), "home")


def test_scenarios_run_in_the_members_name_minor_cannot_get_a_strong_card():
    svc, home, cards = world()
    with tenant_scope(INTERNAL_TENANT):
        assert svc.list_scenarios("us_teen") == {"bridge": "home-bridge", "scenarios": [
            {"id": "nokker_ado_3", "name": "Musique", "strong": False},
            {"id": "nokker_ado_fort", "name": "Porte", "strong": True}]}  # les siens seulement
        assert len(svc.list_scenarios(LOCAL_OWNER)["scenarios"]) == 4  # un adulte voit tout
        with pytest.raises(ZoneError) as e:  # un scénario d'un autre : refusé à l'écriture
            svc.put("us_teen", {"zones": [{"id": "m", "name": "Maison", "on_enter": ["nokker_entree_1"]}]})
        assert e.value.code == "unknown_scenario"
        svc.put("us_teen", {"zones": [{"id": "m", "name": "Maison", "on_enter": ["nokker_ado_3", "nokker_ado_fort"]}]})
        st, out = svc.event("us_teen", "m", ev())
        assert out["scenarios"] == [{"id": "nokker_ado_3", "status": "started"},
                                    {"id": "nokker_ado_fort", "status": "skipped", "reason": "not_allowed"}]
        assert cards == []  # un mineur ne reçoit pas une carte qu'il ne pourrait pas obtenir à la main
        home.in_ha.discard("nokker_entree_1")
        home.scenarios._items["nokker_gone"] = {"owner": LOCAL_OWNER, "nom": "X", "strong": False}
        r = svc.scenarios.run(LOCAL_OWNER, "nokker_entree_1", zone="Maison", event="enter")
        assert r == {"id": "nokker_entree_1", "status": "skipped", "reason": "absent"}
        assert svc.scenarios.run(LOCAL_OWNER, "nokker_inconnu", zone="M", event="enter")["reason"] == "unknown_scenario"
        home.fail = True
        assert svc.scenarios.run(LOCAL_OWNER, "nokker_ado_3", zone="M", event="enter")["reason"] == "bridge_error"


def test_strong_card_decision_runs_only_if_approved_and_still_allowed():
    svc, home, _ = world()
    scen = svc.scenarios
    card = SimpleNamespace(status="approved", arguments={"member": LOCAL_OWNER, "scenario": "nokker_alarme_2"})
    assert scen.on_decision(card) == "scénario lancé"
    assert home.calls == [("automation", "trigger", ["automation.nokker_alarme_2"], {})]
    assert scen.on_decision(SimpleNamespace(status="denied", arguments=card.arguments)) == "scénario non lancé"
    assert scen.on_decision(SimpleNamespace(status="approved", arguments={"member": "us_teen",
                                                                          "scenario": "nokker_alarme_2"})) \
        == "scénario retiré ou plus permis : rien n'est lancé"
    assert len(home.calls) == 1


def test_cloud_has_presence_only_and_zones_are_per_account(beta):  # noqa: F811
    zones_http.configure(ZoneService(store=MemoryZoneStore()))
    try:
        call = beta.call
        assert call("GET", "/v1/me/zones")[0] == 401
        assert call("GET", "/v1/home/scenarios", who="A") == (200, {"scenarios": [], "bridge": None})
        st, body = call("PUT", "/v1/me/zones", {"zones": [{"id": "maison", "name": "Maison",
                                                           "on_enter": ["nokker_x"], "on_exit": []}]}, who="A")
        assert st == 200 and body["zones"][0] == {"id": "maison", "name": "Maison", "on_enter": ["nokker_x"],
                                                  "on_exit": [], "home": False}
        assert call("GET", "/v1/me/zones", who="B")[1] == {"zones": []}  # B ne voit rien de A
        now = datetime.now(UTC)
        st, out = call("POST", "/v1/me/zones/maison/event", ev(at=now), who="A")
        assert (st, out) == (202, {"queued": True, "scenarios": [
            {"id": "nokker_x", "status": "skipped", "reason": "no_bridge"}]})
        assert call("POST", "/v1/me/zones/maison/event", ev(at=now), who="A")[1]["duplicate"] is True
        assert call("POST", "/v1/me/zones/maison/event", ev(at=now, eid="ev-autre-1"), who="B")[1]["error"] \
            == "zone_unknown"  # la zone de A n'existe pas chez B
        assert call("PUT", "/v1/me/zones", {"zones": [{"id": "m", "name": "M", "lat": 46.2}]}, who="A")[1]["error"] \
            == "bad_zone"
    finally:
        zones_http.configure(None)


@pytest.fixture
def lokkal(tmp_path):
    from nokker.api.server import make_handler
    from nokker.local.api import LocalApi
    from tests.unit.agents.conftest import make_world

    api = LocalApi.from_config({"token_path": str(tmp_path / "tok"), "state_dir": str(tmp_path / "state")}, port=8742)
    (tmp_path / "w").mkdir()
    w = make_world(tmp_path / "w")
    svc, home, cards = world(clock=lambda: datetime.now(UTC).timestamp())
    zones_http.configure(svc)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(w.kernel, {api.token: LOCAL_OWNER}, local_api=api))
    api.allowed_hosts = frozenset({f"127.0.0.1:{httpd.server_address[1]}"})
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield api, httpd.server_address[1], home, cards
    httpd.shutdown()
    zones_http.configure(None)


def _call(port, method, path, body=None, token=None):
    h = {"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})}
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, headers=h,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_lokkal_runs_scenarios_through_the_real_server(lokkal):
    api, port, home, cards = lokkal
    assert _call(port, "GET", "/v1/home/scenarios")[0] == 401
    st, body = _call(port, "GET", "/v1/home/scenarios", token=api.token)
    assert st == 200 and body["bridge"] == "home-bridge" and {s["id"] for s in body["scenarios"]} >= {"nokker_entree_1"}
    assert _call(port, "PUT", "/v1/me/zones", ZONES, token=api.token)[0] == 200
    st, out = _call(port, "POST", "/v1/me/zones/maison/event", ev(at=datetime.now(UTC)), token=api.token)
    assert st == 202 and [s["status"] for s in out["scenarios"]] == ["started", "card"]
    assert home.seen == [(tracker_id(LOCAL_OWNER), "home")] and len(cards) == 1
    st, out = _call(port, "POST", "/v1/me/zones/maison/event", ev("exit", datetime.now(UTC), "ev-000009"),
                    token=api.token)
    assert st == 202 and out["scenarios"] == [] and home.seen[-1] == (tracker_id(LOCAL_OWNER), "not_home")


def test_bridge_tracker_see_is_closed():
    from nokker.homebridge.ha import HaOps

    sent = []
    ops = HaOps.__new__(HaOps)
    ops.ha = SimpleNamespace(call_service=lambda d, s, data=None, ids=None: sent.append((d, s, data, ids)))
    ops.trackers = frozenset({"nokker_local_owner", "nokker_x"})
    assert ops.tracker_see({"dev_id": "nokker_local_owner", "location_name": "home"}) == {"ok": True}
    assert sent == [("device_tracker", "see", {"dev_id": "nokker_local_owner", "location_name": "home"}, None)]
    for bad, exc in (({"dev_id": "nokker_x", "location_name": "home", "gps": [46.2, 6.1]}, PermissionError),
                     ({"dev_id": "phone_de_lea", "location_name": "home"}, ValueError),  # pas un traceur Nokker
                     ({"dev_id": "nokker_x", "location_name": ""}, ValueError),
                     ({"dev_id": "nokker_x", "location_name": "x" * 41}, ValueError),
                     ({"dev_id": "nokker_x", "location_name": "a\nb"}, ValueError),
                     ({"dev_id": "nokker_x\n", "location_name": "home"}, ValueError),  # pas de \n final (fullmatch)
                     ({"dev_id": ["nokker_x"], "location_name": "home"}, ValueError),  # des textes seulement
                     ({"dev_id": "nokker_x", "location_name": 42}, ValueError),
                     ({"dev_id": "nokker_inconnu", "location_name": "home"}, PermissionError)):  # membre non déclaré
        with pytest.raises(exc):
            ops.tracker_see(bad)
    assert len(sent) == 1
    from nokker.homebridge.server import OPS

    assert "ha.tracker_see" in OPS
    ops.trackers = frozenset()  # aucun membre déclaré : refusé (fermé par défaut)
    with pytest.raises(PermissionError):
        ops.tracker_see({"dev_id": "nokker_x", "location_name": "home"})


def test_presence_away_and_who_does_what():
    from nokker.ambient.bridge import FakeBridge
    from nokker.ambient.presence import Presence, RoomState
    from nokker.presence.service import PresenceService
    from nokker.zones.presence import wrap

    svc, _, _ = world()
    zones_http.configure(svc)
    try:
        with tenant_scope(INTERNAL_TENANT):
            for who in (LOCAL_OWNER, "us_b"):
                svc.store.replace(who, clean_zones({"zones": [{"id": "maison", "name": "Maison", "home": True}]}))
            svc.event(LOCAL_OWNER, "maison", ev("exit"))
            svc.event("us_b", "maison", ev("exit", eid="ev-b-0001"))
            fake = FakeBridge(presence_value=Presence(rooms={"salon": RoomState(members=frozenset({"us_b"}),
                                                                                 occupied=True)}))
            bridge = wrap(fake)
            p = bridge.presence()
            assert p.away == frozenset({LOCAL_OWNER})  # us_b est vu au salon : la maison fait foi pour les pièces
            assert bridge.play is not None  # le reste passe à la passerelle
            entries = PresenceService(zones=svc).view(LOCAL_OWNER)["entries"]
        assert entries == [{"id": "zone:maison", "kind": "zone", "name": "Maison", "status": "idle",
                            "activity": {"code": "zone_outside"}, "target": {"type": "zone", "id": "maison"},
                            "since": T0.isoformat()}]
        with tenant_scope(INTERNAL_TENANT):
            assert all(e["id"] != "zone:maison" or e["name"] == "Maison"
                       for e in PresenceService(zones=svc).view("us_b")["entries"])  # la sienne seulement
    finally:
        zones_http.configure(None)


def test_registry_scenarios_on_the_real_scenario_store():
    """Le contrat du faux registre est celui du VRAI (nokker/home/scenarios.py, ha-full-r43) : propriétaire, nom, force."""
    from nokker.home.scenarios import ScenarioStore

    store = ScenarioStore(path=None)
    store.record("nokker_entree_1", LOCAL_OWNER, "Entrée", "allume l'entrée", strong=False)
    store.record("nokker_alarme_2", LOCAL_OWNER, "Alarme", "coupe l'alarme", strong=True)
    home = FakeHome()
    home.scenarios = store
    svc, _, cards = world(home=home)
    with tenant_scope(INTERNAL_TENANT):
        assert svc.list_scenarios(LOCAL_OWNER)["scenarios"] == [
            {"id": "nokker_alarme_2", "name": "Alarme", "strong": True},
            {"id": "nokker_entree_1", "name": "Entrée", "strong": False}]
        svc.put(LOCAL_OWNER, ZONES)
        st, out = svc.event(LOCAL_OWNER, "maison", ev())
    assert [s["status"] for s in out["scenarios"]] == ["started", "card"] and len(cards) == 1


def test_bridge_trackers_come_from_the_declared_members(tmp_path):
    """La passerelle ne permet `ha.tracker_see` qu'aux traceurs des membres déclarés : `home_assistant.zone_members`,
    sinon les membres de la présence par pièce ; aucun membre : aucun traceur (fermé)."""
    from nokker.homebridge.config import from_dict
    from nokker.homebridge.server import Bridge

    ha = SimpleNamespace(registry=lambda: {}, states=lambda: [])
    token = "hbtk_" + "Q7w" * 12

    def trackers(raw):
        cfg = from_dict({"allow": [], "api_listen": "127.0.0.1:0", **raw}, token=token, base_dir=tmp_path)
        return Bridge.build(cfg, ha=ha, sinks={}).ha_ops.trackers

    assert trackers({"home_assistant": {"zone_members": [LOCAL_OWNER, "us_ABC"]}}) == frozenset(
        {"nokker_local_owner", "nokker_us_abc"})
    assert trackers({"home_assistant": {}, "members": {"us_alice": {"room_sensor": "sensor.alice"}}}) == frozenset(
        {"nokker_us_alice"})
    assert trackers({"home_assistant": {}}) == frozenset()
