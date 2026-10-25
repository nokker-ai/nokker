"""Inférence relayée de nokker.ai (`POST /v1/inference/chat/completions`, jeton de machine) : comptée dans le locataire
de la machine, sous les plafonds du compte, modèles de la config du noyau seulement, débit et taille bornés.

Revue du train du 2.11 (§0, décision 1) : l'appel partait sur la clé du Router de nokker.ai sans plafond, sans modèle
imposé, sans limite, et `record_usage` levait `NoTenant` hors de tout `tenant_scope` (exception avalée)."""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from http.server import ThreadingHTTPServer

import pytest

from nokker.api.server import make_handler
from nokker.cloud import http as cloud_http
from nokker.cloud.http import CloudLink
from nokker.cloud.link import LinkService, Signer
from nokker.tenancy.context import current_tenant
from nokker.tenancy.limits import MemoryUsage, TenantLimits
from tests.unit.api.test_server import make_kernel
from tests.unit.test_cloud_http import FakeRouter

MODEL = "qwen/qwen3.5-397b-a17b"


def _servers(cloud: CloudLink):
    router = ThreadingHTTPServer(("127.0.0.1", 0), FakeRouter)
    threading.Thread(target=router.serve_forever, daemon=True).start()
    cloud.router_base = f"http://127.0.0.1:{router.server_address[1]}/v1"
    cloud_http.configure(cloud)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(make_kernel(), {}))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return router, httpd


def _machine_token(links: LinkService, account="us_pierre", tenant="tn_pierre") -> str:
    d = links.device("atelier")
    links.approve(d["user_code"], account, tenant)
    return links.token(d["device_code"])["machine_token"]


def _post(port, body, token, raw: bytes | None = None, headers: dict | None = None):
    data = raw if raw is not None else json.dumps(body).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/inference/chat/completions", data=data,
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}",
                                          **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _user(text="bonjour"):
    return [{"role": "user", "content": text}]


def _tool_follow_up():
    return [{"role": "user", "content": "lance les tests"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "3 passed"}]


@pytest.fixture
def guarded():
    """CloudLink avec plafonds durs (2 tours par jour), liste de modèles et débit bas : le chemin du défaut 1."""
    links = LinkService()
    usage: list = []
    limits = TenantLimits(counter=MemoryUsage(), mode="hard", turns_per_day=2, turns_per_week=10,
                          hard_turns_per_day=150, clock=lambda: datetime(2026, 10, 26, 13, 0, tzinfo=UTC))
    cloud = CloudLink(links=links, signer=Signer.generate(), identity=lambda h: None, tenant_of=lambda a: None,
                      router_base="", router_key=lambda: "rk-nokker",
                      record_usage=lambda *a: usage.append((current_tenant(), *a)),
                      limits=limits, models=frozenset({MODEL}), default_model=MODEL,
                      inference_rate_max=5, inference_calls_per_day=4)
    router, httpd = _servers(cloud)
    yield httpd.server_address[1], links, usage, cloud
    cloud_http.configure(None)
    httpd.shutdown()
    router.shutdown()


def test_usage_is_recorded_inside_the_machine_tenant(guarded):
    port, links, usage, _ = guarded
    mt = _machine_token(links)
    assert _post(port, {"model": MODEL, "messages": _user()}, mt)[0] == 200
    # `record_usage` lit le locataire courant (comme PostgresLedger) : il doit être celui de la machine.
    assert [(u[0], u[1]) for u in usage] == [("tn_pierre", "tn_pierre")]


def test_counting_failure_is_logged_as_an_error(guarded, caplog):
    port, links, _, cloud = guarded
    mt = _machine_token(links)

    def broken(*a):
        raise RuntimeError("ledger en panne")

    cloud.record_usage = broken
    with caplog.at_level(logging.ERROR, logger="nokker.cloud.http"):
        assert _post(port, {"model": MODEL, "messages": _user()}, mt)[0] == 200  # la réponse est déjà partie
        deadline = time.monotonic() + 3  # le comptage suit l'envoi de la réponse, dans le fil du serveur
        while time.monotonic() < deadline and not any("comptage" in r.getMessage() for r in caplog.records):
            time.sleep(0.02)
    assert any(r.levelno >= logging.ERROR and "NON compté" in r.getMessage() for r in caplog.records)


def test_only_models_of_the_kernel_config_are_relayed(guarded):
    port, links, _, _ = guarded
    mt = _machine_token(links)
    FakeRouter.seen.clear()
    status, out = _post(port, {"model": "openai/gpt-5-pro", "messages": _user()}, mt)
    assert status == 400 and MODEL in out["error"]["message"]
    assert FakeRouter.seen == []  # rien n'est parti vers le Router
    assert _post(port, {"messages": _user()}, mt)[0] == 200  # sans modèle : celui de la config
    assert FakeRouter.seen[-1] == ("Bearer rk-nokker", MODEL)


