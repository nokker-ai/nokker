"""Serveurs MCP stdio (`nokker/tools/mcp_stdio.py`, docs/NOKKAN-MCP-STDIO.md).

Contrat : le protocole MCP RÉEL. Le serveur de test (`stdio_fixture/server.py`) est écrit avec le SDK officiel et
lancé pour de vrai (un processus, un tube stdin/stdout, la poignée de main `initialize`, `tools/list`, `tools/call`).
Seul le BAC À SABLE est simulé ici : `FakeSandbox.script_command` rend l'argv d'un lanceur local qui a le contrat de
`docker exec -i -e NOM` (le processus ne reçoit que les variables nommées). Le chemin docker réel est dans
tests/integration/test_mcp_stdio_docker.py."""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from nokker.core.ports import ToolContext
from nokker.core.turn import Effect
from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope
from nokker.tools import mcp_stdio
from nokker.tools.mcp_stdio import (
    ERR_BACKEND, ERR_DEAD, ERR_NO_SESSION, ERR_PENDING, Approvals, McpStdioError, SessionEnv, StdioMcpToolSource,
    check_stdio_entry, configured_servers, fingerprint, load_stdio_servers, raw_of, stdio_enabled, stdio_refusal,
    stdio_sources, write_stdio_servers,
)
from nokker.qa.secrets import KnownSecrets

FIXTURE = Path(__file__).parent / "stdio_fixture"
IDENTITY = "alice.identite-7f3a@exemple.test"
PROJECT = "alpha-projet"  # un nom de projet ordinaire, que l'hôte passe AUSSI comme variable d'identité
HANDLE = "[secret:identite-session-"  # poignée du caviardage existant (`nokker/qa/secrets.py`)
TOKEN = "jeton-" + "coffre-" + "93b1c0ffee"


