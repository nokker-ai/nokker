"""Budget de projet (docs/LOKKAL.md §7.15) : plafond du jour et du mois, décompté sur le LEDGER (aucun second
compteur), refus clair AVANT l'appel modèle, vue par membre, droits owner/maintainer, rien d'un autre locataire."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from nokker.core.turn import LLMCall, Tier, Usage
from nokker.ledger.attribution import SpendTag, spend_tag
from nokker.ledger.costs import InMemoryLedger
from nokker.ledger.prices import load_from_yaml
from nokker.team.budget import ProjectBudget
from nokker.team.model import TeamError, clean_budget
from nokker.team.service import Team
from nokker.team.store import MemoryTeamStore
from nokker.tenancy.store import MemoryTenancy

EUR_MODEL = "Qwen3.5-397B-A17B"  # au catalogue en EUR : 0,60 € par million de jetons d'entrée
USD_MODEL = "qwen/qwen3.8-27b"  # au catalogue en USD


def call(eur_cents: int = 60, model: str = EUR_MODEL) -> LLMCall:
    """Un appel qui coûte `eur_cents` centimes (0,60 € = 1 M de jetons d'entrée au catalogue)."""
    return LLMCall(tier=Tier.SHIP, model=model, reasoning=False, prefix_sha256="", layer_tokens={},
                   usage=Usage(prompt=int(eur_cents * 1_000_000 / 60), completion=0), latency_s=0.0,
                   route_reason="test")


class Now:
    def __init__(self) -> None:
        self.t = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)  # lundi, 10:00 à Zurich

    def __call__(self) -> datetime:
        return self.t


@pytest.fixture
def w():
    tenancy = MemoryTenancy()
    a = {k: tenancy.create_account(f"{k}@example.org") for k in ("owner", "maint", "mem", "view", "out")}
    now = Now()
    team = Team(store=MemoryTeamStore(), tenancy=tenancy)
    pid = team.create(a["owner"], {"name": "Plateforme"})["id"]
    for who, role in (("maint", "maintainer"), ("mem", "member"), ("view", "viewer")):
        inv = team.invite(a["owner"], pid, {"email": f"{who}@example.org", "role": role})
        team.accept(a[who], inv["id"])
    ledger = InMemoryLedger(clock=now)
    threads = {"th_proj": pid}
    budget = ProjectBudget(team=team, ledger=ledger, prices=load_from_yaml(), resolve=threads.get, clock=now)
    team.budget = budget

    class W:
        pass

    x = W()
    x.__dict__.update(team=team, a=a, pid=pid, ledger=ledger, budget=budget, now=now, threads=threads,
                      tenancy=tenancy)
    return x


def spend(w, who: str, cents: int, *, model: str = EUR_MODEL, thread: str = "th_proj") -> None:
    acc = w.a[who]
    with spend_tag(SpendTag(w.pid, w.a["owner"].tenant_id, acc.account_id)):
        w.ledger.record(thread, 0, call(cents, model))


def _status(fn, *args):
    with pytest.raises(TeamError) as e:
        fn(*args)
    return e.value.status, e.value.code


def test_budget_shape_daily_and_monthly_in_euros_by_default():
    assert clean_budget({"daily": "5", "monthly": 100}) == {"daily": 5.0, "monthly": 100.0, "currency": "EUR"}
    assert clean_budget({"monthly": 12.5, "currency": "chf"}) == {"monthly": 12.5, "currency": "CHF"}
    assert clean_budget(None) is None
    for bad in ({}, {"daily": -1}, {"daily": "x"}, {"monthly": 10, "currency": "BTC"}, {"weekly": 3}):
        with pytest.raises(TeamError):
            clean_budget(bad)


def test_only_owner_and_maintainer_set_the_cap_and_it_is_journaled(w):
    for who in ("mem", "view"):
        assert _status(w.team.put_budget, w.a[who], w.pid, {"daily": 1}) == (403, "forbidden")
    assert _status(w.team.put_budget, w.a["out"], w.pid, {"daily": 1}) == (404, "not_found")
    got = w.team.put_budget(w.a["maint"], w.pid, {"daily": 1, "monthly": 10})
    assert got["budget"] == {"daily": 1.0, "monthly": 10.0, "currency": "EUR"}
    assert w.team.get(w.a["view"], w.pid)["budget"] == {"daily": 1.0, "monthly": 10.0, "currency": "EUR"}
    events = w.team.audit(w.a["owner"], w.pid)["events"]
    assert [e["event"] for e in events] == ["budget_set"]
    assert events[0]["actor_account"] == w.a["maint"].account_id
    assert events[0]["detail"] == {"daily": 1.0, "monthly": 10.0, "currency": "EUR"}
    assert _status(w.team.audit, w.a["mem"], w.pid) == (403, "forbidden")
    w.team.delete_budget(w.a["owner"], w.pid)
    assert w.team.get(w.a["mem"], w.pid)["budget"] is None
    assert w.team.audit(w.a["maint"], w.pid)["events"][0]["event"] == "budget_removed"  # le plus récent d'abord