def test_turns_count_against_the_account_caps(guarded):
    port, links, _, _ = guarded
    mt = _machine_token(links)
    assert _post(port, {"model": MODEL, "messages": _user("un")}, mt)[0] == 200
    # La suite d'un tour (dernier message : un résultat d'outil) n'ouvre pas de tour.
    assert _post(port, {"model": MODEL, "messages": _tool_follow_up()}, mt)[0] == 200
    assert _post(port, {"model": MODEL, "messages": _user("deux")}, mt)[0] == 200
    status, out = _post(port, {"model": MODEL, "messages": _user("trois")}, mt)
    assert status == 403 and out["error"]["code"] == "quota_exceeded" and "Limite du jour" in out["error"]["message"]


def test_calls_per_day_cap_holds_even_without_new_turns(guarded):
    port, links, _, _ = guarded
    mt = _machine_token(links)
    codes = [_post(port, {"model": MODEL, "messages": _tool_follow_up()}, mt)[0] for _ in range(5)]
    assert codes[:4] == [200] * 4 and codes[4] == 403


def test_rate_limit_per_machine(guarded):
    port, links, _, cloud = guarded
    cloud.inference_calls_per_day = 1000
    mt, other = _machine_token(links), _machine_token(links, "us_jean", "tn_jean")
    codes = [_post(port, {"model": MODEL, "messages": _tool_follow_up()}, mt)[0] for _ in range(6)]
    assert codes[:5] == [200] * 5 and codes[5] == 429
    assert _post(port, {"model": MODEL, "messages": _tool_follow_up()}, other)[0] == 200  # par machine


def test_oversized_body_is_refused(guarded):
    import http.client

    port, links, _, _ = guarded
    mt = _machine_token(links)
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.putrequest("POST", "/v1/inference/chat/completions")
    conn.putheader("Authorization", f"Bearer {mt}")
    conn.putheader("Content-Length", str(cloud_http.MAX_INFERENCE_BYTES + 1))  # refusé sur l'en-tête, corps non lu
    conn.endheaders()
    assert conn.getresponse().status == 413
    conn.close()


def test_configure_cloud_link_wires_tenant_scope_caps_and_models(monkeypatch):
    """Le noyau tel que `serve` le branche : ledger qui exige un locataire (comme PostgresLedger), plafonds du noyau,
    modèles des paliers qui partagent le point d'accès de Deep."""
    from nokker.api.server import configure_cloud_link
    from nokker.config import NokkerConfig, TierConfig

    router = ThreadingHTTPServer(("127.0.0.1", 0), FakeRouter)
    threading.Thread(target=router.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{router.server_address[1]}/v1"
    monkeypatch.setenv("NOKKER_GATEWAY_SIGNING_KEY", Signer.generate().private_pem.decode())
    monkeypatch.setenv("ROUTER_KEY", "rk-nokker")
    recorded = []

    class TenantLedger:  # comme PostgresLedger : hors tenant_scope, NoTenant
        def record(self, thread_id, turn_index, call):
            recorded.append((current_tenant(), thread_id, call.model, call.usage.prompt))

    class Tenancy:
        def account(self, account_id):
            return None

    config = NokkerConfig(tiers={"ship": TierConfig(base_url=base, model="petit"),
                                 "deep": TierConfig(base_url=base, model=MODEL),
                                 "ailleurs": TierConfig(base_url="http://autre.invalid/v1", model="autre")},
                          cloud_link={"base_url": "https://app.nokker.ai", "handoffs": False})
    limits = TenantLimits(counter=MemoryUsage(), mode="hard", turns_per_day=1, turns_per_week=10)
    configure_cloud_link(config, Tenancy(), None, TenantLedger(), limits=limits)
    c = cloud_http._cloud
    try:
        assert c is not None and c.models == frozenset({"petit", MODEL}) and c.limits is limits
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(make_kernel(), {}))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        port = httpd.server_address[1]
        mt = _machine_token(c.links)
        assert _post(port, {"model": "petit", "messages": _user()}, mt)[0] == 200
        assert recorded and recorded[0][0] == "tn_pierre" and recorded[0][2] == "petit"
        assert recorded[0][1].startswith("tn_pierre") and recorded[0][3] == 10
        assert _post(port, {"model": "autre", "messages": _user()}, mt)[0] == 400  # autre point d'accès
        assert _post(port, {"model": MODEL, "messages": _user()}, mt)[0] == 403  # 1 tour par jour
        httpd.shutdown()
    finally:
        cloud_http.configure(None)
        router.shutdown()
