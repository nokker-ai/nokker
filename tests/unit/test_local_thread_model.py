"""Modèle par fil (Lokkal, `PUT /v1/local/sessions/{fil}/model`) : le choix explicite de la personne prime sur le
routage par palier, un modèle inconnu est refusé, `null` efface ; un tour forcé sur une clé de la personne compte
comme BYOK, sur le compte relié il garde le quota du Router ; le fournisseur forcé n'a aucun repli."""
from __future__ import annotations

from dataclasses import dataclass, field

from nokker.byok.context import ByokRoute
from nokker.core.ports import Completion
from nokker.core.turn import Message, Tier, TurnInput, Usage
from nokker.local.api import LocalApi
from nokker.local.setup import LocalSetup, LocalVault
from nokker.tenancy.context import tenant_scope
from nokker.tenancy.limits import MemoryUsage, TenantLimits, day_window
from tests.unit.api.test_server import make_kernel


T = "tn_aaaaaaaaaaaa"


@dataclass
class Prov:
    name: str
    fail: bool = False
    calls: int = 0
    vision = False
    vision_probe = None
    max_context_tokens = 0
    model: str = field(default="m")

    def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048):
        self.calls += 1
        if self.fail:
            raise RuntimeError("point d'accès coupé")
        return Completion(message=Message(role="assistant", content=f"réponse de {self.name}"),
                          usage=Usage(prompt=1, completion=1), model=self.name, latency_s=0.01)


