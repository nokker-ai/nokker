"""Limites de débit des routes publiques du lien et de la passerelle (revue du train du 2.11, §2 : « `token|refresh|revoke`
sans limite de débit »).

- connexion par la passerelle (`/v1/gateway/authorize|token|refresh|revoke`, nokker/cloud/gateway_auth.py) : par IP
  (le relais les appelle tous depuis la sienne : limite large, réglable), par jeton de renouvellement, par compte pour
  `authorize` et le jeton de session ;
- flux d'appareil (`/v1/link/device|token|pending|approve`) : par IP, par `device_code` sondé, par compte ; et un code
  d'appareil (`user_code`, 8 caractères) ne se devine pas : essais faux bornés par compte et par IP, et nombre total
  d'essais faux auxquels un même code est exposé borné (au-delà, il meurt).
Toujours `429 {"error": "rate", "retry_after_s": n}`. Aucun réseau externe."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from nokker.api.server import make_handler
from nokker.cloud import http as cloud_http
from nokker.cloud.gateway_auth import GatewayAuth
from nokker.cloud.http import CloudLink
from nokker.cloud.link import _USER_CODE_ALPHABET, LinkService, Signer
from tests.unit.api.test_server import make_kernel

SESS = {"Bearer " + "sess-" + "pierre": "us_pierre", "Bearer " + "sess-" + "marie": "us_marie"}  # jetons de test
TENANTS = {"us_pierre": "tn_pierre", "us_marie": "tn_marie"}


@pytest.fixture
def cloud():
    links = LinkService()
    signer = Signer.generate()
    c = CloudLink(links=links, signer=signer, identity=lambda h: SESS.get(h.headers.get("Authorization", "")),
                  tenant_of=TENANTS.get, router_base="http://127.0.0.1:9/v1", router_key=lambda: "x",
                  gateway_auth=GatewayAuth(links=links, signer=signer))
    cloud_http.configure(c)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(make_kernel(), {}))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1], c
    cloud_http.configure(None)
    httpd.shutdown()


def _post(port, path, body=None, *, sess=None, ip="198.51.100.1", method="POST"):
    headers = {"Content-Type": "application/json", "CF-Connecting-IP": ip}
    if sess:
        headers["Authorization"] = f"Bearer sess-{sess}"
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers, method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _limited(resp) -> bool:
    status, body = resp
    if status != 429:
        return False
    assert body["error"] == "rate" and isinstance(body["retry_after_s"], int) and 1 <= body["retry_after_s"] <= 900
    return True


# ═════════════════════════════ passerelle ═════════════════════════════


@pytest.mark.parametrize("path,body", [("/v1/gateway/token", {"code": "x" * 43, "code_verifier": "v" * 50,
                                                              "host": "salon.nokker.app"}),
                                       ("/v1/gateway/refresh", {"refresh_token": "r" * 43, "host": "salon.nokker.app"}),
                                       ("/v1/gateway/revoke", {"refresh_token": "r" * 43})])
def test_gateway_routes_are_limited_per_ip(cloud, path, body):
    port, c = cloud
    c.gateway_rate_max = 5
    answers = [_post(port, path, dict(body, refresh_token=f"r{i:042d}") if "refresh_token" in body
                     else dict(body, code=f"c{i:042d}")) for i in range(5)]
    assert all(s in (200, 400) for s, _ in answers), answers
    assert _limited(_post(port, path, body))  # 6e appel de la même IP dans la minute
    assert not _limited(_post(port, path, body, ip="203.0.113.7"))  # une autre IP passe


def test_gateway_limit_is_shared_by_the_three_relay_routes(cloud):
    port, c = cloud
    c.gateway_rate_max = 3
    _post(port, "/v1/gateway/token", {"code": "c" * 43, "code_verifier": "v" * 50, "host": "a.nokker.app"})
    _post(port, "/v1/gateway/refresh", {"refresh_token": "r" * 43, "host": "a.nokker.app"})
    _post(port, "/v1/gateway/revoke", {"refresh_token": "s" * 43})
    assert _limited(_post(port, "/v1/gateway/refresh", {"refresh_token": "t" * 43, "host": "a.nokker.app"}))


def test_one_refresh_token_is_limited_whatever_the_ip(cloud):
    port, _ = cloud
    for i in range(cloud_http.GATEWAY_TOKEN_RATE_MAX):
        assert not _limited(_post(port, "/v1/gateway/refresh", {"refresh_token": "r" * 43, "host": "a.nokker.app"},
                                  ip=f"198.51.100.{i + 10}"))
    assert _limited(_post(port, "/v1/gateway/refresh", {"refresh_token": "r" * 43, "host": "a.nokker.app"},
                          ip="198.51.100.250"))
    assert _limited(_post(port, "/v1/gateway/revoke", {"refresh_token": "r" * 43}, ip="198.51.100.251"))


def test_authorize_and_session_token_are_limited_per_account(cloud):
    port, _ = cloud
    for _ in range(cloud_http.RATE_MAX):
        assert not _limited(_post(port, "/v1/gateway/token", {}, sess="pierre", ip="198.51.100.9"))
    assert _limited(_post(port, "/v1/gateway/token", {}, sess="pierre", ip="203.0.113.9"))
    for _ in range(cloud_http.RATE_MAX):
        assert not _limited(_post(port, "/v1/gateway/authorize", {"host": "x.nokker.app", "code_challenge": "c" * 43},
                                  sess="marie", ip="198.51.100.8"))
    assert _limited(_post(port, "/v1/gateway/authorize", {"host": "x.nokker.app", "code_challenge": "c" * 43},
                          sess="marie", ip="203.0.113.8"))


# ═════════════════════════════ flux d'appareil ═════════════════════════════


def test_device_route_and_token_polling_are_limited(cloud):
    port, _ = cloud
    for _ in range(cloud_http.RATE_MAX):
        assert _post(port, "/v1/link/device", {"machine": "atelier"}, ip="198.51.100.2")[0] == 200
    assert _limited(_post(port, "/v1/link/device", {"machine": "atelier"}, ip="198.51.100.2"))
    d = _post(port, "/v1/link/device", {"machine": "atelier"}, ip="198.51.100.3")[1]
    # un même device_code sondé trop vite, même depuis plusieurs IP
    for i in range(cloud_http.DEVICE_POLL_RATE_MAX):
        assert _post(port, "/v1/link/token", {"device_code": d["device_code"]}, ip=f"198.51.100.{i + 20}") == \
            (200, {"state": "pending"})
    assert _limited(_post(port, "/v1/link/token", {"device_code": d["device_code"]}, ip="198.51.100.99"))


def test_wrong_user_codes_lock_the_account_out_then_the_ip(cloud):
    port, _ = cloud
    d = _post(port, "/v1/link/device", {"machine": "atelier"}, ip="198.51.100.4")[1]
    good = d["user_code"]
    alphabet = _USER_CODE_ALPHABET
    wrong = [f"{alphabet[i % 28] * 4}-{alphabet[(i + 3) % 28] * 4}" for i in range(40)]
    wrong = [w for w in wrong if w != good]
    for w in wrong[:cloud_http.LINK_CODE_FAILS_MAX]:
        assert _post(port, "/v1/link/approve", {"user_code": w}, sess="pierre", ip="198.51.100.5")[0] == 404
    # verrouillé : même le bon code est refusé, depuis une autre IP aussi (le compte est verrouillé)
    assert _limited(_post(port, "/v1/link/approve", {"user_code": good}, sess="pierre", ip="203.0.113.5"))
    assert _limited(_post(port, f"/v1/link/pending?code={good}", None, sess="pierre", ip="203.0.113.5",
                          method="GET"))
    # changer de compte depuis la même IP ne remet pas les compteurs à zéro
    assert _limited(_post(port, "/v1/link/approve", {"user_code": wrong[-1]}, sess="marie", ip="198.51.100.5"))
    # un autre compte, une autre IP : le bon code passe
    assert _post(port, "/v1/link/approve", {"user_code": good}, sess="marie", ip="203.0.113.6")[1]["state"] == \
        "approved"


def test_wrong_codes_on_pending_count_too(cloud):
    port, _ = cloud
    for i in range(cloud_http.LINK_CODE_FAILS_MAX):
        assert _post(port, f"/v1/link/pending?code=ZZZZ-{i:04d}", None, sess="pierre", method="GET")[0] == 404
    assert _limited(_post(port, "/v1/link/pending?code=ZZZZ-9999", None, sess="pierre", method="GET"))


def test_a_user_code_dies_after_too_many_wrong_guesses_anywhere():
    """Borne par code, quels que soient les comptes et les IP de l'attaquant : un code exposé à `max_guesses_per_code`
    essais faux (tous comptes confondus) depuis sa création meurt ; la machine relance `nokker link`. Probabilité de
    le trouver avant : ≤ max_guesses_per_code / 28^8."""
    links = LinkService(max_guesses_per_code=50)
    d = links.device("atelier")
    assert len(d["user_code"].replace("-", "")) == 8 and set(d["user_code"].replace("-", "")) <= set(_USER_CODE_ALPHABET)
    assert len(_USER_CODE_ALPHABET) ** 8 > 3 * 10 ** 11
    for i in range(50):
        with pytest.raises(LookupError):
            links.approve(f"QQQQ-{i:04d}", f"us_attaquant{i}", "tn_x")
    assert links.pending(d["user_code"]) is None  # même réponse qu'un code inconnu : pas d'oracle
    with pytest.raises(LookupError):
        links.approve(d["user_code"], "us_pierre", "tn_pierre")
    assert links.token(d["device_code"]) == {"state": "expired"}
    # un code créé APRÈS ces essais a son propre budget
    d2 = links.device("atelier")
    assert links.approve(d2["user_code"], "us_pierre", "tn_pierre")["state"] == "approved"


def test_lokkal_client_treats_429_as_pending(tmp_path):
    """Le client du flux d'appareil (Lokkal) ne casse pas sur un 429 de sondage : `pending` et le délai à respecter."""
    import httpx

    from nokker.local.setup import LocalLink

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v1/link/device"):
            return httpx.Response(200, json={"device_code": "d" * 43, "user_code": "BCDF-GHJK",
                                             "verification_uri": "https://app.nokker.ai/link", "expires_in": 600,
                                             "interval": 5})
        return httpx.Response(429, json={"error": "rate", "retry_after_s": 7})

    flow = LocalLink(setup=_Setup(), client=httpx.Client(transport=httpx.MockTransport(handler)))
    flow.start("atelier")
    assert flow.status() == {"state": "pending", "retry_after_s": 7}


class _Vault:
    @staticmethod
    def names():
        return []


class _Setup:
    def __init__(self) -> None:
        self.vault, self.state = _Vault(), {}
