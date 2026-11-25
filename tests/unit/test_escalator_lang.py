"""Phrases des escaladeurs vues de la personne, en fr/en/de/it : le français reste mot pour mot, les autres langues
suivent la langue du tour (vrai serveur, vrai noyau, faux modèle)."""
from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from nokker.approvals.rule_labels import LANGS, rule_label
from nokker.core import person_texts as ptx
from nokker.core.turn import Tier
from nokker.qa.escalate import EscalatorV0, LocalEscalator
from nokker.tenancy.context import tenant_scope
from nokker.tenancy.resources import TenantEscalator
from tests.unit.test_escalation_lang import _EscOnSwitch
from tests.unit.test_thread_serialization import _req, _server
from tests.unit.test_turn_lang import Boom

KEYS = ("TRANSMIT", "TRANSMIT_BARE", "TRANSMIT_TEAM", "TRANSMIT_TEAM_BARE", "NEED_YOU", "NEED_YOU_BARE", "TENANT_STOP",
        "TENANT_STOP_BARE")
SEND = "effect:send_external"


def _holes(text: str) -> set[str]:
    return set(re.findall(r"\{(\w+)\}", text))


@pytest.mark.parametrize("key", KEYS)
def test_escalator_tables_have_four_languages_and_the_same_holes(key):
    table = ptx.ALL_TABLES[key]
    assert set(table) == set(LANGS) and all(v.strip() for v in table.values())
    assert len({frozenset(_holes(v)) for v in table.values()}) == 1, key


def test_french_escalator_phrases_are_the_original_ones_word_for_word():
    t = SimpleNamespace(thread_id="t", index=0)
    assert EscalatorV0().escalate(t, "x") == "Je transmets à l'équipe : x."
    assert EscalatorV0().escalate(t, "") == "Je transmets à l'équipe."
    assert EscalatorV0(addressee="Alice").escalate(t, "x") == "Je transmets à Alice : x."
    assert EscalatorV0(addressee="Alice").escalate(t, "") == "Je transmets à Alice."
    assert LocalEscalator().escalate(t, "x") == "J'ai besoin de toi pour continuer : x."
    assert LocalEscalator().escalate(t, "") == "J'ai besoin de toi pour continuer."
    assert ptx.TRANSMIT["fr"].format(addressee="l'équipe", reason="x") == ptx.TRANSMIT_TEAM["fr"].format(reason="x")
    assert ptx.TENANT_STOP_BARE["fr"] == "Je m'arrête ici : cette décision te revient, ou à une personne compétente."
    assert ptx.TENANT_STOP["fr"].format(reason="x") == \
        "Je m'arrête ici : cette décision te revient, ou à une personne compétente — x."
    with tenant_scope("tn_x"):
        te = TenantEscalator(EscalatorV0())
        assert te.escalate(t, "x") == ptx.TENANT_STOP["fr"].format(reason="x")
        assert te.escalate(t, "") == ptx.TENANT_STOP_BARE["fr"]


def test_default_addressee_is_translated_and_a_named_one_is_kept():
    for lang, team in (("en", "the team"), ("de", "das Team"), ("it", "il team")):
        t = SimpleNamespace(thread_id="t", index=0, lang=lang)
        said = EscalatorV0().escalate(t, "x")
        assert said.endswith(": x.") and "équipe" not in said
        assert said == ptx.TRANSMIT_TEAM[lang].format(reason="x")
        assert team.lower().replace("il ", "").replace("das ", "").replace("the ", "") in said.lower()
        named = EscalatorV0(addressee="Alice").escalate(t, "x")
        assert "Alice" in named and named == ptx.TRANSMIT[lang].format(addressee="Alice", reason="x")
        assert EscalatorV0().escalate(t, "") == ptx.TRANSMIT_TEAM_BARE[lang]
    assert "al team" in ptx.TRANSMIT_TEAM["it"] and "a il " not in ptx.TRANSMIT_TEAM["it"]


def test_local_and_tenant_escalators_follow_the_turn_language():
    t = SimpleNamespace(thread_id="t", index=0, lang="de")
    assert LocalEscalator().escalate(t, "x") == "Ich brauche dich, um weiterzumachen: x."
    assert LocalEscalator().escalate(t, "") == "Ich brauche dich, um weiterzumachen."
    with tenant_scope("tn_x"):
        te = TenantEscalator(EscalatorV0())
        assert te.escalate(t, "x").startswith("Ich halte hier an: Diese Entscheidung")
        assert te.escalate(SimpleNamespace(thread_id="t", index=0, lang="it"), "").startswith("Mi fermo qui:")
        assert te.escalate(SimpleNamespace(thread_id="t", index=0, lang="en"), "x").startswith("I'm stopping here")


def _says(port, token, thread, lang):
    h = {"X-Session-Id": thread, **({"Accept-Language": lang} if lang else {})}
    status, body = _req(port, "/v1/chat/completions", token=token, headers=h, timeout=20,
                        body={"messages": [{"role": "user", "content": "salut"}]})
    assert status == 200, body
    return str(body)


@pytest.mark.parametrize("make", ["team", "local", "tenant"])
def test_http_escalation_sentence_is_in_the_request_language(tmp_path, make):
    httpd, port, api, kernel, _approvals, _tools, model, _ = _server(tmp_path)
    model.release.set()
    kernel.gates = _EscOnSwitch(kernel.gates)

    class InTenant(TenantEscalator):
        def escalate(self, turn, reason):
            with tenant_scope("tn_testeur"):
                return super().escalate(turn, reason)

    kernel.escalator = {"team": EscalatorV0(), "local": LocalEscalator(), "tenant": InTenant(EscalatorV0())}[make]
    fr = {"team": "Je transmets à l'équipe", "local": "J'ai besoin de toi pour continuer",
          "tenant": "Je m'arrête ici"}[make]
    de = {"team": "Ich leite das an das Team weiter", "local": "Ich brauche dich, um weiterzumachen",
          "tenant": "Ich halte hier an"}[make]
    it = {"team": "Lo inoltro al team", "local": "Ho bisogno di te per continuare", "tenant": "Mi fermo qui"}[make]
    try:
        body = _says(port, api.token, f"th_esc_{make}_de", "de-CH,de;q=0.9")
        assert de in body and fr not in body and rule_label(SEND, "de") in body, body
        body = _says(port, api.token, f"th_esc_{make}_it", "it")
        assert it in body and fr not in body and de not in body, body
        body = _says(port, api.token, f"th_esc_{make}_fr", None)  # sans en-tête : français inchangé
        assert fr in body and de not in body and it not in body, body
    finally:
        httpd.shutdown()


def test_http_model_down_escalation_keeps_the_frozen_french_without_header(tmp_path):
    httpd, port, api, kernel, _approvals, _tools, _model, _ = _server(tmp_path)
    kernel.providers = {Tier.SHIP: Boom()}
    kernel.escalator = LocalEscalator()
    try:
        assert "Je ne peux pas continuer" in _says(port, api.token, "th_esc_boom", None)
    finally:
        httpd.shutdown()
