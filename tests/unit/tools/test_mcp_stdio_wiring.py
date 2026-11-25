"""Serveurs MCP stdio : drapeau, déclaration sous carte, câblage du noyau, import (docs/NOKKAN-MCP-STDIO.md).

Première moitié : drapeau ÉTEINT = comportement inchangé. Les valeurs attendues (empreinte des schémas, textes) ont été
relevées sur le commit de base aae68868, pas recopiées du code de cette branche."""
from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from nokker.config import NokkerConfig
from nokker.core.ports import ToolContext
from nokker.core.turn import Decision, Effect, ToolCall
from nokker.tools.mcp_config import STDIO_RULE, McpConfigToolSource

CTX = ToolContext(thread_id="th_1", turn_index=0, channel="api")
# sha256 des specs de `McpConfigToolSource` (nom, description, schéma, effet, cœur, source), relevé sur aae68868, puis
# recalculé quand « le noyau » a été retiré de deux descriptions (seul ce texte change : « redémarrage de Nokker »).
BASE_SPECS_SHA = "ec88dc49dde0b2143de22c61a536f536712f28b8bced7a90740f4b4d7c4ec80d"
BASE_LIST = ('{"servers": [], "configured": [], "note": "les serveurs ajoutés sont actifs après un redémarrage de '
             'Nokker ; leurs outils sont des écritures (ils demandent), sauf ceux de read_tools dont la description '
             "n'annonce pas d'écriture\"}")
BASE_REFUSAL = "refusé : adresse refusée : https://…, ou http:// vers la boucle locale ou un service sans point"
STDIO_ARGS = {"name": "mem", "command": "python3", "args": ["-m", "srv"], "env": {"SOKKAN_SESSION_ID": "session"}}


