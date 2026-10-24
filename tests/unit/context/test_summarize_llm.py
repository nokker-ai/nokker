"""Résumeur LLM + garde anti-invention : tests purs, sans réseau (faux modèles)."""
from __future__ import annotations

import json
from types import SimpleNamespace

from nokker.context.summarize_llm import LLMSummarizer, grounded_violations, render_transcript
from nokker.context.types import SummaryResult
from nokker.core.turn import InputCheck, Message, ToolCall, Turn

SOURCE = ("[T3]\nUtilisateur : déploie nokker sur hote21.example.ch, port 8080, avant 14:30 ; budget 1 234 CHF, "
          "taux 1,5 %.\nAssistant : Alice a validé, rs_ab12cd34 contient le log (/srv/app/run.log).")


class FakeFallback:
    def __init__(self) -> None:
        self.calls = 0

    def summarize(self, turns, previous=None):
        self.calls += 1
        return SummaryResult(title="repli", summary="résumé extractif")


def make_turn(index=1, user="bonjour", reply="salut", tool_result="x" * 2000):
    msgs = [Message(role="assistant", content="", tool_calls=[ToolCall("c1", "web_fetch", {"url": "https://a.b/" + "z" * 400})]),
            Message(role="tool", content=tool_result, name="web_fetch", tool_call_id="c1")]
    return Turn(thread_id="th_1", index=index, input=InputCheck(text=user), reply=reply, messages=msgs)


def fake_model(*outputs):
    """Renvoie successivement `outputs` (str ou Exception) ; mémorise les messages reçus."""
    seen: list = []
    it = iter(outputs)

    def complete(messages):
        seen.append(messages)
        out = next(it)
        if isinstance(out, Exception):
            raise out
        return out

    complete.seen = seen
    return complete


def js(**kw):
    base = {"title": "Déploiement", "summary": "Alice a validé le déploiement sur hote21.example.ch.", "keywords": ["déploiement"],
            "facts": []}
    base.update(kw)
    return json.dumps(base, ensure_ascii=False)


# ── transcription ──

def test_render_transcript_borne_et_contenu():
    turns = [make_turn(i) for i in range(5)]
    out = render_transcript(turns, 1500)
    assert len(out) <= 1500
    assert "[T0]" in out and "Utilisateur : bonjour" in out and "Outil appelé : web_fetch(" in out
    assert "Résultat web_fetch" in out and "…" in out
    assert "z" * 300 not in out  # arguments abrégés


def test_render_transcript_vide():
    assert render_transcript([], 1000) == ""


# ── garde ──

def test_nombre_invente_detecte():
    assert "9999" in grounded_violations("Budget de 9999 CHF.", SOURCE)


def test_hote_invente_detecte():
    v = grounded_violations("Déployé sur evil.example.com.", SOURCE)
    assert "evil.example.com" in v
    assert grounded_violations("Déployé sur hote21.example.ch.", SOURCE) == []


def test_nom_propre_invente_detecte():
    assert "Zoé" not in grounded_violations("Zoé a validé.", SOURCE)  # début de phrase : mot courant
    assert "Zoé" in grounded_violations("Le client Zoé a validé.", SOURCE)
    assert grounded_violations("Le client Alice a validé.", SOURCE) == []


def test_identifiants_et_chemins():
    assert grounded_violations("Log dans rs_ab12cd34 et /srv/app/run.log.", SOURCE) == []
    assert "rs_ffff0000" in grounded_violations("Voir rs_ffff0000.", SOURCE)
    assert "/etc/passwd" in grounded_violations("Lu /etc/passwd.", SOURCE)


def test_nombres_reformates_acceptes():
    assert grounded_violations("Budget de 1234 CHF.", SOURCE) == []
    assert grounded_violations("Budget de 1 234 CHF.", SOURCE) == []
    assert grounded_violations("Budget de 1 234 CHF.", SOURCE) == []
    assert grounded_violations("Taux de 1.5 %.", SOURCE) == []
    assert grounded_violations("Taux de 1,50 %.", SOURCE) == []
    assert grounded_violations("Avant 14:30 sur le port 8080.", SOURCE) == []
    assert "1,6" in grounded_violations("Taux de 1,6 %.", SOURCE)


