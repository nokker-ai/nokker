"""Capture automatique des valeurs durables (nokker/memory/values.py) : extraction bornée et validée, écriture datée,
remplacement par archivage (jamais deux valeurs vivantes pour une clé), échec = rien d'écrit, déclenchement à la clôture
d'un épisode et au chargement d'un historique importé. Modèle scripté, magasin en mémoire, aucun réseau."""
from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from nokker.core.turn import InputCheck, Message
from nokker.memory.tenant_long import HashEmbedder, LongMemory, MemoryLongStore
from nokker.memory.values import MAX_ITEMS, ValueCapture, parse_items
from nokker.tenancy.context import tenant_scope

TA, TB = "tn_" + "a" * 12, "tn_" + "b" * 12
A, B = "us_aaaa", "us_bbbb"


@dataclass
class Acc:
    account_id: str
    tenant_id: str
    status: str = "active"


def _turn(index: int, text: str, reply: str = "D'accord."):
    from nokker.core.turn import Turn

    t = Turn(thread_id="th_1", index=index, input=InputCheck(text=text),
             reply=reply)
    t.messages = [Message(role="assistant", content=reply)]
    return t


class Model:
    """Le palier d'extraction : rend ce qu'on lui a dit de rendre (texte), ou lève."""

    def __init__(self, answer: object = None, boom: bool = False) -> None:
        self.answer, self.boom, self.asked = answer, boom, []

    def __call__(self, messages):
        self.asked.append(messages)
        if self.boom:
            raise RuntimeError("palier en panne")
        return self.answer if isinstance(self.answer, str) else json.dumps(self.answer)


def _world(model):
    store = MemoryLongStore(lambda _a: None)
    svc = LongMemory(store=store, embedder=HashEmbedder(),
                     account_for_tenant=lambda t: Acc(A, t) if t == TA else Acc(B, t) if t == TB else None)
    cap = ValueCapture(long=svc, complete=model, enabled=True)
    return svc, cap


def _vals(*items):
    return {"values": [dict(kind="decision", **i) for i in items]}


T1 = [_turn(1, "Le budget marketing est fixé à 5000 francs."), _turn(2, "Ok.")]
T2 = [_turn(3, "Finalement le budget marketing passe de 5000 à 8000 francs.")]


def test_capture_writes_a_dated_value_with_its_source():
    svc, cap = _world(Model(_vals({"key": "Budget marketing", "value": "5000 francs", "turn": 1})))
    with tenant_scope(TA):
        assert cap.capture(A, "th_1", T1, "silence") == 1
        note = svc.listing(A)["notes"][0]
    assert note["key"] == "budget marketing" and note["value"] == "5000 francs"
    assert note["source_thread"] == "th_1" and note["source_turn"] == 1 and note["created_at"] > 0


def test_a_replaced_value_is_archived_with_its_link_and_only_the_final_one_is_recalled():
    model = Model(_vals({"key": "budget marketing", "value": "5000 francs", "turn": 1}))
    svc, cap = _world(model)
    with tenant_scope(TA):
        cap.capture(A, "th_1", T1, "silence")
        model.answer = _vals({"key": "budget marketing", "value": "8000 francs", "turn": 3})
        assert cap.capture(A, "th_1", T2, "silence") == 1
        live = [n for n in svc.listing(A)["notes"] if n.get("key")]
        assert [n["value"] for n in live] == ["8000 francs"]  # jamais deux valeurs vivantes pour la clé
        archived = svc.export(A)["archived"]
        assert [n["value"] for n in archived] == ["5000 francs"] and archived[0]["superseded_by"] == live[0]["name"]
        recalled = svc.recall(A, "quel est le budget marketing ?")
    assert recalled and "8000" in recalled[0].text and "5000" not in recalled[0].text
    assert "2026" in recalled[0].text or "depuis" in recalled[0].text  # sa date accompagne la ligne


def test_the_same_value_twice_writes_nothing_more():
    model = Model(_vals({"key": "budget marketing", "value": "5000 francs", "turn": 1}))
    svc, cap = _world(model)
    with tenant_scope(TA):
        assert cap.capture(A, "th_1", T1, "silence") == 1
        assert cap.capture(A, "th_1", T1, "silence") == 0  # rejeu d'un épisode : aucun doublon
        assert len(svc.listing(A)["notes"]) == 1 and "archived" not in svc.export(A)


@pytest.mark.parametrize("answer", [
    "pas du JSON du tout",
    {"values": "oups"},
    _vals({"key": "budget", "value": "9999 francs", "turn": 1}),          # nombre absent de la transcription
    _vals({"key": "budget", "value": "5000 francs", "turn": 99}),         # tour inconnu
    _vals({"key": "", "value": "5000 francs", "turn": 1}),                # clé vide
    {"values": [{"kind": "bizarre", "key": "budget", "value": "5000 francs", "turn": 1}]},
])
def test_invalid_or_invented_output_writes_nothing(answer):
    svc, cap = _world(Model(answer))
    with tenant_scope(TA):
        assert cap.capture(A, "th_1", T1, "silence") == 0
        assert svc.listing(A)["notes"] == []


def test_a_failing_model_writes_nothing_and_never_raises():
    svc, cap = _world(Model(boom=True))
    with tenant_scope(TA):
        assert cap.capture(A, "th_1", T1, "silence") == 0
        cap("th_1", T1, "silence")  # le point d'entrée du gestionnaire non plus
        assert svc.listing(A)["notes"] == []


def test_the_output_is_bounded():
    many = _vals(*({"key": f"cle {i}", "value": "5000 francs", "turn": 1} for i in range(40)))
    svc, cap = _world(Model(many))
    with tenant_scope(TA):
        assert cap.capture(A, "th_1", T1, "silence") <= MAX_ITEMS


