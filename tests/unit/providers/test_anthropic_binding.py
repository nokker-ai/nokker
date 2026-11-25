"""Liaison des blocs de réflexion à leur préfixe (docs/NOKKAN-ANTHROPIC.md §Liaison des blocs de réflexion).

Un bloc `thinking` est lié à `system`, `tools` et à CHAQUE message qui le précède. Le faux amont (`FakeAnthropic(binding=…)`)
applique la règle : 400 « bound to a different conversation » si un message d'avant un bloc rejoué a changé, ou si le bloc
précédent n'est plus le même.

Constat « avant » (code d'avant ce lot, même faux amont) : au tour qui suit un délestage, le dernier assistant à outils du tour
précédent était rejoué alors que le délestage avait modifié un message ANTÉRIEUR (résultat d'outil réduit, en-tête de date
retiré) : HTTP 400, le tour échouait. Pour le prouver : `git diff` du lot, ou ce fichier lancé contre l'arbre d'avant."""
from __future__ import annotations

import itertools
import json
import logging

import pytest

from nokker.context.types import ContextBudgets
from nokker.core.turn import Message
from nokker.providers.anthropic import AnthropicProvider, stamp_thinking, to_messages_body

from ..context.harness import BIG_MARK, make_session
from .anthropic_upstream import BINDING_BETA, MODEL, FakeAnthropic, message, prefix_fp

THINK = {"type": "thinking", "thinking": "je réfléchis", "signature": "x"}
SEARCH = {"type": "tool_use", "id": "toolu_1", "name": "calendar_list", "input": {}}


def _tool_reply(n: int) -> dict:
    return message([THINK, {"type": "tool_use", "id": f"toolu_{n}", "name": "big_tool", "input": {}}],
                   stop_reason="tool_use")


def _text_reply(n: int) -> dict:
    return message([THINK, {"type": "text", "text": f"Fait n°{n}."}])


def _provider(up: FakeAnthropic, **kw) -> AnthropicProvider:
    p = AnthropicProvider(base_url=up.url, api_key="k", model=MODEL, retry_backoff_s=0.0, **kw)
    p.reasoning_off_body = {"thinking": {"type": "adaptive"}}  # le routeur du banc ne demande pas toujours la réflexion
    return p


def _thinking_count(body: dict) -> int:
    return sum(1 for m in body["messages"] for b in (m["content"] if isinstance(m["content"], list) else [])
               if b.get("type") == "thinking")


def _session(up: FakeAnthropic, tmp_path, *, budgets: ContextBudgets | None = None):
    s = make_session(budgets or ContextBudgets(episodes_budget=500, current_budget=3000, compaction_step=300,
                                               shed_min_tokens=200, reservoir_min_tokens=10 ** 6, keep_last_turns_raw=2,
                                               retroactive_shed=True, shed_heads=True),
                     tmp_path=tmp_path, tool_size=3000, provider=_provider(up))
    s.kernel.tool_loading = "all"  # `tools` stable d'un appel à l'autre (un outil ajouté en cours de tour invalide la réflexion)
    return s


def _run_turns(s, up: FakeAnthropic, n: int, first: int = 0) -> None:
    for i in range(first, first + n):
        up.reply(_tool_reply(i))
        up.reply(_text_reply(i))
        turn = s.say(f"logs {i} {BIG_MARK}")
        assert turn.error is None and turn.reply.startswith("Fait"), (i, turn.error, turn.reply)


def test_multi_turn_capture_with_a_shed_in_the_middle_never_gets_the_binding_400(tmp_path):
    """Capture multi-tours rejouée contre l'amont qui applique la règle (`error`) : le délestage réécrit des messages
    anciens au milieu, le tour suivant ne rejoue plus les blocs d'avant la réécriture (et ne part pas en 400)."""
    with FakeAnthropic(binding="error") as up:
        s = _session(up, tmp_path)
        _run_turns(s, up, 7)
        st = s.manager.state(s.thread)
        assert st.compaction_version >= 1 and st.shed, "le scénario doit contenir un délestage"
        assert up.dropped == []  # le filet `drop_block` n'a jamais servi
        # La même requête, rejouée par le faux amont en mode `error`, a toujours passé : 14 appels, 14 réponses.
        assert len(up.requests) == 14


