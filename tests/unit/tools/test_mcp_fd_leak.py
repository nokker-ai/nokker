"""Fuite de descripteurs : une connexion MCP par jeu d'en-têtes de session, sur la VRAIE pile du client (SDK `mcp`,
serveur streamable-HTTP en loopback). 500 sessions ouvertes puis abandonnées : le nombre de FD reste borné pendant
(cache de 64 entrées de ~3 FD chacune) et revient à sa base après (boucle epoll, sockets, client HTTP, fil : tout est rendu)."""
from __future__ import annotations

import gc
import os
import threading

import pytest

from nokker.core.ports import ToolContext
from nokker.tools import mcp_session
from nokker.tools.mcp import McpToolSource
from tests.unit.tools.fake_mcp_server import FakeMcpServer


def _fds() -> int:
    return len(os.listdir("/proc/self/fd"))


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(mcp_session, "_REGISTRY", mcp_session.SessionHeaders())


def _ctx(tid):
    return ToolContext(thread_id=tid, turn_index=0, channel="api")


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="Linux seulement")
@pytest.mark.parametrize("era,kw", [("legacy", {}), ("modern", {"modern": True})])
def test_500_sessions_do_not_leak_descriptors(era, kw):
    with FakeMcpServer(era) as server:
        src = McpToolSource(name="fake", url=server.url, headers={"X-Static": "s"}, timeout_s=10.0, **kw)
        src.specs()  # connexion du serveur ouverte : elle fait partie de la base
        gc.collect()
        base_fds, base_threads = _fds(), threading.active_count()
        peak = 0
        for i in range(500):
            mcp_session.registry().set(f"th{i}", {"fake": {"Authorization": f"Bearer jeton-{i}"}})
            r = src.call("echo", {"text": "x"}, _ctx(f"th{i}"))
            assert not r.is_error
            peak = max(peak, _fds())
        assert peak <= base_fds + 64 * 5 + 4, (base_fds, peak)  # 64 entrées × (epoll, paire de sockets, 2 sockets HTTP)
        assert len(src._session_io) <= 64
        for i in range(500):  # fin des sessions : leurs connexions partent avec
            src.release_thread(f"th{i}")
        gc.collect()
        assert len(src._session_io) == 0
        assert _fds() <= base_fds + 4, (base_fds, _fds())
        assert threading.active_count() <= base_threads + 2
        src.close()


def test_a_closed_connection_reopens_for_the_same_thread():
    with FakeMcpServer("legacy") as server:
        src = McpToolSource(name="fake", url=server.url, timeout_s=10.0)
        mcp_session.registry().set("t", {"fake": {"X-A": "1"}})
        assert not src.call("echo", {"text": "x"}, _ctx("t")).is_error
        src.release_thread("t")
        assert not src._session_io
        assert not src.call("echo", {"text": "x"}, _ctx("t")).is_error  # même fil : rouvre proprement
        src.close()
        src.close()  # idempotent


def test_closing_a_session_stops_its_stdio_process_and_connections(monkeypatch):
    """La fin d'une session (`close_thread` de l'hôte) arrête aussi le processus MCP stdio du fil."""
    from nokker.local.api import SessionCodeTools
    from nokker.tools import mcp_stdio

    stopped = []

    class _Src:
        def close_thread(self, tid):
            stopped.append(tid)

    src = _Src()
    monkeypatch.setattr(mcp_stdio, "_LIVE_SOURCES", [src])
    SessionCodeTools(backend="local").close_thread("th_x")
    assert stopped == ["th_x"]
