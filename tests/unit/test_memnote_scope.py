"""`memory_note` hors session de code : `scope` absent ⇒ « user » (une seule portée possible) ; scope explicitement invalide ou « project » sans session ⇒
refus inchangé ; en session de code le défaut reste « project ». Le test de bout en bout passe par le VRAI serveur HTTP (`make_handler`, requête
`POST /v1/chat/completions` sur un fil sans dossier), le modèle est scripté."""
from __future__ import annotations

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

from nokker.api.server import make_handler
from nokker.context.project_memory import ProjectMemory, ProjectMemoryTools
from nokker.core.loop import Kernel
from nokker.core.ports import Assembled, Completion, ToolContext
from nokker.core.turn import Message, Tier, ToolCall, Usage
from nokker.ledger.costs import InMemoryLedger
from nokker.store.pg import MemoryStore
from tests.unit.api.test_server import AllowGates, AllowInvariants, NoEscalator, NullMem, ShipOnlyPolicy
from tests.unit.context.test_project_memory import FakeEngine

SAID = "Retiens pour toujours : ma machine de sauvegarde s'appelle Zébulon, elle tourne la nuit à 2 h."
QUOTE = "ma machine de sauvegarde s'appelle Zébulon"
NOTE = {"kind": "fact", "title": "Machine de sauvegarde", "text": "La machine de sauvegarde de l'utilisateur s'appelle Zébulon.",
        "derived_from": "user", "quote": QUOTE}


def _memory(tmp_path) -> ProjectMemory:
    return ProjectMemory(root=tmp_path / "pm", engine=FakeEngine(tmp_path / "pm"), emit=lambda n, d: None)


def _call(pm, args, thread="th_web", turn=1):
    pm.core_block(thread, turn, SAID)  # le noyau enregistre les mots de la personne au premier assemblage du tour
    return ProjectMemoryTools(pm).call("memory_note", args, ToolContext(thread_id=thread, turn_index=turn, channel="local"))


def test_scope_absent_without_a_code_session_defaults_to_user(tmp_path):
    pm = _memory(tmp_path)
    r = _call(pm, dict(NOTE))
    assert not r.is_error and "portée user" in r.content, r.content


def test_explicit_invalid_scope_or_project_without_session_is_still_refused(tmp_path):
    pm = _memory(tmp_path)
    for bad in ("project", "global", "data"):
        r = _call(pm, {**NOTE, "scope": bad})
        assert r.is_error and ("seule la portée user" in r.content or "scope ∈" in r.content), (bad, r.content)


def test_other_refusals_are_unchanged_when_scope_defaults(tmp_path):
    pm = _memory(tmp_path)
    assert "quote" in _call(pm, {**NOTE, "quote": "mots que la personne n'a pas dits"}).content  # citation fausse
    assert "dérivée" in _call(pm, {**NOTE, "derived_from": "agent"}).content  # note dérivée : jamais en portée user
    assert _call(pm, dict(NOTE), thread="th_web/sa-ab12").is_error  # sous-agent


class NoteWithoutScope:
    """Modèle scripté : appelle memory_note SANS scope, puis répond avec ce que l'outil a rendu."""

    def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048):
        if messages[-1]["role"] != "tool":
            args = dict(NOTE)
            msg = Message(role="assistant", content="", tool_calls=[ToolCall(id="c1", name="memory_note", arguments=args, raw_arguments=json.dumps(args))])
        else:
            msg = Message(role="assistant", content="TOOL: " + messages[-1]["content"])
        return Completion(message=msg, usage=Usage(prompt=10, completion=2), model="scripted", latency_s=0.0)


def test_real_http_server_thread_without_folder_note_without_scope_is_noted(tmp_path):
    pm = _memory(tmp_path)

    class Ctx:
        def assemble(self, turn, history, recall, loaded, tier):
            pm.core_block(turn.thread_id, turn.index, turn.input.text)
            msgs = [{"role": "user", "content": turn.input.text}] + [m.to_openai() for m in turn.messages]
            return Assembled(messages=msgs, tools=[], prefix_sha256="t")

        def admit_result(self, r):
            return r

    kernel = Kernel(store=MemoryStore(), context=Ctx(), memory=NullMem(), invariants=AllowInvariants(), gates=AllowGates(), escalator=NoEscalator(),
                    policy=ShipOnlyPolicy(), providers={Tier.SHIP: NoteWithoutScope()}, tools=_Tools(pm), ledger=InMemoryLedger())
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(kernel, {"k": "tester"}, None))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{httpd.server_address[1]}/v1/chat/completions",
                                     data=json.dumps({"model": "nokker", "messages": [{"role": "user", "content": SAID}]}).encode(),
                                     headers={"Authorization": "Bearer k", "Content-Type": "application/json", "X-Session-Id": "th_web"})
        with urllib.request.urlopen(req, timeout=10) as r:
            reply = json.loads(r.read())["choices"][0]["message"]["content"]
    finally:
        httpd.shutdown()
    assert "TOOL:" in reply and "seule la portée user" not in reply and "portée user" in reply, reply
    assert list((tmp_path / "pm" / "u-local").glob("*.md")), "la note de portée user n'a pas été écrite"


class _Tools:
    def __init__(self, pm):
        self.t = ProjectMemoryTools(pm)

    def core(self):
        return self.t.specs()

    def spec(self, name):
        return next((s for s in self.t.specs() if s.name == name), None)

    def find(self, query, limit=5):
        return []

    def index(self):
        return []

    def call(self, name, arguments, ctx):
        return self.t.call(name, arguments, ctx)