def _api(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("NOKKER_LOCAL_SECRET_KEY", Fernet.generate_key().decode())
    root = tmp_path / "projets"
    (root / "app").mkdir(parents=True)
    api = LocalApi.from_config({"token_path": str(tmp_path / "tok"), "code": {"roots": [str(root)],
                                                                             "backend": "local"}}, port=8742)
    api.setup = LocalSetup(state_path=tmp_path / "setup.json", vault=LocalVault(tmp_path / "vault.bin"),
                           detect=lambda: [], test=lambda base, key: {"ok": True, "models": ["m1", "m2"]})
    api.setup.put_models({"byok": [{"name": "perso", "base_url": "https://ex.test/v1", "model": "m1",
                                    "models": ["m1", "m2"], "ok": True, "api_key": "sk-k"}]})
    auth = f"Bearer {api.token}"
    tid = api.handle("POST", "/v1/local/sessions", auth, {"cwd": str(root / "app")})[1]["thread_id"]
    built: list[tuple] = []

    def resolve(model):
        entry = api.setup.model_catalog().get(model)
        if entry is None:
            return None
        built.append((model, *entry))
        return ByokRoute(name=model, provider=Prov(model), own_key=entry[2])

    api.resolve_model = resolve
    return api, auth, tid, built


def _model(api, auth, tid, body):
    return api.handle("PUT", f"/v1/local/sessions/{tid}/model", auth, body)


def _row(api, auth, tid):
    return next(s for s in api.handle("GET", "/v1/local/sessions", auth, {})[1]["sessions"] if s["thread_id"] == tid)


def test_put_sets_lists_and_clears_the_thread_model(tmp_path, monkeypatch):
    api, auth, tid, built = _api(tmp_path, monkeypatch)
    assert _row(api, auth, tid)["model"] is None
    assert _model(api, auth, tid, {"model": "m2"}) == (200, {"thread_id": tid, "model": "m2"})
    assert _row(api, auth, tid)["model"] == "m2" and api.route_for_thread(tid).name == "m2"
    assert api.route_for_thread(f"{tid}/sa-1").name == "m2"  # un sous-agent suit son fil
    assert built == [("m2", "https://ex.test/v1", "sk-k", True)]  # construit au PUT, avec la clé de l'entrée
    assert _model(api, auth, tid, {"model": None})[0] == 200
    assert _row(api, auth, tid)["model"] is None and api.route_for_thread(tid) is None


def test_unknown_model_thread_and_body_are_refused(tmp_path, monkeypatch):
    api, auth, tid, _ = _api(tmp_path, monkeypatch)
    _model(api, auth, tid, {"model": "m1"})
    st, body = _model(api, auth, tid, {"model": "inconnu"})
    assert st == 400 and body["error"]["code"] == "unknown_model"
    assert api.route_for_thread(tid).name == "m1"  # le refus ne touche pas au choix précédent
    for bad in ({}, {"model": 3}, [], None):
        assert _model(api, auth, tid, bad)[0] == 400
    assert _model(api, auth, "th_absent", {"model": "m1"})[0] == 404
    assert api.handle("PUT", f"/v1/local/sessions/{tid}/model", "Bearer faux", {"model": "m1"})[0] == 401
    assert "sk-k" not in repr(_row(api, auth, tid))


def test_catalog_marks_the_linked_account_model_as_not_the_persons_key(tmp_path, monkeypatch):
    api, _, _, _ = _api(tmp_path, monkeypatch)
    api.setup.vault.put("router", "rk-123")
    api.setup.router_default = "ship-27b"
    cat = api.setup.model_catalog()
    assert cat["m1"][2] is True and cat["ship-27b"][2] is False and cat["ship-27b"][1] == "rk-123"


def _world(route):
    router = Prov("router")
    kernel = make_kernel()
    kernel.providers = {Tier.SHIP: router, Tier.DEEP: router, Tier.SMALL: router}
    kernel.limits = TenantLimits(counter=MemoryUsage(), mode="hard", turns_per_day=40, turns_per_week=200,
                                 hard_turns_per_day=50, deep_per_day=0)
    kernel.thread_route = lambda tid: route if tid == "th_x" else None
    return kernel, router


def _used(kernel, kind):
    day, _ = day_window(kernel.limits.clock())
    return kernel.limits.counter.read(T, "us_a", day.date(), kind)


def _run(kernel, tid="th_x"):
    with tenant_scope(T):
        return kernel.run_turn(TurnInput(thread_id=tid, user_text="salut", channel="api", sender="us_a"))


def test_forced_model_prevails_over_tiers_and_other_threads_keep_them():
    forced = Prov("choisi")
    kernel, router = _world(ByokRoute(name="choisi", provider=forced))
    assert _run(kernel).reply == "réponse de choisi" and router.calls == 0 and forced.calls == 1
    assert _run(kernel, "th_y").reply == "réponse de router" and router.calls == 1


def test_forced_model_has_no_fallback_when_down():
    forced = Prov("choisi", fail=True)
    kernel, router = _world(ByokRoute(name="choisi", provider=forced))
    t = _run(kernel)
    assert router.calls == 0 and t.failure["code"] == "byok_unavailable" and "choisi" in t.failure["message"]


def test_forced_turn_counts_as_byok_only_with_the_persons_key():
    kernel, _ = _world(ByokRoute(name="choisi", provider=Prov("choisi"), own_key=True))
    _run(kernel)
    assert _used(kernel, "byok_turns") == 1 and _used(kernel, "turns") == 0
    kernel, _ = _world(ByokRoute(name="ship", provider=Prov("ship"), own_key=False))
    _run(kernel)
    assert _used(kernel, "byok_turns") == 0 and _used(kernel, "turns") == 1


def test_unreadable_thread_state_keeps_the_usual_routing():
    kernel, router = _world(None)
    kernel.thread_route = lambda tid: 1 / 0
    assert _run(kernel).reply == "réponse de router"


# ───────────── réponse d'un modèle muet : celle du fil n'emploie pas le vocabulaire du cloud ─────────────

def test_thread_model_down_reply_is_frozen_and_used_for_a_thread_choice():
    import hashlib

    from nokker.core.loop import THREAD_MODEL_DOWN_REPLY
    assert hashlib.sha256(THREAD_MODEL_DOWN_REPLY.encode()).hexdigest()[:16] == "56e44987511eacaa"
    kernel, router = _world(ByokRoute(name="choisi", provider=Prov("choisi", fail=True)))
    t = _run(kernel)
    reply = t.messages[-1].content
    assert reply == THREAD_MODEL_DOWN_REPLY.format(name="choisi", why="erreur inattendue", why_en="unexpected error")
    assert "Mon compte" not in reply and router.calls == 0


def test_cloud_byok_down_reply_is_unchanged():
    import hashlib

    from nokker.core.loop import BYOK_DOWN_REPLY, THREAD_MODEL_DOWN_REPLY

    class Svc:
        def route_for_turn(self):
            return ByokRoute(name="perso", provider=Prov("perso", fail=True))

    kernel, router = _world(None)
    kernel.byok = Svc()
    assert hashlib.sha256(BYOK_DOWN_REPLY.encode()).hexdigest()[:16] == "710396ac2e4216ce"
    reply = _run(kernel).messages[-1].content
    # Prov(fail=True) lève une erreur sans classe reconnue : classe « erreur inattendue », phrase d'action d'origine.
    assert reply == BYOK_DOWN_REPLY.format(name="perso", why="erreur inattendue", why_en="unexpected error")
    assert reply != THREAD_MODEL_DOWN_REPLY.format(name="perso", why="erreur inattendue", why_en="unexpected error")
