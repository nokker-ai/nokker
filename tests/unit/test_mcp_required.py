"""`mcp_required` par session (docs/HOST-EMBED.md) : un serveur MCP indispensable injoignable fait ÉCHOUER le tour avant l'appel du
modèle (503 `mcp_required_unavailable`), au lieu de le laisser continuer sans ses outils."""
from __future__ import annotations

import json

import pytest

from nokker.api.server import declared_mcp_servers
from nokker.core import session_opts
from nokker.core.turn import Message, Tier, TurnInput
from nokker.tenancy.resources import TenantRegistry
from nokker.tools.registry import Registry, builtin_core_source
from tests.unit.api.host_api_support import Served, host_kernel
from tests.unit.test_loop_contract import kernel as scripted_kernel
from tests.unit.tools.test_registry import _mcp

THREADS = ("th_r", "th_r2", "th_other")


@pytest.fixture(autouse=True)
def _clean():
    reg = session_opts.mcp_required_registry()
    for t in THREADS:
        reg.clear(t)
    session_opts.configure_mcp_required_cache(0.0)  # chaque test sonde pour de vrai ; le cache a ses propres tests
    yield
    for t in THREADS:
        reg.clear(t)
    session_opts.configure_mcp_required_retry(2.0)
    session_opts.configure_mcp_required_cache(15.0)


def _kernel(sources, script):
    k, _tools, ship, _deep = scripted_kernel(script)
    k.tools = Registry(sources=[builtin_core_source(), *sources])
    return k, ship


def _fast_retry(seconds=0.2):
    session_opts.configure_mcp_required_retry(seconds)


# ── noyau : le tour échoue avant le modèle ─────────────────────────────────────────────────────────────────────────
def test_dead_required_server_fails_the_turn_before_the_model_is_called():
    _fast_retry()
    dead, _ = _mcp("metier", ("a",), ok=False)
    k, ship = _kernel([dead], [Message("assistant", "de tête")])
    session_opts.mcp_required_registry().set("th_r", ("metier",))
    turn = k.run_turn(TurnInput("th_r", "bonjour"))
    assert ship.seen == []  # le fournisseur n'est PAS appelé
    assert turn.failure["code"] == "mcp_required_unavailable" and turn.error == "mcp_required_unavailable"
    [only] = turn.failure["servers"]
    assert only["server"] == "metier" and only["reason"].startswith("ConnectionError")
    blob = json.dumps(turn.failure)
    assert "abc123" not in blob and "secret-host" not in blob and "Traceback" not in blob
    assert turn.reply == "" and turn.messages == [] and turn.calls == []  # pas de message assistant « de tête », pas de coût
    assert k.store.turns == []  # rien n'est rangé au fil : le message se renvoie
    assert [v.rule for v in turn.verdicts] == ["mcp:required_unavailable"] and k.ledger.verdicts == turn.verdicts
    assert turn.failure["person_message"] == "Un service indispensable n'est pas joignable pour le moment. Réessaie dans un instant."
    assert "metier" not in turn.failure["person_message"] and "metier" in turn.failure["message"]  # le nom reste côté hôte


def test_rechecked_at_every_turn_and_recovered_server_lets_the_turn_through():
    _fast_retry()
    dead, io = _mcp("metier", ("a",), ok=False)
    k, ship = _kernel([dead], [Message("assistant", "voilà")])
    session_opts.mcp_required_registry().set("th_r", ("metier",))
    assert k.run_turn(TurnInput("th_r", "un")).failure["code"] == "mcp_required_unavailable"
    assert k.run_turn(TurnInput("th_r", "deux")).failure["code"] == "mcp_required_unavailable"  # chaque tour
    io.ok = True  # rétabli
    turn = k.run_turn(TurnInput("th_r", "trois"))
    assert turn.failure is None and turn.reply == "voilà" and len(ship.seen) == 1


def test_a_server_that_goes_down_after_a_first_good_turn_is_detected():
    """Le cache de la liste d'outils ne masque pas la panne : le sondage relit la liste à chaque tour."""
    _fast_retry()
    src, io = _mcp("metier", ("a",), ok=True)
    k, ship = _kernel([src], [Message("assistant", "un"), Message("assistant", "deux")])
    session_opts.mcp_required_registry().set("th_r", ("metier",))
    assert k.run_turn(TurnInput("th_r", "q1")).reply == "un"
    io.ok = False
    assert k.run_turn(TurnInput("th_r", "q2")).failure["code"] == "mcp_required_unavailable"
    assert len(ship.seen) == 1


