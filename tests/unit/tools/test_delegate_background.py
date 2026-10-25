"""Sous-agents en arrière-plan (nokker/tools/delegate.py, `delegate(..., background=true)`).

Le parent reprend la main tout de suite avec des ids ; les enfants tournent sur leurs worktrees, chacun sur son fil
`<parent>/sa-<id>` (son propre verrou de tour) ; le parent relit plus tard (`delegate_status`), annule
(`delegate_cancel`, ou annulation du fil parent) ; mêmes portes, règles et approbations qu'au premier plan."""
from __future__ import annotations

import json
import threading
import time

from nokker.core.ports import ToolContext
from nokker.core.turn import Effect, ToolCall
from nokker.tools.delegate import DelegateToolSource
from nokker.tools.registry import Registry
from nokker.tools.select import relevant_tools
from tests.unit.tools.test_delegate import FakeCode, Factory, Src, ctx, git_fake, names, turn


def bg_source(factory, code=None, **kw):
    code = code if code is not None else FakeCode("/nonexistent")
    kw.setdefault("background", True)
    src = DelegateToolSource(factory, code, **kw)
    base = Src("web_search", "clarify", "skill_view")
    reg = Registry(sources=[base, code, src])
    src.bind(reg)
    return src, base, code


def start(src, tasks, isolate=False, c=None):
    r = src.call("delegate", {"tasks": tasks, "isolate": isolate, "background": True}, c or ctx())
    return r, (json.loads(r.content) if not r.is_error else r.content)


def status(src, c=None, **a):
    r = src.call("delegate_status", a, c or ctx())
    assert not r.is_error, r.content
    return json.loads(r.content)


def gate():
    """Un comportement d'enfant qui attend `release` : l'enfant reste « en cours » tant que le test le veut."""
    release = threading.Event()
    running = threading.Event()

    def behave(kernel, inp):
        running.set()
        release.wait(5)
        return turn("fini en fond")

    return release, running, behave


def test_specs_seulement_quand_l_arriere_plan_est_permis():
    off = DelegateToolSource(Factory(), None)
    assert [s.name for s in off.specs()] == ["delegate"]
    assert "background" not in off.specs()[0].parameters["properties"]
    on = DelegateToolSource(Factory(), None, background=True)
    specs = {s.name: s for s in on.specs()}
    assert set(specs) == {"delegate", "delegate_status", "delegate_cancel"}
    assert specs["delegate"].parameters["properties"]["background"]["type"] == "boolean"
    assert specs["delegate_status"].effect is Effect.READ and specs["delegate_status"].fresh
    assert specs["delegate_cancel"].effect is Effect.READ and specs["delegate_cancel"].fresh  # comme process_stop
    # même effet que le premier plan : WRITE seulement confiné à un worktree jetable
    assert specs["delegate"].effect_for({"tasks": [], "background": True}) is Effect.DESTRUCTIVE
    fam = relevant_tools("lance des sous-agents", set(), list(specs.values()))
    assert {s.name for s in fam} == set(specs)


def test_refus_sans_arriere_plan():
    src = DelegateToolSource(Factory(), FakeCode("/x"))
    src.bind(Registry(sources=[src]))
    r = src.call("delegate", {"tasks": [{"goal": "g"}], "background": True}, ctx())
    assert r.is_error and "arrière-plan" in r.content
    assert src.call("delegate_status", {}, ctx()).is_error


def test_rend_la_main_tout_de_suite_puis_resultat_relu():
    release, running, behave = gate()
    f = Factory(behave=behave)
    events = []
    src, *_ = bg_source(f, emit=lambda n, p: events.append((n, p)))
    t0 = time.monotonic()
    r, out = start(src, [{"goal": "corrige a"}, {"goal": "corrige b"}])
    assert not r.is_error and time.monotonic() - t0 < 1  # pas d'attente des enfants
    assert out["background"] is True and len(out["children"]) == 2
    ids = [c["child_id"] for c in out["children"]]
    assert [c["thread_id"] for c in out["children"]] == [f"t1/sa-{i}" for i in ids]
    assert all(c["status"] in ("queued", "running") for c in out["children"])
    assert "delegate_status" in out["note"]
    running.wait(2)
    now = status(src)
    assert {c["child_id"] for c in now["children"]} == set(ids) and now["running"] == 2
    assert all(c["status"] in ("queued", "running") and c["goal"] for c in now["children"])
    release.set()
    done = status(src, wait_s=5)
    assert done["running"] == 0 and [c["status"] for c in done["children"]] == ["completed", "completed"]
    assert done["children"][0]["summary"] == "fini en fond" and done["children"][0]["tokens"] == 15
    started = [p for n, p in events if n == "subagent.started"]
    finished = [p for n, p in events if n == "subagent.finished"]
    assert len(started) == 2 and all(p["background"] is True for p in started)
    assert len(finished) == 2 and all(p["background"] is True and p["exit_reason"] == "completed"
                                      for p in finished)


