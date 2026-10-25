"""Écriture en direct (X22) : JSON partiel, caviardage avec retenue, débit, plafond, branchement noyau, provider, hub."""
from __future__ import annotations

import json

import httpx
import pytest

from nokker.core.live_input import LiveToolInputs, PartialObject
from nokker.core.ports import Completion
from nokker.core.turn import InputCheck, Message, ToolCall, ToolResult, Turn, TurnInput, Usage
from nokker.pages.store import PageStore
from nokker.pages.tools import PageToolSource, PagingToolSource
from nokker.qa.invariants import DeterministicInvariants
from nokker.qa.secrets import KnownSecrets
from tests.unit.api.test_stream import kernel_with
from tests.unit.bubbles.conftest import clock, hub, rec  # noqa: F401 — fixtures du hub
from tests.unit.test_pages import HTML, FakeCode, ctx

FAKE_SECRET = "Zq9-faux-" + "SECRET-de-test-7Hk2pWm"  # valeur inventée pour le test, jamais une vraie


def _invariants() -> DeterministicInvariants:
    known = KnownSecrets()
    known.add("TEST_FAUX_SECRET", FAKE_SECRET)
    return DeterministicInvariants(known=known)


def _pieces(text: str, step: int) -> list[str]:
    return [text[i:i + step] for i in range(0, len(text), step)]


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _live(scrub=lambda t: t, clock=None, **kw):
    events: list[dict] = []
    live = LiveToolInputs(scrub, events.append, kw.pop("holdback", 8), clock=clock or Clock(), **kw)
    return live, events


def _text(events, call_id="c1"):
    return "".join(e["text"] for e in events if e["call_id"] == call_id)


# ─────────────────────────────── JSON partiel ───────────────────────────────


@pytest.mark.parametrize("step", [1, 2, 3, 5, 64])
def test_partial_object_decodes_escapes_split_anywhere(step):
    content = 'ligne "un"\n\tdeux \\ é 😀 fin'
    raw = json.dumps({"mode": "x", "n": 3, "opts": {"a": [1, "}"]}, "path": "out/é.html", "content": content,
                      "ok": True})
    got: list[str] = []
    strings: dict[str, str] = {}
    po = PartialObject({"content"}, lambda k, p: got.append(p), lambda k, v: strings.__setitem__(k, v))
    for p in _pieces(raw, step):
        po.feed(p)
    assert "".join(got) == content
    assert strings == {"mode": "x", "path": "out/é.html"}
    assert po.state == "end"


def test_partial_object_ascii_escapes_and_broken_input():
    raw = json.dumps({"content": "é😀"}, ensure_ascii=True)  # é😀
    got: list[str] = []
    po = PartialObject({"content"}, lambda k, p: got.append(p), lambda k, v: None)
    for c in raw:
        po.feed(c)
    assert "".join(got) == "é😀"
    bad = PartialObject({"content"}, lambda k, p: None, lambda k, v: None)
    bad.feed("[1, 2]")
    assert bad.broken


# ─────────────────────────────── direct d'un appel ───────────────────────────────


@pytest.mark.parametrize("step", [1, 3, 7, 16, 41])
def test_secret_split_between_pieces_never_leaves(step):
    inv = _invariants()
    content = ("export const config = {\n" + "  option: 'valeur',\n" * 8 + f"  token: '{FAKE_SECRET}',\n"
               + "  autre: 'suite',\n" * 12 + "};\n")
    raw = json.dumps({"path": "src/config.ts", "content": content})
    clock = Clock()
    live, events = _live(inv.scrub_output, clock, holdback=100, every_s=0.0)
    for p in _pieces(raw, step):
        live.feed(0, "c1", "write_file", p)
        clock.t += 0.01
    before = _text(events)
    assert before and FAKE_SECRET not in before
    bare = before.replace("[secret:TEST_FAUX_SECRET]", "")
    for i in range(len(FAKE_SECRET) - 5):
        assert FAKE_SECRET[i:i + 6] not in bare
    live.finish()
    assert _text(events) == inv.scrub_output(content)  # la retenue sort à la fin, caviardée
    assert all(e["path"] == "src/config.ts" for e in events)
    assert all(e["field"] == "content" for e in events if e["text"])  # l'annonce du chemin précède le champ
    assert [e["seq"] for e in events] == list(range(len(events)))


