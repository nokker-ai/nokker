"""`turn.started` porte le message de la personne (`user_message`) : un autre client branché sur le fil l'affiche PENDANT
le tour. Même texte que l'historique relu, recaviardé, borné, absent pour un sous-agent ou un tour sans message."""
from __future__ import annotations

from nokker.bubbles.hub import STARTED_MESSAGE_BYTES
from nokker.core import session_opts
from nokker.core.ports import Completion
from nokker.core.turn import InputCheck, Message, TurnInput, Usage
from tests.unit.bubbles.test_kernel_bubbles import _setup


class MaskingInvariants:
    """Caviardage à l'entrée comme les vrais invariants : le secret n'entre jamais au fil."""

    def check_input(self, text):
        return InputCheck(text=text.replace("S3CR3T-VALUE", "[secret:TEST]"))

    def __getattr__(self, name):
        from tests.unit.api.test_server import AllowInvariants
        return getattr(AllowInvariants(), name)


class Probe:
    """Fournisseur : note les événements déjà publiés au moment de l'appel du modèle."""

    def __init__(self, rec):
        self.rec, self.at_call = rec, []

    def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048):
        self.at_call.append(self.rec.types())
        return Completion(message=Message("assistant", content="ok"), usage=Usage(prompt=1, completion=1), model="m",
                          latency_s=0)


def _kit(tmp_path):
    from nokker.core.turn import Tier
    hub, k, ship, rec, tools = _setup(tmp_path, search=False)
    probe = Probe(rec)
    k.providers[Tier.SHIP] = probe
    return hub, k, probe, rec


def test_started_is_emitted_once_before_the_model_with_exact_content(tmp_path):
    hub, k, probe, rec = _kit(tmp_path)
    t = hub.create_thread("Infra", "alice")
    rec.events.clear()
    k.run_turn(TurnInput(t.id, "bonjour é 你好", sender="alice"))
    started = rec.of("turn.started")
    assert len(started) == 1
    assert started[0] == {"thread_id": t.id, "turn_id": f"{t.id}:0", "user_message": "bonjour é 你好",
                          "_audience": ["alice"]}
    assert probe.at_call and all("turn.started" in seen for seen in probe.at_call)  # observé AVANT l'appel du modèle
    types = rec.types()
    assert types.index("turn.started") < types.index("turn.done")
    assert rec.of("turn.done")[0]["turn_id"] == started[0]["turn_id"]


def test_secret_stays_masked_like_the_history(tmp_path):
    hub, k, probe, rec = _kit(tmp_path)
    k.invariants = MaskingInvariants()
    t = hub.create_thread("Infra", "alice")
    rec.events.clear()
    k.run_turn(TurnInput(t.id, "ma clé S3CR3T-VALUE ici", sender="alice"))
    msg = rec.of("turn.started")[0]["user_message"]
    history = [m for m in hub.messages(t.id) if m["role"] == "user"][0]["content"]
    assert msg == history == "ma clé [secret:TEST] ici" and "S3CR3T" not in msg


def test_rescrubbed_even_if_the_input_check_let_it_through(tmp_path):
    hub, k, probe, rec = _kit(tmp_path)
    t = hub.create_thread("Infra", "alice")
    rec.events.clear()
    k.run_turn(TurnInput(t.id, "ma clé S3CR3T-VALUE ici", sender="alice"))
    assert "S3CR3T" not in rec.of("turn.started")[0]["user_message"]


def test_long_message_is_truncated_on_a_character_boundary(tmp_path):
    hub, k, probe, rec = _kit(tmp_path)
    t = hub.create_thread("Infra", "alice")
    rec.events.clear()
    k.run_turn(TurnInput(t.id, "é" * (STARTED_MESSAGE_BYTES), sender="alice"))  # 2 octets par caractère
    ev = rec.of("turn.started")[0]
    assert ev["truncated"] is True
    raw = ev["user_message"].encode("utf-8")
    assert len(raw) <= STARTED_MESSAGE_BYTES and set(ev["user_message"]) == {"é"} and len(raw) == STARTED_MESSAGE_BYTES
    rec.events.clear()
    k.run_turn(TurnInput(t.id, "a" * STARTED_MESSAGE_BYTES, sender="alice"))  # pile à la borne : intact
    ev = rec.of("turn.started")[0]
    assert len(ev["user_message"]) == STARTED_MESSAGE_BYTES and "truncated" not in ev


