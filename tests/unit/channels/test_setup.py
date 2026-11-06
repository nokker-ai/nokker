"""Config des canaux : éteints par défaut, secrets par référence seulement, lus à la demande."""
from __future__ import annotations

import pytest

from nokker.channels.setup import build_channels
from nokker.config import NokkerConfig
from tests.unit.channels.helpers import FakeResolver, FakeSlackApi

SIGNING, APP_SECRET, VERIFY = "slack-signing-test", "meta-app-secret-test", "meta-verify-test"
VAULT = {"SLACK_BOT": "xoxb-test-token", "SLACK_SIGNING": SIGNING, "WA_TOKEN": "wa-access-test",
         "WA_APP_SECRET": APP_SECRET, "WA_VERIFY": VERIFY}
SLACK_BLOCK = {"enabled": True, "team_id": "T0TEST", "app_id": "A0TEST", "bot_user_id": "UBOT",
               "bot_token": "secret:SLACK_BOT", "signing_secret": "secret:SLACK_SIGNING"}
WA_BLOCK = {"enabled": True, "phone_number_id": "1234567890", "display_number": "+41 00 000 00 00",
            "access_token": "secret:WA_TOKEN", "app_secret": "secret:WA_APP_SECRET", "verify_token": "secret:WA_VERIFY"}


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
