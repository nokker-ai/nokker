"""Salle Maison (docs/HOUSE-ROOM.md) avec le vrai noyau et un faux modèle : intendant par défaut et consultations,
@mention, droits calculés sur le MEMBRE (pas sur la salle), cartes fortes individuelles, contenu privé absent de la
salle, `ask_house` sans élévation, événements entrants, audience adultes, suite d'une carte dans la salle."""
from __future__ import annotations

import pytest

from nokker.core.ports import ToolContext
from nokker.core.turn import Effect
from nokker.house.acl import ACL_RULE, STRONG_RULE
from nokker.house.room import (
    ASK_HOUSE_DESC,
    R_NO_HOUSE,
    AskHouseSource,
    HouseRoom,
)
from nokker.house.roster import UNAVAILABLE, Settings, mention_of
from nokker.team.model import TeamError
from nokker.tenancy.context import tenant_of_thread, tenant_scope
from tests.unit.house.conftest import (
    HOUSE_NOTE,
    PRIVATE_MAIL,
    PRIVATE_MEMORY,
    everything,
    make_world,
    messages,
    post,
)


def _cards(w, who):
    with tenant_scope(who.tenant_id):
        return w.approvals.pending()


def _verdicts(w, who, slug):
    thread = w.room.thread_of(_member(w, who), slug)
    with tenant_scope(who.tenant_id):
        return [v for t in w.kernel.store.history(thread) for v in t.verdicts]


def _denied(w, who, slug):
    from nokker.core.turn import Decision

    return any(v.decision is Decision.DENY and v.rule in (ACL_RULE, "agent:tool_not_declared")
               for v in _verdicts(w, who, slug))


def _member(w, who):
    with tenant_scope(who.tenant_id):
        return w.room.member(who.account_id)


# ─────────────────────────────── intendant, @mention ───────────────────────────────


def test_without_mention_the_intendant_answers_and_consults_the_right_agent(w):
    w.brain.plan["intendant"] = [("tool", "consulter", {"agent": "veille", "question": "du nouveau cette nuit ?"}),
                                 ("text", "Rien de grave : un mouvement à 03:12, à l'entrée.")]
    w.brain.plan["veille"] = [("tool", "veille_lire", {}), ("text", "Mouvement à 03:12 à l'entrée.")]
    out = post(w, w.adult, "Du nouveau cette nuit ?")
    assert out["reply"]["agent"] == "intendant" and "03:12" in out["reply"]["text"]
    rows = messages(w, w.pay)
    assert [(r["kind"], r["agent"]) for r in rows] == [("member", None), ("agent", "veille"), ("agent", "intendant")]
    assert all(r["reply_to"] == rows[0]["id"] for r in rows[1:])
    assert w.watch.calls == [("veille_lire", w.adult.account_id)]  # l'outil voit le membre, pas la salle
    # chaque membre actif reçoit chaque message ; personne d'un autre foyer
    tenants = {t for t, ev, _ in w.published if ev == "house.message"}
    assert tenants == {w.pay.tenant_id, w.adult.tenant_id, w.teen.tenant_id, w.child.tenant_id}
    assert {d["state"] for _, ev, d in w.published if ev == "house.agent"} == {"working", "idle"}


def test_mention_goes_to_that_agent_only(w):
    w.brain.plan["courrier"] = [("tool", "courrier_lire", {}), ("text", "Facture d'électricité, à payer le 15.")]
    out = post(w, w.pay, "@courrier qu'est-ce qui est arrivé ?")
    assert out["reply"]["agent"] == "courrier"
    assert "intendant" not in w.brain.plan or not w.brain.plan["intendant"]
    assert w.paper.calls == [("courrier_lire", w.pay.account_id)] and not w.watch.calls and not w.home.calls
    for text, slug in (("@Mémoire le portail ?", "memoire"), ("dis @veille", "veille"), ("@maison", "maison"),
                       ("salut @inconnu", None), ("a@veille.ch", None)):
        assert mention_of(text, Settings().agents) == slug


def test_agent_without_provider_answers_a_fixed_sentence_without_model(w):
    before = w.brain.calls
    out = post(w, w.adult, "@agenda on a quoi demain ?")
    assert out["reply"]["text"] == UNAVAILABLE.format(name="Agenda") and w.brain.calls == before


