"""Garde « c'est fait » (core/action_claims.py) : une réponse qui affirme une action accomplie sans qu'aucun outil à effet
n'ait abouti dans le tour est corrigée. Faux modèle, vrai noyau ; reconnaissance et faux positifs testés à part."""
from __future__ import annotations

import pytest

from nokker.core import action_claims as ac
from nokker.core import person_texts as ptx
from nokker.core.turn import APPROVAL_CONTINUE, Effect, TurnInput
from tests.unit.test_note_claims import guard_world

SPECS = {"home_light": Effect.WRITE, "home_state": Effect.READ, "mail_send": Effect.SEND_EXTERNAL,
         "memory_note": Effect.READ}
DONE = {"fr": "J'ai allumé la lampe et la prise de test.", "en": "I turned on the lamp and the test plug.",
        "de": "Ich habe die Lampe und die Teststeckdose eingeschaltet.", "it": "Ho acceso la lampada e la presa di prova."}


def run(reply, calls=(), results=None, lang=None, guard=True, tid="th_a"):
    k = guard_world(reply, calls, SPECS, results)
    k.action_claims_guard = guard
    return k.run_turn(TurnInput(thread_id=tid, user_text="allume la lampe", lang=lang))


def fixed(lang, why):
    return ptx.pick(ptx.ACTION_NOT_DONE, lang).format(why=ptx.pick(ptx.ACTION_WHY[why], lang))


@pytest.mark.parametrize("text", [
    "J'ai allumé la lampe.", "Je l'ai éteinte.", "J'ai bien envoyé le message.", "Je viens de lancer la sauvegarde.",
    "C'est fait !", "C'est envoyé.", "Fait.", "Voilà, c'est fait.", "J'ai supprimé le fichier et créé un dossier.",
    "I turned on the light.", "I've sent it.", "I have just deleted the file.", "I switched the lamp off.", "Done.",
    "All done!", "It's done.", "The message has been sent.", "I scheduled the meeting.",
    "Ich habe die Lampe eingeschaltet.", "Ich habe die Datei gelöscht.", "Erledigt.", "Die Nachricht wurde gesendet.",
    "Ho acceso la luce.", "L'ho spento.", "Ho inviato il messaggio.", "Fatto!", "È fatto.", "Ho prenotato il tavolo."])
def test_claims_recognised(text):
    assert ac.claims_done(text)


@pytest.mark.parametrize("text", [
    # négations
    "Je n'ai pas encore envoyé le message.", "Je n'ai pas allumé la lampe.", "Ce n'est pas fait.", "Pas encore envoyé.",
    "I haven't sent it yet.", "I didn't turn on the light.", "Not done yet.", "I can't confirm I sent it.",
    "Ich habe die Lampe noch nicht eingeschaltet.", "Ich habe nichts gesendet.", "Non ho ancora acceso la luce.",
    "Non ho inviato nulla.", "Rien n'est fait.",
    # questions et propositions
    "Tu veux que j'allume la lampe ?", "Did I turn on the light?", "Je peux l'allumer si tu veux.",
    "I can turn it on for you.", "Soll ich sie einschalten?", "Posso accenderla.", "Je l'allume tout de suite.",
    # hypothèse, passé d'un autre moment
    "Si j'ai envoyé le message, tu le verras dans ta boîte.", "Hier j'ai envoyé le rapport.",
    "Yesterday I sent the report.", "Gestern habe ich die Datei gelöscht.", "Ieri ho inviato il file.",
    "If I sent it, you will see it.", "Last week I deleted the old file.",
    # citation, code
    "Tu as écrit « j'ai allumé la lampe » ?", "La phrase `I turned on the light` est au passé.",
    "Voici le code :\n```\nprint('j'ai allumé')\n```",
    # rien d'une action
    "Avec plaisir !", "De rien.", "La lampe est allumée depuis midi.", "Il fait sombre dans le salon.",
    "Je vais envoyer le message.", "J'ai trouvé trois résultats.", "I found three results.", "Ich habe drei Treffer gefunden.",
    # le texte de remplacement lui-même
    *[ptx.ACTION_NOT_DONE[lg].format(why=w[lg]) for lg in ("fr", "en", "de", "it") for w in ptx.ACTION_WHY.values()]])
def test_not_claims(text):
    assert not ac.claims_done(text)


def test_only_the_first_sentences_count():
    assert not ac.claims_done("Voici le contexte. Autre chose encore. Plus tard, j'ai envoyé le message.")


@pytest.mark.parametrize("lang", ["fr", "en", "de", "it"])
def test_claim_without_any_tool_is_replaced(lang):
    t = run(DONE[lang], lang=lang)
    assert t.reply == fixed(lang, "none")
    assert [m.content for m in t.messages if m.role == "assistant"][-1] == t.reply


def test_claim_after_a_refused_call_is_replaced():
    t = run("J'ai allumé la lampe.", [("home_light", {})],
            {"home_light": ("Refusé : À confirmer… appelle l'action forte.", False)})
    assert t.reply == fixed("fr", "refused")


def test_claim_after_a_pending_card_is_replaced():
    t = run("J'ai allumé la lampe.", [("home_light", {})],
            {"home_light": ("EN ATTENTE D'APPROBATION HUMAINE (envoi) [approval:x]. ", False)})
    assert t.reply == fixed("fr", "pending") and "carte" in t.reply


