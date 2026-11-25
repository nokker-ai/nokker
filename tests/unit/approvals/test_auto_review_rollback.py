"""Point de retour PAR CARTE d'une écriture approuvée par le relecteur automatique : capture juste avant l'outil,
proposition à la révocation (`rollback: {available, checkpoint_id}`), restauration seulement sur demande explicite,
jamais par-dessus une modification ultérieure (docs/APPROVALS.md §8)."""
from __future__ import annotations

import json
import os

import pytest

from nokker.approvals.auto_review import AutoReviewer
from nokker.approvals.memory import InMemoryApprovals
from nokker.core.ports import ToolContext, ToolSpec
from nokker.core.turn import (
    Decision,
    Effect,
    Gate,
    InputCheck,
    Message,
    ToolCall,
    ToolResult,
    Turn,
    TurnInput,
    Verdict,
)
from nokker.tools.card_snapshots import CardSnapshots, RollbackUnavailable, touched_paths
from nokker.tools.checkpoints import CheckpointingToolSource


def store(tmp_path, root, **kw) -> CardSnapshots:
    return CardSnapshots(lambda _t: root, lambda _r: tmp_path / "state" / "cards", **kw)


def workspace(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "a.py").write_text("avant\n")
    return root


# ─────────────────────────── magasin ───────────────────────────

def test_restore_brings_back_old_content_and_removes_created_file(tmp_path):
    root = workspace(tmp_path)
    s = store(tmp_path, root)
    s.before("ap_1", "t", root, "patch", [str(root / "a.py"), str(root / "neuf.py")])
    (root / "a.py").write_text("après\n")
    (root / "neuf.py").write_text("créé\n")
    s.after("ap_1", root)
    offer = s.offer("ap_1", "t")
    assert offer == {"available": True, "checkpoint_id": "ap_1", "files": ["a.py", "neuf.py"], "applied": False}
    assert (root / "a.py").read_text() == "après\n"  # la proposition ne restaure rien
    done = s.apply("ap_1", "t")
    assert done == {"checkpoint_id": "ap_1", "restored": ["a.py", "neuf.py"], "conflicts": []}
    assert (root / "a.py").read_text() == "avant\n" and not (root / "neuf.py").exists()
    after = s.offer("ap_1", "t")
    assert after["available"] is False and after["applied"] is True and after["applied_at"] > 0
    assert after["restored"] == ["a.py", "neuf.py"] and after["conflicts"] == []
    with pytest.raises(RollbackUnavailable):  # une seule application
        s.apply("ap_1", "t")


def test_later_change_is_a_conflict_not_overwritten(tmp_path):
    root = workspace(tmp_path)
    s = store(tmp_path, root)
    s.before("ap_2", "t", root, "write_file", [str(root / "a.py")])
    (root / "a.py").write_text("écrit par la carte\n")
    s.after("ap_2", root)
    (root / "a.py").write_text("modifié ensuite à la main\n")
    assert s.apply("ap_2", "t") == {"checkpoint_id": "ap_2", "restored": [], "conflicts": ["a.py"]}
    assert (root / "a.py").read_text() == "modifié ensuite à la main\n"


def test_outside_workspace_symlink_and_size_make_offer_unavailable(tmp_path):
    root = workspace(tmp_path)
    (tmp_path / "dehors.txt").write_text("x")
    os.symlink(tmp_path / "dehors.txt", root / "lien.txt")
    (root / "gros.bin").write_bytes(b"0" * 2048)
    s = store(tmp_path, root, max_file_bytes=1024)
    for i, target in enumerate([str(root / ".." / "dehors.txt"), str(root / "lien.txt"), str(root / "gros.bin")]):
        s.before(f"ap_x{i}", "t", root, "write_file", [target])
        s.after(f"ap_x{i}", root)
        offer = s.offer(f"ap_x{i}", "t")
        assert offer["available"] is False and offer["reason"]
        with pytest.raises(RollbackUnavailable):
            s.apply(f"ap_x{i}", "t")
    assert (tmp_path / "dehors.txt").read_text() == "x"


