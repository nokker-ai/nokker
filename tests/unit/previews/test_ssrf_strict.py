"""Garde SSRF en mode strict (`ssrf.strict`, chantier p2/ssrf-audit) : la garde elle-même, puis UN bloc par client
qu'elle protège désormais (récupération web gardée, serveurs MCP ajoutés, compétences par URL).

Aucune requête vers l'extérieur : résolveur en mémoire, `httpx.MockTransport`, et pour la preuve d'épinglage un petit
serveur HTTP sur la boucle locale. Mode éteint : comportement historique (prouvé en fin de fichier)."""
from __future__ import annotations

import asyncio
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import httpx
import pytest

from nokker.core.ports import ToolContext
from nokker.previews import ssrf
from nokker.previews.fetch import guarded_get
from nokker.previews.ssrf import (
    ResolvedHop,
    SsrfBlocked,
    SsrfPolicy,
    configure,
    current_policy,
    parse_allow_entry,
    pinned_async_client_v2,
    resolve_and_check,
)

STRICT = {"strict": True}
CTX = ToolContext(thread_id="t-ssrf", turn_index=0, channel="api")

# Cibles internes : (URL, IP renvoyée par le faux résolveur pour `interne.example`, fragment attendu du message).
TARGETS = [
    ("http://127.0.0.1/", None, "loopback"),
    ("http://169.254.169.254/latest/meta-data/", None, "link-local"),
    ("http://10.0.0.5/", None, "privée (RFC1918)"),
    ("http://192.168.1.10/", None, "privée (RFC1918)"),
    ("http://100.64.0.1/", None, "CGNAT"),
    ("http://100.100.100.100/", None, "CGNAT"),  # plage du tailnet
    ("http://[::1]/", None, "loopback"),
    ("http://[fd12:3456::1]/", None, "ULA"),
    ("http://[fe80::1]/", None, "link-local"),
    ("http://[::ffff:169.254.169.254]/", None, "link-local"),
    ("http://0.0.0.0/", None, "non spécifiée"),
    ("http://interne.example/", "10.1.2.3", "privée (RFC1918)"),
]


@pytest.fixture(autouse=True)
def _reset_policy():
    configure(None)
    yield
    configure(None)


def _resolver(mapping):
    """`mapping` : hôte -> IP ou liste d'IP. Les IP littérales se résolvent en elles-mêmes (comme getaddrinfo)."""
    calls = []

    def resolver(host, port, proto=socket.IPPROTO_TCP):
        calls.append(host)
        ips = mapping.get(host, host)
        ips = ips if isinstance(ips, list) else [ips]
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ips]

    resolver.calls = calls
    return resolver


def _mapping_for(url, ip):
    return {"interne.example": ip} if ip else {}


# ─────────────────────────────── la garde ───────────────────────────────


def test_policy_from_config_defaults_off():
    assert current_policy() == SsrfPolicy()
    assert current_policy().strict is False
    pol = SsrfPolicy.from_config({"strict": True, "allow": ["localhost:8765", "[::1]:9000", "sans-port", 3],
                                  "ports": [443, 0, True, 70000]})
    assert pol.strict and pol.allow == frozenset({("localhost", 8765), ("::1", 9000)})
    assert pol.ports == frozenset({443})
    assert SsrfPolicy.from_config({"strict": "yes"}).strict is False  # seul `true` allume
    assert parse_allow_entry("Mcp.Local.:8080") == ("mcp.local", 8080)
    assert parse_allow_entry("hote:80/chemin") is None


@pytest.mark.parametrize("url,ip,fragment", TARGETS)
def test_strict_refuses_internal_targets(url, ip, fragment):
    with pytest.raises(SsrfBlocked) as e:
        resolve_and_check(url, resolver=_resolver(_mapping_for(url, ip)), policy=SsrfPolicy(strict=True))
    assert str(e.value).startswith("IP refusée (") and fragment in str(e.value)


@pytest.mark.parametrize("host", ["2130706433", "0177.0.0.1", "0x7f.0.0.1", "127.1", "0x7f000001", "017700000001"])
def test_decimal_octal_hex_forms_resolve_to_loopback_and_are_refused(host):
    # getaddrinfo (vrai, sans réseau : forme numérique) fait la conversion ; la classification suit.
    with pytest.raises(SsrfBlocked, match=r"IP refusée \(loopback\)"):
        resolve_and_check(f"http://{host}/", policy=SsrfPolicy(strict=True))


