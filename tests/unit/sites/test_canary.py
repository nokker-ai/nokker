"""Canaris (docs/CONNECTED-SITES.md §4.6, §5.5, §13.2) : le mot de passe et le code n'apparaissent NULLE PART —
résultats d'outils, arbres, textes, captures (même après « Afficher »), magasin, profil au repos, demandes, journaux,
fil, modèle, rappel."""
from __future__ import annotations

import json
import logging
import threading

from nokker.core.turn import Message, Tier, TurnInput
from nokker.sites.driver import MaskedDriver, find
from nokker.sites.fakesocial import FakeBrowser

from tests.unit.sites.helpers import CANARY_CODE, CANARY_PASSWORD, connect_ok, make_stack
from tests.unit.test_loop_contract import kernel


def test_masking_password_field_snapshot_and_screenshot_even_after_show(tmp_path):
    st = make_stack(tmp=tmp_path)
    drv = MaskedDriver(FakeBrowser(st.server))
    drv.goto("https://www.linkedin.com/login")
    box = find(drv.nodes(), "textbox", "Mot de passe")
    drv.fill_secret(box.ref, CANARY_PASSWORD)
    show = find(drv.nodes(), "button", "Afficher")
    drv.click(show.ref)  # le champ repasse en texte
    assert CANARY_PASSWORD in FakeBrowser.screenshot(drv.inner).decode()  # la page, elle, l'affiche
    assert CANARY_PASSWORD not in drv.snapshot()
    assert CANARY_PASSWORD not in drv.text()
    assert CANARY_PASSWORD.encode() not in drv.screenshot()
    drv.in_login = True
    try:
        drv.screenshot()
        raise AssertionError("capture permise pendant la connexion")
    except PermissionError:
        pass


def test_canary_everywhere_after_flows_and_gestures(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    st = make_stack(mfa="code", tmp=tmp_path)
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", st.source.call(
        "sites_connect", {"site": "LinkedIn", "username": "alex@example.org"}, st.ctx())))
    t.start()
    st.answer("password", CANARY_PASSWORD)
    t.join(5)
    st.answer("code", CANARY_CODE)
    cid = json.loads(out["r"].content)["id"]
    assert st.service.wait_flow(cid, 5).status == "connected"
    results = [out["r"].content]
    for kind in ("notifications", "messages"):
        results.append(st.source.call("sites_list", {"kind": kind}, st.ctx()).content)
    results.append(st.source.call("sites_search", {"kind": "people", "query": "martin"}, st.ctx()).content)
    results.append(st.source.call("sites_connect", {"site": "LinkedIn"}, st.ctx()).content)
    everything = st.everything("\n".join(results)) + caplog.text + repr(st.store.all()) + json.dumps(st.notes)
    assert CANARY_PASSWORD not in everything
    assert CANARY_CODE not in everything
    assert CANARY_CODE not in json.dumps(st.vault.values)  # un code ne va jamais au coffre


def test_kernel_armed_capture_never_reaches_store_model_or_recall(tmp_path):
    st = make_stack(tmp=tmp_path)
    r = st.source.call("sites_connect", {"site": "LinkedIn", "username": "alex@example.org"}, st.ctx())
    assert json.loads(r.content)["status"] == "needs_password"
    k, tools, ship, deep = kernel([Message("assistant", "ne doit pas être appelé")])
    recalled = []
    k.memory.recall = lambda q, k_=5: recalled.append(q) or []
    k.secret_capture = st.capture
    turn = k.run_turn(TurnInput("t1", CANARY_PASSWORD, sender="alex"))
    assert turn.reply.startswith("Ton mot de passe est enregistré en sécurité et effacé de la conversation.")
    assert ship.seen == []  # le modèle n'a pas été appelé
    stored = json.dumps([[t.input.text, t.reply, [m.content for m in t.messages]] for t in k.store.turns],
                        ensure_ascii=False)
    assert CANARY_PASSWORD not in stored and "retiré du fil, jamais vu par l'assistant" in stored
    assert all(CANARY_PASSWORD not in q for q in recalled)


def test_kernel_spontaneous_capture_model_sees_placeholder_only(tmp_path):
    st = make_stack(tmp=tmp_path)
    connect_ok(st)
    k, tools, ship, deep = kernel([Message("assistant", "Je regarde tes messages.")])
    k.secret_capture = st.capture
    turn = k.run_turn(TurnInput("t7", f"mon mot de passe linkedin : {CANARY_PASSWORD} regarde mes messages",
                                sender="alex"))
    seen = json.dumps(ship.seen, ensure_ascii=False, default=str)
    assert CANARY_PASSWORD not in seen and "[mot de passe retiré du fil par sécurité — non enregistré]" in seen
    assert "Veux-tu l'enregistrer pour un de tes sites ?" in turn.reply
    stored = json.dumps([t.input.text for t in k.store.turns], ensure_ascii=False)
    assert CANARY_PASSWORD not in stored
    assert Tier.SHIP in k.providers