def test_a_thread_outside_any_project_is_not_counted(w):
    assert w.budget.check("th_libre", w.a["mem"].account_id) is None


def test_attributed_turn_gets_the_tag_of_the_project_and_its_sender(w):
    w.team.put_budget(w.a["owner"], w.pid, {"daily": 1})
    ticket = w.budget.check("th_proj", w.a["mem"].account_id)
    assert ticket is not None and ticket.verdict is None
    assert ticket.tag == SpendTag(w.pid, w.a["owner"].tenant_id, w.a["mem"].account_id)
    # un fil de sous-agent compte pour le projet du fil de session
    assert w.budget.check("th_proj/sa-abc", w.a["mem"].account_id).tag == ticket.tag
    # un viewer (ou un non-membre) ne dépense pas pour le projet : pas d'attribution
    assert w.budget.check("th_proj", w.a["view"].account_id) is None
    assert w.budget.check("th_proj", w.a["out"].account_id) is None


def test_daily_cap_refuses_before_the_model_call_with_a_clear_reply(w):
    w.team.put_budget(w.a["owner"], w.pid, {"daily": 1.0, "monthly": 50})
    spend(w, "mem", 60)
    spend(w, "maint", 40)  # 1,00 € : plafond du jour atteint
    t = w.budget.check("th_proj", w.a["mem"].account_id)
    assert t.verdict is not None and t.verdict.rule == "project_budget:daily"
    assert t.verdict.decision.value == "deny"
    assert "Plateforme" in t.reply and "1.00 EUR" in t.reply and "minuit" in t.reply
    assert "Project budget" in t.reply  # même forme que les plafonds du locataire : français puis anglais
    # le lendemain (heure suisse), le jour repart ; le mois garde tout
    w.now.t = datetime(2026, 10, 26, 23, 30, tzinfo=UTC)  # 00:30 le 27 à Zurich
    t = w.budget.check("th_proj", w.a["mem"].account_id)
    assert t.verdict is None
    v = w.budget.view(w.a["owner"], w.pid)
    assert v["today"]["spent"] == 0 and v["month"]["spent"] == pytest.approx(1.0)


def test_monthly_cap_and_month_window(w):
    w.team.put_budget(w.a["owner"], w.pid, {"monthly": 1.2})
    spend(w, "mem", 60)
    w.now.t = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
    spend(w, "mem", 60)
    t = w.budget.check("th_proj", w.a["maint"].account_id)
    assert t.verdict.rule == "project_budget:monthly" and "1.20 EUR" in t.reply
    w.now.t = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)  # nouveau mois
    assert w.budget.check("th_proj", w.a["maint"].account_id).verdict is None


def test_eighty_percent_notice_once_when_a_turn_crosses_it(w):
    w.team.put_budget(w.a["owner"], w.pid, {"daily": 1.0})
    spend(w, "mem", 70)
    t = w.budget.check("th_proj", w.a["mem"].account_id)
    assert t.verdict is None
    notice = w.budget.notice(t, [call(20)])  # 0,70 → 0,90 : passe 80 %
    assert notice and "80 %" in notice and "Plateforme" in notice
    spend(w, "mem", 20)
    t2 = w.budget.check("th_proj", w.a["mem"].account_id)
    assert w.budget.notice(t2, [call(5)]) is None  # déjà au-delà : pas une seconde fois


def test_spending_in_a_currency_without_rate_is_refused_as_unverifiable(w):
    w.team.put_budget(w.a["owner"], w.pid, {"daily": 10})
    spend(w, "mem", 60, model=USD_MODEL)  # coût en USD, plafond en EUR, aucun taux posé
    t = w.budget.check("th_proj", w.a["mem"].account_id)
    assert t.verdict.rule == "project_budget:unverifiable" and "USD" in t.reply
    w.budget.rates = {"USD": 0.9}  # taux posé par la configuration (EUR pour 1 USD) : converti
    assert w.budget.check("th_proj", w.a["mem"].account_id).verdict is None
    assert w.budget.view(w.a["owner"], w.pid)["today"]["spent"] > 0


