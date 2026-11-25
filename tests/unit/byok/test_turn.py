"""Un tour d'un locataire dont le défaut est `byok:<nom>` passe par SON point d'accès (base_url, modèle, clé), tous
paliers, sans repli vers le Router ; hors du quota du Router mais sous la garde de débit ; erreur lisible si le point
d'accès tombe ; les autres locataires restent sur le Router."""
from __future__ import annotations

from dataclasses import dataclass, field

from nokker.core.ports import Completion
from nokker.core.turn import Message, Tier, TurnInput, Usage
from nokker.route.policy import PolicyV1
from nokker.tenancy.context import tenant_scope
from nokker.tenancy.limits import LimitedPolicy, MemoryUsage, TenantLimits, day_window
from tests.unit.api.test_server import make_kernel
from tests.unit.byok.conftest import FakeEndpoint, make_service

KEY_A = "sk-tenant-A-0123456789abcdef"
EP = "https://api.example.com/v1"
A, B, POOR = "tn_aaaaaaaaaaaa", "tn_bbbbbbbbbbbb", "tn_cccccccccccc"


@dataclass
class Router:
    calls: list[str] = field(default_factory=list)

    def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048):
        self.calls.append(session_key)
        return Completion(message=Message(role="assistant", content="réponse du Router"),
                          usage=Usage(prompt=1, completion=1), model="router", latency_s=0.01)


def world(hard=50):
    router = Router()
    kernel = make_kernel()
    kernel.providers = {Tier.SHIP: router, Tier.DEEP: router, Tier.SMALL: router}
    kernel.limits = TenantLimits(counter=MemoryUsage(), mode="hard", turns_per_day=40, turns_per_week=200,
                                 hard_turns_per_day=hard, deep_per_day=0)
    svc, ep = make_service({A, B}, FakeEndpoint(keys={"api.example.com": KEY_A}))
    kernel.byok = svc
    with tenant_scope(A):
        svc.put({"default": "byok:perso", "byok": [{"name": "perso", "base_url": EP, "model": "m-large",
                                                    "api_key": KEY_A}]})
    return kernel, router, svc, ep


def turn(kernel, tid, text="salut"):
    with tenant_scope(tid):
        return kernel.run_turn(TurnInput(thread_id=f"{tid}:th", user_text=text, channel="web", sender=f"us_{tid[3:]}"))


def used(kernel, tid, kind):
    day, _ = day_window(kernel.limits.clock())
    return kernel.limits.counter.read(tid, f"us_{tid[3:]}", day.date(), kind)


def test_turn_goes_to_the_tenant_endpoint_with_its_key_and_model():
    kernel, router, _, ep = world()
    t = turn(kernel, A)
    assert t.reply == "réponse de api.example.com (m-large)" and router.calls == []
    chat = [s for s in ep.seen if s["path"].endswith("/chat/completions")]
    assert len(chat) == 1 and chat[0]["auth"] == f"Bearer {KEY_A}" and chat[0]["body"]["model"] == "m-large"
    assert chat[0]["session"] is None  # nos identifiants de fil ne partent pas chez un tiers
    assert "reasoning_effort" not in chat[0]["body"]
    # hors du quota du Router, compté à part
    assert used(kernel, A, "turns") == 0 and used(kernel, A, "week_turns") == 0 and used(kernel, A, "byok_turns") == 1


def test_other_tenant_stays_on_the_router_and_never_sees_a_config():
    kernel, router, _, ep = world()
    n = len(ep.seen)
    t = turn(kernel, B)
    assert t.reply == "réponse du Router" and len(router.calls) == 1 and len(ep.seen) == n
    assert used(kernel, B, "turns") == 1


def test_deep_and_small_tiers_also_use_the_endpoint_without_counting_deep():
    kernel, router, _, ep = world()
    kernel.policy = LimitedPolicy(inner=PolicyV1(available=lambda: (Tier.SHIP, Tier.DEEP, Tier.SMALL)),
                                  limits=kernel.limits)
    for text in ("écris un script python qui trie un fichier", "merci"):
        assert turn(kernel, A, text).reply.startswith("réponse de api.example.com")
    assert router.calls == [] and used(kernel, A, "deep") == 0