def test_one_short_retry_that_succeeds_is_not_a_failure():
    session_opts.configure_mcp_required_retry(2.0)
    src, io = _mcp("metier", ("a",), ok=False)
    first = io.session_async

    async def flaky():
        if io.calls >= 1:
            io.ok = True  # la première connexion a échoué, la nouvelle tentative passe
        return await first()

    io.session_async = flaky
    k, ship = _kernel([src], [Message("assistant", "ok")])
    session_opts.mcp_required_registry().set("th_r", ("metier",))
    turn = k.run_turn(TurnInput("th_r", "q"))
    assert turn.failure is None and turn.reply == "ok" and io.calls == 2


def test_retry_zero_means_a_single_attempt_and_retry_one_means_two():
    for retry, attempts in ((0.0, 1), (1.0, 2)):
        session_opts.configure_mcp_required_retry(retry)
        dead, io = _mcp("metier", ("a",), ok=False)
        k, _ship = _kernel([dead], [])
        session_opts.mcp_required_registry().set("th_r", ("metier",))
        assert k.run_turn(TurnInput("th_r", "q")).failure["code"] == "mcp_required_unavailable"
        assert io.calls == attempts


def test_the_retry_never_holds_the_turn_longer_than_its_delay():
    import asyncio
    import time

    session_opts.configure_mcp_required_retry(0.3)
    src, io = _mcp("metier", ("a",), ok=False)
    first = io.session_async

    async def slow():
        if io.calls >= 1:
            await asyncio.sleep(3)  # la nouvelle tentative s'éternise
        return await first()

    io.session_async = slow
    k, ship = _kernel([src], [])
    session_opts.mcp_required_registry().set("th_r", ("metier",))
    t0 = time.monotonic()
    turn = k.run_turn(TurnInput("th_r", "q"))
    assert turn.failure["code"] == "mcp_required_unavailable" and time.monotonic() - t0 < 2 and ship.seen == []


def test_an_unlisted_dead_server_keeps_the_old_behavior():
    _fast_retry()
    dead, _ = _mcp("panne", ("x",), ok=False)
    good, _ = _mcp("metier", ("a",))
    k, ship = _kernel([dead, good], [Message("assistant", "sans lui")])
    session_opts.mcp_required_registry().set("th_r", ("metier",))
    turn = k.run_turn(TurnInput("th_r", "q"))
    assert turn.failure is None and turn.reply == "sans lui" and [s for s, _w in k.tools.unavailable()] == []  # tour fini : état rendu


def test_no_option_or_empty_list_changes_nothing():
    dead, io = _mcp("metier", ("a",), ok=False)
    k, ship = _kernel([dead], [Message("assistant", "a"), Message("assistant", "b")])
    assert k.run_turn(TurnInput("th_r", "q")).reply == "a"
    assert session_opts.validate_mcp_required([]) == (None, None)
    session_opts.mcp_required_registry().set("th_r", session_opts.validate_mcp_required([])[0])
    assert k.run_turn(TurnInput("th_r", "q")).reply == "b" and io.calls >= 1


def test_several_required_servers_only_the_missing_one_is_listed():
    _fast_retry()
    good, _ = _mcp("bon", ("a",))
    dead, _ = _mcp("panne", ("b",), ok=False)
    dead2, _ = _mcp("panne2", ("c",), ok=False)
    k, ship = _kernel([good, dead, dead2], [])
    session_opts.mcp_required_registry().set("th_r", ("bon", "panne"))
    turn = k.run_turn(TurnInput("th_r", "q"))
    assert [s["server"] for s in turn.failure["servers"]] == ["panne"]  # panne2 n'est pas requis
    assert "panne2" not in turn.failure["message"] and "bon" not in turn.failure["message"]
    session_opts.mcp_required_registry().set("th_r", ("panne2", "panne", "bon"))
    turn = k.run_turn(TurnInput("th_r", "q"))
    assert [s["server"] for s in turn.failure["servers"]] == ["panne", "panne2"]


