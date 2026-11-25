"""Un outil dont l'appel part en carte (« EN ATTENTE D'APPROBATION HUMAINE ») ne doit pas être relancé en boucle dans le même tour
(vu sur lokkal-test1, 12.11 : 48 `desktop_act` identiques en attente, un toutes les 7 s, parce que l'id de la carte `[approval:<id>]`
rendait chaque résultat différent pour `stall_signature`)."""
from __future__ import annotations

import hashlib

from nokker.core.loop import PENDING_TURN_END, human_tool, stall_signature
from nokker.core.ports import ToolResult
from nokker.core.turn import Message, ToolCall, TurnInput

from tests.unit.test_loop_contract import kernel, tc

FP_C7 = "0861bcdf2ea5b561"


def test_stall_signature_ignores_the_approval_id():
    call = ToolCall(id="c1", name="desktop_act", arguments={"action": "click", "ref": "e5"})
    a = ToolResult(call_id="", name="desktop_act", content="EN ATTENTE D'APPROBATION HUMAINE (effect:write) [approval:930770c1-327b]. L'action n'a pas été exécutée.")
    b = ToolResult(call_id="", name="desktop_act", content="EN ATTENTE D'APPROBATION HUMAINE (effect:write) [approval:71226b40-2361]. L'action n'a pas été exécutée.")
    c = ToolResult(call_id="", name="desktop_act", content="appli ouverte : fenêtre w1")
    assert stall_signature(call, a) == stall_signature(call, b) != stall_signature(call, c)


def test_second_pending_of_the_same_tool_ends_the_turn():
    # arguments qui changent : la détection de répétition ne joue pas ; seule la règle « une carte en attente par outil » arrête le tour
    script = [Message("assistant", tool_calls=[tc("infra_lvextend", size=str(i))]) for i in range(1, 6)]
    k, tools, ship, _ = kernel(script + [Message("assistant", "ne doit pas être appelé")])
    k.max_calls, k.stall_limit = 20, 6
    turn = k.run_turn(TurnInput("t1", "agrandis le disque"))
    assert tools.called == []
    assert turn.reply == PENDING_TURN_END.format(what=human_tool("infra_lvextend")) and turn.error is None
    assert len(turn.calls) == 2  # un 3e appel modèle n'a pas lieu
    assert len(turn.pending_approvals) == 2


def test_a_single_pending_does_not_end_the_turn():
    k, tools, *_ = kernel([Message("assistant", tool_calls=[tc("infra_lvextend")]), Message("assistant", "j'attends ta validation")])
    turn = k.run_turn(TurnInput("t1", "agrandis le disque"))
    assert turn.reply == "j'attends ta validation" and len(turn.pending_approvals) == 1


def test_pending_then_another_tool_continues():
    script = [Message("assistant", tool_calls=[tc("infra_lvextend")]), Message("assistant", tool_calls=[tc("calendar_list", day="lundi")]),
              Message("assistant", "terminé")]
    k, *_ = kernel(script)
    turn = k.run_turn(TurnInput("t1", "agrandis le disque puis regarde lundi"))
    assert turn.reply == "terminé"


def test_two_pending_calls_in_one_message_second_is_skipped_but_answered():
    calls = [ToolCall(id="c-a", name="infra_lvextend", arguments={"size": "1"}), ToolCall(id="c-b", name="infra_lvextend", arguments={"size": "2"})]
    k, tools, *_ = kernel([Message("assistant", tool_calls=calls), Message("assistant", "ne doit pas être appelé")])
    turn = k.run_turn(TurnInput("t1", "agrandis le disque"))
    assert tools.called == [] and turn.reply == PENDING_TURN_END.format(what=human_tool("infra_lvextend"))
    assert len(turn.calls) == 1
    tool_msgs = [m for m in turn.messages if m.role == "tool"]
    assert {m.tool_call_id for m in tool_msgs} == {"c-a", "c-b"}  # chaque appel garde sa réponse (historique valide)


