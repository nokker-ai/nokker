"""Commandes en arrière-plan (lot X6, nokker/tools/processes.py, docs/LOKKAL.md §7.7).

Joué sans Hermes : l'environnement est un vrai bash de l'hôte (`ShellEnv`, même contrat que `execute` de Hermes),
donc les vrais scripts de lancement, d'état et d'arrêt tournent, avec de vrais groupes de processus. Chaque test
arrête ce qu'il lance (fixture `cleanup`)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from nokker.core.ports import ToolContext
from nokker.core.turn import Decision, Effect, ToolCall
from nokker.tools import code
from nokker.tools.code import PROCESS_TOOLS, CodeToolSource
from nokker.tools.processes import (
    PreviewSigner, ProcessError, ProcessTable, is_process_id, reap_processes,
)

CTX = ToolContext(thread_id="th_bg", turn_index=0, channel="api")
PY = sys.executable


class ShellEnv:
    """`execute` de Hermes réduit à l'essentiel : un bash, dans le dossier, sortie et code de retour."""

    def __init__(self, cwd: str):
        self.cwd = cwd
        self.calls: list[str] = []

    def execute(self, command, cwd="", timeout=60, **_kw):
        self.calls.append(command)
        r = subprocess.run(["bash", "-c", command], cwd=cwd or self.cwd, capture_output=True, text=True,
                           timeout=timeout, stdin=subprocess.DEVNULL)
        return {"output": r.stdout + r.stderr, "returncode": r.returncode}

    def cleanup(self):
        pass


@pytest.fixture
def tables():
    made: list[ProcessTable] = []
    yield made
    for t in made:
        t.stop_all()


def _table(tmp_path, tables, **kw) -> ProcessTable:
    t = ProcessTable(ShellEnv(str(tmp_path)), str(tmp_path / "logs"), host_logs=kw.pop("host_logs", True),
                     settle_s=0, **kw)
    tables.append(t)
    return t


def _alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    # un chef de groupe zombie (pas encore ramassé) compte comme parti
    try:
        with open(f"/proc/{pgid}/stat") as f:
            return f.read().split(") ")[1][0] != "Z"
    except OSError:
        return True


