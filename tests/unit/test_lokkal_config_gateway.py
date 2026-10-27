"""Config livrée de Lokkal : la passerelle est branchée d'office (sans `relay_url`, un Lokkal lié restait injoignable)."""
from __future__ import annotations

from pathlib import Path

from nokker.config import load_config

SHIPPED = Path(__file__).resolve().parents[2] / "deploy" / "lokkal" / "core-config.yaml"


def test_shipped_config_carries_the_gateway_relay_and_the_web_base():
    local = load_config(str(SHIPPED)).local
    assert local["relay_url"] == "wss://relay.nokker.app/v1/tunnel"  # docs/en/gateway.md
    assert local["web_base"] == "http://web:8080"  # service `web` du compose, nginx sur 8080 (web/lokkal.conf)
