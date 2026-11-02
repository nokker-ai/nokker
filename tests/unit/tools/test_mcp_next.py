"""Client MCP 2026-07-28 et MCP Apps (docs/MCP-NEXT.md), mesurés contre un VRAI serveur HTTP local
(`fake_mcp_server.py`) aux deux révisions : 2025-11-25 (poignée de main) et 2026-07-28 (sans état).

Drapeaux éteints : le fil et les sorties sont ceux d'avant (poignée de main seule, aucun `_meta`, aucun `ui://`
traité). Drapeaux posés : négociation avec repli, MRTR, Tasks, cache des listes, apps transmises au client seulement."""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from nokker.approvals.executor import ApprovalExecutor
from nokker.approvals.memory import InMemoryApprovals
from nokker.bubbles.hub import BubbleHub
from nokker.bubbles.memory import InMemoryBubbles
from nokker.context.engine import ZURICH, ContextEngineV0
from nokker.core.loop import Kernel
from nokker.core.ports import Completion, RouteDecision, ToolContext
from nokker.core.turn import Decision, Gate, Message, Tier, ToolCall, TurnInput, Usage, Verdict
from nokker.ledger.costs import InMemoryLedger
from nokker.qa.invariants import DeterministicInvariants
from nokker.store.pg import MemoryStore
from nokker.threads.memory import InMemoryThreads
from nokker.tools import mcp_next
from nokker.tools.mcp import McpToolSource
from nokker.tools.mcp_apps import APP_CHANNEL, AppRegistry
from nokker.tools.registry import Registry
from tests.unit.api.test_server import NoEscalator, NullMem
from tests.unit.bubbles.conftest import Recorder
from tests.unit.tools.fake_mcp_server import CLOCK_HTML, INLINE_HTML, LEGACY, MODERN, FakeMcpServer

CTX = ToolContext("t1", 0, "api", "alice")


@pytest.fixture
def legacy():
    with FakeMcpServer("legacy") as s:
        yield s


@pytest.fixture
def modern():
    with FakeMcpServer("modern") as s:
        yield s


def source(server, **kw) -> McpToolSource:
    return McpToolSource(name="fake", url=server.url, **kw)


# ─────────────────────────── drapeaux éteints : rien ne change ───────────────────────────


def test_flags_off_legacy_wire_and_outputs_unchanged(legacy):
    """Golden du chemin d'avant (base p2/train-0211-v2 23e3a29, rejoué à l'identique dans le rapport) : poignée de
    main, aucune clé `_meta` de version, aucun en-tête Mcp-Method, le bloc embarqué rendu « [EmbeddedResource] »,
    l'outil réservé à l'app VISIBLE (le client d'avant ne lit pas `_meta.ui`), aucune app."""
    src = source(legacy)
    try:
        assert [s.name for s in src.specs()] == ["echo", "clock", "clock_refresh", "inline_widget"]
        outs = [src.call(n, a, CTX) for n, a in (("echo", {"text": "hi"}), ("clock", {}), ("inline_widget", {}))]
    finally:
        src.close()
    assert [(r.content, r.is_error, r.app) for r in outs] == [
        ("echo: hi", False, None), ("Il est 12:00.", False, None), ("Widget prêt.\n[EmbeddedResource]", False, None)]
    assert legacy.methods() == ["initialize", "notifications/initialized", "tools/list", "tools/call", "tools/call",
                                "tools/call"]
    assert all(not (r["params"].get("_meta") or {}) and "mcp-method" not in r["headers"] for r in legacy.requests)
    init = legacy.requests[0]["params"]
    assert "extensions" not in (init.get("capabilities") or {})


def test_flags_off_old_client_cannot_reach_modern_only_server(modern):
    """Matrice de la spec : client « legacy » ↔ serveur « modern » seul = échec (le drapeau sert à ça)."""
    src = source(modern)
    try:
        r = src.call("echo", {"text": "x"}, CTX)
    finally:
        src.close()
    assert r.is_error and modern.methods() == ["initialize"]