def test_offer_only_for_the_right_thread_and_root(tmp_path):
    root = workspace(tmp_path)
    s = store(tmp_path, root)
    s.before("ap_3", "t", root, "write_file", [str(root / "a.py")])
    s.after("ap_3", root)
    assert s.offer("ap_3", "autre-fil") is None
    assert s.offer("inconnue", "t") is None
    other = tmp_path / "ws2"
    other.mkdir()
    assert store(tmp_path, other).offer("ap_3", "t") is None  # même dossier de stockage, autre racine


def test_snapshot_lives_outside_the_workspace_and_v4a_paths(tmp_path):
    root = workspace(tmp_path)
    s = store(tmp_path, root)
    s.before("ap_4", "t", root, "write_file", [str(root / "a.py")])
    assert not any(p.name == "manifest.json" for p in root.rglob("*"))
    assert json.loads((tmp_path / "state" / "cards" / "ap_4" / "manifest.json").read_text())["files"][0]["existed"]
    patch = "*** Begin Patch\n*** Update File: a.py\n*** Move to: b.py\n*** Add File: c.py\n*** Delete File: d.py\n"
    assert touched_paths("patch", {"mode": "patch", "patch": patch}) == ["a.py", "b.py", "c.py", "d.py"]
    assert touched_paths("patch", {"mode": "replace", "path": "a.py"}) == ["a.py"]
    assert touched_paths("terminal", {"command": "x"}) == []


# ─────────────────────────── enveloppe ───────────────────────────

class Writer:
    mount = "/workspace"

    def __init__(self, root):
        self.root = root

    def specs(self):
        return [ToolSpec("write_file", "w", {}, Effect.WRITE)]

    def call(self, name, args, ctx):
        (self.root / args["path"].removeprefix("/workspace/")).write_text(args["content"])
        return ToolResult(call_id="", name=name, content="ok")


class BrokenUpstream:
    """Gestionnaire de points git en panne (ou amont absent) : le point par carte n'en dépend pas."""

    def __getattr__(self, name):
        def boom(*a, **kw):
            raise RuntimeError("amont indisponible")
        return boom


def test_wrapper_snapshots_only_auto_approved_writes_even_without_upstream(tmp_path):
    root = workspace(tmp_path)
    s = store(tmp_path, root)
    w = CheckpointingToolSource(Writer(root), lambda _t: root, manager=BrokenUpstream(), cards=s)
    w.call("write_file", {"path": "/workspace/a.py", "content": "humain\n"}, ToolContext("t", 0, "api"))
    assert not (tmp_path / "state").exists()  # pas de carte automatique : rien
    w.call("write_file", {"path": "/workspace/a.py", "content": "auto\n"},
           ToolContext("t", 1, "api", auto_approval_id="ap_5"))
    assert s.offer("ap_5", "t")["available"] is True
    assert s.apply("ap_5", "t")["restored"] == ["a.py"] and (root / "a.py").read_text() == "humain\n"


# ─────────────────────────── noyau et routes ───────────────────────────

def test_record_rollback_in_the_approvals_journal():
    from nokker.approvals.errors import ApprovalNotFound

    backend = InMemoryApprovals()
    turn = Turn(thread_id="t", index=0, input=InputCheck(text="x"))
    a = backend.request(turn, ToolCall(id="c", name="write_file", arguments={"path": "a.py"}),
                        Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule="permissions:default",
                                strength="standard"), None)
    backend.record_rollback(a.id, "alice", checkpoint_id=a.id, restored=["a.py"], conflicts=["b.py"])
    ev = [(k, d) for aid, k, d in backend._events if aid == a.id and k == "auto_review_rollback"]
    assert ev and ev[0][1]["by"] == "alice" and ev[0][1]["restored"] == ["a.py"] and ev[0][1]["conflicts"] == ["b.py"]
    with pytest.raises(ApprovalNotFound):
        backend.record_rollback("inconnue", "alice", checkpoint_id="x", restored=[], conflicts=[])