def test_budget_view_per_member_and_rights(w):
    w.team.put_budget(w.a["owner"], w.pid, {"daily": 2, "monthly": 20})
    spend(w, "mem", 60)
    spend(w, "maint", 30)
    v = w.budget.view(w.a["maint"], w.pid)
    assert v["budget"] == {"daily": 2.0, "monthly": 20.0, "currency": "EUR"}
    assert v["today"]["spent"] == pytest.approx(0.9) and v["today"]["cap"] == 2.0
    assert v["today"]["state"] == "ok" and v["month"]["cap"] == 20.0 and v["today"]["resets_at"]
    by = {m["email"]: m for m in v["members"]}
    assert by["mem@example.org"]["today"] == pytest.approx(0.6)
    assert by["maint@example.org"]["month"] == pytest.approx(0.3)
    assert by["view@example.org"]["today"] == 0
    # un member ou un viewer voient le total et leur propre ligne, pas celle des autres
    mine = w.budget.view(w.a["mem"], w.pid)
    assert mine["today"]["spent"] == pytest.approx(0.9)
    assert [m["email"] for m in mine["members"]] == ["mem@example.org"]
    assert _status(w.budget.view, w.a["out"], w.pid) == (404, "not_found")


def test_spending_outside_the_project_never_shows(w):
    # le même membre dépense hors projet (son locataire, sans étiquette) : rien dans la vue du projet
    w.ledger.record("th_libre", 0, call(500))
    other = w.team.create(w.a["mem"], {"name": "Perso"})["id"]
    with spend_tag(SpendTag(other, w.a["mem"].tenant_id, w.a["mem"].account_id)):
        w.ledger.record("th_perso", 0, call(500))
    v = w.budget.view(w.a["owner"], w.pid)
    assert v["today"]["spent"] == 0 and v["month"]["spent"] == 0


def test_a_member_who_left_keeps_his_past_spending_in_the_total(w):
    spend(w, "mem", 60)
    w.team.remove(w.a["mem"], w.pid, w.a["mem"].account_id)
    v = w.budget.view(w.a["owner"], w.pid)
    assert v["month"]["spent"] == pytest.approx(0.6)
    former = [m for m in v["members"] if m.get("former")]
    assert former and former[0]["account_id"] == w.a["mem"].account_id and former[0]["email"] is None
    assert w.budget.check("th_proj", w.a["mem"].account_id) is None  # plus membre : plus d'attribution


def test_spend_of_a_set_of_accounts_over_a_window_is_the_shared_counter(w):
    """Interface réutilisable (compteur du foyer, X23) : la dépense d'un ensemble de comptes sur une fenêtre, lue
    sur des lignes du ledger, convertie par les mêmes règles que le budget."""
    from datetime import timedelta

    spend(w, "mem", 60)
    spend(w, "maint", 30)
    w.now.t = w.now.t + timedelta(hours=2)
    spend(w, "mem", 10)
    since = w.now.t - timedelta(days=1)
    rows = w.ledger.fetch_project_calls(w.pid, w.a["owner"].tenant_id, since=since, actor=w.a["owner"].account_id)
    mem, maint = w.a["mem"].account_id, w.a["maint"].account_id
    got = w.budget.spend_of(rows, accounts={mem}, since=since)
    assert got["total"] == pytest.approx(0.7) and got["by_account"] == {mem: pytest.approx(0.7)}
    assert got["currency"] == "EUR" and got["unconverted"] == [] and got["unpriced_models"] == []
    both = w.budget.spend_of(rows, accounts={mem, maint}, since=since, until=w.now.t - timedelta(hours=1))
    assert both["total"] == pytest.approx(0.9)  # la fenêtre exclut la dernière dépense
    assert w.budget.spend_of(rows, since=since)["total"] == pytest.approx(1.0)  # accounts=None : tous
    spend(w, "mem", 60, model=USD_MODEL)
    rows = w.ledger.fetch_project_calls(w.pid, w.a["owner"].tenant_id, since=since, actor=w.a["owner"].account_id)
    assert w.budget.spend_of(rows, since=since)["unconverted"] == ["USD"]