@pytest.mark.parametrize("step", [1, 4, 13])
def test_spaced_secret_across_a_cut_never_leaves_in_part(step):
    """La variante « espacée » d'une valeur connue contient des blancs : une coupe peut tomber dedans. La fenêtre
    prolongée la caviarde encore : on attend au lieu d'émettre la moitié."""
    inv = _invariants()
    spaced = " ".join(FAKE_SECRET)
    content = "début du fichier, texte ordinaire\n" * 30 + f"clé = {spaced}\n" + "fin du fichier, texte\n" * 30
    live, events = _live(inv.scrub_output, holdback=100, every_s=0.0)
    for p in _pieces(json.dumps({"path": "a.txt", "content": content}), step):
        live.feed(0, "c1", "write_file", p)
    live.finish()
    out = _text(events).replace("[secret:TEST_FAUX_SECRET]", "").replace(" ", "")
    for i in range(len(FAKE_SECRET) - 5):
        assert FAKE_SECRET[i:i + 6] not in out
    assert "[secret:TEST_FAUX_SECRET]" in _text(events)


def test_rate_is_bounded_and_big_calls_are_cut():
    clock = Clock()
    live, events = _live(clock=clock, every_s=0.1, every_chars=2048, max_chars=20_000)
    raw = json.dumps({"path": "a.txt", "content": "abcdefghi\n" * 5000})
    for p in _pieces(raw, 10):  # 5 000 morceaux en 1 s
        live.feed(0, "c1", "write_file", p)
        clock.t += 0.0002
    live.finish()
    # ≤ 1 événement / 100 ms ou / 2 Ko, plus l'annonce du chemin et l'événement final.
    assert len(events) <= 20_000 // 2048 + 4
    assert events[-1].get("truncated") is True
    assert len(_text(events)) <= 20_000
    assert sum(1 for e in events if e.get("truncated")) == 1


def test_path_after_content_late_id_other_tools_and_page_create():
    live, events = _live(every_s=0.0)
    live.feed(0, "", "write_file", '{"content": "<p>un deux trois quatre cinq six</p>\\n", ')  # id pas encore là
    assert events == []
    live.feed(0, "c1", "write_file", '"path": "web/index.html"}')
    live.feed(1, "c2", "terminal", '{"command": "rm -rf /"}')  # pas un outil d'écriture : jamais en direct
    live.feed(2, "c3", "page_create", '{"path": "web/index.html", "title": "Démo"}')
    live.finish()
    assert {e["call_id"] for e in events} == {"c1", "c3"}
    assert _text(events, "c1") == "<p>un deux trois quatre cinq six</p>\n"
    assert events[-1]["path"] == "web/index.html"
    pc = [e for e in events if e["call_id"] == "c3"]
    assert len(pc) == 1 and pc[0]["text"] == "" and pc[0]["path"] == "web/index.html" and "field" not in pc[0]


def test_v4a_patch_follows_the_file_being_patched_and_discard():
    patch = ("*** Begin Patch\n*** Update File: a.py\n@@\n-x = 1\n+x = 2\n"
             "*** Add File: b.html\n+<p>nouveau fichier avec un peu de texte</p>\n*** End Patch\n")
    live, events = _live(every_s=0.0)
    for p in _pieces(json.dumps({"mode": "patch", "patch": patch}), 9):
        live.feed(0, "c1", "patch", p)
    live.finish()
    assert _text(events) == patch and events[-1]["field"] == "patch" and events[-1]["path"] == "b.html"
    assert any(e.get("path") == "a.py" for e in events)
    live2, events2 = _live(every_s=0.0)
    live2.feed(0, "c9", "write_file", '{"path": "x.txt", "content": "beaucoup de texte ici et encore "')
    live2.discard()
    assert events2[-1] == {"call_id": "c9", "name": "write_file", "seq": events2[-1]["seq"], "text": "",
                           "path": "x.txt", "field": "content", "discarded": True}


