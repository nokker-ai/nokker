"""Une écriture ne s'exécute JAMAIS deux fois (banc de parité de l'hôte : tests lancés 3×, patch et write_file 2×).

Mécanisme : un tour pose plusieurs cartes (une par outil), le modèle ne voit pas leur résultat ; au tour de suite de la
première décision il redemande l'action encore en attente, et le noyau posait une 2e carte IDENTIQUE (même outil, mêmes
arguments) à côté de la première : chacune, décidée, s'exécutait. Le noyau rend désormais la carte déjà en attente
(aucune carte de plus), et l'exécuteur n'exécute jamais une carte déjà tranchée (clé = id + empreinte des arguments).
"""
from __future__ import annotations

import threading

import pytest

from nokker.approvals.crypto import args_sha256
from nokker.approvals.executor import ApprovalExecutor
from nokker.approvals.memory import InMemoryApprovals
from nokker.core.loop import Kernel
from nokker.core.ports import ToolContext, ToolSpec
from nokker.core.turn import (
    Decision,
    Effect,
    Gate,
    Message,
    Tier,
    ToolCall,
    ToolResult,
    TurnInput,
    Verdict,
)
from tests.unit.test_loop_approvals import (
    Ctx,
    Esc,
    LedgerFake,
    Mem,
    Pol,
    ScriptedProvider,
    StoreFake,
)


class WriteInv:
    def check_input(self, text):
        from nokker.core.turn import InputCheck

        return InputCheck(text=text)

    def check_action(self, call, spec, turn):
        if spec is not None and spec.effect is Effect.WRITE:
            return Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule="permissions:default",
                           reason="écriture", strength="standard")
        return Verdict(gate=Gate.ACTION, decision=Decision.ALLOW, layer=1, rule="allow")

    def redact_result(self, result, spec):
        return result

    def scrub_output(self, text):
        return text


class Gates:
    def __init__(self, inv):
        self.inv = inv

    def action(self, call, spec, turn):
        return [self.inv.check_action(call, spec, turn)]

    def switch(self, reply, turn, tier):
        return [Verdict(gate=Gate.SWITCH, decision=Decision.ALLOW, layer=1, rule="ok")]


class CountedTools:
    """Outil d'écriture factice qui compte ses exécutions par (nom, arguments)."""

    def __init__(self):
        self.runs: list[tuple[str, str]] = []
        self.specs = {n: ToolSpec(n, n, {}, Effect.WRITE) for n in ("patch", "write_file")}

    def core(self):
        return []

    def spec(self, n):
        return self.specs.get(n)

    def find(self, q, limit=5):
        return []

    def index(self):
        return []

    def call(self, n, a, ctx):
        self.runs.append((n, str(sorted(a.items()))))
        return ToolResult(call_id="", name=n, content="ok")


def calls(*specs):
    return Message("assistant", tool_calls=[ToolCall(id=f"c{i}-{n}", name=n, arguments=a)
                                            for i, (n, a) in enumerate(specs)])


def build(script, tools=None):
    tools = tools or CountedTools()
    backend = InMemoryApprovals()
    inv = WriteInv()
    k = Kernel(store=StoreFake(), context=Ctx(), memory=Mem(), invariants=inv, gates=Gates(inv), escalator=Esc(),
               policy=Pol(), providers={Tier.SHIP: ScriptedProvider(script)}, tools=tools, ledger=LedgerFake(),
               approvals=backend)
    return k, ApprovalExecutor(kernel=k, approvals=backend), backend, tools


def approve(backend, aid):
    return backend.decide_local(aid, "approve", "alice")


def drain(backend, ex):
    """Décide et exécute, un par un, tout ce qui reste en attente."""
    n = 0
    while backend.pending():
        ex.run(approve(backend, backend.pending()[0].id))
        n += 1
        assert n < 20
    return n


A = {"path": "a.txt", "content": "A"}
B = {"path": "b.txt", "content": "B"}
TEXT = Message("assistant", "j'attends")
DONE = Message("assistant", "fini")