@pytest.mark.parametrize("ip,fragment", [
    ("64:ff9b::a9fe:a9fe", "169.254.169.254"),     # NAT64 vers les métadonnées
    ("2002:a9fe:a9fe::1", "6to4 vers 169.254.169.254, link-local"),
    ("2002:7f00:1::1", "6to4 vers 127.0.0.1, loopback"),
    ("fec0::1", "site-local (fec0::/10)"),
])
def test_strict_ipv6_embedded_and_site_local(ip, fragment):
    with pytest.raises(SsrfBlocked) as e:
        resolve_and_check("http://v6.example/", resolver=_resolver({"v6.example": ip}), policy=SsrfPolicy(strict=True))
    assert fragment in str(e.value)


def test_teredo_embedded_ipv4_is_classified():
    # Teredo : l'IPv4 cliente est stockée inversée dans les 32 derniers bits (RFC 4380).
    ip = "2001:0:4136:e378:8000:63bf:f5ff:fffe"  # client 10.0.0.1
    assert ssrf.strict_blocked_reason(ssrf.ipaddress.ip_address(ip)) == "Teredo vers 10.0.0.1, privée (RFC1918)"


def test_rebinding_mixed_answers_refused_and_single_resolution():
    res = _resolver({"rebind.example": ["93.184.216.34", "127.0.0.1"]})
    with pytest.raises(SsrfBlocked, match=r"IP refusée \(loopback\) pour rebind.example : 127.0.0.1"):
        resolve_and_check("https://rebind.example/", resolver=res, policy=SsrfPolicy(strict=True))
    res = _resolver({"ok.example": "93.184.216.34"})
    hop = resolve_and_check("https://ok.example/", resolver=res, policy=SsrfPolicy(strict=True))
    assert (hop.ip, hop.hostname, hop.port) == ("93.184.216.34", "ok.example", 443) and res.calls == ["ok.example"]


def test_strict_ports():
    pol = SsrfPolicy(strict=True)
    res = _resolver({"pub.example": "93.184.216.34"})
    with pytest.raises(SsrfBlocked, match=r"^port 6379 refusé \(autorisés : 80, 443, 8080, 8443\)$"):
        resolve_and_check("http://pub.example:6379/", resolver=res, policy=pol, enforce_ports=True)
    assert resolve_and_check("http://pub.example:6379/", resolver=res, policy=pol).port == 6379  # appelant sans ports
    assert resolve_and_check("https://pub.example:8443/", resolver=res, policy=pol, enforce_ports=True).port == 8443
    with pytest.raises(SsrfBlocked, match=r"^port invalide dans l'URL$"):
        resolve_and_check("http://pub.example:99999/", resolver=res, policy=pol)
    open_pol = SsrfPolicy(strict=True, ports=frozenset())
    assert resolve_and_check("http://pub.example:6379/", resolver=res, policy=open_pol, enforce_ports=True).port == 6379


def test_allowlist_is_host_and_port_exact_and_never_link_local():
    pol = SsrfPolicy(strict=True, allow=frozenset({("localhost", 8765), ("ollama", 11434)}))
    res = _resolver({"localhost": "127.0.0.1", "ollama": "172.18.0.4", "metadata": "169.254.169.254"})
    assert resolve_and_check("http://localhost:8765/mcp", resolver=res, policy=pol, enforce_ports=True).ip == "127.0.0.1"
    assert resolve_and_check("http://ollama:11434/", resolver=res, policy=pol, enforce_ports=True).ip == "172.18.0.4"
    with pytest.raises(SsrfBlocked, match="loopback"):
        resolve_and_check("http://localhost:8766/mcp", resolver=res, policy=pol)  # autre port : pas autorisé
    with pytest.raises(SsrfBlocked, match="link-local"):
        resolve_and_check("http://metadata:80/", resolver=res,
                          policy=SsrfPolicy(strict=True, allow=frozenset({("metadata", 80)})))


def test_policy_off_is_historical():
    res = _resolver({"pub.example": "93.184.216.34", "v6.example": "fec0::1"})
    # Ports non vérifiés, site-local historique accepté, port illisible = ValueError comme avant.
    assert resolve_and_check("http://pub.example:6379/", resolver=res, enforce_ports=True).port == 6379
    assert resolve_and_check("http://v6.example/", resolver=res, enforce_ports=True).ip == "fec0::1"
    with pytest.raises(ValueError):
        resolve_and_check("http://pub.example:99999/", resolver=res)
    with pytest.raises(SsrfBlocked, match=r"^IP refusée \(loopback\) pour 127.0.0.1 : 127.0.0.1$"):
        resolve_and_check("http://127.0.0.1/", resolver=res)