def _sha(src: McpConfigToolSource) -> str:
    dump = json.dumps([[s.name, s.description, s.parameters, s.effect.value, s.core, s.source] for s in src.specs()],
                      ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(dump.encode()).hexdigest()


def _call(name: str, args: dict) -> ToolCall:
    return ToolCall(id="c1", name=name, arguments=args)


def _local(tmp_path: Path):
    from tests.unit.test_local_api import _api

    api = _api(tmp_path)
    api.state_dir = tmp_path / "etat"
    api.state_dir.mkdir(exist_ok=True)
    return api


def _lokkal(**mcp_stdio) -> NokkerConfig:
    return NokkerConfig(local={"token_path": "x"}, mcp_stdio=mcp_stdio)


def _off(setup) -> bool:
    """Rien n'a été monté : ni source, ni fichier de déclarations, ni accords, ni serveur de config."""
    return setup.sources == [] and setup.path is None and setup.approvals is None and setup.config_servers == []


# ─────────────────────────────── drapeau éteint = inchangé ───────────────────────────────


def test_flag_off_by_default_everywhere():
    assert NokkerConfig().mcp_stdio == {}
    from nokker.api.server import mcp_stdio_setup

    for cfg in (NokkerConfig(), NokkerConfig(local={"token_path": "x"}), _lokkal(servers=[STDIO_ARGS]),
                _lokkal(enabled=False, servers=[STDIO_ARGS]), _lokkal(enabled="true", servers=[STDIO_ARGS])):
        assert _off(mcp_stdio_setup(cfg, object()))


def test_flag_off_tool_surface_is_byte_identical_to_base(tmp_path):
    src = McpConfigToolSource(path=tmp_path / "mcp-servers.yaml")
    assert _sha(src) == BASE_SPECS_SHA
    assert src.call("mcp_server_list", {}, CTX).content == BASE_LIST
    r = src.call("mcp_server_add", STDIO_ARGS, CTX)  # `command` : paramètre inconnu, refusé comme avant
    assert r.is_error and r.content == BASE_REFUSAL
    assert src.floor(_call("mcp_server_add", STDIO_ARGS), None, None) is None
    assert not (tmp_path / "mcp-stdio-servers.yaml").exists()


def test_flag_off_kernel_never_imports_the_module(tmp_path):
    """Processus neuf : charger le serveur du noyau, la config par défaut, la source des outils `mcp_server_*`, la
    route d'ouverture de session et l'import Claude Code ne charge jamais `nokker.tools.mcp_stdio`."""
    code = f"""
import sys, json
from pathlib import Path
from nokker.api.server import mcp_stdio_setup
from nokker.config import NokkerConfig
from nokker.importers import claude_code
from nokker.tools.mcp_config import McpConfigToolSource
from nokker.core.ports import ToolContext
from tests.unit.test_local_api import _api
tmp = Path({str(tmp_path)!r})
api = _api(tmp)
assert api.mcp_env is None
ctx = ToolContext(thread_id="t", turn_index=0, channel="api")
src = McpConfigToolSource(path=tmp / "mcp-servers.yaml")
src.specs(); src.call("mcp_server_list", {{}}, ctx); src.call("mcp_server_add", {{"name": "m", "command": "x"}}, ctx)
off = mcp_stdio_setup(NokkerConfig(local={{"token_path": "x"}}), api)
assert off.sources == [] and off.path is None and off.approvals is None and off.config_servers == []
(tmp / ".claude.json").write_text(json.dumps({{"mcpServers": {{"m": {{"command": "npx", "args": ["x"]}}}}}}))
report = claude_code.scan(tmp)
st, body = api.handle("POST", "/v1/local/sessions", f"Bearer {{api.token}}",
                      {{"cwd": str(tmp / "projets" / "app"), "mcp_env": {{"A": "valeur"}}}})
assert st == 201 and sorted(body) == ["cwd", "thread_id", "workspace"], body
print(json.dumps({{"loaded": "nokker.tools.mcp_stdio" in sys.modules,
                  "mcp": [[i.kind, i.status, i.reason] for i in report.items if i.kind.startswith("mcp")]}}))
"""
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120,
                         cwd=Path(__file__).parents[3])
    assert out.returncode == 0, out.stderr[-2000:]
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert got["loaded"] is False
    # import : un serveur stdio reste « sans équivalent », au mot près (texte du commit de base)
    assert got["mcp"] == [["mcp", "unsupported",
                           "serveur stdio (commande lancée sur l'hôte) : pas d'équivalent dans Nokker"]]


def test_flag_off_docker_exec_argv_unchanged(tmp_path):
    from nokker.tools.code import CodeToolSource, Workspace

    class Env:
        cwd, _container_id, _docker_exe = "/workspace", "c0ffee", "docker"

    src = CodeToolSource(root=str(tmp_path), backend="docker")
    src._spaces["th_1"] = Workspace(env=Env(), ops=None, todos=None, cwd="/workspace")
    base = ["docker", "exec", "-i", "-w", "/workspace", "c0ffee", "python3", "srv.py"]
    assert src.script_command("th_1", ["python3", "srv.py"]) == base
    assert src.script_command("th_1", ["python3", "srv.py"], env_names=[]) == base
    # allumé : variables par NOM, jamais `NOM=valeur` dans la ligne de commande
    assert src.script_command("th_1", ["python3", "srv.py"], env_names=["A", "B"]) == [
        "docker", "exec", "-i", "-e", "A", "-e", "B", "-w", "/workspace", "c0ffee", "python3", "srv.py"]
    with pytest.raises(PermissionError):
        CodeToolSource(root=str(tmp_path), backend="local").script_command("th_1", ["x"], env_names=["A"])


def test_kubernetes_cannot_pass_env_by_name(tmp_path):
    from nokker.tools.code import CodeToolSource, Workspace

    class Env:
        cwd, pod = "/workspace", "p"

        class kubectl:
            @staticmethod
            def exec_argv(pod, argv, stdin):
                return ["kubectl", "exec", pod, "--", *argv]

    src = CodeToolSource(root=str(tmp_path), backend="kubernetes", env_factory=lambda **k: None)
    src._spaces["th_1"] = Workspace(env=Env(), ops=None, todos=None, cwd="/workspace")
    assert src.script_command("th_1", ["x"])[:3] == ["kubectl", "exec", "p"]  # comme avant
    with pytest.raises(PermissionError):
        src.script_command("th_1", ["x"], env_names=["A"])