def test_chaque_enfant_a_son_fil_et_les_memes_filtres():
    release, running, behave = gate()
    f = Factory(behave=behave)
    src, *_ = bg_source(f)
    start(src, [{"goal": "g"}])
    running.wait(2)
    assert f.threads[0].startswith("t1/sa-") and f.kernels[0].inputs[0].channel == "subagent"
    got = names(f.registries[0])
    assert "clarify" not in got and "delegate" not in got and "delegate_status" not in got
    release.set()
    status(src, wait_s=5)


def test_le_parent_est_prevenu_au_tour_suivant():
    notes = []
    src, *_ = bg_source(Factory(["ok"]), note=lambda tid, line: notes.append((tid, line)))
    _, out = start(src, [{"goal": "g"}])
    cid = out["children"][0]["child_id"]
    status(src, wait_s=5)
    deadline = time.monotonic() + 2
    while not notes and time.monotonic() < deadline:
        time.sleep(0.01)
    assert notes and notes[0][0] == "t1" and cid in notes[0][1] and "delegate_status" in notes[0][1]


def test_lecture_par_ids_et_fils_etrangers():
    src, *_ = bg_source(Factory(["ok"]))
    _, a = start(src, [{"goal": "a"}])
    _, b = start(src, [{"goal": "b"}], c=ctx("t2"))
    ida, idb = a["children"][0]["child_id"], b["children"][0]["child_id"]
    mine = status(src, ids=[ida, idb], wait_s=5)
    assert [c["child_id"] for c in mine["children"]] == [ida] and mine["unknown"] == [idb]
    assert [c["child_id"] for c in status(src, c=ctx("t2"), wait_s=5)["children"]] == [idb]
    assert src.call("delegate_status", {}, ctx("t1/sa-xyz")).is_error  # un enfant ne lit pas ses frères


def test_annulation_par_l_outil():
    release, running, behave = gate()
    f = Factory(behave=behave)
    src, *_ = bg_source(f)
    _, out = start(src, [{"goal": "long"}])
    cid = out["children"][0]["child_id"]
    running.wait(2)
    r = src.call("delegate_cancel", {"ids": [cid]}, ctx())
    assert not r.is_error and json.loads(r.content)["cancelled"] == [cid]
    res = status(src, wait_s=5)["children"][0]
    assert res["status"] == "interrupted" and res["exit_reason"] == "cancelled"
    # le noyau de l'enfant a reçu l'annulation de SON fil ; ses outils refusent tout
    assert set(f.kernels[0].cancelled) == {f.threads[0]}
    c = ToolContext(thread_id=f.threads[0], turn_index=0, channel="subagent")
    assert f.registries[0].call("web_search", {}, c).is_error
    release.set()
    time.sleep(0.1)
    assert status(src)["children"][0]["exit_reason"] == "cancelled"  # le retour tardif n'écrase pas l'annulation
    assert json.loads(src.call("delegate_cancel", {"ids": [cid]}, ctx()).content)["cancelled"] == []


def test_annulation_du_fil_parent_arrete_ses_enfants():
    release, running, behave = gate()
    f = Factory(behave=behave)
    src, *_ = bg_source(f)
    start(src, [{"goal": "a"}, {"goal": "b"}])
    _, other = start(src, [{"goal": "c"}], c=ctx("t2"))
    running.wait(2)
    assert src.cancel_children("t1") == 2
    assert all(c["exit_reason"] == "cancelled" for c in status(src, wait_s=5)["children"])
    assert status(src, c=ctx("t2"))["running"] == 1  # l'autre fil continue
    one = other["children"][0]
    assert src.cancel_children(one["thread_id"]) == 1  # un fil d'enfant : cet enfant seulement
    release.set()