def test_blocks_stay_replayed_for_the_whole_window_between_two_sheds(tmp_path):
    """Entre deux réécritures la fenêtre est append-only : chaque appel rejoue TOUS les blocs du même modèle déjà en
    fenêtre, et le préfixe de la requête précédente est repris tel quel (cache)."""
    with FakeAnthropic(binding="error") as up:
        s = _session(up, tmp_path, budgets=ContextBudgets(episodes_budget=500, current_budget=10 ** 6,
                                                          compaction_step=10 ** 6, shed_min_tokens=200,
                                                          reservoir_min_tokens=10 ** 6))
        _run_turns(s, up, 4)
        bodies = [r["body"] for r in up.requests]
    # appel k (0-based) : 2 appels par tour ; avant lui, k messages assistant avaient une réflexion
    for k, body in enumerate(bodies):
        assert _thinking_count(body) == k, (k, _thinking_count(body))
    for prev, nxt in itertools.pairwise(bodies):
        for a, b in zip(prev["messages"], nxt["messages"], strict=False):  # message par message, réflexion rejouée exclue
            assert json.dumps([x for x in a["content"] if x["type"] != "thinking"]) == \
                json.dumps([x for x in b["content"] if x["type"] != "thinking"])


def test_after_a_shed_the_older_blocks_are_not_replayed_but_new_ones_are(tmp_path):
    with FakeAnthropic(binding="error") as up:
        s = _session(up, tmp_path)
        _run_turns(s, up, 7)
        compacted_at = None
        v = 0
        # le tour qui suit la réécriture repart d'une chaîne neuve : ses blocs sont rejoués, pas ceux d'avant
        for k, r in enumerate(up.requests):
            c = _thinking_count(r["body"])
            if c < v:
                compacted_at = k
            v = c
        assert compacted_at is not None, "le nombre de blocs rejoués doit retomber après le délestage"
        assert _thinking_count(up.requests[-1]["body"]) >= 1


def test_the_stamped_fingerprint_is_the_one_the_upstream_computes():
    with FakeAnthropic(binding="error") as up:
        p = _provider(up)
        up.reply(_tool_reply(1))
        comp = p.complete([{"role": "system", "content": "S"}, {"role": "user", "content": "q"}], [], reasoning=True,
                          session_key="t")
        first = up.requests[0]["body"]
        stamped = comp.message.thinking_blocks[0]
        assert stamped["_fp"] == prefix_fp(first) and stamped["_prev"] == ""
        # jamais envoyés à l'amont
        up.reply(message([{"type": "text", "text": "ok"}]))
        p.complete([{"role": "system", "content": "S"}, {"role": "user", "content": "q"},
                    comp.message.to_openai(thinking=True), {"role": "tool", "tool_call_id": "toolu_1", "content": "r"}],
                   [], reasoning=True, session_key="t")
        wire = json.dumps(up.requests[1]["body"])
    assert "_fp" not in wire and "_prev" not in wire and "_by" not in wire
    assert _thinking_count(up.requests[1]["body"]) == 1  # et le bloc, lui, a été rejoué (préfixe identique)


def test_a_message_changed_before_a_replayed_block_drops_it_instead_of_a_400():
    with FakeAnthropic(binding="error") as up:
        p = _provider(up)
        up.reply(_tool_reply(1))
        comp = p.complete([{"role": "user", "content": "q"}], [], reasoning=True, session_key="t")
        up.reply(message([{"type": "text", "text": "ok"}]))
        # l'historique a changé AVANT le bloc : le message user ancien est réécrit
        p.complete([{"role": "user", "content": "q (réécrit)"}, comp.message.to_openai(thinking=True),
                    {"role": "tool", "tool_call_id": "toolu_1", "content": "r"}], [], reasoning=True, session_key="t")
        body = up.requests[1]["body"]
    assert _thinking_count(body) == 0  # abandonné : l'appel part sans ce bloc, l'amont a répondu 200


def test_final_answers_keep_their_thinking_so_the_chain_has_no_gap(tmp_path):
    with FakeAnthropic(binding="error") as up:
        s = _session(up, tmp_path)
        _run_turns(s, up, 1)
        turn = s.store.history(s.thread)[-1]
    finals = [m for m in turn.messages if m.role == "assistant" and not m.tool_calls]
    assert finals and finals[-1].thinking_blocks and "je réfléchis" in finals[-1].thinking_blocks[0]["thinking"]
    assert "je réfléchis" not in turn.reply


# ── filet `drop_block` et détecteur `input_transformations` ──


def _unstamped_wire(comp) -> dict:
    """Message assistant d'avant le marquage (aucune empreinte) : rejoué par l'ancienne portée, donc non protégé."""
    wire = comp.message.to_openai(thinking=True)
    for b in wire["thinking_blocks"]:
        b.pop("_fp", None)
        b.pop("_prev", None)
    return wire