def _wait(pred, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


# ─────────────────────────────── table des processus ───────────────────────────────


@pytest.mark.parametrize("host_logs", [True, False])
def test_start_logs_exit_code_and_own_process_group(tmp_path, tables, host_logs):
    t = _table(tmp_path, tables, host_logs=host_logs)
    p = t.start(f"{PY} -u -c \"import time; print('prêt'); time.sleep(30)\"", name="srv")
    assert is_process_id(p.id) and p.name == "srv" and p.state == "running"
    assert os.getpgid(p.pgid) == p.pgid != os.getpgid(0)  # son propre groupe, pas celui du noyau
    assert _wait(lambda: "prêt" in t.logs(p.id)["output"])
    with open(f"/proc/{p.pgid}/environ", "rb") as f:
        assert f"NOKKER_BG={p.id}".encode() in f.read().split(b"\0")  # la marque vérifiée avant tout signal
    short = t.start("echo fini; exit 3")
    assert _wait(lambda: t.logs(short.id)["state"] == "exited")
    got = t.logs(short.id)
    assert got["returncode"] == 3 and got["output"] == "fini\n" and got["log_bytes"] == 5 and not got["truncated"]
    assert {x.id: x.state for x in t.list()} == {p.id: "running", short.id: "exited"}
    stopped = t.stop(p.id)
    assert stopped.state == "stopped" and _wait(lambda: not _alive(p.pgid))
    assert t.stop(p.id).state == "stopped"  # déjà arrêté : sans effet


def test_stop_kills_the_whole_group_and_survives_sigterm_trap(tmp_path, tables):
    t = _table(tmp_path, tables)
    # un enfant qui ignore SIGTERM : tué par le SIGKILL après la grâce
    p = t.start("trap '' TERM; sleep 60 & sleep 60; wait")
    _wait(lambda: subprocess.run(["pgrep", "-g", str(p.pgid)], capture_output=True).stdout.count(b"\n") >= 2)
    t0 = time.time()
    assert t.stop(p.id).state == "stopped"
    assert time.time() - t0 < 10
    assert _wait(lambda: not subprocess.run(["pgrep", "-g", str(p.pgid)], capture_output=True).stdout.strip())


def test_cap_per_thread_and_unknown_id(tmp_path, tables):
    t = _table(tmp_path, tables, max_running=2)
    a, b = t.start("sleep 30"), t.start("sleep 30")
    with pytest.raises(ProcessError, match="plafond 2"):
        t.start("sleep 30")
    t.stop(a.id)
    t.start("sleep 30")  # une place libérée
    with pytest.raises(ProcessError, match="inconnu"):
        t.logs("bg_00000000")
    assert b.id in {x.id for x in t.list()}


def test_a_recycled_pid_is_never_signalled(tmp_path, tables):
    """L'id pointe vers un groupe qui n'est plus le nôtre (pid recyclé) : `lost`, aucun signal."""
    t = _table(tmp_path, tables)
    p = t.start("sleep 30")
    victim = subprocess.Popen(["sleep", "30"], start_new_session=True)  # pas à nous : tué par ce test seulement
    t.stop(p.id)
    p2 = t.start("sleep 30")
    real = p2.pgid
    try:
        p2.pgid = victim.pid  # comme si le pid avait été réattribué à un processus étranger
        assert t.stop(p2.id).state == "lost"
        assert victim.poll() is None  # toujours vivant
    finally:
        p2.pgid, p2.state = real, "running"
        t.stop(p2.id)
        victim.kill()
        victim.wait()


def test_logs_are_tailed_and_clipped(tmp_path, tables):
    t = _table(tmp_path, tables)
    p = t.start(f"{PY} -c \"[print('ligne', i) for i in range(5000)]\"")
    assert _wait(lambda: t.logs(p.id)["state"] == "exited")
    got = t.logs(p.id, tail=10)
    assert got["output"].splitlines() == [f"ligne {i}" for i in range(4990, 5000)] and got["truncated"]
    assert t.logs(p.id, tail=10**6)["tail"] == 2000  # borné


def test_stop_all_and_reap_after_a_killed_kernel(tmp_path, tables):
    t = _table(tmp_path, tables)
    a, b = t.start("sleep 30"), t.start("sleep 30")
    assert sorted(f.name for f in (tmp_path / "logs").glob("*.json")) == sorted([f"{a.id}.json", f"{b.id}.json"])
    assert t.stop_all() == 2 and not list((tmp_path / "logs").glob("*.json"))
    # noyau tué : les fiches restent, le ménage au démarrage arrête ce qui porte encore sa marque
    root = tmp_path / "state" / "processes"
    t2 = ProcessTable(ShellEnv(str(tmp_path)), str(root / "th_x-1234"), host_logs=True, settle_s=0)
    tables.append(t2)
    c = t2.start("sleep 30")
    (root / "th_x-1234" / "bg_deadbeef.json").write_text(json.dumps({"id": "bg_deadbeef", "pgid": os.getpid()}))
    assert reap_processes(root) == 1  # c seulement : le noyau de test (sans la marque) n'est jamais touché
    assert _wait(lambda: not _alive(c.pgid)) and not list(root.glob("*/*.json"))
    assert reap_processes(tmp_path / "absent") == 0


def test_preview_signer(tmp_path):
    clock = [1000.0]
    s = PreviewSigner(clock=lambda: clock[0])
    url, exp = s.link("bg_0123abcd")
    cap = url.split("/")[5]
    assert url.startswith("/v1/local/preview/bg_0123abcd/") and url.endswith("/") and exp == 4600.0
    assert s.verify("bg_0123abcd", cap) and not s.verify("bg_99999999", cap)
    assert not s.verify("bg_0123abcd", cap[:-1] + "0") and not s.verify("bg_0123abcd", "x")
    clock[0] = 5000.0
    assert not s.verify("bg_0123abcd", cap)


# ─────────────────────────────── outils de la source de code ───────────────────────────────


def _source(tmp_path, monkeypatch, background=True) -> CodeToolSource:
    work = tmp_path / "repo"
    work.mkdir(exist_ok=True)
    src = CodeToolSource(root=str(work), backend="local", background=background, state_dir=str(tmp_path / "state"))
    monkeypatch.setattr(src, "_open", lambda tid: code.Workspace(env=ShellEnv(str(work)), ops=None, todos=None,
                                                                 cwd=str(work)))
    return src


def test_specs_follow_the_flag_and_effects(tmp_path, monkeypatch):
    off = CodeToolSource(root=str(tmp_path), backend="local")
    assert not set(PROCESS_TOOLS) & {s.name for s in off.specs()}
    assert off.call("process_list", {}, CTX).content.startswith("outil inconnu")
    specs = {s.name: s for s in CodeToolSource(root=str(tmp_path), backend="local", background=True).specs()}
    assert set(PROCESS_TOOLS) <= set(specs)
    assert {n: specs[n].effect for n in PROCESS_TOOLS} == {
        "process_start": Effect.DESTRUCTIVE, "process_list": Effect.READ, "process_logs": Effect.READ,
        "process_stop": Effect.READ}
    assert specs["process_logs"].fresh and specs["process_list"].fresh and not specs["process_stop"].fresh

    class Det:
        @staticmethod
        def detect_hardline_command(c):
            return (c == "mkfs /dev/sda", "format")

        @staticmethod
        def detect_dangerous_command(c):
            return (c.startswith("rm -rf"), "rm", "rm")

    monkeypatch.setattr(code, "_hermes_detection", lambda: Det)
    start = specs["process_start"]
    assert start.effect_for({"command": "npm run dev"}) is Effect.WRITE  # comme terminal
    assert start.effect_for({"command": "tail -f app.log"}) is Effect.READ
    assert start.effect_for({"command": "rm -rf build && npm run dev"}) is Effect.DESTRUCTIVE


def test_tools_end_to_end_and_close_stops_everything(tmp_path, monkeypatch):
    src = _source(tmp_path, monkeypatch)
    try:
        r = src.call("process_start", {"command": f"{PY} -u -c \"print('écoute'); import time; time.sleep(30)\"",
                                       "name": "srv", "port": 5173}, CTX)
        started = json.loads(r.content)
        assert not r.is_error and started["state"] == "running" and started["port"] == 5173
        assert "aperçu privé" in started["hint"]
        pid = started["id"]
        listing = json.loads(src.call("process_list", {}, CTX).content)["processes"]
        assert [p["id"] for p in listing] == [pid]
        assert _wait(lambda: "écoute" in json.loads(src.call("process_logs", {"id": pid}, CTX).content)["output"])
        other = ToolContext(thread_id="th_autre", turn_index=0, channel="api")
        assert src.call("process_stop", {"id": pid}, other).is_error  # un autre fil ne l'arrête pas
        assert json.loads(src.call("process_list", {}, other).content)["processes"] == []
        logs_dir = src._process_dir(CTX.thread_id)
        assert logs_dir.startswith(str(tmp_path / "state" / "processes")) and os.path.isfile(f"{logs_dir}/{pid}.log")
        pgid = src.workspace(CTX.thread_id).procs.get(pid).pgid
        assert src.call("process_start", {"command": "  "}, CTX).is_error
        assert src.call("process_start", {"command": "sleep 1", "port": 70000}, CTX).is_error
        assert src.call("process_logs", {}, CTX).content == "argument manquant : id"
        monkeypatch.setattr(code, "hardline_reason", lambda c: "fork bomb" if ":(){" in c else None)
        refused = src.call("process_start", {"command": ":(){ :|:& };:"}, CTX)
        assert refused.is_error and "jamais exécuté" in refused.content
    finally:
        src.close()
    assert _wait(lambda: not _alive(pgid))  # fin du fil : arrêté


def test_permissions_apply_terminal_rules_and_hardline(tmp_path, monkeypatch):
    from nokker.local.approvals import suggested_rules
    from nokker.qa.permissions import LocalPermissions, Mode, PermissionGates, Rule

    call = ToolCall(id="c1", name="process_start", arguments={"command": "npm run dev -- --port 5173"})
    assert Rule.parse("terminal(npm run*)").matches(call)  # une règle terminal couvre la même commande
    assert Rule.parse("process_start(npm run*)").matches(call)
    assert not Rule.parse("process_start(npm run*)").matches(ToolCall(id="c2", name="terminal",
                                                                      arguments={"command": "npm run dev"}))
    perms = LocalPermissions(mode=Mode.DEFAULT, deny=[Rule.parse("terminal(npm run dev*)")])
    assert perms.decide(call, Effect.WRITE)[0] is Decision.DENY
    assert LocalPermissions(mode=Mode.DEFAULT).decide(call, Effect.WRITE)[0] is Decision.HITL  # carte, comme terminal
    stop = ToolCall(id="c3", name="process_stop", arguments={"id": "bg_0123abcd"})
    assert LocalPermissions(mode=Mode.DEFAULT).decide(stop, Effect.READ)[0] is Decision.ALLOW  # sans carte
    assert suggested_rules("process_start", {"command": "npm run dev"}, "standard") == [
        "process_start(npm run dev)", "process_start(npm run*)"]

    monkeypatch.setattr(code, "hardline_reason", lambda c: "fork bomb")
    gates = PermissionGates(inner=None, permissions=LocalPermissions(mode=Mode.AUTO))
    v = gates.action(call, None, None)
    assert v[0].decision is Decision.DENY and v[0].rule == "permissions:hardline"


# ─────────────────────────────── aperçu d'un port par la route locale ───────────────────────────────


def _get(port, path, token=None, headers=None):
    h = {"Authorization": f"Bearer {token}"} if token else {}
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers={**h, **(headers or {})})

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=10) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_preview_route_serves_the_port_with_sandbox_headers(tmp_path, monkeypatch):
    from nokker.api.server import make_handler
    from nokker.approvals.memory import InMemoryApprovals
    from nokker.local.api import LOCAL_OWNER
    from tests.unit.api.test_approvals_routes import make_kernel_with_approvals
    from tests.unit.test_local_api import _api, _req

    approvals = InMemoryApprovals()
    kernel, _ = make_kernel_with_approvals(approvals)
    api = _api(tmp_path)
    api.state_dir = tmp_path / "state"
    api.code.background, api.code.state_dir = True, str(tmp_path / "state")
    app = tmp_path / "projets" / "app"
    (app / "static").mkdir()
    (app / "index.html").write_text('<link rel="stylesheet" href="static/s.css"><p>bonjour</p>')
    (app / "static" / "s.css").write_text("p{color:red}")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(kernel, {api.token: LOCAL_OWNER}, approvals=approvals,
                                                               local_api=api))
    port = httpd.server_address[1]
    api.allowed_hosts = frozenset({f"127.0.0.1:{port}"})
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    dev = _free_port()
    try:
        status, body = _req(port, "/v1/local/sessions", token=api.token, body={"cwd": str(app)})
        tid = body["thread_id"]
        src = api.code.for_thread(tid)
        monkeypatch.setattr(src, "_open", lambda t: code.Workspace(env=ShellEnv(str(app)), ops=None, todos=None,
                                                                   cwd=str(app)))
        ctx = ToolContext(thread_id=tid, turn_index=0, channel="api")
        started = json.loads(src.call("process_start", {"command": f"{PY} -m http.server {dev} --bind 127.0.0.1",
                                                        "port": dev}, ctx).content)
        pid = started["id"]
        assert _req(port, "/v1/local/processes")[0] == 401
        status, listing = _req(port, "/v1/local/processes", token=api.token)
        assert status == 200 and [(p["thread_id"], p["id"], p["port"]) for p in listing["processes"]] == [
            (tid, pid, dev)]
        assert _req(port, f"/v1/local/processes/{pid}/link", body={})[0] == 401
        status, link = _req(port, f"/v1/local/processes/{pid}/link", token=api.token, body={})
        assert status == 200 and link["url"].endswith("/") and link["port"] == dev
        _wait(lambda: _get(port, link["url"])[0] == 200)
        status, headers, page = _get(port, link["url"])  # l'iframe : sans en-tête d'authentification
        assert status == 200 and b"bonjour" in page and headers["Content-Type"].startswith("text/html")
        assert headers["Content-Security-Policy"].startswith("sandbox") and "allow-same-origin" not in \
            headers["Content-Security-Policy"] and headers["X-Content-Type-Options"] == "nosniff"
        status, headers, css = _get(port, link["url"] + "static/s.css")  # URL relative : sous le préfixe signé
        assert status == 200 and css == b"p{color:red}" and headers["Content-Type"].startswith("text/css")
        status, headers, _ = _get(port, link["url"] + "static")  # redirection de l'appli : gardée sous le préfixe
        assert status == 301 and headers["Location"] == link["url"] + "static/"
        assert _get(port, link["url"] + "absent.txt")[0] == 404
        bad = link["url"].replace(link["url"].split("/")[5], link["url"].split("/")[5][:-1] + "0")
        assert _get(port, bad)[0] == 404
        assert _get(port, link["url"], headers={"X-Nokker-Relay": "1"})[0] == 404  # jamais par la passerelle
        assert _req(port, "/v1/local/processes/bg_00000000/link", token=api.token, body={})[0] == 404
        src.call("process_stop", {"id": pid}, ctx)
        assert _get(port, link["url"])[0] == 502  # arrêté : plus rien à montrer
        assert _req(port, f"/v1/local/processes/{pid}/link", token=api.token, body={})[0] == 404
    finally:
        api.code.close()
        httpd.shutdown()