# ─────────────────────────────── refus hors Lokkal ───────────────────────────────


def test_refused_in_multi_tenant_even_with_flag_on(tmp_path, caplog):
    from nokker.api.server import mcp_stdio_setup

    on = {"enabled": True, "servers": [STDIO_ARGS]}
    api = _local(tmp_path)
    with caplog.at_level(logging.ERROR):
        assert _off(mcp_stdio_setup(NokkerConfig(mcp_stdio=on), None))  # cloud : ni bloc local ni API locale
        assert _off(mcp_stdio_setup(NokkerConfig(mcp_stdio=on), api))
        assert _off(mcp_stdio_setup(NokkerConfig(local={"token_path": "x"}, code={"enabled": True}, mcp_stdio=on),
                                    api))
    assert caplog.text.count("mcp_stdio.enabled ignoré") == 3 and "multi-locataire" in caplog.text
    assert api.mcp_env is None  # rien n'a été posé : `mcp_env` reste ignoré à l'ouverture d'une session


# ─────────────────────────────── allumé (Lokkal) : câblage ───────────────────────────────


def test_setup_builds_sources_for_approved_servers_only(tmp_path):
    from nokker.api.server import mcp_stdio_setup
    from nokker.tools.mcp_stdio import SessionEnv, check_stdio_entry, raw_of, write_stdio_servers

    api = _local(tmp_path)
    write_stdio_servers(api.state_dir / "mcp-stdio-servers.yaml", [
        {**check_stdio_entry({"name": "board", "command": "python3"}), "approved": True},
        check_stdio_entry({"name": "importe", "command": "npx"}),  # à approuver
        {**check_stdio_entry({"name": "pris", "command": "python3"}), "approved": True}])
    cfg = _lokkal(enabled=True, servers=[STDIO_ARGS], timeout_s=12, idle_ttl_s=60, max_processes=3)
    setup = mcp_stdio_setup(cfg, api, taken=["pris"])
    assert setup.path == api.state_dir / "mcp-stdio-servers.yaml"
    assert [s["name"] for s in setup.config_servers] == ["mem"]
    raws = [raw_of(s) for s in setup.sources]
    # Défaut : le serveur de config.yaml attend SA carte (porte fermée) ; celui déclaré sous carte n'a pas de porte.
    assert raws[0].gate is not None and raws[0].gate() is False and raws[1].gate is None
    setup.approvals.grant(setup.config_servers[0])
    assert raws[0].gate() is True
    trusted = mcp_stdio_setup(_lokkal(enabled=True, servers=[STDIO_ARGS], trust_config=True), api, taken=["pris"])
    assert raw_of(trusted.sources[0]).gate is None  # réglage explicite : config.yaml vaut approbation
    assert [r.name for r in raws] == ["mem", "board"]  # config.yaml (opérateur) puis déclaré ; ni « importe » ni « pris »
    assert raws[0].timeout_s == 12 and raws[0].idle_ttl_s == 60 and raws[0].max_processes == 3
    assert raws[0].env == {"SOKKAN_SESSION_ID": "session"}
    assert isinstance(api.mcp_env, SessionEnv) and raws[0].session_env is api.mcp_env
    assert all(s.specs() == [] for s in setup.sources)  # rien n'est lancé au démarrage