# ─────────────────────────── négociation (mcp_modern) ───────────────────────────


def test_modern_client_falls_back_to_handshake_on_legacy_server(legacy):
    src = source(legacy, modern=True)
    try:
        assert src.call("echo", {"text": "hi"}, CTX).content == "echo: hi"
        assert src._io._session.protocol_version == LEGACY
    finally:
        src.close()
    assert legacy.methods()[:3] == ["server/discover", "initialize", "notifications/initialized"]


def test_modern_client_is_stateless_on_modern_server(modern):
    src = source(modern, modern=True)
    try:
        assert src.call("echo", {"text": "hi"}, CTX).content == "echo: hi"
        assert src._io._session.protocol_version == MODERN
    finally:
        src.close()
    assert "initialize" not in modern.methods() and modern.methods()[0] == "server/discover"
    call = next(r for r in modern.requests if r["method"] == "tools/call")
    meta = call["params"]["_meta"]
    assert meta["io.modelcontextprotocol/protocolVersion"] == MODERN
    assert call["headers"]["mcp-method"] == "tools/call" and call["headers"]["mcp-name"] == "echo"
    assert call["headers"]["mcp-protocol-version"] == MODERN
    caps = meta["io.modelcontextprotocol/clientCapabilities"]
    # Ni élicitation ni échantillonnage déclarés ; l'extension Tasks oui.
    assert "elicitation" not in caps and "sampling" not in caps
    assert "io.modelcontextprotocol/tasks" in caps["extensions"]
    assert "mcp-session-id" not in call["headers"]


def test_mrtr_request_state_rounds_are_driven(modern):
    src = source(modern, modern=True)
    try:
        r = src.call("slow_sum", {}, CTX)
    finally:
        src.close()
    assert (r.content, r.is_error) == ("somme : 3", False)
    states = [q["params"].get("requestState") for q in modern.requests if q["method"] == "tools/call"]
    assert states == [None, "r1", "r2"]


def test_mrtr_elicitation_is_refused_not_invented(modern):
    src = source(modern, modern=True)
    try:
        r = src.call("ask_name", {}, CTX)
    finally:
        src.close()
    assert r.is_error and r.content.startswith("erreur MCP fake/ask_name:")
    assert [q["method"] for q in modern.requests].count("tools/call") == 1  # aucune réponse inventée renvoyée


def test_task_is_polled_to_completion(modern):
    src = source(modern, modern=True)
    try:
        r = src.call("long_job", {}, CTX)
    finally:
        src.close()
    assert (r.content, r.is_error) == ("travail fini", False)
    gets = [q for q in modern.requests if q["method"] == "tasks/get"]
    assert len(gets) == 2 and gets[0]["headers"]["mcp-name"] == gets[0]["params"]["taskId"]


def test_task_still_running_after_wait_returns_short_note(modern):
    src = source(modern, modern=True, task_wait_s=0.3)
    try:
        r = src.call("stuck_job", {}, CTX)
    finally:
        src.close()
    assert r.content == mcp_next.TASK_PENDING.format(status="working") and not r.is_error


def test_apps_flag_alone_stays_on_handshake_era(modern):
    """`mcp_apps` sans `mcp_modern` : poignée de main seule, donc un serveur 2026-07-28 seul reste injoignable."""
    src = source(modern, apps=True)
    try:
        r = src.call("echo", {"text": "x"}, CTX)
    finally:
        src.close()
    assert r.is_error and set(modern.methods()) == {"initialize"}
    assert "initialize n'existe plus" in r.content


def test_tool_list_cached_per_ttl_with_floor(modern):
    now = [1000.0]
    src = source(modern, modern=True, clock=lambda: now[0], list_ttl_floor_s=60.0)
    try:
        src.specs(), src.specs()
        assert modern.list_calls == 1
        modern.ttl_ms = 0  # « aussitôt périmée » : le plancher protège
        now[0] += 119.0
        src.specs()
        assert modern.list_calls == 1
        now[0] += 2.0  # au-delà des 120 s annoncées
        src.specs()
        assert modern.list_calls == 2
        now[0] += 30.0  # ttl 0 → plancher 60 s
        src.specs()
        assert modern.list_calls == 2
        now[0] += 31.0
        src.specs()
        assert modern.list_calls == 3
    finally:
        src.close()


