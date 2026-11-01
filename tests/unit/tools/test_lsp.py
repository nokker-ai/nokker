"""Diagnostics de langage à l'édition (nokker/tools/lsp.py, lot 13, docs/LOKKAL-LSP.md).

Le client JSON-RPC joue contre un FAUX serveur de langage sur stdio (tests/unit/tools/fake_lsp_server.py,
déterministe : une ligne `ERR <code> <message>` = une erreur). Puis : forme exacte du bloc `[Diagnostics]`, plafonds,
lancement dans le bac à sable, et la règle du gel : drapeaux éteints = schémas et résultats IDENTIQUES à avant.
Un dernier test fait tourner pyright pour de vrai s'il est installé (sauté sinon)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from nokker.core.ports import ToolContext
from nokker.core.turn import Effect
from nokker.tools import lsp
from nokker.tools.code import CODE_TOOLS, CodeToolSource, SharedEnv, Workspace
from nokker.tools.lsp import (Diag, LspClient, LspManager, errors_fixed, format_block, language_of,
                              sandbox_argv)

FAKE = str(Path(__file__).with_name("fake_lsp_server.py"))
CTX = ToolContext(thread_id="t-lsp", turn_index=0, channel="api")


def fake(*flags: str) -> tuple[str, ...]:
    return (sys.executable, FAKE, *flags)


class HostEnv:
    """Environnement « local » minimal : `execute` comme l'amont (returncode, output)."""

    def __init__(self, cwd: str) -> None:
        self.cwd = cwd
        self.commands: list[str] = []

    def execute(self, command: str, cwd: str = "", timeout: int = 120, **_: object) -> dict:
        self.commands.append(command)
        p = subprocess.run(["bash", "-c", command], cwd=cwd or self.cwd, capture_output=True, text=True,
                           timeout=timeout)
        return {"returncode": p.returncode, "output": p.stdout + p.stderr}

    def cleanup(self) -> None:
        pass


def d(path: str, line: int, msg: str, code: str = "rX", sev: str = "error", col: int = 1) -> Diag:
    return Diag(path=path, line=line, col=col, severity=sev, code=code, message=msg)


# ─────────────────────────────── bloc ───────────────────────────────


def test_block_exact_form_sorted_errors_only():
    after = [d("src/b.py", 3, "trois"), d("src/a.py", 12, "douze", col=5), d("src/a.py", 2, "deux", code=""),
             d("src/a.py", 1, "avert", sev="warning")]
    assert format_block(after, 2) == (
        "\n\n[Diagnostics]\n"
        "src/a.py:2:1 error: deux\n"
        "src/a.py:12:5 error rX: douze\n"
        "src/b.py:3:1 error rX: trois\n"
        "2 erreurs corrigées\n"
        "[/Diagnostics]")


def test_block_caps_at_20_lines_then_more():
    after = [d("m.py", i, f"e{i}") for i in range(1, 26)]
    lines = format_block(after).split("\n")
    assert lines[:3] == ["", "", "[Diagnostics]"] and lines[-1] == "[/Diagnostics]"
    body = lines[3:-1]
    assert len(body) == 21 and body[0] == "m.py:1:1 error rX: e1" and body[19] == "m.py:20:1 error rX: e20"
    assert body[20] == "… 5 de plus"


def test_block_fixed_singular_and_only_fixed():
    assert format_block([], 1) == "\n\n[Diagnostics]\n1 erreur corrigée\n[/Diagnostics]"
    assert format_block([d("a.py", 1, "x", sev="warning")], 3) == "\n\n[Diagnostics]\n3 erreurs corrigées\n[/Diagnostics]"


def test_no_block_without_errors_or_fixes():
    assert format_block([], 0) == ""
    assert format_block([d("a.py", 1, "w", sev="warning"), d("a.py", 2, "i", sev="information")], 0) == ""


