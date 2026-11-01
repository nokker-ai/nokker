"""Nokker comme serveur MCP (`python -m nokker mcp serve`, docs/MCP-SERVER.md) : le client MCP du SDK lance le vrai
serveur stdio (sous-processus) contre une vraie API `/v1` en boucle locale, au nom d'un compte testeur. Isolation entre
comptes (fils, mémoire), jeton invalide refusé, route mémoire éteinte par défaut."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from nokker.api.events import EventBus
from nokker.api.server import make_handler
from nokker.bubbles.hub import BubbleHub
from nokker.bubbles.memory import InMemoryBubbles
from nokker.core.ports import Recall
from nokker.tenancy.context import current_tenant
from nokker.tenancy.resources import TenantMemory
from nokker.tenancy.store import MemoryTenancy
from nokker.threads.memory import InMemoryThreads
from tests.unit.api.test_bubbles_routes import call
from tests.unit.api.test_server import _Server, make_kernel

ROOT = Path(__file__).resolve().parents[3]


class Notes:
    """Mémoire d'un locataire : rend une note qui porte le locataire qui l'a demandée."""

    def __init__(self, tid):
        self.tid = tid

    def recall(self, query, k=5):
        assert current_tenant() == self.tid  # résolue dans le bon tenant_scope
        return [Recall(note=f"note-{self.tid}", text=f"{query} chez {self.tid}", score=0.9)]

    def propose(self, t, f):
        pass


def start(memory_api=True):
    tenancy = MemoryTenancy()
    a, b = tenancy.create_account("a@example.com"), tenancy.create_account("b@example.com")
    keys = {"key-a": a.account_id, "key-b": b.account_id}
    bus = EventBus()
    hub = BubbleHub(threads=InMemoryThreads(), bubbles=InMemoryBubbles(), publish=bus.publish,
                    known_users=frozenset(), spawn=lambda fn: fn())
    kernel = make_kernel("Réponse de Nokker.")
    kernel.memory = TenantMemory(internal=Notes("tn_interne"), tenant_memory=Notes)
    kernel.observer = hub
    hub.kernel = kernel
    hub.scrub = kernel.invariants.scrub_output
    handler = make_handler(kernel, keys, None, approvals=None, events=bus, hub=hub, tenancy=tenancy,
                           memory_search_api=memory_api)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    return _Server(httpd=httpd, thread=th, port=httpd.server_address[1]), a, b


async def _session(url, token, fn):
    env = {"NOKKER_TOKEN": token, "PYTHONPATH": str(ROOT), **({"PATH": os.environ["PATH"]} if "PATH" in os.environ
                                                             else {})}
    params = StdioServerParameters(command=sys.executable, args=["-m", "nokker", "mcp", "serve", "--url", url],
                                   env=env, cwd=str(ROOT))
    errlog = open(os.devnull, "w")  # noqa: ASYNC230, SIM115 — journal du sous-processus, jeté
    try:
        async with stdio_client(params, errlog=errlog) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            return await fn(session)
    finally:
        errlog.close()


def mcp(url, token, fn):
    return asyncio.run(asyncio.wait_for(_session(url, token, fn), timeout=60))


def _payload(result):
    assert not result.is_error, result.content
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


@pytest.fixture
def srv():
    s, a, b = start()
    yield s, a, b
    s.stop()


def test_tools_listed_and_send_read_list_round_trip(srv):
    s, a, _ = srv
    base = f"http://127.0.0.1:{s.port}"

    async def flow(session):
        names = sorted(t.name for t in (await session.list_tools()).tools)
        sent = _payload(await session.call_tool("nokker_thread_send", {"message": "Bonjour", "thread_id": "f1"}))
        listed = _payload(await session.call_tool("nokker_threads_list", {}))
        read = _payload(await session.call_tool("nokker_thread_read", {"thread_id": "f1"}))
        mem = _payload(await session.call_tool("nokker_memory_search", {"query": "budget"}))
        return names, sent, listed, read, mem

    names, sent, listed, read, mem = mcp(base, "key-a", flow)
    assert names == ["nokker_memory_search", "nokker_thread_read", "nokker_thread_send", "nokker_threads_list"]
    assert sent == {"thread_id": "f1", "reply": "Réponse de Nokker."}
    assert [t["id"] for t in listed["threads"]] == [f"{a.tenant_id}:f1"]  # forme de l'API : préfixé par le locataire
    assert [(m["role"], m["content"]) for m in read["messages"]] == [("user", "Bonjour"),
                                                                     ("assistant", "Réponse de Nokker.")]
    assert mem == {"results": [{"note": f"note-{a.tenant_id}", "text": f"budget chez {a.tenant_id}", "score": 0.9}]}


def test_accounts_are_isolated(srv):
    s, a, b = srv
    base = f"http://127.0.0.1:{s.port}"
    mcp(base, "key-a", lambda ses: ses.call_tool("nokker_thread_send", {"message": "secret de A", "thread_id": "fa"}))

    async def as_b(session):
        listed = _payload(await session.call_tool("nokker_threads_list", {}))
        read = await session.call_tool("nokker_thread_read", {"thread_id": "fa"})
        mem = _payload(await session.call_tool("nokker_memory_search", {"query": "budget"}))
        return listed, read, mem

    listed, read, mem = mcp(base, "key-b", as_b)
    assert listed == {"threads": []}
    assert read.is_error and "inconnu ou interdit" in read.content[0].text
    assert mem["results"][0]["note"] == f"note-{b.tenant_id}" and a.tenant_id not in json.dumps(mem)
    # même identifiant de fil chez B : un AUTRE fil (préfixé par son locataire), sans rien de A
    sent = mcp(base, "key-b", lambda ses: ses.call_tool("nokker_thread_send", {"message": "x", "thread_id": "fa"}))
    assert not sent.is_error
    st, one = call(s, "GET", "/v1/threads/fa", key="key-b")
    assert st == 200 and "secret de A" not in json.dumps(one)


def test_invalid_token_is_refused(srv):
    s, _, _ = srv

    async def flow(session):
        return [await session.call_tool(n, args) for n, args in (
            ("nokker_threads_list", {}), ("nokker_thread_send", {"message": "x"}),
            ("nokker_memory_search", {"query": "q"}))]

    for r in mcp(f"http://127.0.0.1:{s.port}", "faux-jeton", flow):
        assert r.is_error and "jeton refusé par Nokker (401)" in r.content[0].text


def test_memory_route_off_by_default_and_needs_identity():
    s, _, _ = start(memory_api=False)
    try:
        assert call(s, "POST", "/v1/memory/search", {"query": "q"}, key="key-a")[0] == 404
    finally:
        s.stop()
    s, _, _ = start()
    try:
        assert call(s, "POST", "/v1/memory/search", {"query": "q"}, key=None)[0] == 401
        assert call(s, "POST", "/v1/memory/search", {"query": ""}, key="key-a")[0] == 400
    finally:
        s.stop()


def test_token_never_on_the_command_line(tmp_path):
    from nokker.mcp_server import main, read_token

    assert main(["serve", "--token-env", "NOKKER_TEST_ABSENT_TOKEN"]) == 2
    f = tmp_path / "tok"
    f.write_text("abc\n")
    f.chmod(0o644)
    with pytest.raises(SystemExit):
        read_token("X", str(f))
    f.chmod(0o600)
    assert read_token("X", str(f)) == "abc"
