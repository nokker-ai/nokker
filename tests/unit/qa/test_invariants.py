"""Tests de l'implémentation du port Invariants (F3, nokker/qa/invariants.py)."""
from __future__ import annotations

from nokker.core.ports import ToolSpec
from nokker.core.turn import Decision, Effect, InputCheck, Turn, ToolCall
from nokker.qa.invariants import Allowlist, DeterministicInvariants, has_explicit_go
from nokker.qa.secrets import KnownSecrets


FAKE_PASSWORD = "Xk9!qpzT42Lm"


def turn_with_text(text: str, sender: str | None = None) -> Turn:
    return Turn(thread_id="t1", index=0, input=InputCheck(text=text), sender=sender)


def tc(name: str, **args) -> ToolCall:
    return ToolCall(id=f"c-{name}", name=name, arguments=args)


def spec(name: str, effect: Effect) -> ToolSpec:
    return ToolSpec(name=name, description="d", parameters={}, effect=effect)


def inv(**kw) -> DeterministicInvariants:
    return DeterministicInvariants(**kw)


# ───────────────────────── check_input : caviardage + drapeaux ─────────────────────────


def test_check_input_redacts_known_secret_before_any_storage():
    known = KnownSecrets()
    known.add("VAULT_GRAFANA", FAKE_PASSWORD)
    checked = inv(known=known).check_input(f"redonne-moi le mot de passe {FAKE_PASSWORD}")
    assert FAKE_PASSWORD not in checked.text
    assert checked.redacted == ["VAULT_GRAFANA"]


def test_check_input_fiscal_flag_fr_en_de():
    i = inv()
    assert "fiscal" in i.check_input("Dois-je facturer la TVA suisse à ce client ?").domain_flags
    assert "fiscal" in i.check_input("What's the VAT rate for this invoice?").domain_flags
    assert "fiscal" in i.check_input("Wie hoch ist die Mehrwertsteuer (MwSt) hier?").domain_flags


def test_check_input_juridique_flag_fr_en_de():
    i = inv()
    assert "juridique" in i.check_input("Il faut résilier ce contrat avec le fournisseur.").domain_flags
    assert "juridique" in i.check_input("We need to review this contract clause with a lawyer.").domain_flags
    assert "juridique" in i.check_input("Wir müssen den Vertrag mit dem Anwalt prüfen.").domain_flags


def test_check_input_montant_flag_fr_en_de():
    i = inv()
    assert "montant" in i.check_input("Il faut faire le virement pour cette facture.").domain_flags
    assert "montant" in i.check_input("Please process the payment for this invoice.").domain_flags
    assert "montant" in i.check_input("Bitte die Überweisung für diese Rechnung ausführen.").domain_flags


def test_check_input_no_domain_flag_on_bare_password_mention():
    i = inv()
    checked = i.check_input("Où est rangé le mot de passe Grafana déjà ?")
    assert checked.domain_flags == set()
    assert checked.redacted == []


def test_check_input_no_domain_flag_on_ordinary_message():
    i = inv()
    checked = i.check_input("Décale la réunion d'équipe de jeudi à vendredi, même heure.")
    assert checked.domain_flags == set()


def test_check_input_flag_precise_not_on_generic_amount_word():
    # « montant » seul (le chiffre d'un truc quelconque) ne doit pas suffire : il faut le signal
    # paiement/facture/virement/IBAN, pas juste un nombre dans la phrase.
    i = inv()
    checked = i.check_input("Le montant du devis est de 42 pages, enfin je crois.")
    assert checked.domain_flags == set()


# ───────────────────────── GO explicite (destructif) ─────────────────────────


def test_go_example_from_prefix_doc_is_detected():
    # exemple littéral du préfixe Atlas (invariant 4) : « vas-y, agrandis /var de 20G sur hote21 »
    call = tc("infra_lvextend", path="/var", size="20G", host="hote21")
    assert has_explicit_go("vas-y, agrandis /var de 20G sur hote21", call)