def test_a_required_name_with_no_source_is_unavailable_not_registered():
    _fast_retry()
    good, _ = _mcp("bon", ("a",))
    k, ship = _kernel([good], [Message("assistant", "ok")])
    session_opts.mcp_required_registry().set("th_r", ("bon", "fantome"))
    turn = k.run_turn(TurnInput("th_r", "q"))
    assert turn.failure["servers"] == [{"server": "fantome", "reason": "not_registered"}] and ship.seen == []


def test_a_successful_probe_is_kept_for_the_cache_window_but_a_failure_never_is():
    session_opts.configure_mcp_required_cache(15.0)
    _fast_retry(0.0)
    src, io = _mcp("metier", ("a",), ok=True)
    k, ship = _kernel([src], [Message("assistant", "un"), Message("assistant", "deux"), Message("assistant", "trois")])
    session_opts.mcp_required_registry().set("th_r", ("metier",))
    assert k.run_turn(TurnInput("th_r", "q1")).reply == "un"
    assert io.calls == 1
    assert k.run_turn(TurnInput("th_r", "q2")).reply == "deux"  # < 15 s : un seul tools/list réel
    assert io.calls == 1
    # un serveur tombé dans la fenêtre n'est vu qu'à l'expiration
    io.ok = False
    assert k.run_turn(TurnInput("th_r", "q3")).failure is None
    k.tools._probe_ok.clear()  # expiration
    assert k.run_turn(TurnInput("th_r", "q4")).failure["code"] == "mcp_required_unavailable"
    # un échec n'est jamais gardé : re-sondé à chaque tour, et rétabli aussitôt vu
    calls = io.calls
    assert k.run_turn(TurnInput("th_r", "q5")).failure["code"] == "mcp_required_unavailable" and io.calls > calls


def test_cache_expires_with_the_clock_and_zero_disables_it():
    import time

    src, io = _mcp("metier", ("a",), ok=True)
    reg = Registry(sources=[src])
    assert reg.required_unavailable(["metier"], 0.0, 15.0) == [] and io.calls == 1
    assert reg.required_unavailable(["metier"], 0.0, 15.0) == [] and io.calls == 1
    assert reg.required_unavailable(["metier"], 0.0, 0.0) == [] and io.calls == 2  # 0 = pas de cache
    reg._probe_ok[id(src)] = time.monotonic() - 16
    assert reg.required_unavailable(["metier"], 0.0, 15.0) == [] and io.calls == 3  # fenêtre passée


def test_cache_config_is_bounded():
    assert session_opts.parse_mcp_required_cache(None) == 15.0
    assert session_opts.parse_mcp_required_cache(0) == 0.0 and session_opts.parse_mcp_required_cache(60) == 60.0
    for bad in (-1, 60.5, "15", True, []):
        with pytest.raises(ValueError):
            session_opts.parse_mcp_required_cache(bad)


def test_delegate_child_probe_failure_is_a_tool_error_for_the_parent_who_continues():
    from nokker.core.ports import ToolSpec
    from nokker.core.turn import Effect, ToolCall, ToolResult
    from nokker.tools.delegate import DelegateToolSource
    from tests.unit.tools.test_delegate import FakeCode, Src

    _fast_retry(0.0)
    mcp, io = _mcp("metier", ("a",), ok=True)
    child_kernels = []

    def factory(registry, thread_id, observer):
        ck, _t, cship, _d = scripted_kernel([Message("assistant", "de tête")])
        ck.tools = Registry(sources=[builtin_core_source(), mcp])
        child_kernels.append(cship)
        io.ok = False  # le serveur tombe entre le début du tour parent (sondé) et le fils
        return ck

    code = FakeCode("/nonexistent")
    dele = DelegateToolSource(factory, code)
    dele.bind(Registry(sources=[Src("web_search"), code, dele]))

    class Parent:
        def specs(self):
            return [ToolSpec("lancer", "lance un fils", {}, Effect.READ)]

        def call(self, name, args, c):
            r = dele.call("delegate", {"tasks": [{"goal": "g"}], "isolate": False}, c)
            return ToolResult(call_id="", name=name, content=r.content, is_error=r.is_error)

    pk, _t, pship, _d = scripted_kernel([Message("assistant", tool_calls=[ToolCall(id="c1", name="lancer", arguments={})]),
                                         Message("assistant", "je continue sans lui")])
    pk.tools = Registry(sources=[builtin_core_source(), mcp, Parent()])
    session_opts.mcp_required_registry().set("th_r", ("metier",))
    turn = pk.run_turn(TurnInput("th_r", "délègue"))
    assert turn.failure is None and turn.reply == "je continue sans lui"  # le parent n'est pas tombé en 503
    tool_msg = next(m for m in turn.messages if m.role == "tool")
    [res] = json.loads(tool_msg.content)
    assert res["status"] == "failed" and res["exit_reason"] == "mcp_required_unavailable"
    assert res["servers"][0]["server"] == "metier" and "abc123" not in tool_msg.content and "secret-host" not in tool_msg.content
    assert child_kernels and child_kernels[0].seen == []  # le fils n'a pas appelé le modèle
    assert len(pship.seen) == 2  # le parent, lui, a parlé deux fois (outil puis conclusion)


