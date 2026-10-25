"""Doublures des sessions de code multi-utilisateur (docs/LOKKAL.md §7.28) : briques de fichiers de l'amont et
environnement d'exécution « kubernetes » factices, pour jouer le vrai chemin de `CodeToolSource` (chemins bornés,
lectures dans le dossier monté, écritures par l'environnement) sans l'amont ni cluster.

`FakeEnvironment` respecte la signature convenue du module du bac à sable kubernetes
(`KubernetesEnvironment(cfg, cwd, sub_path, labels, timeout)`) et imite un pod : il ne voit QUE la racine de sa session,
montée sur le point de montage ; un chemin hors du montage n'existe pas, un lien symbolique qui sort de la racine non
plus."""
from __future__ import annotations

import json
import os
import sys
import types
from dataclasses import dataclass, field
from typing import Any

import pytest


@dataclass
class _R:
    data: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return dict(self.data)


class FakeLocalEnvironment:
    """Le lecteur du noyau : chemins de l'hôte, tels quels."""

    def __init__(self, cwd: str = "", timeout: int = 60, **_: Any) -> None:
        self.cwd = cwd

    def real(self, path: str) -> str | None:
        return path

    def execute(self, command: str, **_: Any) -> dict[str, Any]:
        return {"output": "", "returncode": 0}

    def cleanup(self) -> None:
        pass


class FakeOps:
    """`ShellFileOperations` réduit : lire, écrire, remplacer, chercher, par l'environnement qui l'a ouvert."""

    def __init__(self, env: Any, cwd: str = "") -> None:
        self.env, self.cwd = env, cwd

    def read_file(self, path: str, offset: int = 1, limit: int = 500) -> _R:
        real = self.env.real(path)
        if real is None or not os.path.isfile(real):
            return _R({"error": f"File not found: {path}"})
        with open(real, encoding="utf-8") as f:
            return _R({"content": f.read(), "path": path})

    def write_file(self, path: str, content: str) -> _R:
        real = self.env.real(path)
        if real is None:
            return _R({"error": f"cannot write {path}"})
        os.makedirs(os.path.dirname(real), exist_ok=True)
        with open(real, "w", encoding="utf-8") as f:
            f.write(content)
        return _R({"bytes_written": len(content)})

    def patch_replace(self, path: str, old: str, new: str, replace_all: bool = False) -> _R:
        real = self.env.real(path)
        if real is None or not os.path.isfile(real):
            return _R({"error": f"Could not find {path}"})
        text = open(real, encoding="utf-8").read()
        with open(real, "w", encoding="utf-8") as f:
            f.write(text.replace(old, new) if replace_all else text.replace(old, new, 1))
        return _R({"success": True})

    def search(self, pattern: str, path: str = ".", **_: Any) -> _R:
        real = self.env.real(path)
        hits = []
        if real is not None and os.path.isdir(real):
            for d, _, files in os.walk(real):
                for n in files:
                    p = os.path.join(d, n)
                    try:
                        if pattern in open(p, encoding="utf-8").read():
                            hits.append({"path": p})
                    except (OSError, UnicodeDecodeError):
                        continue
        return _R({"matches": hits})


class FakeTodoStore:
    def __init__(self) -> None:
        self.items: list[Any] = []


def _todo_tool(todos: Any, merge: bool, store: FakeTodoStore) -> str:
    if todos:
        store.items = list(todos)
    return json.dumps({"todos": store.items})


@pytest.fixture
def fake_upstream(monkeypatch):
    """Briques de l'amont remplacées (présentes ou non sur la machine) : `tools.file_operations`,
    `tools.todo_tool`, `tools.environments.local`."""
    pkg = types.ModuleType("tools")
    pkg.__path__ = []  # type: ignore[attr-defined]
    fo = types.ModuleType("tools.file_operations")
    fo.ShellFileOperations = FakeOps  # type: ignore[attr-defined]
    td = types.ModuleType("tools.todo_tool")
    td.TodoStore, td.todo_tool = FakeTodoStore, _todo_tool  # type: ignore[attr-defined]
    envs = types.ModuleType("tools.environments")
    envs.__path__ = []  # type: ignore[attr-defined]
    loc = types.ModuleType("tools.environments.local")
    loc.LocalEnvironment = FakeLocalEnvironment  # type: ignore[attr-defined]
    for name, mod in (("tools", pkg), ("tools.file_operations", fo), ("tools.todo_tool", td),
                      ("tools.environments", envs), ("tools.environments.local", loc)):
        monkeypatch.setitem(sys.modules, name, mod)
    monkeypatch.delitem(sys.modules, "tools.approval_detection", raising=False)
    yield


