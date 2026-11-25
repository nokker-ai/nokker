"""Préfixe par modèle servant le tour (`context/turn_model.py`, `ContextEngineV0.prefix_by_model`, `local.prefix_by_model`) : sans la clé, préfixe et sha identiques à
l'octet ; avec elle, id EXACT du modèle → son préfixe, sinon repli ; sha par (locataire, modèle), stable dans un tour ; bascule à chaud du défaut entre deux tours ;
un remplacement des fournisseurs PENDANT un tour ne change pas le préfixe de ce tour ; chemin illisible = erreur claire au démarrage ; le tout par le vrai serveur HTTP."""
from __future__ import annotations

import json
import logging
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from nokker.api.server import make_handler
from nokker.context.engine import ContextEngineV0
from nokker.context.turn_model import active_model, serving_model, turn_model_scope
from nokker.core.loop import Kernel
from nokker.core.ports import Completion
from nokker.core.turn import Message, Tier, ToolCall, Usage
from nokker.ledger.costs import InMemoryLedger
from nokker.store.pg import MemoryStore
from nokker.tools.registry import Registry
from tests.unit.api.test_server import AllowGates, AllowInvariants, NoEscalator, NullMem, ShipOnlyPolicy


def _engine(tmp_path, by_model=None):
    base = tmp_path / "base.md"
    base.write_text("PREFIXE-DE-BASE\n", encoding="utf-8")
    kw = {} if by_model is None else {"prefix_by_model": {m: _file(tmp_path, f, t) for m, (f, t) in by_model.items()}}
    return ContextEngineV0(tools=Registry(sources=[]), prefix_path=str(base), **kw)


def _file(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_without_the_key_prefix_and_sha_are_identical_to_the_byte(tmp_path):
    plain = _engine(tmp_path)
    empty = _engine(tmp_path, by_model={})  # clé présente mais vide
    with turn_model_scope("nina70b"):  # même avec un modèle servant : rien ne change sans table
        assert (plain._prefix_text, plain._prefix_sha256) == (empty._prefix_text, empty._prefix_sha256)
    assert plain._prefix_text == "PREFIXE-DE-BASE\n"


def test_exact_model_id_selects_its_prefix_and_everything_else_falls_back(tmp_path):
    eng = _engine(tmp_path, {"nina-70b": ("n.md", "PREFIXE-NINA\n"), "qwen3.8-27b": ("q.md", "PREFIXE-QWEN\n")})
    base_sha = eng._prefix_sha256
    with turn_model_scope("nina-70b"):
        assert eng._prefix_text == "PREFIXE-NINA\n" and eng._prefix_sha256 != base_sha
    with turn_model_scope("qwen3.8-27b"):
        assert eng._prefix_text == "PREFIXE-QWEN\n"
    for not_exact in ("Nina-70B", "nina-70", "nina-70b-fp8", " nina-70b", "inconnu", None):  # casse, préfixe, variante, absent, aucun modèle
        with turn_model_scope(not_exact):
            assert eng._prefix_text == "PREFIXE-DE-BASE\n" and eng._prefix_sha256 == base_sha, not_exact


def test_sha_is_frozen_per_model_and_stable(tmp_path):
    eng = _engine(tmp_path, {"a": ("a.md", "A\n"), "b": ("b.md", "B\n")})
    seen = {}
    for m in ("a", "b", "a", "b"):
        with turn_model_scope(m):
            seen.setdefault(m, set()).add(eng._prefix_sha256)
    assert len(seen["a"]) == 1 and len(seen["b"]) == 1 and seen["a"] != seen["b"]


def test_unreadable_path_is_a_clear_startup_error_and_the_startup_log_lists_ids_and_files_without_content(tmp_path, caplog):
    base = tmp_path / "base.md"
    base.write_text("X", encoding="utf-8")
    with pytest.raises(ValueError, match="prefix_by_model.*illisible.*'nina-70b'"):
        ContextEngineV0(tools=Registry(sources=[]), prefix_path=str(base), prefix_by_model={"nina-70b": str(tmp_path / "absent.md")})
    ok = _file(tmp_path, "n.md", "CONTENU-SECRET-DU-PREFIXE")
    with caplog.at_level(logging.INFO, logger="nokker.context.engine"):
        ContextEngineV0(tools=Registry(sources=[]), prefix_path=str(base), prefix_by_model={"nina-70b": ok})
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "nina-70b -> " + ok in text and "CONTENU-SECRET-DU-PREFIXE" not in text and str(base) in text


def test_serving_model_needs_the_same_model_on_ship_and_deep():
    class P:
        def __init__(self, model):
            self.model = model
    assert serving_model({Tier.SHIP: P("m"), Tier.DEEP: P("m")}, Tier.SHIP, Tier.DEEP) == "m"
    assert serving_model({Tier.SHIP: P("m"), Tier.DEEP: P("n")}, Tier.SHIP, Tier.DEEP) is None  # paliers différents : repli
    assert serving_model({Tier.SHIP: P("m")}, Tier.SHIP, Tier.DEEP) is None and serving_model({Tier.SHIP: object(), Tier.DEEP: object()}, Tier.SHIP, Tier.DEEP) is None
    assert active_model() is None


class Recorder:
    """Fournisseur scripté : enregistre le message système de chaque appel ; `swap` (optionnel) remplace les fournisseurs du noyau PENDANT le tour."""

    def __init__(self, model, tool_first=False):
        self.model, self.tool_first, self.systems, self.swap = model, tool_first, [], None

    def complete(self, messages, tools, *, reasoning, session_key, max_tokens=2048):
        self.systems.append(messages[0]["content"])
        if self.tool_first and messages[-1]["role"] != "tool":
            if self.swap:
                self.swap()
            msg = Message(role="assistant", content="", tool_calls=[ToolCall(id="c1", name="outil_inconnu", arguments={}, raw_arguments="{}")])
        else:
            msg = Message(role="assistant", content="ok")
        return Completion(message=msg, usage=Usage(prompt=5, completion=1), model=self.model, latency_s=0.0)


def _server(tmp_path, providers):
    eng = _engine(tmp_path, {"nina-70b": ("n.md", "PREFIXE-NINA\n"), "qwen3.8-27b": ("q.md", "PREFIXE-QWEN\n")})
    kernel = Kernel(store=MemoryStore(), context=eng, memory=NullMem(), invariants=AllowInvariants(), gates=AllowGates(), escalator=NoEscalator(),
                    policy=ShipOnlyPolicy(), providers=providers, tools=eng.tools, ledger=InMemoryLedger())
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(kernel, {"k": "tester"}, None))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return kernel, httpd


