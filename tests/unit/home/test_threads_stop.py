"""Arrêt idempotent des fils de fond de la passerelle du foyer, du balayage des notifications et de la boucle AirPlay."""
from __future__ import annotations

import threading
import time

from nokker import bg_stop


def _alive(name):
    return [t for t in threading.enumerate() if t.name == name and t.is_alive()]


def test_serve_close_stops_both_servers_and_threads(tmp_path):
    from tests.unit.home.fake_ha import FakeHomeAssistant
    from tests.unit.home.test_home import start_bridge

    before = {t.ident for t in threading.enumerate()}
    start_bridge(tmp_path, FakeHomeAssistant())
    mine = [t for t in threading.enumerate() if t.ident not in before and t.name.startswith("home-bridge-")]
    assert {t.name for t in mine} >= {"home-bridge-api", "home-bridge-lan"}
    assert bg_stop.stop_all() >= 1
    assert bg_stop.stop_all() == 0
    assert not [t for t in mine if t.is_alive()]


def test_notify_sweeper_stops_without_waiting_a_period():
    from nokker.api.server import _start_notify_sweeper

    class Hub:
        n = 0

        def sweep_expired(self):
            Hub.n += 1

    s = _start_notify_sweeper(Hub(), period_s=3600.0)
    assert _alive("nokker-notify-sweeper")
    t0 = time.monotonic()
    s.stop()
    s.stop()
    assert time.monotonic() - t0 < 1.5 and not s._thread.is_alive()


def test_airplay_backend_close_stops_loop_thread():
    from nokker.cast.screens import AirPlayBackend

    b = AirPlayBackend()

    async def noop():
        return 7

    assert b._run(noop()) == 7
    t = b._thread
    assert t is not None and t.is_alive()
    b.close()
    b.close()
    assert not t.is_alive()
    assert b._run(noop()) == 7  # rouvrable
    b.close()
