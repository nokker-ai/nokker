"""ContextEngineV1 : ligne de langue du tour (`context.reply_language`) et queue (`reply_language_tail`), sous les mêmes
drapeaux et conditions que V0. Drapeaux éteints : requête V1 identique à l'octet près (empreinte figée avant le lot)."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime

from nokker.context.engine import ZURICH
from nokker.context.engine_v1 import ContextEngineV1
from nokker.context.manager import ContextManager
from nokker.context.types import ContextBudgets
from nokker.core import model_texts as mt
from nokker.core import session_opts
from nokker.core.ports import Recall
from nokker.core.turn import InputCheck, Message, Tier, Turn
from nokker.qa.invariants import DeterministicInvariants
from nokker.store.pg import MemoryStore
from nokker.tools.registry import Registry

# Empreinte de la requête V1 complète (system + messages de chaque assemblage), drapeaux éteints, calculée sur
# origin/p2/integ-beta-core (16f2f0cc) AVANT le lot, via ce même banc.
EMPREINTE_V1_EXTEINTS = "f06f7c0afc2ed9a671c3d440e0238e26d7078206e39c81df2356fc80041ef4ac"

LANGS = ("fr", "en", "de", "it")
TEXTES = {"fr": "Résume le document.", "en": "Summarise the document.", "de": "Fasse das Dokument zusammen.",
          "it": "Riassumi il documento."}


def _engine(tmp_path, **flags) -> ContextEngineV1:
    flags.setdefault("strip_past_recall", False)  # ces tests portent sur la ligne de langue figée, pas sur le rappel
    prefix = tmp_path / "prefix.md"
    if not prefix.exists():
        prefix.write_text("Tu es Atlas.\n\n## Style\n\n- Réponds dans la langue de l'utilisateur ; s'il change de langue, suis-le.\n- Sois bref.\n")
    mgr = ContextManager(store=MemoryStore(), budgets=ContextBudgets(), scrub=DeterministicInvariants().scrub_output)
    return ContextEngineV1(tools=Registry(), prefix_path=str(prefix), manager=mgr,
                           now_fn=lambda: datetime(2026, 11, 18, 9, 0, tzinfo=ZURICH),
                           self_context=lambda turn: "[Environnement]\nSurface : web\n[/Environnement]", **flags)


def _run(tmp_path, langs=("de", "de", "en"), *, signal=True, thread="t", **flags):
    """Une conversation de plusieurs tours ; renvoie (requêtes de chaque assemblage, tours)."""
    eng = _engine(tmp_path, **flags)
    turns: list[Turn] = []
    requests = []
    for i, lang in enumerate(langs):
        t = Turn(thread_id=thread, index=i, input=InputCheck(text=TEXTES[lang]), lang=lang, lang_signal=signal)
        recall = [Recall(note="projet", text="Le projet s'appelle Atlas.", score=0.9)] if i else []
        out = eng.assemble(t, list(turns), recall, [], Tier.SHIP)
        requests.append(out.messages)
        t.messages = [Message(role="assistant", content=f"Réponse {i}")]
        t.reply = f"Réponse {i}"
        turns.append(t)
    return requests, turns


def test_empreinte_drapeaux_eteints(tmp_path):
    requests, _ = _run(tmp_path)
    blob = json.dumps(requests, ensure_ascii=False, sort_keys=True)
    assert "[Environnement]" in blob and "[Rappel mémoire]" in blob
    assert hashlib.sha256(blob.encode()).hexdigest() == EMPREINTE_V1_EXTEINTS


def _users(msgs):
    return [m["content"] for m in msgs if m["role"] == "user"]


def test_ligne_dans_l_en_tete_du_tour_et_figee_dans_l_historique(tmp_path):
    for flags in ({"reply_language": True}, {"reply_language_tail": True}, {"reply_language": True, "reply_language_tail": True}):
        requests, _ = _run(tmp_path, ("de", "fr", "en"), **flags)
        for i, lang in enumerate(("de", "fr", "en")):
            line = mt.pick(mt.REPLY_LANGUAGE, lang)
            users = _users(requests[i])
            assert users[-1].count(line) == 1  # tour courant : la ligne est dans l'en-tête, avant le message
            assert users[-1].index(line) < users[-1].index(TEXTES[lang])
        # append-only : au dernier assemblage, l'en-tête des tours passés porte la langue DE CE TOUR-LÀ, inchangée
        last = _users(requests[2])
        assert mt.pick(mt.REPLY_LANGUAGE, "de") in last[0] and mt.pick(mt.REPLY_LANGUAGE, "fr") in last[1]
        assert mt.pick(mt.REPLY_LANGUAGE, "en") not in last[0] + last[1]
        # et chaque en-tête passé est octet pour octet celui envoyé à l'époque
        assert last[0] == _users(requests[0])[0] and last[1] == _users(requests[1])[1]


def test_les_quatre_langues(tmp_path):
    seen = set()
    for lang in LANGS:
        requests, _ = _run(tmp_path, (lang,), reply_language=True)
        line = mt.pick(mt.REPLY_LANGUAGE, lang)
        assert _users(requests[0])[-1].count(line) == 1
        seen.add(line)
    assert len(seen) == 4  # quatre textes distincts


def test_queue_toujours_en_dernier_et_figee(tmp_path):
    from nokker.context.reply_language import tail_line

    requests, _ = _run(tmp_path, ("de", "en"), reply_language=True, reply_language_tail=True)
    cur = _users(requests[1])[-1]
    assert cur.endswith(f"{tail_line()}\n\n{TEXTES['en']}")
    assert cur.index(mt.pick(mt.REPLY_LANGUAGE, "en")) < cur.index(tail_line())  # ligne de langue avant la queue
    assert tail_line() in _users(requests[1])[0]  # tour passé : sa queue est figée avec lui


def test_pas_de_ligne_sans_vrai_signal(tmp_path):
    requests, _ = _run(tmp_path, ("de", "en"), signal=False, reply_language=True, reply_language_tail=True)
    for lang in ("de", "en"):
        assert all(mt.pick(mt.REPLY_LANGUAGE, lang) not in u for r in requests for u in _users(r))


def test_lang_hint_false_respecte(tmp_path):
    reg = session_opts.lang_hint_registry()
    reg.set("th_nohint", False)
    try:
        requests, _ = _run(tmp_path, ("de",), thread="th_nohint", reply_language=True)
        assert mt.pick(mt.REPLY_LANGUAGE, "de") not in _users(requests[0])[-1]
    finally:
        reg.clear("th_nohint")


def test_meme_ligne_que_v0(tmp_path):
    from nokker.context.engine import ContextEngineV0
    from nokker.context.reply_language import turn_line

    t = Turn(thread_id="t", index=0, input=InputCheck(text=TEXTES["it"]), lang="it", lang_signal=True)
    v0 = ContextEngineV0(tools=Registry(), prefix_path=str(tmp_path / "x"), now_fn=lambda: datetime(2026, 11, 18, 9, 0, tzinfo=ZURICH))
    out = v0.assemble(t, [], [], [], Tier.SHIP)
    assert turn_line(t) in out.messages[-1]["content"]
    assert turn_line(t) == mt.pick(mt.REPLY_LANGUAGE, "it")


def test_drapeaux_eteints_aucune_ligne_meme_avec_signal(tmp_path):
    requests, _ = _run(tmp_path, ("de", "en"))
    for lang in ("de", "en"):
        assert all(mt.pick(mt.REPLY_LANGUAGE, lang) not in u for r in requests for u in _users(r))
