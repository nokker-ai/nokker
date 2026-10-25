"""Pages créées en session et bulle `artifact` (docs/BUBBLES.md §10)."""
from __future__ import annotations

import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from nokker.bubbles.producers import Producers
from nokker.core.ports import ToolContext
from nokker.core.turn import InputCheck, ToolCall, ToolResult, Turn
from nokker.pages.store import MAX_BYTES, PageError, PageStore
from nokker.pages.tools import PageToolSource, PagingToolSource, page_states
from tests.unit.bubbles.conftest import clock, hub, rec  # noqa: F401 — fixtures du hub

HTML = b"<!doctype html><title>t</title><p>v1</p>"


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


class FakeCode:
    """write_file/patch d'une source de code : écrit vraiment dans le dossier (chemins relatifs ou sous /workspace)."""

    mount = "/workspace"

    def __init__(self, root: Path):
        self.root = root

    def specs(self):
        return []

    def call(self, name, arguments, ctx):
        p = str(arguments.get("path") or "")
        p = p[len(self.mount) + 1:] if p.startswith(self.mount + "/") else p
        (self.root / p).write_text(str(arguments.get("content") or arguments.get("new_string") or ""))
        return ToolResult(call_id="", name=name, content='{"bytes_written": 1}')


def ctx(thread="th_a"):
    return ToolContext(thread_id=thread, turn_index=0, channel="cli")


def test_store_versions_ids_and_limits(tmp_path):
    s = PageStore(tmp_path / "pages", clock=Clock())
    m = s.create("th_a", "out/index.html", "Rapport", HTML)
    assert m.page_id.startswith("pg_") and len(m.page_id) == 19 and m.version == 1 and not m.public
    assert s.create("th_a", "out/index.html", "", HTML).page_id == m.page_id  # une page par (fil, chemin)
    assert s.snapshot(m.page_id, HTML).version == 1  # même contenu : pas de version
    assert s.snapshot(m.page_id, b"<p>v2</p>").version == 2
    assert s.read(m.page_id)[1] == b"<p>v2</p>" and s.read(m.page_id, 1)[1] == HTML and s.read(m.page_id, 3) is None
    assert s.create("th_b", "out/index.html", "", HTML).page_id != m.page_id
    with pytest.raises(PageError):
        s.create("th_a", "logo.svg", "", b"<svg/>")
    with pytest.raises(PageError):
        s.create("th_a", "gros.html", "", b"x" * (MAX_BYTES + 1))
    again = PageStore(tmp_path / "pages")  # relu du disque
    assert again.get(m.page_id).version == 2 and again.by_path("th_a", "out/index.html").page_id == m.page_id
    assert "content" not in m.state() and set(m.state()) >= {"page_id", "path", "title", "version", "public"}


def test_signed_link_expires_and_is_bound_to_the_page(tmp_path):
    clock = Clock()
    s = PageStore(tmp_path / "pages", clock=clock)
    a, b = s.create("th_a", "a.html", "", HTML), s.create("th_a", "b.html", "", HTML)
    url, _ = s.link(a.page_id)
    exp, sig = url.split("exp=")[1].split("&sig=")
    assert s.verify(a.page_id, exp, sig) and not s.verify(b.page_id, exp, sig)
    clock.t += 3601
    assert not s.verify(a.page_id, exp, sig)


def test_page_create_then_writes_follow_the_file(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "index.html").write_bytes(HTML)
    store = PageStore(tmp_path / "pages")
    tools = PageToolSource(store, lambda _t: root)
    r = tools.call("page_create", {"path": "/workspace/index.html", "title": "Démo"}, ctx())
    assert not r.is_error and page_states(r.content)[0]["version"] == 1
    assert tools.call("page_create", {"path": "../hors.html"}, ctx()).is_error
    assert tools.call("page_create", {"path": "absent.html"}, ctx()).is_error
    paging = PagingToolSource(FakeCode(root), store, lambda _t: root)
    w = paging.call("write_file", {"path": "index.html", "content": "<p>v2</p>"}, ctx())
    assert page_states(w.content)[0]["version"] == 2
    other = paging.call("write_file", {"path": "notes.txt", "content": "x"}, ctx())
    assert page_states(other.content) == []  # pas une page
    assert page_states(paging.call("write_file", {"path": "index.html", "content": "<p>v3</p>"}, ctx("th_b")).content) == []