def test_kernel_passes_the_card_to_the_auto_approved_call():
    from tests.unit.approvals.test_auto_review import Transport, kernel, patch_call

    rev = AutoReviewer(transport=Transport("DÉCISION: APPROUVER\nRAISON: demandé."))
    k, tools, backend = kernel([patch_call(), Message("assistant", "fait")], reviewer=rev)
    seen = []
    orig = tools.call

    def call(n, a, ctx):
        seen.append(ctx.auto_approval_id)
        return orig(n, a, ctx)

    tools.call = call
    turn = k.run_turn(TurnInput("t1", "corrige s.py", sender="alice"))
    assert seen == [turn.approval_ids[0]]


class Refusing:
    def __init__(self, reason=None, root="/"):
        self.reason, self.root = reason, root

    def root_of(self, _t):
        return self.root

    def check(self, _root, _tool, _args):
        return self.reason


@pytest.mark.parametrize("stores, why", [
    ([], "aucun point de retour possible pour ce fil"),
    ([Refusing(root=None)], "aucun point de retour possible pour ce fil"),
    ([Refusing("a.py : trop grand pour un point de retour")], "trop grand"),
])
def test_no_snapshot_no_auto_approval_of_a_write(stores, why):
    """Pas de point de retour possible : la carte va à l'humain, par règle, sans appel au modèle ; le modèle voit le
    texte d'attente habituel, la raison est rangée sur la carte."""
    from tests.unit.approvals.test_auto_review import WAIT_TEXT, Transport, kernel, patch_call, tool_texts

    rev = AutoReviewer(transport=Transport("DÉCISION: APPROUVER\nRAISON: demandé."))
    k, tools, backend = kernel([patch_call(), Message("assistant", "j'attends")], reviewer=rev)
    k.card_snapshots = stores
    turn = k.run_turn(TurnInput("t1", "corrige s.py", sender="alice"))
    pending = backend.pending()
    assert rev.transport.bodies == [] and tools.called == [] and len(pending) == 1
    rec = pending[0].auto_review
    assert rec["decision"] == "escalate" and rec["by_rule"] is True and why in rec["reason"]
    assert tool_texts(turn) == [WAIT_TEXT.format(id=pending[0].id)]
    assert backend.notifier.sent == [pending[0].id]


def test_preflight_with_a_real_store_and_mount(tmp_path):
    from nokker.tools.card_snapshots import preflight

    root = workspace(tmp_path)
    (root / "gros.bin").write_bytes(b"0" * 2048)
    (tmp_path / "dehors.txt").write_text("x")
    os.symlink(tmp_path / "dehors.txt", root / "lien.txt")
    s = store(tmp_path, root, max_file_bytes=1024)
    CheckpointingToolSource(Writer(root), lambda _t: root, manager=BrokenUpstream(), cards=s)  # pose le montage
    ok = preflight([s], "t", "write_file", {"path": "/workspace/a.py", "content": "x"})
    assert ok is None
    assert preflight([s], "t", "patch", {"mode": "patch", "patch": "*** Add File: neuf.py\n"}) is None
    for path in ("/workspace/gros.bin", "/workspace/../dehors.txt", "/workspace/lien.txt"):
        assert "point de retour impossible" in preflight([s], "t", "write_file", {"path": path, "content": "x"})
    assert preflight([s], "t", "patch", {"mode": "patch", "patch": "rien"}) is not None  # aucun fichier visé


