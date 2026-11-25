"""Bureau B2a (docs/DESKTOP.md §12) : en mode session, chaque action ouvre un PAS validé à l'écran (bulle `pointer` à
cible desktop) ; Vas-y / Je le fais moi-même / Arrête ; au-delà de wait_s, `desktop_pending` puis tour de suite par le
suivi de P7b repris tel quel (`PhoneFollow`)."""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from nokker.bubbles.hub import BubbleForbidden, BubbleHub
from nokker.bubbles.memory import InMemoryBubbles
from nokker.cloud.phone_tools import PhoneFollow
from nokker.core.ports import ToolContext
from nokker.core.turn import Effect, ToolCall
from nokker.desktop import texts as T
from nokker.desktop.node import Executor, Jobs, NodeError
from nokker.desktop.service import DesktopNode, DesktopToolSource, build_desktop
from nokker.desktop.steps import configure_follow, continuation_for
from nokker.qa.permissions import LocalPermissions, Mode
from nokker.threads.memory import InMemoryThreads

from .fake_ad import FakeAD


def _session(tmp_path, wait_s=2):
    svc = build_desktop({"enabled": True, "mode": "session", "apps": {"session": ["gnome-text-editor", "python3"]},
                         "wait_s": wait_s}, tmp_path / "st")
    node = DesktopNode("dk_s", "poste", "session", "spki", "fp", "0" * 64, 1.0, beat_at=time.time(),
                       health={"desktop": "on"})
    svc._nodes[node.node_id] = node
    fake = FakeAD()
    ex = Executor(ad=__import__("nokker.desktop.node", fromlist=["AgentDesktop"]).AgentDesktop(runner=fake),
                  jobs=Jobs(root=tmp_path / "run"), mode="session", sleep=lambda s: time.sleep(0.05))
    ex.jobs.sweep_all()
    stop = threading.Event()

    def worker():  # le nœud : relève la file du service et exécute (comme NodeClient, sans HTTP)
        while not stop.is_set():
            for t in svc.poll(node, 0.2):
                try:
                    res = {"status": "done", "result": ex.handle(t["op"], t["args"], t["policy"])}
                except NodeError as e:
                    res = {"status": "failed", "error": {"code": e.code, "detail": {k: str(v) for k, v in
                                                                                    e.detail.items()}}}
                svc.result(node, t["task_id"], res, 10)

    threading.Thread(target=worker, daemon=True).start()
    hub = BubbleHub(threads=InMemoryThreads(), bubbles=InMemoryBubbles(), publish=lambda *a: None,
                    spawn=lambda fn: fn())
    hub.desktop_steps = svc.steps
    svc.steps.open_bubble = hub.desktop_step_opened
    svc.steps.update_bubble = hub.desktop_step_update
    th = hub.create_thread("Bureau", "alice")
    return SimpleNamespace(svc=svc, node=node, fake=fake, ex=ex, stop=stop, hub=hub, th=th,
                           src=DesktopToolSource(svc), ctx=ToolContext(thread_id=th.id, turn_index=0, channel="cli",
                                                                       sender="alice"))


@pytest.fixture()
def w(tmp_path):
    world = _session(tmp_path)
    yield world
    world.stop.set()
    configure_follow(None)


def _snapshot(w):
    res = w.src.call(T.LOOK_TOOL, {"what": "snapshot", "window": "w-gtk4"}, w.ctx)  # lecture : pas de pas
    assert not res.is_error, res.content
    return res.content.split("snap_id ", 1)[1].split(" ", 1)[0]


def _type_args(snap):
    return {"action": "type", "window": "w-gtk4", "app": "python3", "snap_id": snap, "ref": "e1",
            "expect_role": "textfield", "expect_name": "Utilisateur", "text": "carol"}


def _bubble(w):
    for _ in range(100):
        bs = [b for b in w.hub.bubbles.list_thread(w.th.id) if b.kind == "pointer" and b.closed_at is None]
        if bs:
            return bs[-1]
        time.sleep(0.02)
    raise AssertionError("aucune bulle de pas")


def _answer_soon(w, action, user="alice"):
    out = {}

    def go():
        b = _bubble(w)
        out["bubble"] = b
        try:
            out["r"] = w.hub.action(b.id, user, action)
        except Exception as e:  # noqa: BLE001 — le test lit l'erreur
            out["err"] = e

    t = threading.Thread(target=go, daemon=True)
    t.start()
    return out, t


def test_session_description_and_reads_have_no_step(w):
    specs = {s.name: s for s in w.src.specs()}
    assert specs[T.ACT_TOOL].description == T.ACT_DESCRIPTION + T.ACT_SESSION_SUFFIX
    _snapshot(w)
    assert not [b for b in w.hub.bubbles.list_thread(w.th.id) if b.kind == "pointer"]