def test_tools_enter_the_registry_like_http_mcp_tools(tmp_path):
    """Source `mcp:<serveur>`, effet WRITE par défaut (donc carte), relevé par la table des effets."""
    from nokker.api.server import mcp_stdio_setup
    from nokker.tools.mcp_stdio import raw_of
    from nokker.tools.registry import Registry

    api = _local(tmp_path)
    cfg = NokkerConfig(local={"token_path": "x"}, mcp_stdio={"enabled": True, "servers": [STDIO_ARGS]},
                       effect_overrides={"mem_purge": "destructive"})
    cfg.mcp_stdio["trust_config"] = True
    (source,) = mcp_stdio_setup(cfg, api).sources
    raw_of(source)._catalog = [{"name": "mem_search", "description": "Cherche.", "input_schema": {}},
                               {"name": "mem_purge", "description": "Vide.", "input_schema": {}}]
    reg = Registry(sources=[source])
    assert reg.spec("mem_search").source == "mcp:mem" and reg.spec("mem_search").effect is Effect.WRITE
    assert reg.spec("mem_purge").effect is Effect.DESTRUCTIVE
    assert ("mem_search", "Cherche.") in reg.index()  # trouvé par tool_find, hors préfixe


def test_session_identity_is_posted_by_the_host_through_the_real_server(tmp_path):
    from nokker.api.server import make_handler
    from nokker.local.api import LOCAL_OWNER
    from nokker.tools.mcp_stdio import SessionEnv
    from tests.unit.api.test_server import make_kernel
    from tests.unit.test_local_api import _req

    api = _local(tmp_path)
    api.mcp_env = SessionEnv()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(make_kernel("ok"), {api.token: LOCAL_OWNER},
                                                               local_api=api))
    port = httpd.server_address[1]
    api.allowed_hosts = frozenset({f"127.0.0.1:{port}"})
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        cwd = str(tmp_path / "projets" / "app")
        ident = {"SOKKAN_SESSION_ID": "s-42", "SOKKAN_SESSION_USER": "alice@exemple.test"}
        st, body = _req(port, "/v1/local/sessions", token=api.token, body={"cwd": cwd, "mcp_env": ident})
        assert st == 201 and api.mcp_env.get(body["thread_id"]) == ident
        assert "alice@exemple.test" not in json.dumps(body) and sorted(body) == ["cwd", "thread_id", "workspace"]
        st, body = _req(port, "/v1/local/sessions", token=api.token, body={"cwd": cwd, "mcp_env": {"LD_PRELOAD": "x"}})
        assert st == 400 and body["error"]["code"] == "bad_body" and len(api.code.sessions()) == 1
        st, _ = _req(port, "/v1/local/sessions", token=api.token, body={"cwd": cwd})  # sans identité : comme avant
        assert st == 201
    finally:
        httpd.shutdown()
    # par la passerelle : refusé (l'identité se pose sur la machine)
    api.relayed_owner = lambda authorization: {"scope": "lokkal"}
    st, _ = api.handle("POST", "/v1/local/sessions", "Bearer x", {"cwd": cwd, "mcp_env": ident}, relayed=True)
    assert st == 403


# ─────────────────────────────── allumé : déclaration sous carte forte ───────────────────────────────


@pytest.fixture
def conf(tmp_path):
    return McpConfigToolSource(path=tmp_path / "mcp-servers.yaml", stdio_path=tmp_path / "mcp-stdio-servers.yaml",
                               known_secrets=lambda: ["PRESENT"], configured=lambda: ["de-la-config"])


def test_flag_on_only_adds_three_parameters(conf, tmp_path):
    off = {s.name: s for s in McpConfigToolSource(path=tmp_path / "x.yaml").specs()}
    on = {s.name: s for s in conf.specs()}
    assert set(on) == set(off)
    for name in on:  # noms, descriptions, effets : ceux d'avant
        assert (on[name].description, on[name].effect) == (off[name].description, off[name].effect)
        if name != "mcp_server_add":
            assert on[name].parameters == off[name].parameters
    added = set(on["mcp_server_add"].parameters["properties"]) - set(off["mcp_server_add"].parameters["properties"])
    assert added == {"command", "args", "env"} and on["mcp_server_add"].parameters["required"] == ["name"]
    assert on["mcp_server_add"].effect is Effect.DESTRUCTIVE


