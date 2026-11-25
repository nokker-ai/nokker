"""Un chemin qui sort du dossier de travail est un REFUS pour l'hôte (`status: "refused"`, `reason: "outside_root"` dans
`tool.result` et la réponse de `tools/call`), comme le refus d'une porte ; le modèle, lui, reçoit le même texte et le même
`is_error` qu'avant. Un chemin dedans, ou un chemin de l'hôte sous le dossier (traduit), n'est pas un refus."""
from __future__ import annotations

import os

import pytest

from nokker.core.ports import ToolContext
from nokker.core.turn import InputCheck, ToolCall, Turn
from nokker.tools.checkpoints import direct_result_body
from nokker.tools.code import CodeToolSource, Workspace
from tests.unit.tools.test_code_host_paths import Env, Ops
from tests.unit.tools.test_code_host_paths import _src as docker_src

CTX = ToolContext(thread_id="th_1", turn_index=0, channel="api")


def _local(tmp_path):
    work = tmp_path / "w"
    work.mkdir()
    (tmp_path / "secret.txt").write_text("x")
    (work / "ok.txt").write_text("ok")
    os.symlink(tmp_path / "secret.txt", work / "lien")
    os.symlink(tmp_path, work / "dirlien")
    src = CodeToolSource(root=str(work), backend="local")
    ops = Ops()
    src._spaces["th_1"] = Workspace(env=Env(), ops=ops, todos=None, cwd=str(work))
    return src, ops, work


def _arguments(name, path):
    return {"read_file": {"path": path}, "write_file": {"path": path, "content": "x"},
            "search_files": {"pattern": "x", "path": path},
            "patch": {"mode": "replace", "path": path, "old_string": "a", "new_string": "b"}}[name]


@pytest.mark.parametrize("name", ["read_file", "write_file", "search_files", "patch"])
@pytest.mark.parametrize("path", ["../secret.txt", "/etc/passwd", "lien", "dirlien/secret.txt", "~/.ssh/id_ed25519"])
def test_outside_the_root_is_a_refusal_for_the_host(tmp_path, name, path):
    src, ops, _ = _local(tmp_path)
    r = src.call(name, _arguments(name, path), CTX)
    assert r.refused is True and r.reason == "outside_root"
    assert r.is_error is True and r.content.startswith("REFUSÉ : ") and "hors du dossier de travail" in r.content  # modèle : idem
    assert ops.calls == []                                                      # rien n'a touché le dossier
    assert direct_result_body(name, r)["status"] == "refused" and direct_result_body(name, r)["reason"] == "outside_root"


def test_v4a_patch_outside_the_root_is_a_refusal(tmp_path):
    src, _, _ = _local(tmp_path)
    r = src.call("patch", {"mode": "patch", "patch": "*** Begin Patch\n*** Add File: ../evil.py\n+x\n*** End Patch"}, CTX)
    assert r.refused is True and r.reason == "outside_root" and r.is_error and r.content.startswith("REFUSÉ")


@pytest.mark.parametrize("name", ["read_file", "write_file", "search_files", "patch"])
def test_inside_the_root_is_unchanged(tmp_path, name):
    src, _, work = _local(tmp_path)
    for path in ("ok.txt", str(work / "ok.txt"), "."):
        r = src.call(name, _arguments(name, path), CTX)
        assert r.refused is False and r.reason is None, (name, path, r.content)
        assert "status" not in direct_result_body(name, r) and "reason" not in direct_result_body(name, r)


def test_another_refusal_of_the_tool_is_not_outside_root(tmp_path):
    src, _, _ = _local(tmp_path)
    r = src.call("execute_code", {"code": "print(1)"}, CTX)
    assert not (r.refused and r.reason == "outside_root")


def test_host_path_under_the_cwd_is_translated_not_refused(tmp_path):
    ws = tmp_path / "srv" / "ws"
    ws.mkdir(parents=True)
    src, _, _ = docker_src(ws)
    for name, args in (("write_file", {"path": f"{ws}/a.txt", "content": "x"}), ("read_file", {"path": f"{ws}/a.txt"}),
                       ("search_files", {"pattern": "x", "path": str(ws)})):
        r = src.call(name, args, CTX)
        assert r.refused is False and r.reason is None and not r.is_error, name


def test_symlink_escape_in_docker_reads_is_a_refusal(tmp_path):
    ws, out = tmp_path / "ws", tmp_path / "dehors"
    ws.mkdir()
    out.mkdir()
    (out / "secret").write_text("s")
    os.symlink(out, ws / "lien")
    src, _, rd = docker_src(ws)
    for path in (f"{ws}/lien/secret", "/workspace/lien/secret"):
        r = src.call("read_file", {"path": path}, CTX)
        assert r.refused is True and r.reason == "outside_root" and r.is_error and "sort du dossier de travail" in r.content
    assert not rd.calls