def test_config_flag_defaults_off(tmp_path):
    from nokker.local.api import LocalApi

    root = tmp_path / "p"
    root.mkdir()
    base = {"token_path": str(tmp_path / "tok"), "state_dir": str(tmp_path / "st"), "code": {"roots": [str(root)]}}
    assert not LocalApi.from_config(base, port=8742).code.background
    on = LocalApi.from_config({**base, "background_processes": True}, port=8742)
    assert on.code.background and on.code.state_dir == str(tmp_path / "st")
    assert set(PROCESS_TOOLS) <= {s.name for s in on.code.specs()}
    import yaml

    deploy = yaml.safe_load(open(os.path.join(os.path.dirname(__file__), "../../../deploy/lokkal/core-config.yaml")))
    assert deploy["local"]["background_processes"] is True


# ─────────────────────────────── bac à sable docker (essai réel) ───────────────────────────────
# Comme test_code.py : joué seulement avec NOKKER_TEST_DOCKER_ROOT (dossier vu au même chemin par le démon et par ce
# processus) et Hermes importable.

HERMES = __import__("importlib.util").util.find_spec("tools") is not None


@pytest.mark.skipif(not (HERMES and os.environ.get("NOKKER_TEST_DOCKER_ROOT")),
                    reason="NOKKER_TEST_DOCKER_ROOT absent ou Hermes non importable")
