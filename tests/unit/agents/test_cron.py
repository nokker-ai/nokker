"""`nokker/agents/cron.py` — cron à 5 champs en heure murale d'un fuseau, changements d'heure compris."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from nokker.agents.cron import Cron, CronError, next_run, zone

UTC = timezone.utc


def _u(text):
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def test_weekdays_at_seven_in_zurich_winter_and_summer():
    # vendredi 24.10.2026 08:00 Zurich (été, UTC+2) → lundi 26.10 07:00 (hiver, UTC+1)
    assert next_run("0 7 * * 1-5", "Europe/Zurich", _u("2026-10-24T06:00:00")) == _u("2026-10-26T06:00:00")
    # lundi 7:00 en été : 05:00 UTC
    assert next_run("0 7 * * 1-5", "Europe/Zurich", _u("2026-10-19T04:00:00")) == _u("2026-10-19T05:00:00")


def test_strictly_after():
    assert next_run("0 7 * * *", "UTC", _u("2026-10-26T07:00:00")) == _u("2026-10-27T07:00:00")
    assert next_run("0 7 * * *", "UTC", _u("2026-10-26T06:59:59")) == _u("2026-10-26T07:00:00")


def test_steps_ranges_lists_and_names():
    c = Cron.parse("*/15 9-17/4 1,15 jan-mar mon,fri")
    assert c.minutes == frozenset({0, 15, 30, 45}) and c.hours == frozenset({9, 13, 17})
    assert c.doms == frozenset({1, 15}) and c.months == frozenset({1, 2, 3}) and c.dows == frozenset({1, 5})
    assert Cron.parse("0 0 * * 7").dows == frozenset({0})  # 7 = dimanche
    assert Cron.parse("5/20 * * * *").minutes == frozenset({5, 25, 45})


def test_dom_and_dow_both_restricted_means_either():
    # le 13 OU un vendredi : après le jeudi 1.10.2026, le vendredi 2.10
    assert next_run("0 12 13 * 5", "UTC", _u("2026-10-01T13:00:00")) == _u("2026-10-02T12:00:00")
    # seulement le jour du mois
    assert next_run("0 12 13 * *", "UTC", _u("2026-10-01T13:00:00")) == _u("2026-10-13T12:00:00")


def test_spring_gap_occurrence_is_skipped():
    # 29.03.2026 : 02:00 → 03:00 à Zurich ; 02:30 n'existe pas ce jour-là
    got = next_run("30 2 * * *", "Europe/Zurich", _u("2026-03-28T02:00:00"))
    assert got == _u("2026-03-30T00:30:00")  # le lendemain 02:30 (UTC+2)


def test_fall_back_hour_runs_once_only():
    # 25.10.2026 : 03:00 → 02:00 ; 02:30 existe deux fois, une seule échéance (la première, UTC+2)
    first = next_run("30 2 * * *", "Europe/Zurich", _u("2026-10-24T23:00:00"))
    assert first == _u("2026-10-25T00:30:00")
    assert next_run("30 2 * * *", "Europe/Zurich", first) == _u("2026-10-26T01:30:00")


def test_every_minute_over_fall_back_has_no_duplicate_wall_time():
    c, z = Cron.parse("*/30 * * * *"), zone("Europe/Zurich")
    t, seen = _u("2026-10-24T23:00:00"), []
    for _ in range(8):
        t = c.next_after(t, z)
        seen.append(t)
    assert seen == sorted(seen) and len(set(seen)) == 8


def test_no_occurrence_returns_none():
    assert Cron.parse("0 0 30 2 *").next_after(_u("2026-01-01T00:00:00"), zone("UTC")) is None


@pytest.mark.parametrize("expr", ["", "* * * *", "61 * * * *", "* 24 * * *", "* * 0 * *", "* * * 13 *",
                                  "*/0 * * * *", "5-2 * * * *", "a * * * *", "* * * * 8", "1,,2 * * * *"])
def test_invalid_expressions(expr):
    with pytest.raises(CronError):
        Cron.parse(expr)


def test_unknown_zone():
    with pytest.raises(CronError):
        zone("Mars/Olympus")