def test_claim_after_a_failed_tool_is_replaced():
    t = run("C'est fait !", [("home_light", {})], {"home_light": ("délai dépassé", True)})
    assert t.reply == fixed("fr", "failed")


def test_claim_after_a_read_only_call_is_replaced():
    assert run("C'est fait !", [("home_state", {})]).reply == fixed("fr", "none")


def test_real_success_is_left_alone():
    t = run("J'ai allumé la lampe.", [("home_light", {})])
    assert t.reply == "J'ai allumé la lampe."
    assert run("Done.", [("mail_send", {})], lang="en").reply == "Done."


def test_partial_success_is_left_alone():
    t = run("J'ai allumé la lampe et la prise.", [("home_light", {}), ("home_light", {})],
            {"home_light": ("ok", False)})
    assert t.reply == "J'ai allumé la lampe et la prise."


def test_guard_is_off_by_default():
    assert run("J'ai allumé la lampe.", guard=False).reply == "J'ai allumé la lampe."


def test_medium_reply_is_completed_not_replaced():
    text = "J'ai allumé la lampe. " + "Le salon est maintenant éclairé comme tu le souhaitais, bonne soirée. " * 3
    assert 160 < len(text) <= ac.MAX_REPLY
    t = run(text)
    assert t.reply.startswith(text.rstrip()) and t.reply.endswith(fixed("fr", "none"))


def test_drafted_text_is_never_touched():
    long = "J'ai ajouté une section sur les risques. " + "Voici le détail de ce que contient le document. " * 12
    assert run(long).reply == long
    listed = "J'ai créé le plan :\n- étape un\n- étape deux"
    assert run(listed).reply == listed


def test_a_turn_with_only_a_question_to_the_person_is_untouched():
    assert run("Tu veux que j'allume la lampe ?").reply == "Tu veux que j'allume la lampe ?"


def test_continuation_of_a_decided_card_is_left_alone():
    k = guard_world("C'est fait, le message est parti.", (), SPECS)
    k.action_claims_guard = True
    t = k.run_turn(TurnInput(thread_id="th_cont", user_text="[Approbation]\nOutil : mail_send\nDécision : executed",
                             control=APPROVAL_CONTINUE))
    assert t.reply == "C'est fait, le message est parti."


# ───────────────────────── par palier : allumée d'office sur `nina` ─────────────────────────

def run_on(tier, reply="J'ai allumé la lampe.", global_flag=False, per_tier=None, tid="th_t"):
    from nokker.core.ports import RouteDecision
    from nokker.core.turn import Tier
    from tests.unit.test_note_claims import Scripted

    k = guard_world(reply, (), SPECS)
    k.providers = {Tier.SHIP: Scripted(reply), tier: Scripted(reply)}
    k.policy = type("P", (), {"choose": lambda self, state: RouteDecision(tier=tier, reasoning=False, reason="t")})()
    k.action_claims_guard = global_flag
    if per_tier is not None:
        k.tier_action_claims.update(per_tier)
    return k.run_turn(TurnInput(thread_id=tid, user_text="allume la lampe"))


def test_nina_tier_turns_the_guard_on_by_itself():
    from nokker.core.turn import Tier

    t = run_on(Tier.NINA)
    assert t.reply == fixed("fr", "none")
    assert [m.content for m in t.messages if m.role == "assistant"][-1] == fixed("fr", "none")  # l'historique ne garde pas le faux


def test_other_tiers_keep_the_global_behaviour_off():
    from nokker.core.turn import Tier

    for tier in (Tier.SHIP, Tier.DEEP, Tier.SMALL):
        assert run_on(tier).reply == "J'ai allumé la lampe."


def test_global_flag_still_applies_to_every_tier():
    from nokker.core.turn import Tier

    assert run_on(Tier.SHIP, global_flag=True).reply == fixed("fr", "none")


def test_tier_setting_overrides_both_ways():
    from nokker.core.turn import Tier

    assert run_on(Tier.NINA, per_tier={"nina": False}).reply == "J'ai allumé la lampe."  # `tiers.nina.action_claims_guard: false`
    assert run_on(Tier.DEEP, per_tier={"deep": True}).reply == fixed("fr", "none")
    assert run_on(Tier.SHIP, global_flag=True, per_tier={"ship": False}).reply == "J'ai allumé la lampe."


def test_tier_config_parses_tristate(tmp_path):
    from nokker.config import load_config

    p = tmp_path / "c.yaml"
    p.write_text("tiers:\n  ship: {base_url: http://x, model: m}\n  nina: {base_url: http://x, model: m, action_claims_guard: false}\n"
                 "  deep: {base_url: http://x, model: m, action_claims_guard: true}\n")
    tiers = load_config(str(p)).tiers
    assert (tiers["ship"].action_claims_guard, tiers["nina"].action_claims_guard, tiers["deep"].action_claims_guard) == (None, False, True)
    p.write_text("tiers:\n  ship: {base_url: http://x, model: m, action_claims_guard: 'false'}\n")
    with pytest.raises(ValueError):
        load_config(str(p))
