"""Mur F1 côté noyau (docs/FAMILY-PROJECTS.md §10) : `GET /v1/glance/household` et ses coches, jeton nkg_ d'un MUR
seulement. Prouve : prénoms seulement ; agenda partagé, sans titre pour un événement privé ni en mode invité ;
tâches du jour hors projets réservés aux parents ; mode invité (retire titres, tâches, courses, projets ; 4 h au plus) ;
Home Assistant filtré (jamais serrure, alarme, garage, portail) ; coches idempotentes, locales, 30 par heure, 404 hors
périmètre ; par le VRAI serveur : 403 not_wall pour des lunettes, ETag puis 304, 403 glance_scope inchangé ailleurs."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

from nokker.core.ports import Effect, ToolResult, ToolSpec
from nokker.household.projects import wall as wall_mod
from nokker.household.projects.wall import GUEST_MAX_S, HouseholdWall, WallError, ha_entity_ok
from tests.unit.household.test_family_projects import ACC, DAY, HID, NOW, SCHOOL, TA, TAXES, TC, TP, Rig, _commit

WALL = SimpleNamespace(id="dp_00000000000000aa", account_id=ACC[TP], household_id="", surface="wall")
GLASSES = SimpleNamespace(id="dp_00000000000000bb", account_id=ACC[TP], household_id="", surface="glasses")


class Agenda:
    def __init__(self, events):
        self.events = events

    def specs(self):
        return [ToolSpec(name="calendar_events", description="", parameters={}, effect=Effect.READ)]

    def call(self, name, args, ctx):
        from nokker.connectors.calendar import fence_events

        return ToolResult(call_id="", name=name, content=fence_events({"count": len(self.events),
                                                                        "events": self.events}))


def _wall(r: Rig, **kw) -> HouseholdWall:
    names = {ACC[TP]: "Nina", ACC[TA]: "", ACC[TC]: "Léa"}  # Marc n'a pas saisi de prénom
    w = HouseholdWall(projects=r.svc, first_name=names.get, clock=lambda: r.now, **kw)
    return w


def _school_today(r: Rig) -> tuple[str, str]:
    """Rentrée (tout le foyer) + Impôts (parents) ; Léa et Marc ont une tâche le 09.11, jour du mur."""
    plan = {**SCHOOL, "lines": [{"title": "Couvrir les cahiers", "assignee": "Léa", "due": "2026-11-09"},
                                {"title": "Acheter le sac", "assignee": "Marc", "due": "2026-11-12"}]}
    school = _commit(r, plan)
    taxes = _commit(r, {**TAXES, "lines": [{"title": "Rassembler les relevés", "assignee": "Marc",
                                            "due": "2026-11-09"}]})
    r.now = NOW + 2 * DAY  # lundi 09.11
    return school, taxes


def test_view_content_rules():
    r = Rig()
    school, taxes = _school_today(r)
    agenda = Agenda([{"summary": "Piscine", "start": "2026-11-10T17:00:00+01:00", "end": "2026-11-10T18:00:00+01:00"},
                     {"summary": "Médecin (privé)", "start": "2026-11-11T09:00:00+01:00", "class": "PRIVATE"},
                     {"summary": "Vacances", "start": "2026-11-14", "all_day": True}])
    w = _wall(r, calendars=lambda acc: [(ACC[TA], agenda)], shopping=lambda h: ["Lait", "Pain", "lait "],
              ha_states=lambda acc, ids: [{"entity_id": e, "name": e, "state": "21", "unit": "°C"} for e in ids]
              + [{"entity_id": "cover.portail", "name": "Portail", "state": "closed"}])
    with r.as_(TP):
        r.svc.put_settings({"wall_ha": ["sensor.salon_temperature"]})
        for bad in (["lock.porte"], ["alarm_control_panel.maison"], ["cover.garage_door"]):
            with pytest.raises(Exception, match="refusée sur le mur"):
                r.svc.put_settings({"wall_ha": bad})
    v = w.view(WALL)
    assert [(m["name"], m["initials"]) for m in v["members"]] == [("Nina", "N"), ("Membre 2", "M2"), ("Léa", "L")]
    assert "@" not in json.dumps(v["members"])
    assert [t["title"] for t in v["tasks"]] == ["Couvrir les cahiers"]  # du jour, hors projet « parents »
    assert v["tasks"][0]["member_id"] == ACC[TC] and v["tasks"][0]["done"] is False
    assert [p["id"] for p in v["projects"]] == [school] and taxes not in json.dumps(v)
    assert v["projects"][0] == {"id": school, "title": "Rentrée de janvier", "progress": 0.0,
                                "next_due": "2026-11-09", "next_step": "Couvrir les cahiers"}
    assert [e.get("title") for e in v["agenda"]] == ["Piscine", None, "Vacances"]
    assert v["agenda"][1]["member_id"] == ACC[TA] and v["agenda"][2]["all_day"] is True
    assert [s["text"] for s in v["shopping"]] == ["Lait", "Pain"]  # même article une fois
    assert v["ha"] == [{"entity_id": "sensor.salon_temperature", "name": "sensor.salon_temperature", "state": "21",
                        "unit": "°C", "domain": "sensor"}]
    assert v["meals"] == [] and v["weather"] is None and v["departures"] == [] and v["guest"] is False


def test_guest_mode_strips_titles_tasks_shopping_projects_and_expires_after_4h():
    r = Rig()
    _school_today(r)
    agenda = Agenda([{"summary": "Piscine", "start": "2026-11-10T17:00:00+01:00"}])
    w = _wall(r, calendars=lambda acc: [(ACC[TP], agenda)], shopping=lambda h: ["Lait"])
    assert w.set_guest(WALL, True)["guest"] is True
    v = w.view(WALL)
    assert v["guest"] is True and v["tasks"] == [] and v["shopping"] == [] and v["projects"] == []
    assert v["agenda"] and "title" not in v["agenda"][0] and v["members"]
    with r.as_(TP):
        lea = next(t.task_id for t in r.store.tasks(HID) if t.assignee == ACC[TC])
    with pytest.raises(WallError) as e:
        w.task_done(WALL, lea)
    assert e.value.status == 404
    r.now += GUEST_MAX_S + 1
    assert w.view(WALL)["guest"] is False and w.view(WALL)["tasks"]
    w.set_guest(WALL, True)
    w.set_guest(WALL, False)
    assert w.view(WALL)["guest"] is False
    with pytest.raises(WallError):
        w.set_guest(WALL, "oui")


def test_wall_checks_are_local_idempotent_rate_limited_and_scoped():
    r = Rig()
    _, taxes = _school_today(r)
    w = _wall(r, shopping=lambda h: ["Lait"])
    with r.as_(TP):
        lea = next(t.task_id for t in r.store.tasks(HID) if t.assignee == ACC[TC])
        tax = next(t.task_id for t in r.store.tasks(HID) if t.project_id == taxes)
    assert w.task_done(WALL, lea) == {"id": lea, "done": True}
    assert w.task_done(WALL, lea) == {"id": lea, "done": True}  # idempotent
    with r.as_(TP):
        t = r.store.task(HID, lea)
    assert t.done_by == "wall:dp_00000000000000aa"
    for bad in (tax, "ht_inconnue"):
        with pytest.raises(WallError) as e:
            w.task_done(WALL, bad)
        assert e.value.status == 404
    sid = wall_mod.shopping_id("Lait")
    assert w.shopping_done(WALL, sid, True) == {"id": sid, "done": True}
    assert w.view(WALL)["shopping"] == [{"id": sid, "text": "Lait", "done": True}]
    with pytest.raises(WallError) as e:
        w.shopping_done(WALL, "sh_000000000000")
    assert e.value.status == 404
    with pytest.raises(WallError) as e:
        w.task_done(GLASSES, lea)
    assert e.value.status == 403 and e.value.code == "not_wall"
    for _ in range(wall_mod.CHECKS_PER_HOUR - 3):
        w.task_done(WALL, lea, False)
    with pytest.raises(WallError) as e:
        w.task_done(WALL, lea)
    assert e.value.status == 429
    r.now += 3601
    assert w.task_done(WALL, lea)["done"] is True


def test_no_household_is_null_and_ha_filter():
    r = Rig()
    w = _wall(r)
    outsider = SimpleNamespace(id="dp_00000000000000cc", account_id=ACC["tn_oscar000000"], household_id="",
                               surface="wall")
    assert w.view(outsider) is None
    stale = SimpleNamespace(**{**WALL.__dict__, "household_id": "pj_autre"})
    assert w.view(stale) is None
    assert ha_entity_ok("light.salon") and not ha_entity_ok("lock.entree")
    assert not ha_entity_ok("alarm_control_panel.x") and not ha_entity_ok("cover.portail_jardin")
    assert ha_entity_ok("cover.store_salon")
    assert not wall_mod.ha_allowed({"entity_id": "cover.c1", "device_class": "garage"})


def test_essentials_structured_readers_reuse_source_and_cache():
    """Mur : météo du jour et départs lus par les services du pack Essentiels (mêmes caches), sans réseau."""
    from datetime import datetime, timedelta, timezone

    from nokker.tools.essentials import meteo
    from tests.unit.tools import essentials_fixtures as fx_

    net = fx_.meteo_net()
    m = fx_.meteo_service(net)
    assert m.today_at(46.2098, 6.1437) == {"now_c": None, "code": 29, "hi_c": 9.3, "lo_c": 6.6,
                                           "summary": meteo.SYMBOLS_FR[29]}
    n = len(net.calls)
    assert m.today_at(46.2101, 6.1440)["hi_c"] == 9.3 and len(net.calls) == n  # caches du run et des points
    assert m.today_at(48.8566, 2.3522) is None  # hors de Suisse : rien d'inventé
    from nokker.tools.essentials import transit

    tnet = fx_.transit_net()
    t = transit.Transit(source=fx_.src(tnet))
    now = datetime(2026, 11, 5, 18, 50, tzinfo=timezone(timedelta(hours=1)))
    assert t.board("8501120", 4, now=now) == [{"stop": "Lausanne", "line": "IR 15", "to": "Genève Aéroport",
                                               "minutes": 5},
                                              {"stop": "Lausanne", "line": "M 2", "to": "Croisettes", "minutes": 7}]
    calls = len(tnet.calls)
    assert [r["minutes"] for r in t.board("8501120", 4, now=now + timedelta(minutes=6))] == [1]
    assert len(tnet.calls) == calls  # cache de 60 s de l'horaire
    assert any("id=8501120" in u for u in tnet.calls)
    with pytest.raises(Exception, match="identifiant"):
        t.board("Lausanne")


def test_wall_place_weather_departures_never_500():
    r = Rig()
    _school_today(r)
    seen = []

    def weather(place):
        seen.append(place)
        return {"now_c": None, "code": 1, "hi_c": 10.0, "lo_c": 2.0, "summary": "ensoleillé"}

    def broken(place):
        raise OSError("réseau coupé")

    w = _wall(r, weather=weather, departures=broken)
    assert w.view(WALL)["weather"] is None and seen == []  # sans lieu réglé : aucun appel
    with r.as_(TP):
        for bad in ({"lat": "x", "lon": 6}, {"lat": 46, "lon": 6, "stops": ["Lausanne"]},
                    {"lat": 46, "lon": 6, "stops": ["8501120"] * 5 + ["1"]}, {"lat": 95, "lon": 6}):
            with pytest.raises(Exception, match="wall_place"):
                r.svc.put_settings({"wall_place": bad})
        r.svc.put_settings({"wall_place": {"label": "Maison", "lat": 46.21, "lon": 6.14, "stops": ["8501120"]}})
        with pytest.raises(Exception, match="adultes"), r.as_(TC):
            r.svc.put_settings({"wall_place": None})
    v = w.view(WALL)
    assert v["weather"]["summary"] == "ensoleillé" and v["departures"] == []  # panne réseau : [], pas un 500
    assert seen == [{"label": "Maison", "lat": 46.21, "lon": 6.14, "stops": ["8501120"]}]
    w.weather = broken
    assert w.view(WALL)["weather"] is None


# ── par le vrai serveur ──


def _req(port, path, *, token, body=None, method=None, headers=None):
    h = {"Authorization": f"Bearer {token}", **(headers or {})}
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, headers=h,
                                 method=method or ("POST" if data is not None else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None), resp.headers
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, (json.loads(raw) if raw else None), e.headers


def test_wall_routes_through_the_real_server(cloud):  # noqa: F811
    from tests.unit.ambient.test_glance_wall import pair

    c = cloud
    r = Rig()
    _school_today(r)
    names = {c.ana.account_id: "Ana"}
    from nokker.household.projects.review import Person
    from nokker.household.projects.service import Home

    people = (Person(c.ana.account_id, "Ana"), Person(ACC[TC], "Léa", "child"))
    r.svc.home_of = lambda acc: Home(HID, TP, people[0], people) if acc == c.ana.account_id else None
    w = HouseholdWall(projects=r.svc, first_name=lambda a: names.get(a, ""), clock=lambda: r.now)
    wall_mod.configure(w)
    try:
        token, _ = pair(c, c.sa, "wall")
        glasses, _ = pair(c, c.sa)
        st, view, hdr = _req(c.port, "/v1/glance/household", token=token)
        assert st == 200 and [t["title"] for t in view["tasks"]] == ["Couvrir les cahiers"]
        tag = hdr["ETag"]
        st, body, _ = _req(c.port, "/v1/glance/household", token=token, headers={"If-None-Match": tag})
        assert st == 304 and body is None
        st, out, _ = _req(c.port, "/v1/glance/household", token=glasses)
        assert st == 403 and out["error"]["code"] == "not_wall"
        assert _req(c.port, "/v1/glance/household", token=c.sa)[0] in (401, 403, 404)  # pas un jeton de mur
        task = view["tasks"][0]["id"]
        st, out, _ = _req(c.port, f"/v1/glance/tasks/{task}/done", token=token, body={})
        assert st == 200 and out == {"id": task, "done": True}
        st, out, _ = _req(c.port, f"/v1/glance/tasks/{task}/done", token=glasses, body={})
        assert st == 403 and out["error"]["code"] == "not_wall"
        st, out, _ = _req(c.port, "/v1/glance/guest", token=token, body={"on": True})
        assert st == 200 and out["guest"] is True
        st, view2, hdr2 = _req(c.port, "/v1/glance/household", token=token)
        assert st == 200 and view2["tasks"] == [] and hdr2["ETag"] != tag
        st, out, _ = _req(c.port, "/v1/glance/shopping/sh_000000000000/done", token=token, body={"done": True})
        assert st == 404
        # Le reste du périmètre glance ne bouge pas.
        st, out, _ = _req(c.port, "/v1/household/projects", token=token)
        assert st == 403 and out["error"]["code"] == "glance_scope"
    finally:
        wall_mod.configure(None)


from tests.unit.ambient.test_glance_wall import cloud  # noqa: E402, F401  (fixture du serveur glance)
