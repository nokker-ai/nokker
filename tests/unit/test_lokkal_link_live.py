"""Lien de compte Lokkal pris en compte SANS redémarrage (recette rc.8) : tunnel de la passerelle lancé à l'approbation
(une seule fois, arrêté au retrait du lien, `web_base` transmis) et clé du lien utilisée tout de suite par Ship, Deep,
le petit palier et les juges du Router. La clé n'apparaît dans aucune réponse ni aucun journal."""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet

from nokker.api.server import build_kernel, default_model_applier, link_endpoint
from nokker.config import load_config
from nokker.core.turn import Tier
from nokker.local.api import LocalApi
from nokker.local.setup import LocalLink, LocalSetup, LocalVault
from nokker.local.tunnel import Tunnel

KEY = "rk-LINK-0123456789abcdef"
TOKEN = "mt-MACHINE-0123456789abcdef"
RELAY = "wss://relay.nokker.app/v1/tunnel"
SHIPPED = Path(__file__).resolve().parents[2] / "deploy" / "lokkal" / "core-config.yaml"


def _cloud(req: httpx.Request) -> httpx.Response:
    if req.url.path == "/v1/link/device":
        return httpx.Response(200, json={"device_code": "dc", "user_code": "ABCD-1234",
                                         "verification_uri": "https://app.nokker.ai/link", "expires_in": 600,
                                         "interval": 5})
    if req.url.path == "/v1/link/token":
        return httpx.Response(200, json={"state": "approved", "api_key": KEY, "machine_token": TOKEN,
                                         "relay_host": "atelier.nokker.app", "machine_id": "m1", "account": "us_abc",
                                         "inference_base": INFERENCE})
    if req.url.path == "/v1/gateway/machine":
        return httpx.Response(200, json={"pages_public": False})
    return httpx.Response(200, json={})


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("NOKKER_LOCAL_SECRET_KEY", Fernet.generate_key().decode())
    monkeypatch.delenv("ROUTER_KEY", raising=False)
    root = tmp_path / "projets"
    root.mkdir()
    a = LocalApi.from_config({"state_dir": str(tmp_path / "etat"), "token_path": str(tmp_path / "tok"),
                              "code": {"roots": [str(root)], "backend": "local"}}, port=8742)
    a.setup = LocalSetup(state_path=tmp_path / "setup.json", vault=LocalVault(tmp_path / "vault.bin"), detect=lambda: [])
    a.link = LocalLink(setup=a.setup, client=httpx.Client(transport=httpx.MockTransport(_cloud)))
    return a


@pytest.fixture
def fake_tunnels(monkeypatch):
    """Pas de réseau : `start` ouvre un fil qui vit jusqu'à `stop`, comme le vrai tunnel (jamais deux en marche)."""
    started: list[Tunnel] = []

    def start(self: Tunnel) -> None:
        started.append(self)
        self._thread = threading.Thread(target=self._stop.wait, daemon=True)
        self._thread.start()

    monkeypatch.setattr(Tunnel, "start", start)
    return started


def _approve(api: LocalApi) -> dict:
    auth = f"Bearer {api.token}"
    assert api.handle("POST", "/v1/local/link/start", auth, {})[0] == 200
    status, body = api.handle("GET", "/v1/local/link/status", auth, {})
    assert status == 200 and body["state"] == "approved"
    return body


# ── tunnel ──

def test_tunnel_starts_when_the_link_is_approved_without_restart(api, fake_tunnels):
    api.tunnel_args = (RELAY, "http://127.0.0.1:8742", "http://web:8080")
    assert api.tunnel is None  # démarrage non lié : pas de tunnel
    _approve(api)
    [t] = fake_tunnels
    assert t is api.tunnel and t.running
    assert (t.url, t.local_base, t.web_base, t.machine_token) == (RELAY, "http://127.0.0.1:8742", "http://web:8080",
                                                                  TOKEN)
    assert api.relay_host == "atelier.nokker.app"


def test_no_second_tunnel_while_one_runs(api, fake_tunnels):
    api.tunnel_args = (RELAY, "http://127.0.0.1:8742", None)
    _approve(api)
    first = api.tunnel
    auth = f"Bearer {api.token}"
    for _ in range(3):  # le client sonde l'état tant que la page est ouverte : déjà lié, rien de relancé
        assert api.handle("GET", "/v1/local/link/status", auth, {})[1]["state"] == "approved"
    api.link_approved()  # même un second signal d'approbation
    assert api.start_tunnel(*api.tunnel_args) is False
    assert fake_tunnels == [first] and api.tunnel is first