def test_drop_block_is_only_a_net_after_a_binding_400_and_is_journaled(caplog):
    caplog.set_level(logging.INFO, logger="nokker.providers.anthropic")
    with FakeAnthropic(binding="error") as up:
        p = _provider(up)
        up.reply(_tool_reply(1))
        comp = p.complete([{"role": "user", "content": "q"}], [], reasoning=True, session_key="t")
        wire = _unstamped_wire(comp)
        changed = [{"role": "user", "content": "q (réécrit)"}, wire, {"role": "tool", "tool_call_id": "toolu_1", "content": "r"}]
        up.reply(message([{"type": "text", "text": "ok"}]))
        done = p.complete(changed, [], reasoning=True, session_key="t")
        first, retry = up.requests[1], up.requests[2]
        assert "block_binding" not in first["body"]["thinking"]  # jamais d'emblée
        assert retry["body"]["thinking"]["block_binding"] == {"prefix_mismatch_behavior": "drop_block"}
        assert BINDING_BETA in retry["headers"]["anthropic-beta"]
        assert done.message.content == "ok" and done.attempts == 2
        assert len(up.dropped) == 1 and up.dropped[0]["reason"] == "prefix_binding_mismatch"
        # le fil est mémorisé : l'appel suivant part avec drop_block d'emblée
        up.reply(message([{"type": "text", "text": "ok2"}]))
        p.complete(changed, [], reasoning=True, session_key="t")
        assert up.requests[3]["body"]["thinking"]["block_binding"]["prefix_mismatch_behavior"] == "drop_block"
    logs = "\n".join(r.getMessage() for r in caplog.records)
    assert "drop_block" in logs and "je réfléchis" not in logs and "q (réécrit)" not in logs


def test_other_400s_are_never_retried_with_drop_block():
    with FakeAnthropic() as up:
        p = _provider(up)
        up.reply({"type": "error", "error": {"type": "invalid_request_error", "message": "autre chose"}}, status=400)
        with pytest.raises(RuntimeError, match="autre chose"):
            p.complete([{"role": "user", "content": "q"}], [], reasoning=True, session_key="t")
        assert len(up.requests) == 1


def test_binding_controls_sends_the_beta_header_and_journals_the_transformations_without_text(caplog):
    caplog.set_level(logging.WARNING, logger="nokker.providers.anthropic")
    with FakeAnthropic(binding="allow") as up:
        p = _provider(up, binding_controls=True)
        up.reply(_tool_reply(1))
        comp = p.complete([{"role": "user", "content": "q"}], [], reasoning=True, session_key="t")
        up.reply(message([{"type": "text", "text": "ok"}]))
        p.complete([{"role": "user", "content": "q (réécrit)"}, _unstamped_wire(comp),
                    {"role": "tool", "tool_call_id": "toolu_1", "content": "r"}], [], reasoning=True, session_key="t")
        assert BINDING_BETA in up.requests[0]["headers"]["anthropic-beta"]
        assert "block_binding" not in up.requests[1]["body"]["thinking"]  # le détecteur ne change pas la requête
    logs = "\n".join(r.getMessage() for r in caplog.records)
    assert "thinking_mismatch_allowed" in logs and "prefix_binding_mismatch" in logs and "je réfléchis" not in logs


def test_without_binding_controls_no_beta_header_is_sent():
    with FakeAnthropic() as up:
        p = _provider(up)
        up.reply(message([{"type": "text", "text": "ok"}]))
        p.complete([{"role": "user", "content": "q"}], [], reasoning=True, session_key="t")
        assert "anthropic-beta" not in up.requests[0]["headers"]


def test_stamp_is_a_noop_without_thinking():
    m = Message("assistant", "x")
    stamp_thinking(m, {"messages": []})
    assert m.thinking_blocks == []


def test_unstamped_blocks_keep_the_previous_scope_only():
    """Messages d'avant le marquage : seul le tour d'outils en cours (et le dernier assistant à outils) est rejoué."""
    blk = [{"type": "thinking", "thinking": "t", "signature": "S", "_by": MODEL}]
    old = {"role": "assistant", "content": "", "thinking_blocks": blk,
           "tool_calls": [{"id": "a", "type": "function", "function": {"name": "f", "arguments": "{}"}}]}
    msgs = [{"role": "user", "content": "1"}, old, {"role": "tool", "tool_call_id": "a", "content": "r"},
            {"role": "assistant", "content": "fin"}, {"role": "user", "content": "2"}]
    body = to_messages_body(msgs, [], model=MODEL, max_tokens=10, thinking_model=MODEL)
    assert _thinking_count(body) == 1  # le dernier assistant à outils, comme avant ce lot