def test_revoke_offers_rollback_then_explicit_route_applies(tmp_path):
    from tests.unit.api.test_approvals_routes import (
        _error_body,
        _get,
        _post,
        make_kernel_with_approvals,
        start_full_server,
    )
    from tests.unit.approvals.test_auto_review import Transport

    root = workspace(tmp_path)
    backend = InMemoryApprovals()
    k, _ = make_kernel_with_approvals(backend)
    k.auto_reviewer = AutoReviewer(transport=Transport())
    s = store(tmp_path, root)
    k.card_snapshots = [s]
    turn = Turn(thread_id="t", index=0, input=InputCheck(text="x"))
    a = backend.request(turn, ToolCall(id="c", name="write_file", arguments={"path": "a.py", "content": "auto\n"}),
                        Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule="permissions:default",
                                strength="standard"), None)
    backend.decide_auto(a.id, "approve", "demandé", effect="write")
    s.before(a.id, "t", root, "write_file", [str(root / "a.py")])
    (root / "a.py").write_text("auto\n")
    s.after(a.id, root)
    srv = start_full_server(k, approvals=backend, api_keys={"tok-a": "alice@example.ch", "tok-x": "mallory"})
    try:
        rb = f"/v1/approvals/{a.id}/auto-review/rollback"
        assert _error_body(lambda: _post(srv, rb, {}, bearer="tok-a"))["message"] == \
            "révoquer d'abord l'approbation automatique"
        out = json.loads(_post(srv, f"/v1/approvals/{a.id}/auto-review/revoke", {}, bearer="tok-a").read())
        assert out["rollback"] == {"available": True, "checkpoint_id": a.id, "files": ["a.py"], "applied": False}
        got = json.loads(_get(srv, f"/v1/approvals/{a.id}", bearer="tok-a").read())
        assert got["rollback"] == out["rollback"]  # GET de la carte : même proposition
        assert (root / "a.py").read_text() == "auto\n"  # la révocation ne restaure rien en silence
        done = json.loads(_post(srv, rb, {}, bearer="tok-a").read())
        assert done == {"approval_id": a.id, "checkpoint_id": a.id, "restored": ["a.py"], "conflicts": []}
        assert (root / "a.py").read_text() == "avant\n"
        assert "déjà appliquée" in _error_body(lambda: _post(srv, rb, {}, bearer="tok-a"))["message"]
        got = json.loads(_get(srv, f"/v1/approvals/{a.id}", bearer="tok-a").read())["rollback"]
        assert got["applied"] is True and got["available"] is False  # le bouton ne revient pas
        assert got["restored"] == ["a.py"] and got["conflicts"] == []
        logged = [d for aid, kind, d in backend._events if aid == a.id and kind == "auto_review_rollback"]
        assert len(logged) == 1 and logged[0]["by"] == "alice@example.ch" and logged[0]["at"] > 0
        assert logged[0]["restored"] == ["a.py"] and logged[0]["conflicts"] == [] and logged[0]["checkpoint_id"] == a.id
    finally:
        srv.stop()


def test_revoke_without_snapshot_says_unavailable(tmp_path):
    from tests.unit.api.test_approvals_routes import _get, _post, make_kernel_with_approvals, start_full_server
    from tests.unit.approvals.test_auto_review import Transport

    backend = InMemoryApprovals()
    k, _ = make_kernel_with_approvals(backend)
    k.auto_reviewer = AutoReviewer(transport=Transport())
    turn = Turn(thread_id="t", index=0, input=InputCheck(text="x"))
    a = backend.request(turn, ToolCall(id="c", name="calendar_create", arguments={}),
                        Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule="permissions:default",
                                strength="standard"), None)
    backend.decide_auto(a.id, "approve", "demandé", effect="write")
    srv = start_full_server(k, approvals=backend, api_keys={"tok-a": "alice@example.ch"})
    try:
        path = f"/v1/approvals/{a.id}"
        assert "rollback" not in json.loads(_get(srv, path, bearer="tok-a").read())  # pas encore révoquée
        out = json.loads(_post(srv, f"{path}/auto-review/revoke", {}, bearer="tok-a").read())
        assert out["rollback"] == {"available": False, "checkpoint_id": None, "applied": False,
                                   "reason": "aucun point de retour pour cette carte"}
        assert json.loads(_get(srv, path, bearer="tok-a").read())["rollback"] == out["rollback"]
    finally:
        srv.stop()


# ─────────────────────────── bulle `approval` de la décision automatique ───────────────────────────

def _hub():
    from nokker.bubbles.hub import BubbleHub
    from nokker.bubbles.memory import InMemoryBubbles
    from nokker.threads.memory import InMemoryThreads

    events = []
    hub = BubbleHub(threads=InMemoryThreads(), bubbles=InMemoryBubbles(), publish=lambda t, d: events.append((t, d)),
                    spawn=lambda fn: fn())
    return hub, events