def test_diag_from_lsp_one_line_cut_and_base_1():
    raw = {"range": {"start": {"line": 11, "character": 4}}, "severity": 1, "code": {"value": 2339},
           "message": "Cannot access\n  attribute   " + "x" * 300}
    got = Diag.from_lsp("src/calc.py", raw)
    assert (got.line, got.col, got.code) == (12, 5, "2339")
    assert "\n" not in got.message and len(got.message) == 200 and got.message.startswith("Cannot access attribute x")
    assert Diag.from_lsp("a.py", {"message": "m"}).severity == "error"  # sévérité absente : la plus prudente
    assert Diag.from_lsp("a.py", {"message": "m", "severity": 2}).text() == "a.py:1:1 warning: m"


def test_errors_fixed_counts_disappeared_errors_by_code_and_message():
    before = [d("a.py", 1, "x"), d("a.py", 2, "x"), d("a.py", 3, "y"), d("a.py", 4, "w", sev="warning")]
    after = [d("a.py", 9, "x"), d("a.py", 10, "z")]  # lignes décalées : même erreur
    assert errors_fixed(before, after) == 2  # un « x » et le « y » ; l'avertissement ne compte pas
    assert errors_fixed([], after) == 0


def test_language_detection_by_extension():
    assert language_of("/w/a.py") == ("python", "python") and language_of("x.PYI")[0] == "python"
    assert language_of("c.tsx") == ("typescript", "typescriptreact") and language_of("d.mjs")[1] == "javascript"
    assert language_of("Makefile") is None and language_of("x.go") is None


# ─────────────────────────────── client JSON-RPC ───────────────────────────────


def test_client_open_change_publish_and_shutdown(tmp_path):
    seen: list[tuple[str, int]] = []
    c = LspClient(list(fake("--ask-config")), cwd=str(tmp_path), on_publish=lambda p, ds: seen.append((p, len(ds))))
    try:
        c.initialize(str(tmp_path), timeout=5)
        path = str(tmp_path / "a.py")
        mark, v = c.sync(path, "python", "x = 1\ny  # ERR rUndef \"y\" is not defined\n")
        assert v == 1
        got = c.wait(path, mark, v, time.monotonic() + 5)  # la requête de configuration a reçu sa réponse avant
        assert [(g["range"]["start"]["line"], g["code"]) for g in got] == [(1, "rUndef")]
        mark, v = c.sync(path, "python", "x = 1\n")
        assert v == 2 and c.wait(path, mark, v, time.monotonic() + 5) == []
        assert c.current(path) == [] and seen == [(path, 1), (path, 0)]
    finally:
        c.close()
    assert not c.alive and c.proc.poll() is not None


def test_client_waits_for_its_version_and_times_out(tmp_path):
    c = LspClient(list(fake("--silent")), cwd=str(tmp_path))
    try:
        c.initialize(str(tmp_path), timeout=5)
        mark, v = c.sync(str(tmp_path / "a.py"), "python", "ERR r x\n")
        t0 = time.monotonic()
        assert c.wait(str(tmp_path / "a.py"), mark, v, t0 + 0.3) is None
        assert time.monotonic() - t0 < 2
    finally:
        c.close()


def test_client_reports_a_dead_server(tmp_path):
    c = LspClient(list(fake("--crash-init")), cwd=str(tmp_path))
    with pytest.raises(lsp.LspError):
        c.initialize(str(tmp_path), timeout=5)
    c.close()
    assert not c.alive


# ─────────────────────────────── lancement dans le bac à sable ───────────────────────────────


