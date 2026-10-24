"""Quotas de l'offre Solo (nokker/tenancy/limits.py, décision de Alice du 22.10.2026) : 40 tours par jour (12:00 UTC →
12:00 UTC), 200 par semaine (lundi 06:00 UTC), mode soft pendant la bêta (on compte, on prévient une fois, seul le
plafond de sécurité coupe), mode hard ensuite."""
from datetime import UTC, datetime, timedelta

import pytest

from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope
from nokker.tenancy.limits import MemoryUsage, TenantLimits, day_window, week_window

TESTER = "tn_aaaaaaaaaaaa"


def at(*args):
    return datetime(*args, tzinfo=UTC)


@pytest.mark.parametrize("now,start", [
    (at(2026, 10, 28, 11, 59), at(2026, 10, 27, 12)),
    (at(2026, 10, 28, 12, 0), at(2026, 10, 28, 12)),
    (at(2026, 10, 28, 23, 30), at(2026, 10, 28, 12)),
])
def test_day_window_runs_noon_to_noon_utc(now, start):
    assert day_window(now) == (start, start + timedelta(days=1))


@pytest.mark.parametrize("now,start", [
    (at(2026, 11, 2, 5, 59), at(2026, 10, 26, 6)),   # lundi avant 06:00 : semaine précédente
    (at(2026, 11, 2, 6, 0), at(2026, 11, 2, 6)),
    (at(2026, 11, 8, 23, 59), at(2026, 11, 2, 6)),   # dimanche soir
])
def test_week_window_starts_monday_0600_utc(now, start):
    assert week_window(now) == (start, start + timedelta(days=7))


def rig(**kw):
    now = [at(2026, 10, 28, 9, 0)]
    limits = TenantLimits(counter=MemoryUsage(), clock=lambda: now[0], **kw)
    return limits, now


def test_soft_mode_warns_once_per_window_and_only_the_safety_cap_cuts():
    limits, _ = rig(turns_per_day=2, turns_per_week=100, hard_turns_per_day=4)
    with tenant_scope(TESTER):
        seen = []
        for _ in range(4):
            assert limits.check_turn("us_a") is None
            seen.append(limits.notice("us_a"))
        assert [n is not None for n in seen] == [False, False, True, False]
        assert "2 messages du jour" in seen[2] and "rien n'est coupé" in seen[2] and "13:00" in seen[2]
        over = limits.check_turn("us_a")
        assert over.rule == "limits:hard_turns_per_day"
        assert limits.reply(over).startswith("Limite du jour atteinte : 4 messages")


def test_soft_mode_warns_when_the_week_is_passed():
    limits, now = rig(turns_per_day=10, turns_per_week=3, hard_turns_per_day=10)
    with tenant_scope(TESTER):
        notices = []
        for day in range(4):  # un tour par jour : jamais le quota du jour, la semaine au 4e
            assert limits.check_turn("us_a") is None
            notices.append(limits.notice("us_a"))
            now[0] += timedelta(days=1)
        assert [n is not None and "de la semaine" in n for n in notices] == [False, False, False, True]


def test_hard_mode_refuses_on_the_week_and_gives_back_the_day():
    limits, now = rig(mode="hard", turns_per_day=5, turns_per_week=2, hard_turns_per_day=150)
    with tenant_scope(TESTER):
        assert limits.check_turn("us_a") is None and limits.check_turn("us_a") is None
        over = limits.check_turn("us_a")
        assert over.rule == "limits:turns_per_week" and "lundi" in limits.reply(over)
        assert limits.usage("us_a")["today"]["turns"]["used"] == 2  # le tour refusé n'est pas compté au jour
        assert limits.notice("us_a") is None  # jamais d'avertissement en mode hard
        now[0] = at(2026, 11, 2, 6, 0)  # lundi 06:00 UTC : nouvelle semaine
        assert limits.check_turn("us_a") is None


def test_usage_reports_both_windows_in_swiss_time():
    limits, _ = rig()
    with tenant_scope(TESTER):
        for _ in range(3):
            limits.check_turn("us_a")
        u = limits.usage("us_a")
    assert u["mode"] == "soft" and u["tz"] == "Europe/Zurich" and u["plan"] is None
    assert u["today"]["day"] == "2026-10-27" and u["today"]["resets_at"] == "2026-10-28T13:00:00+01:00"
    assert u["today"]["turns"] == {"used": 3, "cap": 40, "over": False, "hard_cap": 150}
    assert u["week"]["starts"] == "2026-10-26T07:00:00+01:00"
    assert u["week"]["resets_at"] == "2026-11-02T07:00:00+01:00"
    assert u["week"]["turns"] == {"used": 3, "cap": 200, "over": False}


def test_internal_tenant_is_never_counted():
    limits, _ = rig(turns_per_day=0, hard_turns_per_day=0)
    with tenant_scope(INTERNAL_TENANT):
        assert all(limits.check_turn("alice") is None for _ in range(3))
        assert limits.notice("alice") is None
        assert limits.usage("alice")["today"]["turns"] == {"used": 0, "cap": None, "over": False, "hard_cap": None}


def test_from_config_defaults_to_the_solo_plan_in_soft_mode():
    limits = TenantLimits.from_config({})
    assert (limits.mode, limits.turns_per_day, limits.turns_per_week, limits.hard_turns_per_day) == ("soft", 40, 200, 150)
    with pytest.raises(ValueError):
        TenantLimits.from_config({"mode": "strict"})