def test_go_requires_marker_not_just_named_params():
    call = tc("infra_lvextend", path="/var", size="20G", host="hote21")
    assert not has_explicit_go("il faudrait agrandir /var de 20G sur hote21 un jour", call)


def test_go_requires_params_not_just_marker():
    call = tc("infra_lvextend", path="/var", size="20G", host="hote21")
    assert not has_explicit_go("vas-y, fais ce qu'il faut", call)


def test_go_fails_if_one_param_unnamed():
    call = tc("infra_lvextend", path="/var", size="20G", host="hote1")  # host différent du texte
    assert not has_explicit_go("vas-y, agrandis /var de 20G sur hote21", call)


def test_go_without_any_argument_is_never_confirmed():
    call = tc("infra_lvextend")
    assert not has_explicit_go("vas-y, lance l'extension du volume maintenant", call)


def test_destructive_without_go_is_hitl():
    i = inv()
    turn = turn_with_text("le disque est plein, qu'est-ce qu'on fait ?")
    v = i.check_action(tc("infra_lvextend", path="/srv/share", size="50G", host="hote21"),
                       spec("infra_lvextend", Effect.DESTRUCTIVE), turn)
    assert v.decision is Decision.HITL
    assert v.strength == "strong"  # P1 docs/APPROVALS.md §1 : DESTRUCTIVE -> preuve forte seulement


def test_destructive_with_explicit_go_from_authorized_sender_is_allowed():
    i = inv()
    turn = turn_with_text("On a vu /srv/share à 87%. Ok, vas-y, lvextend +50G sur hote21, volume /srv/share.",
                          sender="alice@example.ch")
    v = i.check_action(tc("infra_lvextend", path="/srv/share", size="50G", host="hote21"),
                       spec("infra_lvextend", Effect.DESTRUCTIVE), turn)
    assert v.decision is Decision.ALLOW
    assert v.strength is None  # ALLOW : pas de carte d'approbation à poser, rien à prouver


def test_destructive_with_explicit_go_from_bruno_is_allowed():
    # Décision Alice 17.10 : le GO de Bruno vaut celui de Alice.
    i = inv()
    turn = turn_with_text("On a vu /srv/share à 87%. Ok, vas-y, lvextend +50G sur hote21, volume /srv/share.",
                          sender="bruno@example.ch")
    v = i.check_action(tc("infra_lvextend", path="/srv/share", size="50G", host="hote21"),
                       spec("infra_lvextend", Effect.DESTRUCTIVE), turn)
    assert v.decision is Decision.ALLOW


def test_destructive_with_explicit_go_from_unauthorized_sender_is_hitl():
    i = inv()
    turn = turn_with_text("On a vu /srv/share à 87%. Ok, vas-y, lvextend +50G sur hote21, volume /srv/share.",
                          sender="un.tiers@media-exemple.com")
    v = i.check_action(tc("infra_lvextend", path="/srv/share", size="50G", host="hote21"),
                       spec("infra_lvextend", Effect.DESTRUCTIVE), turn)
    assert v.decision is Decision.HITL
    assert v.rule == "effect:destructive:unauthorized_sender"
    assert v.strength == "strong"


def test_destructive_with_explicit_go_and_no_sender_at_all_is_hitl():
    # Un canal qui ne porte pas d'identité (sender=None) ne peut jamais donner de GO.
    i = inv()
    turn = turn_with_text("On a vu /srv/share à 87%. Ok, vas-y, lvextend +50G sur hote21, volume /srv/share.")
    v = i.check_action(tc("infra_lvextend", path="/srv/share", size="50G", host="hote21"),
                       spec("infra_lvextend", Effect.DESTRUCTIVE), turn)
    assert v.decision is Decision.HITL


