"""Never clear (docs/NEVER-CLEAR.md §6) : les invariants de la fenêtre calculée, avec un vrai noyau."""
from __future__ import annotations

from nokker.context.manager import ContextManager
from nokker.context.types import ContextBudgets
from nokker.core.turn import InputCheck, Message, Turn

from .harness import BIG_MARK, chars_of, make_session

FILLER = "On parle de la migration du disque et des sauvegardes nocturnes du serveur, avec beaucoup de détails. " * 3


def _window_chars(messages: list[dict]) -> int:
    """Couches 1-4 : tout sauf le dernier message utilisateur (bloc du tour) et ce que le tour a produit."""
    last_user = max(i for i, m in enumerate(messages) if m["role"] == "user")
    return chars_of(messages[:last_user])


def test_window_stays_under_budget_over_many_turns(tmp_path):
    s = make_session(tmp_path=tmp_path)
    b = s.manager.budgets
    cap_chars = 4 * (b.episodes_budget + b.current_budget) + 4 * 600  # marge : préfixe, un tour, entêtes
    worst = 0
    for i in range(300):
        s.say(f"tour {i} : {FILLER}")
        worst = max(worst, _window_chars(s.provider.seen[-1]))
    assert worst <= cap_chars, (worst, cap_chars)
    # le journal, lui, ne perd rien
    assert len(s.store.history(s.thread)) == 300
    assert len(s.manager.episodes.list(s.thread, closed_only=True)) >= 5


def test_append_only_between_events(tmp_path):
    """Hors délestage et clôture, tout ce qui précède le tour précédent est inchangé d'un appel à l'autre (cache amont)."""
    s = make_session(ContextBudgets(current_budget=10 ** 6, episodes_budget=500), tmp_path=tmp_path)
    s.say("premier message")
    s.say("deuxième message")
    prev = s.provider.seen[-1]
    s.say("troisième message")
    nxt = s.provider.seen[-1]
    stable = max(i for i, m in enumerate(prev) if m["role"] == "user")  # tout avant le dernier user de `prev`
    assert nxt[:stable] == prev[:stable]
    assert nxt[0] == prev[0]  # le message system (préfixe + mémoire) est identique


def test_big_result_goes_to_reservoir_and_can_be_recalled(tmp_path):
    s = make_session(tmp_path=tmp_path)
    s.say(f"montre les logs {BIG_MARK}")
    turn = s.store.history(s.thread)[-1]
    tool_msg = next(m for m in turn.messages if m.role == "tool")
    assert len(tool_msg.content) < 2000  # extrait seulement
    assert 'recall_context("rs_' in tool_msg.content
    item_id = tool_msg.content.split('recall_context("')[1].split('"')[0]
    item = s.manager.reservoir.get(item_id)
    assert item is not None and len(item.content) == 8000
    page = s.manager.recall(s.thread, ref=item_id, max_chars=1000)
    assert "ligne de log numéro 00000" in page and "suite : offset=1000" in page
    assert "inconnu" in s.manager.recall("autre_fil", ref=item_id)


def test_compaction_sheds_old_results_keeps_recent(tmp_path):
    b = ContextBudgets(episodes_budget=500, current_budget=3000, compaction_step=300, shed_min_tokens=200,
                       reservoir_min_tokens=10 ** 6, keep_last_turns_raw=2)
    s = make_session(b, tmp_path=tmp_path, tool_size=3000)  # 750 tokens par résultat, jamais admis au réservoir
    for i in range(6):
        s.say(f"logs {i} {BIG_MARK}")
    st = s.manager.state(s.thread)
    assert st.compaction_version >= 1 and st.shed
    hist = s.manager.history_window(s.thread)
    last_idx = hist[-1].index
    for key, stub in st.shed.items():
        turn_index = int(key.split(":")[0])
        assert turn_index <= last_idx - 2  # les 2 derniers tours restent bruts
        assert stub.startswith("[résultat de big_tool") and "recall_context(\"rs_" in stub
    # le prochain appel voit des stubs, pas les gros résultats
    s.say("et maintenant ?")
    seen = s.provider.seen[-1]
    assert sum(1 for m in seen if m["role"] == "tool" and m["content"].startswith("[résultat de big_tool")) >= 1


def test_watermark_closes_episode_with_summary_and_bubble(tmp_path):
    s = make_session(tmp_path=tmp_path)
    for i in range(60):
        s.say(f"sujet {i} : {FILLER}")
    eps = s.manager.episodes.list(s.thread, closed_only=True)
    assert eps and all(e.closed_reason == "watermark" and e.summary and e.title for e in eps)
    assert len(s.closed) == len(eps) and all(e.bubble_id == f"bu_{e.id}" for e in eps)
    ep = eps[0]
    assert ep.turn_ids_sha256 and ep.raw_tokens > 0 and ep.day
    system = s.provider.seen[-1][0]["content"]
    assert "[Mémoire de la session]" in system and eps[-1].id in system


def test_silence_closes_episode(tmp_path):
    s = make_session(tmp_path=tmp_path)
    s.say("on commence")
    s.say("on continue")
    s.clock.advance(7 * 3600)
    s.say("nouvelle journée")
    eps = s.manager.episodes.list(s.thread, closed_only=True)
    assert len(eps) == 1 and eps[0].closed_reason == "silence"
    assert s.manager.episodes.current(s.thread) is not None