@pytest.mark.parametrize("up", [True, False])
def test_delegate_with_restricted_tools_still_probes_the_child_through_the_mcp_source(up):
    """Les outils du serveur sont retirés au fils (`deny_tools` du parent), la SOURCE reste dans son registre : le sondage du fils
    réussit si le serveur répond, échoue en `mcp_required_unavailable` sinon, jamais en `not_registered`."""
    from nokker.core.ports import ToolSpec
    from nokker.core.turn import Effect, ToolCall, ToolResult
    from nokker.tools.delegate import DelegateToolSource
    from tests.unit.tools.test_delegate import FakeCode, Src

    _fast_retry(0.0)
    mcp, io = _mcp("metier", ("a",), ok=True)
    child_ship = []
    child_registries = []

    def factory(registry, thread_id, observer):
        ck, _t, cship, _d = scripted_kernel([Message("assistant", "fils ok")])
        ck.tools = registry  # le VRAI registre du fils, filtré
        child_ship.append(cship)
        child_registries.append(registry)
        io.ok = up  # l'état du serveur au moment où le fils se sonde
        return ck

    code = FakeCode("/nonexistent")
    dele = DelegateToolSource(factory, code)
    dele.thread_hidden = lambda thread: ("a",)  # `deny_tools` du parent : outils du serveur retirés au fils
    dele.bind(Registry(sources=[Src("web_search"), code, mcp, dele]))

    class Parent:
        def specs(self):
            return [ToolSpec("lancer", "lance un fils", {}, Effect.READ)]

        def call(self, name, args, c):
            r = dele.call("delegate", {"tasks": [{"goal": "g"}], "isolate": False}, c)
            return ToolResult(call_id="", name=name, content=r.content, is_error=r.is_error)

    pk, _t, _pship, _d = scripted_kernel([Message("assistant", tool_calls=[ToolCall(id="c1", name="lancer", arguments={})]),
                                          Message("assistant", "suite")])
    pk.tools = Registry(sources=[builtin_core_source(), mcp, Parent()])
    session_opts.mcp_required_registry().set("th_r", ("metier",))
    turn = pk.run_turn(TurnInput("th_r", "délègue"))
    assert turn.failure is None and turn.reply == "suite"
    [res] = json.loads(next(m for m in turn.messages if m.role == "tool").content)
    assert "a" not in {s.name for s in child_registries[0]._all()}  # le fils n'a pas les outils du serveur
    if up:
        assert res["status"] == "completed" and res["summary"] == "fils ok" and len(child_ship[0].seen) == 1
    else:
        assert res["exit_reason"] == "mcp_required_unavailable" and res["servers"][0]["reason"] != "not_registered"
        assert child_ship[0].seen == []


def test_the_option_of_one_session_does_not_touch_another_and_sub_agents_inherit():
    _fast_retry()
    dead, _ = _mcp("metier", ("a",), ok=False)
    k, ship = _kernel([dead], [Message("assistant", "libre")])
    session_opts.mcp_required_registry().set("th_r", ("metier",))
    assert k.run_turn(TurnInput("th_other", "q")).reply == "libre"
    assert k.run_turn(TurnInput("th_r/sa-abc", "q")).failure["code"] == "mcp_required_unavailable"