def test_go_runs_this_step_only(w):
    snap = _snapshot(w)
    out, t = _answer_soon(w, "go")
    res = w.src.call(T.ACT_TOOL, _type_args(snap), w.ctx)
    t.join(5)
    assert not res.is_error and res.content.startswith(T.STEP_DONE + "fait : type sur textfield")
    b = out["bubble"]
    assert b.state["target"]["kind"] == "desktop" and b.state["target"]["preview"] == "carol"
    assert [a["id"] for a in b.actions] == ["go", "self", "stop"] and b.title == "Étape 1"
    assert w.hub.bubbles.get(b.id).closed_at is not None
    assert w.fake.argvs("type")  # le geste n'est parti qu'après « Vas-y »


def test_nothing_runs_before_the_answer_and_stop_runs_nothing(w):
    snap = _snapshot(w)
    out, t = _answer_soon(w, "stop")
    res = w.src.call(T.ACT_TOOL, _type_args(snap), w.ctx)
    t.join(5)
    assert res.is_error and "La personne a arrêté le pas" in res.content
    assert not w.fake.argvs("type")


def test_self_observes_only_the_target(w):
    w.svc.steps.observe_s = 1
    snap = _snapshot(w)
    out, t = _answer_soon(w, "self")
    res = w.src.call(T.ACT_TOOL, _type_args(snap), w.ctx)
    t.join(5)
    assert "La personne devait faire elle-même" in res.content  # rien n'a changé (faux bureau figé)
    assert not w.fake.argvs("type")
    finds = w.fake.argvs("find")
    gets = w.fake.argvs("get")
    assert finds and all("--name" in a and a[a.index("--name") + 1] == "Utilisateur" and "--exact" in a
                         for a in finds)  # la cible seule, par rôle et nom exacts
    assert all(a[2].endswith(":e1") for a in gets)  # la valeur de la cible seule, jamais d'un autre champ
    assert not any(a[1] in ("wait", "snapshot") for a, _s, _h in w.fake.calls[-len(finds) - len(gets):])


def test_self_never_reads_a_secret_target(w):
    w.svc.steps.observe_s = 1
    snap = _snapshot(w)
    out, t = _answer_soon(w, "self")
    args = dict(_type_args(snap), ref="e2", expect_name="Mot de passe")
    w.src.call(T.ACT_TOOL, args, w.ctx)
    t.join(5)
    assert not [a for a in w.fake.argvs("get") if a[2].endswith(":e2")]


def test_non_member_cannot_answer(w):
    snap = _snapshot(w)
    out, t = _answer_soon(w, "go", user="mallory")
    w.svc.steps.ttl_s = 1  # personne d'autre ne répondra : le pas expire
    res = w.src.call(T.ACT_TOOL, _type_args(snap), w.ctx)
    t.join(5)
    from nokker.bubbles.types import BubbleNotFound

    assert isinstance(out.get("err"), (BubbleNotFound, BubbleForbidden)) and "r" not in out
    assert not w.fake.argvs("type")
    assert res.content.startswith("desktop_pending") or "n'a pas été validé" in res.content


def test_pending_then_continuation_turn(w):
    launched = []
    configure_follow(PhoneFollow(phone=w.svc.steps, launch=lambda *a: launched.append(a), tasks_field="desktop_tasks",
                                 continue_with=continuation_for(w.src._format), thread_name="desktop-follow",
                                 poll_s=0.05))
    from nokker.desktop.steps import follow

    w.svc.wait_s = 0.3
    snap = _snapshot(w)
    res = w.src.call(T.ACT_TOOL, _type_args(snap), w.ctx)
    assert res.content.startswith("desktop_pending : le pas « saisie » sur textfield « Utilisateur »")
    fields = follow().message_fields(w.th.id, 0)
    assert fields["desktop_tasks"][0]["status"] == "pending" and "phone_tasks" not in fields
    w.hub.action(_bubble(w).id, "alice", "go")
    for _ in range(200):
        if launched:
            break
        time.sleep(0.02)
    thread_id, sender, channel, text, attached = launched[0]
    assert thread_id == w.th.id and sender == "alice"
    assert text.startswith("[Bureau] Validé par la personne à l'écran ; fait : type")


def test_expired_step_continuation_says_nothing_was_done(w):
    launched = []
    configure_follow(PhoneFollow(phone=w.svc.steps, launch=lambda *a: launched.append(a), tasks_field="desktop_tasks",
                                 continue_with=continuation_for(w.src._format), poll_s=0.05))
    w.svc.wait_s, w.svc.steps.ttl_s = 0.2, 0.5
    w.src.call(T.ACT_TOOL, _type_args(_snapshot(w)), w.ctx)
    for _ in range(200):
        if launched:
            break
        time.sleep(0.02)
    assert launched[0][3] == "[Bureau] " + T.STEP_EXPIRED.format(action="saisie", target="textfield « Utilisateur »")
    assert not w.fake.argvs("type")