def test_declaration_always_asks_a_strong_card(conf):
    v = conf.floor(_call("mcp_server_add", STDIO_ARGS), None, None)
    assert v.decision is Decision.HITL and v.strength == "strong" and v.rule == STDIO_RULE
    assert "python3 -m srv" in v.reason and "SOKKAN_SESSION_ID" in v.reason  # la personne voit ce qui sera lancé
    # refusée tout de suite : une valeur en clair, une commande que le noyau n'exécute jamais
    v = conf.floor(_call("mcp_server_add", {**STDIO_ARGS, "env": {"API_KEY": "sk-en-clair-123"}}), None, None)
    assert v.decision is Decision.DENY and "sk-en-clair-123" not in v.reason
    # un serveur HTTP garde son chemin (pas de plancher)
    assert conf.floor(_call("mcp_server_add", {"name": "h", "url": "https://x.example/mcp"}), None, None) is None
    from nokker.approvals.rule_labels import ASKING, LABELS

    assert STDIO_RULE in LABELS and STDIO_RULE in ASKING


def test_add_list_remove(conf, tmp_path):
    from nokker.tools.mcp_stdio import load_stdio_servers

    r = conf.call("mcp_server_add", {**STDIO_ARGS, "env": {"SOKKAN_SESSION_ID": "session", "K": "secret:ABSENT"}}, CTX)
    assert not r.is_error
    got = json.loads(r.content)
    assert got == {"added": "mem", "command": ["python3", "-m", "srv"], "env": ["K", "SOKKAN_SESSION_ID"],
                   "secrets": ["ABSENT"], "active": "au prochain redémarrage de Nokker",
                   "missing_secrets": ["ABSENT"], "next": "pose chaque valeur toi-même : nokker secret set ABSENT"}
    (entry,) = load_stdio_servers(conf.stdio_path)
    assert entry["approved"] is True  # l'appel n'arrive qu'après la carte forte
    listed = json.loads(conf.call("mcp_server_list", {}, CTX).content)
    assert listed["stdio"] == [{"name": "mem", "command": ["python3", "-m", "srv"], "approved": True,
                                "env": {"SOKKAN_SESSION_ID": "session", "K": "secret:ABSENT"},
                                "missing_secrets": ["ABSENT"]}]
    assert listed["servers"] == []  # la liste des serveurs HTTP reste à part
    for clash in (STDIO_ARGS, {"name": "mem", "url": "https://x.example/mcp"},
                  {**STDIO_ARGS, "name": "de-la-config"}):
        r = conf.call("mcp_server_add", clash, CTX)
        assert r.is_error and "s'appelle déjà" in r.content
    r = conf.call("mcp_server_add", {**STDIO_ARGS, "name": "deux", "url": "https://x.example/mcp"}, CTX)
    assert r.is_error and "pas les deux" in r.content
    r = conf.call("mcp_server_add", {"name": "trois", "command": "x", "env": {"A": "valeur"}}, CTX)
    assert r.is_error and r.content.startswith("refusé : env A")
    assert json.loads(conf.call("mcp_server_remove", {"name": "mem"}, CTX).content)["removed"] == "mem"
    assert load_stdio_servers(conf.stdio_path) == []


def test_pending_import_is_approved_only_if_identical(conf):
    from nokker.tools.mcp_stdio import check_stdio_entry, load_stdio_servers, write_stdio_servers

    write_stdio_servers(conf.stdio_path, [check_stdio_entry({**STDIO_ARGS, "purpose": "importé (mem)"})])
    r = conf.call("mcp_server_add", {**STDIO_ARGS, "args": ["-m", "autre"]}, CTX)  # pas ce qui attendait
    assert r.is_error and "s'appelle déjà" in r.content and load_stdio_servers(conf.stdio_path)[0]["approved"] is False
    r = conf.call("mcp_server_add", STDIO_ARGS, CTX)
    assert not r.is_error and json.loads(r.content)["added"] == "mem"
    (entry,) = load_stdio_servers(conf.stdio_path)
    assert entry["approved"] is True and entry["purpose"] == "importé (mem)"


# ─────────────────────────────── import Claude Code ───────────────────────────────


