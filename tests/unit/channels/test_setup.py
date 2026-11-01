"""Config des canaux (éteints par défaut, secrets par référence seulement) et routes du vrai serveur (bêta)."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

import pytest

from nokker.channels import http as channels_http
from nokker.channels.setup import build_channels, configure_channels
from nokker.channels.slack import sign as slack_sign
from nokker.channels.whatsapp import sign as wa_sign
from nokker.config import NokkerConfig
from nokker.tenancy.context import tenant_scope

from tests.unit.channels.helpers import FakeSlackApi, FakeWaApi
from tests.unit.tenancy.test_isolation_suite import beta  # noqa: F401 — fixture du vrai serveur de la bêta

SIGNING, APP_SECRET, VERIFY = "slack-signing-test", "meta-app-secret-test", "meta-verify-test"
VAULT = {"SLACK_BOT": "xoxb-test-token", "SLACK_SIGNING": SIGNING, "WA_TOKEN": "wa-access-test",
         "WA_APP_SECRET": APP_SECRET, "WA_VERIFY": VERIFY}
SLACK_BLOCK = {"enabled": True, "team_id": "T0TEST", "app_id": "A0TEST", "bot_user_id": "UBOT",
               "bot_token": "secret:SLACK_BOT", "signing_secret": "secret:SLACK_SIGNING"}
WA_BLOCK = {"enabled": True, "phone_number_id": "1234567890", "display_number": "+41 00 000 00 00",
            "access_token": "secret:WA_TOKEN", "app_secret": "secret:WA_APP_SECRET", "verify_token": "secret:WA_VERIFY"}


class FakeResolver:
    def __init__(self, values):
        self.values, self.reads = values, []

    def value(self, name):
        self.reads.append(name)
        return self.values[name]


class FakeKernel:
    class invariants:  # noqa: N801
        from nokker.qa.secrets import KnownSecrets

        known = KnownSecrets()

        @staticmethod
        def scrub_output(text):
            return text

    def run_turn(self, inp):
        raise AssertionError("aucun tour attendu")


def test_channels_are_off_by_default():
    cfg = NokkerConfig()
    assert cfg.slack is None and cfg.whatsapp is None
    assert build_channels(cfg, None, FakeKernel(), None) == (None, None, [])
    cfg.slack, cfg.whatsapp = {**SLACK_BLOCK, "enabled": False}, {**WA_BLOCK, "enabled": False}
    assert build_channels(cfg, None, FakeKernel(), None, resolver=FakeResolver(VAULT)) == (None, None, [])


@pytest.mark.parametrize("key", ["bot_token", "signing_secret"])
def test_a_plaintext_secret_refuses_the_slack_channel(key):
    cfg = NokkerConfig()
    cfg.slack = {**SLACK_BLOCK, key: "xoxb-en-clair"}
    slack, wa, cards = build_channels(cfg, None, FakeKernel(), None, resolver=FakeResolver(VAULT))
    assert slack is None and cards == []


@pytest.mark.parametrize("key", ["access_token", "app_secret", "verify_token"])
def test_a_plaintext_secret_refuses_the_whatsapp_channel(key):
    cfg = NokkerConfig()
    cfg.whatsapp = {**WA_BLOCK, key: "en-clair"}
    assert build_channels(cfg, None, FakeKernel(), None, resolver=FakeResolver(VAULT))[1] is None


def test_secrets_are_read_lazily_and_join_the_scrubber():
    cfg, kernel, res = NokkerConfig(), FakeKernel(), FakeResolver(VAULT)
    cfg.slack = dict(SLACK_BLOCK)
    slack, _, _ = build_channels(cfg, None, kernel, None, resolver=res, slack_api=FakeSlackApi())
    assert res.reads == []  # rien lu au démarrage
    assert slack.signing_secret() == SIGNING and res.reads == ["SLACK_SIGNING"]
    assert "nokker:SLACK_SIGNING" in kernel.invariants.known.names()
    assert repr(slack.signing_secret) == "secret:SLACK_SIGNING"  # jamais la valeur dans un journal


def test_disabled_channel_routes_do_not_exist(beta):  # noqa: F811
    channels_http.configure(None, None)
    assert beta.call("POST", "/v1/slack/events", {"type": "url_verification", "challenge": "x"})[0] == 404
    # GET inconnu : le serveur demande une identité avant de dire 404 (comportement existant) — jamais le challenge
    st, body = beta.call("GET", "/v1/whatsapp/webhook?hub.mode=subscribe&hub.verify_token=&hub.challenge=1")
    assert st in (401, 404) and body != "1"
    assert beta.call("POST", "/v1/whatsapp/webhook", {})[0] == 404
    st, body = beta.call("POST", "/v1/slack/pairing", {}, who="A")
    assert st == 503 and body == {"error": "slack_disabled"}


@pytest.fixture
def wired(beta):  # noqa: F811
    beta.config.slack, beta.config.whatsapp = dict(SLACK_BLOCK), dict(WA_BLOCK)
    beta.config.app_url = "https://app.nokker.ai"
    slack_api, wa_api = FakeSlackApi(), FakeWaApi()
    configure_channels(beta.config, beta.tenancy, beta.kernel, None, beta.approvals, resolver=FakeResolver(VAULT),
                       slack_api=slack_api, whatsapp_api=wa_api, sync=True)
    beta.slack_api, beta.wa_api = slack_api, wa_api

    def raw(method, path, body: bytes | None, headers):
        req = urllib.request.Request(f"http://127.0.0.1:{beta.port}{path}", method=method, data=body,
                                     headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def slack(obj, *, secret=SIGNING, ts=None):
        body = json.dumps(obj).encode()
        ts = int(time.time()) if ts is None else ts
        return raw("POST", "/v1/slack/events", body, {"Content-Type": "application/json",
                                                       "X-Slack-Request-Timestamp": str(ts),
                                                       "X-Slack-Signature": slack_sign(secret, ts, body)})

    def wa(obj, *, secret=APP_SECRET):
        body = json.dumps(obj).encode()
        return raw("POST", "/v1/whatsapp/webhook", body, {"Content-Type": "application/json",
                                                           "X-Hub-Signature-256": wa_sign(secret, body)})

    beta.raw, beta.slack, beta.wa = raw, slack, wa
    try:
        yield beta
    finally:
        channels_http.configure(None, None)


def test_slack_end_to_end_on_the_real_server(wired):
    w = wired
    assert w.slack({"type": "url_verification", "challenge": "abc"}) == (200, b'{"challenge": "abc"}')
    assert w.slack({"type": "url_verification", "challenge": "abc"}, secret="faux")[0] == 401
    assert w.slack({"type": "url_verification", "challenge": "abc"}, ts=int(time.time()) - 600)[0] == 401
    st, body = w.call("POST", "/v1/slack/pairing", {}, who="A")
    assert st == 200 and body["command"] == f"pair {body['code']}" and "app_redirect?app=A0TEST" in body["link"]

    def event(eid, text, user="U1"):
        return w.slack({"type": "event_callback", "team_id": "T0TEST", "api_app_id": "A0TEST", "event_id": eid,
                        "event": {"type": "message", "channel_type": "im", "channel": "D1", "user": user,
                                  "text": text, "ts": "1.1"}})

    assert event("Ev1", body["command"])[0] == 200
    w.model.load([], "réponse slack")
    assert event("Ev2", "bonjour")[0] == 200 and event("Ev2", "bonjour")[0] == 200  # rejeu : 200, un seul tour
    assert [p for p in w.slack_api.posts if p[1] == "réponse slack"] == [("D1", "réponse slack", None)]
    with tenant_scope(w.a.tenant_id):
        assert len(w.kernel.store.history(f"{w.a.tenant_id}:sl-T0TEST-D1")) == 1
    with tenant_scope(w.b.tenant_id):
        assert w.kernel.store.history(f"{w.b.tenant_id}:sl-T0TEST-D1") == []
    st, _ = w.raw("POST", "/v1/slack/interactivity", b"payload=%7B%7D", {"X-Slack-Request-Timestamp": "1",
                                                                          "X-Slack-Signature": "v0=00"})
    assert st == 401


def test_whatsapp_end_to_end_on_the_real_server(wired):
    w = wired
    ok = w.raw("GET", f"/v1/whatsapp/webhook?hub.mode=subscribe&hub.verify_token={VERIFY}&hub.challenge=987", None, {})
    assert ok == (200, b"987")
    assert w.raw("GET", "/v1/whatsapp/webhook?hub.mode=subscribe&hub.verify_token=x&hub.challenge=987", None, {})[0] \
        == 403
    st, body = w.call("POST", "/v1/whatsapp/pairing", {}, who="B")
    assert st == 200 and body["link"] == f"https://wa.me/41000000000?text=pair%20{body['code']}"

    def msg(wamid, text):
        return {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {
            "metadata": {"phone_number_id": "1234567890"},
            "messages": [{"from": "41790000002", "id": wamid, "timestamp": str(int(time.time())), "type": "text",
                          "text": {"body": text}}]}}]}]}

    assert w.wa(msg("wamid.1", body["command"]), secret="faux")[0] == 401
    assert w.wa(msg("wamid.1", body["command"]))[0] == 200
    assert w.tenancy.account_for_identity("whatsapp", "41790000002") == w.b
    w.model.load([], "réponse whatsapp")
    assert w.wa(msg("wamid.2", "salut"))[0] == 200
    assert w.wa_api.texts[-1] == ("41790000002", "réponse whatsapp")
    with tenant_scope(w.b.tenant_id):
        assert len(w.kernel.store.history(f"{w.b.tenant_id}:wa-41790000002")) == 1
