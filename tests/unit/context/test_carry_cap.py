"""Lot compact-cache, plafond du report de clôture (`carry_max_ratio`). Reproduction : des tours LOURDS (6 résultats d'outil
de 6 000 caractères chacun, ≈ 25 % de `current_budget` par tour) reportés en entier après une clôture remplissent à eux seuls
la moitié du budget : la clôture revenait tous les 2 tours et réécrivait ≈ 7,5 k jetons à chaque fois. Mesuré sur le faux amont
Anthropic qui compte le cache (`cache_scenario`) ; les défauts de `ContextBudgets` sont ceux de la production."""
from __future__ import annotations

from nokker.context.types import ContextBudgets

from .cache_scenario import closures, run
from .harness import make_session

HEAVY = {"tool_every": 1, "calls_per_turn": 6, "tool_size": 6000, "n_turns": 14}


def _heavy(tmp_path, **over):
    bodies: list = []
    # Le scénario lourd suppose l'ancien seuil du réservoir (1 000) : les résultats de ~2 000 jetons partent en stub ; le défaut
    # de production est maintenant 8 000 (docs/COMPACT-CACHE.md), mesuré séparément ci-dessous.
    rows = run(ContextBudgets(**{"reservoir_min_tokens": 1000, **over}), tmp_path, bodies=bodies, **HEAVY)
    return rows, bodies, closures(rows)


def test_before_full_carry_closes_every_two_turns_and_rewrites_a_lot(tmp_path):
    """Ancien comportement (`carry_max_ratio` = 1) : la reproduction du défaut."""
    _, _, cl = _heavy(tmp_path, carry_max_ratio=1.0)
    assert [c["gap"] for c in cl[2:]] == [2] * len(cl[2:]) and len(cl) >= 4
    assert all(c["write"] > 7000 for c in cl[1:]), cl


def test_default_cap_spaces_the_closures_and_shrinks_the_rewrite(tmp_path):
    _, _, cl = _heavy(tmp_path)
    assert len(cl) <= 3, cl  # 14 tours lourds : 4 clôtures avant
    assert all(c["gap"] >= 4 for c in cl), cl
    assert all(c["write"] <= 4000 for c in cl), cl  # ≈ 7,6 k avant
    assert len({c["read"] for c in cl}) == 1 and cl[0]["read"] > 5000, "outils + système restent lus en cache"


def test_append_only_between_closures_with_the_cap(tmp_path):
    rows, bodies, _ = _heavy(tmp_path)
    after_close = {i + 1 for i, r in enumerate(rows) if "close" in r["event"]}
    for i in range(1, len(rows)):
        if i in after_close:
            continue
        assert rows[i]["read"] == rows[i - 1]["total"], (i, rows[i - 1], rows[i])
    assert all(b["system"] == bodies[0]["system"] for b in bodies)  # préfixe stable : jamais touché
    assert all(b["tools"] == bodies[0]["tools"] for b in bodies)


def test_carried_turn_comes_back_in_reduced_form_without_new_text(tmp_path):
    """Un tour reporté qui dépasse le plafond reste, mais ses résultats d'outils sont le stub de délestage (extrait +
    `recall_context("rs_…")`), jamais un texte inventé pour l'occasion ; l'élément est au réservoir."""
    rows, bodies, cl = _heavy(tmp_path, carry_max_ratio=0.4)
    first = next(i for i, r in enumerate(rows) if "close" in r["event"]) + 1
    msgs = bodies[first]["messages"]
    text = "\n".join(str(m["content"]) for m in msgs)
    assert "[Mémoire de la session]" in text and "déplacé hors fenêtre" in text
    assert "ligne de log numéro" in text  # l'extrait reste
    assert len(text) < len("\n".join(str(m["content"]) for m in bodies[first - 1]["messages"])) / 2


def test_manager_carry_cap_drops_oldest_turns_and_zero_ratio_carries_nothing(tmp_path):
    for ratio, expect_none in ((0.0, True), (1.0, False)):
        s = make_session(ContextBudgets(current_budget=4000, carry_max_ratio=ratio, shed_min_tokens=200,
                                        reservoir_min_tokens=600, reservoir_excerpt_chars=600), tmp_path=tmp_path)
        for i in range(2):
            s.say(f"[[BIG]] tour {i}")
        s.manager.new_episode(s.thread, "manual")
        st = s.manager.state(s.thread)
        last = s.manager.episodes.list(s.thread, closed_only=True)[-1].last_turn
        assert (st.base_turn == last + 1) is expect_none, (ratio, st.base_turn, last)


def test_from_config_bounds_carry_max_ratio():
    assert ContextBudgets().carry_max_ratio == 0.3
    assert ContextBudgets.from_config({"carry_max_ratio": 7}).carry_max_ratio == 1.0
    assert ContextBudgets.from_config({"carry_max_ratio": -1}).carry_max_ratio == 0.0
    assert ContextBudgets.from_config({"carry_max_ratio": "x"}).carry_max_ratio == 0.3
    assert ContextBudgets.from_config({"carry_max_ratio": float("nan")}).carry_max_ratio == 0.0


# ───────── admit_turn_budget : le modèle LIT ce qu'il vient de demander (mesure NDyGen, banc dyna-ft 45,5 → 49) ─────────

