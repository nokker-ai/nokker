"""Fils de fond de la passerelle du foyer et des ponts : un seul abonné aux événements HA par passerelle (même sous
deux sondages simultanés), et un arrêt idempotent pour chaque fil de fond (`stop` / `close`)."""
from __future__ import annotations

import threading
import time

from nokker import bg_stop
from nokker.homebridge import ha as ha_mod


class _Silent:
    """Faux client HA : une session qui n'émet jamais d'événement (attente courte) et se ferme."""

    class _S:
        def subscribe_state_changed(self):
            return None

        def events(self, timeout):
            time.sleep(0.02)
            return []

        def close(self):
            return None

    def session(self):
        return self._S()


def _alive(name):
    return [t for t in threading.enumerate() if t.name == name and t.is_alive()]


def test_two_simultaneous_polls_start_one_ha_subscriber(monkeypatch):
    ops = ha_mod.HaOps(ha=_Silent(), entities=("light.*",))
    real = threading.Thread

    class Slow(real):  # élargit la fenêtre entre « aucun abonné » et « abonné inscrit »
        def __init__(self, *a, **k):
            time.sleep(0.05)
            super().__init__(*a, **k)

    monkeypatch.setattr(ha_mod.threading, "Thread", Slow)
    before = len(_alive("home-bridge-ha-events"))
    go = threading.Barrier(5)

    def poll():
        go.wait()
        ops._ensure_subscriber()

    ts = [real(target=poll) for _ in range(5)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    try:
        assert len(_alive("home-bridge-ha-events")) - before == 1
    finally:
        ops.stop()
    assert len(_alive("home-bridge-ha-events")) == before


def test_ha_subscriber_stop_is_idempotent_and_final():
    ops = ha_mod.HaOps(ha=_Silent(), entities=("light.*",))
    ops._ensure_subscriber()
    assert ops._sub is not None and ops._sub.is_alive()
    ops.stop()
    ops.stop()
    assert not ops._sub.is_alive()
    ops._ensure_subscriber()  # arrêtée : pas de nouvel abonné
    assert not ops._sub.is_alive()


def test_knock_socket_close_is_idempotent(tmp_path):
    from nokker.homebridge.knock.events import KnockBridge, KnockConfig

    kb = KnockBridge(KnockConfig(socket_path=str(tmp_path / "k.sock"), mute_dir=str(tmp_path)))
    t = kb.serve_socket()
    assert t.is_alive()
    kb.close()
    kb.close()
    assert not t.is_alive()
    t2 = kb.serve_socket()  # rouvrable, sans laisser l'ancien fil
    assert t2.is_alive() and not t.is_alive()
    kb.close()
    assert not t2.is_alive()


def test_bg_stop_ignores_collected_and_failing_resources():
    class Boom:
        def close(self):
            raise RuntimeError("x")

    keep = Boom()
    bg_stop.track(keep)
    bg_stop.track(Boom())  # ramassé aussitôt
    assert bg_stop.stop_all() <= 1  # ne lève jamais