def test_tunnel_stops_when_the_link_is_removed_and_restarts_on_a_new_link(api, fake_tunnels):
    api.tunnel_args = (RELAY, "http://127.0.0.1:8742", None)
    _approve(api)
    first = api.tunnel
    assert api.handle("DELETE", "/v1/local/link", f"Bearer {api.token}", {})[1] == {"state": "unlinked"}
    assert first._stop.is_set() and not first.running and first.state == "down"
    assert api.tunnel is None and api.relay_host is None
    first._thread.join(2)
    assert not first._thread.is_alive()
    _approve(api)  # nouveau lien : nouveau tunnel
    assert len(fake_tunnels) == 2 and api.tunnel is fake_tunnels[1] and api.tunnel.running


def test_no_tunnel_without_a_configured_relay(api, fake_tunnels):
    _approve(api)  # `local.relay_url` absent : aucune passerelle, rien ne part
    assert fake_tunnels == [] and api.tunnel is None


def test_stop_closes_an_open_connection():
    class WS:
        closed = False

        async def close(self):
            self.closed = True

    async def run():
        t = Tunnel(url=RELAY, machine_token="x", local_base="http://x")
        t._loop, t._ws, t.state = asyncio.get_running_loop(), WS(), "up"
        t.stop()
        await asyncio.sleep(0.05)
        return t

    t = asyncio.run(run())
    assert t._ws.closed and t.state == "down"


def test_tunnel_sends_non_api_paths_to_web_base():
    """Un GET / du navigateur sur <nom>.nokker.app va à l'interface web, /v1 et /healthz au noyau."""
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        return httpx.Response(200, content=b"ok")

    class WS:
        async def send(self, raw):
            pass

    async def run(web_base):
        t = Tunnel(url=RELAY, machine_token="x", local_base="http://core:8742", web_base=web_base)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            for path in ("/", "/assets/app.js", "/v1/local/sessions", "/healthz"):
                await t._forward(WS(), client, 1, {"method": "GET", "path": path, "headers": []}, b"")

    asyncio.run(run("http://web:8080"))
    assert seen == ["http://web:8080/", "http://web:8080/assets/app.js", "http://core:8742/v1/local/sessions",
                    "http://core:8742/healthz"]
    seen.clear()
    asyncio.run(run(None))  # sans web_base (config sans `local.web_base`) : tout part au noyau, d'où le 401
    assert set(u.split("/")[2] for u in seen) == {"core:8742"}


# ── clé d'inférence du lien ──

INFERENCE = "https://app.nokker.ai/v1/inference"  # `inference_base` rendu par nokker.ai au lien
ROUTER = "https://router.sokkan.ch/v1"


def _real_kernel(monkeypatch, caplog=None):
    """Noyau de `build_kernel` sur la config livrée : Ship, Deep, petit palier, reviewer et advisor RÉELS, tels que
    `serve` les a avant tout lien (ROUTER_KEY vide : paliers sans clé, juges sans clé)."""
    monkeypatch.delenv("ROUTER_KEY", raising=False)
    cfg = load_config(str(SHIPPED))
    cfg.pg_dsn_env = "TEST_NOKKER_ABSENT_DSN_VAR"
    cfg.mcp_tool_servers = []
    return cfg, build_kernel(cfg)


def _endpoint_fn(api):
    return lambda: link_endpoint(api.setup.state, api.setup.vault.get)


def _assert_all_on_the_link(kernel, cfg, base=INFERENCE):
    """Ship, Deep, petit palier, reviewer, advisor : base ET clé du lien (la clé du lien est refusée par le Router)."""
    for tier in (Tier.SHIP, Tier.DEEP, Tier.SMALL):
        assert (kernel.providers[tier].base_url, kernel.providers[tier].api_key) == (base, KEY), tier
    assert kernel.providers[Tier.SMALL].model == cfg.tiers["small"].model
    for role in (kernel.reviewer, kernel.advisor):  # l'avis de carte est kernel.advisor (une seule instance)
        assert (role.transport.base_url, role.transport.api_key) == (base, KEY)
        assert role.transport.model == cfg.judge.model  # le juge garde son modèle de la config


def test_hot_link_gives_the_link_endpoint_to_ship_small_and_both_judges(api, monkeypatch):
    cfg, kernel = _real_kernel(monkeypatch)
    assert kernel.reviewer.transport.api_key == "" and kernel.advisor.transport.api_key == ""
    assert kernel.reviewer.transport.base_url == ROUTER  # config livrée : le Router
    api.on_default_model = default_model_applier(cfg, kernel, _endpoint_fn(api))
    _approve(api)
    _assert_all_on_the_link(kernel, cfg)


def test_already_linked_startup_gives_the_link_endpoint_to_judges_too(api, monkeypatch):
    """Redémarrage d'un Lokkal déjà lié : `serve` appelle l'applier avec le défaut du coffre."""
    api.setup.vault.put("router", KEY)
    api.setup.state.update({"default": "router", "inference_base": INFERENCE})
    cfg, kernel = _real_kernel(monkeypatch)
    use = default_model_applier(cfg, kernel, _endpoint_fn(api))
    chosen = api.setup.provider_for_default()
    assert chosen[:2] == (INFERENCE, KEY)
    use(*chosen)
    _assert_all_on_the_link(kernel, cfg)