def test_unclosed_pem_never_leaves_even_at_finish():
    live, events = _live(every_s=0.0)
    body = "clé :\n-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n"
    live.feed(0, "c1", "write_file", json.dumps({"path": "k.pem", "content": body})[:-2])  # coupé avant la fin
    live.finish()
    assert "BEGIN" not in _text(events) and "MIIE" not in _text(events)


# ─────────────────────────────── noyau ───────────────────────────────


class ToolStreamingShip:
    """Provider qui streame aussi les arguments d'outils (`on_tool_delta`)."""

    def __init__(self, path: str, content: str) -> None:
        self.args = json.dumps({"path": path, "content": content})
        self.calls: list[bool] = []

    def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048, on_delta=None,
                 on_tool_delta=None):
        self.calls.append(on_tool_delta is not None)
        if len(self.calls) % 2 == 1:
            if on_tool_delta is not None:
                for p in _pieces(self.args, 5):
                    on_tool_delta(0, "call_w", "write_file", p)
            tc = ToolCall(id="call_w", name="write_file", arguments=json.loads(self.args))
            return Completion(message=Message(role="assistant", content="", tool_calls=[tc]),
                              usage=Usage(prompt=1, completion=1), model="m", latency_s=0.0)
        return Completion(message=Message(role="assistant", content="Fait."), usage=Usage(prompt=1, completion=1),
                          model="m", latency_s=0.0)


class Observer:
    def __init__(self) -> None:
        self.log: list[tuple[str, object]] = []

    def context_for(self, thread_id):
        return []

    def turn_started(self, turn):
        pass

    def tool_input_delta(self, turn, data):
        self.log.append(("delta", data))

    def tool_call(self, turn, call):
        self.log.append(("call", call.id))

    def tool_result(self, turn, call, result, ms):
        self.log.append(("result", call.id))

    def tool_executed(self, turn, call, spec, result):
        return None

    def turn_done(self, turn):
        self.log.append(("done", None))


class WriteTools:
    """Registre minimal : `write_file` noté (pas d'écriture tant que la boucle n'exécute pas l'outil)."""

    def __init__(self, observer: Observer, live: bool) -> None:
        self.observer, self.live = observer, live
        self.written: list[str] = []

    def core(self):
        from nokker.core.ports import ToolSpec

        return [ToolSpec(name="write_file", description="écrit", parameters={"type": "object"}, core=True)]

    def spec(self, name):
        return self.core()[0] if name == "write_file" else None

    def find(self, query, limit=5):
        return []

    def index(self):
        return []

    def call(self, name, arguments, ctx):
        assert any(k == "delta" for k, _ in self.observer.log) is self.live  # le direct est déjà parti…
        assert ("call", "call_w") in self.observer.log  # … mais rien n'est écrit avant l'exécution normale
        self.written.append(arguments["path"])
        return ToolResult(call_id="", name=name, content="ok")


class _Ctx:
    def assemble(self, turn, history, recall, loaded, tier):
        from nokker.core.ports import Assembled

        return Assembled(messages=[{"role": "user", "content": "x"}],
                         tools=[{"type": "function", "function": {"name": "write_file"}}], prefix_sha256="0")

    def admit_result(self, result):
        return result


class _Sink:
    def delta(self, text):
        pass

    def reset(self):
        pass


def _kernel(ship, observer, live=True):
    k = kernel_with(ship)
    k.context = _Ctx()
    k.tools = WriteTools(observer, live)
    k.observer = observer
    return k


