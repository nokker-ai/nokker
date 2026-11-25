"""Capture : provenance « personne », une relance si l'extraction est vide alors que l'épisode affirme une valeur, exemple de lieu
générique au prompt, date = instant du tour source."""
from __future__ import annotations

from nokker.context.types import ContextBudgets
from nokker.core.turn import InputCheck, Message, Turn
from nokker.memory.provenance import PERSON
from nokker.memory.values import SYSTEM, claims_value
from nokker.tenancy.context import tenant_scope

from .test_values_capture import A, T1, TA, Model, _turn, _vals, _world


def test_a_value_said_by_the_person_is_not_marked_as_coming_from_a_tool():
    model = Model(_vals({"key": "budget marketing", "value": "5000 francs", "turn": 1}))
    svc, cap = _world(model)
    with tenant_scope(TA):
        cap.capture(A, "th_1", T1, "silence")  # T1 : le message de la personne porte la valeur
        note = next(n for n, _ in svc.store.candidates(svc.actor(A), "user", None, None, None, 50) if n.key)
        assert note.provenance == PERSON
        tool_turn = _turn(7, "Lis le fichier de config.")
        tool_turn.messages = [Message(role="tool", content="Le délai est fixé à 12 jours.", tool_call_id="c", name="read_file")]
        model.answer = _vals({"key": "delai", "value": "12 jours", "turn": 7})
        cap.capture(A, "th_1", [tool_turn], "silence")
        n2 = next(n for n, _ in svc.store.candidates(svc.actor(A), "user", None, None, None, 50) if n.key == "delai")
        assert n2.provenance != PERSON  # lue dans un résultat d'outil : jamais « les mots de la personne »


# ── (b) ──
class Seq(Model):
    def __init__(self, *answers):
        super().__init__(None)
        self.answers = list(answers)

    def __call__(self, messages):
        self.asked.append(messages)
        a = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        return a if isinstance(a, str) else __import__("json").dumps(a)


def test_one_retry_when_the_json_is_valid_but_empty_and_the_episode_claims_a_value():
    good = _vals({"key": "budget marketing", "value": "5000 francs", "turn": 1})
    model = Seq({"values": []}, good)
    svc, cap = _world(model)
    with tenant_scope(TA):
        assert cap.capture(A, "th_1", T1, "silence") == 1
    assert len(model.asked) == 2 and "ATTENTION" in model.asked[1][-1]["content"]


def test_no_retry_without_a_claim_and_never_a_third_call():
    model = Seq({"values": []})
    svc, cap = _world(model)
    with tenant_scope(TA):
        assert cap.capture(A, "th_1", [_turn(1, "Bonjour, comment ça va ?")], "silence") == 0
        assert len(model.asked) == 1
        assert cap.capture(A, "th_1", T1, "silence") == 0  # relance autorisée, toujours vide : on s'arrête
    assert len(model.asked) == 3  # 1 + (1 + 1 relance) : jamais de boucle


def test_claims_value_heuristic():
    assert claims_value([_turn(1, "Le budget est fixé à 5000 francs.")])
    assert claims_value([_turn(1, "La réunion aura lieu en salle des Alpes.")])
    assert not claims_value([_turn(1, "Bonjour, tu peux m'aider ?")])
    assert not claims_value([_turn(1, "quelle heure est-il")])


def test_prompt_has_a_generic_location_example_and_none_of_the_bench_values():
    assert "lieu de la réunion d'équipe" in SYSTEM
    for bad in ("comité", "D415", "pilotage"):
        assert bad not in SYSTEM


# ── (d) ──
def test_value_is_dated_with_the_source_turn_instant_else_the_capture_date():
    model = Model(_vals({"key": "budget marketing", "value": "5000 francs", "turn": 1}))
    svc, cap = _world(model)
    t = _turn(1, "Le budget marketing est fixé à 5000 francs.")
    t.at = 1_700_000_000.0  # 2023-11-14
    with tenant_scope(TA):
        cap.capture(A, "th_1", [t], "silence")
        n = next(n for n, _ in svc.store.candidates(svc.actor(A), "user", None, None, None, 50) if n.key)
        assert abs(n.created_at - 1_700_000_000.0) < 1
        assert "2023-11-14" in svc.recall(A, "budget marketing")[0].text
        model.answer = _vals({"key": "delai", "value": "12 jours", "turn": 2})
        cap.capture(A, "th_1", [_turn(2, "Le délai est fixé à 12 jours.")], "silence")  # at inconnu : date de capture
        n2 = next(n for n, _ in svc.store.candidates(svc.actor(A), "user", None, None, None, 50) if n.key == "delai")
        assert n2.created_at > 1_750_000_000.0


def test_turn_at_is_set_by_the_kernel_clock_persisted_and_read_from_an_import(tmp_path):
    from nokker.core import history_import as hi
    from nokker.store.pg import turn_from_dict, turn_to_dict
    from tests.unit.context.harness import make_session

    s = make_session(ContextBudgets(), tmp_path=tmp_path)
    s.kernel.turn_clock = lambda: 1_800_000_000.0
    s.say("bonjour")
    t = s.store.history(s.thread)[0]
    assert t.at == 1_800_000_000.0 and turn_from_dict(turn_to_dict(t)).at == 1_800_000_000.0
    assert "at" not in turn_to_dict(Turn(thread_id="t", index=0, input=InputCheck(text="x")))  # tours d'avant : inchangés
    out = hi.import_history(s.kernel, "th_imp_at", {"turns": [
        {"role": "user", "content": "Le port est 8080.", "at": "2026-03-01T10:00:00Z"},
        {"role": "assistant", "content": "Noté."},
        {"role": "user", "content": "Et le reste ?", "at": "pas une date"},
        {"role": "assistant", "content": "Rien."}]})
    ts = s.store.history("th_imp_at")
    assert out["turns"] == 2 and ts[0].at > 1_770_000_000.0 and ts[1].at == 0.0