def test_queued_action_asked_again_is_not_a_second_card_and_runs_once():
    # tour 0 : deux écritures du même outil → deux cartes (la 2e ferme le tour) ; au tour de suite de la 1re, le
    # modèle redemande la 2e, encore en attente.
    k, ex, backend, tools = build([calls(("write_file", A), ("write_file", B)), calls(("write_file", B)), TEXT,
                                   DONE, DONE, DONE])
    k.run_turn(TurnInput("t1", "écris a et b"))
    first, second = sorted(backend.pending(), key=lambda a: a.created_at)
    ex.run(approve(backend, first.id))  # la suite redemande B
    assert len(backend.pending()) == 1 and backend.pending()[0].id == second.id  # aucune carte de plus
    drain(backend, ex)
    assert sorted(tools.runs) == sorted([("write_file", str(sorted(A.items()))), ("write_file", str(sorted(B.items())))])


def test_same_card_cannot_run_twice_even_if_scheduled_twice():
    k, ex, backend, tools = build([calls(("patch", A)), TEXT, DONE, DONE])
    k.run_turn(TurnInput("t1", "patch"))
    decided = approve(backend, backend.pending()[0].id)
    ex.run(decided)
    ex.run(decided)  # même carte, deuxième ordonnancement (décision HTTP + relève, redémarrage de la suite…)
    assert len(tools.runs) == 1 and backend.get(decided.id).status == "executed"