def test_annulation_pendant_l_attente_au_premier_plan():
    """Le fil parent annulé pendant un delegate au premier plan : l'appel rend la main (avant : il attendait)."""
    release, running, behave = gate()
    src, *_ = bg_source(Factory(behave=behave))
    box = {}
    th = threading.Thread(target=lambda: box.update(r=src.call("delegate", {"tasks": [{"goal": "g"}],
                                                                            "isolate": False}, ctx())))
    th.start()
    running.wait(2)
    assert src.cancel_children("t1") == 1
    th.join(3)
    assert not th.is_alive()
    assert json.loads(box["r"].content)[0]["exit_reason"] == "cancelled"
    release.set()


def test_plafond_de_concurrence_partage_et_plafond_d_attente():
    cur, peak, lock = [0], [0], threading.Lock()

    def behave(kernel, inp):
        with lock:
            cur[0] += 1
            peak[0] = max(peak[0], cur[0])
        time.sleep(0.1)
        with lock:
            cur[0] -= 1
        return turn()

    src, *_ = bg_source(Factory(behave=behave), max_children=2, max_background=4)
    _, out = start(src, [{"goal": str(i)} for i in range(4)])
    assert len(out["children"]) == 4
    r, msg = start(src, [{"goal": "de trop"}])
    assert r.is_error and "4" in msg  # au plus 4 enfants de fond non terminés
    assert [c["status"] for c in status(src, wait_s=5)["children"]] == ["completed"] * 4
    assert peak[0] == 2
    _, again = start(src, [{"goal": "après"}])  # les terminés ne comptent plus
    assert len(again["children"]) == 1
    status(src, wait_s=5)


def test_delai_d_inactivite_en_fond():
    release, running, behave = gate()
    src, *_ = bg_source(Factory(behave=behave), idle_timeout_s=0.2)
    start(src, [{"goal": "g"}])
    res = status(src, wait_s=5)["children"][0]
    assert res["status"] == "interrupted" and res["exit_reason"] == "idle_timeout"
    release.set()


def test_carte_d_approbation_en_fond():
    def behave(kernel, inp):
        return turn("j'attends", pending_approvals=[ToolCall("1", "terminal", {"command": "rm x"})])

    f = Factory(behave=behave)
    src, *_ = bg_source(f)
    start(src, [{"goal": "g"}])
    res = status(src, wait_s=5)["children"][0]
    assert res["status"] == "interrupted" and res["exit_reason"] == "approval_pending"
    assert f.threads[0] in res["summary"]


def test_worktree_finalise_en_fond(tmp_path):
    (tmp_path / ".git").mkdir()
    st = {"commits": 1}
    code = FakeCode(str(tmp_path))
    code.run_impl = git_fake(st)
    release, running, behave = gate()
    src, *_ = bg_source(Factory(behave=behave), code=code)
    _, out = start(src, [{"goal": "g"}], isolate=True)
    cid = out["children"][0]["child_id"]
    assert out["children"][0]["worktree"] == {"path": f"/workspace/.nokker/worktrees/{cid}",
                                              "branch": f"nokker/sa-{cid}"}
    release.set()
    res = status(src, wait_s=5)["children"][0]
    assert res["worktree"]["kept"] and res["worktree"]["commits"] == 1
    assert code.scopes and code.scopes[0].closed


def test_retour_relu_au_tour_suivant_meme_apres_delai_de_lecture():
    src, *_ = bg_source(Factory(["ok"]))
    start(src, [{"goal": "g"}])
    status(src, wait_s=5)
    again = status(src)
    assert again["children"][0]["status"] == "completed"  # relisible, pas consommé


def test_wait_borne():
    release, running, behave = gate()
    src, *_ = bg_source(Factory(behave=behave))
    start(src, [{"goal": "g"}])
    t0 = time.monotonic()
    out = status(src, wait_s=0.2)
    assert out["running"] == 1 and time.monotonic() - t0 < 2
    assert src.call("delegate_status", {"wait_s": "x"}, ctx()).is_error
    release.set()
    status(src, wait_s=5)
