"""`nokker/previews/fetch.py` — récupération gardée. AUCUNE requête réseau : le client HTTP est
remplacé par `httpx.MockTransport` (`client_factory=`) et la résolution DNS par un résolveur en
mémoire (`resolver=`) — les deux points d'injection de `guarded_get`."""
from __future__ import annotations

import socket
import time

import httpx
import pytest

from nokker.previews.fetch import FetchError, guarded_get
from nokker.previews.ssrf import SsrfBlocked


def _resolver(mapping):
    def resolver(host, port, proto=socket.IPPROTO_TCP):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (mapping[host], port))]
    return resolver


def _mock_factory(handler):
    def factory(hop, timeout_s):
        return httpx.Client(transport=httpx.MockTransport(handler), timeout=timeout_s, follow_redirects=False)
    return factory


# ─────────────────────────────── cas simple ───────────────────────────────


def test_simple_get_returns_body():
    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html>ok</html>")

    result = guarded_get("http://8.8.8.8/page", max_bytes=1000, client_factory=_mock_factory(handler))
    assert result.status_code == 200
    assert result.body == b"<html>ok</html>"
    assert result.final_url == "http://8.8.8.8/page"
    assert result.redirects == 0


# ─────────────────────────────── redirections : suivies, re-vérifiées, bornées ───────────────────────────────


def test_follows_redirect_to_allowed_target():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if str(request.url) == "http://8.8.8.8/start":
            return httpx.Response(302, headers={"location": "http://8.8.4.4/final"})
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"final page")

    result = guarded_get("http://8.8.8.8/start", max_bytes=1000, client_factory=_mock_factory(handler))
    assert result.final_url == "http://8.8.4.4/final"
    assert result.body == b"final page"
    assert result.redirects == 1
    assert calls == ["http://8.8.8.8/start", "http://8.8.4.4/final"]


def test_redirect_to_private_ip_is_blocked_even_if_initial_host_is_public():
    """Le cœur de la garde « re-vérifiée à CHAQUE redirection » : la première URL est publique, la
    redirection pointe en interne — doit être bloquée à CE moment-là, pas suivie."""
    resolver = _resolver({"public.example.com": "93.184.216.34", "interne.example.com": "100.64.12.34"})

    def handler(request):
        if "public.example.com" in str(request.url):
            return httpx.Response(302, headers={"location": "http://interne.example.com/secret"})
        raise AssertionError("ne doit jamais être appelé : la redirection doit être bloquée avant")

    with pytest.raises(SsrfBlocked, match="CGNAT"):
        guarded_get("http://public.example.com/", max_bytes=1000, client_factory=_mock_factory(handler),
                   resolver=resolver)


def test_redirect_to_rebinding_decimal_ip_is_blocked():
    def handler(request):
        return httpx.Response(302, headers={"location": "http://2130706433/evil"})

    with pytest.raises(SsrfBlocked, match="loopback"):
        guarded_get("http://8.8.8.8/start", max_bytes=1000, client_factory=_mock_factory(handler))


def test_too_many_redirects_is_blocked():
    counter = {"n": 0}

    def handler(request):
        counter["n"] += 1
        return httpx.Response(302, headers={"location": f"http://8.8.8.8/hop{counter['n']}"})

    with pytest.raises(SsrfBlocked, match="trop de redirections"):
        guarded_get("http://8.8.8.8/start", max_bytes=1000, max_redirects=3, client_factory=_mock_factory(handler))
    assert counter["n"] <= 5  # pas de boucle infinie


def test_redirect_without_location_is_blocked():
    def handler(request):
        return httpx.Response(302)

    with pytest.raises(SsrfBlocked, match="Location"):
        guarded_get("http://8.8.8.8/", max_bytes=1000, client_factory=_mock_factory(handler))


# ─────────────────────────────── limites de taille ───────────────────────────────


def test_body_over_max_bytes_is_rejected_by_streaming_check():
    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"x" * 5000)

    with pytest.raises(FetchError, match="octets"):
        guarded_get("http://8.8.8.8/big", max_bytes=1000, client_factory=_mock_factory(handler))


def test_body_over_max_bytes_rejected_fast_via_content_length_header():
    """Un `Content-Length` mensonger-vers-le-haut n'a pas besoin d'être lu en entier pour être
    refusé (défense en profondeur ; voir aussi le test précédent qui couvre le cas SANS l'en-tête,
    ou avec un en-tête faux-vers-le-bas)."""
    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/html", "content-length": "999999"},
                              content=b"x" * 10)

    with pytest.raises(FetchError, match="annoncé"):
        guarded_get("http://8.8.8.8/big", max_bytes=1000, client_factory=_mock_factory(handler))


def test_body_at_exactly_max_bytes_is_allowed():
    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"x" * 1000)

    result = guarded_get("http://8.8.8.8/exact", max_bytes=1000, client_factory=_mock_factory(handler))
    assert len(result.body) == 1000


# ─────────────────────────────── délai global ───────────────────────────────


def test_global_timeout_spans_all_redirects_not_per_hop():
    """Le budget de temps est GLOBAL : une suite de redirections qui dépasse `timeout_s` au total
    échoue, même si chaque sauts pris isolément tiendrait dans la fenêtre."""
    def handler(request):
        time.sleep(0.05)
        return httpx.Response(302, headers={"location": "http://8.8.8.8/next"})

    with pytest.raises(FetchError, match="délai dépassé"):
        guarded_get("http://8.8.8.8/start", max_bytes=1000, timeout_s=0.12, max_redirects=10,
                   client_factory=_mock_factory(handler))


# ─────────────────────────────── garde PII sur l'URL ───────────────────────────────


def test_pii_email_in_url_is_refused_before_any_request():
    called = {"n": 0}

    def handler(request):
        called["n"] += 1
        return httpx.Response(200)

    with pytest.raises(SsrfBlocked, match="personnelle"):
        guarded_get("http://8.8.8.8/?u=alice@example.com", max_bytes=1000, client_factory=_mock_factory(handler))
    assert called["n"] == 0


def test_check_pii_false_bypasses_the_guard_for_our_own_generated_urls():
    """`check_pii=False` : réservé aux URLs qu'on construit nous-mêmes (oEmbed, image dérivée de la
    page) — jamais pour l'URL saisie par l'utilisateur (voir `service.py`)."""
    def handler(request):
        return httpx.Response(200, content=b"ok")

    result = guarded_get("http://8.8.8.8/?u=alice@example.com", max_bytes=1000,
                         client_factory=_mock_factory(handler), check_pii=False)
    assert result.body == b"ok"
