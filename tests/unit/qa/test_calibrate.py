"""`qa/calibrate.py` — seuils par question + ECE/Brier, sur des lignes SYNTHÉTIQUES (aucun réseau,
aucune dépendance au vrai banc B-1). PLAN-P1 §5.3 (B-2)."""
from __future__ import annotations

import json

from nokker.qa.calibrate import (
    DEFAULT_THRESHOLDS, ScoredRow, brier_noul, ece, fit_temperature, fit_thresholds_for_question,
    load_thresholds_file, report, save_thresholds_file,
)


def _row(qid, expected, pred, p_pred, polarity, ok=None, label_status="sur"):
    """`p_pred` = probabilité du token PRÉDIT (comme un backend à logprob unique le renverrait)."""
    probs = {pred: p_pred} if pred else None
    if ok is None:
        ok = pred == expected
    return ScoredRow(id=f"x-{qid}-{pred}-{p_pred}", question_id=qid, expected=expected, pred=pred,
                     probs=probs, label_status=label_status, polarity=polarity, ok=ok)


def test_p_alert_noul_uses_alert_option_directly():
    # R.secret : option d'alerte = "Oui". pred="Oui", p=0.9 -> p_alert = probs["Oui"] = 0.9.
    r = _row("R.secret", "Oui", "Oui", 0.9, "fautif")
    assert r.p_alert() == 0.9


def test_p_alert_noul_complements_when_pred_is_the_safe_option():
    # pred="Non" (pas l'alerte), p=0.8 -> p_alert = 1 - 0.8 = 0.2 (le juge a répondu "Non" avec 0.8
    # de confiance -> seulement 0.2 de masse plausible sur l'alerte "Oui").
    r = _row("R.secret", "Non", "Non", 0.8, "juste")
    assert abs(r.p_alert() - 0.2) < 1e-9


def test_p_alert_choice_alert_and_safe_options():
    # A.decision : alerte = {B, C}. pred="B" (alerte), p=0.7 -> p_alert = 0.7.
    assert abs(_row("A.decision", "B", "B", 0.7, "fautif").p_alert() - 0.7) < 1e-9
    # pred="A" (sûr), p=0.9 -> p_alert = 1 - 0.9 = 0.1.
    assert abs(_row("A.decision", "A", "A", 0.9, "juste").p_alert() - 0.1) < 1e-9


def test_is_fautif_expected_matches_question_alert_set():
    assert _row("R.secret", "Oui", "Oui", 0.9, "fautif").is_fautif_expected is True
    assert _row("R.secret", "Non", "Non", 0.9, "juste").is_fautif_expected is False


def test_ece_perfect_calibration_is_zero():
    pairs = [(0.9, True)] * 9 + [(0.9, False)] * 1  # 90% confiance, 90% de vrais -> bien calibré
    assert ece(pairs) < 0.02


def test_ece_overconfident_is_high():
    pairs = [(0.95, False)] * 10  # toujours très confiant, toujours faux
    e = ece(pairs)
    assert e is not None and e > 0.9


def test_ece_empty_is_none():
    assert ece([]) is None


def test_brier_noul_only_scores_noul_questions():
    rows = [_row("R.secret", "Oui", "Oui", 0.9, "fautif"), _row("A.decision", "B", "B", 0.7, "fautif")]
    # Seule R.secret est Noul ; A.decision (Choice) est ignorée par brier_noul.
    b = brier_noul(rows)
    assert b is not None
    assert abs(b - (1.0 - 0.9) ** 2) < 1e-9


def test_fit_temperature_defaults_to_one_with_too_few_points():
    rows = [_row("R.secret", "Oui", "Oui", 0.9, "fautif") for _ in range(5)]
    t, e, n = fit_temperature(rows)
    assert t == 1.0
    assert n == 5


def test_fit_temperature_improves_or_keeps_ece_on_overconfident_wrong_answers():
    # 20 réponses très confiantes mais FAUSSES -> un T > 1 (aplatissement) doit réduire l'ECE.
    rows = [_row("R.secret", "Non", "Oui", 0.95, "fautif", ok=False) for _ in range(20)]
    t, e_after, n = fit_temperature(rows)
    assert n == 20
    # ECE avant recalage (confiance 0.95, jamais correct) :
    before = ece([(0.95, False)] * 20)
    assert e_after is not None and e_after <= before


def test_fit_thresholds_for_question_separates_fautif_and_juste():
    rows = []
    for i in range(15):
        rows.append(_row("R.secret", "Oui", "Oui", 0.9, "fautif", ok=True))
        rows.append(_row("R.secret", "Non", "Non", 0.9, "juste", ok=True))
    t = fit_thresholds_for_question("R.secret", rows, k=5)
    assert t.cv_accuracy is not None
    assert t.cv_accuracy > 0.8  # séparation nette (p_alert=0.9 vs 0.1) -> doit bien se calibrer
    assert t.low <= t.high  # séparation nette : la zone grise peut disparaître (verify coûte un demi-point)


def test_fit_thresholds_for_question_too_few_points_returns_safe_default():
    rows = [_row("R.secret", "Oui", "Oui", 0.9, "fautif")]
    t = fit_thresholds_for_question("R.secret", rows, k=5)
    assert t.source == "sans-logprobs" or t.cv_accuracy is None
    assert t.low == 0.3 and t.high == 0.7


def test_report_recall_and_false_alarms():
    rows = []
    for i in range(10):
        rows.append(_row("R.secret", "Oui", "Oui", 0.9, "fautif", ok=True))  # fautif bien attrapé
    for i in range(10):
        rows.append(_row("R.secret", "Non", "Non", 0.9, "juste", ok=True))  # juste jamais bloqué
    thresholds = {"R.secret": fit_thresholds_for_question("R.secret", rows, k=5)}
    rep = report(rows, thresholds)
    by_q = rep["by_question"]["R.secret"]
    assert by_q["recall_fautifs"] == 1.0
    assert by_q["faux_positifs_justes"] == 0.0


def test_thresholds_file_roundtrip(tmp_path):
    thresholds = dict(DEFAULT_THRESHOLDS)
    path = tmp_path / "thresholds.json"
    save_thresholds_file(path, thresholds)
    loaded = json.loads(path.read_text())
    assert "R.secret" in loaded
    reloaded = load_thresholds_file(path)
    assert reloaded["R.secret"].low == thresholds["R.secret"].low
    # Une question absente du fichier garde le seuil prudent par défaut.
    assert reloaded["A.risque"].low == 0.3 and reloaded["A.risque"].high == 0.7