def test_ssh_copy_destination_outside_the_root_is_a_refusal(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    src, _, _ = docker_src(ws)
    with pytest.raises(PermissionError) as e:
        src.workspace_path("th_1", f"{ws}/../x")
    assert getattr(e.value, "outside_root", False) is True


def test_tool_result_event_carries_status_and_reason(tmp_path):
    from nokker.api.events import EventBus
    from nokker.api.server import build_hub
    from nokker.config import NokkerConfig

    hub = build_hub(NokkerConfig(web_search_enabled=False, approvals_enabled=False), EventBus())
    seen = []
    hub.publish = lambda name, data: seen.append((name, data))
    src, _, _ = _local(tmp_path)
    turn, call = Turn(thread_id="th_x", index=0, input=InputCheck(text="x")), ToolCall(id="c1", name="read_file", arguments={})
    hub.tool_result(turn, call, src.call("read_file", {"path": "../secret.txt"}, CTX), 3)
    hub.tool_result(turn, call, src.call("read_file", {"path": "ok.txt"}, CTX), 3)
    refused, fine = [d for n, d in seen if n == "tool.result"]
    assert refused["status"] == "refused" and refused["reason"] == "outside_root"
    assert refused["is_error"] is True and refused["ok"] is False        # ce que l'hôte lisait déjà : inchangé
    assert "status" not in fine and "reason" not in fine


# ── n°27 (a) : backend docker, chemin absolu hors /workspace ──
_DOCKER_OUTSIDE = ["/etc/passwd", "/tmp/x", "/root/.ssh/id_ed25519", "/workspacex/f", "/workspace/../etc/passwd", "../../etc/x"]


@pytest.mark.parametrize("reader", [True, False])
@pytest.mark.parametrize("name", ["read_file", "write_file", "search_files", "patch"])
@pytest.mark.parametrize("path", _DOCKER_OUTSIDE)
def test_docker_absolute_path_outside_the_mount_is_a_refusal(tmp_path, name, path, reader):
    ws = tmp_path / "ws"
    ws.mkdir()
    src, ops, rd = docker_src(ws, reader=reader)
    r = src.call(name, _arguments(name, path), CTX)
    assert r.refused is True and r.reason == "outside_root"
    assert r.is_error is True and r.content.startswith("REFUSÉ : ") and "hors du dossier de travail" in r.content
    assert ops.calls == [] and rd.calls == []                    # rien n'est parti vers le bac
    body = direct_result_body(name, r)
    assert body["status"] == "refused" and body["reason"] == "outside_root"


def test_docker_v4a_patch_outside_the_mount_is_a_refusal(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    src, ops, _ = docker_src(ws)
    for head in ("*** Add File: /tmp/evil.py", "*** Move File: /workspace/a.py -> /etc/b.py"):
        r = src.call("patch", {"mode": "patch", "patch": f"*** Begin Patch\n{head}\n+x\n*** End Patch"}, CTX)
        assert r.refused is True and r.reason == "outside_root" and r.is_error and r.content.startswith("REFUSÉ")
    assert ops.calls == []


@pytest.mark.parametrize("name", ["read_file", "write_file", "search_files", "patch"])
def test_docker_inside_the_mount_is_unchanged(tmp_path, name):
    ws = tmp_path / "ws"
    ws.mkdir()
    src, ops, rd = docker_src(ws)
    for path in ("a.txt", "/workspace/a.txt", "/workspace", ".", "sous/../a.txt", f"{ws}/a.txt"):
        r = src.call(name, _arguments(name, path), CTX)
        assert r.refused is False and r.reason is None and not r.is_error, (name, path, r.content)
    assert ops.calls or rd.calls


def test_docker_subdir_cwd_may_go_up_inside_the_mount(tmp_path):
    ws = tmp_path / "ws"
    (ws / "sub").mkdir(parents=True)
    src, ops, _ = docker_src(ws, subdir="sub")
    assert not src.call("write_file", {"path": "../a.txt", "content": "x"}, CTX).is_error
    assert ops.calls == [("write_file", "/workspace/a.txt")]
    assert src.call("write_file", {"path": "../../a.txt", "content": "x"}, CTX).reason == "outside_root"


def test_docker_terminal_is_not_rewritten_nor_refused_by_the_path_rule(tmp_path):
    from tests.unit.tools.test_code_host_paths import _term

    ws = tmp_path / "ws"
    ws.mkdir()
    r, env, _ = _term(ws, "cat /etc/passwd", 0, "root:x:0:0")
    assert env.commands == ["cat /etc/passwd"] and not r.refused and r.reason is None and not r.is_error


def test_docker_tool_result_event_carries_status_and_reason(tmp_path):
    from nokker.api.events import EventBus
    from nokker.api.server import build_hub
    from nokker.config import NokkerConfig

    hub = build_hub(NokkerConfig(web_search_enabled=False, approvals_enabled=False), EventBus())
    seen = []
    hub.publish = lambda name, data: seen.append((name, data))
    ws = tmp_path / "ws"
    ws.mkdir()
    src, _, _ = docker_src(ws)
    turn, call = Turn(thread_id="th_x", index=0, input=InputCheck(text="x")), ToolCall(id="c1", name="write_file", arguments={})
    hub.tool_result(turn, call, src.call("write_file", {"path": "/tmp/x", "content": "x"}, CTX), 3)
    refused = next(d for n, d in seen if n == "tool.result")
    assert refused["status"] == "refused" and refused["reason"] == "outside_root" and refused["is_error"] is True
