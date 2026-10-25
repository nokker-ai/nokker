"""Sous-agents et redémarrage du noyau (nokker/tools/delegate.py, docs/LOKKAL.md §7.22.9) : l'état utile est gardé dans
l'état local de la machine (`state_path`). Au redémarrage, un enfant non fini est `interrupted / restart`, son
worktree conservé, et `delegate_status` du parent le liste ; une carte d'enfant en attente reste refusée proprement —
jamais exécutée avec le noyau du parent. Reprendre un enfant après redémarrage n'est pas fait."""
from __future__ import annotations

import json
import threading

from nokker.tools.delegate import DelegateToolSource
from nokker.tools.registry import Registry
from tests.unit.tools.test_delegate import FakeCode, Factory, Src, ctx, git_fake, turn
from tests.unit.tools.test_delegate_cards import approve, world


def _source(factory, code, path, **kw):
    src = DelegateToolSource(factory, code, background=True, state_path=path, **kw)
    src.bind(Registry(sources=[Src("web_search"), code, src]))
    return src


def _status(src, tid="t1", **a):
    r = src.call("delegate_status", a, ctx(tid))
    assert not r.is_error, r.content
    return json.loads(r.content)


def test_unfinished_background_children_are_interrupted_by_restart_with_their_worktree(tmp_path):
    (tmp_path / ".git").mkdir()
    code = FakeCode(str(tmp_path))
    code.run_impl = git_fake({})
    release = threading.Event()

    def behave(kernel, inp):
        release.wait(5)
        return turn("fini")

    path = tmp_path / "state" / "subagents.json"
    before = _source(Factory(behave=behave), code, path)
    out = json.loads(before.call("delegate", {"tasks": [{"goal": "corrige a"}, {"goal": "corrige b"}],
                                              "background": True}, ctx("t1")).content)
    ids = [c["child_id"] for c in out["children"]]
    assert path.is_file()  # gardé dès le lancement, pas seulement à la fin
    # le noyau redémarre : une source neuve, même état local (l'ancienne ne tourne plus pour personne)
    after = _source(Factory(), FakeCode(str(tmp_path)), path)
    st = _status(after)
    assert [c["child_id"] for c in st["children"]] == ids and st["running"] == 0 and st["finished"] == 2
    for c in st["children"]:
        assert (c["status"], c["exit_reason"]) == ("interrupted", "restart")
        assert c["worktree"]["kept"] is True and c["worktree"]["path"].endswith(c["child_id"])
        assert c["worktree"]["branch"] == f"nokker/sa-{c['child_id']}"
        assert "redémarrage" in c["summary"] and c["goal"].startswith("corrige")
    assert _status(after, "autre")["children"] == []  # seulement les enfants du fil appelant
    # un second redémarrage ne change rien (l'état a été réécrit « interrompu »)
    again = _source(Factory(), FakeCode(str(tmp_path)), path)
    assert [c["exit_reason"] for c in _status(again)["children"]] == ["restart", "restart"]
    release.set()


def test_finished_results_stay_readable_after_restart(tmp_path):
    path = tmp_path / "subagents.json"
    before = _source(Factory(["fait en fond"]), FakeCode("/nonexistent"), path)
    before.call("delegate", {"tasks": [{"goal": "g"}], "isolate": False, "background": True}, ctx("t1"))
    done = _status(before, wait_s=5)["children"][0]
    assert done["status"] == "completed"
    after = _source(Factory(), FakeCode("/nonexistent"), path)
    st = _status(after)["children"][0]
    assert (st["status"], st["exit_reason"], st["summary"]) == ("completed", "completed", "fait en fond")


def test_a_childs_card_after_restart_is_refused_never_run_with_the_parent(tmp_path):
    path = tmp_path / "subagents.json"
    for background in (False, True):
        parent, approvals, base, src, executor, published = world(background)
        src.state_path = path
        args = {"tasks": [{"goal": "agrandis le volume"}], "isolate": False, **({"background": True} if background
                                                                              else {})}
        r = src.call("delegate", args, ctx("t1"))
        assert not r.is_error, r.content
        child = (_status(src, wait_s=5)["children"][0] if background else json.loads(r.content)[0])
        assert child["exit_reason"] == "approval_pending"
        card = approve(approvals)
        restarted = DelegateToolSource(src.kernel_factory, None, background=True, state_path=path)
        executor.subagent_kernel = restarted.kernel_for_card
        assert restarted.kernel_for_card(card.thread_id) is None
        assert executor.run(card) is None  # rien n'est exécuté
        a = approvals.get(card.id)
        assert a.status == "failed" and "sous-agent" in (a.error or "") and base.calls == []
        assert published[-1][2] is True
        listed = {c["child_id"]: c for c in _status(restarted)["children"]}
        mine = listed[child["child_id"]]
        assert (mine["status"], mine["exit_reason"]) == ("interrupted", "restart")
        assert "carte" in mine["summary"]


def test_unreadable_state_starts_empty(tmp_path):
    path = tmp_path / "subagents.json"
    path.write_text("{pas du json")
    src = _source(Factory(), FakeCode("/nonexistent"), path)
    assert _status(src)["children"] == []
    src.call("delegate", {"tasks": [{"goal": "g"}], "isolate": False, "background": True}, ctx("t1"))
    _status(src, wait_s=5)
    assert json.loads(path.read_text())  # réécrit proprement


def test_without_state_path_nothing_is_written(tmp_path):
    src = DelegateToolSource(Factory(), FakeCode("/nonexistent"), background=True)
    src.bind(Registry(sources=[src]))
    src.call("delegate", {"tasks": [{"goal": "g"}], "isolate": False, "background": True}, ctx("t1"))
    _status(src, wait_s=5)
    assert list(tmp_path.iterdir()) == []
