"""Inférence relayée (`POST /v1/inference/chat/completions`, jeton de machine) et foyer (X23, docs/LOKKAL.md §7.29.3) :
un appel qui OUVRE un tour compte comme un tour web au quota commun du foyer, avec la même garde que le noyau
(`HouseholdQuota.check`, puis `TenantLimits.check_turn(shared=True)`), et chaque appel d'un membre actif est rangé au
ledger avec l'étiquette du foyer (`SpendTag.household_id`). Aucun second compteur : le foyer relit le ledger.

Avant (rc.7) : `check_turn(machine.account)` sans le foyer, ledger sans étiquette ; un membre contournait le quota
commun par sa machine liée, et le payeur ne voyait pas cette consommation."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime
from http.server import ThreadingHTTPServer

import pytest

from nokker.api.server import make_handler
from nokker.cloud import http as cloud_http
from nokker.cloud.http import CloudLink, ledger_usage_recorder
from nokker.cloud.link import LinkService, Signer
from nokker.household.model import Settings
from nokker.household.quota import HouseholdQuota
from nokker.household.service import Household
from nokker.household.store import MemoryHouseholdStore
from nokker.ledger.costs import InMemoryLedger
from nokker.team.service import Team
from nokker.tenancy.context import tenant_scope
from nokker.tenancy.limits import MemoryUsage, TenantLimits
from nokker.tenancy.store import MemoryTenancy
from tests.unit.api.test_server import make_kernel
from tests.unit.test_cloud_http import FakeRouter

MODEL = "qwen/qwen3.5-397b-a17b"
NOW = datetime(2026, 10, 26, 13, 0, tzinfo=UTC)


def _post(port, body, token, session: str | None = None):
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
    if session:
        headers["X-Session-Id"] = session
    req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/inference/chat/completions",
                                 data=json.dumps(body).encode(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _opens(text="bonjour"):
    return {"model": MODEL, "messages": [{"role": "user", "content": text}]}


def _follow_up():
    return {"model": MODEL, "messages": [
        {"role": "user", "content": "lance les tests"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "3 passed"}]}


class World:
    pass


@pytest.fixture
def world():
    w = World()
    tenancy = MemoryTenancy()
    w.pay, w.mem, w.out = (tenancy.create_account(f"{n}@example.com") for n in ("payeur", "membre", "voisin"))
    team = Team(store=MemoryHouseholdStore(), tenancy=tenancy)
    w.hh = Household(team=team, settings=Settings(turns_per_day=3, turns_per_week=20, member_turns_per_day=None))
    with tenant_scope(w.pay.tenant_id):
        w.hh.create(w.pay, {"name": "Maison"})
        iv = w.hh.invite(w.pay, {"email": w.mem.email})
    with tenant_scope(w.mem.tenant_id):
        w.hh.accept(w.mem, iv["id"])
    w.ledger = InMemoryLedger(clock=lambda: NOW)
    w.quota = HouseholdQuota(household=w.hh, ledger=w.ledger, clock=lambda: NOW)
    w.hh.quota = w.quota
    w.links = LinkService()
    # Plafond de COMPTE plus bas que le quota du foyer : un membre suit le foyer (`shared`), pas son compte.
    limits = TenantLimits(counter=MemoryUsage(), mode="hard", turns_per_day=1, turns_per_week=10,
                          hard_turns_per_day=150, clock=lambda: NOW)
    w.cloud = CloudLink(links=w.links, signer=Signer.generate(), identity=lambda h: None, tenant_of=lambda a: None,
                        router_base="", router_key=lambda: "rk-nokker",
                        record_usage=ledger_usage_recorder(w.ledger, MODEL),
                        limits=limits, models=frozenset({MODEL}), default_model=MODEL,
                        inference_rate_max=50, inference_calls_per_day=50, household=w.quota)
    router = ThreadingHTTPServer(("127.0.0.1", 0), FakeRouter)
    threading.Thread(target=router.serve_forever, daemon=True).start()
    w.cloud.router_base = f"http://127.0.0.1:{router.server_address[1]}/v1"
    cloud_http.configure(w.cloud)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(make_kernel(), {}))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    w.port = httpd.server_address[1]
    yield w
    cloud_http.configure(None)
    httpd.shutdown()
    router.shutdown()


def _machine(w, acc) -> str:
    d = w.links.device("atelier")
    w.links.approve(d["user_code"], acc.account_id, acc.tenant_id)
    return w.links.token(d["device_code"])["machine_token"]


def _used(w) -> int:
    return w.quota.usage(w.hh.membership(w.pay.account_id)[1], w.pay.account_id)["today"]["used"]


def test_member_calls_are_tagged_with_the_household_in_the_ledger(world):
    w = world
    mt, ot = _machine(w, w.mem), _machine(w, w.out)
    assert _post(w.port, _opens(), mt, "th_a")[0] == 200
    assert _post(w.port, _follow_up(), mt, "th_a")[0] == 200
    assert _post(w.port, _opens(), ot)[0] == 200
    pid = w.hh.view(w.pay)["id"]
    rows = w.ledger.fetch_calls()
    mine = [r for r in rows if r.thread_id.endswith(f"lokkal-{w.links.machines_of(w.mem.account_id)[0].id}")]
    assert len(mine) == 2
    assert {(r.household_id, r.household_tenant, r.account) for r in mine} == {
        (pid, w.pay.tenant_id, w.mem.account_id)}
    assert {r.project_id for r in rows} == {None}  # pas de projet d'équipe sans moyen fiable de le relier (§7.27)
    others = [r for r in rows if r not in mine]
    assert len(others) == 1 and others[0].household_id is None  # un non-membre n'est pas attribué


def test_a_turn_is_counted_once_and_its_follow_ups_are_not(world):
    w = world
    mt = _machine(w, w.mem)
    assert _post(w.port, _opens("un"), mt, "th_a")[0] == 200
    assert _post(w.port, _follow_up(), mt, "th_a")[0] == 200
    assert _post(w.port, _follow_up(), mt, "th_a")[0] == 200
    assert _used(w) == 1  # un tour ouvert, deux suites : un seul tour au foyer
    assert _post(w.port, _opens("deux"), mt, "th_b")[0] == 200
    assert _post(w.port, _follow_up(), mt, "th_b")[0] == 200
    assert _used(w) == 2
    rows = [r for r in w.ledger.fetch_calls() if r.household_id]
    by_turn: dict[int, int] = {}
    for r in rows:
        by_turn[r.turn_index] = by_turn.get(r.turn_index, 0) + 1
    assert sorted(by_turn.values()) == [2, 3]  # chaque suite rangée sous le tour de SA session


def test_household_quota_refuses_an_opening_call_before_the_router(world):
    w = world
    mt = _machine(w, w.mem)
    for text in ("un", "deux", "trois"):
        assert _post(w.port, _opens(text), mt)[0] == 200
    FakeRouter.seen.clear()
    status, out = _post(w.port, _opens("quatre"), mt)
    assert status == 403 and out["error"]["code"] == "quota_exceeded"
    assert "foyer" in out["error"]["message"] and "3 messages par jour" in out["error"]["message"]
    assert FakeRouter.seen == []  # rien n'est parti vers le Router
    # La suite d'un tour déjà commencé n'est pas coupée (un tour commencé finit, comme dans le noyau).
    assert _post(w.port, _follow_up(), mt)[0] == 200


def test_web_turns_and_relayed_turns_share_the_same_counter(world):
    """Un tour du noyau (étiquette posée par `Kernel.run_turn`) et un tour relayé se comptent ensemble."""
    from nokker.core.turn import LLMCall, Tier, Usage
    from nokker.ledger.attribution import spend_tag

    w = world
    ticket = w.quota.check("th_web", w.pay.account_id)
    call = LLMCall(tier=Tier.DEEP, model=MODEL, reasoning=False, prefix_sha256="", layer_tokens={},
                   usage=Usage(prompt=1, completion=1), latency_s=0.1, route_reason="test")
    with tenant_scope(w.pay.tenant_id), spend_tag(ticket.tag):
        w.ledger.record("th_web", 0, call)
        w.ledger.record("th_web", 1, call)
    mt = _machine(w, w.mem)
    assert _post(w.port, _opens(), mt)[0] == 200
    assert _used(w) == 3
    assert _post(w.port, _opens(), mt)[0] == 403  # 2 tours web + 1 relayé = le quota de 3


def test_member_follows_the_household_not_its_account_cap(world):
    """Plafond de compte : 1 tour par jour ; membre du foyer (`shared`) : seul le quota commun coupe."""
    w = world
    mt = _machine(w, w.mem)
    assert [_post(w.port, _opens(str(i)), mt)[0] for i in range(3)] == [200, 200, 200]
    out_t = _machine(w, w.out)
    assert [_post(w.port, _opens(str(i)), out_t)[0] for i in range(2)] == [200, 403]  # hors foyer : son compte


def test_mirror_copy_of_a_relayed_turn_is_not_counted_twice(world):
    """Le miroir range les tours poussés par la machine SANS leurs appels (nokker/cloud/mirror.py) : le foyer, qui
    lit le ledger, ne les compte pas une seconde fois."""
    from nokker.cloud import mirror
    from nokker.core.turn import InputCheck, LLMCall, Message, Tier, Turn, Usage
    from nokker.store.pg import MemoryStore, turn_to_dict

    w = world
    mt = _machine(w, w.mem)
    assert _post(w.port, _opens(), mt, "th_l")[0] == 200
    before = (len(w.ledger.fetch_calls()), _used(w))
    call = LLMCall(tier=Tier.DEEP, model=MODEL, reasoning=False, prefix_sha256="", layer_tokens={},
                   usage=Usage(prompt=1, completion=1), latency_s=0.1, route_reason="lokkal")
    local = Turn(thread_id="th_l", index=0, input=InputCheck(text="bonjour"), channel="api",
                 sender=w.mem.account_id, reply="ok", messages=[Message("assistant", "ok")], calls=[call])
    mid = w.links.machines_of(w.mem.account_id)[0].id
    got = mirror.push(MemoryStore(), w.mem.tenant_id, mid, [{"thread_id": "th_l", "turn": turn_to_dict(local)}])
    assert got["accepted"] == 1
    assert (len(w.ledger.fetch_calls()), _used(w)) == before


def test_server_hands_the_household_quota_to_the_relay(monkeypatch):
    """`configure_household` (après `configure_cloud_link`) pose le quota du foyer du noyau sur le relais d'inférence."""
    from types import SimpleNamespace

    from nokker.api import server
    from nokker.config import NokkerConfig
    from nokker.household import http as household_http
    from nokker.onboarding import http as onboarding_http
    from nokker.team import http as team_http

    monkeypatch.delenv("NOKKER_PG_DSN", raising=False)
    config = NokkerConfig(team=True, household=True)
    tenancy = MemoryTenancy()
    kernel = SimpleNamespace(ledger=InMemoryLedger(), memory=object(), gates=None, tools=object(), context=object(),
                             household=None)
    cloud = CloudLink(links=LinkService(), signer=Signer.generate(), identity=lambda h: None,
                      tenant_of=lambda a: None, router_base="", router_key=lambda: "")
    cloud_http.configure(cloud)
    try:
        server.configure_team(config, tenancy)
        server.configure_household(config, kernel, tenancy, None)
        assert kernel.household is not None and cloud.household is kernel.household
    finally:
        cloud_http.configure(None)
        household_http.configure(None)
        team_http.configure(None)
        onboarding_http.configure_minors(None)