def test_one_waiting_step_per_conversation(w):
    w.svc.wait_s = 0.2
    snap = _snapshot(w)
    first = w.src.call(T.ACT_TOOL, _type_args(snap), w.ctx)
    assert first.content.startswith("desktop_pending")
    second = w.src.call(T.ACT_TOOL, _type_args(snap), w.ctx)
    assert second.is_error and "un pas attend déjà la personne" in second.content


def test_answer_is_bound_to_the_step_it_names(w):
    w.svc.wait_s = 0.2
    snap = _snapshot(w)
    w.src.call(T.ACT_TOOL, _type_args(snap), w.ctx)
    step = next(iter(w.svc.steps._steps.values()))
    step.args["text"] = "autre chose"  # l'appel montré n'est plus celui qui partirait : refus
    assert w.svc.steps.answer(step.step_id, "alice", "go") == {"accepted": False, "status": "failed"}
    assert not w.fake.argvs("type")
    with pytest.raises(LookupError):
        w.svc.steps.answer("ds_inconnu", "alice", "go")


def test_session_steps_replace_the_write_card_but_not_send_or_delete():
    perms = LocalPermissions(mode=Mode.DEFAULT)
    write = ToolCall(id="c", name="desktop_act", arguments={"action": "type", "app": "a", "window": "w"})
    assert perms.decide(write, Effect.WRITE, "th")[0].value == "hitl"  # sans pas : carte
    perms.desktop_steps = lambda: True
    assert perms.decide(write, Effect.WRITE, "th")[0].value == "allow"  # le pas validé à l'écran remplace la carte
    delete = ToolCall(id="d", name="desktop_act", arguments={"action": "click", "app": "a", "window": "w",
                                                             "expect_role": "button", "expect_name": "Supprimer"})
    assert perms.decide(delete, Effect.DESTRUCTIVE, "th")[0] is None  # les invariants posent la carte


def test_virtual_mode_has_no_steps(tmp_path):
    svc = build_desktop({"enabled": True}, tmp_path / "st")
    assert svc.steps is None
    svc._nodes["dk_v"] = SimpleNamespace(mode="virtual", node_id="dk_v")
    act = {s.name: s for s in DesktopToolSource(svc).specs()}[T.ACT_TOOL]
    assert act.description == T.ACT_DESCRIPTION


def test_phone_follow_defaults_are_unchanged():
    f = PhoneFollow(phone=None)
    assert f.tasks_field == "phone_tasks" and f.continue_with is None and f.thread_name == "phone-follow"


def _typing_elsewhere(w, target_changes=False):
    """Pendant l'observation, la personne tape dans UN AUTRE champ (e2, mot de passe) ; la cible e1 change ou non."""
    calls = {"n": 0}
    real = w.fake.run

    def run(cmd, a, headed, stdin):
        if cmd == "get":
            calls["n"] += 1
            ref = a[0]
            if ref.endswith(":e2"):
                return {"ok": True, "data": {"value": "s3cr3t-tapé"}}
            return {"ok": True, "data": {"value": "carol" if target_changes and calls["n"] > 2 else ""}}
        return real(cmd, a, headed, stdin)

    w.fake.run = run


def test_typing_in_another_field_leaves_no_trace(w):
    w.svc.steps.observe_s = 1
    _typing_elsewhere(w)
    snap = _snapshot(w)
    out, t = _answer_soon(w, "self")
    res = w.src.call(T.ACT_TOOL, _type_args(snap), w.ctx)
    t.join(5)
    assert "aucun changement vu" in res.content
    assert not [a for a in w.fake.argvs("get") if not a[2].endswith(":e1")]  # l'autre champ n'est jamais lu
    step = next(iter(w.svc.steps._steps.values()))
    assert step.result == {"changed": False, "by": "self"}  # rien d'autre n'est gardé
    dump = repr(w.svc.steps._steps) + repr(w.svc._tasks) + repr(w.svc.view())
    assert "s3cr3t" not in dump and "carol" not in dump.replace("'text': 'carol'", "")


def test_a_change_of_the_target_is_seen(w):
    w.svc.steps.observe_s = 2
    _typing_elsewhere(w, target_changes=True)
    snap = _snapshot(w)
    out, t = _answer_soon(w, "self")
    res = w.src.call(T.ACT_TOOL, _type_args(snap), w.ctx)
    t.join(5)
    assert "un changement est vu sur cet élément, pas sa valeur" in res.content
    task = [x for x in w.svc._tasks.values() if x.op == "desktop.observe"][-1]
    assert task.result == {"changed": True}  # ni valeur ni empreinte ne quittent le nœud