def test_sandbox_argv_per_backend():
    class Docker:
        _container_id, _docker_exe = "abc123", "/usr/bin/docker"

    class Kubectl:
        def exec_argv(self, pod, argv, stdin):
            return ["kubectl", "exec", *(["-i"] if stdin else []), pod, "--", *argv]

    class Pod:
        kubectl, pod = Kubectl(), "nokker-sb-1"

    argv = ["pyright-langserver", "--stdio"]
    assert sandbox_argv(Docker(), "/workspace", argv) == (
        ["/usr/bin/docker", "exec", "-i", "-w", "/workspace", "-e", "HOME=/tmp", "abc123", *argv], None)
    # sous-agent : l'environnement prêté est celui du parent (même container)
    assert sandbox_argv(SharedEnv(Docker(), "/workspace/wt"), "/workspace/wt", argv)[0][7] == "abc123"
    assert sandbox_argv(Pod(), "/ws/a b", argv) == (
        ["kubectl", "exec", "-i", "nokker-sb-1", "--", "sh", "-c",
         "cd '/ws/a b' && exec pyright-langserver --stdio"], None)
    assert sandbox_argv(HostEnv("/x"), "/x", argv) == (argv, "/x")


# ─────────────────────────────── serveurs d'un fil ───────────────────────────────


def manager(tmp_path, *flags: str, inline: bool = True, **kw) -> tuple[LspManager, lsp.ThreadLsp, list]:
    events: list = []
    m = LspManager(inline=inline, servers={"python": fake(*flags)}, emit=lambda n, p: events.append((n, p)), **kw)
    return m, m.thread("t-lsp", HostEnv(str(tmp_path)), str(tmp_path)), events


def test_inline_block_then_fixed_and_events(tmp_path):
    m, t, events = manager(tmp_path)
    try:
        path = str(tmp_path / "src" / "calc.py")
        block = m.after_edit(t, [(path, "a = 1\nb  # ERR rA first\nc  # ERR rB second\n")])
        assert block == ("\n\n[Diagnostics]\nsrc/calc.py:2:6 error rA: first\nsrc/calc.py:3:6 error rB: second\n"
                         "[/Diagnostics]")
        assert m.after_edit(t, [(path, "a = 1\nc  # ERR rB second\n")]) == \
            "\n\n[Diagnostics]\nsrc/calc.py:2:6 error rB: second\n1 erreur corrigée\n[/Diagnostics]"
        assert m.after_edit(t, [(path, "a = 1\n")]) == "\n\n[Diagnostics]\n1 erreur corrigée\n[/Diagnostics]"
        assert m.after_edit(t, [(path, "a = 2\n")]) == ""  # 0 erreur, rien de réparé : pas de bloc
        assert m.after_edit(t, [(str(tmp_path / "notes.md"), "ERR r x")]) == ""  # langage non suivi
        assert [e[0] for e in events] == ["lsp.diagnostics"] * 4
        assert events[0][1] == {"thread_id": "t-lsp", "path": "src/calc.py", "counts": {"error": 2, "warning": 0},
                                "items": [{"line": 2, "col": 6, "severity": "error", "code": "rA", "message": "first"},
                                          {"line": 3, "col": 6, "severity": "error", "code": "rB",
                                           "message": "second"}]}
    finally:
        t.close()
    assert m.live() == 0


def test_no_block_on_timeout_late_diagnostics_go_to_events(tmp_path):
    m, t, events = manager(tmp_path, "--delay", "1.0", wait_s=0.3, first_wait_s=0.3)
    try:
        path = str(tmp_path / "a.py")
        assert m.after_edit(t, [(path, "x  # ERR r late\n")]) == ""
        deadline = time.monotonic() + 5
        while not events and time.monotonic() < deadline:
            time.sleep(0.05)
        assert events and events[0][1]["counts"] == {"error": 1, "warning": 0}
    finally:
        t.close()


def test_absent_binary_is_silent(tmp_path):
    m = LspManager(inline=True, servers={"python": ("nokker-absent-langserver", "--stdio")})
    env = HostEnv(str(tmp_path))
    t = m.thread("t", env, str(tmp_path))
    try:
        assert m.after_edit(t, [(str(tmp_path / "a.py"), "ERR r x\n")]) == ""
        assert m.after_edit(t, [(str(tmp_path / "a.py"), "ERR r x\n")]) == ""
        assert sum("command -v" in c for c in env.commands) == 1  # absent une fois pour toutes dans ce fil
        assert m.diagnostics(t, [(str(tmp_path / "a.py"), "")]) == {
            "files": [], "language_servers": []}
    finally:
        t.close()


