"""Qualité de la capture (banc « décisions révisées », critère mémoire) : (i) la garde anti-invention porte sur la clé et la
valeur, jamais sur la note qui retire seulement son texte ; (ii) le prompt d'extraction reçoit les clés vivantes ;
(iii) deux clés vivantes du même sujet sont réconciliées (la plus récente gagne)."""
from __future__ import annotations

from nokker.memory.tenant_long import LongNote, same_subject, value_type
from nokker.memory.values import SYSTEM, parse_items
from nokker.tenancy.context import tenant_scope

from .test_values_capture import A, T1, TA, Model, _vals, _world


def test_a_note_citing_an_absent_element_loses_its_text_but_the_entry_is_kept():
    src = "Le budget marketing est fixé à 5000 francs."
    obj = {"values": [{"kind": "decision", "key": "budget marketing", "value": "5000 francs", "turn": 1,
                       "note": "validé lors de la réunion du 14 mars avec Mme Fontana"}]}
    items = parse_items(obj, {1}, src)
    assert [(i.key, i.value, i.note) for i in items] == [("budget marketing", "5000 francs", "")]


def test_an_invented_key_or_value_still_discards_the_entry():
    src = "Le budget marketing est fixé à 5000 francs."
    for bad in ({"key": "budget marketing", "value": "7777 francs"}, {"key": "budget 2031 marketing", "value": "5000 francs"}):
        obj = {"values": [{"kind": "decision", "turn": 1, **bad}]}
        assert parse_items(obj, {1}, src) == []


def test_the_prompt_reuses_known_keys_and_forbids_variants():
    assert "MÊME clé" in SYSTEM or "TOUJOURS la même clé" in SYSTEM and "variante" in SYSTEM
    model = Model(_vals({"key": "budget marketing", "value": "5000 francs", "turn": 1}))
    svc, cap = _world(model)
    with tenant_scope(TA):
        cap.capture(A, "th_1", T1, "silence")
        cap.capture(A, "th_1", T1, "silence")
    asked = model.asked[-1][-1]["content"]
    assert "- budget marketing : 5000 francs" in asked


def _live(svc, key, value, at):
    actor = svc.actor(A)
    n = LongNote(name=f"fact-{key.replace(' ', '-')}-{int(at)}", scope="user", kind="fact", title=key, text=f"{key} : {value}",
                 account_id=actor.account_id, tenant_id=actor.tenant_id, author_account=actor.account_id,
                 created_at=at, updated_at=at, key=key, value=value)
    svc.store.put(actor, n, None, None)


def test_reconcile_never_merges_distinct_quantities_by_word_inclusion():
    svc, _cap = _world(Model(None))
    with tenant_scope(TA):
        _live(svc, "delai livraison", "21 jours", 1000.0)
        _live(svc, "delai livraison express", "48 heures", 2000.0)
        _live(svc, "budget marketing", "5000 francs", 1000.0)
        _live(svc, "budget marketing campagne de rentree", "8000 francs", 2000.0)
        assert svc.reconcile_values(A) == 0
        assert len([n for n in svc.listing(A)["notes"] if n.get("key")]) == 4  # TOUTES vivantes


def test_reconcile_merges_only_equal_normalized_keys():
    svc, _cap = _world(Model(None))
    with tenant_scope(TA):
        _live(svc, "Port de l'API", "8080", 1000.0)  # écrite avant la normalisation
        _live(svc, "port de l api", "9090", 2000.0)
        assert svc.reconcile_values(A) == 1
        live = [n for n in svc.listing(A)["notes"] if n.get("key")]
        assert [n["value"] for n in live] == ["9090"] and svc.reconcile_values(A) == 0