def test_producer_and_hub_keep_one_bubble_per_page(hub, tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "index.html").write_bytes(HTML)
    store = PageStore(tmp_path / "pages")
    hub.producers = Producers()
    hub.pages = store
    th = hub.create_thread("Démo", "alice")
    turn = Turn(thread_id=th.id, index=0, input=InputCheck(text="fais une page"), sender="alice")
    tools = PageToolSource(store, lambda _t: root)
    paging = PagingToolSource(FakeCode(root), store, lambda _t: root)
    call = ToolCall(id="c1", name="page_create", arguments={"path": "index.html", "title": "Démo"})
    hub.tool_executed(turn, call, None, tools.call("page_create", call.arguments, ctx(th.id)))
    w = ToolCall(id="c2", name="write_file", arguments={"path": "index.html", "content": "<p>v2</p>"})
    hub.tool_executed(turn, w, None, paging.call("write_file", w.arguments, ctx(th.id)))
    bubbles = [b for b in hub.bubbles.list_thread(th.id) if b.kind == "artifact"]
    assert len(bubbles) == 1 and bubbles[0].state["version"] == 2 and bubbles[0].status == "working"
    assert "<p>" not in str(bubbles[0].state)
    hub.turn_done(turn)
    assert hub.bubbles.get(bubbles[0].id).status == "done"
    dest = hub.create_thread("Autre", "alice")
    hub.transfer(bubbles[0].id, "alice", to_thread=dest.id, mode="move")
    assert store.get(bubbles[0].state["page_id"]).thread_id == dest.id


def test_client_cannot_create_an_artifact(hub):
    from nokker.bubbles.types import BubbleError

    th = hub.create_thread("Démo", "alice")
    with pytest.raises(BubbleError):
        hub.create_bubble(th.id, "alice", kind="artifact", title="x", state={"page_id": "pg_0000000000000000"})