def test_casse_ignoree():
    assert grounded_violations("Le client ALICE a validé sur HOTE21.EXAMPLE.CH.", SOURCE) == []


# ── LLMSummarizer ──

def test_nominal():
    fb = FakeFallback()
    s = LLMSummarizer(fake_model(js(facts=["Alice a validé le port 8080."])), fb)
    r = s.summarize([make_turn(user="Alice valide hote21.example.ch, port 8080")])
    assert r.summary.startswith("Alice a validé") and r.facts == ["Alice a validé le port 8080."]
    assert r.keywords == ["déploiement"] and r.title == "Déploiement" and fb.calls == 0


def test_json_dans_bloc_et_texte_autour():
    fb = FakeFallback()
    out = "Voici :\n```json\n" + js() + "\n```\nFin."
    r = LLMSummarizer(fake_model(out), fb).summarize([make_turn(user="Alice valide hote21.example.ch")])
    assert r.summary.startswith("Alice") and fb.calls == 0
    r2 = LLMSummarizer(fake_model("blabla " + js() + " merci"), fb).summarize([make_turn(user="Alice hote21.example.ch")])
    assert fb.calls == 0 and r2.summary.startswith("Alice")


def test_violation_puis_reessai_reussi():
    fb = FakeFallback()
    model = fake_model(js(summary="Alice a payé 9999 CHF."), js())
    r = LLMSummarizer(model, fb).summarize([make_turn(user="Alice valide hote21.example.ch")])
    assert fb.calls == 0 and "9999" not in r.summary
    assert len(model.seen) == 2 and "9999" in model.seen[1][-1]["content"]


def test_violation_persistante_repli():
    fb = FakeFallback()
    bad = js(summary="Alice a payé 9999 CHF.")
    r = LLMSummarizer(fake_model(bad, bad), fb).summarize([make_turn()])
    assert r.title == "repli" and fb.calls == 1


def test_exception_modele_repli():
    fb = FakeFallback()
    r = LLMSummarizer(fake_model(ConnectionError("réseau")), fb).summarize([make_turn()])
    assert r.title == "repli" and fb.calls == 1


def test_json_invalide_repli():
    fb = FakeFallback()
    r = LLMSummarizer(fake_model("pas du json du tout"), fb).summarize([make_turn()])
    assert r.title == "repli" and fb.calls == 1


def test_resume_vide_repli():
    fb = FakeFallback()
    r = LLMSummarizer(fake_model(js(summary="  ")), fb).summarize([make_turn()])
    assert r.title == "repli"


def test_facts_non_fideles_retires():
    fb = FakeFallback()
    out = js(facts=["Alice a validé le port 8080.", "Le budget est de 777 CHF.", "Hôte : ghost.example.org."])
    r = LLMSummarizer(fake_model(out), fb).summarize([make_turn(user="Alice valide hote21.example.ch, port 8080")])
    assert r.facts == ["Alice a validé le port 8080."] and fb.calls == 0


def test_resume_plus_long_que_source_repli():
    fb = FakeFallback()
    r = LLMSummarizer(fake_model(js(summary="bonjour " * 400)), fb).summarize([make_turn(user="a", reply="b", tool_result="c")])
    assert r.title == "repli" and fb.calls == 1


def test_provider_et_modele_renseigne():
    calls = {}

    class Prov:
        def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048):
            calls.update(tools=tools, reasoning=reasoning, session_key=session_key, max_tokens=max_tokens)
            return SimpleNamespace(message=Message(role="assistant", content=js()), model="magnitude-x")

    fb = FakeFallback()
    r = LLMSummarizer(Prov(), fb, max_tokens=123).summarize([make_turn(user="Alice hote21.example.ch")])
    assert r.model == "magnitude-x" and calls["reasoning"] is False and calls["max_tokens"] == 123
    assert calls["tools"] == [] and calls["session_key"]


def test_previous_sert_d_ancre():
    model = fake_model(js())
    LLMSummarizer(model, FakeFallback()).summarize([make_turn(user="Alice hote21.example.ch")], previous="Ancien résumé X.")
    assert "Ancien résumé X." in model.seen[0][1]["content"]