def test_odd_cut_does_not_break_utf8(tmp_path):
    hub, k, probe, rec = _kit(tmp_path)
    t = hub.create_thread("Infra", "alice")
    rec.events.clear()
    k.run_turn(TurnInput(t.id, "a" + "é" * STARTED_MESSAGE_BYTES, sender="alice"))  # coupe au milieu d'un « é »
    ev = rec.of("turn.started")[0]
    assert ev["truncated"] is True and len(ev["user_message"].encode("utf-8")) == STARTED_MESSAGE_BYTES - 1
    assert "�" not in ev["user_message"]


def test_no_message_for_subagent_or_empty_turn_but_the_event_stays(tmp_path):
    hub, k, probe, rec = _kit(tmp_path)
    t = hub.create_thread("Infra", "alice")
    rec.events.clear()
    k.run_turn(TurnInput(f"{t.id}/sa-abc", "consigne du modèle", sender="alice"))
    k.run_turn(TurnInput(t.id, "   ", sender="alice"))
    started = rec.of("turn.started")
    assert len(started) == 2 and all("user_message" not in e and "truncated" not in e for e in started)


def test_kernel_written_turn_has_no_user_message(tmp_path):
    hub, k, probe, rec = _kit(tmp_path)
    t = hub.create_thread("Infra", "alice")
    rec.events.clear()
    k.run_turn(TurnInput(t.id, "[Approbation] ok", sender="alice", control="approval_continue"))
    [ev] = rec.of("turn.started")
    assert "user_message" not in ev


def test_required_mcp_failure_has_started_then_error_done(tmp_path):
    hub, k, probe, rec = _kit(tmp_path)
    t = hub.create_thread("Infra", "alice")
    rec.events.clear()
    session_opts.mcp_required_registry().set(t.id, ("metier",))
    k._required_mcp_missing = lambda required: [("metier", "ConnectionError")]
    try:
        turn = k.run_turn(TurnInput(t.id, "bonjour", sender="alice"))
    finally:
        session_opts.mcp_required_registry().clear(t.id)
    assert turn.failure["code"] == "mcp_required_unavailable" and probe.at_call == []
    types = rec.types()
    assert types.index("turn.started") < types.index("mcp.required_unavailable") < types.index("turn.done")
    assert rec.of("turn.started")[0]["user_message"] == "bonjour" and rec.of("turn.done")[0]["error"]


def test_observer_failure_does_not_break_the_turn(tmp_path):
    hub, k, probe, rec = _kit(tmp_path)
    t = hub.create_thread("Infra", "alice")
    hub._started_message = lambda turn: (_ for _ in ()).throw(RuntimeError("boom"))
    turn = k.run_turn(TurnInput(t.id, "bonjour", sender="alice"))
    assert turn.reply == "ok" and "turn.done" in rec.types()


def test_two_threads_are_isolated(tmp_path):
    hub, k, probe, rec = _kit(tmp_path)
    hub.known_users = frozenset({"alice", "bruno"})
    a = hub.create_thread("A", "alice")
    b = hub.create_thread("B", "bruno")
    rec.events.clear()
    k.run_turn(TurnInput(a.id, "message A", sender="alice"))
    k.run_turn(TurnInput(b.id, "message B", sender="bruno"))
    by = {e["thread_id"]: e for e in rec.of("turn.started")}
    assert by[a.id]["user_message"] == "message A" and by[a.id]["_audience"] == ["alice"]
    assert by[b.id]["user_message"] == "message B" and by[b.id]["_audience"] == ["bruno"]