def test_capture_keeps_distinct_quantities_alive_without_a_declared_replacement():
    model = Model(_vals({"key": "delai livraison", "value": "5000 francs", "turn": 1}))
    svc, cap = _world(model)
    with tenant_scope(TA):
        cap.capture(A, "th_1", T1, "import")
        model.answer = _vals({"key": "delai livraison express", "value": "5000 francs", "turn": 1})
        cap.capture(A, "th_1", T1, "import")
        assert sorted(n["key"] for n in svc.listing(A)["notes"] if n.get("key")) == [
            "delai livraison", "delai livraison express"]


def test_a_declared_replaces_key_supersedes_and_an_unknown_one_is_ignored():
    model = Model(_vals({"key": "budget marketing", "value": "5000 francs", "turn": 1}))
    svc, cap = _world(model)
    with tenant_scope(TA):
        cap.capture(A, "th_1", T1, "silence")
        model.answer = _vals({"key": "budget marketing campagne de rentree", "value": "5000 francs", "turn": 1,
                              "replaces_key": "cle inexistante"})
        cap.capture(A, "th_1", T1, "silence")
        assert len([n for n in svc.listing(A)["notes"] if n.get("key")]) == 2  # reprise non déclarée valablement
        model.answer = _vals({"key": "budget campagne", "value": "5000 francs", "turn": 1,
                              "replaces_key": "budget marketing"})
        cap.capture(A, "th_1", T1, "silence")
        keys = sorted(n["key"] for n in svc.listing(A)["notes"] if n.get("key"))
        assert keys == ["budget campagne", "budget marketing campagne de rentree"]  # « budget marketing » reprise, archivée


def test_parse_items_validates_replaces_key_against_live_keys():
    src = "Le budget est 5000 francs."
    obj = {"values": [{"kind": "decision", "key": "budget campagne", "value": "5000 francs", "turn": 1,
                       "replaces_key": "Budget  Marketing"}]}
    assert parse_items(obj, {1}, src, {"budget marketing"})[0].replaces_key == "budget marketing"
    assert parse_items(obj, {1}, src, {"autre"})[0].replaces_key == ""
    assert parse_items(obj, {1}, src)[0].replaces_key == ""


def test_proximity_net_needs_equal_words_same_head_and_same_value_type():
    assert same_subject("port de l'API", "port api", "8080", "9090")
    assert not same_subject("sauvegarde nas", "frequence sauvegarde nas")  # inclusion de mots seule : jamais
    assert not same_subject("delai livraison", "delai livraison express")
    assert not same_subject("port api", "port base")
    assert not same_subject("port api", "port de l'API", "8080", "à définir")  # types différents : coexistence


def test_value_types():
    assert value_type("5000 francs") == "number" and value_type("8080") == "number" and value_type("12 h") == "number"
    assert value_type("2026-11-22") == "date" and value_type("14.03.2026") == "date"
    assert value_type("D415") == "identifier" and value_type("v2.1") == "identifier"
    assert value_type("toutes les 12 heures") == "text" and value_type("Mme Fontana") == "text"


def test_superseded_values_list_the_final_value_and_other_values_follow_the_recall_marked():
    model = Model(_vals({"key": "budget marketing", "value": "5000 francs", "turn": 1}))
    svc, cap = _world(model)
    with tenant_scope(TA):
        cap.capture(A, "th_1", T1, "silence")
        svc.write_value(A, key="budget marketing", value="8000 francs", title="b", text="budget marketing : 8000 francs")
        sup = svc.superseded_values(A)
        assert [(s["old"], s["new"]) for s in sup] == [("5000 francs", "8000 francs")]
        # une question sans mot commun : le NOM de la valeur suit (jamais sa valeur : hors score, elle serait recopiée)
        out = svc.recall(A, "quelle somme a-t-on arrêtée pour la publicité ?")
        assert any("8000" in r.text and "peut ne pas concerner" in r.text for r in out)  # « marked » (défaut) : marquée
        assert not any("5000" in r.text for r in out)  # jamais l'ancienne valeur
        svc.settings.always_values_max = 0
        assert not any("budget marketing" in r.text for r in svc.recall(A, "zzz qqq"))