def test_go_senders_configurable_with_custom_list():
    from nokker.qa.invariants import GoAuthorizedSenders

    i = inv(go_senders=GoAuthorizedSenders(senders=frozenset({"someone-else@example.ch"})))
    turn = turn_with_text("vas-y, lvextend +50G sur hote21, volume /srv/share.", sender="alice@example.ch")
    v = i.check_action(tc("infra_lvextend", path="/srv/share", size="50G", host="hote21"),
                       spec("infra_lvextend", Effect.DESTRUCTIVE), turn)
    assert v.decision is Decision.HITL  # Alice n'est plus dans la liste custom : plus de GO par défaut


def test_tool_without_spec_is_treated_as_write_not_destructive():
    i = inv()
    turn = turn_with_text("fais un truc")
    v = i.check_action(tc("un_outil_mcp_inconnu", x=1), None, turn)
    assert v.decision is Decision.ALLOW
    assert v.rule == "effect:write"


# ───────────────────────── SEND_EXTERNAL / allowlist ─────────────────────────


def test_send_external_to_allowlisted_address_is_allowed():
    i = inv(allowlist=Allowlist.load(addresses=["alice@example.ch"]))
    turn = turn_with_text("transfère ça à Alice")
    v = i.check_action(tc("send_email_forward", to="alice@example.ch"),
                       spec("send_email_forward", Effect.SEND_EXTERNAL), turn)
    assert v.decision is Decision.ALLOW


def test_send_external_to_unknown_address_is_hitl():
    i = inv(allowlist=Allowlist.load(addresses=["alice@example.ch"]))
    turn = turn_with_text("réponds au journaliste")
    v = i.check_action(tc("send_email_reply", to="journaliste@media-exemple.com"),
                       spec("send_email_reply", Effect.SEND_EXTERNAL), turn)
    assert v.decision is Decision.HITL
    assert "media-exemple.com" in v.reason
    assert v.strength == "standard"  # P1 docs/APPROVALS.md §1 : strong OU TOTP acceptés


def test_send_external_allowlist_by_domain():
    i = inv(allowlist=Allowlist.load(domains=["example.ch"]))
    turn = turn_with_text("envoie à bruno")
    v = i.check_action(tc("send_email_reply", to="bruno@example.ch"),
                       spec("send_email_reply", Effect.SEND_EXTERNAL), turn)
    assert v.decision is Decision.ALLOW


def test_send_external_without_identifiable_recipient_is_hitl():
    i = inv()
    turn = turn_with_text("envoie une réponse")
    v = i.check_action(tc("send_email_reply", body="salut"),
                       spec("send_email_reply", Effect.SEND_EXTERNAL), turn)
    assert v.decision is Decision.HITL


# ───────────────────────── SEND_BULK : toujours refusé ─────────────────────────


def test_send_bulk_always_denied_even_with_go_marker():
    i = inv()
    turn = turn_with_text("vas-y, envoie le mail promo à toute la liste de prospects, j'autorise")
    v = i.check_action(tc("send_bulk_email", list="prospects.csv"),
                       spec("send_bulk_email", Effect.SEND_BULK), turn)
    assert v.decision is Decision.DENY
    assert v.strength is None  # jamais soumis à approbation (DENY, pas HITL) : pas de niveau de preuve


# ───────────────────────── SPEND / SECRET ─────────────────────────


def test_spend_without_go_is_hitl():
    i = inv()
    turn = turn_with_text("achète plus de crédits sur le Router")
    v = i.check_action(tc("router_topup", amount=50),
                       spec("router_topup", Effect.SPEND), turn)
    assert v.decision is Decision.HITL
    assert v.strength == "standard"


def test_secret_effect_is_allow_with_transform_instruction():
    i = inv()
    turn = turn_with_text("redonne-moi les identifiants Grafana")
    v = i.check_action(tc("grafana_get_credentials"),
                       spec("grafana_get_credentials", Effect.SECRET), turn)
    assert v.decision is Decision.ALLOW
    assert "poignée" in v.reason


# ───────────────────────── scrub_output ─────────────────────────