def test_old_link_without_inference_base_keeps_the_router(api, monkeypatch):
    api.setup.vault.put("router", KEY)
    api.setup.state["default"] = "router"
    cfg, kernel = _real_kernel(monkeypatch)
    default_model_applier(cfg, kernel, _endpoint_fn(api))(*api.setup.provider_for_default())
    _assert_all_on_the_link(kernel, cfg, base=ROUTER)


def test_env_router_key_wins_over_the_link_for_small_and_judges(api, monkeypatch):
    api.setup.vault.put("router", KEY)
    api.setup.state.update({"default": "router", "inference_base": INFERENCE})
    cfg, kernel = _real_kernel(monkeypatch)
    monkeypatch.setenv("ROUTER_KEY", "env-key-0123456789")
    default_model_applier(cfg, kernel, _endpoint_fn(api))(*api.setup.provider_for_default())
    assert kernel.providers[Tier.SHIP].api_key == KEY  # le défaut choisi (le lien) sert Ship et Deep, comme avant
    assert (kernel.providers[Tier.SMALL].base_url, kernel.providers[Tier.SMALL].api_key) == (ROUTER,
                                                                                            "env-key-0123456789")
    for role in (kernel.reviewer, kernel.advisor):
        assert (role.transport.base_url, role.transport.api_key) == (ROUTER, "env-key-0123456789")


def test_judges_follow_the_link_when_the_default_is_another_endpoint(api, monkeypatch):
    api.setup.vault.put("router", KEY)
    api.setup.state["inference_base"] = INFERENCE
    cfg, kernel = _real_kernel(monkeypatch)
    default_model_applier(cfg, kernel, _endpoint_fn(api))("https://byok.example/v1", "byok-key-0123456789", "autre")
    assert kernel.providers[Tier.SHIP].api_key == "byok-key-0123456789" and Tier.SMALL not in kernel.providers
    for role in (kernel.reviewer, kernel.advisor):  # juge de la config : le lien, pas la clé de l'autre point
        assert (role.transport.base_url, role.transport.api_key) == (INFERENCE, KEY)


def test_link_endpoint():
    get = lambda name: KEY if name == "router" else None  # noqa: E731
    assert link_endpoint({"inference_base": INFERENCE}, get) == (INFERENCE, KEY)
    assert link_endpoint({}, get) == (ROUTER, KEY)
    assert link_endpoint({}, lambda name: None) is None


def test_no_missing_key_warning_in_lokkal_config_and_one_when_still_keyless(api, monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    cfg, kernel = _real_kernel(monkeypatch)  # build_kernel : la clé du lien n'est pas encore lue
    assert "sans clé du juge" not in caplog.text
    api.setup.vault.put("router", KEY)
    api.setup.state.update({"default": "router", "inference_base": INFERENCE})
    default_model_applier(cfg, kernel, _endpoint_fn(api))(*api.setup.provider_for_default())
    assert all(r.transport.api_key for r in (kernel.reviewer, kernel.advisor))  # `serve` n'a rien à avertir


def test_non_lokkal_config_still_warns_without_a_judge_key(monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    monkeypatch.delenv("ROUTER_KEY", raising=False)
    cfg = load_config(str(SHIPPED))
    cfg.local = {}
    cfg.pg_dsn_env = "TEST_NOKKER_ABSENT_DSN_VAR"
    cfg.mcp_tool_servers = []
    build_kernel(cfg)
    assert "judge_review sans clé du juge" in caplog.text and "judge_advice sans clé du juge" in caplog.text


# ── la clé ne fuit pas ──

def test_the_link_key_never_leaks(api, fake_tunnels, caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    api.tunnel_args = (RELAY, "http://127.0.0.1:8742", None)
    cfg = load_config(str(SHIPPED))
    api.on_default_model = default_model_applier(cfg, _real_kernel(monkeypatch)[1], _endpoint_fn(api))
    auth = f"Bearer {api.token}"
    body = _approve(api)
    outputs = [json.dumps(body), json.dumps(api.handle("GET", "/v1/local/setup", auth, {})[1]),
               json.dumps(api.handle("GET", "/v1/local/link/status", auth, {})[1]), caplog.text]
    for out in outputs:
        assert KEY not in out and TOKEN not in out
    names = dict((v, n) for n, v in api.vault.known.items_by_length_desc())
    assert names[KEY] == "nokker:router" and names[TOKEN] == "nokker:machine_token"  # caviardée sans attendre 30 s


def test_a_failing_effect_does_not_break_the_link_nor_log_the_key(api, caplog):
    caplog.set_level(logging.DEBUG)

    def boom(*a):
        raise RuntimeError(f"échec avec {a[1]}")  # une erreur qui porterait la clé

    api.on_default_model = boom
    body = _approve(api)
    assert body["state"] == "approved" and KEY not in caplog.text
    assert "RuntimeError" in caplog.text and api.setup.vault.get("router") == KEY