def test_the_failed_turn_reaches_the_observer_with_the_host_event():
    _fast_retry()
    dead, _ = _mcp("metier", ("a",), ok=False)
    k, ship = _kernel([dead], [])
    seen = []

    class Obs:
        def __getattr__(self, name):
            return lambda *a: seen.append((name, a))

    k.observer = Obs()
    session_opts.mcp_required_registry().set("th_r", ("metier",))
    k.run_turn(TurnInput("th_r", "q"))
    names = [n for n, _a in seen]
    assert "mcp_required_unavailable" in names and names[-1] == "turn_done" and "turn_started" in names
    [(_n, args)] = [x for x in seen if x[0] == "mcp_required_unavailable"]
    assert args[1]["servers"][0]["server"] == "metier"


# ── textes de la personne ──────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("lang", ["fr", "en", "de", "it"])
def test_person_text_exists_in_four_languages_without_address_or_token(lang):
    from nokker.core import person_texts as ptx

    text = ptx.pick(ptx.MCP_REQUIRED_UNAVAILABLE, lang)
    assert "http" not in text and "{" not in text and text.endswith(".")


# ── validation et API locale ───────────────────────────────────────────────────────────────────────────────────────
def test_validate_shapes():
    assert session_opts.validate_mcp_required(None) == (None, None)
    assert session_opts.validate_mcp_required(["a", "b", "a"]) == (("a", "b"), None)
    for bad in ("a", 1, {"a": 1}, [1], [""], ["a", None], [["a"]], True, ["x" * 200]):
        assert session_opts.validate_mcp_required(bad)[1] == session_opts.MCP_REQUIRED_ERROR


def test_retry_config_is_bounded():
    assert session_opts.parse_mcp_required_retry(None) == 2.0
    assert session_opts.parse_mcp_required_retry(0) == 0.0 and session_opts.parse_mcp_required_retry(10) == 10.0
    for bad in (-1, 10.5, "2", True, []):
        with pytest.raises(ValueError):
            session_opts.parse_mcp_required_retry(bad)


def _served(tmp_path, sources, script):
    k, _ship = host_kernel(script)
    k.tools = TenantRegistry(Registry(sources=[builtin_core_source(), *sources]), [])
    s = Served(tmp_path, k)
    s.api.mcp_server_names = lambda thread: declared_mcp_servers(k, thread)
    return s, k


def test_open_validates_names_and_remembers_clears_and_deletes(tmp_path):
    good, _ = _mcp("metier", ("a",))
    s, k = _served(tmp_path, [good], [])
    try:
        status, body = s.session(thread_id="th_r", mcp_required="metier")
        assert status == 400 and body["error"]["code"] == "bad_mcp_required"
        status, body = s.session(thread_id="th_r", mcp_required=[1])
        assert status == 400 and body["error"]["code"] == "bad_mcp_required" and s.api.code.sessions() == {}
        status, body = s.session(thread_id="th_r", mcp_required=["metier", "inconnu"])
        assert status == 400 and body["error"]["code"] == "unknown_mcp_server" and body["error"]["valid"] == ["metier"]
        assert "inconnu" in body["error"]["message"] and s.api.code.sessions() == {}
        assert session_opts.mcp_required("th_r") == ()
        status, body = s.session(thread_id="th_r", mcp_required=["metier"])
        assert status == 201 and body["mcp_required"] == ["metier"] and session_opts.mcp_required("th_r") == ("metier",)
        assert session_opts.mcp_required("th_r/sa-x") == ("metier",)
        status, body = s.session(thread_id="th_r")  # réouverture sans le champ : effacé
        assert status == 201 and "mcp_required" not in body and session_opts.mcp_required("th_r") == ()
        s.session(thread_id="th_r", mcp_required=["metier"])
        s.session(thread_id="th_r2", mcp_required=["metier"])
        assert s.req("/v1/local/sessions/th_r", method="DELETE")[0] == 200  # DELETE : effacé, l'autre session intacte
        assert session_opts.mcp_required("th_r") == () and session_opts.mcp_required("th_r2") == ("metier",)
        status, body = s.session(thread_id="th_r2", mcp_required=[])  # liste vide : aucune option
        assert status == 201 and "mcp_required" not in body and session_opts.mcp_required("th_r2") == ()
    finally:
        s.close()