def test_not_inline_does_not_wait_and_still_emits(tmp_path):
    m, t, events = manager(tmp_path, "--delay", "0.5", inline=False)
    try:
        t0 = time.monotonic()
        assert m.after_edit(t, [(str(tmp_path / "a.py"), "x  # ERR r e\n")]) == ""
        assert time.monotonic() - t0 < 0.3  # l'édition n'attend rien
        deadline = time.monotonic() + 10
        while not events and time.monotonic() < deadline:
            time.sleep(0.05)
        assert events[0][1]["path"] == "a.py"
    finally:
        t.close()


def test_diagnostics_tool_requested_and_open_files(tmp_path):
    m, t, _ = manager(tmp_path)
    try:
        a, b = str(tmp_path / "a.py"), str(tmp_path / "b.py")
        out = m.diagnostics(t, [(a, "x  # ERR r1 un\ny  # WARN w1 avert\n"), (b, "ok\n")])
        assert out == {"files": [
            {"path": "a.py", "errors": 1, "warnings": 1,
             "items": [{"line": 1, "col": 6, "severity": "error", "code": "r1", "message": "un"}]},
            {"path": "b.py", "errors": 0, "warnings": 0, "items": []}],
            "language_servers": ["pyright"]}  # nom de la spec pour le serveur python (ici le faux)
        assert list(out) == ["files", "language_servers"]  # pas de « pending » vide
        out = m.diagnostics(t, None, "warning")  # sans chemin : les fichiers déjà ouverts du fil
        assert [x["severity"] for x in out["files"][0]["items"]] == ["error", "warning"] and "pending" not in out
    finally:
        t.close()


def test_diagnostics_tool_caps_items_per_file_and_reports_pending(tmp_path):
    m, t, _ = manager(tmp_path)
    try:
        text = "".join(f"x{i}  # ERR r{i} e{i}\n" for i in range(60))
        out = m.diagnostics(t, [(str(tmp_path / "a.py"), text)])
        f = out["files"][0]
        assert f["errors"] == 60 and len(f["items"]) == 50 and f["items"][-1]["line"] == 50
    finally:
        t.close()
    m, t, _ = manager(tmp_path, "--silent", wait_s=0.3, first_wait_s=0.3)
    try:
        out = m.diagnostics(t, [(str(tmp_path / "a.py"), "ERR r x\n")])
        assert out == {"files": [], "language_servers": ["pyright"], "pending": ["a.py"]}
    finally:
        t.close()


def test_caps_per_thread_and_per_instance(tmp_path):
    started: list[str] = []

    class Stub:
        def __init__(self, name: str) -> None:
            self.name, self.alive, self.last_used, self.versions, self.closed = name, True, time.monotonic(), {}, False

        def initialize(self, root, timeout):
            pass

        def close(self, timeout=2.0):
            self.closed, self.alive = True, False

    def launch(argv, cwd, on_publish):
        started.append(argv[0])
        return Stub(argv[0])

    servers = {"python": ("true",), "typescript": ("true",), "go": ("true",)}
    m = LspManager(servers=servers, max_per_thread=2, max_total=3, launch=launch)
    t1 = m.thread("t1", HostEnv(str(tmp_path)), str(tmp_path))
    t2 = m.thread("t2", HostEnv(str(tmp_path)), str(tmp_path))
    far = time.monotonic() + 5
    a, b = t1._client("python", far, {"python"}), t1._client("typescript", far, {"typescript"})
    c = t1._client("go", far, {"go"})  # 3e langage du fil : le plus ancien inactif (python) s'arrête
    assert a.closed and not b.closed and set(t1.servers) == {"typescript", "go"}
    e = t2._client("python", far, {"python"})
    assert m.live() == 3
    f = t2._client("typescript", far, {"typescript"})  # 4e de l'instance : le plus ancien inactif ailleurs
    assert b.closed and m.live() == 3 and not (c.closed or e.closed or f.closed)
    t1.busy = True  # un fil occupé n'est jamais choisi
    t2.servers["python"].last_used = time.monotonic()
    t3 = m.thread("t3", HostEnv(str(tmp_path)), str(tmp_path))
    g = t3._client("python", far, {"python"})
    assert not c.closed and g is not None and (e.closed or f.closed)
    for t in (t1, t2, t3):
        t.close()
    assert m.live() == 0 and len(started) == 6