def test_kernel_streams_tool_input_before_the_tool_runs_and_only_when_streamed():
    content = "<!doctype html>\n<h1>Bonjour</h1>\n" + "<p>ligne</p>\n" * 20
    ship = ToolStreamingShip("index.html", content)
    obs = Observer()
    k = _kernel(ship, obs)
    turn = k.run_turn(TurnInput(thread_id="t1", user_text="écris"), _Sink())
    assert turn.reply == "Fait." and k.tools.written == ["index.html"]
    kinds = [kind for kind, _ in obs.log]
    assert kinds.index("delta") < kinds.index("call") < kinds.index("result")
    deltas = [d for kind, d in obs.log if kind == "delta"]
    assert "".join(d["text"] for d in deltas) == content and deltas[0]["call_id"] == "call_w"
    # Sans flux client : chemin inchangé, pas de direct.
    obs2 = Observer()
    ship2 = ToolStreamingShip("index.html", content)
    k2 = _kernel(ship2, obs2, live=False)
    assert k2.run_turn(TurnInput(thread_id="t2", user_text="écris")).reply == "Fait."
    assert k2.tools.written == ["index.html"] and ship2.calls == [False, False] and not any(kind == "delta" for kind, _ in obs2.log)


def test_kernel_discards_live_input_when_the_stream_breaks():
    from nokker.core.ports import StreamInterrupted

    class Breaks(ToolStreamingShip):
        def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048, on_delta=None,
                     on_tool_delta=None):
            if on_tool_delta is not None:
                self.calls.append(True)
                for p in _pieces(self.args[:60], 5):
                    on_tool_delta(0, "call_x", "write_file", p)
                raise StreamInterrupted("coupé")
            return Completion(message=Message(role="assistant", content="Réponse."), usage=Usage(prompt=1, completion=1),
                              model="m", latency_s=0.0)

    obs = Observer()
    k = _kernel(Breaks("a.html", "<p>du texte qui ne sera jamais écrit sur le disque</p>"), obs)
    turn = k.run_turn(TurnInput(thread_id="t3", user_text="écris"), _Sink())
    deltas = [d for kind, d in obs.log if kind == "delta"]
    assert turn.reply == "Réponse." and deltas and deltas[-1].get("discarded") is True
    assert k.tools.written == []


# ─────────────────────────────── provider ───────────────────────────────


def test_sokkan_stream_relays_tool_argument_pieces_and_keeps_the_same_completion():
    from tests.unit.providers.test_sokkan_stream import TOOL_STREAM, _provider, _sse

    p = _provider(lambda req: httpx.Response(200, content=_sse(TOOL_STREAM)))
    seen: list[tuple[int, str, str, str]] = []
    comp = p.complete([{"role": "user", "content": "x"}], [], reasoning=False, session_key="s",
                      on_delta=lambda _t: None, on_tool_delta=lambda *a: seen.append(a))
    by_index: dict[int, str] = {}
    for idx, call_id, name, piece in seen:
        by_index[idx] = by_index.get(idx, "") + piece
    assert {i: json.loads(v) for i, v in by_index.items()} == {tc_i: tc.arguments
                                                             for tc_i, tc in enumerate(comp.message.tool_calls)}
    assert {(i, c, n) for i, c, n, _ in seen} == {(0, "call_a", "calendar_list"), (1, "call_b", "mail_search")}


def test_hedged_relays_tool_argument_pieces():
    from nokker.providers.hedged import HedgedProvider

    class P:
        model = "m"

        def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048, on_delta=None,
                     on_tool_delta=None):
            on_tool_delta(0, "c1", "write_file", '{"path": "a"}')
            return Completion(message=Message(role="assistant", content=""), usage=Usage(), model="m", latency_s=0.0)

    seen: list[tuple] = []
    HedgedProvider(primary=P(), fallback=P(), hedge_after_s=5.0).complete(
        [], [], reasoning=False, session_key="s", on_delta=lambda _t: None, on_tool_delta=lambda *a: seen.append(a))
    assert seen == [(0, "c1", "write_file", '{"path": "a"}')]


# ─────────────────────────────── hub et bulle `artifact` ───────────────────────────────