def _claude_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {
        "Sokkan Memory": {"command": "python3", "args": ["/srv/memory.py"],
                          "env": {"MEMORY_API_KEY": "valeur-tres-secrete-0042", "MEMORY_SCOPE": "projet-alpha"}},
        "avec-cle": {"command": "npx", "args": ["srv", "--token=sk-proj-abcdefghijklmnop"]},
        "distant": {"type": "http", "url": "https://mcp.example.com/mcp"}}}), encoding="utf-8")
    return home


def test_import_proposes_stdio_servers_pending_with_env_names_only(tmp_path):
    from nokker.importers import claude_code
    from nokker.importers.apply import apply
    from nokker.tools.mcp_stdio import SessionEnv, load_stdio_servers, stdio_sources

    report = claude_code.scan(_claude_home(tmp_path), nokker_home=tmp_path / "nk", mcp_stdio=True)
    items = {i.detail.get("name") or i.source.rsplit(".", 1)[-1]: i for i in report.items
             if i.kind in ("mcp", "mcp-stdio")}
    mem = items["sokkan-memory"]
    assert mem.kind == "mcp-stdio" and mem.status == "planned" and "carte forte" in mem.reason
    assert mem.detail["entry"] == {
        "name": "sokkan-memory", "command": "python3", "args": ["/srv/memory.py"], "approved": False,
        "purpose": "importé (Sokkan Memory)",
        "env": {"MEMORY_API_KEY": "secret:MCP_SOKKAN_MEMORY_MEMORY_API_KEY",
                "MEMORY_SCOPE": "secret:MCP_SOKKAN_MEMORY_MEMORY_SCOPE"}}
    assert items["avec-cle"].status == "unsupported"  # une clé dans les arguments : jamais reprise
    assert items["distant"].kind == "mcp" and items["distant"].status == "planned"  # HTTP : comme avant
    dumped = json.dumps(report.to_json(), ensure_ascii=False)
    assert "valeur-tres-secrete-0042" not in dumped and "projet-alpha" not in dumped and "sk-proj-" not in dumped

    path = tmp_path / "nk" / "mcp-stdio-servers.yaml"
    apply(report, user_settings=tmp_path / "nk" / "settings.json", backup_dir=tmp_path / "bak",
          mcp_stdio_path=path)
    assert mem.status == "imported"
    (entry,) = load_stdio_servers(path)
    assert entry["approved"] is False and "valeur-tres-secrete-0042" not in path.read_text()
    # « à approuver » : ni outil ni processus tant que la carte forte n'est pas signée
    assert stdio_sources([entry], state_dir=tmp_path, resolve=lambda t: None, session_env=SessionEnv(),
                         secret=None) == []
    again = claude_code.scan(_claude_home(tmp_path / "bis") if (tmp_path / "bis").mkdir() is None else None,
                             nokker_home=tmp_path / "nk", mcp_stdio=True)
    apply(again, user_settings=tmp_path / "nk" / "settings.json", backup_dir=tmp_path / "bak", mcp_stdio_path=path)
    assert next(i for i in again.items if i.kind == "mcp-stdio" and i.detail.get("name")).status == "conflict"


def test_import_without_stdio_path_writes_nothing(tmp_path):
    from nokker.importers import claude_code
    from nokker.importers.apply import apply

    report = claude_code.scan(_claude_home(tmp_path), nokker_home=tmp_path / "nk", mcp_stdio=True)
    apply(report, user_settings=tmp_path / "nk" / "settings.json", backup_dir=tmp_path / "bak")
    mem = next(i for i in report.items if i.kind == "mcp-stdio" and i.detail.get("name"))
    assert mem.status == "skipped" and not (tmp_path / "nk" / "mcp-stdio-servers.yaml").exists()


# ─────────────────────────────── serveur de config.yaml : carte au premier lancement ───────────────────────────────


