"""Mémoire longue CortHeXis en mode direct, branchée par `build_kernel` (lot X1b, docs/LOKKAL.md §7.13).

Sans Postgres ni embed : `corthexis.service` est remplacé par un faux module (le paquet n'est pas requis ici). Le test
réel (vrai Postgres pgvector, vrai embed, vrai CortHeXis) est `test_direct_lokkal_integration.py`."""
from __future__ import annotations

import logging
import sys
import types
from pathlib import Path

import pytest

from nokker.api.server import build_kernel
from nokker.config import NokkerConfig, load_config
from nokker.context.project_memory import USER_PROJECT, ProjectMemoryTools, ProjectRecallMemory, install
from nokker.core.ports import Recall
from nokker.memory import corthexis as cx
from nokker.memory.corthexis import DirectCorthexisMemory, from_config, memory_mode, status_of
from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope
from nokker.tenancy.resources import NullMemory
from tests.unit.context.test_project_memory import ctx, make_memory, make_repo

ROOT = Path(__file__).resolve().parents[3]
TESTER = "tn_aaaaaaaaaaaa"


@pytest.fixture
def fake_cx(monkeypatch):
    """Un faux paquet `corthexis` (service + embed) : lignes de recherche et pannes à volonté."""
    state = {"rows": [], "calls": [], "stats": {"notes": 3, "chunks": 5, "generation": 1, "model": "m"},
             "store_error": None, "embed_error": None}

    def search(query, top_k=8, *, deep=False, projects=None):
        state["calls"].append({"query": query, "projects": projects, "top_k": top_k})
        return list(state["rows"])

    class Store:
        def stats(self):
            if state["store_error"]:
                raise RuntimeError(state["store_error"])
            return state["stats"]

    class Emb:
        def embed_query(self, text, timeout=30):
            if state["embed_error"]:
                raise RuntimeError(state["embed_error"])
            return [0.1] * 8

    service = types.SimpleNamespace(search=search, get=lambda *a, **k: None, reset=lambda: None,
                                    get_store=lambda: Store())
    embed = types.SimpleNamespace(get=lambda: Emb())
    pkg = types.ModuleType("corthexis")
    pkg.service, pkg.embed = service, embed
    monkeypatch.setitem(sys.modules, "corthexis", pkg)
    monkeypatch.setitem(sys.modules, "corthexis.service", service)
    monkeypatch.setitem(sys.modules, "corthexis.embed", embed)
    monkeypatch.setattr(DirectCorthexisMemory, "warm_up", lambda self: None)  # pas de fil de fond dans ces tests
    return state


def _cfg(**kw) -> NokkerConfig:
    return NokkerConfig(pg_dsn_env="TEST_NOKKER_ABSENT_DSN_VAR", mcp_tool_servers=[], web_search_enabled=False,
                        approvals_enabled=False, **kw)


def _names(kernel) -> set[str]:
    return {s.name for s in kernel.tools.core()}


# ─────────────────────────────── choix du mode ───────────────────────────────


def test_mode_comes_from_config_then_env_then_mcp(monkeypatch):
    monkeypatch.delenv("NOKKER_MEMORY_MODE", raising=False)
    assert memory_mode(_cfg()) == "mcp"
    monkeypatch.setenv("NOKKER_MEMORY_MODE", "direct")
    assert memory_mode(_cfg()) == "direct"
    assert memory_mode(_cfg(memory_mode="off")) == "off"  # la config prime sur l'environnement
    with pytest.raises(ValueError):
        memory_mode(_cfg(memory_mode="sokkan"))


def test_mcp_mode_unchanged_and_off_is_explicit(monkeypatch):
    monkeypatch.delenv("NOKKER_MEMORY_MODE", raising=False)
    assert from_config(_cfg()) is None  # mcp, URL vide : aucune mémoire longue (bêta), comme avant
    assert from_config(_cfg(memory_mode="off", memory_mcp_url="http://x/mcp")) is None
    monkeypatch.setattr(cx.CorthexisMemory, "warm_up", lambda self: None)
    mem = from_config(_cfg(memory_mcp_url="http://127.0.0.1:1/mcp"))
    assert isinstance(mem, cx.CorthexisMemory) and mem.url == "http://127.0.0.1:1/mcp"


def test_direct_mode_builds_the_scoped_local_memory(fake_cx, monkeypatch):
    monkeypatch.setenv("CORTHEXIS_DATABASE_URL", "postgresql://u@pg/corthexis")
    mem = from_config(_cfg(memory_mode="direct"))
    assert isinstance(mem, DirectCorthexisMemory)
    assert mem.projects == (USER_PROJECT,)  # jamais « tout » : ni les fils s-…, ni les dépôts p-…
    assert (mem.score_threshold, mem.relative_floor, mem.min_rerank) == (0.15, 0.6, 0.35)
    mem2 = from_config(_cfg(memory_mode="direct", memory_direct={"projects": ["u-local", "notes"],
                                                                 "min_score": 0.2}))
    assert mem2.projects == ("u-local", "notes") and mem2.score_threshold == 0.2