def test_legacy_list_kept_until_refresh_even_with_modern_flag(legacy):
    now = [0.0]
    src = source(legacy, modern=True, clock=lambda: now[0])
    try:
        src.specs()
        now[0] += 10_000
        src.specs()
        assert legacy.list_calls == 1
        src.refresh()
        src.specs()
        assert legacy.list_calls == 2
    finally:
        src.close()


# ─────────────────────────── MCP Apps (mcp_apps) ───────────────────────────


@pytest.mark.parametrize("era", ["legacy", "modern"])
def test_app_resource_goes_to_client_never_to_model(era):
    with FakeMcpServer(era) as server:
        src = source(server, modern=True, apps=True)
        try:
            names = [s.name for s in src.specs()]
            r = src.call("clock", {}, CTX)
            inline = src.call("inline_widget", {}, CTX)
            assert src.app_tools() == {"clock", "clock_refresh"}
        finally:
            src.close()
    assert "clock_refresh" not in names  # visibility ["app"] : hors de la surface du modèle
    assert r.content == "Il est 12:00.\n" + mcp_next.APP_SUMMARY.format(tool="clock")
    assert CLOCK_HTML not in r.content and "ui://" not in r.content
    app = r.app
    assert app["html"] == CLOCK_HTML and app["server"] == "fake" and app["tool"] == "clock"
    assert app["resource_uri"] == "ui://clock/app.html" and app["mime_type"] == "text/html;profile=mcp-app"
    assert app["sandbox"] == "allow-scripts" and app["sandbox_origin"] is None  # défaut : srcdoc, origine opaque
    assert app["allowed_messages"] == list(mcp_next.ALLOWED_APP_MESSAGES)
    assert "ui/update-model-context" not in app["allowed_messages"] and "ui/message" not in app["allowed_messages"]
    # CSP : domaines sains gardés, mot-clé et tentative d'injection écartés.
    assert app["csp_domains"] == {"connectDomains": ["https://api.example.com"],
                                  "resourceDomains": ["https://cdn.example.com"]}
    assert "'unsafe-eval'" not in app["csp"] and "script-src *" not in app["csp"]
    assert "connect-src 'self' https://api.example.com" in app["csp"] and "object-src 'none'" in app["csp"]
    assert app["permissions_requested"] == ["camera"] and app["prefers_border"] is True
    assert app["tool_result"]["structuredContent"] == {"time": "12:00"}
    assert inline.content == "Widget prêt.\n" + mcp_next.APP_SUMMARY.format(tool="inline_widget")
    assert inline.app["html"] == INLINE_HTML and inline.app["csp"] == mcp_next.DEFAULT_CSP
    assert all(b.get("type") != "resource" for b in inline.app["tool_result"]["content"])


def test_apps_only_flag_keeps_handshake(legacy):
    src = source(legacy, apps=True)
    try:
        r = src.call("clock", {}, CTX)
    finally:
        src.close()
    assert r.app is not None and legacy.methods()[0] == "initialize"
    init = legacy.requests[0]["params"]
    assert init["capabilities"]["extensions"] == {"io.modelcontextprotocol/ui":
                                                  {"mimeTypes": ["text/html;profile=mcp-app"]}}


def test_csp_sources_filter():
    assert mcp_next.csp_sources(["https://a.example.com", "https://*.cdn.example.org:8443", "http://x.org",
                                 "https://a.com;", "*", "'self'", 3]) == \
        ["https://a.example.com", "https://*.cdn.example.org:8443"]


def test_app_payload_refuses_wrong_mime_and_oversize():
    class R:
        uri, mime_type, text, meta = "ui://x", "text/html", "<p>", None

    assert mcp_next.app_payload(server="s", tool="t", resource=R(), visibility=["model"], arguments={},
                                result=None) is None
    R.mime_type, R.text = "text/html;profile=mcp-app", "x" * (mcp_next.APP_HTML_MAX_BYTES + 1)
    assert mcp_next.app_payload(server="s", tool="t", resource=R(), visibility=["model"], arguments={},
                                result=None) is None