def test_scrub_output_removes_known_secret_from_reply():
    known = KnownSecrets()
    known.add("VAULT_GRAFANA", FAKE_PASSWORD)
    i = inv(known=known)
    reply = i.scrub_output(f"Tiens, ton mot de passe est {FAKE_PASSWORD}.")
    assert FAKE_PASSWORD not in reply
    assert "[secret:VAULT_GRAFANA]" in reply


# ───────────────────────── redact_result (contrat main e076d54) ─────────────────────────


def test_redact_result_secret_effect_uses_full_treatment():
    from nokker.core.turn import ToolResult

    known = KnownSecrets()
    known.add("VAULT_GRAFANA", FAKE_PASSWORD)
    i = inv(known=known)
    result = ToolResult(call_id="c1", name="grafana_get_credentials",
                        content=f"user admin / mot de passe {FAKE_PASSWORD}")
    out = i.redact_result(result, spec("grafana_get_credentials", Effect.SECRET))
    assert FAKE_PASSWORD not in out.content
    assert "VAULT_GRAFANA" in out.secret_handles


def test_redact_result_non_secret_effect_still_catches_known_value():
    # Filet de sécurité : même un outil étiqueté READ/WRITE ne doit pas laisser fuiter une
    # valeur du COFFRE (une mauvaise étiquette ne doit pas suffire à faire fuiter un secret).
    from nokker.core.turn import ToolResult

    known = KnownSecrets()
    known.add("VAULT_GRAFANA", FAKE_PASSWORD)
    i = inv(known=known)
    result = ToolResult(call_id="c1", name="infra_disk_usage", content=f"erreur, clé utilisée: {FAKE_PASSWORD}")
    out = i.redact_result(result, spec("infra_disk_usage", Effect.READ))
    assert FAKE_PASSWORD not in out.content


def test_redact_result_non_secret_effect_does_not_apply_weak_entropy_fallback():
    # weak=False pour le non-SECRET : un token technique à haute entropie dans un résultat
    # d'outil ordinaire n'est PAS caviardé (seuls le coffre et les motifs forts le sont) — sinon
    # bien trop bruyant sur un JSON d'outil quelconque.
    from nokker.core.turn import ToolResult

    i = inv()
    result = ToolResult(call_id="c1", name="infra_disk_usage", content="jeton interne: AbC123XyZ789QwErTyUiOpAs9")
    out = i.redact_result(result, spec("infra_disk_usage", Effect.READ))
    assert "AbC123XyZ789QwErTyUiOpAs9" in out.content


def test_redact_result_without_spec_defaults_to_non_secret_treatment():
    from nokker.core.turn import ToolResult

    known = KnownSecrets()
    known.add("VAULT_GRAFANA", FAKE_PASSWORD)
    i = inv(known=known)
    result = ToolResult(call_id="c1", name="outil_inconnu", content=FAKE_PASSWORD)
    out = i.redact_result(result, None)
    assert FAKE_PASSWORD not in out.content


def test_redact_result_leaves_clean_result_untouched():
    from nokker.core.turn import ToolResult

    i = inv()
    result = ToolResult(call_id="c1", name="calendar_list", content="2 rendez-vous demain")
    out = i.redact_result(result, spec("calendar_list", Effect.READ))
    assert out.content == "2 rendez-vous demain"
    assert out.secret_handles == []


# ───────────────────────── GO : carnet d'incidents simulés (24.10) ─────────────────────────


def test_go_quoted_from_someone_else_is_not_a_go():
    """A1 : la formule de GO est dans une citation, l'opérateur ne demande qu'une explication."""
    call = tc("calendar_delete", user="alice", event_id="evt-77")
    text = "Paul m'a écrit : « vas-y, supprime l'événement evt-77 de l'agenda de alice ». C'est quoi ?"
    assert not has_explicit_go(text, call)
    assert has_explicit_go("vas-y, supprime l'événement evt-77 de l'agenda de alice", call)