# ── chaîne de tours de suite (12.11 : 85 tours de suite sans décision, un pas clos de lui-même relançait un tour) ──
def test_follow_chain_is_capped_and_logged():
    from nokker.core.turn import Decision

    script = [Message("assistant", f"suite {i}") for i in range(10)]
    k, tools, ship, _ = kernel(script)
    k.follow_chain_max = 3
    replies = [k.run_turn(TurnInput("t-chain", f"[suite {i}]", control="desktop_continue")) for i in range(5)]
    assert [r.reply for r in replies[:3]] == ["suite 0", "suite 1", "suite 2"]
    assert all("Je m'arrête" in r.reply for r in replies[3:])
    assert any(v.rule == "loop:follow_chain" and v.decision is Decision.DENY for v in replies[3].verdicts)
    assert len(ship.calls if hasattr(ship, "calls") else []) <= 3  # les tours refusés n'appellent pas le modèle


def test_a_person_message_resets_the_follow_chain():
    k, *_ = kernel([Message("assistant", f"r{i}") for i in range(12)])
    k.follow_chain_max = 2
    k.run_turn(TurnInput("t-reset", "a", control="phone_continue"))
    k.run_turn(TurnInput("t-reset", "b", control="phone_continue"))
    k.run_turn(TurnInput("t-reset", "salut"))  # message de la personne
    t = k.run_turn(TurnInput("t-reset", "c", control="phone_continue"))
    assert "Je m'arrête" not in t.reply


def test_approval_continue_is_not_capped():
    k, *_ = kernel([Message("assistant", f"r{i}") for i in range(12)])
    k.follow_chain_max = 1
    turns = [k.run_turn(TurnInput("t-appr", f"[Approbation] {i}", control="approval_continue")) for i in range(4)]
    assert all("Je m'arrête" not in t.reply for t in turns)  # chaque suite de carte suit une décision de la personne


def test_desktop_step_without_person_decision_launches_no_turn():
    from types import SimpleNamespace

    from nokker.cloud.phone_tools import PhoneFollow
    from nokker.desktop.steps import decided_by_person

    launched = []
    follow = PhoneFollow(phone=None, launch=lambda *a: launched.append(a), tasks_field="desktop_tasks",
                         continue_with=lambda f, step, status: ("suite", ""), continue_if=decided_by_person)
    f = SimpleNamespace(thread_id="t", sender="s", channel="api", task_id="ds_1", status="waiting")
    for status in ("expired", "failed"):
        follow.settled(f, SimpleNamespace(status=status, answer=None))
    assert launched == []
    follow.settled(f, SimpleNamespace(status="done", answer="go"))
    follow.settled(f, SimpleNamespace(status="failed", answer="stop"))
    assert len(launched) == 2


# ── lot C7 : PENDING_TURN_END lu par la personne, sans nom d'outil brut ──
def test_pending_turn_end_text_is_frozen_and_names_the_same_tool_rule():
    from nokker.core.loop import HUMAN_TOOL_FALLBACK, HUMAN_TOOLS

    assert PENDING_TURN_END == ("En attente de ta validation : une carte ({what}) attend déjà ta décision. Je ne propose pas d'autre action "
                                "avec le même outil avant ta réponse ; je reprendrai avec le résultat.")
    assert "du même type" not in PENDING_TURN_END and "même outil" in PENDING_TURN_END
    blob = repr((PENDING_TURN_END, sorted(HUMAN_TOOLS.items()), HUMAN_TOOL_FALLBACK))
    assert hashlib.sha256(blob.encode()).hexdigest()[:16] == FP_C7


def test_human_tool_never_shows_a_raw_or_hostile_name():
    assert human_tool("terminal") == "terminal" and human_tool("desktop_act") == "bureau"
    evil = "mcp__x»évil»\nname"
    assert human_tool(evil) == "action non nommée" == human_tool("infra_lvextend")
    assert human_tool("patch") == human_tool("write_file") == "fichier"
    assert {human_tool(n) for n in ("mail_send", "mail_draft", "mail_read", "mail_search", "mail_move", "mail_trash")} == {"e-mail"}
    assert human_tool("mail_evil_mcp") == "action non nommée" and human_tool("mcp__mail_send") == "action non nommée"
    text = PENDING_TURN_END.format(what=human_tool(evil))
    assert "mcp__" not in text and "»" not in text and "\n" not in text


def test_turn_reply_does_not_contain_the_raw_tool_name():
    script = [Message("assistant", tool_calls=[tc("infra_lvextend", size=str(i))]) for i in range(1, 4)]
    k, tools, ship, _ = kernel(script + [Message("assistant", "x")])
    k.max_calls, k.stall_limit = 20, 6
    turn = k.run_turn(TurnInput("t1", "agrandis le disque"))
    assert "infra_lvextend" not in turn.reply and "action non nommée" in turn.reply