# ─────────────────────────── noyau : tour, événement, portes ───────────────────────────


class Pol:
    def choose(self, state):
        return RouteDecision(tier=Tier.SHIP, reasoning=False, reason="t")


class Ship:
    def __init__(self):
        self.seen: list[list[dict]] = []

    def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048):
        self.seen.append(messages)
        if not any(m.get("role") == "tool" for m in messages):
            return Completion(message=Message("assistant", tool_calls=[ToolCall("c1", "clock", {})]),
                              usage=Usage(prompt=3), model="m", latency_s=0)
        return Completion(message=Message("assistant", content="voilà l'heure"), usage=Usage(prompt=5), model="m",
                          latency_s=0)


class CardForAppTool:
    """Portes de test : carte pour `clock_refresh`, le reste passe."""

    def action(self, call, spec, turn):
        if call.name == "clock_refresh":
            return [Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule="effect:write",
                            reason="test", strength="standard")]
        return []

    def switch(self, reply, turn, tier):
        return []


def _kernel(tmp_path, server, approvals=None):
    rec = Recorder()
    hub = BubbleHub(threads=InMemoryThreads(), bubbles=InMemoryBubbles(), publish=rec,
                    scrub=lambda s: s, known_users=frozenset({"alice"}),
                    spawn=lambda fn: fn())
    raw = source(server, modern=True, apps=True)
    reg = AppRegistry()
    reg.add_server("fake", raw, raw)
    tools = Registry(sources=[raw])
    prefix = tmp_path / "prefix.md"
    prefix.write_text("PRÉFIXE", encoding="utf-8")
    ctx = ContextEngineV0(tools=tools, prefix_path=str(prefix), now_fn=lambda: datetime(2026, 11, 1, tzinfo=ZURICH))
    ship = Ship()
    k = Kernel(store=MemoryStore(), context=ctx, memory=NullMem(), invariants=DeterministicInvariants(),
               gates=CardForAppTool(), escalator=NoEscalator(), policy=Pol(), providers={Tier.SHIP: ship},
               tools=tools, ledger=InMemoryLedger(), observer=hub, approvals=approvals)
    k.mcp_apps = reg
    hub.kernel = k
    return k, hub, rec, ship, raw


def test_turn_emits_tool_app_and_model_sees_only_summary(tmp_path, modern):
    k, hub, rec, ship, raw = _kernel(tmp_path, modern)
    try:
        t = hub.create_thread("Heure", "alice")
        turn = k.run_turn(TurnInput(t.id, "quelle heure ?", sender="alice"))
    finally:
        raw.close()
    assert turn.reply == "voilà l'heure"
    seen = json.dumps(ship.seen, ensure_ascii=False)
    assert CLOCK_HTML not in seen and "ui://" not in seen and "allow-scripts" not in seen
    tool_msg = next(m for m in ship.seen[-1] if m.get("role") == "tool")
    assert tool_msg["content"] == "Il est 12:00.\n" + mcp_next.APP_SUMMARY.format(tool="clock")
    assert "clock_refresh" not in seen  # outil réservé à l'app : jamais dans le catalogue du modèle
    types = [x for x in rec.types() if x.startswith("tool.")]
    assert types == ["tool.call", "tool.app", "tool.result"]
    [ev] = rec.of("tool.app")
    assert ev["call_id"] == "c1" and ev["thread_id"] == t.id and ev["turn_id"] == f"{t.id}:0"
    assert ev["app_id"].startswith("app_") and ev["html"] == CLOCK_HTML and ev["_audience"] == ["alice"]
    rec_app = k.mcp_apps.get(ev["app_id"])
    assert (rec_app.server, rec_app.tool, rec_app.thread_id) == ("fake", "clock", t.id)
    # Rien de l'app dans le tour persisté au-delà du résumé.
    assert CLOCK_HTML not in json.dumps([m.content for m in turn.messages], ensure_ascii=False)


