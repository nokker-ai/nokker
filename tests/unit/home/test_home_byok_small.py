"""Juge « demande explicite » de la maison sous BYOK : le palier `small` sert par la clé de la personne quand sa base
est celle du point d'accès BYOK (jamais par le lien Nokker) ; échec du juge (quota…) : événement hôte
`home.judge_failed {reason}` à classe fermée, WARNING, repli « carte »."""
from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx

from nokker.api.server import default_model_applier
from nokker.config import load_config
from nokker.core.turn import Tier
from nokker.home import source as S
from tests.unit.home.test_home import make
from tests.unit.home.test_home_unasked import LIGHT, turn_ctx

SHIPPED = Path(__file__).resolve().parents[3] / "deploy" / "lokkal" / "core-config.yaml"
ROUTER = "https://router.sokkan.ch/v1"
INFERENCE = "https://app.nokker.ai/v1/inference"
LINK_KEY = "rk-LINK-0123456789abcdef"
BYOK_KEY = "sk-" + "BYOK-" + "0123456789" + "abcdef"


class Hub:
    def __init__(self):
        self.events = []

    def host_event(self, name, data, thread_id=None):
        self.events.append((name, dict(data), thread_id))


class Upstream:
    """Faux amont : compte les appels par (hôte, clé) ; `judge` rend la réponse du juge, `status` force une erreur."""

    def __init__(self, judge="oui", status=None):
        self.calls: list[dict] = []
        self.judge, self.status = judge, status

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.calls.append({"host": request.url.host, "auth": request.headers.get("authorization"), "body": body})
        if self.status and body["messages"][0]["content"] == S.EXPLICIT_JUDGE_SYSTEM:
            return httpx.Response(self.status, json={"error": {"code": "quota_exceeded", "message": "150 par jour"}})
        judging = body["messages"][0]["content"] == S.EXPLICIT_JUDGE_SYSTEM
        content = self.judge if judging else LIGHT
        return httpx.Response(200, json={"model": body["model"], "usage": {"prompt_tokens": 5, "completion_tokens": 1},
                                         "choices": [{"message": {"role": "assistant", "content": content},
                                                      "finish_reason": "stop"}]})


def setup(tmp_path, monkeypatch, up, *, byok_base=ROUTER, models=None, hub=None):
    monkeypatch.delenv("ROUTER_KEY", raising=False)
    cfg = load_config(str(SHIPPED))
    real_client = httpx.Client  # le plan est servi par une COPIE du provider (`dataclasses.replace`) : client global
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: real_client(*a, **{**k, "transport": httpx.MockTransport(up)}))
    src, ha, _small, _ship, kernel = make(tmp_path, [], small_tier=Tier.SMALL)
    kernel.reviewer = kernel.advisor = None
    default_model_applier(cfg, kernel, lambda: (INFERENCE, LINK_KEY), models_fn=lambda: models)(
        byok_base, BYOK_KEY, "byok-model")
    if Tier.SMALL in kernel.providers:
        kernel.providers[Tier.SMALL].max_retries = 0
    src.bind(kernel, hub)
    return src, ha, kernel


def test_polite_request_under_byok_same_base_runs_directly_without_a_card_and_never_touches_the_link(
        tmp_path, monkeypatch):
    up = Upstream(judge="oui")
    src, ha, _k = setup(tmp_path, monkeypatch, up, models=["qwen/qwen3.8-27b"])
    r = src.call("maison", {"demande": "Éteins la prise X, s'il te plaît"},
                 turn_ctx("Éteins la prise X, s'il te plaît"))
    assert not r.is_error and ha.calls == [("light", "turn_off", ["light.salon"], {})]
    assert S.UNASKED_SUMMARY not in r.content and "Fait :" in r.content
    assert len(up.calls) == 2  # le plan, puis le juge
    assert {c["host"] for c in up.calls} == {"router.sokkan.ch"}
    assert {c["auth"] for c in up.calls} == {f"Bearer {BYOK_KEY}"}  # jamais LINK_KEY, jamais app.nokker.ai
    assert all(c["body"]["model"] == "qwen/qwen3.8-27b" for c in up.calls)


def test_model_absent_the_judge_uses_the_byok_model_short_and_without_reasoning(tmp_path, monkeypatch):
    up = Upstream(judge="oui")
    src, _ha, _k = setup(tmp_path, monkeypatch, up, models=["byok-model"])
    src.call("maison", {"demande": "allume"}, turn_ctx("Allume la lampe, s'il te plaît."))
    judge = next(c for c in up.calls if c["body"]["messages"][0]["content"] == S.EXPLICIT_JUDGE_SYSTEM)
    assert judge["body"]["model"] == "byok-model" and judge["body"]["max_tokens"] == 3
    assert judge["body"]["reasoning_effort"] == "none" and judge["auth"] == f"Bearer {BYOK_KEY}"


def test_byok_on_another_base_keeps_the_link_and_never_sends_the_byok_key_there(tmp_path, monkeypatch):
    up = Upstream(judge="oui")
    src, _ha, kernel = setup(tmp_path, monkeypatch, up, byok_base="https://byok.example/v1", models=["byok-model"])
    # base différente : le petit palier du Router est retiré (la maison n'en a plus) ; la clé BYOK ne part pas vers le
    # Router ni vers le lien, et la clé du lien ne part pas vers la base BYOK
    assert Tier.SMALL not in kernel.providers
    src.call("maison", {"demande": "allume"}, turn_ctx("Allume la lampe, s'il te plaît."))
    for c in up.calls:
        assert c["host"] == "byok.example" or c["auth"] != f"Bearer {BYOK_KEY}"
        assert c["host"] != "byok.example" or c["auth"] != f"Bearer {LINK_KEY}"


def test_quota_on_the_judge_emits_a_closed_event_logs_a_warning_and_still_poses_a_card(tmp_path, monkeypatch, caplog):
    up = Upstream(status=403)
    hub = Hub()
    src, ha, _k = setup(tmp_path, monkeypatch, up, models=["qwen/qwen3.6-35b-a3b-eu"], hub=hub)
    caplog.set_level(logging.WARNING)
    r = src.call("maison", {"demande": "allume"}, turn_ctx("Allume la lampe, s'il te plaît."))
    assert ha.calls == [] and S.UNASKED_SUMMARY in r.content  # repli « non » : carte
    assert hub.events == [("home.judge_failed", {"reason": "quota exceeded (HTTP 403)"}, "th_u")]
    blob = json.dumps(hub.events) + caplog.text
    for secret in (BYOK_KEY, LINK_KEY, "router.sokkan.ch", "150 par jour", "quota_exceeded"):
        assert secret not in blob, secret
    assert any(rec.levelno == logging.WARNING and "juge maison" in rec.getMessage() for rec in caplog.records)