def _chat(httpd, thread, text="bonjour"):
    req = urllib.request.Request(f"http://127.0.0.1:{httpd.server_address[1]}/v1/chat/completions",
                                 data=json.dumps({"model": "nokker", "messages": [{"role": "user", "content": text}]}).encode(),
                                 headers={"Authorization": "Bearer k", "Content-Type": "application/json", "X-Session-Id": thread})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())["choices"][0]["message"]["content"]


def test_real_http_server_hot_switch_between_two_turns_of_one_thread(tmp_path):
    a, b = Recorder("nina-70b"), Recorder("qwen3.8-27b")
    kernel, httpd = _server(tmp_path, {Tier.SHIP: a, Tier.DEEP: a})
    try:
        assert _chat(httpd, "th_1") == "ok" and a.systems[-1].startswith("PREFIXE-NINA")
        kernel.providers[Tier.SHIP] = kernel.providers[Tier.DEEP] = b  # bascule du défaut (PUT /v1/local/setup/models) entre deux tours
        assert _chat(httpd, "th_1", "encore") == "ok" and b.systems[-1].startswith("PREFIXE-QWEN")
        kernel.providers[Tier.SHIP] = kernel.providers[Tier.DEEP] = Recorder("autre-modele")  # id absent de la table : repli
        _chat(httpd, "th_1", "et puis")
        assert kernel.providers[Tier.SHIP].systems[-1].startswith("PREFIXE-DE-BASE")
    finally:
        httpd.shutdown()


def test_real_http_server_swap_during_a_turn_does_not_touch_the_running_turn(tmp_path):
    a, b = Recorder("nina-70b", tool_first=True), Recorder("qwen3.8-27b")
    kernel, httpd = _server(tmp_path, {Tier.SHIP: a, Tier.DEEP: a})
    # outil inconnu : le tour continue avec une erreur d.outil et fait un 2e appel (chez le fournisseur remplacé).
    a.swap = lambda: kernel.providers.update({Tier.SHIP: b, Tier.DEEP: b})
    try:
        _chat(httpd, "th_2")
        assert kernel.providers[Tier.SHIP] is b  # le remplacement a eu lieu PENDANT le tour : le 2e appel du tour part chez b
        assert a.systems == ["PREFIXE-NINA\n"] and b.systems == ["PREFIXE-NINA\n"]  # …avec le préfixe capturé au DÉBUT du tour, pas celui de b
        _chat(httpd, "th_2", "suite")
        assert b.systems and b.systems[-1].startswith("PREFIXE-QWEN")  # le tour suivant prend le nouveau modèle
    finally:
        httpd.shutdown()


def test_thread_forced_model_selects_the_prefix_of_the_model_that_serves_the_turn(tmp_path):
    from nokker.byok.context import ByokRoute

    default, forced, unknown = Recorder("nina-70b"), Recorder("qwen3.8-27b"), Recorder("modele-sans-table")
    kernel, httpd = _server(tmp_path, {Tier.SHIP: default, Tier.DEEP: default})
    routes = {"th_f": ByokRoute(name="qwen3.8-27b", provider=forced, own_key=False),
              "th_u": ByokRoute(name="modele-sans-table", provider=unknown, own_key=False)}
    kernel.thread_route = routes.get
    try:
        assert _chat(httpd, "th_f") == "ok" and forced.systems[-1].startswith("PREFIXE-QWEN") and not default.systems
        assert _chat(httpd, "th_u") == "ok" and unknown.systems[-1].startswith("PREFIXE-DE-BASE")  # id sans entrée : repli sur prefix_path
        assert _chat(httpd, "th_other") == "ok" and default.systems[-1].startswith("PREFIXE-NINA")  # fil sans modèle forcé : le défaut
    finally:
        httpd.shutdown()


def test_hash_differs_per_model_and_fallback_hash_is_the_base_one(tmp_path):
    eng = _engine(tmp_path, {"nina-70b": ("n.md", "PREFIXE-NINA\n"), "qwen3.8-27b": ("q.md", "PREFIXE-QWEN\n")})
    plain = _engine(tmp_path)
    shas = {}
    for m in ("nina-70b", "qwen3.8-27b", "inconnu"):
        with turn_model_scope(m):
            shas[m] = eng._prefix_sha256
    assert len(set(shas.values())) == 3 and shas["inconnu"] == plain._prefix_sha256
