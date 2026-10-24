"""Le noyau avec le hub branché en observateur : contexte reçu injecté au prochain tour (et rejoué par
l'historique), événements turn.*/tool.*/thread.updated, producteurs appelés seulement sur exécution réelle."""
from __future__ import annotations

import json
from datetime import datetime

from nokker.bubbles.hub import BubbleHub
from nokker.bubbles.memory import InMemoryBubbles
from nokker.context.engine import ZURICH, ContextEngineV0
from nokker.core.loop import Kernel
from nokker.core.ports import Completion, RouteDecision, ToolSpec
from nokker.core.turn import Effect, Message, Tier, ToolCall, ToolResult, TurnInput, Usage
from nokker.ledger.costs import InMemoryLedger
from nokker.store.pg import MemoryStore
from nokker.threads.memory import InMemoryThreads
from tests.unit.api.test_server import AllowGates, AllowInvariants, NoEscalator, NullMem
from tests.unit.bubbles.conftest import Recorder, scrub


class Tools:
    def __init__(self):
        self.specs = {"web_search": ToolSpec("web_search", "web", {}, Effect.READ)}
        self.calls = 0
        self.pad = ""
        self.url_by_query: dict = {}

    def core(self):
        return list(self.specs.values())

    def spec(self, n):
        return self.specs.get(n)

    def find(self, q, limit=5):
        return []

    def index(self):
        return []

    def call(self, n, a, ctx):
        self.calls += 1
        payload = {"query": a.get("query"), "engines_queried": ["exa"],
                   "results": [{"url": self.url_by_query.get(a.get("query"), "https://a.org"), "title": "A",
                               "snippet": "", "domain": "a.org"}]}
        return ToolResult("", n, f"{self.pad}1. A\n<!-- nokker:web_search:data -->\n{json.dumps(payload)}\n"
                                 "<!-- /nokker:web_search:data -->")


class Pol:
    def choose(self, state):
        return RouteDecision(tier=Tier.SHIP, reasoning=False, reason="t")


class Ship:
    """1er appel du tour : web_search deux fois avec les MÊMES arguments (le 2e est un rejeu du mémo READ) ;
    ensuite : répond. Garde les messages reçus pour inspection."""

    def __init__(self, search=True):
        self.seen: list[list[dict]] = []
        self.search = search

    def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048):
        self.seen.append(messages)
        if self.search and not any(m.get("role") == "tool" for m in messages[-3:]):
            tcs = [ToolCall("c1", "web_search", {"query": "lvm"}), ToolCall("c2", "web_search", {"query": "lvm"})]
            return Completion(message=Message("assistant", tool_calls=tcs), usage=Usage(prompt=3), model="m",
                              latency_s=0)
        return Completion(message=Message("assistant", content="voilà"), usage=Usage(prompt=7, completion=2),
                          model="m", latency_s=0)


def _setup(tmp_path, search=True):
    rec = Recorder()
    hub = BubbleHub(threads=InMemoryThreads(), bubbles=InMemoryBubbles(), publish=rec, scrub=scrub,
                    known_users=frozenset({"alice"}), spawn=lambda fn: fn())
    prefix = tmp_path / "prefix.md"
    prefix.write_text("PRÉFIXE", encoding="utf-8")
    tools = Tools()
    ctx = ContextEngineV0(tools=tools, prefix_path=str(prefix), now_fn=lambda: datetime(2026, 10, 18, tzinfo=ZURICH))
    ship = Ship(search)
    k = Kernel(store=MemoryStore(), context=ctx, memory=NullMem(), invariants=AllowInvariants(), gates=AllowGates(),
               escalator=NoEscalator(), policy=Pol(), providers={Tier.SHIP: ship}, tools=tools,
               ledger=InMemoryLedger(), observer=hub)
    hub.kernel = k
    return hub, k, ship, rec, tools


def test_turn_events_and_thread_working_flag(tmp_path):
    hub, k, ship, rec, _ = _setup(tmp_path)
    t = hub.create_thread("Infra", "alice")
    rec.events.clear()
    k.run_turn(TurnInput(t.id, "cherche lvm", sender="alice"))
    types = [x for x in rec.types() if x != "bubble.created"]
    assert types[:2] == ["turn.started", "thread.updated"]
    assert types[-3:] == ["turn.delta", "turn.done", "thread.updated"]
    assert types.count("tool.call") == 2 and types.count("tool.result") == 2
    updates = [d["thread"]["working"] for d in rec.of("thread.updated")]
    assert updates[-2:] == [True, False]
    done = rec.of("turn.done")[0]
    assert done["turn_id"] == f"{t.id}:0" and done["tier"] == "ship" and done["usage"]["prompt_tokens"] == 10
    assert rec.of("turn.delta")[0]["text"] == "voilà"
    assert all(d["_audience"] == ["alice"] for _, d in rec.events)
    assert hub.threads.get(t.id).last_snippet == "voilà"


def test_search_bubble_produced_once_even_with_cached_replay(tmp_path):
    hub, k, ship, rec, tools = _setup(tmp_path)
    t = hub.create_thread("Infra", "alice")
    turn = k.run_turn(TurnInput(t.id, "cherche lvm", sender="alice"))
    assert tools.calls == 1
    created = rec.of("bubble.created")
    assert len(created) == 1 and created[0]["bubble"]["kind"] == "search"
    bid = created[0]["bubble"]["id"]
    tool_msgs = [m.content for m in turn.messages if m.role == "tool"]
    assert f"[bulle {bid} (search)" in tool_msgs[0]