def test_values_stay_with_their_account():
    model = Model(_vals({"key": "budget marketing", "value": "5000 francs", "turn": 1}))
    svc, cap = _world(model)
    with tenant_scope(TA):
        cap.capture(A, "th_1", T1, "silence")
    with tenant_scope(TB):
        assert svc.listing(B)["notes"] == [] and svc.recall(B, "budget marketing") == []


def test_erasing_the_account_erases_its_archived_values_too():
    model = Model(_vals({"key": "budget marketing", "value": "5000 francs", "turn": 1}))
    svc, cap = _world(model)
    with tenant_scope(TA):
        cap.capture(A, "th_1", T1, "silence")
        model.answer = _vals({"key": "budget marketing", "value": "8000 francs", "turn": 3})
        cap.capture(A, "th_1", T2, "silence")
    assert svc.store.erase_account(A, TA) == 2
    with tenant_scope(TA):
        assert svc.listing(A)["notes"] == [] and svc.store.archived(svc.actor(A)) == []


def test_each_recall_search_is_journaled_even_without_result():
    svc, _cap = _world(Model(None))
    with tenant_scope(TA):
        svc.recall(A, "une question sans réponse", thread_id="th_9")
    log = svc.store.recall_log
    assert len(log) == 1 and log[0]["thread_id"] == "th_9" and log[0]["injected"] == 0


def test_parse_items_orders_by_turn_and_drops_unknown_turns():
    src = "Le budget est 5000. Le délai est 12 jours."
    obj = _vals({"key": "délai", "value": "12 jours", "turn": 2}, {"key": "budget", "value": "5000", "turn": 1},
                {"key": "x", "value": "5000", "turn": 7})
    assert [i.key for i in parse_items(obj, {1, 2}, src)] == ["budget", "délai"]


# ── déclenchement par le gestionnaire de contexte ──
def test_episode_close_and_import_trigger_the_capture(tmp_path):
    from tests.unit.context.harness import make_session
    from tests.unit.context.import_fixture import import_into

    s = make_session(tmp_path=tmp_path)
    seen: list = []
    s.manager.on_capture = lambda thread, turns, reason: seen.append((thread, len(turns), reason))
    s.say("bonjour")
    s.say("le budget est 5000")
    assert s.manager.new_episode(s.thread, "manual")
    assert seen and seen[-1][0] == s.thread and seen[-1][2] == "manual" and seen[-1][1] == 2

    s2 = make_session(tmp_path=tmp_path / "b" if (tmp_path / "b").mkdir() is None else tmp_path, thread="th_imp")
    imported: list = []
    s2.manager.on_capture = lambda thread, turns, reason: imported.append((thread, len(turns), reason))
    import_into(s2)
    s2.say("quelle est la suite ?")
    n_eps = len(s2.manager.episodes.list("th_imp"))
    assert imported and all(r == "import" for _, _, r in imported)
    assert len(imported) == n_eps  # TOUS les épisodes importés, sans plafond qui jette les anciens
    assert [i for i, _ in enumerate(imported)] == sorted(i for i, _ in enumerate(imported))


def test_a_failing_capture_hook_never_breaks_the_turn(tmp_path):
    from tests.unit.context.harness import make_session

    s = make_session(tmp_path=tmp_path)

    def boom(*_a):
        raise RuntimeError("capture en panne")

    s.manager.on_capture = boom
    s.say("bonjour")
    assert s.manager.new_episode(s.thread, "manual")
    assert s.say("encore").error is None


def test_a_revised_value_under_another_wording_of_the_key_supersedes_the_old_one():
    """Cas mesuré : « port de l'API » révisé sous une clé reformulée — l'ancienne valeur ne reste jamais vivante."""
    t1 = [_turn(1, "Le port de l'API est 8080.")]
    t2 = [_turn(4, "Finalement le port API passe de 8080 à 9090.")]
    model = Model(_vals({"key": "Port de l'API", "value": "8080", "turn": 1}))
    svc, cap = _world(model)
    with tenant_scope(TA):
        cap.capture(A, "th_1", t1, "silence")
        model.answer = _vals({"key": "port api", "value": "9090", "turn": 4})  # autre clé, même sujet
        assert cap.capture(A, "th_1", t2, "silence") == 1
        live = [n for n in svc.listing(A)["notes"] if n.get("key")]
        assert [n["value"] for n in live] == ["9090"]
        assert [n["value"] for n in svc.export(A)["archived"]] == ["8080"]
        recalled = svc.recall(A, "quel est le port de l'API ?")
    assert recalled and "9090" in recalled[0].text and "8080" not in recalled[0].text


def test_same_normalized_key_with_accents_and_case_is_the_same_key():
    svc, _cap = _world(Model(None))
    with tenant_scope(TA):
        svc.write_value(A, key="Délai de livraison", value="12 jours", title="délai", text="délai : 12 jours")
        r = svc.write_value(A, key="delai de LIVRAISON", value="15 jours", title="délai", text="délai : 15 jours")
        assert r.get("superseded") and len([n for n in svc.listing(A)["notes"] if n.get("key")]) == 1


def test_different_subjects_do_not_supersede_each_other():
    svc, _cap = _world(Model(None))
    with tenant_scope(TA):
        svc.write_value(A, key="port api", value="8080", title="p", text="port api : 8080")
        svc.write_value(A, key="port base", value="5432", title="p", text="port base : 5432")
        assert len([n for n in svc.listing(A)["notes"] if n.get("key")]) == 2