def test_new_command_closes_episode_without_model_call(tmp_path):
    s = make_session(tmp_path=tmp_path)
    s.say("un sujet")
    s.say("un autre")
    calls = s.provider.calls
    turn = s.say("/new")
    assert s.provider.calls == calls  # aucun appel modèle
    assert "rien n'est perdu" in turn.reply
    eps = s.manager.episodes.list(s.thread, closed_only=True)
    assert len(eps) == 1 and eps[0].closed_reason == "marker"
    assert len(s.store.history(s.thread)) == 3  # le journal garde tout, `/new` compris


def test_day_one_fact_is_reachable_on_day_n(tmp_path):
    """Banc de reprise (déterministe) : un fait du jour 1 reste atteignable après des centaines de tours."""
    s = make_session(tmp_path=tmp_path)
    s.say("Mon code de coffre est 4711 et le serveur s'appelle Zébulon.")
    for i in range(240):
        if i % 20 == 0:
            s.clock.advance(26 * 3600)
        s.say(f"tour {i} : {FILLER}")
    s.say("Rappelle-moi mon code de coffre.")
    final = s.provider.seen[-1]
    joined = "\n".join(m.get("content") or "" for m in final)
    found_in_window = "4711" in joined
    hits = s.manager.recall(s.thread, query="code coffre")
    assert found_in_window or "ep_" in hits, "ni dans la fenêtre ni retrouvable par recall_context"
    ep_id = next(tok.strip(" ,(").split()[0] for tok in hits.replace("\n", " ").split("- ") if tok.startswith("ep_"))
    raw = s.manager.recall(s.thread, ref=ep_id, part="raw", max_chars=8000)
    assert "4711" in raw and "Zébulon" in raw


def test_recall_is_thread_scoped_and_export_copies(tmp_path):
    s = make_session(tmp_path=tmp_path)
    for i in range(30):
        s.say(f"sujet {i} : {FILLER}")
    ep = s.manager.episodes.list(s.thread, closed_only=True)[0]
    assert "inconnu" in s.manager.recall("th_autre", ref=ep.id)
    item = s.manager.export_episode(ep.id, "th_autre")
    assert item is not None and item.thread_id == "th_autre" and item.origin["episode_id"] == ep.id
    assert "sujet 0" in s.manager.recall("th_autre", ref=item.id, max_chars=8000)
    assert "inconnu" in s.manager.recall(s.thread, ref=item.id)  # l'export n'existe que dans le fil d'arrivée


def test_pin_keeps_old_episode_in_memory_block(tmp_path):
    s = make_session(tmp_path=tmp_path)
    for i in range(80):
        s.say(f"sujet {i} : {FILLER}")
    eps = s.manager.episodes.list(s.thread, closed_only=True)
    first = eps[0]
    assert first.id not in s.manager.memory_block(s.thread)
    assert s.manager.pin(s.thread, first.id)
    assert first.id in s.manager.memory_block(s.thread)
    assert not s.manager.pin("th_autre", first.id)  # pas hors du fil
    assert s.manager.unpin(s.thread, first.id)


def test_existing_thread_is_bootstrapped(tmp_path):
    s = make_session(tmp_path=tmp_path)
    for i in range(120):  # fil « d'avant » : tours déjà au journal, aucun épisode connu
        t = Turn(thread_id="th_old", index=i, input=InputCheck(text=f"ancien sujet {i} : {FILLER}"),
                 reply=f"ancienne réponse {i}")
        t.messages = [Message(role="assistant", content=t.reply)]
        s.store.append(t)
    s.say("on reprend ?", thread="th_old")
    eps = s.manager.episodes.list("th_old", closed_only=True)
    assert len(eps) >= 3
    window = s.manager.history_window("th_old")
    assert 1 <= len(window) < 60 and window[-1].input.text == "on reprend ?"
    assert len(s.store.history("th_old")) == 121


def test_prefix_hash_stable_across_episodes(tmp_path):
    s = make_session(tmp_path=tmp_path)
    shas = set()
    for i in range(80):
        t = s.say(f"sujet {i} : {FILLER}")
        shas.update(c.prefix_sha256 for c in t.calls)
    assert len(shas) == 1


def test_manager_is_optional_for_v0_engine():
    """Un engine sans crochets (V0) reste valide : la boucle ne les appelle que s'ils existent."""
    from nokker.context.engine import ContextEngineV0
    assert not hasattr(ContextEngineV0, "history_window") and not hasattr(ContextEngineV0, "after_turn")
    assert ContextManager.__name__ == "ContextManager"


def test_turn_heads_replayed_while_recent_and_shed_only_at_compaction(tmp_path):
    """Option A (cache entre tours, 20.10.2026) : l'en-tête figé d'un tour est rejoué tel qu'envoyé, puis retiré
    des tours anciens AU DÉLESTAGE seulement — la fenêtre reste sous budget et ne change pas à chaque tour."""
    s = make_session(tmp_path=tmp_path)
    for i in range(30):
        s.say(f"tour {i} : {FILLER}")
    floor = s.manager.state(s.thread).heads_shed_before  # état de vue AVANT le tour suivant (le délestage suit le tour)
    window = s.manager.history_window(s.thread)
    assert floor > 0
    s.say("fin")
    users = [m["content"] for m in s.provider.seen[-1] if m["role"] == "user"]
    assert users[-1].startswith("[Date et heure actuelles]")  # tour courant
    for t, content in zip(window, users[:-1]):
        assert content.startswith("[Date et heure actuelles]") == (t.index >= floor)