def test_concurrent_runs_of_the_same_card_execute_once():
    k, ex, backend, tools = build([calls(("patch", A)), TEXT, DONE, DONE, DONE, DONE])
    k.run_turn(TurnInput("t1", "patch"))
    decided = approve(backend, backend.pending()[0].id)
    threads = [threading.Thread(target=ex.run, args=(decided,)) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(tools.runs) == 1


def test_second_decision_is_refused_and_does_not_rerun():
    from nokker.approvals.errors import ApprovalNotPending

    k, ex, backend, tools = build([calls(("patch", A)), TEXT, DONE])
    k.run_turn(TurnInput("t1", "patch"))
    aid = backend.pending()[0].id
    ex.run(approve(backend, aid))
    with pytest.raises(ApprovalNotPending):
        approve(backend, aid)
    assert len(tools.runs) == 1


def test_a_card_already_executed_is_not_rerun_by_a_restarted_continuation():
    k, ex, backend, tools = build([calls(("patch", A)), TEXT, DONE, DONE])
    k.run_turn(TurnInput("t1", "patch"))
    decided = approve(backend, backend.pending()[0].id)
    ex.run(decided)
    ex2 = ApprovalExecutor(kernel=k, approvals=backend)  # exécuteur neuf : le registre en mémoire est perdu
    assert ex2.run(decided) is None
    assert len(tools.runs) == 1


def test_same_action_after_it_was_executed_is_still_asked_again():
    # une commande identique relancée après exécution (tests après correction) reste une décision du modèle : nouvelle carte
    k, ex, backend, _tools = build([calls(("patch", A)), TEXT, calls(("patch", A)), TEXT, DONE])
    k.run_turn(TurnInput("t1", "patch"))
    ex.run(approve(backend, backend.pending()[0].id))
    assert len(backend.pending()) == 1  # la suite a redemandé A : l'ancienne est exécutée, donc une nouvelle carte


def test_duplicate_reuses_the_pending_card_text_for_the_model():
    k, _ex, backend, _tools = build([calls(("write_file", A)), TEXT, calls(("write_file", A)), TEXT])
    k.run_turn(TurnInput("t1", "a"))
    pending_id = backend.pending()[0].id
    turn = k.run_turn(TurnInput("t1", "encore"))
    assert [a.id for a in backend.pending()] == [pending_id]
    assert any(m.role == "tool" and f"[approval:{pending_id}]" in m.content for m in turn.messages)
    assert pending_id in turn.approval_ids


def test_args_hash_is_part_of_the_dedup_key():
    k, _ex, backend, _tools = build([calls(("write_file", A)), TEXT, calls(("write_file", {**A, "content": "A2"})), TEXT])
    k.run_turn(TurnInput("t1", "a"))
    k.run_turn(TurnInput("t1", "a2"))
    assert len(backend.pending()) == 2 and len({a.args_sha256 for a in backend.pending()}) == 2
    assert backend.pending()[0].args_sha256 in {args_sha256(A), args_sha256({**A, "content": "A2"})}


class FsTools(CountedTools):
    """Écriture réelle sur un vrai dossier (remplacement de texte, comme `patch` mode replace)."""

    def __init__(self, root):
        super().__init__()
        self.root = root

    def call(self, n, a, ctx):
        self.runs.append((n, str(sorted(a.items()))))
        f = self.root / a["path"]
        if n == "write_file":
            f.write_text(a["content"])
        else:
            f.write_text(f.read_text().replace(a["old_string"], a["new_string"]))
        return ToolResult(call_id="", name=n, content="ok")


def _patch_scenario(tools):
    patch = {"mode": "replace", "path": "f.txt", "old_string": "x", "new_string": "xx"}
    other = {"path": "g.txt", "content": "G"}
    k, ex, backend, tools = build([calls(("write_file", other), ("patch", patch)), TEXT, calls(("patch", patch)), TEXT,
                                   DONE, DONE, DONE], tools=tools)
    k.run_turn(TurnInput("t1", "patche"))
    wf = next(a for a in backend.pending() if a.tool_name == "write_file")
    ex.run(approve(backend, wf.id))
    drain(backend, ex)
    return tools


def test_patch_on_a_real_folder_is_applied_once(tmp_path):
    """« x » → « xx » appliqué deux fois donnerait « xxxx » : le fichier ne change qu'une fois."""
    (tmp_path / "f.txt").write_text("x")
    tools = _patch_scenario(FsTools(tmp_path))
    assert (tmp_path / "f.txt").read_text() == "xx" and (tmp_path / "g.txt").read_text() == "G"
    assert sum(1 for n, _ in tools.runs if n == "patch") == 1


def test_upstream_patch_tool_is_applied_once(tmp_path):
    """Même scénario avec le vrai outil `patch` de la source de code (amont requis, absent : ignoré)."""
    try:
        from tools.file_operations import ShellFileOperations  # noqa: F401
        from tools.todo_tool import TodoStore  # noqa: F401
    except Exception:  # noqa: BLE001
        pytest.skip("amont (tools.file_operations) non importable")
    from nokker.tools.code import CodeToolSource

    (tmp_path / "f.txt").write_text("x")
    src = CodeToolSource(root=str(tmp_path), backend="local")

    class RealTools(CountedTools):
        def __init__(self):
            super().__init__()
            self.specs = {s.name: s for s in src.specs() if s.name in ("patch", "write_file")}

        def call(self, n, a, ctx):
            self.runs.append((n, str(sorted(a.items()))))
            return src.call(n, a, ToolContext(thread_id=ctx.thread_id, turn_index=0, channel=ctx.channel))

    tools = _patch_scenario(RealTools())
    assert (tmp_path / "f.txt").read_text() == "xx"
    assert sum(1 for n, _ in tools.runs if n == "patch") == 1


def test_double_post_decide_on_the_real_server_executes_once(tmp_path):
    """Deux POST /v1/local/approvals/{id}/decide sur la même carte : le 2e est refusé (409), l'outil a tourné une fois."""
    import time
    from http.server import ThreadingHTTPServer

    from nokker.api.server import make_handler
    from nokker.local.api import LOCAL_OWNER
    from tests.unit.api.test_approvals_routes import make_kernel_with_approvals
    from tests.unit.test_local_api import _api, _req

    approvals = InMemoryApprovals()
    kernel, tools = make_kernel_with_approvals(approvals)
    api = _api(tmp_path)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(kernel, {api.token: LOCAL_OWNER}, approvals=approvals,
                                                               local_api=api))
    port = httpd.server_address[1]
    api.allowed_hosts = frozenset({f"127.0.0.1:{port}"})
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        kernel.run_turn(TurnInput("th_1", "agrandis", sender=LOCAL_OWNER))
        card = approvals.pending()[0]
        body = {"decision": "approve", "scope": "once"}
        first = _req(port, f"/v1/local/approvals/{card.id}/decide", token=api.token, body=body)
        second = _req(port, f"/v1/local/approvals/{card.id}/decide", token=api.token, body=body)
        assert first[0] in (200, 202) and second[0] == 409, (first, second)
        deadline = time.time() + 5
        while approvals.get(card.id).status == "approved" and time.time() < deadline:
            time.sleep(0.05)
        time.sleep(0.3)
        assert tools.called == ["infra_lvextend"] and approvals.get(card.id).status == "executed"
    finally:
        httpd.shutdown()