def test_build_kernel_direct_is_never_a_silent_null_memory(fake_cx, monkeypatch):
    monkeypatch.setenv("NOKKER_MEMORY_MODE", "direct")
    fake_cx["rows"] = [{"note_name": "feedback-langue-ab12cd", "score": 0.31, "rerank": None,
                        "description": "Répondre en français", "snippet": "toujours en français"}]
    kernel = build_kernel(_cfg())
    assert isinstance(kernel.memory.internal, DirectCorthexisMemory)
    with tenant_scope(INTERNAL_TENANT):
        assert {"memory_search", "memory_get"} <= _names(kernel)
        hits = kernel.memory.recall("dans quelle langue répondre ?")
        assert [h.note for h in hits] == ["feedback-langue-ab12cd"]
        assert fake_cx["calls"][-1]["projects"] == ("u-local",)
    # Même cloisonnement qu'ailleurs : un testeur n'a ni rappel ni outils de la mémoire du propriétaire.
    n = len(fake_cx["calls"])
    with tenant_scope(TESTER):
        assert kernel.memory.recall("dans quelle langue répondre ?") == []
        assert not _names(kernel) & {"memory_search", "memory_get"}
    assert len(fake_cx["calls"]) == n


def test_lokkal_config_asks_for_direct_memory_and_corthexis_never_clear():
    cfg = load_config(str(ROOT / "deploy" / "lokkal" / "core-config.yaml"))
    assert memory_mode(cfg) == "direct" and cfg.memory_direct["projects"] == ["u-local"]
    assert cfg.context_corthexis["enabled"] is True and cfg.memory_mcp_url == ""


# ─────────────────────────────── pannes visibles ───────────────────────────────


def test_store_or_embed_down_is_logged_and_reported(fake_cx, caplog):
    mem = DirectCorthexisMemory(projects=("u-local",), relative_floor=0.6, score_threshold=0.15)
    assert mem.check()["ok"] is True
    fake_cx["store_error"] = 'connection to "postgresql://nokker_app:s3cr3t@postgres:5432/corthexis" failed'
    st = mem.check()
    assert st["ok"] is False and "base CortHeXis injoignable" in st["error"] and "s3cr3t" not in st["error"]
    fake_cx["store_error"], fake_cx["embed_error"] = None, "Connection refused"
    st = mem.check()
    assert st["ok"] is False and "embeddings" in st["error"]
    # Une recherche en panne (ligne `error`, jamais une exception de corthexis) : rappel vide, MAIS journal + état.
    fake_cx["embed_error"] = None
    fake_cx["rows"] = [{"error": "memory store unavailable: postgresql://u:pw@pg/db down"}]
    with caplog.at_level(logging.WARNING, logger="nokker.memory.corthexis"):
        assert mem.recall("une question") == []
    assert "rappel de la mémoire longue" in caplog.text and "pw@" not in caplog.text
    assert "store unavailable" in mem.check()["last_error"]


def test_missing_package_is_an_error_not_a_null_memory(monkeypatch):
    monkeypatch.setitem(sys.modules, "corthexis", None)  # import corthexis -> ImportError
    st = DirectCorthexisMemory().check()
    assert st["ok"] is False and "paquet corthexis absent" in st["error"]


def test_status_of_unwraps_the_kernel_ports(fake_cx):
    from nokker.tenancy.resources import TenantMemory

    mem = DirectCorthexisMemory(projects=("u-local",))
    pm, _ = make_memory(Path("/nonexistent-x1b"))
    assert status_of(TenantMemory(ProjectRecallMemory(mem, pm)))["ok"] is True
    assert status_of(TenantMemory(NullMemory())) == {"mode": "off", "ok": None}
    assert status_of(cx.CorthexisMemory(url="http://127.0.0.1:1/mcp")) == {"mode": "mcp", "ok": None}


def test_local_rule_keeps_the_relative_winner_and_drops_noise(fake_cx):
    mem = DirectCorthexisMemory(projects=("u-local",), score_threshold=0.15, relative_floor=0.6, min_rerank=0.35)
    fake_cx["rows"] = [
        {"note_name": "fact-a", "score": 0.30, "rerank": None, "description": "A", "snippet": "a"},
        {"note_name": "fact-b", "score": 0.20, "rerank": None, "description": "B", "snippet": "b"},  # 0,20 ≥ 0,18
        {"note_name": "fact-c", "score": 0.16, "rerank": None, "description": "C", "snippet": "c"},  # < 60 % du 1er
        {"note_name": "fact-d", "score": 0.05, "rerank": 0.8, "description": "D", "snippet": "d"},  # reranker sûr
    ]
    out = mem.recall("question", k=5)
    assert [r.note for r in out] == ["fact-a", "fact-b", "fact-d"]
    assert out[0].text == "A — extrait : a"


