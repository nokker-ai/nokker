"""V7 : le catalogue refuse tout ce qui n'en est pas, le rendu est statique, échappé, sous CSP, et passe le plancher."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from nokker.design.floor import check
from nokker.screens.catalog import SpecError, validate
from nokker.screens.render import CSP, CSS, render

NOW = datetime(2026, 11, 9, 7, 30, tzinfo=timezone.utc)
FULL = {"title": "Bonjour la maison", "theme": "dark", "layout": "columns", "blocks": [
    {"type": "group", "title": "Aujourd'hui", "blocks": [{"type": "clock"}, {"type": "next_event"},
                                                          {"type": "note", "text": "Piscine à 17 h", "tone": "warn"}]},
    {"type": "group", "title": "Courses", "blocks": [
        {"type": "checklist", "items": [{"text": "pain", "done": True}, {"text": "lait"}, {"text": "œufs"}]},
        {"type": "divider"}, {"type": "stat", "label": "jours avant les vacances", "value": "12"}]},
    {"type": "group", "blocks": [{"type": "heading", "text": "Menu"}, {"type": "list", "items": ["Lundi : soupe"]},
                                 {"type": "text", "text": "Bon appétit"}]}]}


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_full_screen_passes_the_design_floor(theme):
    spec = validate({**FULL, "theme": theme})
    out = render(spec, now=NOW, next_event={"start": "2026-11-09T09:00:00Z", "all_day": False, "title": "Dentiste"})
    findings = check(out)
    assert findings == [], [(f.rule, f.message) for f in findings]
    assert "<script" not in out and "http" not in out.replace('http-equiv', '') and " style=" not in out


def test_text_is_escaped_and_csp_matches_the_only_style():
    spec = validate({"title": "<script>alert(1)</script>", "blocks": [{"type": "text", "text": '"><img src=x onerror=1>'}]})
    out = render(spec, now=NOW)
    assert "<script>" not in out and "<img" not in out and "&lt;script&gt;" in out
    assert CSP.startswith("default-src 'none'; style-src 'sha256-")
    assert out.count("<style>") == 1 and CSS in out


def test_preview_never_shows_household_data():
    spec = validate({"title": "Mur", "blocks": [{"type": "next_event"}]})
    out = render(spec, now=NOW, next_event={"start": "2026-11-09T09:00:00Z", "title": "Secret"}, preview=True)
    assert "Secret" not in out and "Sur le mur : ton prochain rendez-vous." in out


@pytest.mark.parametrize("bad,needle", [
    ({"title": "x", "blocks": [{"type": "html", "html": "<b>"}]}, "type inconnu"),
    ({"title": "x", "blocks": [{"type": "text", "text": "a", "style": "color:red"}]}, "champs inconnus"),
    ({"title": "x", "blocks": [{"type": "group", "blocks": [{"type": "group", "blocks": [{"type": "divider"}]}]}]},
     "ne contient pas de group"),
    ({"title": "x", "layout": "columns", "blocks": [{"type": "text", "text": "a"}]}, "que des group"),
    ({"title": "", "blocks": [{"type": "divider"}]}, "title"),
    ({"title": "x", "blocks": [{"type": "list", "items": ["a"] * 11}]}, "items"),
    ({"title": "x", "blocks": [{"type": "divider"}] * 17}, "au plus 16"),
    ({"title": "x", "theme": "neon", "blocks": [{"type": "divider"}]}, "theme"),
    ({"title": "x", "css": "*{}", "blocks": [{"type": "divider"}]}, "champs inconnus"),
])
def test_catalog_refuses_anything_else(bad, needle):
    with pytest.raises(SpecError) as e:
        validate(bad)
    assert any(needle in p for p in e.value.problems), e.value.problems


def test_controls_and_bidi_are_stripped():
    spec = validate({"title": "a‮b", "blocks": [{"type": "text", "text": "x\x07y"}]})
    assert spec["title"] == "ab" and spec["blocks"][0]["text"] == "xy"


def test_tomorrow_is_said_tomorrow():
    spec = validate({"title": "Mur", "blocks": [{"type": "next_event"}]})
    out = render(spec, now=NOW, next_event={"start": "2026-11-10T08:00:00+00:00", "all_day": False})
    assert "demain, 08:00" in out
