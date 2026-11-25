"""Le contrôle de démarrage de la mémoire directe s'arrête proprement : pas de fil qui boucle après la fin d'un test."""
from __future__ import annotations

import threading
import time

from nokker.memory import corthexis as cx


def _alive():
    return [t for t in threading.enumerate() if t.name == "nokker-memory-check" and t.is_alive()]


def _down_memory(monkeypatch):
    mem = object.__new__(cx.DirectCorthexisMemory)
    mem.status = lambda max_age_s=0.0: {"ok": False, "error": "embed injoignable"}
    monkeypatch.setattr(cx, "STARTUP_RETRY_S", 600)
    monkeypatch.setattr(cx, "STARTUP_RETRY_EVERY_S", 15)
    return mem


def _wait_gone(deadline_s=5.0):
    t0 = time.monotonic()
    while _alive() and time.monotonic() - t0 < deadline_s:
        time.sleep(0.02)
    return not _alive()


def test_close_stops_the_retry_thread(monkeypatch):
    mem = _down_memory(monkeypatch)
    mem.warm_up()
    assert _alive()
    mem.stop_warm_up()
    assert _wait_gone()
    mem.stop_warm_up()  # idempotent


def test_stop_all_warm_ups_stops_every_thread(monkeypatch):
    for _ in range(3):
        _down_memory(monkeypatch).warm_up()
    assert len(_alive()) >= 3
    cx.stop_all_warm_ups()
    assert _wait_gone()
    cx.stop_all_warm_ups()


def test_a_kernel_built_in_a_test_leaves_no_retry_thread(monkeypatch):
    """Le chemin réel : `from_config` en mode direct sur une base injoignable lance le contrôle ; le fixture autouse du
    conftest l'arrête après le test (vérifié par le test suivant)."""
    class Cfg:
        memory_mode = "direct"
        memory_direct = {}
    monkeypatch.setattr(cx.DirectCorthexisMemory, "status", lambda self, max_age_s=0.0: {"ok": False, "error": "x"})
    cx.from_config(Cfg())
    assert _alive()


def test_previous_test_thread_is_gone():
    assert _wait_gone()