# ─────────────────────────────── crochet dans code.py et gel des textes ───────────────────────────────


class FakeOps:
    """Opérations de fichiers de l'amont, réduites : écrit pour de vrai, rend le dict de l'amont."""

    class R(dict):
        def to_dict(self) -> dict:
            return dict(self)

    def write_file(self, path: str, content: str):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        Path(path).write_text(content)
        return self.R(bytes_written=len(content), dirs_created=False)

    def patch_replace(self, path: str, old: str, new: str, replace_all: bool = False):
        text = Path(path).read_text()
        if old not in text:
            return self.R(success=False, error="Could not find a match for old_string")
        Path(path).write_text(text.replace(old, new) if replace_all else text.replace(old, new, 1))
        return self.R(success=True, diff="--- a\n+++ b\n")


def source(tmp_path, lsp_manager=None) -> CodeToolSource:
    src = CodeToolSource(root=str(tmp_path), backend="local", lsp=lsp_manager)
    ws = Workspace(env=HostEnv(str(tmp_path)), ops=FakeOps(), todos=None, cwd=src.root)
    src._spaces[CTX.thread_id] = ws
    return src


def edits(src: CodeToolSource) -> list:
    calls = [("write_file", {"path": "pkg/m.py", "content": "a = 1\nb  # ERR rB bad\n"}),
             ("patch", {"mode": "replace", "path": "pkg/m.py", "old_string": "a = 1", "new_string": "a = 2"}),
             ("patch", {"mode": "replace", "path": "pkg/m.py", "old_string": "absent", "new_string": "x"}),
             ("write_file", {"path": "README.md", "content": "ERR r x\n"}),
             ("patch", {"mode": "replace", "path": "pkg/m.py", "old_string": "b  # ERR rB bad\n", "new_string": ""})]
    return [src.call(n, a, CTX) for n, a in calls]


BASE_NAMES = ["read_file", "search_files", "write_file", "patch", "terminal", "todo"]


def _spec_view(specs) -> list:
    return [(s.name, s.description, json.dumps(s.parameters, sort_keys=True), s.effect, s.core, s.source, s.fresh)
            for s in specs]


def test_flags_off_tools_and_results_are_identical(tmp_path, monkeypatch):
    from nokker.local.api import LocalApi
    from nokker.connectors.crypto import SecretBox

    monkeypatch.setenv("NOKKER_LOCAL_SECRET_KEY", SecretBox.generate_key())
    base = {"state_dir": str(tmp_path / "s"), "token_path": str(tmp_path / "tok"), "code": {"backend": "local"}}
    plain = LocalApi.from_config(base, 8742)
    off = LocalApi.from_config({**base, "lsp": {"enabled": False, "inline": False}}, 8742)
    inline_only = LocalApi.from_config({**base, "lsp": {"inline": True}}, 8742)  # inline sans enabled : rien
    assert plain.code.lsp is None and off.code.lsp is None and inline_only.code.lsp is None
    assert [s.name for s in plain.code.specs()] == BASE_NAMES == list(CODE_TOOLS)
    assert _spec_view(off.code.specs()) == _spec_view(plain.code.specs()) == _spec_view(inline_only.code.specs())
    assert _spec_view(off.code.specs()) == _spec_view(CodeToolSource(root=str(tmp_path), backend="local").specs())

    (tmp_path / "w1").mkdir()
    (tmp_path / "w2").mkdir()
    before = edits(source(tmp_path / "w1"))  # comme avant le lot 13 : pas de champ lsp du tout
    after = edits(source(tmp_path / "w2", None))
    assert [(r.content, r.is_error) for r in after] == [(r.content, r.is_error) for r in before]
    assert not any("[Diagnostics]" in r.content for r in after)
    assert source(tmp_path / "w1").call("lsp_diagnostics", {}, CTX).content == "outil inconnu : lsp_diagnostics"