def test_memoire_reads_the_household_notes(w):
    w.brain.plan["memoire"] = [("tool", "memoire_foyer", {"query": "portail"}), ("text", "Le code change le 1er.")]
    post(w, w.teen, "@memoire le portail ?")
    assert HOUSE_NOTE in w.brain.prompts()


# ─────────────────────────────── droits par rôle ───────────────────────────────


def test_a_teen_does_not_unlock_the_door_even_in_the_room(w):
    w.brain.plan["maison"] = [("tool", "maison_fort", {}), ("text", "Je ne peux pas.")]
    post(w, w.teen, "@maison déverrouille la porte")
    assert w.home.calls == [] and _cards(w, w.teen) == []
    assert _denied(w, w.teen, "maison")  # outil hors de son droit : ni offert, ni exécutable
    # même demande par l'intendant, ou par ask_house : même refus (droits du membre, pas de la salle)
    w.brain.plan["intendant"] = [("tool", "consulter", {"agent": "maison", "question": "déverrouille"}), ("text", "ok")]
    w.brain.plan["maison"] = [("tool", "maison_fort", {}), ("text", "refusé")]
    post(w, w.teen, "déverrouille la porte")
    w.brain.plan["intendant"] = [("tool", "consulter", {"agent": "maison", "question": "déverrouille"}), ("text", "ok")]
    w.brain.plan["maison"] = [("tool", "maison_fort", {}), ("text", "refusé")]
    with tenant_scope(w.teen.tenant_id):
        AskHouseSource(w.room).call("ask_house", {"question": "déverrouille"},
                                    ToolContext(thread_id="x", turn_index=0, channel="web", sender=w.teen.account_id))
    assert w.home.calls == [] and _cards(w, w.teen) == []


def test_teen_ordinary_action_is_a_standard_card_child_reads_only(w):
    w.brain.plan["maison"] = [("tool", "maison_agir", {}), ("text", "Carte posée.")]
    post(w, w.teen, "@maison allume le salon")
    (card,) = _cards(w, w.teen)
    assert card.strength in (None, "standard")  # toc-toc possible : carte standard
    assert tenant_of_thread(card.thread_id) == w.teen.tenant_id
    w.brain.plan["maison"] = [("tool", "maison_agir", {}), ("tool", "maison_lire", {}), ("text", "Le salon est allumé.")]
    post(w, w.child, "@maison allume le salon")
    assert _cards(w, w.child) == [] and w.home.calls == [("maison_lire", w.child.account_id)]
    assert _denied(w, w.child, "maison")


def test_agents_reserved_to_adults_and_their_messages_hidden_from_minors(w):
    for who in (w.teen, w.child):
        with pytest.raises(TeamError) as e:
            post(w, who, "@courrier il y a du courrier ?")
        assert e.value.code == "agent_forbidden"
    with pytest.raises(TeamError):
        post(w, w.child, "@veille la caméra ?")  # veille : adultes et ados
    w.brain.plan["courrier"] = [("tool", "courrier_lire", {}), ("text", "Facture d'électricité.")]
    post(w, w.adult, "@courrier quoi de neuf ?")
    assert any("électricité" in r["text"] for r in messages(w, w.pay))
    for who in (w.teen, w.child):
        assert not any(r["agent"] == "courrier" or "électricité" in r["text"] for r in messages(w, who))
        assert not any("électricité" in str(d) for t, _, d in w.published if t == who.tenant_id)
    # l'intendant qui a consulté le courrier : sa synthèse non plus
    w.brain.plan["intendant"] = [("tool", "consulter", {"agent": "courrier", "question": "factures ?"}),
                                 ("text", "Une facture d'électricité.")]
    w.brain.plan["courrier"] = [("text", "Facture d'électricité.")]
    post(w, w.adult, "des factures ?")
    assert not any("électricité" in r["text"] for r in messages(w, w.teen))


def test_strong_action_is_an_individual_strong_card_even_with_a_written_go(w):
    w.brain.plan["maison"] = [("tool", "maison_fort", {}), ("text", "Il faut confirmer.")]
    out = post(w, w.adult, "@maison GO déverrouille la porte")
    (card,) = _cards(w, w.adult)
    assert card.strength == "strong" and tenant_of_thread(card.thread_id) == w.adult.tenant_id
    assert _cards(w, w.pay) == []  # le payeur ne la voit pas : elle est à la personne qui la demande
    assert w.home.calls == [] and any(v.rule == STRONG_RULE for v in _verdicts(w, w.adult, "maison"))
    assert out["reply"]["approvals"] == [card.id]  # elle seule les voit
    assert "approvals" not in messages(w, w.pay)[-2]
    assert any(r["event"] == "card.pending" for r in messages(w, w.pay))
    # décidée par la personne : l'outil part avec SON identité, la suite revient dans la salle
    w.brain.plan["maison"] = [("text", "Porte déverrouillée.")]
    with tenant_scope(w.adult.tenant_id):
        decided = w.approvals.decide_local(card.id, "approve", w.adult.account_id)
    w.executor.run(decided)
    assert w.home.calls == [("maison_fort", w.adult.account_id)]
    assert messages(w, w.pay)[-1]["text"] == "Porte déverrouillée."


