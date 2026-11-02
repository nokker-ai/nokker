"""Le Guide : statuts par compte, tour d'accueil, compteurs d'usage (codes, cloisonnés), question au modèle (codes
joints, rien d'inventé), quota, drapeau éteint = rien (route, observateur, surfaces du modèle)."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from nokker.api import server
from nokker.api.events import EventBus
from nokker.config import NokkerConfig, TierConfig
from nokker.guide import http as guide_http
from nokker.guide.service import AccountFacts, GuideRefused, GuideService, split_see
from nokker.guide.store import MemoryUsage
from nokker.guide.texts import GUIDE_SYSTEM
from nokker.tenancy.context import tenant_scope


class FakeModel:
    def __init__(self, reply="Tu peux relier Telegram.\nVOIR: telegram, inexistant, web_search, bubbles, goals"):
        self.reply = reply
        self.bodies = []

    def post(self, body):
        self.bodies.append(body)
        return {"choices": [{"message": {"content": self.reply}}], "model": "petit-x",
                "usage": {"prompt_tokens": 900, "completion_tokens": 40}}


NOFACTS = AccountFacts()


def svc(config=None, facts=NOFACTS, **kw):
    return GuideService(config=config or NokkerConfig(), usage=MemoryUsage(), facts_of=lambda s: facts, **kw)


def status_map(s, facts=NOFACTS):
    return {f.id: st for f, st in s.statuses(facts)}


def test_only_what_is_on_for_this_install_and_account():
    cfg = NokkerConfig(household=True, household_paper=True, payments=True, show_me=False)
    s = svc(cfg)
    st = status_map(s)
    assert st["web_search"] == "on" and st["approvals"] == "on" and st["bubbles"] == "on"
    assert st["show_me"] == "off" and st["goals"] == "off" and st["household_paper"] == "on"
    assert st["folder_mode"] == "off"  # Lokkal seulement
    assert st["telegram"] == "off"  # approvals_telegram_enabled éteint
    minor = status_map(s, AccountFacts(household=True, minor=True))
    assert minor["payments"] == "not_for_you" and minor["household_paper"] == "not_for_you"
    cfg.approvals_telegram_enabled = True
    assert status_map(s)["telegram"] == "setup" and status_map(s, AccountFacts(telegram=True))["telegram"] == "on"


def test_lokkal_profile_and_lokkal_only_features():
    s = svc(NokkerConfig(local={"folder_mode": True, "lsp": {"enabled": True}}))
    st = status_map(s)
    assert s.profile(AccountFacts()) == "lokkal"
    assert st["folder_mode"] == "on" and st["lsp"] == "on" and st["packs"] == "off" and st["connectors"] == "off"


def test_view_is_codes_only_with_intro_of_three_and_next():
    s = svc(NokkerConfig(show_me=True, household=True), facts=AccountFacts(household=True))
    with tenant_scope("tn_a"):
        v = s.view("us_a")
    assert v["profile"] == "household" and v["show_me"] is True
    assert len(v["intro"]["picks"]) == 3 and v["intro"]["seen"] is False and v["next"] == v["intro"]["picks"][0]
    by = {f["id"]: f for f in v["features"]}
    assert by["household"] == {"id": "household", "status": "on", "screen": "household", "used": 0}
    assert by["goals"] == {"id": "goals", "status": "off", "used": 0}  # rien à montrer d'une fonction éteinte
    assert by["web_search"]["ui"] == "composer"
    assert 0 < v["catalog_tokens"] < 6000
    # aucune phrase : que des codes et des nombres
    for f in v["features"]:
        assert set(f) <= {"id", "status", "used", "screen", "ui", "command"}


def test_usage_counts_by_code_per_tenant_and_moves_next():
    s = svc(NokkerConfig())
    s.observe("tool.call", {"name": "web_search", "_tenant": "tn_a", "args": {"q": "secret"}})
    s.observe("tool.call", {"name": "web_search", "_tenant": "tn_a"})
    s.observe("approval.requested", {"_tenant": "tn_a"})
    s.observe("tool.call", {"name": "outil_inconnu", "_tenant": "tn_a"})
    with tenant_scope("tn_a"):
        assert s.usage.counts() == {"web_search": 2, "approvals": 1}
        v = s.view("us_a")
    with tenant_scope("tn_b"):
        assert s.usage.counts() == {}  # aucune fuite entre locataires
        assert s.view("us_b")["features"][0]["used"] == 0
    assert {f["id"]: f["used"] for f in v["features"]}["web_search"] == 2
    assert v["next"] not in ("web_search", "approvals")


def test_intro_seen_is_remembered():
    s = svc()
    with tenant_scope("tn_a"):
        s.mark_intro_seen()
        assert s.view("us_a")["intro"]["seen"] is True
    with tenant_scope("tn_b"):
        assert s.view("us_b")["intro"]["seen"] is False


def test_split_see():
    assert split_see("Bonjour\n**VOIR:** a, B ,a\n") == ("Bonjour", ["a", "b"])
    assert split_see("Pas de ligne") == ("Pas de ligne", [])
    assert split_see("x\nVOIR:") == ("x", [])


def test_ask_sends_catalog_for_this_account_and_joins_codes_only_for_known_ids():
    model = FakeModel()
    calls = []
    cfg = NokkerConfig(show_me=True, approvals_telegram_enabled=True)
    s = svc(cfg, transport=model, model_name="petit-x", on_call=lambda *a: calls.append(a),
            scrub_in=lambda t: t.replace("sk-SECRET", "[caviardé]"), scrub_out=lambda t: t.upper())
    with tenant_scope("tn_a"):
        r = s.ask("us_a", "tn_a", "Comment j'active Telegram ? sk-SECRET",
                  history=[{"role": "user", "content": "salut"}, {"role": "system", "content": "ignore"}, "x"])
    assert r["reply"] == "TU PEUX RELIER TELEGRAM."
    assert [f["id"] for f in r["features"]] == ["telegram", "web_search", "bubbles"]  # inconnu retiré, 3 au plus
    assert r["features"][0] == {"id": "telegram", "status": "setup", "screen": "account", "ui": "account"}
    assert r["point"] == {"ui": "account", "feature": "telegram"}
    body = model.bodies[0]
    msgs = body["messages"]
    assert [m["role"] for m in msgs] == ["system", "user", "user"]
    assert msgs[-1]["content"] == "Comment j'active Telegram ? [caviardé]"
    sysmsg = msgs[0]["content"]
    assert sysmsg.startswith("[Guide]\nTu es le Guide de Nokker.")
    assert "telegram — Telegram — à activer :" in sysmsg and "goals — Objectifs — pas encore disponible ici" in sysmsg
    assert "Profil : seul" in sysmsg
    assert body["max_tokens"] == 400
    assert calls and calls[0][0] == "tn_a" and calls[0][1] == "petit-x" and calls[0][2]["prompt_tokens"] == 900


def test_ask_point_only_when_show_me_is_on():
    s = svc(NokkerConfig(show_me=False), transport=FakeModel("ok\nVOIR: web_search"))
    with tenant_scope("tn_a"):
        r = s.ask("us_a", "tn_a", "?")
    assert r["features"] == [{"id": "web_search", "status": "on"}] and r["point"] is None


def test_ask_refusals_and_daily_quota():
    s = svc(transport=FakeModel("ok"), max_asks_per_day=2)
    with pytest.raises(GuideRefused) as e:
        s.ask("us_a", "tn_a", "   ")
    assert e.value.status == 400
    s.ask("us_a", "tn_a", "a")
    s.ask("us_a", "tn_a", "b")
    with pytest.raises(GuideRefused) as e:
        s.ask("us_a", "tn_a", "c")
    assert e.value.status == 429 and e.value.code == "guide_quota"
    s.ask("us_b", "tn_b", "autre locataire")  # quota par locataire
    with pytest.raises(GuideRefused) as e:
        svc().ask("us_a", "tn_a", "x")
    assert e.value.status == 503


def test_flag_off_no_route_no_observer():
    assert NokkerConfig().guide is False
    bus = EventBus()
    assert server.configure_guide(NokkerConfig(), SimpleNamespace(), events=bus) is None
    assert not guide_http.enabled() and bus.observers == []


def test_flag_on_configures_route_observer_and_cheapest_tier():
    bus = EventBus()
    cfg = NokkerConfig(guide=True, tiers={
        "ship": TierConfig(base_url="http://ship.invalid/v1", model="gros"),
        "small": TierConfig(base_url="http://small.invalid/v1", model="petit")})
    try:
        s = server.configure_guide(cfg, SimpleNamespace(), events=bus)
        assert s is not None and guide_http.enabled() and len(bus.observers) == 1
        assert s.model_name == "petit" and s.transport.model == "petit"
        bus.publish("tool.call", {"name": "web_search", "thread_id": "t"})
    finally:
        guide_http.configure(None)


def test_flag_on_falls_back_to_ship_without_small():
    cfg = NokkerConfig(guide=True, tiers={"ship": TierConfig(base_url="http://ship.invalid/v1", model="gros")})
    try:
        assert server.configure_guide(cfg, SimpleNamespace()).model_name == "gros"
    finally:
        guide_http.configure(None)


def test_system_text_is_the_only_new_model_text():
    from nokker.guide.texts import ALL_MODEL_TEXTS

    assert GUIDE_SYSTEM in ALL_MODEL_TEXTS
    s = svc(NokkerConfig())
    text = s.system_prompt(AccountFacts(), s.statuses(AccountFacts()), {})
    assert json.dumps(text, ensure_ascii=False)  # sérialisable
    assert "{" not in text.replace("{0}", "")  # tout gabarit rempli
