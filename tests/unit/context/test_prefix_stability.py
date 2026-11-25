"""Mesure du préfixe commun d'un appel au suivant (tests/unit/context/prefix_stability.py) : entre deux événements la requête
ne fait que s'allonger (append-only), donc le préfixe commun dépasse les blocs de cache d'un amont à blocs alignés ; à une
clôture d'épisode, seuls `tools` et le système (et le message de mémoire) restent communs."""
from __future__ import annotations

from .prefix_stability import run


def test_steady_state_prefix_exceeds_two_cache_blocks():
    rows = run(turns=9, prefix_chars=20000, close_at=None)
    assert rows
    steady = [r for r in rows if r["breaks"] != "tools"]
    assert steady
    for r in steady:
        assert r["common_tokens"] > 3200, r  # préfixe hôte ~7 k jetons + outils : au-delà de 2 blocs de 1 600
        assert r["breaks"] == "fin de la requête précédente (ajout seul)", r


def test_loading_a_deferred_tool_breaks_the_whole_prefix():
    """CONSTAT (mesure, pas de correctif ici) : `tools` est rendu EN PREMIER ; quand un outil différé est chargé (premier
    appel d'un tour qui en a besoin), le bloc d'outils change et le préfixe commun tombe à ce qui précède les outils (<
    1 600 jetons) : aucun bloc de cache réutilisé pour cet appel, une seule fois par outil chargé dans le fil."""
    rows = run(turns=9, prefix_chars=20000, close_at=None)
    loads = [r for r in rows if r["breaks"] == "tools"]
    assert loads and all(r["common_tokens"] < 1600 for r in loads), loads
    assert len(loads) <= 1  # une fois par outil : le deuxième tour d'outil n'y touche plus


def test_episode_close_keeps_tools_and_system_in_common():
    rows = run(turns=10, prefix_chars=20000, close_at=7)
    at_close = [r for r in rows if r["breaks"] not in ("fin de la requête précédente (ajout seul)", "tools")]
    assert at_close, "la clôture doit casser le préfixe quelque part"
    assert all(r["common_tokens"] > 3200 for r in at_close), at_close  # outils + système communs même à la clôture


def test_loaded_tools_only_grow_so_the_common_prefix_survives_a_load():
    """Le jeu d'outils chargés d'un fil ne fait que grandir, à la suite des outils de base : un outil utilisé une fois
    (tour 3) n'est plus retiré quand le message suivant n'en parle plus (avant : il sortait après `tool_reuse_turns` tours
    et le bloc d'outils réécrivait tout le préfixe en cache de l'amont). Ordre de rendu du Router (système, outils, tours) :
    préfixe commun > 3 200 jetons après le chargement ET à chaque appel suivant."""
    rows = run(turns=14, prefix_chars=20000, close_at=None, tool_turns=(3,))
    assert rows
    assert all(r["tools_append_only"] for r in rows), [r for r in rows if not r["tools_append_only"]]
    assert all(r["router_common_tokens"] > 3200 for r in rows), [r for r in rows if r["router_common_tokens"] <= 3200]
    after_load = [r for r in rows if r["turn"] > 3]
    assert after_load and all(r["breaks"] != "tools" for r in after_load), after_load