def test_app_tool_call_goes_through_gates_card_then_executor_without_model(tmp_path, modern):
    approvals = InMemoryApprovals()
    k, hub, _rec, ship, raw = _kernel(tmp_path, modern, approvals=approvals)
    try:
        t = hub.create_thread("Heure", "alice")
        k.run_turn(TurnInput(t.id, "quelle heure ?", sender="alice"))
        calls_before = len(ship.seen)
        # Outil lisible par le modèle ET l'app : passe les portes, résultat rendu, rien vers le modèle.
        out = k.app_tool_call(t.id, "fake", "echo", {"text": "app"}, sender="alice")
        assert out.content == "echo: app" and out.app is None
        # Outil réservé à l'app, soumis à carte : rien n'est exécuté avant décision.
        card_id = k.app_tool_call(t.id, "fake", "clock_refresh", {}, sender="alice")
        assert isinstance(card_id, str)
        [card] = approvals.pending()
        assert card.channel == APP_CHANNEL and card.tool_name == "clock_refresh"
        assert [q["params"].get("name") for q in modern.requests if q["method"] == "tools/call"].count(
            "clock_refresh") == 0
        published = []
        ex = ApprovalExecutor(kernel=k, approvals=approvals,
                              on_direct=lambda a, content, err: published.append((a.id, content, err)))
        decided = approvals.decide_local(card.id, "approve", "alice")
        assert ex.run(decided) is None  # aucun tour relancé
        assert published == [(card.id, "Il est 12:01.", False)]
        assert approvals.get(card.id).status == "executed"
        assert len(ship.seen) == calls_before  # le modèle n'a pas été rappelé
        assert k._owner_notes == {}  # aucune note « hors tour » pour le prochain tour
    finally:
        raw.close()


def test_app_registry_refuses_tools_outside_app_visibility(modern):
    raw = source(modern, modern=True, apps=True)
    reg = AppRegistry()
    reg.add_server("fake", raw, raw)
    try:
        rec = reg.register(tenant="tn_a", thread_id="th_1", server="fake", tool="clock", call_id="c1")
        assert reg.allowed(rec, "clock_refresh") and reg.allowed(rec, "clock")
        assert not reg.allowed(rec, "echo") and not reg.allowed(rec, "slow_sum")
        other = reg.register(tenant="tn_a", thread_id="th_1", server="autre", tool="x", call_id="c2")
        assert not reg.allowed(other, "clock_refresh")  # jamais vers un autre serveur
    finally:
        raw.close()


def test_registry_bounded_by_count_and_age():
    now = [0.0]
    reg = AppRegistry(max_apps=2, max_age_s=10.0, clock=lambda: now[0])
    a = reg.register(tenant=None, thread_id="t", server="s", tool="x", call_id="1")
    b = reg.register(tenant=None, thread_id="t", server="s", tool="x", call_id="2")
    c = reg.register(tenant=None, thread_id="t", server="s", tool="x", call_id="3")
    assert reg.get(a.app_id) is None and reg.get(b.app_id) and reg.get(c.app_id)
    now[0] = 11.0
    assert reg.get(c.app_id) is None


def test_new_model_texts_are_free_of_guard_words():
    from pathlib import Path

    guard = Path(__file__).resolve().parents[2] / "guard-words.txt"
    words = [w.strip() for w in guard.read_text(encoding="utf-8").splitlines() if w.strip() and not w.startswith("#")] \
        if guard.exists() else []
    joined = " ".join([mcp_next.APP_SUMMARY, mcp_next.TASK_PENDING, mcp_next.TASK_FAILED, mcp_next.TASK_CANCELLED,
                       mcp_next.TASK_INPUT]).lower()
    assert not [w for w in words if w.lower() in joined]


# ─────────────────────────── route HTTP : cloisonnement ───────────────────────────