# ─────────────────────────────── cohabitation avec la mémoire de projet ───────────────────────────────


def test_long_and_project_memory_never_duplicate_a_note_in_the_window(tmp_path):
    class Long:
        def recall(self, query, k=5):
            return [Recall(note="feedback-langue-1", text="long", score=0.4),
                    Recall(note="rule-tests-2", text="long", score=0.5),
                    Recall(note="fact-hors-projet", text="long", score=0.3)]

        def propose(self, thread_id, facts):
            pass

    repo = make_repo(tmp_path)
    pm, _ = make_memory(tmp_path)
    pm.open_session("th_1", repo, tmp_path)
    tools = ProjectMemoryTools(pm)
    said = "pour tous mes projets : lance toujours les tests avant de conclure"
    pm.core_block("th_1", 0, said)
    r = tools.call("memory_note", {"kind": "rule", "title": "Tests", "text": "lancer les tests avant de conclure",
                                   "derived_from": "user", "scope": "user", "quote": said}, ctx(turn=0))
    assert not r.is_error, r.content
    rule = r.content.split()[2]
    pm.core_block("th_1", 1, "suite")
    assert rule in pm.core_names("th_1")  # règle de portée user : dans le noyau

    class LongWithCore(Long):
        def recall(self, query, k=5):
            return [*super().recall(query, k), Recall(note=rule, text="long", score=0.9)]

    port = ProjectRecallMemory(LongWithCore(), pm)
    pm.recall = lambda thread_id, query, k=5: [Recall(note="feedback-langue-1", text="projet", score=0.6)]
    hits = port.recall_for("th_1", "tests", 5)
    names = [h.note for h in hits]
    assert len(names) == len(set(names))  # aucune note deux fois
    assert rule not in names  # déjà dans le noyau : pas répétée dans le rappel
    assert hits[0].note == "feedback-langue-1" and hits[0].score == 0.6  # le meilleur des deux gardé


def test_user_note_without_code_session_feeds_the_long_memory(tmp_path):
    pm, engine = make_memory(tmp_path)
    tools = ProjectMemoryTools(pm)
    said = "retiens pour toujours : mon fuseau horaire est Europe/Zurich"
    pm.core_block("th_web", 3, said)  # le Context Engine l'appelle à chaque tour, session de code ou non
    refused = tools.call("memory_note", {"kind": "fact", "title": "Fuseau", "text": "Europe/Zurich",
                                         "derived_from": "user"}, ctx("th_web", 3))
    assert refused.is_error and "portée user" in refused.content
    ok = tools.call("memory_note", {"kind": "fact", "title": "Fuseau horaire", "text": "Europe/Zurich",
                                    "derived_from": "user", "scope": "user", "quote": "mon fuseau horaire est "
                                    "Europe/Zurich"}, ctx("th_web", 3))
    assert not ok.is_error, ok.content
    assert engine.enqueued[-1] == USER_PROJECT and len(list((tmp_path / "pm" / USER_PROJECT).glob("*.md"))) == 1
    listed = tools.call("memory", {"action": "list"}, ctx("th_web", 4))
    assert not listed.is_error and '"project": null' in listed.content and "Fuseau horaire" in listed.content


def test_install_catches_up_the_user_notes_index(tmp_path):
    pm, engine = make_memory(tmp_path)
    (tmp_path / "pm" / USER_PROJECT).mkdir(parents=True)

    class K:
        invariants = types.SimpleNamespace(scrub_output=lambda s: s)
        context = types.SimpleNamespace()
        memory = NullMemory()

    install(K(), pm)
    assert engine.enqueued == [USER_PROJECT]


# ─────────────────────────────── routes d'état ───────────────────────────────


def test_healthz_and_local_memory_route_report_the_long_memory(fake_cx, tmp_path, monkeypatch):
    import threading
    from http.server import ThreadingHTTPServer

    from nokker.api.server import make_handler
    from nokker.local.api import LOCAL_OWNER
    from tests.unit.test_local_api import _api, _req

    monkeypatch.setenv("NOKKER_MEMORY_MODE", "direct")
    kernel = build_kernel(_cfg())
    api = _api(tmp_path)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(kernel, {api.token: LOCAL_OWNER}, local_api=api))
    port = httpd.server_address[1]
    api.allowed_hosts = frozenset({f"127.0.0.1:{port}"})
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        assert _req(port, "/healthz")[1]["memory"] == "ok"
        assert _req(port, "/v1/local/memory")[0] == 401
        status, body = _req(port, "/v1/local/memory", token=api.token)
        assert status == 200 and body["mode"] == "direct" and body["ok"] is True
        assert body["projects"] == ["u-local"] and body["store"]["notes"] == 3
        fake_cx["embed_error"] = "Connection refused"
        kernel.memory.internal._status = None  # cache de 15 s : état relu
        assert _req(port, "/healthz")[1]["memory"] == "down"  # le noyau répond toujours 200
    finally:
        httpd.shutdown()