def test_a_member_who_leaves_loses_the_room_at_once(w):
    post(w, w.adult, "@agenda bonjour")
    with tenant_scope(w.adult.tenant_id):
        w.hh.remove(w.adult, w.adult.account_id)
    for fn in (lambda: post(w, w.adult, "encore là ?"), lambda: messages(w, w.adult)):
        with pytest.raises(TeamError) as e:
            fn()
        assert e.value.status == 404
    with tenant_scope(w.adult.tenant_id):
        assert AskHouseSource(w.room).available_for(w.adult.tenant_id) is False


# ─────────────────────────────── contenu privé ───────────────────────────────


def test_private_memory_and_personal_mail_never_reach_the_room(w):
    # le noyau ordinaire du membre a bien sa mémoire de profil et son courrier (le canari est réel)
    assert PRIVATE_MEMORY in w.kernel.memory.recall("x")[0].text
    w.brain.plan["intendant"] = [("tool", "mail_perso", {}), ("tool", "memory_search", {"query": "x"}),
                                 ("text", "Je n'ai pas accès à ça.")]
    w.brain.plan["memoire"] = [("tool", "mail_perso", {}), ("text", "rien")]
    post(w, w.adult, "lis mon courrier perso et ma mémoire")
    post(w, w.adult, "@memoire et ma mémoire perso ?")
    assert PRIVATE_MEMORY not in w.brain.prompts() and PRIVATE_MAIL not in w.brain.prompts()
    assert PRIVATE_MEMORY not in everything(w) and PRIVATE_MAIL not in everything(w)
    with tenant_scope(w.adult.tenant_id):
        for slug in ("intendant", "memoire", "maison"):
            names = {s.name for s in w.room._kernel(w.room.settings.agents[slug], "strong").tools.core()}
            assert "mail_perso" not in names and "ask_house" not in names
    assert {s.name for s in w.room._kernel(w.room.settings.agents["maison"], "strong").tools.core()} == {
        "maison_lire", "maison_agir", "maison_fort"}
    assert {s.name for s in w.room._kernel(w.room.settings.agents["maison"], "read").tools.core()} == {"maison_lire"}


def test_house_tools_exist_only_in_the_room(w):
    assert w.member_tools.spec("maison_fort") is None and not any(
        s.name.startswith(("maison_", "courrier_", "veille_", "consulter")) for s in w.member_tools.core())


# ─────────────────────────────── ask_house ───────────────────────────────


def _ask(w, who, q, sender="self"):
    src = AskHouseSource(w.room)
    ctx = ToolContext(thread_id="web", turn_index=0, channel="web",
                      sender=who.account_id if sender == "self" else sender)
    with tenant_scope(who.tenant_id):
        return src.call("ask_house", {"question": q}, ctx)


def test_ask_house_is_offered_to_members_only_and_is_a_write():
    from nokker.house.room import AskHouseSource as S

    (spec,) = S(room=None).specs()  # type: ignore[arg-type]
    assert spec.effect is Effect.WRITE and spec.description == ASK_HOUSE_DESC  # carte en mode par défaut : geste


def test_ask_house_delegates_with_the_caller_rights_and_shows_provenance(w):
    src = AskHouseSource(w.room)
    assert src.available_for(w.adult.tenant_id) and src.available_for(w.teen.tenant_id)
    assert not src.available_for(w.lone.tenant_id)
    w.brain.plan["intendant"] = [("tool", "consulter", {"agent": "veille", "question": "caméras ?"}),
                                 ("text", "Tout est calme.")]
    w.brain.plan["veille"] = [("tool", "veille_lire", {}), ("text", "Calme.")]
    r = _ask(w, w.adult, "Tout est calme à la maison ?")
    assert not r.is_error and "Tout est calme." in r.content
    rows = messages(w, w.pay)
    assert rows[0]["origin"] == {"kind": "ask_house", "channel": "web"} and rows[0]["author"] == "membre B (adulte)"
    assert rows[-1]["origin"]["kind"] == "ask_house"