def test_hub_publishes_the_event_and_the_artifact_bubble_follows_the_write(hub, rec, tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "index.html").write_bytes(HTML)
    store = PageStore(tmp_path / "pages")
    hub.pages = store
    th = hub.create_thread("Démo", "alice")
    turn = Turn(thread_id=th.id, index=0, input=InputCheck(text="page"), sender="alice")
    pc = ToolCall(id="c1", name="page_create", arguments={"path": "index.html", "title": "Démo"})
    hub.tool_executed(turn, pc, None, PageToolSource(store, lambda _t: root).call("page_create", pc.arguments,
                                                                                ctx(th.id)))
    bubble = next(b for b in hub.bubbles.list_thread(th.id) if b.kind == "artifact")
    hub.turn_done(turn)

    turn = Turn(thread_id=th.id, index=1, input=InputCheck(text="change"), sender="alice")
    data = {"call_id": "c2", "name": "write_file", "seq": 0, "text": "<p>v2", "path": "/workspace/index.html",
            "field": "content"}
    hub.tool_input_delta(turn, data)
    hub.tool_input_delta(turn, {**data, "seq": 1, "text": "</p>"})
    ev = rec.of("tool.input.delta")
    assert ev[0]["thread_id"] == th.id and ev[0]["turn_id"] == f"{th.id}:1" and ev[0]["_audience"] == ["alice"]
    live = hub.bubbles.get(bubble.id)
    assert live.status == "working" and live.state["writing"]["call_id"] == "c2"
    assert live.state["version"] == 1 and "<p>" not in json.dumps(live.state)  # jamais le HTML dans l'état
    updates = [d for t, d in rec.events if t == "bubble.updated" and d["bubble"]["id"] == bubble.id]
    assert sum(1 for u in updates if "writing" in u["bubble"]["state"]) == 1  # une seule mise à jour par appel

    w = ToolCall(id="c2", name="write_file", arguments={"path": "index.html", "content": "<p>v2</p>"})
    paging = PagingToolSource(FakeCode(root), store, lambda _t: root)
    hub.tool_executed(turn, w, None, paging.call("write_file", w.arguments, ctx(th.id)))
    hub.tool_result(turn, w, ToolResult(call_id="c2", name="write_file", content="ok"), 3)
    after = hub.bubbles.get(bubble.id)
    assert "writing" not in after.state and after.state["version"] == 2  # recalée sur la version enregistrée
    hub.turn_done(turn)
    assert hub.bubbles.get(bubble.id).status == "done"


def test_hub_clears_the_writing_state_on_refusal_discard_and_turn_end(hub, tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "p.html").write_bytes(HTML)
    store = PageStore(tmp_path / "pages")
    hub.pages = store
    th = hub.create_thread("Démo", "alice")
    turn = Turn(thread_id=th.id, index=0, input=InputCheck(text="page"), sender="alice")
    pc = ToolCall(id="c1", name="page_create", arguments={"path": "p.html"})
    hub.tool_executed(turn, pc, None, PageToolSource(store, lambda _t: root).call("page_create", pc.arguments,
                                                                                ctx(th.id)))
    bid = next(b for b in hub.bubbles.list_thread(th.id) if b.kind == "artifact").id
    base = {"name": "write_file", "seq": 0, "text": "x", "path": "p.html", "field": "content"}
    # Refus (ou carte en attente) : le résultat arrive sans nouvelle version → recalage.
    hub.tool_input_delta(turn, {**base, "call_id": "c2"})
    hub.tool_result(turn, ToolCall(id="c2", name="write_file", arguments={}),
                    ToolResult(call_id="c2", name="write_file", content="REFUSÉ"), 1)
    assert "writing" not in hub.bubbles.get(bid).state
    # Abandon (flux coupé).
    hub.tool_input_delta(turn, {**base, "call_id": "c3"})
    hub.tool_input_delta(turn, {**base, "call_id": "c3", "seq": 1, "text": "", "discarded": True})
    assert "writing" not in hub.bubbles.get(bid).state
    # Jamais exécuté (tour annulé) : la fin du tour recale.
    hub.tool_input_delta(turn, {**base, "call_id": "c4"})
    hub.turn_done(turn)
    b = hub.bubbles.get(bid)
    assert "writing" not in b.state and b.status == "done" and b.state["version"] == 1