# ───────────── expect_name rempli par le noyau depuis le snapshot (S5) ─────────────
@pytest.mark.parametrize("given", ['"', "", "  ", "…", "?!", None])
def test_expect_name_without_letter_is_filled_from_the_snapshot_before_the_card(w, given):
    snap = _snapshot(w)
    args = _type_args(snap)
    args.pop("expect_name") if given is None else args.update(expect_name=given)
    out, t = _answer_soon(w, "go")
    res = w.src.call(T.ACT_TOOL, args, w.ctx)
    t.join(5)
    assert not res.is_error and res.content.startswith(T.STEP_DONE + "fait : type sur textfield"), res.content
    b = out["bubble"]
    assert b.state["target"]["name"] == "Utilisateur"  # la personne voit le VRAI nom sur la carte
    assert "« Utilisateur »" in b.state["caption"]
    step = next(iter(w.svc.steps._steps.values()))
    assert step.args["expect_name"] == "Utilisateur"
    assert w.fake.argvs("type")


def test_a_wrong_but_meaningful_expect_name_is_still_refused(w):
    snap = _snapshot(w)
    out, t = _answer_soon(w, "go")
    res = w.src.call(T.ACT_TOOL, dict(_type_args(snap), expect_name="Supprimer"), w.ctx)
    t.join(5)
    assert res.is_error and not w.fake.argvs("type")
    assert out["bubble"].state["target"]["name"] == "Supprimer"  # rien n'est écrasé : le nom du modèle reste


def test_unknown_ref_is_refused_as_before(w):
    snap = _snapshot(w)
    out, t = _answer_soon(w, "go")
    res = w.src.call(T.ACT_TOOL, dict(_type_args(snap), ref="e99", expect_name='"'), w.ctx)
    t.join(5)
    assert res.is_error and not w.fake.argvs("type")
    assert out["bubble"].state["target"]["name"] == ""  # pas de nom inventé ; ponctuation jamais comparée telle quelle


def test_fill_never_ranks_the_call_higher_than_the_gate_saw():
    from nokker.desktop.service import DesktopToolSource

    src = DesktopToolSource(SimpleNamespace())
    src._seen[("j", "s1")] = {"e1": ("button", "Supprimer"), "e2": ("button", "Connexion")}
    a = {"action": "click", "snap_id": "s1", "ref": "e1", "expect_role": "button", "expect_name": '"'}
    src._fill_expect_name("j", a)
    assert a["expect_name"] == ""  # la porte a vu une écriture/envoi, le vrai nom détruit : pas de remplissage
    b = {"action": "click", "snap_id": "s1", "ref": "e2", "expect_role": "button", "expect_name": '"'}
    src._fill_expect_name("j", b)
    assert b["expect_name"] == "Connexion"


# ───────────── carte générique : `expect_name` normalisé AVANT la pose (crochet `normalize_args`) ─────────────
def _act_spec(w):
    return next(s for s in w.src.specs() if s.name == T.ACT_TOOL)


def test_generic_card_shows_the_real_name_not_the_one_the_model_gave(w):
    from nokker.approvals.crypto import args_sha256

    snap = _snapshot(w)
    spec = _act_spec(w)
    given = dict(_type_args(snap), expect_name='"')
    out = spec.normalized(w.ctx.thread_id, given)
    assert out["expect_name"] == "Utilisateur"
    assert given["expect_name"] == '"'  # l'original n'est pas muté : seule la copie normalisée va sur la carte
    assert args_sha256(out) == args_sha256(spec.normalized(w.ctx.thread_id, dict(out)))  # idempotent : ce qui est signé = ce qui s'exécute
    assert args_sha256(out) != args_sha256(given)


def test_normalization_leaves_unknown_elements_and_meaningful_names_alone(w):
    snap = _snapshot(w)
    spec = _act_spec(w)
    unknown = dict(_type_args(snap), ref="e99", expect_name='"')
    assert spec.normalized(w.ctx.thread_id, unknown) == dict(unknown, expect_name="")  # ponctuation jamais gardée ; rien inventé
    wrong = dict(_type_args(snap), expect_name="Supprimer")
    assert spec.normalized(w.ctx.thread_id, wrong) == wrong  # un vrai nom du modèle reste : le garde du nœud le refuse
    other_thread = spec.normalized("autre-fil", dict(_type_args(snap), expect_name='"'))
    assert other_thread["expect_name"] == ""  # snapshot d'un autre fil : inconnu


def test_only_desktop_act_has_the_hook(w):
    assert {s.name for s in w.src.specs() if s.normalize_args is not None} == {T.ACT_TOOL}
