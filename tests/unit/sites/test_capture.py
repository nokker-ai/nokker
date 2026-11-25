"""Mot de passe écrit dans le chat (docs/CONNECTED-SITES.md §4.6) : règles exactes, cas armé, cas spontané."""
from __future__ import annotations

import json
import threading

import pytest

from nokker.sites.capture import (ADVICE_CODE, ADVICE_PASSWORD, NOT_SAVED, PH_ARMED_CODE, PH_ARMED_PASSWORD,
                                  PH_SPONTANEOUS, SAVED_SPONTANEOUS, detect_password)

from tests.unit.sites.helpers import CANARY_CODE, CANARY_PASSWORD, OWNER, connect_ok, make_stack

# Jeu réutilisable par la relecture des textes (§4.6) : (message, valeur attendue).
POSITIVES = [
    ("mon mot de passe linkedin : Xk9!qpzT42Lm", "Xk9!qpzT42Lm"),
    ("mdp=Sup3r-Secret", "Sup3r-Secret"),
    ("password: hunter22", "hunter22"),
    ("le mot de passe de mon insta c'est Boutique2026!", "Boutique2026!"),
    ("voici mon mdp Xk9!qpzT42Lm merci", "Xk9!qpzT42Lm"),
    ("my password is Tr0ub4dor&3", "Tr0ub4dor&3"),
    ("mot de passe linkedin pro Xk9!qpzT42Lm", "Xk9!qpzT42Lm"),
    ("pass: Abc12345", "Abc12345"),
]
NEGATIVES = [
    "mon mot de passe Wi-Fi est sur le frigo",
    "j'ai oublié mon mot de passe",
    "mot de passe : oublié",
    "le mot de passe est trop court",
    "change ton mot de passe stp",
    "mot de passe du compte : ?",
    "le mdp est dans le coffre",
    "Xk9!qpzT42Lm",
    "réf. commande AB12-CD34-EF56",
    "password reset link https://example.org/r?t=Ab12Cd34",
    "un passe-partout pour la cave",
    "mot de passe : [secret:password-gbgbei]",
]


@pytest.mark.parametrize("text,value", POSITIVES)
def test_spontaneous_rules_positive(text, value):
    found = detect_password(text)
    assert found is not None and found[0] == value
    assert text[found[1]:found[2]] == value


@pytest.mark.parametrize("text", NEGATIVES)
def test_spontaneous_rules_negative(text):
    assert detect_password(text) is None


def _arm(st, mfa_kind="password"):
    """sites_connect rend needs_password : le RÉSULTAT arme la capture pour ce fil."""
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", st.source.call(
        "sites_connect", {"site": "LinkedIn", "username": "alex@example.org"}, st.ctx())))
    t.start()
    t.join(5)
    return json.loads(out["r"].content)


def test_connect_result_gives_only_the_secure_link_and_arms(tmp_path):
    st = make_stack(tmp=tmp_path)
    res = _arm(st)
    assert res["status"] == "needs_password" and res["secure_link"].startswith("https://app.nokker.ai/sites/r/rq_")
    assert "Ne demande pas le mot de passe dans la conversation." in res["message"]
    assert ("t1", "alex") in st.requests._arms


def test_armed_password_is_captured_vaulted_and_replaced(tmp_path):
    st = make_stack(tmp=tmp_path)
    res = _arm(st)
    cap = st.capture.intercept("t1", "alex", CANARY_PASSWORD)
    assert cap.text == PH_ARMED_PASSWORD.format(label="LinkedIn")
    assert cap.text == "[mot de passe enregistré pour « LinkedIn » — retiré du fil, jamais vu par l'assistant]"
    assert cap.direct_reply == ADVICE_PASSWORD
    assert st.service.wait_flow(res["id"], 5).status == "connected"
    assert st.vault.values[f"sites.{res['id']}.password"] == CANARY_PASSWORD
    assert CANARY_PASSWORD not in cap.text + cap.direct_reply
    assert st.capture.intercept("t1", "alex", "et maintenant ?") is None  # UN message seulement


def test_armed_only_for_this_thread_and_sender(tmp_path):
    st = make_stack(tmp=tmp_path)
    _arm(st)
    assert st.capture.intercept("t2", "alex", CANARY_PASSWORD) is None
    assert st.capture.intercept("t1", "camille", CANARY_PASSWORD) is None


def test_armed_sentence_is_not_taken(tmp_path):
    st = make_stack(tmp=tmp_path)
    _arm(st)
    assert st.capture.intercept("t1", "alex", "je n'ai pas Telegram sur ce téléphone") is None


def test_armed_code_goes_to_the_page_never_to_the_vault(tmp_path):
    st = make_stack(mfa="code", tmp=tmp_path)
    res = _arm(st)
    st.answer("password", CANARY_PASSWORD)
    for _ in range(200):
        if st.store.get(res["id"]).state.value == "needs_code":
            break
        import time

        time.sleep(0.01)
    r = st.source.call("sites_connect", {"site": "LinkedIn"}, st.ctx())  # suivre le flux : needs_code arme
    assert json.loads(r.content)["status"] == "needs_code"
    cap = st.capture.intercept("t1", "alex", CANARY_CODE)
    assert cap.text == PH_ARMED_CODE == "[code de vérification transmis au site — non conservé]"
    assert cap.direct_reply == ADVICE_CODE
    assert st.service.wait_flow(res["id"], 5).status == "connected"
    assert CANARY_CODE not in json.dumps(st.vault.values)


def test_spontaneous_is_removed_offered_then_saved(tmp_path):
    st = make_stack(tmp=tmp_path)
    cid = connect_ok(st)
    msg = f"au fait mon mot de passe linkedin : {CANARY_PASSWORD} tu peux regarder mes messages ?"
    cap = st.capture.intercept("t9", "alex", msg)
    assert CANARY_PASSWORD not in cap.text and PH_SPONTANEOUS in cap.text
    assert cap.text == "au fait mon mot de passe linkedin : [mot de passe retiré du fil par sécurité — non enregistré] " \
                       "tu peux regarder mes messages ?"
    assert cap.direct_reply is None and "Veux-tu l'enregistrer" in cap.notice and "LinkedIn" in cap.notice
    st.vault.values.pop(f"sites.{cid}.password")
    choice = st.capture.intercept("t9", "alex", "LinkedIn")
    assert choice.direct_reply == SAVED_SPONTANEOUS and "tu peux le changer sur le site" in choice.direct_reply
    assert st.vault.values[f"sites.{cid}.password"] == CANARY_PASSWORD


def test_spontaneous_refused_is_forgotten(tmp_path):
    st = make_stack(tmp=tmp_path)
    connect_ok(st)
    st.capture.intercept("t9", "alex", "password: Zz9-Oubli!x")
    assert st.capture.intercept("t9", "alex", "non").direct_reply == NOT_SAVED
    assert st.capture.intercept("t9", "alex", "LinkedIn") is None  # plus rien en attente


def test_spontaneous_buffer_expires(tmp_path):
    st = make_stack(tmp=tmp_path)
    connect_ok(st)
    st.capture.intercept("t9", "alex", "password: Zz9-Oubli!x")
    st.capture.clock.t += 601
    assert st.capture.intercept("t9", "alex", "LinkedIn") is None


def test_secret_values_join_the_scrubber(tmp_path):
    st = make_stack(tmp=tmp_path)
    _arm(st)
    st.capture.intercept("t1", "alex", CANARY_PASSWORD)
    from nokker.qa.secrets import redact

    assert CANARY_PASSWORD not in redact(f"x {CANARY_PASSWORD} y", st.known).text
    assert OWNER == "alex"