def test_search_bubble_produced_even_when_result_truncated_by_reservoir(tmp_path):
    """Canari 18.10 : un web_search réel dépasse `reservoir_char_limit` ; tronqué AVANT le producteur, il
    perdait son bloc de données et aucune bulle ne naissait."""
    hub, k, ship, rec, tools = _setup(tmp_path)
    tools.pad = "x" * (k.context.reservoir_char_limit + 500)
    t = hub.create_thread("Infra", "alice")
    turn = k.run_turn(TurnInput(t.id, "cherche lvm", sender="alice"))
    created = rec.of("bubble.created")
    assert len(created) == 1 and created[0]["bubble"]["state"]["results"][0]["url"] == "https://a.org"
    tool_msgs = [m.content for m in turn.messages if m.role == "tool"]
    assert "tronqué" in tool_msgs[0] and "(search)" in tool_msgs[0]


class ShipTwoSearches(Ship):
    def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048):
        self.seen.append(messages)
        last_user = max(i for i, m in enumerate(messages) if m.get("role") == "user")
        n_tool = sum(1 for m in messages[last_user:] if m.get("role") == "tool")
        if n_tool < 2:
            q = "lvm" if n_tool == 0 else "lvm resize"
            return Completion(message=Message("assistant", tool_calls=[ToolCall(f"c{n_tool}", "web_search", {"query": q})]),
                              usage=Usage(prompt=3), model="m", latency_s=0)
        return Completion(message=Message("assistant", content="voilà"), usage=Usage(prompt=7, completion=2),
                          model="m", latency_s=0)


def test_several_searches_in_one_turn_merge_into_one_bubble(tmp_path):
    hub, k, ship, rec, tools = _setup(tmp_path)
    k.providers[Tier.SHIP] = ShipTwoSearches()
    tools.url_by_query = {"lvm": "https://a.org", "lvm resize": "https://b.org"}
    t = hub.create_thread("Infra", "alice")
    k.run_turn(TurnInput(t.id, "cherche lvm", sender="alice"))
    assert len(rec.of("bubble.created")) == 1
    b = hub.bubbles.require(rec.of("bubble.created")[0]["bubble"]["id"])
    assert [r["url"] for r in b.state["results"]] == ["https://a.org", "https://b.org"]
    assert b.state["queries"] == ["lvm", "lvm resize"]
    # tour suivant : nouvelle bulle
    k.providers[Tier.SHIP] = ShipTwoSearches()
    k.run_turn(TurnInput(t.id, "encore", sender="alice"))
    assert len(rec.of("bubble.created")) == 2


def test_received_context_injected_next_turn_and_replayed(tmp_path):
    hub, k, ship, rec, _ = _setup(tmp_path, search=False)
    a = hub.create_thread("Infra", "alice")
    b = hub.create_thread("Compta", "alice")
    bu = hub.create_bubble(a.id, "alice", kind="monitor", title="Disque",
                           state={"target": "hote21", "metric": "disk", "value": 91, "unit": "%"})
    hub.transfer(bu.id, "alice", b.id, "copy")
    turn = k.run_turn(TurnInput(b.id, "et alors ?", sender="alice"))
    assert turn.injected == ["[Bulle reçue de « Infra » — monitor « Disque »] hote21 disk = 91%"]
    last_user = [m for m in ship.seen[-1] if m["role"] == "user"][-1]["content"]
    assert "[Contexte reçu]\n[Bulle reçue de « Infra » — monitor « Disque »]" in last_user
    assert last_user.endswith("et alors ?")
    k.run_turn(TurnInput(b.id, "suite", sender="alice"))
    msgs = ship.seen[-1]
    first_user = [m for m in msgs if m["role"] == "user"][0]["content"]
    # Rejoué tel qu'envoyé (en-tête figé : date puis contexte reçu), option A du 20.10.2026.
    assert first_user.startswith("[Date et heure actuelles]") and "[Contexte reçu]" in first_user
    assert first_user.endswith("et alors ?")
    assert "[Contexte reçu]" not in [m for m in msgs if m["role"] == "user"][-1]["content"]


def test_injected_round_trips_through_store_serialization():
    from nokker.core.turn import InputCheck, Turn
    from nokker.store.pg import turn_from_dict, turn_to_dict

    t = Turn(thread_id="x", index=0, input=InputCheck(text="a"), injected=["[Bulle reçue …] b"])
    assert turn_from_dict(turn_to_dict(t)).injected == ["[Bulle reçue …] b"]
    d = turn_to_dict(t)
    d.pop("injected")
    assert turn_from_dict(d).injected == []  # tours d'avant ce chantier


def test_broken_observer_never_breaks_turn(tmp_path):
    hub, k, ship, rec, _ = _setup(tmp_path, search=False)

    class Boom:
        def __getattr__(self, name):
            def f(*a):
                raise RuntimeError("boom")
            return f

    k.observer = Boom()
    turn = k.run_turn(TurnInput("fil", "salut", sender="alice"))
    assert turn.reply == "voilà" and turn.error is None
