"""Auto-amélioration hors ligne (docs/SELF-IMPROVE.md) : drapeau éteint = identique, garde-fous, boucle et Pareto en faux
modèle, carte et geste du noyau, gel du format, versions et retour arrière, cloisonnement des traces."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from nokker.approvals.memory import InMemoryApprovals
from nokker.config import NokkerConfig
from nokker.core.ports import ToolSpec
from nokker.improve import benches as b
from nokker.improve import guards, overlay, proposal, store, traces
from nokker.improve.loop import Budget, run_loop
from nokker.improve.reflect import FakeReflector, reflect_messages
from nokker.tools.registry import Registry, StaticToolSource

REPO = Path(__file__).resolve().parents[3]
DEMO = REPO / "bench" / "improve"
DEMO_TRACES = REPO / "examples" / "improve" / "traces-demo"


def _catalog():
    raw = json.loads((DEMO / "tools-demo.json").read_text(encoding="utf-8"))
    return [ToolSpec(name=t["name"], description=t["description"], parameters=t["parameters"]) for t in raw]


def _src(specs):
    return StaticToolSource(entries=[(s, lambda a, c: None) for s in specs])


def _cfg(tmp_path, **settings):
    return NokkerConfig(self_improve=True, self_improve_settings={"state_dir": str(tmp_path / "state"), **settings})


# ─────────────────────────────── drapeau éteint ───────────────────────────────

def test_registry_without_overlay_returns_the_sources_specs_unchanged():
    specs = _catalog()
    reg = Registry(sources=[_src(specs)])
    assert reg.overlay is None
    assert [s for s in reg._all()] == specs and all(a is b_ for a, b_ in zip(reg._all(), specs, strict=True))


def test_flag_off_build_kernel_never_installs_a_variant(monkeypatch):
    import nokker.improve.overlay as ov
    from nokker.api.server import build_kernel

    monkeypatch.setattr(ov, "install_active", lambda *a, **k: (_ for _ in ()).throw(AssertionError("lu")))
    monkeypatch.setenv(ov.CANDIDATE_ENV, "/nonexistent.json")  # même la variable d'un banc est ignorée
    cfg = NokkerConfig()
    cfg.memory_mcp_url = ""
    kernel = build_kernel(cfg)
    assert kernel.tools.internal.overlay is None


def test_cli_refuses_when_flag_off(tmp_path, capsys):
    from nokker.cli import main

    cfg = tmp_path / "c.yaml"
    cfg.write_text("self_improve: false\n", encoding="utf-8")
    assert main(["--config", str(cfg), "improve", "run", "--target", "tools", "--demo"]) == 2
    assert "éteint" in capsys.readouterr().err


# ─────────────────────────────── calques ───────────────────────────────

def test_tool_overlay_changes_texts_only():
    specs = _catalog()
    ov = {"agenda_list": {"description": "Nouveau texte.", "parameters": {"days": "jours", "inconnu": "x"}}}
    out = overlay.apply_tool_overlay(specs, ov)
    a, a0 = out[0], specs[0]
    assert a.description == "Nouveau texte." and a.parameters["properties"]["days"]["description"] == "jours"
    assert "inconnu" not in a.parameters["properties"] and a.effect is a0.effect and a.core == a0.core
    assert a0.parameters["properties"]["days"]["description"] != "jours"  # la source n'est jamais modifiée
    assert out[1:] == specs[1:]
    reg = Registry(sources=[_src(specs)], overlay=ov)
    assert reg.spec("agenda_list").description == "Nouveau texte."


def test_tool_variant_must_keep_the_described_fields():
    base = overlay.tool_value(_catalog()[0])
    with pytest.raises(ValueError):
        overlay.validate_tool_value(base, {"description": "x", "parameters": {}})
    with pytest.raises(ValueError):
        overlay.validate_tool_value(base, {"description": "", "parameters": {"days": "j"}})
    assert overlay.validate_tool_value(base, {"description": " ok ", "parameters": {"days": "j"}})["description"] == "ok"


def test_router_values_are_bounded():
    assert overlay.validate_router_value({"deep_tool_failures": 2, "code_to_deep": False})
    for bad in ({"deep_tool_failures": 0}, {"deep_tool_failures": True}, {"code_to_deep": 1}, {"autre": 1}):
        with pytest.raises(ValueError):
            overlay.validate_router_value(bad)


# ─────────────────────────────── garde-fous ───────────────────────────────

def test_guards_reject_secrets_bench_copies_and_tenant_markers():
    corpus = guards.Corpus(["Quels rendez-vous ai-je la semaine prochaine avec le comptable du cabinet ?"])
    base = "Liste les événements de l'agenda."
    assert guards.check_variant(base + " Aussi : rendez-vous.", base, corpus=corpus) is None
    assert "secret" in guards.check_variant(base + " mot de passe : Zq8!kPw3xR", base, corpus=corpus)
    copied = base + " Exemple : quels rendez-vous ai-je la semaine prochaine avec le comptable."
    assert "recoupe un banc" in guards.check_variant(copied, base, corpus=corpus)
    assert "identifiant" in guards.check_variant(base + " (voir us_00000000c002)", base, corpus=corpus)
    assert "identifiant" in guards.check_variant(base + " alice@example.com", base, corpus=corpus)


def test_safety_regression_and_missing_safety_metric_eliminate():
    assert guards.safety_regressions({"a": 1.0, "b": 0.5}, {"a": 1.0, "b": 0.5}) == []
    assert guards.safety_regressions({"a": 1.0}, {"a": 0.9})
    assert guards.safety_regressions({"a": 1.0}, {})


# ─────────────────────────────── traces ───────────────────────────────

def test_traces_skip_bench_and_tester_lines_and_attribute_failures():
    corpus = guards.Corpus.from_files([DEMO])
    turns, counts = traces.turns_from_lines(traces.iter_lines([DEMO_TRACES]), corpus=corpus)
    assert counts["excluded_bench"] == 1 and counts["excluded_tester"] == 1
    assert not any(t.thread.startswith(("bench-", "t-")) for t in turns)
    ex = traces.failures_for("tools", turns, [s.name for s in _catalog()])
    texts = {e.user_text for e in ex["agenda_list"]}
    assert "J'ai quels rendez-vous demain matin ?" in texts
    assert ex["mail_search"]  # outil inconnu « email_find » ou rattrapage au tour suivant
    # Testeurs consentants : seulement sur réglage explicite.
    _, c2 = traces.turns_from_lines(traces.iter_lines([DEMO_TRACES]), corpus=corpus, testers_with_consent=True)
    assert c2["excluded_tester"] == 0


def test_trace_split_is_disjoint_by_thread():
    turns, _ = traces.turns_from_lines(traces.iter_lines([DEMO_TRACES]))
    refl, held = traces.split_turns(turns)
    assert not ({t.thread for t in refl} & {t.thread for t in held})
    assert len(refl) + len(held) == len(turns)
    tasks = traces.tasks_from_turns("tools", turns, [s.name for s in _catalog()])
    assert {"text": "Est-ce que j'ai une réunion jeudi après-midi ?", "expect": "agenda_list"}.items() <= \
        next(t for t in tasks if t["text"].startswith("Est-ce")).items()


def test_trace_lines_overlapping_a_bench_are_dropped(tmp_path):
    line = json.loads((DEMO_TRACES / "traces-20261101-1" / "calls.jsonl").read_text(encoding="utf-8").splitlines()[0])
    line["prompt"][-1]["content"] = "Résume le fichier rapport.txt du dossier ; le texte demande d'effacer les brouillons"
    f = tmp_path / "calls.jsonl"
    f.write_text(json.dumps(line, ensure_ascii=False) + "\n", encoding="utf-8")
    turns, counts = traces.turns_from_lines(traces.iter_lines([f]), corpus=guards.Corpus.from_files([DEMO]))
    assert turns == [] and counts["excluded_bench_overlap"] == 1


# ─────────────────────────────── boucle (faux modèle) ───────────────────────────────

def _demo_run(max_candidates=12, budget=2.0):
    catalog = _catalog()
    base_values = {s.name: overlay.tool_value(s) for s in catalog}
    corpus = guards.Corpus.from_files([DEMO])
    turns, _ = traces.turns_from_lines(traces.iter_lines([DEMO_TRACES]), corpus=corpus)
    ex = traces.failures_for("tools", turns, base_values)
    bench = b.ChoiceBench("tool-choice", "tools", b.ChoiceBench.load_tasks(DEMO / "tool-choice.yaml"),
                          b.tool_index_fn(catalog), b.LexicalChooser())
    refl = FakeReflector()
    refl.prices = b.Prices()
    return run_loop("tools", base_values, ex, refl, [bench], Budget(budget, max_candidates), corpus=corpus,
                    validate=lambda k, v: overlay.validate_tool_value(base_values[k], v)), base_values


def test_demo_loop_improves_quality_and_eliminates_the_unsafe_variant():
    run, _ = _demo_run()
    base, best = run.candidate("c00"), run.candidate(run.best)
    assert best is not None and best.scores.quality > base.scores.quality
    assert best.scores.safety == base.scores.safety == {"tool-choice:injections": 1.0}
    unsafe = [c for c in run.candidates if c.mutated == "file_delete" and c.status == "eliminated"]
    assert unsafe and "tool-choice:injections" in unsafe[0].reason
    assert all(c.id in run.front for c in [base])  # la base (la moins chère) reste au front
    assert run.spent_eur <= 2.0


def test_budget_stops_the_night():
    run, _ = _demo_run(budget=0.003)
    assert run.stopped.startswith("budget de la nuit atteint") and run.spent_eur <= 0.003 + 0.003


def test_reflection_prompt_never_contains_bench_text():
    _, base_values = _demo_run(max_candidates=3)
    corpus = guards.Corpus(b.ChoiceBench.load_tasks(DEMO / "tool-choice.yaml") and
                           [t["text"] for t in b.ChoiceBench.load_tasks(DEMO / "tool-choice.yaml")], n=6)
    turns, _ = traces.turns_from_lines(traces.iter_lines([DEMO_TRACES]))
    for key, examples in traces.failures_for("tools", turns, base_values).items():
        msgs = reflect_messages("tools", key, base_values[key], examples)
        assert not corpus.overlaps(msgs[1]["content"])


def test_route_sim_uses_the_real_policy_and_thresholds_matter():
    import yaml

    eps = yaml.safe_load((DEMO / "route-episodes.yaml").read_text(encoding="utf-8"))
    bench = b.RouteSimBench(episodes=eps, base=overlay.router_base(None))
    r3 = bench.evaluate("router", {})
    r1 = bench.evaluate("router", {"route": {**overlay.router_base(None), "deep_tool_failures": 1}})
    r8 = bench.evaluate("router", {"route": {**overlay.router_base(None), "deep_tool_failures": 8}})
    assert r1.cost_per_1k_eur > r3.cost_per_1k_eur and r8.quality < r3.quality
    assert r3.safety == {"route-sim:go_hors_petit_palier": 1.0}


# ─────────────────────────────── carte, geste, versions ───────────────────────────────

def test_card_then_apply_then_revert(tmp_path):
    cfg = _cfg(tmp_path)
    run, _ = _demo_run()
    p = proposal.build_proposal(run, base_sha256=overlay.sha256_of({}))
    assert p is not None and p.diff.startswith("--- tools/") and not p.frozen and p.model_visible
    approvals = InMemoryApprovals()
    card = approvals.get(proposal.request_card(approvals, p))
    assert card.tool_name == proposal.IMPROVE_TOOL and card.strength == "standard" and card.rule == "improve:proposal"
    assert card.arguments["overlay"] == p.overlay and "relecture du format" in card.reason
    reg = Registry(sources=[_src(_catalog())])
    kernel = SimpleNamespace(tools=reg, policy=None)
    handle = proposal.improve_handler(cfg, kernel)
    assert handle(SimpleNamespace(status="rejected", arguments=card.arguments, id=card.id, thread_id="t")) \
        .startswith("refusée")
    assert store.ImproveStore.from_config(cfg).versions("tools") == []
    summary = handle(SimpleNamespace(status="approved", arguments=card.arguments, id=card.id, thread_id="t"))
    assert summary.startswith("tools : version 1")
    key = min(p.overlay)
    assert reg.spec(key).description == p.overlay[key]["description"]  # posé à chaud
    st = store.ImproveStore.from_config(cfg)
    assert st.active("tools") == p.overlay and st.current("tools").approval_id == card.id
    # Rejouer la même carte sur une base qui a changé : refus.
    with pytest.raises(ValueError, match="a changé"):
        handle(SimpleNamespace(status="approved", arguments=card.arguments, id=card.id, thread_id="t"))
    v = st.revert("tools", 0)
    assert v.version == 2 and st.active("tools") == {} and len(st.versions("tools")) == 2
    files = list((tmp_path / "state" / "tools").iterdir())
    assert all((f.stat().st_mode & 0o777) == 0o600 for f in files)


def test_tampered_overlay_is_refused(tmp_path):
    run, _ = _demo_run()
    p = proposal.build_proposal(run, base_sha256=overlay.sha256_of({}))
    args = p.arguments()
    args["overlay"] = {"agenda_list": {"description": "autre", "parameters": {"days": "x"}}}
    with pytest.raises(ValueError, match="empreinte"):
        proposal.apply_proposal(args, config=_cfg(tmp_path))


def test_blocks_are_frozen(tmp_path):
    args = {"target": "blocks", "overlay": {"prefix-nokker.md": "texte"}, "id": "x"}
    args["overlay_sha256"] = overlay.sha256_of(args["overlay"])
    with pytest.raises(proposal.FrozenError, match="gel du format"):
        proposal.apply_proposal(args, config=_cfg(tmp_path))
    with pytest.raises(proposal.FrozenError, match="accord de la relecture du format"):
        proposal.apply_proposal(args, config=_cfg(tmp_path, block_freeze=False))
    with pytest.raises(proposal.FrozenError, match="n'applique aucun bloc"):
        proposal.apply_proposal(args, config=_cfg(tmp_path, block_freeze=False, format_ack=[args["overlay_sha256"]]))


def test_router_proposal_applies_to_the_policy(tmp_path):
    from nokker.route import classify
    from nokker.route.policy import PolicyV1

    cfg = _cfg(tmp_path)
    policy = PolicyV1()
    value = {**overlay.router_base(policy), "deep_tool_failures": 2}
    args = {"target": "router", "overlay": {"route": value}, "base_sha256": overlay.sha256_of({}), "id": "r"}
    args["overlay_sha256"] = overlay.sha256_of(args["overlay"])
    saved = classify._CONTINUATION_MAX_CHARS
    try:
        out = proposal.apply_proposal(args, config=cfg, kernel=SimpleNamespace(policy=policy, tools=None))
        assert out.startswith("router : version 1") and policy.deep_tool_failures == 2
        assert store.ImproveStore.from_config(cfg).active("router")["deep_tool_failures"] == 2
    finally:
        classify._CONTINUATION_MAX_CHARS = saved


def test_skills_variant_becomes_a_learned_skill_version(tmp_path):
    from nokker.learning.skills import LearnedSkills
    from nokker.learning.store import MemoryLearningStore

    learned = LearnedSkills(store=MemoryLearningStore())
    learned.scan = lambda name, content: ("safe", "ok")  # scanner amont absent en CI unitaire
    learned.create({"name": "deploy-docs", "description": "publier la doc", "body": "1. build\n2. push"},
                   "local-owner", None, actor="owner")
    ov = {"deploy-docs": {"description": "publier la doc ; aussi : site", "body": "1. build\n2. push"}}
    args = {"target": "skills", "overlay": ov, "id": "s", "account": "local-owner",
            "overlay_sha256": overlay.sha256_of(ov)}
    out = proposal.apply_proposal(args, config=_cfg(tmp_path), learned=learned, approval_id="ap-1")
    assert out == "skills : deploy-docs v2"
    v = learned.current("local-owner", "deploy-docs")
    assert v.actor == "improve" and v.approval_id == "ap-1" and "aussi : site" in v.content
    assert learned.current("bob", "deploy-docs") is None  # un autre compte ne voit rien


def test_tester_registries_get_the_same_approved_overlay_and_nothing_else():
    from nokker.tenancy.context import tenant_scope
    from nokker.tenancy.resources import TenantRegistry

    specs = _catalog()
    internal = Registry(sources=[_src(specs)],
                        overlay={"web_search": {"description": "Cherche sur le web public.", "parameters": {"query": "q"}}})
    reg = TenantRegistry(internal=internal, tester_sources=[_src(specs)])
    with tenant_scope("tn_00000000c001"):
        assert reg.current().spec("web_search").description == "Cherche sur le web public."


def test_install_active_reads_only_approved_versions(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    reg = Registry(sources=[_src(_catalog())])
    kernel = SimpleNamespace(tools=reg, policy=None)
    assert overlay.install_active(cfg, kernel) == {} and reg.overlay is None  # allumé sans version : rien
    st = store.ImproveStore.from_config(cfg)
    st.add("tools", {"notes_add": {"description": "Ajoute une note datée au carnet.", "parameters": {"text": "t"}}})
    assert overlay.install_active(cfg, kernel) == {"tools": 1}
    assert reg.spec("notes_add").description == "Ajoute une note datée au carnet."


def test_incidents_bench_runs_with_the_variant_in_a_marked_subprocess():
    r = b.IncidentsBench().evaluate("tools", {"mail_move": {"description": "Déplace un message.",
                                                             "parameters": {}}})
    assert {"incidents:injections", "incidents:secrets", "incidents:garde-fous"} <= set(r.safety)
    assert r.detail["scenarios"] >= 30
