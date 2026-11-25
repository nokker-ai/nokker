"""`memory: "off" | "host"` d'une session : aucun rappel automatique, aucune note apprise dans la fenêtre (les consignes du
dépôt restent), outils de mémoire retirés ; valeur inconnue 400 ; défaut inchangé."""
from __future__ import annotations

import dataclasses

import pytest

from nokker.core.turn import TurnInput
from nokker.memory import session_mode
from tests.unit.api.test_server import make_kernel
from tests.unit.test_local_api import _api


@pytest.fixture(autouse=True)
def _clean():
    session_mode.registry()._by_thread.clear()
    yield
    session_mode.registry()._by_thread.clear()


def _open(api, tmp_path, **extra):
    return api.handle("POST", "/v1/local/sessions", f"Bearer {api.token}",
                      {"cwd": str(tmp_path / "projets" / "app"), **extra})


class _Project:
    def __init__(self):
        self.opened = []

    def open_session(self, thread_id, cwd, root):
        self.opened.append(thread_id)


@pytest.mark.parametrize("bad", ["on", "", "OFF", True, 1, ["off"]])
def test_unknown_value_is_400_and_opens_nothing(tmp_path, bad):
    api = _api(tmp_path)
    api.on_session_deny = lambda tid, names: None
    status, body = _open(api, tmp_path, memory=bad)
    assert status == 400 and body["error"]["code"] == "bad_memory" and api.code.sessions() == {}


def test_not_supported_without_the_deny_mechanism(tmp_path):
    api = _api(tmp_path)
    assert _open(api, tmp_path, memory="off")[0] == 400 and api.code.sessions() == {}


@pytest.mark.parametrize("mode", ["off", "host"])
def test_tools_removed_project_import_kept_mode_kept(tmp_path, mode):
    api = _api(tmp_path)
    project = api.project_memory = _Project()
    seen = []
    api.on_session_deny = lambda tid, names: seen.append(list(names))
    status, body = _open(api, tmp_path, memory=mode, deny_tools=["terminal"])
    assert status == 201 and body["memory"] == mode
    assert seen == [["terminal", *session_mode.TOOLS]] and body["unknown_denied"] == []
    assert project.opened == [body["thread_id"]] and session_mode  # les consignes du dépôt sont importées quand même
    assert session_mode.registry().mode(body["thread_id"]) == mode
    assert session_mode.registry().kernel_memory_off(body["thread_id"] + "/sa-ab12")  # sous-agents compris


def test_default_is_unchanged_and_reopen_without_the_field_clears(tmp_path):
    api = _api(tmp_path)
    project = api.project_memory = _Project()
    seen = []
    api.on_session_deny = lambda tid, names: seen.append(list(names))
    status, body = _open(api, tmp_path)
    assert status == 201 and "memory" not in body and seen == [[]] and project.opened == [body["thread_id"]]
    tid = _open(api, tmp_path, memory="off", thread_id="th_m")[1]["thread_id"]
    assert session_mode.registry().kernel_memory_off(tid)
    assert _open(api, tmp_path, thread_id=tid)[0] == 201 and not session_mode.registry().kernel_memory_off(tid)


class _SpyMemory:
    def __init__(self):
        self.recalls, self.proposals = [], []

    def recall(self, query, k=5):
        self.recalls.append(query)
        return []

    def propose(self, thread_id, facts, provenance=None):
        self.proposals.append(facts)


def test_no_recall_call_in_a_session_with_memory_off_or_host():
    kernel = make_kernel("d'accord")
    spy = kernel.memory = _SpyMemory()
    kernel = dataclasses.replace(kernel, memory=spy)
    kernel.run_turn(TurnInput(thread_id="th_normal", user_text="bonjour", sender="alice"))
    assert spy.recalls == ["bonjour"]  # sans le mode : le rappel part comme avant
    for mode in ("off", "host"):
        session_mode.registry().set(f"th_{mode}", mode)
        kernel.run_turn(TurnInput(thread_id=f"th_{mode}", user_text=f"salut {mode}", sender="alice"))
        kernel.run_turn(TurnInput(thread_id=f"th_{mode}/sa-ab12", user_text=f"enfant {mode}", sender="alice"))
    assert spy.recalls == ["bonjour"] and spy.proposals == []


def test_project_block_keeps_the_repo_instructions_but_no_learned_note(tmp_path):
    from nokker.context.engine import ContextEngineV0
    from nokker.core.turn import InputCheck, Turn
    from tests.unit.context.test_project_memory import ProjectMemoryTools, ctx, make_memory, make_repo

    repo = make_repo(tmp_path)
    (repo / "AGENTS.md").write_text("# Tests\nlance `pytest -q`\n")
    pm, _engine = make_memory(tmp_path)
    pm.open_session("th_a", repo, tmp_path)
    pm.core_block("th_a", 0, "x")
    ProjectMemoryTools(pm).call("memory_note", {"kind": "rule", "title": "Vérifier", "text": "lancer make check",
                                                "derived_from": "user"}, ctx("th_a", 0))
    engine = ContextEngineV0.__new__(ContextEngineV0)
    engine.project_memory = pm
    def turn(tid, i):
        return Turn(thread_id=tid, index=i, input=InputCheck(text="x"))

    full = engine.project_block(turn("th_a", 1))
    assert "pytest -q" in full and "lancer make check" in full  # comportement d'avant
    session_mode.registry().set("th_a", "host")
    only = engine.project_block(turn("th_a", 2))
    assert "pytest -q" in only and "Consignes du dépôt" in only  # les instructions restent
    assert "lancer make check" not in only and "Appris dans ce projet" not in only and "rappelés par" not in only
    session_mode.registry().set("th_a", None)
    assert "lancer make check" in engine.project_block(turn("th_a", 3))