def test_ask_house_without_house_or_with_a_foreign_sender_gains_nothing(w):
    n = len(w.room.store.rows)
    r = _ask(w, w.lone, "déverrouille la porte")
    assert r.is_error and r.content == R_NO_HOUSE
    r = _ask(w, w.teen, "déverrouille", sender=w.pay.account_id)  # se faire passer pour le payeur
    assert r.is_error and r.content == R_NO_HOUSE
    assert len(w.room.store.rows) == n and w.home.calls == []


def test_ask_house_adult_strong_action_stays_a_card(w):
    w.brain.plan["intendant"] = [("tool", "consulter", {"agent": "maison", "question": "déverrouille"}),
                                 ("text", "Une carte attend ta décision.")]
    w.brain.plan["maison"] = [("tool", "maison_fort", {}), ("text", "Carte posée.")]
    r = _ask(w, w.adult, "déverrouille la porte")
    assert not r.is_error and w.home.calls == []
    (card,) = _cards(w, w.adult)
    assert card.strength == "strong"


# ─────────────────────────────── événements ───────────────────────────────


def test_events_enter_the_room_as_agent_messages(w):
    m = _member(w, w.pay)
    w.room.post_event(m.household_id, m.household_tenant, "camera.incident", "Mouvement devant le garage à 02:40.")
    w.room.post_event(m.household_id, m.household_tenant, "mail.arrived", "Lettre de la caisse maladie.",
                      audience="all")  # l'audience d'un agent pour adultes ne s'élargit pas
    w.room.post_event(m.household_id, m.household_tenant, "memory.report", "3 notes fusionnées cette nuit.")
    rows = messages(w, w.pay)
    assert [(r["kind"], r["agent"], r["event"]) for r in rows] == [
        ("event", "veille", "camera.incident"), ("event", "courrier", "mail.arrived"),
        ("event", "memoire", "memory.report")]
    assert [r["agent"] for r in messages(w, w.teen)] == ["veille", "memoire"]
    with pytest.raises(ValueError):
        w.room.post_event(m.household_id, m.household_tenant, "door.open", "x")
    # les agents voient les événements dans le fil de la salle, comme une DONNÉE
    post(w, w.adult, "@veille et cette nuit ?")
    assert "événement camera.incident" in w.brain.prompts() and "untrusted-data" in w.brain.prompts()


# ─────────────────────────────── never clear, réglages ───────────────────────────────


def test_room_is_persistent_and_each_agent_keeps_its_thread(w):
    post(w, w.adult, "@agenda un")
    post(w, w.pay, "@agenda deux")
    rows = messages(w, w.teen)
    assert [r["text"] for r in rows if r["kind"] == "member"] == ["@agenda un", "@agenda deux"]
    assert messages(w, w.teen, after=rows[1]["id"])[0]["id"] == rows[2]["id"]
    assert w.room.thread_of(_member(w, w.adult), "maison") == w.room.thread_of(_member(w, w.adult), "maison")
    assert tenant_of_thread(w.room.thread_of(_member(w, w.adult), "maison")) == w.adult.tenant_id


def test_settings_refuse_unsafe_rights():
    with pytest.raises(ValueError):
        Settings.from_config({"rights": {"teen": "strong"}})
    with pytest.raises(ValueError):
        Settings.from_config({"rights": {"child": "act"}})
    with pytest.raises(ValueError):
        Settings.from_config({"agents": {"intendant": {"enabled": False}}})
    with pytest.raises(ValueError):
        Settings.from_config({"oops": 1})
    s = Settings.from_config({"agents": {"veille": {"profiles": ["adult"], "tier": "deep"}}})
    assert s.agents["veille"].profiles == ("adult",) and s.agents["veille"].tier.value == "deep"


def test_post_text_is_bounded_and_needs_a_household(w):
    with pytest.raises(TeamError):
        post(w, w.adult, "")
    with pytest.raises(TeamError):
        post(w, w.adult, "x" * 2001)
    with pytest.raises(TeamError) as e:
        post(w, w.lone, "bonjour")
    assert e.value.status == 404


def test_room_class_is_importable_without_flag():
    assert HouseRoom.__name__ == "HouseRoom" and make_world