def test_card_request_goes_through_the_kernel_gates_as_the_owner():
    """`ask` de la source = un appel direct de `mcp_server_add` par les portes du noyau (plancher : carte forte)."""
    from nokker.api.server import stdio_card_request
    from nokker.core.turn import ToolResult
    from nokker.local.api import LOCAL_OWNER

    calls = []

    class K:
        def __init__(self, out):
            self.out = out

        def direct_call(self, thread_id, name, arguments, sender=None):
            calls.append((thread_id, name, arguments, sender))
            return self.out

    assert stdio_card_request(K("ap_42"), "th_1", STDIO_ARGS) is True  # un identifiant de carte : posée
    assert calls == [("th_1", "mcp_server_add", STDIO_ARGS, LOCAL_OWNER)]
    refused = ToolResult(call_id="c", name="mcp_server_add", content="refusé par le noyau", is_error=True)
    assert stdio_card_request(K(refused), "th_1", STDIO_ARGS) is False  # refus (mode plan…) : pas de carte
    assert stdio_card_request(None, "th_1", STDIO_ARGS) is False  # noyau pas encore construit


def test_signing_the_card_of_a_config_server_records_its_fingerprint(tmp_path):
    from nokker.tools.mcp_stdio import Approvals, check_stdio_entry, load_stdio_servers

    conf_entry = {**check_stdio_entry(STDIO_ARGS), "origin": "config"}
    approvals = Approvals(tmp_path / "mcp-stdio-approvals.json")
    done = []
    conf = McpConfigToolSource(path=tmp_path / "mcp-servers.yaml", stdio_path=tmp_path / "mcp-stdio-servers.yaml",
                               configured=lambda: ["mem"], stdio_config=lambda: [conf_entry],
                               stdio_approvals=approvals, on_stdio_approved=lambda name, thread: done.append((name, thread)))
    # la carte est la même carte forte, avec la commande exacte
    v = conf.floor(_call("mcp_server_add", STDIO_ARGS), None, None)
    assert v.decision is Decision.HITL and v.strength == "strong" and "python3 -m srv" in v.reason
    listed = json.loads(conf.call("mcp_server_list", {}, CTX).content)["stdio"]
    assert listed == [{"name": "mem", "command": ["python3", "-m", "srv"], "env": {"SOKKAN_SESSION_ID": "session"},
                       "approved": False, "origin": "config"}]
    # une déclaration qui n'est pas celle de la config : jamais approuvée par ce chemin
    r = conf.call("mcp_server_add", {**STDIO_ARGS, "args": ["-m", "autre"]}, CTX)
    assert r.is_error and "s'appelle déjà" in r.content and not approvals.granted(conf_entry) and done == []
    r = conf.call("mcp_server_add", STDIO_ARGS, CTX)  # exécuté après la signature de la carte
    assert not r.is_error
    assert json.loads(r.content) == {"approved": "mem", "command": ["python3", "-m", "srv"],
                                     "env": ["SOKKAN_SESSION_ID"]}
    assert approvals.granted(conf_entry) and done == [("mem", "th_1")]
    assert load_stdio_servers(conf.stdio_path) == []  # rien n'est recopié dans le fichier des déclarations
    assert json.loads(conf.call("mcp_server_list", {}, CTX).content)["stdio"][0]["approved"] is True
    # la config change : l'accord ne vaut plus pour la nouvelle déclaration
    conf_entry["args"] = ["-m", "srv", "--tout"]
    assert json.loads(conf.call("mcp_server_list", {}, CTX).content)["stdio"][0]["approved"] is False


def test_trusted_config_server_cannot_be_redeclared(tmp_path):
    from nokker.tools.mcp_stdio import check_stdio_entry

    trusted = {**check_stdio_entry(STDIO_ARGS), "origin": "config", "approved": True}
    conf = McpConfigToolSource(path=tmp_path / "mcp-servers.yaml", stdio_path=tmp_path / "mcp-stdio-servers.yaml",
                               configured=lambda: ["mem"], stdio_config=lambda: [trusted])
    r = conf.call("mcp_server_add", STDIO_ARGS, CTX)
    assert r.is_error and "s'appelle déjà" in r.content