def _reads(tmp_path, size, **over):
    """Trois lectures DIFFÉRENTES dans un même tour ; renvoie, pour le dernier appel amont, les 3 résultats vus par le modèle."""
    bodies: list = []
    rows = run(ContextBudgets(**{"reservoir_min_tokens": 1000, **over}), tmp_path, n_turns=1, tool_size=size, tool_every=1,
               calls_per_turn=3, distinct=True, bodies=bodies)
    out = []
    for m in bodies[-1]["messages"]:
        for c in (m["content"] if isinstance(m["content"], list) else []):
            if c.get("type") == "tool_result":
                out.append(c["content"] if isinstance(c["content"], str) else "".join(x.get("text", "") for x in c["content"]))
    return out, rows


def test_before_turn_budget_1500_stubs_the_second_and_third_read_on_arrival(tmp_path):
    res, _ = _reads(tmp_path, 2500, admit_turn_budget=1500)  # ≈ 810 jetons chacune
    assert len(res) == 3 and "recall_context" not in res[0] and len(res[0]) >= 2500
    assert all("recall_context" in r and len(r) < 1500 for r in res[1:]), [len(r) for r in res]


def test_default_keeps_each_medium_read_in_full_in_the_turn_it_arrives(tmp_path):
    res, rows = _reads(tmp_path, 2500)
    assert [len(r) >= 2500 and "recall_context" not in r for r in res] == [True] * 3
    for i in range(1, len(rows)):  # append-only : tout l'appel précédent est relu
        assert rows[i]["read"] == rows[i - 1]["total"], (i, rows[i - 1], rows[i])


def test_a_result_above_reservoir_min_tokens_is_still_stubbed_whatever_the_turn_budget(tmp_path):
    res, _ = _reads(tmp_path, 6000)  # ≈ 2 000 jetons : stub + extrait + rs_…, comme avant
    assert all("recall_context" in r for r in res)


# ───────── le bloc `[Mémoire de la session]` compte dans le plafond (mesure NDyGen : écriture 7 – 8,7 k au lieu de 4 – 5 k) ─────────

def _closed_session(tmp_path, monkeypatch, mem_chars: int, **over):
    from nokker.context.manager import ContextManager
    from nokker.context.tokens import estimate_tokens

    from .token_fixtures import prose_fr

    mem = prose_fr(mem_chars) if mem_chars else ""
    monkeypatch.setattr(ContextManager, "memory_block", lambda self, thread_id, recall_available=True: mem)
    s = make_session(ContextBudgets(current_budget=6000, carry_max_ratio=0.5, shed_min_tokens=200,
                                    reservoir_min_tokens=1500, reservoir_excerpt_chars=600, **over),
                     tmp_path=tmp_path, tool_size=3000)
    for i in range(3):
        s.say(f"[[BIG]] lecture {i}")
    assert s.manager.new_episode(s.thread, "manual")
    return s, estimate_tokens(mem)


def test_session_memory_block_counts_in_the_carry_cap(tmp_path, monkeypatch):
    s, mem = _closed_session(tmp_path, monkeypatch, mem_chars=6000)  # bloc ≈ 2 000 jetons
    assert mem > 1500
    cap = 3000
    window = s.manager.window_tokens(s.thread)
    assert window + mem <= cap, (window, mem, cap)  # avant : report seul ≤ 3 000, donc report + bloc > 3 000


def test_memory_block_is_never_shrunk_it_is_the_carry_that_gives_way(tmp_path, monkeypatch):
    s, mem = _closed_session(tmp_path, monkeypatch, mem_chars=12000)  # bloc ≈ 4 000 jetons > plafond 3 000
    st = s.manager.state(s.thread)
    last = s.manager.episodes.list(s.thread, closed_only=True)[-1].last_turn
    assert st.base_turn == last + 1 and s.manager.window_tokens(s.thread) == 0  # rien n'est reporté


def test_without_session_memory_message_the_cap_ignores_the_block(tmp_path, monkeypatch):
    s, _ = _closed_session(tmp_path, monkeypatch, mem_chars=12000, memory_as_message=False)
    assert s.manager.window_tokens(s.thread) > 0  # le bloc est dans le système : le report n'est pas touché


def test_default_reservoir_threshold_keeps_a_few_thousand_tokens_result_in_full(tmp_path):
    """Défaut de production : un résultat de ~2 000 jetons (6 000 car.) que le modèle vient de lire reste en clair ;
    au-delà de `reservoir_min_tokens` (8 000) seulement : stub + extrait."""
    assert ContextBudgets().reservoir_min_tokens == 8000
    bodies: list = []
    run(ContextBudgets(), tmp_path, n_turns=1, tool_size=6000, tool_every=1, calls_per_turn=2, distinct=True, bodies=bodies)
    res = []
    for m in bodies[-1]["messages"]:
        for c in (m["content"] if isinstance(m["content"], list) else []):
            if c.get("type") == "tool_result":
                res.append(c["content"] if isinstance(c["content"], str) else "".join(x.get("text", "") for x in c["content"]))
    assert res and all(len(r) >= 6000 and "recall_context" not in r for r in res), [len(r) for r in res]
    bodies2: list = []
    run(ContextBudgets(reservoir_min_tokens=1000), tmp_path, n_turns=1, tool_size=6000, tool_every=1, calls_per_turn=2,
        distinct=True, bodies=bodies2)
    stubbed = [c for m in bodies2[-1]["messages"] for c in (m["content"] if isinstance(m["content"], list) else [])
               if c.get("type") == "tool_result" and "recall_context" in str(c.get("content"))]
    assert stubbed, "avec l'ancien seuil, ces résultats partaient en stub"
