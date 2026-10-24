"""`qa/judge.py::OpenAICompatJudge` — backend scripté (`ScriptedTransport`), AUCUN réseau. Preuve de
l'approximation « single-logprob » (PLAN-P1 §5.3 B-2) et du cache par contenu."""
from __future__ import annotations

import math

from nokker.qa.judge import (
    OpenAICompatJudge, _apply_single_logprob_approximation, probability_from_top_logprobs,
)


class ScriptedTransport:
    """Transport scripté : une liste de réponses OpenAI-compatibles jouées dans l'ordre. Aucun
    réseau — satisfait le Protocol `JudgeTransport` par duck-typing."""

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[dict] = []

    def post(self, body):
        self.calls.append(body)
        return self.script.pop(0)


def _completion(token: str, logprob: float, model: str = "scripted-model") -> dict:
    return {"model": model, "choices": [{"message": {"content": token},
            "logprobs": {"content": [{"token": token, "logprob": logprob,
                                      "top_logprobs": [{"token": token, "logprob": logprob}]}]}}]}


def test_probability_from_top_logprobs_single_entry_is_not_trivially_renormalized_to_one():
    """Piège documenté dans judge.py : une seule entrée de logprob (plafond Router) ne doit JAMAIS
    être renormalisée par elle-même (ce qui donnerait toujours 1.0 et effacerait la confiance
    réelle p=0.9 du modèle)."""
    top = [{"token": "Oui", "logprob": math.log(0.9)}]
    probs = probability_from_top_logprobs(top, ["Oui", "Non"])
    assert probs is not None
    assert math.isclose(probs["Oui"], 0.9, abs_tol=1e-6)
    assert "Non" not in probs  # pas rempli à 0 : seule l'option réellement observée est connue


def test_probability_from_top_logprobs_multi_entry_normalizes_over_observed_options():
    top = [{"token": "Oui", "logprob": math.log(0.6)}, {"token": "Non", "logprob": math.log(0.3)}]
    probs = probability_from_top_logprobs(top, ["Oui", "Non"])
    assert math.isclose(probs["Oui"] + probs["Non"], 1.0, abs_tol=1e-6)
    assert probs["Oui"] > probs["Non"]


def test_single_logprob_approximation_fills_the_other_noul_option():
    raw = {"Oui": 1.0}  # sortie de probability_from_top_logprobs avec top_logprobs=1
    out = _apply_single_logprob_approximation(raw, "Oui", ["Oui", "Non"])
    # Le point n'est PAS 1.0 (le logprob brut) mais `p`, la probabilité réelle sous le logprob —
    # ce test vérifie seulement la mécanique de complément sur un p synthétique.
    assert out == {"Oui": 1.0, "Non": 0.0}


def test_single_logprob_approximation_leaves_choice_untouched():
    """Choice/Score (> 2 options) : pas de complément possible, `probs` reste `{choix: p}`."""
    raw = {"B": 0.8}
    out = _apply_single_logprob_approximation(raw, "B", ["A", "B", "C"])
    assert out == {"B": 0.8}


def test_ask_noul_high_confidence_oui():
    transport = ScriptedTransport([_completion("Oui", math.log(0.95))])
    judge = OpenAICompatJudge(transport=transport, model_name="scripted")
    answer = judge.ask("R.secret", "La réponse contient-elle un secret ?", ["Oui", "Non"])
    assert answer.choice == "Oui"
    assert answer.probabilities["Oui"] > 0.9
    assert answer.probabilities["Non"] < 0.1
    assert answer.model == "scripted-model"


def test_ask_noul_non_branch():
    transport = ScriptedTransport([_completion("Non", math.log(0.8))])
    judge = OpenAICompatJudge(transport=transport, model_name="scripted")
    answer = judge.ask("R.secret", "...", ["Oui", "Non"])
    assert answer.choice == "Non"
    assert answer.probabilities["Non"] > 0.7
    assert answer.probabilities["Oui"] < 0.3


def test_ask_choice_only_confidence_of_chosen_token():
    transport = ScriptedTransport([_completion("B", math.log(0.6))])
    judge = OpenAICompatJudge(transport=transport, model_name="scripted")
    answer = judge.ask("A.decision", "...", ["A", "B", "C"])
    assert answer.choice == "B"
    assert set(answer.probabilities) == {"B"}


def test_ask_caches_by_content_not_position():
    transport = ScriptedTransport([_completion("Oui", math.log(0.9))])
    judge = OpenAICompatJudge(transport=transport, model_name="scripted")
    a1 = judge.ask("R.secret", "même question", ["Oui", "Non"])
    a2 = judge.ask("R.secret", "même question", ["Oui", "Non"])
    assert len(transport.calls) == 1  # un seul appel réseau pour les deux questions identiques
    assert a1.choice == a2.choice
    assert a2.latency_s == 0.0  # servi par le cache


def test_ask_cache_disabled_calls_transport_every_time():
    transport = ScriptedTransport([_completion("Oui", math.log(0.9)), _completion("Oui", math.log(0.9))])
    judge = OpenAICompatJudge(transport=transport, model_name="scripted", cache=False)
    judge.ask("R.secret", "x", ["Oui", "Non"])
    judge.ask("R.secret", "x", ["Oui", "Non"])
    assert len(transport.calls) == 2


def test_ask_different_question_id_is_not_a_cache_hit():
    transport = ScriptedTransport([_completion("Oui", math.log(0.9)), _completion("Non", math.log(0.9))])
    judge = OpenAICompatJudge(transport=transport, model_name="scripted")
    judge.ask("R.secret", "x", ["Oui", "Non"])
    judge.ask("R.humain", "x", ["Oui", "Non"])
    assert len(transport.calls) == 2


def test_ask_unrecognized_token_falls_back_without_crashing():
    transport = ScriptedTransport([_completion("???", -1.0)])
    judge = OpenAICompatJudge(transport=transport, model_name="scripted")
    answer = judge.ask("R.secret", "x", ["Oui", "Non"])
    assert answer.choice in ("Oui", "Non")  # repli, jamais d'exception


class RaisingTransport:
    def post(self, body):
        raise RuntimeError("panne réseau simulée")


def test_ask_transport_failure_never_raises():
    judge = OpenAICompatJudge(transport=RaisingTransport(), model_name="scripted")
    answer = judge.ask("R.secret", "x", ["Oui", "Non"])
    assert answer.choice == "Oui"  # repli sur la première option, jamais d'exception propagée
    assert answer.probabilities is None


def test_advise_returns_text():
    transport = ScriptedTransport([{"model": "scripted-model",
                                    "choices": [{"message": {"content": "Avis : sauvegarde la table avant."}}]}])
    judge = OpenAICompatJudge(transport=transport, model_name="scripted")
    text = judge.advise("Que conseilles-tu ?")
    assert "sauvegarde" in text


def test_advise_never_raises_on_transport_failure():
    judge = OpenAICompatJudge(transport=RaisingTransport(), model_name="scripted")
    assert judge.advise("x") == ""
