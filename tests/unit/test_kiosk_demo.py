"""« Foyer démo » du kiosque (nokker/kiosk_demo.py, docs/KIOSK.md §1.2) : vue Foyer FICTIVE lue en configuration, jours
relatifs à aujourd'hui, forme `GlanceHousehold` (sans météo, départs ni Home Assistant), marquée `demo: true` ; absente
par défaut (la vue du kiosque reste celle d'avant) ; configuration mal formée refusée EN ENTIER sans éteindre le
kiosque ; aucune donnée de compte."""
from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from nokker.kiosk import Kiosk
from nokker.kiosk_demo import Demo, DemoError

T0 = 1_794_236_400.0  # 09.11.2026 16:00 Europe/Zurich
BASE_KEYS = {"now", "tz", "weather", "departures", "ha", "presenter", "night"}

DEMO = {
    "members": [{"name": "Léa", "color": "#E4572E"}, {"name": "Hugo"}, "Charlie"],
    "agenda": [{"day": 2, "all_day": True, "member": "Léa", "title": "Sortie d'école"},
               {"day": 0, "time": "17:30", "duration_min": 90, "member": "Hugo", "title": "Foot"}],
    "tasks": [{"member": "Léa", "title": "Devoirs", "done": False, "due_day": 0}, {"title": "Sortir les poubelles", "done": True}],
    "meals": [{"day": 1, "dinner": "Soupe"}, {"day": 0, "lunch": "Pâtes", "dinner": "Gratin"}],
    "shopping": ["Pain", {"text": "Lait", "done": True}],
    "projects": [{"title": "Anniversaire de Charlie", "progress": 0.4, "next_step": "Réserver la salle", "next_due_day": 3}],
}


def kiosk(**raw):
    k = Kiosk.from_config({"enabled": True, **raw}, clock=lambda: T0)
    assert k is not None
    return k


def test_absent_by_default_the_kiosk_view_is_unchanged():
    v = kiosk().view()
    assert set(v) == BASE_KEYS and "household" not in v


def test_demo_view_has_the_wall_household_shape_and_is_marked_demo():
    v = kiosk(household_demo=DEMO).view()
    assert set(v) == BASE_KEYS | {"household"}
    h = v["household"]
    assert h["demo"] is True and h["guest"] is False
    assert [m["name"] for m in h["members"]] == ["Léa", "Hugo", "Charlie"]
    assert h["members"][0]["color"] == "#E4572E" and h["members"][1]["initials"] and h["members"][0]["id"] == "demo-m1"
    assert set(h) == {"demo", "guest", "members", "agenda", "tasks", "meals", "shopping", "projects", "at"}  # ni météo, ni HA
    assert h["at"] == v["now"]


def test_days_are_relative_to_today_in_the_kiosk_timezone():
    h = kiosk(household_demo=DEMO).view()["household"]
    foot, sortie = h["agenda"]  # triés par début
    assert foot["start"] == "2026-11-09T17:30:00+01:00" and foot["end"] == "2026-11-09T19:00:00+01:00"
    assert foot["member_id"] == "demo-m2" and foot["all_day"] is False and foot["title"] == "Foot"
    assert sortie == {"id": "demo-e1", "member_id": "demo-m1", "all_day": True, "title": "Sortie d'école", "start": "2026-11-11"}
    assert h["tasks"][0] == {"id": "demo-t1", "member_id": "demo-m1", "title": "Devoirs", "done": False, "due": "2026-11-09"}
    assert h["tasks"][1] == {"id": "demo-t2", "member_id": None, "title": "Sortir les poubelles", "done": True}
    assert [m["date"] for m in h["meals"]] == ["2026-11-09", "2026-11-10"] and h["meals"][0]["lunch"] == "Pâtes"
    assert h["shopping"] == [{"id": "demo-s1", "text": "Pain", "done": False}, {"id": "demo-s2", "text": "Lait", "done": True}]
    assert h["projects"] == [{"id": "demo-p1", "title": "Anniversaire de Charlie", "progress": 0.4,
                              "next_step": "Réserver la salle", "next_due": "2026-11-12"}]
    # le lendemain, la démo glisse avec la date (elle ne vieillit pas)
    later = Demo.parse(DEMO).view(datetime(2026, 11, 20, 8, 0, tzinfo=ZoneInfo("Europe/Zurich")))
    assert later["agenda"][0]["start"].startswith("2026-11-20T17:30")


@pytest.mark.parametrize("bad", [
    {"members": [{"name": "Léa", "age": 9}]},  # champ inconnu
    {"members": [{"name": "Léa"}, {"name": "Léa"}]},  # doublon
    {"members": [{"name": "Léa", "color": "rouge"}]},
    {"members": ["Léa"], "agenda": [{"day": 9, "all_day": True, "title": "x"}]},  # jour hors 0..6
    {"members": ["Léa"], "agenda": [{"day": 0, "time": "25:00", "title": "x"}]},
    {"members": ["Léa"], "agenda": [{"day": 0, "all_day": True, "member": "Zoé", "title": "x"}]},  # membre inconnu
    {"members": ["Léa"], "tasks": [{"title": "x" * 81}]},
    {"members": ["Léa"], "projects": [{"title": "p", "progress": 1.5}]},
    {"members": ["a", "b", "c", "d", "e", "f", "g"]},  # 7 membres
    {"members": ["Léa"], "calendar": "https://exemple"},  # clé inconnue (aucune source externe)
    {},  # vide
])
def test_a_bad_demo_is_refused_whole_and_the_kiosk_stays_up(bad, caplog):
    with pytest.raises(DemoError):
        Demo.parse(bad)
    k = kiosk(household_demo=bad)
    assert "household" not in k.view()  # pas de demi-vue ; le kiosque, lui, répond
    assert "household_demo refusé" in caplog.text


def test_control_and_bidi_characters_are_stripped_and_nothing_else_leaks():
    h = Demo.parse({"members": ["Lé‮a\u0007"], "tasks": [{"title": "Dev​oirs"}]}).view(
        datetime(2026, 11, 9, 16, 0, tzinfo=ZoneInfo("Europe/Zurich")))
    assert h["members"][0]["name"] == "Léa" and h["tasks"][0]["title"] == "Devoirs"
    assert not any(w in json.dumps(h) for w in ("@", "http", "account", "us_"))


def test_demo_does_not_change_the_other_kiosk_fields():
    plain, demo = kiosk().view(), kiosk(household_demo=DEMO).view()
    assert {k: demo[k] for k in BASE_KEYS} == plain