def _get(port, path, token=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 headers={"Authorization": f"Bearer {token}"} if token else {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def test_routes_serve_private_pages_with_sandbox_headers(tmp_path):
    from nokker.local.api import LOCAL_OWNER
    from nokker.api.server import make_handler
    from tests.unit.api.test_approvals_routes import make_kernel_with_approvals
    from nokker.approvals.memory import InMemoryApprovals
    from tests.unit.test_local_api import _api, _req

    approvals = InMemoryApprovals()
    kernel, _ = make_kernel_with_approvals(approvals)
    api = _api(tmp_path)
    api.state_dir = tmp_path / "state"  # jamais le vrai ~/.config/nokker
    meta = api.pages.create("th_a", "index.html", "Démo", HTML)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(kernel, {api.token: LOCAL_OWNER}, approvals=approvals,
                                                               local_api=api))
    port = httpd.server_address[1]
    api.allowed_hosts = frozenset({f"127.0.0.1:{port}"})
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        assert _get(port, f"/v1/pages/{meta.page_id}")[0] == 404  # privée : n'existe pas pour un inconnu
        status, headers, body = _get(port, f"/v1/pages/{meta.page_id}", api.token)
        assert status == 200 and body == HTML
        assert headers["Content-Security-Policy"].startswith("sandbox") and "allow-same-origin" not in \
            headers["Content-Security-Policy"] and headers["X-Content-Type-Options"] == "nosniff"
        assert _req(port, f"/v1/pages/{meta.page_id}/link", body={})[0] == 401
        status, link = _req(port, f"/v1/pages/{meta.page_id}/link", token=api.token, body={})
        assert status == 200 and _get(port, link["url"])[2] == HTML  # l'iframe, sans en-tête
        assert _get(port, link["url"].replace("sig=", "sig=0"))[0] == 404
        status, dl = _req(port, f"/v1/pages/{meta.page_id}/link", token=api.token, body={"download": True})
        status2, headers2, body2 = _get(port, dl["url"])
        assert status2 == 200 and body2 == HTML and headers2["Content-Disposition"].startswith('attachment; filename="')
        assert ".html" in headers2["Content-Disposition"]
        assert _get(port, dl["url"].replace("&dl=1", ""))[0] == 404  # la signature couvre le téléchargement
        assert _get(port, link["url"].replace("&sig=", "&dl=1&sig="))[0] == 404
        assert "Content-Disposition" not in _get(port, link["url"])[1]
        assert "public_url" not in link  # privée : pas de lien à copier
        api.pages.update(meta.page_id, public=True)
        assert _get(port, f"/v1/pages/{meta.page_id}")[0] == 200
        api.setup.state["relay_host"] = "atelier.nokker.app"
        pub = _req(port, f"/v1/pages/{meta.page_id}/link", token=api.token, body={})[1]
        assert pub["public_url"] == f"https://atelier--pages.nokker.app/{meta.page_id}"
        assert _get(port, "/v1/pages/pg_zzzz")[0] == 404
    finally:
        httpd.shutdown()


def test_pages_are_off_until_clients_know_the_kind(tmp_path):
    """Recette du 24.10 : la CLI des releases rc.5 plante sur un kind de bulle inconnu → pas de `page_create` par défaut."""
    from tests.unit.test_local_api import _api

    api = _api(tmp_path)
    api.state_dir = tmp_path / "state"
    names = {s.name for src in api.tool_sources() for s in src.specs()}
    assert "page_create" not in names
    api.pages_enabled = True
    assert "page_create" in {s.name for src in api.tool_sources() for s in src.specs()}


def test_relayed_page_needs_the_tunnel_secret(tmp_path):
    """Par la passerelle : le relais dit le compte (cookie vérifié) ; cru seulement avec le secret du tunnel."""
    import urllib.request as ur

    from nokker.api.server import make_handler
    from nokker.approvals.memory import InMemoryApprovals
    from nokker.local.api import LOCAL_OWNER
    from tests.unit.api.test_approvals_routes import make_kernel_with_approvals
    from tests.unit.test_local_api import _api

    approvals = InMemoryApprovals()
    kernel, _ = make_kernel_with_approvals(approvals)
    api = _api(tmp_path)
    api.state_dir = tmp_path / "state"
    api.tunnel = type("T", (), {"secret": "s3cr3t-tunnel"})()
    meta = api.pages.create("th_a", "index.html", "Démo", HTML)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(kernel, {api.token: LOCAL_OWNER}, approvals=approvals,
                                                               local_api=api))
    port = httpd.server_address[1]
    api.allowed_hosts = frozenset({f"127.0.0.1:{port}"})
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def get(headers):
        req = ur.Request(f"http://127.0.0.1:{port}/v1/pages/{meta.page_id}", headers=headers)
        try:
            with ur.urlopen(req, timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    try:
        relay = {"X-Nokker-Relay": "1", "X-Nokker-Pages": "1", "X-Nokker-Account": "tn_pierre"}
        assert get(relay) == 404  # imitation locale des en-têtes du relais
        assert get({**relay, "X-Nokker-Tunnel": "faux"}) == 404
        assert get({**relay, "X-Nokker-Tunnel": "s3cr3t-tunnel"}) == 200
        assert get({"X-Nokker-Relay": "1", "X-Nokker-Pages": "1", "X-Nokker-Tunnel": "s3cr3t-tunnel"}) == 404  # sans compte
    finally:
        httpd.shutdown()
