"""`nokker/previews/ssrf.py` — garde SSRF. Aucune requête réseau : les IP littérales (127.0.0.1,
2130706433, ::1, 100.x…) se résolvent SANS DNS (c'est justement le point), et pour un nom d'hôte on
monkeypatch le résolveur (`resolver=` de `resolve_and_check`) plutôt que de taper un vrai DNS."""
from __future__ import annotations

import ipaddress
import socket

import pytest

from nokker.previews.ssrf import SsrfBlocked, blocked_reason, resolve_and_check


def _fake_resolver(mapping):
    def resolver(host, port, proto=socket.IPPROTO_TCP):
        ip = mapping[host]
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (ip, port))]
    return resolver


# ─────────────────────────────── schéma ───────────────────────────────


@pytest.mark.parametrize("url", ["ftp://example.com/", "file:///etc/passwd", "gopher://x/", "//no-scheme/"])
def test_rejects_non_http_schemes(url):
    with pytest.raises(SsrfBlocked, match="schéma"):
        resolve_and_check(url)


def test_rejects_url_without_host():
    with pytest.raises(SsrfBlocked, match="hôte"):
        resolve_and_check("http:///path")


# ─────────────────────────────── IP littérales (résolution LOCALE, pas de DNS) ───────────────────────────────


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/",
    "http://127.1/",            # forme courte
    "http://2130706433/",       # décimal == 127.0.0.1
    "http://0x7f000001/",       # hex == 127.0.0.1
    "http://017700000001/",    # octal == 127.0.0.1
    "http://[::1]/",
])
def test_rejects_loopback_in_all_numeric_forms(url):
    with pytest.raises(SsrfBlocked, match="loopback"):
        resolve_and_check(url)


@pytest.mark.parametrize("url,label", [
    ("http://10.0.0.1/", "privée"),
    ("http://172.16.5.4/", "privée"),
    ("http://192.168.1.1/", "privée"),
    ("http://169.254.1.1/", "link-local"),
    ("http://224.0.0.1/", "multicast"),
    ("http://0.0.0.0/", "spécifiée"),
])
def test_rejects_other_blocked_ipv4_ranges(url, label):
    with pytest.raises(SsrfBlocked, match=label):
        resolve_and_check(url)


@pytest.mark.parametrize("url", ["http://100.64.0.1/", "http://100.64.12.34/", "http://100.127.255.254/"])
def test_rejects_cgnat_tailscale_range(url):
    """100.64.0.0/10 est le réseau CGNAT (celui des réseaux privés type Tailscale) — refusé explicitement, pas
    seulement « parce que `is_private` le couvrirait peut-être »."""
    with pytest.raises(SsrfBlocked, match="CGNAT"):
        resolve_and_check(url)


def test_cgnat_boundary_100_63_is_not_blocked_by_cgnat_itself():
    # 100.63.255.255 est hors de 100.64.0.0/10 (borne juste avant) : pas de faux-positif sur la plage.
    assert blocked_reason(ipaddress.ip_address("100.63.255.255")) is None


def test_rejects_ipv6_ula_fc00():
    with pytest.raises(SsrfBlocked, match="ULA"):
        resolve_and_check("http://[fd12:3456:789a::1]/")


def test_rejects_ipv4_mapped_ipv6_loopback():
    # ::ffff:127.0.0.1 doit être démappé avant classification, sinon il passerait tel quel.
    with pytest.raises(SsrfBlocked, match="loopback"):
        resolve_and_check("http://[::ffff:127.0.0.1]/")


def test_allows_public_ip():
    hop = resolve_and_check("http://8.8.8.8/")
    assert hop.ip == "8.8.8.8"
    assert hop.scheme == "http"
    assert hop.port == 80


def test_default_ports():
    assert resolve_and_check("http://8.8.8.8/").port == 80
    assert resolve_and_check("https://8.8.8.8/").port == 443
    assert resolve_and_check("http://8.8.8.8:8080/").port == 8080


# ─────────────────────────────── résolveur mocké (nom d'hôte -> IP) ───────────────────────────────


def test_hostname_resolved_to_private_ip_is_blocked():
    """Simule une redirection vers un nom qui résout en interne (la classe de bug visée par la
    garde : un domaine public qui pointe vers du réseau privé — DNS rebinding ou simple faute de
    config)."""
    resolver = _fake_resolver({"interne.example.com": "10.0.0.5"})
    with pytest.raises(SsrfBlocked, match="privée"):
        resolve_and_check("http://interne.example.com/", resolver=resolver)


def test_hostname_resolved_to_hote21_tailscale_ip_is_blocked():
    resolver = _fake_resolver({"evil.example.com": "100.101.102.103"})
    with pytest.raises(SsrfBlocked, match="CGNAT"):
        resolve_and_check("http://evil.example.com/", resolver=resolver)


def test_hostname_resolved_to_public_ip_is_allowed():
    resolver = _fake_resolver({"public.example.com": "93.184.216.34"})
    hop = resolve_and_check("http://public.example.com/", resolver=resolver)
    assert hop.ip == "93.184.216.34"
    assert hop.hostname == "public.example.com"


def test_dns_failure_raises_blocked():
    def resolver(host, port, proto=socket.IPPROTO_TCP):
        raise socket.gaierror("introuvable")
    with pytest.raises(SsrfBlocked, match="résolution DNS"):
        resolve_and_check("http://nope.invalid/", resolver=resolver)


def test_any_blocked_address_among_several_blocks_the_whole_url():
    """Un résolveur qui renvoie une IP publique ET une IP privée pour le même nom : on refuse
    l'URL entière, on ne « choisit » pas la bonne — c'est en soi un signal de rebinding."""
    def resolver(host, port, proto=socket.IPPROTO_TCP):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port)),
        ]
    with pytest.raises(SsrfBlocked, match="loopback"):
        resolve_and_check("http://mixed.example.com/", resolver=resolver)
