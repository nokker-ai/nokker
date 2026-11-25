"""Garde réseau du BYOK cloud (nokker/byok/guard.py) : https seulement, toutes les IP du nom publiques, et la garde
revérifiée à CHAQUE connexion (re-résolution DNS, connexion à l'IP vérifiée, aucun autre hôte)."""
from __future__ import annotations

import httpcore
import pytest

from nokker.byok.guard import BlockedEndpoint, GuardedBackend, check_endpoint, normalize_base_url
from tests.unit.byok.conftest import PUBLIC, fake_resolver


@pytest.mark.parametrize("url,code", [
    ("http://api.example.com/v1", "bad_base_url"),
    ("ftp://api.example.com", "bad_base_url"),
    ("https://user:pw@api.example.com/v1", "bad_base_url"),
    ("https://api.example.com/v1?x=1", "bad_base_url"),
    ("https:///v1", "bad_base_url"),
    ("", "bad_base_url"),
    (None, "bad_base_url"),
])
def test_form_is_https_without_credentials(url, code):
    with pytest.raises(BlockedEndpoint) as e:
        normalize_base_url(url)
    assert e.value.code == code


@pytest.mark.parametrize("host", [
    "intranet.example.com", "metadata.example.com", "loop.example.com", "ula.example.com", "tailnet.example.com",
    "mixed.example.com", "10.1.2.3", "127.0.0.1", "169.254.169.254", "[::1]", "[fd12::5]", "[::ffff:10.0.0.1]",
    "[64:ff9b::a00:1]", "localhost-unknown.invalid",
])
def test_private_loopback_link_local_metadata_ula_cgnat_are_refused(host):
    with pytest.raises(BlockedEndpoint) as e:
        check_endpoint(f"https://{host}/v1", resolver=fake_resolver())
    assert e.value.code == "blocked_address"


def test_public_endpoint_passes_and_is_normalized():
    assert check_endpoint("https://api.example.com/v1/", resolver=fake_resolver()) == "https://api.example.com/v1"


class _Inner(httpcore.SyncBackend):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, int]] = []

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.calls.append((host, port))
        return object()


def test_every_connection_resolves_again_and_pins_the_checked_ip():
    dns = {"api.example.com": [PUBLIC]}
    inner = _Inner()
    backend = GuardedBackend("https://api.example.com/v1", resolver=fake_resolver(dns), inner=inner)
    backend.connect_tcp("api.example.com", 443)
    assert inner.calls == [(PUBLIC, 443)]  # connexion à l'IP vérifiée, pas au nom
    dns["api.example.com"] = ["10.0.0.7"]  # DNS rebinding : le nom bascule vers le privé après l'enregistrement
    with pytest.raises(httpcore.ConnectError, match="adresse refusée"):
        backend.connect_tcp("api.example.com", 443)
    dns["api.example.com"] = ["169.254.169.254"]
    with pytest.raises(httpcore.ConnectError, match="adresse refusée"):
        backend.connect_tcp("api.example.com", 443)
    assert inner.calls == [(PUBLIC, 443)]


def test_no_other_host_or_port_than_the_endpoint():
    inner = _Inner()
    backend = GuardedBackend("https://api.example.com/v1", resolver=fake_resolver(), inner=inner)
    for host, port in (("other.example.com", 443), ("api.example.com", 8443), ("10.0.0.1", 443)):
        with pytest.raises(httpcore.ConnectError, match="connexion refusée"):
            backend.connect_tcp(host, port)
    with pytest.raises(httpcore.ConnectError):
        backend.connect_unix_socket("/var/run/docker.sock")
    assert inner.calls == []