def test_enabled_without_inline_keeps_texts_identical_and_adds_the_tool(tmp_path):
    (tmp_path / "w1").mkdir()
    (tmp_path / "w2").mkdir()
    m = LspManager(inline=False, servers={"python": fake()})
    src = source(tmp_path / "w2", m)
    try:
        before = edits(source(tmp_path / "w1"))
        assert [(r.content, r.is_error) for r in edits(src)] == [(r.content, r.is_error) for r in before]
        specs = src.specs()
        assert [s.name for s in specs] == [*BASE_NAMES, "lsp_diagnostics"]
        assert _spec_view(specs[:-1]) == _spec_view(source(tmp_path / "w1").specs())  # les existants : intacts
        assert specs[-1].effect is Effect.READ and specs[-1].core is False
        assert specs[-1].description == (
            "Diagnostics du serveur de langage (erreurs de type et de syntaxe) pour des fichiers du dossier de travail. À utiliser après une édition pour vérifier qu\'on n\'a pas cassé les types ou les imports, avant de lancer toute la suite de tests.")
        props = specs[-1].parameters["properties"]
        assert set(props) == {"paths", "severity"} and "required" not in specs[-1].parameters
        assert props["paths"]["items"] == {"type": "string"}
        assert props["severity"]["enum"] == ["error", "warning"] and props["severity"]["default"] == "error"
    finally:
        src.close()


def test_inline_appends_the_block_at_the_end_only(tmp_path):
    (tmp_path / "w1").mkdir()
    (tmp_path / "w2").mkdir()
    m = LspManager(inline=True, servers={"python": fake()})
    src = source(tmp_path / "w2", m)
    try:
        before, after = edits(source(tmp_path / "w1")), edits(src)
        blocks = [r.content[len(b.content):] for r, b in zip(after, before)]
        assert all(r.content.startswith(b.content) for r, b in zip(after, before))  # aucun texte existant ne bouge
        assert blocks == [
            "\n\n[Diagnostics]\npkg/m.py:2:6 error rB: bad\n[/Diagnostics]",
            "\n\n[Diagnostics]\npkg/m.py:2:6 error rB: bad\n[/Diagnostics]",
            "",  # patch en erreur : rien
            "",  # langage non suivi
            "\n\n[Diagnostics]\n1 erreur corrigée\n[/Diagnostics]"]
        tool = json.loads(src.call("lsp_diagnostics", {"paths": ["pkg/m.py"]}, CTX).content)
        assert tool == {"files": [{"path": "pkg/m.py", "errors": 0, "warnings": 0, "items": []}],
                        "language_servers": ["pyright"]}
        bad = src.call("lsp_diagnostics", {"severity": "fatal"}, CTX)
        assert bad.is_error
    finally:
        src.close()
    assert m.live() == 0  # fermé avec le bac à sable du fil


def test_direct_tools_whitelist_follows_the_flag():
    import inspect

    from nokker.api import server

    assert "lsp_diagnostics" not in server.DIRECT_TOOLS
    assert '{"lsp_diagnostics"} if getattr(local_api.code, "lsp", None) is not None' in inspect.getsource(server)