@dataclass
class FakeCluster:
    """Ce que le faux module du bac à sable kubernetes a vu : un pod par environnement ouvert."""

    workspace_root: str
    pods: list[Any] = field(default_factory=list)
    reaped: list[str] = field(default_factory=list)

    def factory(self) -> Any:
        cluster = self

        class FakeEnvironment:
            def __init__(self, cfg: dict, cwd: str, sub_path: str, labels: dict, timeout: int) -> None:
                self.cfg, self.cwd, self.sub_path, self.labels, self.timeout = cfg, cwd, sub_path, labels, timeout
                self.mount = cfg.get("workspace_mount") or "/workspace"
                self.root = os.path.realpath(os.path.join(cluster.workspace_root, sub_path))
                self.commands: list[str] = []
                self.closed = False
                cluster.pods.append(self)

            def real(self, path: str) -> str | None:
                p = os.path.normpath(path)
                if p != self.mount and not p.startswith(self.mount + "/"):
                    return None  # le pod ne voit rien hors du montage de SA session
                full = os.path.realpath(os.path.join(self.root, p[len(self.mount):].lstrip("/")))
                return full if full == self.root or full.startswith(self.root + os.sep) else None

            def execute(self, command: str, timeout: int = 120, stdin_data: str | None = None,
                        bounded_capture: bool = False, **_: Any) -> dict[str, Any]:
                self.commands.append(command)
                return {"output": f"ran:{command}", "returncode": 0}

            def cleanup(self) -> None:
                self.closed = True

        return FakeEnvironment

    def module(self) -> types.ModuleType:
        """Le module `nokker.tools.k8s_sandbox` tel que convenu : la classe et le ménage par instance."""
        mod = types.ModuleType("nokker.tools.k8s_sandbox")
        mod.KubernetesEnvironment = self.factory()  # type: ignore[attr-defined]

        def reap_k8s_sandboxes(instance: str) -> int:
            self.reaped.append(instance)
            return 2

        mod.reap_k8s_sandboxes = reap_k8s_sandboxes  # type: ignore[attr-defined]
        return mod


@pytest.fixture
def cluster(tmp_path, monkeypatch):
    root = tmp_path / "workspaces"
    root.mkdir()
    c = FakeCluster(workspace_root=str(root))
    monkeypatch.setitem(sys.modules, "nokker.tools.k8s_sandbox", c.module())
    return c


class MemorySecrets:
    """Fournisseur de coffre de portée `system` en mémoire (get, set, delete, list) — jamais de valeur au repr."""

    kind, scope = "memory", "system"

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def __repr__(self) -> str:
        return f"MemorySecrets({len(self.values)})"

    def get(self, name: str) -> str | None:
        return self.values.get(name)

    def set(self, name: str, value: str) -> Any:
        from nokker.secrets.base import SecretInfo

        self.values[name] = value
        return SecretInfo(name=name, updated_at=None)

    def delete(self, name: str) -> bool:
        return self.values.pop(name, None) is not None

    def list(self) -> list[Any]:
        from nokker.secrets.base import SecretInfo

        return [SecretInfo(name=n, updated_at=None) for n in sorted(self.values)]


def code_block(workspace_root: str, /, **over: Any) -> dict[str, Any]:
    """Le bloc `code` du contrat (backend kubernetes, faux module installé par la fixture `cluster`)."""
    block: dict[str, Any] = {
        "enabled": True, "backend": "kubernetes", "workspace_root": workspace_root, "layout": "tenant/user/session",
        "max_sessions_per_user": 3, "idle_ttl_s": 1800, "instance": "nokker-test",
        "forge": {"allowed_hosts": ["git.example.org"]},
        "kubernetes": {"namespace": "", "image": "sandbox:1", "pvc": "workspaces",
                       "resources": {"cpu": "1", "memory": "1Gi"}, "exec_timeout_s": 120, "network": "deny"},
    }
    block.update(over)
    return block
