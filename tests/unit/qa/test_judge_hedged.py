"""Juge avec repli (`qa/judge.py::HedgedJudgeTransport`, `api/server.py::judge_transport`) : le modèle RÉEL qui répond
remonte partout (verdict, relecteur automatique, avis). Aucun réseau."""
from __future__ import annotations

import threading
import time

import pytest

from nokker.approvals.auto_review import AutoReviewer
from nokker.config import load_config
from nokker.core.ports import Approval
from nokker.qa.judge import HedgedJudgeTransport, OpenAICompatJudge
from nokker.qa.review import CardAdvisor

FLASH, BIG = "deepseek/deepseek-v4.1-flash", "qwen/qwen3.5-397b-a17b"


class Fake:
    """Transport scripté : répond (avec son modèle), échoue, ou traîne `delay` secondes."""

    def __init__(self, model, *, fail=False, delay=0.0, text="Oui", stamp=True):
        self.model, self.fail, self.delay, self.text, self.stamp, self.calls = model, fail, delay, text, stamp, 0
        self.release = threading.Event()

    def post(self, body):
        self.calls += 1
        if self.delay:
            self.release.wait(self.delay)
        if self.fail:
            raise RuntimeError(f"HTTP 504 juge ({self.model})")
        d = {"choices": [{"message": {"content": self.text}}]}
        if self.stamp:
            d["model"] = self.model
        return d


def hedged(primary, fallback, after=0.3):
    return HedgedJudgeTransport(primary, fallback, hedge_after_s=after)


def test_primary_answers_judge_model_is_primary():
    p, f = Fake(FLASH), Fake(BIG)
    ans = OpenAICompatJudge(transport=hedged(p, f), model_name=FLASH, verdict_max_tokens=4).ask("q", "p", ["Oui", "Non"])
    assert ans.model == FLASH and f.calls == 0


def test_primary_without_model_field_is_stamped_with_its_own_name():
    p, f = Fake(FLASH, stamp=False), Fake(BIG)
    assert hedged(p, f).post({})["model"] == FLASH


@pytest.mark.parametrize("primary", [Fake(FLASH, delay=5.0), Fake(FLASH, fail=True)], ids=["lent", "504"])
def test_slow_or_failing_primary_fallback_answers_and_is_named(primary):
    f = Fake(BIG, stamp=False)
    t0 = time.monotonic()
    t = hedged(primary, f)
    ans = OpenAICompatJudge(transport=t, model_name=FLASH, verdict_max_tokens=4).ask("q", "p", ["Oui", "Non"])
    primary.release.set()
    assert ans.model == BIG and t.hedged_calls == 1 and time.monotonic() - t0 < 3


def test_fallback_model_reaches_card_advice_and_auto_review_decision():
    slow = Fake(FLASH, delay=5.0, text="DÉCISION: APPROUVER\nRAISON: lecture seule")
    f = Fake(BIG, text="DÉCISION: APPROUVER\nRAISON: lecture seule", stamp=False)
    t = hedged(slow, f)
    card = Approval(id="a1", thread_id="t", turn_index=0, channel="api", tool_name="read_file", arguments={"path": "x"},
                    args_sha256="", strength="standard", rule="effect:read")
    d = AutoReviewer(transport=t, model_name=FLASH).review_text("lis x", card, [])
    slow.release.set()
    assert (d.decision, d.model) == ("approve", BIG)
    adv = CardAdvisor(transport=hedged(Fake(FLASH, fail=True), Fake(BIG, text="Risque : faible. Sauvegarde : aucune. Retour arrière : oui.")), model_name=FLASH)
    from nokker.core.turn import InputCheck, Turn, TurnInput  # noqa: F401
    turn = Turn(thread_id="t", index=0, input=InputCheck(text="x"))
    adv.advise(turn, "write_file", {}, "r", "strong")
    assert adv.last_model == BIG


def test_both_fail_error_surfaces_and_judge_keeps_current_behaviour():
    t = hedged(Fake(FLASH, fail=True), Fake(BIG, fail=True))
    with pytest.raises(RuntimeError):
        t.post({})
    ans = OpenAICompatJudge(transport=t, model_name=FLASH, verdict_max_tokens=4).ask("q", "p", ["Oui", "Non"])
    assert ans.choice == "Oui" and ans.probabilities is None and ans.model == FLASH  # repli d'échec existant
    card = Approval(id="a1", thread_id="t", turn_index=0, channel="api", tool_name="read_file", arguments={},
                    args_sha256="", strength="standard", rule="effect:read")
    d = AutoReviewer(transport=t).review_text("lis x", card, [])
    assert (d.decision, d.by_rule) == ("escalate", True)  # relecteur : à l'humain


def test_config_reads_fallback_and_builds_hedged_transport(tmp_path, monkeypatch):
    from nokker.api.server import judge_transport

    p = tmp_path / "c.yaml"
    p.write_text(f"judge:\n  base_url: https://r.example/v1\n  model: {FLASH}\n  fallback_model: {BIG}\n"
                 "  hedge_after_s: 5\n", encoding="utf-8")
    cfg = load_config(str(p))
    j = cfg.judge_config()
    assert (j.model, j.fallback_model, j.hedge_after_s) == (FLASH, BIG, 5.0)
    t = judge_transport(cfg)
    assert isinstance(t, HedgedJudgeTransport) and (t.primary.model, t.fallback.model) == (FLASH, BIG)
    assert t.hedge_after_s == 5.0
    t.base_url = "https://x/v1"
    assert t.fallback.base_url == "https://x/v1"


def test_without_fallback_model_transport_unchanged(tmp_path):
    from nokker.api.server import judge_transport
    from nokker.qa.judge import HttpJudgeTransport

    p = tmp_path / "c.yaml"
    p.write_text(f"judge:\n  base_url: https://r.example/v1\n  model: {BIG}\n", encoding="utf-8")
    cfg = load_config(str(p))
    assert cfg.judge_config().fallback_model == ""
    assert type(judge_transport(cfg)) is HttpJudgeTransport