def test_app_route_tenant_member_and_visibility_checks(legacy):
    """`POST /v1/apps/{app_id}/tools/call` : app d'un AUTRE locataire = 404 (aucune fuite), non-membre du fil = 403,
    outil hors `visibility: app` = 403, appel permis = 200 avec le texte de l'outil ; route absente sans le drapeau."""
    from nokker.tenancy.context import INTERNAL_TENANT
    from tests.unit.api.test_bubbles_routes import call, start

    srv, hub, _ = start()
    raw = source(legacy, apps=True)
    try:
        assert call(srv, "POST", "/v1/apps/app_x/tools/call", {"name": "clock_refresh"})[0] == 404  # drapeau éteint
        reg = AppRegistry()
        reg.add_server("fake", raw, raw)
        hub.kernel.mcp_apps = reg
        st, t = call(srv, "POST", "/v1/threads", {"title": "Heure"})
        mine = reg.register(tenant=INTERNAL_TENANT, thread_id=t["id"], server="fake", tool="clock", call_id="c1")
        foreign = reg.register(tenant="tn_autre", thread_id=t["id"], server="fake", tool="clock", call_id="c2")
        path = f"/v1/apps/{mine.app_id}/tools/call"
        assert call(srv, "POST", f"/v1/apps/{foreign.app_id}/tools/call", {"name": "clock_refresh"})[0] == 404
        assert call(srv, "POST", "/v1/apps/app_inconnue/tools/call", {"name": "clock_refresh"})[0] == 404
        assert call(srv, "POST", path, {"name": "clock_refresh"}, key="key-bruno")[0] == 403
        assert call(srv, "POST", path, {"name": "echo", "arguments": {"text": "x"}})[0] == 403
        assert call(srv, "POST", path, {"name": "clock_refresh", "arguments": []})[0] == 400
        st, body = call(srv, "POST", path, {"name": "clock_refresh", "arguments": {}})
        assert st == 200 and body["content"] == "Il est 12:01." and body["is_error"] is False
        assert body["result"] == {"content": [{"type": "text", "text": "Il est 12:01."}], "isError": False}
        assert [q["params"].get("name") for q in legacy.requests if q["method"] == "tools/call"] == ["clock_refresh"]
    finally:
        raw.close()
        srv.stop()


# ─────────── bac à sable : « allow-scripts » seul sans domaine dédié (audit public, 02.11) ───────────


def test_sandbox_same_origin_only_with_a_dedicated_apps_origin(legacy):
    src = source(legacy, apps=True, apps_origin="https://apps.example-usercontent.net")
    try:
        app = src.call("clock", {}, CTX).app
    finally:
        src.close()
    assert app["sandbox"] == "allow-scripts allow-same-origin"
    assert app["sandbox_origin"] == "https://apps.example-usercontent.net"


@pytest.mark.parametrize("value, ok", [
    ("", True), ("https://apps.example-usercontent.net", True), ("https://apps.example-usercontent.net/", True),
    ("https://app.example.com", False),               # = l'origine de l'app
    ("https://app.example.com:443", False),
    ("https://APP.example.com", False),
    ("https://ui.app.example.com", False),            # sous-domaine de l'app
    ("https://example.com", False),                   # parent de l'app
    ("http://apps.example-usercontent.net", False),   # pas https
    ("https://apps.example-usercontent.net/x", False),  # un chemin n'est pas une origine
    ("apps.example-usercontent.net", False),
])
def test_apps_origin_checked_at_startup(value, ok):
    from nokker.api.server import build_kernel
    from nokker.config import NokkerConfig

    cfg = dict(pg_dsn_env="TEST_NOKKER_ABSENT_DSN_VAR", mcp_tool_servers=[], mcp_apps=True, mcp_apps_origin=value,
               app_url="https://app.example.com")
    if ok:
        assert mcp_next.check_apps_origin(value, ["https://app.example.com"]) == value.rstrip("/")
        build_kernel(NokkerConfig(**cfg))
    else:
        with pytest.raises(ValueError, match="mcp_apps_origin"):
            build_kernel(NokkerConfig(**cfg))