def test_a_kernel_with_no_declared_servers_refuses_any_name(tmp_path):
    k, _ship = host_kernel([])
    s = Served(tmp_path, k)
    try:
        status, body = s.session(thread_id="th_r", mcp_required=["metier"])
        assert status == 400 and body["error"]["code"] == "unknown_mcp_server" and body["error"]["valid"] == []
    finally:
        s.close()


# ── surfaces HTTP ──────────────────────────────────────────────────────────────────────────────────────────────────
def test_chat_completions_503_before_the_model_then_recovers(tmp_path):
    _fast_retry()
    dead, io = _mcp("metier", ("a",), ok=False)
    s, k = _served(tmp_path, [dead], [Message("assistant", "voilà")])
    try:
        assert s.session(thread_id="th_r", mcp_required=["metier"])[0] == 201
        status, out = s.chat("th_r", "bonjour")
        assert status == 503
        err = out["error"]
        assert err["code"] == "mcp_required_unavailable" and "choices" not in out
        assert [x["server"] for x in err["servers"]] == ["metier"] and err["servers"][0]["reason"].startswith("ConnectionError")
        assert "abc123" not in json.dumps(out) and "secret-host" not in json.dumps(out)
        assert k.providers[Tier.SHIP].seen == [] and k.store.turns == []  # le modèle n'a pas été appelé
        ev = s.events("mcp.required_unavailable")
        assert len(ev) == 1 and ev[0][1]["servers"][0]["server"] == "metier"
        done = s.events("turn.done")
        assert done[-1][1]["error"]["code"] == "mcp_required_unavailable"  # la bulle d'erreur
        assert done[-1][1]["error"]["message"] == "Un service indispensable n'est pas joignable pour le moment. Réessaie dans un instant."
        assert "metier" not in done[-1][1]["error"]["message"] and "metier" in err["message"]  # nom : API hôte seulement
        io.ok = True  # rétabli au tour suivant
        status, out = s.chat("th_r", "bonjour")
        assert status == 200 and out["choices"][0]["message"]["content"] == "voilà"
    finally:
        s.close()


@pytest.mark.parametrize("tokens", [True, False])
def test_streamed_turn_sends_a_clean_error_event_before_any_delta(tmp_path, tokens):
    _fast_retry()
    dead, _ = _mcp("metier", ("a",), ok=False)
    s, k = _served(tmp_path, [dead], [Message("assistant", "voilà")])
    try:
        s.session(thread_id="th_r", mcp_required=["metier"])
        headers = {"X-Session-Id": "th_r", **({"X-Nokker-Stream-Tokens": "1"} if tokens else {})}
        status, raw = s.req("/v1/chat/completions", headers=headers, raw=True,
                            body={"messages": [{"role": "user", "content": "q"}], "stream": True})
        assert status == 200  # en-têtes SSE déjà partis : l'erreur est un événement
        lines = [ln for ln in raw.decode().splitlines() if ln.startswith("data:")]
        objs = [json.loads(ln[5:]) for ln in lines if ln[5:].strip() != "[DONE]"]
        assert "[DONE]" not in raw.decode() and objs[-1]["error"]["code"] == "mcp_required_unavailable"
        assert objs[-1]["error"]["servers"][0]["server"] == "metier"
        for o in objs[:-1]:  # au plus le chunk de rôle : jamais un delta de contenu
            assert not (o["choices"][0]["delta"].get("content"))
        assert "Traceback" not in raw.decode() and k.providers[Tier.SHIP].seen == []
    finally:
        s.close()


def test_session_without_the_option_is_served_as_before_even_with_a_dead_server(tmp_path):
    dead, _ = _mcp("metier", ("a",), ok=False)
    s, k = _served(tmp_path, [dead], [Message("assistant", "sans lui")])
    try:
        s.session(thread_id="th_r")
        status, out = s.chat("th_r", "q")
        assert status == 200 and out["choices"][0]["message"]["content"] == "sans lui"
    finally:
        s.close()