# ─────────────────────────────── connexion épinglée (client async du SDK mcp) ───────────────────────────────


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/redir":
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
            self.end_headers()
            return
        body = json.dumps({"host": self.headers.get("Host")}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def local_server():
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv.server_address[1]
    srv.shutdown()


def test_pinned_async_client_connects_to_verified_ip_only(local_server):
    port = local_server
    # Le nom n'existe pas dans le DNS : si le client résolvait lui-même, il échouerait. Il va à l'IP vérifiée.
    hop = ResolvedHop(url=f"http://mcp.invalid:{port}/", scheme="http", hostname="mcp.invalid", port=port,
                      ip="127.0.0.1")

    async def go():
        async with pinned_async_client_v2(hop, timeout_s=5) as c:
            ok = await c.get(f"http://mcp.invalid:{port}/x")
            redir = await c.get(f"http://mcp.invalid:{port}/redir")
            with pytest.raises(Exception) as other:
                await c.get(f"http://autre.invalid:{port}/x")
            return ok, redir, other.value

    ok, redir, other = asyncio.run(go())
    assert ok.status_code == 200 and ok.json()["host"] == f"mcp.invalid:{port}"
    assert redir.status_code == 302  # jamais suivie
    assert "n'est pas l'hôte vérifié" in str(other)


# ─────────────────────────────── client 1 : récupération web gardée (web_fetch, aperçus, pièces jointes) ───────


def _never(hop, timeout_s):
    raise AssertionError("aucune connexion ne doit partir")


@pytest.mark.parametrize("url,ip,fragment", TARGETS)
def test_guarded_get_strict_refuses(url, ip, fragment):
    configure(STRICT)
    with pytest.raises(SsrfBlocked, match=fragment.replace("(", r"\(").replace(")", r"\)")):
        guarded_get(url, max_bytes=1000, client_factory=_never, resolver=_resolver(_mapping_for(url, ip)))


def test_guarded_get_strict_redirect_to_private_and_port():
    configure(STRICT)

    def handler(request):
        return httpx.Response(302, headers={"location": "http://192.168.0.1/admin"})

    def factory(hop, timeout_s):
        return httpx.Client(transport=httpx.MockTransport(handler), timeout=timeout_s, follow_redirects=False)

    with pytest.raises(SsrfBlocked, match=r"IP refusée \(privée \(RFC1918\)\) pour 192.168.0.1"):
        guarded_get("http://8.8.8.8/start", max_bytes=1000, client_factory=factory)
    with pytest.raises(SsrfBlocked, match=r"^port 22 refusé"):
        guarded_get("http://8.8.8.8:22/", max_bytes=1000, client_factory=_never)


def test_guarded_get_strict_rebinding_refused():
    configure(STRICT)
    res = _resolver({"rebind.example": ["8.8.8.8", "10.0.0.1"]})
    with pytest.raises(SsrfBlocked, match="10.0.0.1"):
        guarded_get("http://rebind.example/", max_bytes=1000, client_factory=_never, resolver=res)


# ─────────────────────────────── client 2 : serveurs MCP ajoutés par l'utilisateur ───────────────────────────────


def _user_sources(ssrf_cfg, url, origin="user"):
    from nokker.api.server import mcp_tool_sources
    from nokker.config import NokkerConfig
    from nokker.tools.effects import EffectsTable

    configure(ssrf_cfg)
    cfg = NokkerConfig(mcp_tool_servers=[{"name": "perso", "url": url, **({"origin": origin} if origin else {})}])
    [src] = mcp_tool_sources(cfg, EffectsTable.load({}), None)
    return src.inner  # SecretScrubbingToolSource -> McpToolSource


@pytest.mark.parametrize("url,ip,fragment", [t for t in TARGETS if t[1] is None])
def test_user_mcp_strict_refuses_before_any_connection(url, ip, fragment):
    src = _user_sources(STRICT, url.rstrip("/") + "/mcp" if "meta-data" not in url else url)
    assert src.guard is not None
    with pytest.raises(SsrfBlocked) as e:
        asyncio.run(src._io.session_async())
    assert fragment in str(e.value) and src._io._session is None


def test_user_mcp_strict_specs_empty_and_call_message():
    src = _user_sources(STRICT, "http://127.0.0.1:8080/mcp")
    try:
        assert src.specs() == []
        r = src.call("lire", {}, CTX)
        assert r.is_error
        assert r.content == "erreur MCP perso/lire: IP refusée (loopback) pour 127.0.0.1 : 127.0.0.1"
    finally:
        src.close()


def test_user_mcp_strict_dns_rebinding_and_redirect(monkeypatch, local_server):
    # Le nom résout vers une IP publique ET la boucle locale : refusé. Autorisé par la liste : la session est épinglée
    # et la redirection du serveur n'est jamais suivie (le SDK reçoit le 302 tel quel).
    monkeypatch.setattr(socket, "getaddrinfo", _resolver({"mcp.example": ["8.8.8.8", "127.0.0.1"]}))
    src = _user_sources(STRICT, "https://mcp.example/mcp")
    with pytest.raises(SsrfBlocked, match="loopback"):
        asyncio.run(src._io.session_async())
    port = local_server
    monkeypatch.setattr(socket, "getaddrinfo", _resolver({"mcp.example": "127.0.0.1"}))
    src = _user_sources({"strict": True, "allow": [f"mcp.example:{port}"]}, f"http://mcp.example:{port}/redir")
    hop = src.guard(src.url)
    assert hop.ip == "127.0.0.1"

    async def go():
        async with pinned_async_client_v2(hop) as c:
            return await c.get(src.url)

    assert asyncio.run(go()).status_code == 302


def test_user_mcp_unguarded_when_off_or_from_config():
    assert _user_sources({}, "http://127.0.0.1:8765/mcp").guard is None              # drapeau éteint
    assert _user_sources(STRICT, "http://127.0.0.1:8765/mcp", origin=None).guard is None  # serveur de config.yaml


def test_user_servers_tagged_origin(tmp_path):
    from nokker.config import NokkerConfig
    from nokker.tools.packs import with_user_extensions

    (tmp_path / "mcp-servers.yaml").write_text("servers:\n  - name: perso\n    url: https://mcp.example.org/mcp\n",
                                               encoding="utf-8")
    cfg = with_user_extensions(NokkerConfig(mcp_tool_servers=[{"name": "conf", "url": "http://svc/mcp"}]), tmp_path)
    by_name = {s["name"]: s for s in cfg.mcp_tool_servers}
    assert "origin" not in by_name["conf"]
    assert by_name["perso"]["origin"] == "user"


# ─────────────────────────────── client 3 : compétences par URL (skill_install) ───────────────────────────────


@pytest.fixture
def skills_src(tmp_path, monkeypatch):
    from nokker.tools import skills as sk

    monkeypatch.setattr(sk, "_guard_module", lambda: SimpleNamespace())
    events = []
    return sk.SkillToolSource(home=tmp_path / "h", audit=events.append), events


@pytest.mark.parametrize("host", ["127.0.0.1", "169.254.169.254", "10.0.0.1", "192.168.1.1", "100.64.0.1", "[::1]"])
def test_skill_install_url_strict_refused_with_existing_message(skills_src, host):
    configure(STRICT)
    s, events = skills_src
    source = f"https://{host}/skills/x/SKILL.md"
    r = s.call("skill_install", {"source": source}, CTX)
    assert r.is_error and r.content == f"compétence introuvable à cette source : {source}"
    assert events[-1]["event"] == "install_refused" and "adresse refusée" in events[-1]["reason"]


def test_skill_install_url_strict_port_and_public_passes_guard(skills_src):
    configure(STRICT)
    s, events = skills_src
    r = s.call("skill_install", {"source": "https://8.8.8.8:6379/SKILL.md"}, CTX)
    assert r.content.startswith("compétence introuvable") and "port 6379 refusé" in events[-1]["reason"]
    r = s.call("skill_install", {"source": "https://8.8.8.8/SKILL.md"}, CTX)
    assert not r.content.startswith("compétence introuvable")  # la garde laisse passer : la suite (amont) décide


def test_skill_install_url_off_does_not_consult_guard(skills_src, monkeypatch):
    s, _events = skills_src
    monkeypatch.setattr(ssrf, "resolve_and_check", lambda *a, **k: (_ for _ in ()).throw(AssertionError("appelée")))
    r = s.call("skill_install", {"source": "https://127.0.0.1/SKILL.md"}, CTX)
    assert not r.content.startswith("compétence introuvable")  # comportement historique : l'amont décide