class FakeSandbox:
    """La source de code d'un fil, vue par `StdioMcpToolSource` : `backend`, `script_command`, `run_hook`."""

    backend = "docker"

    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.hooks: list[str] = []

    def script_command(self, thread_id, argv, env_names=None):
        argv = [sys.executable if a == "python3" else a for a in argv]
        cmd = [sys.executable, str(FIXTURE / "fake_exec.py"), *[x for n in env_names or [] for x in ("-e", n)], *argv]
        self.commands.append(cmd)
        return cmd

    def run_hook(self, thread_id, command, stdin, timeout):
        self.hooks.append(command)
        r = subprocess.run(["bash", "-c", command], capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout


def ctx(thread: str = "th_a") -> ToolContext:
    return ToolContext(thread_id=thread, turn_index=1, channel="api")


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # un zombie n'est plus un processus qui tourne
        return Path(f"/proc/{pid}/stat").read_text().split(") ")[1][0] != "Z"
    except OSError:
        return False


def wait_dead(pid: int, seconds: float = 5.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if not alive(pid):
            return True
        time.sleep(0.05)
    return False


class Rig:
    def __init__(self, tmp_path: Path, **kw) -> None:
        self.proof = tmp_path / "proof"
        self.proof.mkdir()
        self.sandboxes: dict[str, FakeSandbox] = {"th_a": FakeSandbox(), "th_b": FakeSandbox()}
        self.session_env = SessionEnv()
        self.session_env.set("th_a", {"TEST_SESSION_USER": IDENTITY, "TEST_SESSION_PROJECT": PROJECT})
        self.session_env.set("th_b", {"TEST_SESSION_USER": "bob.autre-5521@exemple.test",
                                      "TEST_SESSION_PROJECT": "beta-projet"})
        self.known = KnownSecrets()  # tient la place de `kernel.invariants.known` (le caviardage du noyau)
        self.changes = 0
        self.catalog = tmp_path / "state" / "mcp-stdio" / "srv.json"
        self.source = StdioMcpToolSource(
            name="srv", command="python3", args=[str(FIXTURE / "server.py"), str(self.proof)],
            env={"TEST_SESSION_USER": "session", "TEST_SESSION_PROJECT": "session",
                 "TEST_API_TOKEN": "secret:API_TOKEN", "TEST_THREAD": "thread"},
            resolve=lambda t: self.sandboxes.get(t), session_env=self.session_env, known=lambda: self.known,
            secret=lambda name: {"API_TOKEN": TOKEN}[name], catalog_path=self.catalog,
            on_catalog=self._changed, **{"timeout_s": 20.0, "start_timeout_s": 30.0, **kw})

    def _changed(self) -> None:
        self.changes += 1

    def server_env(self) -> list[dict[str, str]]:
        return [json.loads(p.read_text()) for p in sorted(self.proof.glob("env-*.json"))]

    def pid(self, thread: str = "th_a") -> int:
        r = self.source.call("pid", {}, ctx(thread))
        assert not r.is_error, r.content
        return int(next(line for line in r.content.splitlines() if line.strip().isdigit()))


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    yield r
    r.source.close()


# ─────────────────────────────── découverte, appel ───────────────────────────────


def test_discovery_at_first_turn_end_then_catalog_on_disk(rig, tmp_path):
    assert rig.source.specs() == []  # pas de fil, pas de processus : rien à offrir tant que rien n'a été lancé
    assert rig.sandboxes["th_a"].commands == []  # démarrage paresseux : construire la source ne lance rien
    rig.source.turn_done("th_inconnu")  # fil sans session de code : rien
    assert rig.source.specs() == [] and rig.changes == 0
    rig.source.turn_done("th_a")
    specs = {s.name: s for s in rig.source.specs()}
    assert set(specs) == {"echo", "notes_list", "boom", "leak", "whoami", "blocks", "project", "die", "slow", "pid"}
    assert rig.changes == 1  # le registre est prévenu une fois
    echo = specs["echo"]
    assert echo.source == "mcp:srv" and echo.effect is Effect.WRITE and echo.core is False
    assert echo.description == "Renvoie le texte."
    assert echo.parameters["properties"]["text"]["type"] == "string" and echo.parameters["required"] == ["text"]
    # Catalogue gardé : une source neuve (redémarrage du noyau) offre les outils sans rien lancer.
    again = StdioMcpToolSource(name="srv", command="python3", args=[str(FIXTURE / "server.py"), str(rig.proof)],
                               catalog_path=rig.catalog)
    assert [s.name for s in again.specs()] == [s.name for s in rig.source.specs()]
    # Commande changée : le catalogue gardé ne vaut plus.
    other = StdioMcpToolSource(name="srv", command="python3", args=["autre.py"], catalog_path=rig.catalog)
    assert other.specs() == []
    rig.source.turn_done("th_a")  # déjà relevé : pas de second lancement, pas de second signal
    assert len(rig.sandboxes["th_a"].commands) == 1 and rig.changes == 1


def test_call_goes_through_real_protocol_and_is_fenced(rig):
    rig.source.turn_done("th_a")
    r = rig.source.call("echo", {"text": "bonjour le tube"}, ctx())
    assert not r.is_error
    assert r.content.startswith("Résultat de srv/echo :")
    assert "bonjour le tube" in r.content and 'untrusted-data kind="mcp:result"' in r.content
    # Une session MCP tenue ouverte : le même processus sert les appels suivants.
    assert rig.pid() == rig.pid() and len(rig.sandboxes["th_a"].commands) == 1


def test_unknown_tool_is_not_sent_to_the_server(rig):
    rig.source.turn_done("th_a")
    r = rig.source.call("inconnu", {}, ctx())
    assert r.is_error and r.content.startswith("outil inconnu : inconnu")


def test_server_error_comes_back_as_error_result(rig):
    rig.source.turn_done("th_a")
    r = rig.source.call("boom", {}, ctx())
    assert r.is_error and "panne pour" in r.content
    assert 'untrusted-data kind="mcp:result"' in r.content  # texte de tiers : clôturé
    assert rig.pid() > 0  # une erreur d'outil ne tue pas la session


def test_invalid_arguments_rejected_by_the_real_server(rig):
    rig.source.turn_done("th_a")
    r = rig.source.call("echo", {}, ctx())  # `text` requis : le serveur du SDK refuse
    assert r.is_error and "text" in r.content


# ─────────────────────────────── cycle de vie ───────────────────────────────


def test_process_death_during_call_then_restart_without_replay(rig):
    rig.source.turn_done("th_a")
    first = rig.pid()
    r = rig.source.call("die", {}, ctx())
    assert r.is_error and ERR_DEAD in r.content
    assert wait_dead(first)
    second = rig.pid()  # l'appel suivant relance le processus
    assert second != first and alive(second)
    logs = "".join(p.read_text() for p in rig.proof.glob("calls-*.log"))
    assert logs.count("die") == 1  # l'appel qui a tué le serveur n'a pas été rejoué


def test_process_killed_between_calls_then_restart(rig):
    rig.source.turn_done("th_a")
    first = rig.pid()
    os.kill(first, 9)
    assert wait_dead(first)
    r = rig.source.call("echo", {"text": "x"}, ctx())
    assert r.is_error and ERR_DEAD in r.content  # constaté à l'appel ; jamais rejoué de lui-même
    r = rig.source.call("echo", {"text": "revenu"}, ctx())
    assert not r.is_error and "revenu" in r.content


def test_call_timeout_keeps_the_process(tmp_path):
    rig = Rig(tmp_path, timeout_s=1.0)
    try:
        rig.source.turn_done("th_a")
        pid = rig.pid()
        r = rig.source.call("slow", {"seconds": 3}, ctx())
        assert r.is_error and "délai dépassé (1 s)" in r.content
        time.sleep(2.5)  # le serveur finit son outil, puis répond de nouveau
        assert rig.pid() == pid
    finally:
        rig.source.close()


def test_start_failure_is_a_kernel_error_without_detail(tmp_path, caplog):
    rig = Rig(tmp_path, start_timeout_s=15.0)
    rig.source.args = ["-c", "import sys; sys.exit(7)"]  # le programme sort avant la poignée de main
    rig.source._catalog = [{"name": "echo", "description": "", "input_schema": {}}]
    try:
        with caplog.at_level(logging.DEBUG):
            r = rig.source.call("echo", {"text": "x"}, ctx())
        assert r.is_error and mcp_stdio.ERR_START in r.content
        assert IDENTITY not in r.content and IDENTITY not in caplog.text and TOKEN not in caplog.text
    finally:
        rig.source.close()


def test_one_process_per_thread_each_with_its_identity(rig):
    rig.source.turn_done("th_a")
    a, b = rig.pid("th_a"), rig.pid("th_b")
    assert a != b and sorted(rig.source.running()) == ["th_a", "th_b"]
    users = {e["TEST_THREAD"]: e["TEST_SESSION_USER"] for e in rig.server_env()}
    assert users == {"th_a": IDENTITY, "th_b": "bob.autre-5521@exemple.test"}
    # Un sous-agent du fil a partage son processus (même bac à sable, même identité).
    assert rig.pid("th_a/sa-12") == a


def test_idle_processes_are_stopped_and_close_is_clean(tmp_path):
    now = [1000.0]
    rig = Rig(tmp_path, idle_ttl_s=60.0, clock=lambda: now[0])
    rig.source.turn_done("th_a")
    a, b = rig.pid("th_a"), rig.pid("th_b")
    now[0] += 30
    rig.pid("th_b")  # th_b sert encore
    now[0] += 45  # th_a : 75 s sans appel ; th_b : 45 s
    assert rig.source.reap_idle() == 1
    assert wait_dead(a) and alive(b) and rig.source.running() == ["th_b"]
    assert "NOKKER_MCP=" in rig.sandboxes["th_a"].hooks[-1]  # ménage par la marque, dans le bac à sable
    assert rig.pid("th_a") != a  # relancé à la demande
    rig.source.close_thread("th_b")
    assert wait_dead(b)
    last = rig.pid("th_a")
    rig.source.close()
    assert wait_dead(last) and rig.source.running() == []


def test_process_cap(tmp_path):
    rig = Rig(tmp_path, max_processes=1)
    try:
        rig.source.turn_done("th_a")
        r = rig.source.call("echo", {"text": "x"}, ctx("th_b"))
        assert r.is_error and mcp_stdio.ERR_LIMIT in r.content and rig.source.running() == ["th_a"]
    finally:
        rig.source.close()


# ─────────────────────────────── identité : côté serveur oui, côté modèle non ───────────────────────────────


def test_identity_reaches_the_server_and_nothing_the_model_sees(rig, caplog, monkeypatch):
    monkeypatch.setenv("KERNEL_ONLY_SECRET", "ne-sort-pas-du-noyau-41")
    with caplog.at_level(logging.DEBUG):
        rig.source.turn_done("th_a")
        seen = [rig.source.call("echo", {"text": "salut"}, ctx()), rig.source.call("boom", {}, ctx()),
                rig.source.call("leak", {}, ctx()), rig.source.call("die", {}, ctx()),
                rig.source.call("echo", {"text": "encore"}, ctx())]
    # Côté serveur : les variables déclarées y sont, avec leurs valeurs ; rien d'autre de l'environnement du noyau.
    envs = rig.server_env()
    assert len(envs) == 2  # le processus d'origine et celui relancé après `die` : la même identité
    for env in envs:
        assert env["TEST_SESSION_USER"] == IDENTITY and env["TEST_API_TOKEN"] == TOKEN and env["TEST_THREAD"] == "th_a"
        assert "KERNEL_ONLY_SECRET" not in env
    # Côté modèle : schémas et descriptions, résultats, erreurs.
    specs_text = json.dumps([[s.name, s.description, s.parameters] for s in rig.source.specs()], ensure_ascii=False)
    model_text = specs_text + "".join(r.content for r in seen)
    assert IDENTITY not in model_text and TOKEN not in model_text
    notes = next(s for s in rig.source.specs() if s.name == "notes_list")
    assert notes.description.startswith(f"Notes de {HANDLE}")  # le serveur se décrivait avec son identité
    assert seen[1].is_error and f"panne pour {HANDLE}" in seen[1].content and "(jeton [secret:API_TOKEN])" in seen[1].content
    assert "jeton=[secret:API_TOKEN]" in seen[2].content  # la poignée d'un secret du coffre porte son NOM
    # Journaux du noyau, catalogue sur disque, ligne de commande du processus lancé.
    assert IDENTITY not in caplog.text and TOKEN not in caplog.text
    assert IDENTITY not in rig.catalog.read_text() and TOKEN not in rig.catalog.read_text()
    for cmd in rig.sandboxes["th_a"].commands:
        joined = " ".join(cmd)
        assert IDENTITY not in joined and TOKEN not in joined
        assert "-e TEST_SESSION_USER" in joined and "-e TEST_API_TOKEN" in joined  # par NOM seulement
    assert IDENTITY not in repr(rig.session_env) and IDENTITY not in repr(rig.source)


def test_docker_client_settings_reach_the_client_and_not_the_server(rig, monkeypatch):
    """Trouvé au vrai compose (docs/NOKKAN-MCP-STDIO.md §7) : le noyau de Lokkal joint docker par `DOCKER_HOST`
    (tcp://dockerproxy:2375) ; le client stdio du SDK ne transmet que HOME, PATH… à son processus. Sans les `DOCKER_*`
    du noyau, `docker exec` vise le socket local, qui n'existe pas, et sort aussitôt."""
    monkeypatch.setenv("DOCKER_HOST", "tcp://dockerproxy:2375")
    monkeypatch.setenv("DOCKER_CONFIG", "/tmp/docker-config")
    rig.source.turn_done("th_a")
    assert rig.pid() > 0
    (client,) = [json.loads(p.read_text()) for p in rig.proof.glob("client-*.json")]
    assert client == {"DOCKER_HOST": "tcp://dockerproxy:2375", "DOCKER_CONFIG": "/tmp/docker-config"}
    (server,) = rig.server_env()
    assert not any(k.startswith("DOCKER_") for k in server)  # réglage du client, jamais une variable du serveur
    assert not any("-e DOCKER" in " ".join(c) for c in rig.sandboxes["th_a"].commands)
    with pytest.raises(McpStdioError):  # et une déclaration ne peut pas les détourner
        check_stdio_entry({"name": "m", "command": "x", "env": {"DOCKER_HOST": "session"}})


def test_missing_session_variable_refuses_to_start(rig):
    rig.source.turn_done("th_a")
    rig.sandboxes["th_c"] = FakeSandbox()  # session de code, mais l'hôte n'a posé aucune identité
    r = rig.source.call("echo", {"text": "x"}, ctx("th_c"))
    assert r.is_error and "variable de session absente : TEST_SESSION_PROJECT, TEST_SESSION_USER" in r.content
    assert rig.sandboxes["th_c"].commands == []  # rien n'a été lancé sans identité


def test_missing_vault_secret_refuses_to_start(rig):
    rig.source.turn_done("th_a")
    rig.source.secret = lambda name: (_ for _ in ()).throw(KeyError(name))
    r = rig.source.call("echo", {"text": "x"}, ctx("th_b"))
    assert r.is_error and "TEST_API_TOKEN" in r.content and rig.sandboxes["th_b"].commands == []


def test_session_env_rejects_bad_names_and_values():
    env = SessionEnv()
    assert env.set("t", {"SOKKAN_SESSION_ID": "s1", "SOKKAN_SESSION_USER": ""}) == ["SOKKAN_SESSION_ID",
                                                                                    "SOKKAN_SESSION_USER"]
    for bad in ({"LD_PRELOAD": "x"}, {"PATH": "/x"}, {"NOKKER_MCP": "x"}, {"1X": "x"}, {"A": "a\nb"}, {"A": 3}):
        with pytest.raises(McpStdioError):
            env.set("t", bad)
    env.drop("t")
    assert env.get("t") == {}


# ─────────────────────────────── jamais hors du bac à sable, jamais en multi-locataire ───────────────────────────────


def test_no_code_session_no_process(rig):
    rig.source.turn_done("th_a")
    r = rig.source.call("echo", {"text": "x"}, ctx("th_sans_session"))
    assert r.is_error and ERR_NO_SESSION in r.content


def test_local_backend_is_refused(rig):
    rig.source.turn_done("th_a")
    host = FakeSandbox()
    host.backend = "local"  # les commandes tourneraient sur l'hôte du noyau
    rig.sandboxes["th_h"] = host
    rig.session_env.set("th_h", {"TEST_SESSION_USER": "x"})
    r = rig.source.call("echo", {"text": "x"}, ctx("th_h"))
    assert r.is_error and ERR_BACKEND in r.content and host.commands == []


def test_refused_for_any_tenant_but_the_owner(rig):
    with tenant_scope(INTERNAL_TENANT):
        rig.source.turn_done("th_a")
        assert not rig.source.call("echo", {"text": "x"}, ctx()).is_error
    with tenant_scope("tn_testeur"):
        r = rig.source.call("echo", {"text": "x"}, ctx("th_b"))
        assert r.is_error and r.content.startswith("outil inconnu")
        rig.source.turn_done("th_b")
    assert rig.sandboxes["th_b"].commands == [] and rig.source.running() == ["th_a"]


class _Cfg:
    def __init__(self, **kw):
        self.local, self.code, self.mcp_stdio = kw.get("local"), kw.get("code"), kw.get("mcp_stdio", {})


def test_refusal_outside_lokkal():
    on = {"enabled": True}
    assert stdio_refusal(_Cfg(local={"token_path": "x"}, mcp_stdio=on)) is None
    assert "multi-locataire" in stdio_refusal(_Cfg(local=None, mcp_stdio=on))
    assert "multi-utilisateur" in stdio_refusal(_Cfg(local={}, code={"enabled": True}, mcp_stdio=on))
    assert stdio_enabled(_Cfg(mcp_stdio=on)) and not stdio_enabled(_Cfg()) and not stdio_enabled(_Cfg(mcp_stdio={}))
    assert not stdio_enabled(_Cfg(mcp_stdio={"enabled": "yes"}))  # seulement `true`


# ─────────────────────────────── déclaration ───────────────────────────────


def test_entry_validation():
    e = check_stdio_entry({"name": "mem", "command": "python3", "args": ["-m", "srv"],
                           "env": {"SOKKAN_SESSION_ID": "session", "API": "secret:MEM_API", "T": "thread"}})
    assert e == {"name": "mem", "command": "python3", "args": ["-m", "srv"], "approved": False,
                 "env": {"SOKKAN_SESSION_ID": "session", "API": "secret:MEM_API", "T": "thread"}}
    assert check_stdio_entry({"name": "m", "command": "x", "env": ["A", "B"]})["env"] == {"A": "session", "B": "session"}
    assert mcp_stdio.secret_names(e) == ["MEM_API"]
    bad = [
        {"name": "Mem", "command": "x"}, {"name": "m"}, {"name": "m", "command": "sh -c x"},
        {"name": "m", "command": "x", "env": {"API": "sk-en-clair"}},  # une valeur, jamais
        {"name": "m", "command": "x", "env": {"LD_PRELOAD": "session"}},
        {"name": "m", "command": "x", "env": {"NOKKER_MCP": "session"}},
        {"name": "m", "command": "x", "args": ["--token=sk-proj-abcdefghijklmnop"]},  # une clé en argument
        {"name": "m", "command": "x", "args": ["a\nb"]},
        {"name": "m", "command": "x", "args": ["a"] * 40},
    ]
    for raw in bad:
        with pytest.raises(McpStdioError):
            check_stdio_entry(raw)


def test_file_round_trip_and_unapproved_brings_nothing(tmp_path):
    path = tmp_path / "mcp-stdio-servers.yaml"
    write_stdio_servers(path, [
        check_stdio_entry({"name": "ok", "command": "python3", "approved": True, "read_tools": ["echo"]}),
        check_stdio_entry({"name": "attente", "command": "python3"}),
        {"name": "cassé", "command": ""}])
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    servers = load_stdio_servers(path)
    assert [(s["name"], s["approved"]) for s in servers] == [("ok", True), ("attente", False)]
    sources = stdio_sources(servers, state_dir=tmp_path, resolve=lambda t: None, session_env=SessionEnv(),
                            secret=None)
    assert len(sources) == 1 and raw_of(sources[0]).name == "ok"  # « à approuver » : ni source, ni processus
    assert raw_of(object()) is None
    assert load_stdio_servers(tmp_path / "absent.yaml") == []


def test_read_tools_and_effects_table_apply_like_http_servers(rig, tmp_path):
    rig.source.turn_done("th_a")
    servers = [{**check_stdio_entry({"name": "srv", "command": "python3",
                                     "args": [str(FIXTURE / "server.py"), str(rig.proof)],
                                     "read_tools": ["echo", "notes_list"]}), "approved": True}]
    (src,) = stdio_sources(servers, state_dir=tmp_path / "state", resolve=lambda t: None, session_env=SessionEnv(),
                           secret=None)
    effects = {s.name: s.effect for s in src.specs()}
    assert effects["echo"] is Effect.READ and effects["notes_list"] is Effect.READ
    assert effects["boom"] is Effect.WRITE and effects["die"] is Effect.WRITE  # non déclarés : ils demandent


# ─────────────── identité traitée comme un secret : le caviardage EXISTANT du noyau, résultats normaux compris ───────────────


def test_identity_in_a_normal_result_is_redacted_by_the_kernel_mechanism(rig):
    rig.source.turn_done("th_a")
    r = rig.source.call("whoami", {}, ctx())
    assert not r.is_error and IDENTITY not in r.content and f"session de {HANDLE}" in r.content
    # La valeur est entrée dans les valeurs connues DU NOYAU (pas un second mécanisme) : le même `redact` la retire
    # partout ailleurs — réponse du modèle, résultat d'un AUTRE outil (terminal qui lirait /proc/<pid>/environ), fil.
    from nokker.core.turn import ToolResult
    from nokker.qa.invariants import DeterministicInvariants

    inv = DeterministicInvariants(known=rig.known)
    other = inv.redact_result(ToolResult(call_id="c", name="terminal", content=f"TEST_SESSION_USER={IDENTITY}"), None)
    assert IDENTITY not in other.content and HANDLE in other.content
    assert IDENTITY not in inv.scrub_output(f"Tu es connecté comme {IDENTITY}.")
    # … y compris encodée (contournement couvert par le mécanisme existant, pas par un remplacement de texte maison)
    import base64

    assert base64.b64encode(IDENTITY.encode()).decode() not in inv.scrub_output(
        "en base64 : " + base64.b64encode(IDENTITY.encode()).decode())
    assert not any(IDENTITY in v for v in rig.known.names())  # les NOMS de poignées ne portent pas la valeur


def test_identity_in_an_error_and_in_multi_block_content(rig):
    rig.source.turn_done("th_a")
    boom = rig.source.call("boom", {}, ctx())
    assert boom.is_error and IDENTITY not in boom.content and HANDLE in boom.content
    multi = rig.source.call("blocks", {}, ctx())
    assert not multi.is_error and "premier bloc, sans rien" in multi.content  # le bloc sans valeur est intact
    assert IDENTITY not in multi.content and f"second bloc, pour {HANDLE}" in multi.content


def test_legitimate_project_name_equal_to_an_identity_value_is_redacted_too(rig):
    """Le compromis, montré : un nom de projet que l'hôte passe comme variable d'identité devient une poignée partout,
    même là où il est légitime (docs/NOKKAN-MCP-STDIO.md §2.3). Le modèle sait qu'il y a un projet, pas son nom."""
    rig.source.turn_done("th_a")
    r = rig.source.call("project", {}, ctx())
    assert not r.is_error and PROJECT not in r.content
    assert "3 notes dans le projet [secret:identite-session-" in r.content
    # et, côté noyau, dans tout autre texte : un fichier qui cite ce nom est caviardé de la même façon
    from nokker.qa.secrets import redact

    assert PROJECT not in redact(f"README : bienvenue dans {PROJECT}", rig.known, weak=False).text
    # Une valeur de moins de 4 caractères n'est PAS enregistrée (elle abîmerait tout texte) : limite documentée.
    rig.session_env.set("th_b", {"TEST_SESSION_USER": "bob.autre-5521@exemple.test", "TEST_SESSION_PROJECT": "ab"})
    r = rig.source.call("project", {}, ctx("th_b"))
    assert "3 notes dans le projet ab" in r.content


def test_works_without_a_kernel_known_set(tmp_path):
    """Hors noyau (appel direct, banc) : la source caviarde quand même ce qu'elle rend, avec la même fonction."""
    rig = Rig(tmp_path)
    rig.source.known = None
    try:
        rig.source.turn_done("th_a")
        r = rig.source.call("whoami", {}, ctx())
        assert IDENTITY not in r.content and HANDLE in r.content
    finally:
        rig.source.close()


# ─────────────── serveur de config.yaml : carte au premier lancement, décision mémorisée par empreinte ───────────────


CONF_ENTRY = {"name": "mem", "command": "python3", "args": ["-m", "srv"], "env": {"SOKKAN_SESSION_ID": "session"}}


def test_configured_servers_need_a_card_unless_explicitly_trusted():
    servers = [dict(CONF_ENTRY), {"name": "X", "command": "y"}]
    cfg = _Cfg(local={}, mcp_stdio={"enabled": True, "servers": servers})
    (mem,) = configured_servers(cfg)  # défaut : en attente de carte
    assert (mem["name"], mem["approved"], mem["origin"]) == ("mem", False, "config")
    for value in (False, "true", 1):  # seulement `true`
        cfg = _Cfg(local={}, mcp_stdio={"enabled": True, "servers": servers, "trust_config": value})
        assert configured_servers(cfg)[0]["approved"] is False
    cfg = _Cfg(local={}, mcp_stdio={"enabled": True, "servers": servers, "trust_config": True})
    assert configured_servers(cfg)[0]["approved"] is True  # ancien comportement, explicite


def test_fingerprint_covers_command_args_and_variables_only():
    base = check_stdio_entry(CONF_ENTRY)
    same = check_stdio_entry({**CONF_ENTRY, "purpose": "autre libellé", "read_tools": ["x_get"]})
    assert fingerprint(base) == fingerprint(same)
    for change in ({"command": "python3.12"}, {"args": ["-m", "autre"]}, {"args": []},
                   {"env": {"SOKKAN_SESSION_ID": "session", "SOKKAN_SESSION_USER": "session"}},
                   {"env": {"SOKKAN_SESSION_ID": "secret:X"}}, {"env": {}}):
        assert fingerprint(check_stdio_entry({**CONF_ENTRY, **change})) != fingerprint(base), change


def test_approvals_are_remembered_per_server_and_fingerprint(tmp_path):
    path = tmp_path / "etat" / "mcp-stdio-approvals.json"
    entry = check_stdio_entry(CONF_ENTRY)
    a = Approvals(path)
    assert not a.granted(entry)
    a.grant(entry)
    assert a.granted(entry) and oct(path.stat().st_mode & 0o777) == "0o600"
    assert Approvals(path).granted(entry)  # mémorisée : un noyau redémarré ne redemande pas
    assert not Approvals(path).granted(check_stdio_entry({**CONF_ENTRY, "args": ["-m", "autre"]}))  # la carte revient
    assert not Approvals(path).granted(check_stdio_entry({**CONF_ENTRY, "name": "autre"}))  # par serveur
    path.write_text("pas du json")
    assert not Approvals(path).granted(entry)  # fichier illisible : rien n'est approuvé


def _pending_rig(tmp_path):
    rig = Rig(tmp_path)
    decl = {"name": "srv", "command": rig.source.command, "args": list(rig.source.args), "env": dict(rig.source.env)}
    rig.approvals = Approvals(tmp_path / "state" / "mcp-stdio-approvals.json")
    rig.asked = []

    def ask(thread_id, declaration):
        rig.asked.append((thread_id, declaration))
        return True  # carte posée

    rig.source.gate = lambda: rig.approvals.granted(decl)
    rig.source.ask = ask
    rig.decl = decl
    return rig


def test_config_server_asks_one_card_at_first_launch_and_runs_nothing_before(tmp_path):
    rig = _pending_rig(tmp_path)
    try:
        rig.source.turn_done("th_sans_session")  # pas de bac à sable : pas de premier lancement, pas de carte
        assert rig.asked == []
        rig.source.turn_done("th_a")
        assert rig.asked == [("th_a", rig.decl)]  # la carte porte la déclaration EXACTE (commande, arguments, variables)
        assert rig.sandboxes["th_a"].commands == [] and rig.source.specs() == []  # rien lancé, rien offert
        rig.source.turn_done("th_a")
        rig.source.turn_done("th_b")
        assert len(rig.asked) == 1  # une carte, pas une par tour ni par fil
        rig.source._catalog = [{"name": "echo", "description": "", "input_schema": {}}]  # même avec un vieux catalogue
        assert rig.source.specs() == []
        r = rig.source.call("echo", {"text": "x"}, ctx())
        assert r.is_error and r.content.startswith("outil inconnu") and rig.sandboxes["th_a"].commands == []
        with pytest.raises(Exception) as refused:
            rig.source._conn("th_a")  # et le lancement lui-même refuse (verrou du bas)
        assert str(refused.value) == ERR_PENDING
    finally:
        rig.source.close()


def test_config_server_runs_after_approval_and_asks_again_when_the_declaration_changes(tmp_path):
    rig = _pending_rig(tmp_path)
    try:
        rig.source.turn_done("th_a")
        rig.approvals.grant(rig.decl)  # la personne signe la carte (`mcp_server_add`, voir test_mcp_stdio_wiring)
        rig.source.approved("th_a")  # suite de la carte : premier lancement dans le fil de la carte
        assert {"echo", "pid"} <= {s.name for s in rig.source.specs()} and rig.changes >= 1
        assert not rig.source.call("echo", {"text": "x"}, ctx()).is_error
        assert len(rig.asked) == 1
        # Redémarrage du noyau (source neuve, même fichier d'accords), même déclaration : pas de nouvelle carte.
        again = StdioMcpToolSource(name="srv", command=rig.source.command, args=list(rig.source.args),
                                   env=dict(rig.source.env), resolve=rig.source.resolve, catalog_path=rig.catalog,
                                   gate=lambda: Approvals(rig.approvals.path).granted(rig.decl),
                                   ask=lambda t, d: rig.asked.append(("redemandé", d)) or True)
        again.turn_done("th_a")
        assert len(rig.asked) == 1 and {"echo", "pid"} <= {s.name for s in again.specs()}
        again.close()
        # La déclaration change (un argument de plus) : l'approbation ne vaut plus, la carte revient.
        rig.source.close_thread("th_a")
        rig.source.args = [*rig.source.args, "--verbose"]
        rig.decl["args"] = list(rig.source.args)
        assert rig.source.specs() == []
        rig.source.turn_done("th_a")
        assert len(rig.asked) == 2 and rig.asked[1][1]["args"][-1] == "--verbose"
        assert rig.source.call("echo", {"text": "x"}, ctx()).content.startswith("outil inconnu")
    finally:
        rig.source.close()


def test_card_not_posed_is_asked_again_later(tmp_path):
    rig = _pending_rig(tmp_path)
    posed = [False, True]
    rig.source.ask = lambda thread_id, declaration: posed.pop(0)  # refus du noyau (mode plan…), puis carte posée
    try:
        rig.source.turn_done("th_a")
        rig.source.turn_done("th_a")
        rig.source.turn_done("th_a")
        assert posed == []  # deux demandes : la première n'avait pas abouti ; plus aucune après la carte posée
    finally:
        rig.source.close()


def test_sources_for_config_servers_are_gated_and_trusted_ones_are_not(tmp_path):
    entry = {**check_stdio_entry(CONF_ENTRY), "origin": "config"}
    approvals = Approvals(tmp_path / "a.json")
    kw = {"state_dir": tmp_path, "resolve": lambda t: None, "session_env": SessionEnv(), "secret": None}
    (pending,) = stdio_sources([entry], approvals=approvals, ask=lambda t, d: True, **kw)
    assert raw_of(pending).gate() is False
    approvals.grant(entry)
    assert raw_of(pending).gate() is True
    (trusted,) = stdio_sources([{**entry, "approved": True}], approvals=approvals, **kw)
    assert raw_of(trusted).gate is None
    assert stdio_sources([{**entry, "origin": None}], approvals=approvals, **kw) == []  # import en attente : rien


def test_stdio_catalog_keeps_the_whole_description_cut_only_at_the_per_server_cap(tmp_path, caplog):
    from types import SimpleNamespace

    def tools(n):
        return [SimpleNamespace(name="t", description="Début.\nRègles : " + "r" * n, input_schema=None)]

    src = StdioMcpToolSource(name="srv", command="python3", args=["x"], catalog_path=tmp_path / "cat.json")
    src._set_catalog(tools(1000))
    assert src._catalog[0]["description"] == "Début.\nRègles : " + "r" * 1000
    with caplog.at_level("WARNING", logger="nokker.tools.schema"):
        src._set_catalog(tools(5000))
    assert len(src._catalog[0]["description"]) == 4000 and "srv" in caplog.records[-1].getMessage()
    small = StdioMcpToolSource(name="other", command="python3", args=["x"], description_max_chars=50)
    small._set_catalog(tools(1000))
    assert len(small._catalog[0]["description"]) == 50