def test_endpoint_down_gives_a_readable_error_without_router_fallback_or_key():
    kernel, router, _, ep = world()
    ep.down.add("api.example.com")  # 401 qui recopie l'en-tête Authorization
    t = turn(kernel, A)
    assert router.calls == [] and not t.escalated
    assert t.failure["code"] == "byok_unavailable" and "perso" in t.failure["message"]
    assert "Ton modèle « perso » ne répond pas" in t.reply
    assert KEY_A not in t.reply and KEY_A not in t.failure["message"]
    assert all(KEY_A not in v.reason for v in t.verdicts)


def test_rate_limit_still_applies_to_byok_turns():
    kernel, router, _, _ = world(hard=2)
    assert [turn(kernel, A).failure for _ in range(2)] == [None, None]
    t = turn(kernel, A)
    assert [v.rule for v in t.verdicts] == ["limits:byok_turns_per_day"] and router.calls == []


def test_lost_entitlement_falls_back_to_the_router_config_kept():
    kernel, router, svc, _ = world()
    svc.entitled = lambda tid: False
    svc.forget(A)
    assert turn(kernel, A).reply == "réponse du Router" and len(router.calls) == 1
    with tenant_scope(A):
        view = svc.view()
    assert view["default"] == "byok:perso" and view["effective"] == "router" and view["entitled"] is False


def byok_plan_world(only: set[str]):
    kernel, router, svc, ep = world()
    svc.byok_only = lambda tid: tid in only
    return kernel, router, svc, ep


def assert_refused_without_count(kernel, router, ep, tid):
    n = len(ep.seen)
    t = turn(kernel, tid)
    assert [v.rule for v in t.verdicts] == ["byok:not_configured"] and not t.calls
    assert t.failure["code"] == "byok_not_configured"
    assert "Mon compte → Modèles" in t.reply and "My account → Models" in t.reply
    assert router.calls == [] and len(ep.seen) == n
    for kind in ("turns", "week_turns", "byok_turns", "deep"):
        assert used(kernel, tid, kind) == 0


def test_byok_plan_without_byok_default_is_refused_not_routed():
    kernel, router, svc, ep = byok_plan_world({B})
    svc.entitled = lambda tid: tid in {A, B}
    assert_refused_without_count(kernel, router, ep, B)  # B : aucun réglage (défaut router)
    with tenant_scope(B):
        view = svc.view()
    assert view["router_included"] is False and view["effective"] == "none"


def test_byok_plan_with_entry_without_model_or_key_is_refused():
    kernel, router, svc, ep = byok_plan_world({A})
    with tenant_scope(A):  # entrée sans clé (la clé est retirée par un changement d'hôte), défaut dessus
        svc.put({"default": "byok:perso", "byok": [{"name": "perso", "base_url": "https://other.example.com/v1",
                                                    "model": "m"}]})
        assert svc.view()["effective"] == "none"
    assert_refused_without_count(kernel, router, ep, A)
    with tenant_scope(A):  # entrée sans modèle : le défaut ne peut pas y pointer, il revient à router
        svc.put({"byok": [{"name": "perso", "base_url": EP, "api_key": KEY_A}]})
    svc.forget(A)
    assert_refused_without_count(kernel, router, ep, A)


def test_byok_plan_with_usable_endpoint_is_served_by_it():
    kernel, router, svc, ep = byok_plan_world({A})
    t = turn(kernel, A)
    assert t.reply.startswith("réponse de api.example.com") and router.calls == []
    with tenant_scope(A):
        assert svc.view()["router_included"] is False and svc.view()["effective"] == "byok:perso"


def test_solo_keeps_the_router_with_its_quota():
    kernel, router, svc, _ = byok_plan_world(set())  # B en Solo : défaut router, rien ne change
    assert turn(kernel, B).reply == "réponse du Router" and used(kernel, B, "turns") == 1
    with tenant_scope(B):
        assert svc.view()["router_included"] is True and svc.view()["effective"] == "router"


def test_byok_only_plan_reads_the_billing():
    from nokker.byok.service import byok_only_plan

    class Billing:
        def active_plan(self, tid):
            return {A: "byok", B: "solo"}.get(tid)

    assert byok_only_plan(A, billing=Billing()) is True
    assert byok_only_plan(B, billing=Billing()) is False
    assert byok_only_plan(POOR, billing=Billing()) is False
    assert byok_only_plan(A, billing=None) is False