# ─────────────────────────────── pyright réel ───────────────────────────────


@pytest.mark.skipif(shutil.which("pyright-langserver") is None, reason="pyright-langserver absent")
def test_real_pyright_broken_then_fixed(tmp_path):
    m = LspManager(inline=True)  # serveurs par défaut : pyright-langserver --stdio
    t = m.thread("t-pyright", HostEnv(str(tmp_path)), str(tmp_path))
    try:
        path = str(tmp_path / "src" / "calc.py")
        os.makedirs(os.path.dirname(path))
        broken = "def add(a: int, b: int) -> int:\n    return a + b\n\nx: int = add(1, 2)\nx.foo\n"
        Path(path).write_text(broken)
        block = m.after_edit(t, [(path, broken)])
        assert block.startswith("\n\n[Diagnostics]\nsrc/calc.py:5:3 error reportAttributeAccessIssue: ")
        assert block.endswith("\n[/Diagnostics]")
        fixed = broken.replace("x.foo\n", "x.bit_length()\n")
        assert m.after_edit(t, [(path, fixed)]) == "\n\n[Diagnostics]\n1 erreur corrigée\n[/Diagnostics]"
    finally:
        t.close()


def test_fixed_counts_only_against_the_state_before_this_call(tmp_path):
    """« N errors fixed » = disparues par rapport à l'état d'AVANT CET appel, jamais depuis le début de la session : la
    seconde édition ne recompte pas ce que la première a réparé."""
    m, t, _ = manager(tmp_path)
    try:
        path = str(tmp_path / "a.py")
        m.after_edit(t, [(path, "x  # ERR r1 un\ny  # ERR r2 deux\nz  # ERR r3 trois\n")])
        assert m.after_edit(t, [(path, "x  # ERR r1 un\n")]) == \
            "\n\n[Diagnostics]\na.py:1:6 error r1: un\n2 erreurs corrigées\n[/Diagnostics]"
        assert m.after_edit(t, [(path, "x  # ERR r1 un\n# rien d'autre\n")]) == \
            "\n\n[Diagnostics]\na.py:1:6 error r1: un\n[/Diagnostics]"  # rien de réparé PAR CET appel
        assert m.after_edit(t, [(path, "x = 1\n")]) == "\n\n[Diagnostics]\n1 erreur corrigée\n[/Diagnostics]"
    finally:
        t.close()


def test_server_messages_pass_through_untranslated(tmp_path):
    """Les messages du serveur restent tels quels (anglais), seulement ramenés sur une ligne."""
    m, t, _ = manager(tmp_path)
    try:
        msg = 'Cannot access attribute "foo" for class "int"'
        block = m.after_edit(t, [(str(tmp_path / "a.py"), f"x  # ERR reportAttributeAccessIssue {msg}\n")])
        assert block == f"\n\n[Diagnostics]\na.py:1:6 error reportAttributeAccessIssue: {msg}\n[/Diagnostics]"
    finally:
        t.close()


@pytest.mark.skipif(shutil.which("typescript-language-server") is None, reason="typescript-language-server absent")
def test_real_typescript_broken_then_fixed(tmp_path):
    m = LspManager(inline=True)
    t = m.thread("t-ts", HostEnv(str(tmp_path)), str(tmp_path))
    try:
        path = str(tmp_path / "a.ts")
        broken = "const x: number = 1;\nx.foo();\n"
        Path(path).write_text(broken)
        assert m.after_edit(t, [(path, broken)]) == (
            "\n\n[Diagnostics]\na.ts:2:3 error 2339: Property 'foo' does not exist on type 'number'.\n[/Diagnostics]")
        assert m.after_edit(t, [(path, broken.replace("foo", "toFixed"))]) == \
            "\n\n[Diagnostics]\n1 erreur corrigée\n[/Diagnostics]"
    finally:
        t.close()
