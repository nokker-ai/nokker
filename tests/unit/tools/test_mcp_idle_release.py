"""Libération à l'idle des connexions MCP de session, sur la VRAIE pile du client (SDK `mcp`, serveur streamable-HTTP en
loopback). Horloge simulée : le balayage reçoit un « maintenant » avancé, aucun sleep long."""
from __future__ import annotations

import gc
import os
import threading
import time

import pytest

from nokker.core.ports import ToolContext
from nokker.tools import mcp as mcp_mod
from nokker.tools import mcp_session
from nokker.tools.mcp import McpToolSource
from tests.unit.tools.fake_mcp_server import FakeMcpServer

pytestmark = pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="Linux seulement")


def _fds() -> int:
    return len(os.listdir("/proc/self/fd"))


def _ctx(tid):
    return ToolContext(thread_id=tid, turn_index=0, channel="api")


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(mcp_session, "_REGISTRY", mcp_session.SessionHeaders())
    monkeypatch.setattr(mcp_mod, "_idle_s", 300.0)
    yield
    mcp_mod._stop_idle_thread()


def _call(src, i):
    mcp_session.registry().set(f"th{i}", {"fake": {"Authorization": f"Bearer jeton-{i}"}})
    r = src.call("echo", {"text": "x"}, _ctx(f"th{i}"))
    assert not r.is_error
    return r


def test_unused_connection_is_closed_after_ttl_and_fds_return_to_base():
    """Horloge simulée seule. La libération se lit sur LA connexion (sa boucle fermée, son fil terminé, donc son epoll et
    ses sockets rendus), jamais sur le nombre de FD du processus : lui bouge avec les autres fils (serveur factice,
    ramasse-miettes) et rendait le test instable sous charge."""
    with FakeMcpServer("legacy") as server:
        src = McpToolSource(name="fake", url=server.url, timeout_s=10.0)
        src.specs()
        _call(src, 0)
        assert len(src._session_io) == 1
        io = next(iter(src._session_io.values()))
        loop, thread = io._loop, io._thread
        assert loop is not None and not loop.is_closed() and thread is not None and thread.is_alive()
        assert mcp_mod.sweep_idle(now=io.last_used + 100) == 0  # trop tôt
        assert len(src._session_io) == 1 and not loop.is_closed()
        assert mcp_mod.sweep_idle(now=io.last_used + 301) == 1
        assert not src._session_io
        thread.join(60)  # fin de la boucle : point de synchronisation (borné pour échouer plutôt que bloquer)
        assert loop.is_closed() and not thread.is_alive()
        assert io._loop is None and io._session is None  # rien n'est gardé : la connexion est rouvrable à vide
        src.close()


def test_a_call_in_flight_is_never_closed():
    with FakeMcpServer("legacy") as server:
        src = McpToolSource(name="fake", url=server.url, timeout_s=10.0)
        mcp_session.registry().set("t", {"fake": {"X-A": "1"}})
        io = src._io_for("t")  # appel « en vol » : compteur d'usage posé
        assert io.active == 1
        assert mcp_mod.sweep_idle(now=time.monotonic() + 10_000) == 0
        assert len(src._session_io) == 1
        src._done(io)
        assert io.active == 0
        assert mcp_mod.sweep_idle(now=time.monotonic() + 10_000) == 1
        src.close()


def test_reuse_after_idle_close_reopens_and_succeeds():
    with FakeMcpServer("modern") as server:
        src = McpToolSource(name="fake", url=server.url, timeout_s=10.0, modern=True)
        _call(src, 0)
        assert mcp_mod.sweep_idle(now=time.monotonic() + 301) == 1
        assert not src._session_io
        _call(src, 0)  # même fil : rouvre proprement
        assert len(src._session_io) == 1 and src._session_io[next(iter(src._session_io))].active == 0
        src.close()


def test_ttl_zero_closes_nothing():
    with FakeMcpServer("legacy") as server:
        src = McpToolSource(name="fake", url=server.url, timeout_s=10.0)
        mcp_mod.configure_idle(0)
        _call(src, 0)
        assert mcp_mod._idle_thread is None  # pas de fil quand c'est désactivé
        assert mcp_mod.sweep_idle(now=time.monotonic() + 10**6) == 0
        assert len(src._session_io) == 1
        src.close()


def test_shared_connection_is_never_idle_closed():
    with FakeMcpServer("legacy") as server:
        src = McpToolSource(name="fake", url=server.url, timeout_s=10.0)
        src.specs()
        assert not src.call("echo", {"text": "x"}, _ctx("sans-entetes")).is_error
        mcp_mod.sweep_idle(now=time.monotonic() + 10**6)
        assert not src.call("echo", {"text": "x"}, _ctx("sans-entetes")).is_error
        assert src._io._loop is not None  # toujours ouverte
        src.close()


@pytest.mark.parametrize("era,kw", [("legacy", {}), ("modern", {"modern": True})])
def test_64_kept_connections_then_idle_return_to_base(era, kw):
    with FakeMcpServer(era) as server:
        src = McpToolSource(name="fake", url=server.url, timeout_s=10.0, **kw)
        src.specs()
        gc.collect()
        base = _fds()
        for i in range(64):
            _call(src, i)
        assert len(src._session_io) == 64
        assert _fds() > base + 64
        assert mcp_mod.sweep_idle(now=time.monotonic() + 301) == 64
        gc.collect()
        assert not src._session_io
        assert _fds() <= base + 4, (base, _fds())
        src.close()


def test_background_thread_sweeps_and_stops_on_close_sessions():
    with FakeMcpServer("legacy") as server:
        src = McpToolSource(name="fake", url=server.url, timeout_s=10.0)
        mcp_mod._idle_step_s = 0.05
        try:
            mcp_mod.configure_idle(30)
            _call(src, 0)
            threads = [t for t in threading.enumerate() if t.name == "nokker-mcp-idle"]
            assert len(threads) == 1 and threads[0].daemon
            _call(src, 1)  # pas de nouveau fil par connexion
            assert len([t for t in threading.enumerate() if t.name == "nokker-mcp-idle"]) == 1
            # horloge simulée : les connexions ont l'air vieilles de 31 s
            for io in src._session_io.values():
                io.last_used -= 31
            deadline = time.monotonic() + 5
            while src._session_io and time.monotonic() < deadline:
                time.sleep(0.05)
            assert not src._session_io
            _call(src, 2)
            mcp_mod.close_sessions()
            assert not any(t.name == "nokker-mcp-idle" for t in threading.enumerate())
            assert not src._session_io
        finally:
            mcp_mod._idle_step_s = None
            src.close()


def test_config_bounds():
    from nokker.local.api import _mcp_idle

    assert _mcp_idle(None) == 300 and _mcp_idle(0) == 0 and _mcp_idle(30) == 30 and _mcp_idle(86400) == 86400
    for bad in (1, 29, 86401, -5, True, "300", 1.5):
        with pytest.raises(ValueError):
            _mcp_idle(bad)