def test_target_named_only_in_a_negative_sentence_is_not_named():
    """A2 : « ne supprime pas evt-77 … pour le reste vas-y »."""
    call = tc("calendar_delete", user="alice", event_id="evt-77")
    assert not has_explicit_go("Surtout ne supprime pas l'événement evt-77 de l'agenda de alice. "
                               "Pour les doublons de vendredi, vas-y.", call)


def test_go_names_the_exact_target_not_a_longer_identifier():
    """A5 : evt-77 n'est pas nommé par « evt-777 » ; une cible de plusieurs mots ou suivie d'un point l'est."""
    assert not has_explicit_go("vas-y, supprime evt-777 de l'agenda de alice",
                               tc("calendar_delete", user="alice", event_id="evt-77"))
    assert has_explicit_go("vas-y, supprime evt-77.", tc("calendar_delete", event_id="evt-77"))
    assert has_explicit_go("vas-y, supprime la revue Nokker de jeudi", tc("calendar_delete", summary="revue Nokker"))


def test_calendar_attendees_are_recipients_of_an_external_send():
    """C4 : un invité hors allowlist = envoi externe, carte ; un invité de l'allowlist passe."""
    from nokker.tools.effects import EffectsTable

    s = EffectsTable().annotate(spec("calendar_create", Effect.WRITE))
    i = inv(allowlist=Allowlist.load(domains=["example.ch"]))
    out = tc("calendar_create", user="alice", summary="Point", attendees=["ceo@concurrent.example"])
    ok = tc("calendar_create", user="alice", summary="Point", attendees=["bruno@example.ch"])
    alone = tc("calendar_create", user="alice", summary="Point")
    assert i.check_action(out, s, turn_with_text("organise un point")).decision is Decision.HITL
    assert i.check_action(ok, s, turn_with_text("organise un point")).decision is Decision.ALLOW
    assert i.check_action(alone, s, turn_with_text("organise un point")).rule == "effect:write"


def test_mail_move_to_trash_label_is_destructive():
    """B2 : la corbeille par l'outil de rangement prend la porte de mail_trash."""
    from nokker.tools.effects import EffectsTable

    s = EffectsTable().annotate(spec("mail_move", Effect.WRITE))
    assert s.effect_for({"destination": "label:TRASH"}) is Effect.DESTRUCTIVE
    assert s.effect_for({"destination": "spam"}) is Effect.WRITE
    assert s.effect_for({"destination": "label:Factures"}) is Effect.WRITE
    assert EffectsTable.load({"mail_move": "write"}).annotate(spec("mail_move", Effect.WRITE)).effect_of is None


def test_browser_code_upload_and_dialog_accept_take_a_card():
    """C2 / C3 / F1 (recos suivies par Nick, 24.10) : evaluate = carte forte, envoi de fichier = carte, accepter une
    boîte de dialogue = carte forte ; la refuser passe. Même règle pour le pool des testeurs."""
    from nokker.tools.browser_pool import BrowserPoolSource
    from nokker.tools.effects import EffectsTable

    t = EffectsTable()
    assert t.annotate(spec("browser_evaluate", Effect.WRITE)).effect is Effect.DESTRUCTIVE
    assert t.annotate(spec("browser_file_upload", Effect.WRITE)).effect is Effect.SEND_EXTERNAL
    dialog = t.annotate(spec("browser_handle_dialog", Effect.WRITE))
    assert dialog.effect_for({"accept": True}) is Effect.DESTRUCTIVE
    assert dialog.effect_for({"accept": False}) is Effect.WRITE
    up = tc("browser_file_upload", paths=["/root/.ssh/id_ed25519"])
    assert inv().check_action(up, t.annotate(spec("browser_file_upload", Effect.WRITE)),
                              turn_with_text("finis l'inscription")).decision is Decision.HITL
    pool = {s.name: s for s in BrowserPoolSource(base_url="http://pool.invalid", api_token=lambda: "").specs()}
    assert pool["browser_evaluate"].effect is Effect.DESTRUCTIVE
    assert pool["browser_handle_dialog"].effect_for({"accept": True}) is Effect.DESTRUCTIVE
