"""La langue du tour suit les fils de travail lancés POUR le tour : sous-agents (`delegate`), recherche web en parallèle,
crochets en arrière-plan. Deux tours simultanés en langues différentes ne se mélangent pas."""
from __future__ import annotations

import threading

import pytest

from nokker.approvals.rule_labels import current_lang, lang_scope
from nokker.core import model_texts as mt
from nokker.tenancy.context import start_thread_in_context, submit_in_context
from tests.unit.tools.test_delegate import Factory, ctx, run, source, turn


def _delegate(lang):
    seen = {}

    def behave(k, inp):
        seen["ctx_lang"] = current_lang()
        seen["inp_lang"] = inp.lang
        seen["text"] = mt.pick(mt.DELEGATE_NO_ISOLATION)
        return turn("fini")

    src, _, _ = source(Factory(behave=behave))
    with lang_scope(lang):
        run(src, [{"goal": "g"}], isolate=False, c=ctx())
    return seen


@pytest.mark.parametrize("lang", ["en", "de", "it"])
def test_sous_agent_dans_la_langue_du_parent(lang):
    seen = _delegate(lang)
    assert seen["ctx_lang"] == lang and seen["inp_lang"] == lang
    assert seen["text"] == mt.DELEGATE_NO_ISOLATION[lang] != mt.DELEGATE_NO_ISOLATION["fr"]


def test_sous_agent_francais_inchange_et_sans_langue_defaut():
    assert _delegate("fr")["text"] == mt.DELEGATE_NO_ISOLATION["fr"]
    seen = {}

    def behave(k, inp):
        seen["lang"] = current_lang()
        return turn("fini")

    src, _, _ = source(Factory(behave=behave))
    run(src, [{"goal": "g"}], isolate=False)
    assert seen["lang"] == "fr"


def test_pool_parallele_voit_la_langue_de_chaque_tour():
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=4) as pool:
        with lang_scope("de"):
            futs = [submit_in_context(pool, current_lang) for _ in range(4)]
        assert [f.result() for f in futs] == ["de"] * 4
        assert pool.submit(current_lang).result() == "fr"  # sans le helper, le fil du pool retombe sur le défaut


def test_deux_tours_simultanes_ne_se_melangent_pas():
    barrier = threading.Barrier(2)
    out: dict[str, list[str]] = {}

    def turn_in(lang):
        with lang_scope(lang):
            res = []

            def work():
                barrier.wait(5)  # les deux fils de travail tournent en même temps
                res.append(current_lang())

            start_thread_in_context(work).join(5)
            res.append(current_lang())
            out[lang] = res

    ts = [threading.Thread(target=turn_in, args=(lg,)) for lg in ("en", "it")]
    [t.start() for t in ts]
    [t.join(10) for t in ts]
    assert out == {"en": ["en", "en"], "it": ["it", "it"]}


def test_recherche_web_en_fils_garde_la_langue(monkeypatch):
    from nokker.tools import web

    seen = []

    def fake(name, fn, client, query, limit, timeout_s):
        seen.append(current_lang())
        return web.EngineOutcome(engine=name, rows=[], elapsed_s=0.0)

    monkeypatch.setitem(web._ENGINE_SEARCHERS, web._ALL_ENGINES[0], lambda *a: [])
    monkeypatch.setattr(web, "_run_search_engine", fake)
    src = web.WebToolSource(engines=[web._ALL_ENGINES[0]])
    with lang_scope("en"):
        src._fan_out_search("q", 3)
    src.close()
    assert seen == ["en"]


def test_crochet_en_arriere_plan_garde_la_langue():
    from nokker.tools.hooks import HookRunner

    got, done = [], threading.Event()
    runner = HookRunner.__new__(HookRunner)
    object.__setattr__(runner, "background", True)
    with lang_scope("it"):
        runner._later(lambda: (got.append(current_lang()), done.set()))
    assert done.wait(5) and got == ["it"]