def test_auto_approval_opens_an_approval_bubble_without_model_text():
    """Approbation automatique : bulle `approval` dans le fil (approuvée, `decided_via: auto-review`, raison) ; le
    modèle voit le résultat de l'outil, rien d'autre ; aucune bulle pour une escalade."""
    from tests.unit.approvals.test_auto_review import Transport, kernel, patch_call, tool_texts

    hub, events = _hub()
    rev = AutoReviewer(transport=Transport("DÉCISION: APPROUVER\nRAISON: modification demandée."))
    k, tools, backend = kernel([patch_call(), Message("assistant", "fait")], reviewer=rev)
    k.observer, hub.kernel = hub, k
    turn = k.run_turn(TurnInput("t1", "corrige s.py", sender="alice"))
    assert tool_texts(turn) == ["fait"]  # inchangé : le résultat de l'outil
    bubbles = [b for b in hub.bubbles.list_thread("t1") if b.kind == "approval"]
    assert len(bubbles) == 1
    st = bubbles[0].state
    assert st["approval_id"] == turn.approval_ids[0] and st["decided_via"] == "auto-review"
    assert st["decision"] == "approve" and st["status"] == "executed" and "demandée" in st["reason"]
    assert st["revoked"] is False and bubbles[0].created_by == "nina"
    created = [d for t, d in events if t == "bubble.created" and d["bubble"]["kind"] == "approval"]
    assert len(created) == 1 and created[0]["bubble"]["state"]["decided_via"] == "auto-review"
    assert not any(t.startswith("notify") for t, _ in events)

    k2, _, _ = kernel([patch_call(), Message("assistant", "j'attends")],
                      reviewer=AutoReviewer(transport=Transport("DÉCISION: ESCALADER\nRAISON: doute.")))
    k2.observer = hub
    k2.run_turn(TurnInput("t2", "corrige s.py", sender="alice"))
    assert [b for b in hub.bubbles.list_thread("t2") if b.kind == "approval"] == []


def test_revoke_and_rollback_update_the_bubble(tmp_path):
    from nokker.api.server import make_handler
    from tests.unit.api.test_approvals_routes import _get, _post, make_kernel_with_approvals
    from tests.unit.approvals.test_auto_review import Transport

    import threading
    from http.server import ThreadingHTTPServer

    root = workspace(tmp_path)
    hub, events = _hub()
    backend = InMemoryApprovals()
    k, _ = make_kernel_with_approvals(backend)
    k.auto_reviewer = AutoReviewer(transport=Transport())
    s = store(tmp_path, root)
    k.card_snapshots = [s]
    turn = Turn(thread_id="t", index=0, input=InputCheck(text="x"))
    a = backend.request(turn, ToolCall(id="c", name="write_file", arguments={"path": "a.py", "content": "auto\n"}),
                        Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule="permissions:default",
                                strength="standard"), None)
    backend.decide_auto(a.id, "approve", "demandé", effect="write")
    s.before(a.id, "t", root, "write_file", [str(root / "a.py")])
    (root / "a.py").write_text("auto\n")
    s.after(a.id, root)
    hub.auto_review_decided(turn, backend.get(a.id))
    bid = next(b.id for b in hub.bubbles.list_thread("t") if b.kind == "approval")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(k, {"tok-a": "alice@example.ch"}, None,
                                                                  approvals=backend, hub=hub))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    class Srv:
        def url(self, path):
            return f"http://127.0.0.1:{httpd.server_address[1]}{path}"
    srv = Srv()
    try:
        _post(srv, f"/v1/approvals/{a.id}/auto-review/revoke", {}, bearer="tok-a").read()
        b = hub.bubbles.get(bid)
        assert b.state["revoked"] is True and "rollback_applied" not in b.state and b.version >= 2
        _post(srv, f"/v1/approvals/{a.id}/auto-review/rollback", {}, bearer="tok-a").read()
        assert hub.bubbles.get(bid).state["rollback_applied"] is True
        assert sum(1 for t, _ in events if t == "bubble.updated") >= 2
        assert json.loads(_get(srv, f"/v1/approvals/{a.id}", bearer="tok-a").read())["rollback"]["applied"] is True
    finally:
        httpd.shutdown()
