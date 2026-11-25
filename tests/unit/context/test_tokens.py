"""Estimateur de jetons (`context/tokens.py`) : non-régression par classe de contenu sur les 5 points de référence du Router
(textes synthétiques de même forme), ±25 %, et calibrateur par palier."""
from __future__ import annotations

import json

import pytest

from nokker.context.tokens import CALIBRATOR, TokenCalibrator, estimate_chars_tokens, estimate_tokens

from . import token_fixtures as fx

# (nom, texte, octets/jeton comptés par le Router)
REF = [
    ("prompt hôte", lambda: fx.prose_fr(25657), 3.52),
    ("schémas d'outils", lambda: fx.tool_schemas(18359), 3.43),
    ("invariants", lambda: fx.prose_fr(1269), 3.88),
    ("mémoire de session", lambda: fx.memory_block(1408), 2.92),
    ("résultat d'outil JSON/UUID", lambda: fx.tool_result_json(7021), 1.96),
    ("petit résultat d'outil", lambda: fx.tool_result_json(2400, 9), 2.15),
]


@pytest.mark.parametrize("name,make,ratio", REF, ids=[r[0] for r in REF])
def test_estimate_within_25_percent_of_router_reference(name, make, ratio):
    text = make()
    real = len(text.encode()) / ratio
    est = estimate_tokens(text)
    assert abs(est - real) / real <= 0.25, (name, est, round(real))


def test_old_len_over_4_was_far_below_on_dense_content():
    """Le constat : `len // 4` sous-estimait ×1,9 les résultats d'outils (c'est le but du changement)."""
    text = fx.tool_result_json(7021)
    real = len(text.encode()) / 1.96
    assert len(text) // 4 < real * 0.6 and estimate_tokens(text) > real * 0.8


def test_mixed_window_ratio_close_to_measured_x1_9():
    """Fenêtre type : 12 729 jetons de résultats d'outils pour 3 600 de messages → ×1,9 sur l'ancien len//4."""
    tools = [fx.tool_result_json(n, s) for n, s in [(2374, 1), (7021, 2), (7021, 3), (2722, 4), (2878, 5), (2896, 6)]]
    chat = fx.chat_messages()
    total = sum(len(t.encode()) for t in tools) + sum(len(c.encode()) for c in chat)
    est = sum(estimate_tokens(t) for t in tools + chat)
    assert 1.5 * (total // 4) < est < 2.2 * (total // 4)  # ×1,9 mesuré
    assert est > 1.5 * (total // 4)


def test_kinds_force_a_fixed_ratio_and_empty_is_zero():
    assert estimate_tokens("") == 0
    assert estimate_tokens("x" * 280, "tool") == 140
    assert estimate_tokens("x" * 350, "prose") == 100
    assert estimate_tokens("abc") >= 1
    assert estimate_chars_tokens(2800) == 1000  # défaut 2,8 car./jeton


def test_estimate_is_deterministic_and_monotone():
    t = fx.prose_fr(5000)
    assert estimate_tokens(t) == estimate_tokens(t)
    assert estimate_tokens(t + t) >= estimate_tokens(t)


def test_uuid_and_hex_cost_about_two_chars_per_token():
    ids = " ".join(f"3f2a9c1e-7b4d-4e8a-9c3b-{i:012x}" for i in range(200))
    ratio = len(ids) / estimate_tokens(ids)
    assert 1.6 <= ratio <= 2.7, ratio


def test_json_is_denser_than_prose():
    j = json.dumps([{"a": i, "b": [i * 0.37, i * 1.1], "c": "x"} for i in range(100)])
    assert len(j) / estimate_tokens(j) < len(fx.prose_en(2000)) / estimate_tokens(fx.prose_en(2000))


def test_calibrator_bounded_moving_average_per_tier():
    c = TokenCalibrator()
    assert c.factor("ship") == 1.0 and c.factor() == 1.0
    c.observe("ship", 1000, 1900)
    assert c.factor("ship") == pytest.approx(1.9)
    c.observe("ship", 1000, 1000)  # moyenne mobile : descend d'un pas, pas d'un coup
    assert 1.5 < c.factor("ship") < 1.9
    c.observe("deep", 10, 1000)  # borné à 2,5
    assert c.factor("deep") == 2.5
    c.observe("deep", 1000, 10)  # borné à 1,0 en bas
    assert c.factor("deep") >= 1.0
    c.observe("ship", 0, 5)
    c.observe("ship", 5, 0)  # observations vides ignorées
    assert c.factor("nina") == 1.0
    c.reset()
    assert c.factor("ship") == 1.0


def test_global_calibrator_starts_neutral():
    assert CALIBRATOR.factor("never-seen-tier") == 1.0


def test_calibrated_window_follows_the_observed_ratio_only_when_the_flag_is_on(tmp_path):
    from nokker.context.types import ContextBudgets

    from .harness import make_session

    CALIBRATOR.reset()
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    try:
        plain = make_session(ContextBudgets(current_budget=10 ** 6), tmp_path=tmp_path / "a")
        calibrated = make_session(ContextBudgets(current_budget=10 ** 6, calibrate_tokens=True), tmp_path=tmp_path / "b")
        for s in (plain, calibrated):
            s.say("bonjour, une question sur le calcul du maillage ?")
        CALIBRATOR.reset()  # la boucle du faux modèle a déjà posé sa propre observation
        base = plain.manager.window_tokens(plain.thread)
        assert calibrated.manager.window_tokens(calibrated.thread) == base  # aucune observation : ratio 1,0
        CALIBRATOR.observe("ship", 1000, 1900)
        assert plain.manager.window_tokens(plain.thread) == base  # drapeau éteint : jamais appliqué
        assert calibrated.manager.window_tokens(calibrated.thread) == pytest.approx(base * 1.9, abs=1)
    finally:
        CALIBRATOR.reset()