def test_docker_background_server_logs_preview_and_cleanup(monkeypatch):
    import shutil
    import uuid

    base = os.environ["NOKKER_TEST_DOCKER_ROOT"]
    work = os.path.join(base, f"ws-{uuid.uuid4().hex[:8]}")
    os.makedirs(work)
    with open(os.path.join(work, "index.html"), "w") as f:
        f.write("<p>depuis le bac à sable</p>")
    monkeypatch.setenv("HERMES_HOME", os.path.join(base, ".hermes"))
    instance = f"x6test-{uuid.uuid4().hex[:6]}"
    src = CodeToolSource(root=work, backend="docker", background=True, instance=instance,
                         image=os.environ.get("NOKKER_TEST_SANDBOX_IMAGE", code.DEFAULT_SANDBOX_IMAGE))
    ctx = ToolContext(thread_id="th_dockerbg", turn_index=0, channel="api")

    def ours() -> list[str]:
        return subprocess.run(["docker", "ps", "-aq", "--filter", f"label=nokker.instance={instance}"],
                              capture_output=True, text=True, check=True).stdout.split()

    try:
        r = json.loads(src.call("process_start", {"command": "python3 -u -m http.server 8000 --bind 127.0.0.1",
                                                  "name": "web", "port": 8000}, ctx).content)
        pid = r["id"]
        assert r["state"] == "running", r
        cid = src.workspace(ctx.thread_id).env._container_id
        ports = subprocess.run(["docker", "inspect", "--format", "{{json .NetworkSettings.Ports}}", cid],
                               capture_output=True, text=True, check=True).stdout.strip()
        assert ports in ("{}", "null"), ports  # aucun port publié sur l'hôte
        table = src.process_table(ctx.thread_id)
        assert _wait(lambda: table.fetch(pid, "/index.html")[0] == 200, timeout=15)
        status, ctype, body, _ = table.fetch(pid, "/index.html")  # joint DANS le bac à sable
        assert "bac à sable" in body.decode() and ctype.startswith("text/html")
        assert _wait(lambda: "GET /index.html" in json.loads(src.call("process_logs", {"id": pid}, ctx).content)
                     ["output"], timeout=10)
        log_in_box = subprocess.run(["docker", "exec", cid, "ls", "/tmp/.nokker-bg"], capture_output=True,
                                    text=True).stdout
        assert f"{pid}.log" in log_in_box and not os.path.exists(os.path.join(work, ".nokker-bg"))  # journal dans le container
        assert json.loads(src.call("process_stop", {"id": pid}, ctx).content)["state"] == "stopped"
        left = subprocess.run(["docker", "exec", cid, "sh", "-c", "ps -eo args | grep -c '[h]ttp.server' || true"],
                              capture_output=True, text=True).stdout.strip()
        assert left == "0", left
        src.call("process_start", {"command": "sleep 300"}, ctx)  # encore en cours à la fermeture du fil
        assert ours() == [cid[:12]] or [c for c in ours() if cid.startswith(c)]
    finally:
        src.close()
        from tools.environments.docker import DockerEnvironment

        DockerEnvironment.wait_for_all_teardowns(30)
        shutil.rmtree(work, ignore_errors=True)
    assert ours() == []  # fin du fil : le container et tout ce qui y tournait sont partis
