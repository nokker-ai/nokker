"""`nokker improve run|nightly|history|revert` (docs/SELF-IMPROVE.md §7). Commande d'opérateur, lancée la nuit (timer) :
elle n'écrit que son compte rendu et, avec `--propose`, une carte. Drapeau `self_improve` éteint : refus, code 2.

    nokker improve run --target tools --demo                      # démonstration, faux modèle, 0 réseau
    nokker improve run --target router --traces LOTS/ --budget 1.5 --propose
    nokker improve run --target tools --tools-from-kernel --tasks TACHES.yaml --traces LOTS/ --propose
    nokker improve nightly --traces LOTS/ --propose               # tools, skills, router ; jamais les blocs
    nokker improve history --target tools
    nokker improve revert --target tools --version 0              # plus aucune variante appliquée
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from nokker.improve import TARGETS

REPO = Path(__file__).resolve().parents[2]
DEMO = REPO / "bench" / "improve"
# Bancs par défaut : `trace-choice` (tâches tirées de la moitié « évaluation » des traces, disjointe par fil de celle que
# lit la réflexion) quand aucune tâche n'est donnée ; `tool-choice` / `skill-choice` avec --tasks (ou --demo).
DEFAULT_BENCHES = {"tools": ["choice", "incidents"], "skills": ["choice"],
                   "router": ["route-sim", "route-labels", "incidents"], "blocks": ["incidents"]}
MIN_TRACE_TASKS = 4


def _err(msg: str, code: int = 1) -> int:
    print(f"[erreur: {msg}]", file=sys.stderr)
    return code


def _settings(config: Any) -> dict[str, Any]:
    return dict(getattr(config, "self_improve_settings", None) or {})


def _prices(settings: dict[str, Any]) -> Any:
    from nokker.improve.benches import Prices

    p = Prices(in_eur_per_mtok=float(settings.get("price_in_eur_per_mtok", 0.5)),
               out_eur_per_mtok=float(settings.get("price_out_eur_per_mtok", 1.5)))
    p.tier_call_eur.update({k: float(v) for k, v in (settings.get("tier_call_eur") or {}).items()})
    return p


def _complete_fn(config: Any, tier: str, run_id: str) -> Any:
    """Appel d'un palier de la config (OpenAI-compatible), hors noyau : aucune trace, aucun outil."""
    from nokker.providers.sokkan import SokkanProvider

    t = config.tiers.get(tier)
    if t is None:
        raise SystemExit(f"palier {tier!r} absent de la config")
    provider = SokkanProvider(base_url=t.base_url, api_key=t.api_key(), model=t.model)

    def complete(messages: list[dict[str, str]]) -> tuple[str, int, int]:
        comp = provider.complete(messages, [], reasoning=False, session_key=run_id, max_tokens=2048)
        return str(comp.message.content or ""), int(comp.usage.prompt), int(comp.usage.completion)

    return complete


def _tool_catalog(args: argparse.Namespace, config: Any) -> list[Any]:
    from nokker.core.ports import ToolSpec

    if args.tools_file:
        raw = json.loads(Path(args.tools_file).read_text(encoding="utf-8"))
        return [ToolSpec(name=t["name"], description=t["description"], parameters=t.get("parameters") or {})
                for t in raw]
    if args.tools_from_kernel:
        from nokker.api.server import build_kernel

        reg = getattr(build_kernel(config).tools, "internal", None)
        reg.overlay = None  # le catalogue des sources ; le calque actif est reposé plus bas, comme en production
        reg.refresh()
        return list(reg._all())
    from nokker.tools.registry import builtin_core_source

    return list(builtin_core_source().specs())


def _units(args: argparse.Namespace, config: Any, store: Any) -> tuple[dict[str, Any], Any, list[Any]]:
    """(valeurs de départ par unité, empreinte de la base, catalogue d'outils) — l'état de PRODUCTION : sources + calque
    approuvé actif."""
    from nokker.improve.overlay import apply_tool_overlay, router_base, sha256_of, tool_value

    if args.target == "tools":
        catalog = apply_tool_overlay(_tool_catalog(args, config), store.active("tools"))
        return {s.name: tool_value(s) for s in catalog}, sha256_of(store.active("tools")), catalog
    if args.target == "router":
        route = dict(getattr(config, "route", None) or {})
        base = router_base(None)
        base.update({k: route[k] for k in ("deep_tool_failures", "code_to_deep") if k in route})
        base.update(store.active("router"))
        return {"route": base}, sha256_of(store.active("router")), []
    if args.target == "skills":
        from nokker.learning.store import FileLearningStore
        from nokker.tools.skills import parse_frontmatter

        local = dict(getattr(config, "local", None) or {})
        base_dir = Path(local.get("state_dir") or "~/.config/nokker").expanduser()
        lstore = FileLearningStore(Path(local.get("learning_dir") or base_dir / "learning").expanduser())
        units = {}
        for name in lstore.skill_names(args.account):
            v = lstore.skill_versions(args.account, name)[-1]
            if v.active:
                meta, body = parse_frontmatter(v.content)
                units[name] = {"description": str(meta.get("description") or ""), "body": body.strip()}
        return units, sha256_of(units), []
    units = {p.name: p.read_text(encoding="utf-8") for p in sorted((REPO / "nokker" / "prompts").glob("*.md"))}
    return units, sha256_of(units), []


def _validator(target: str, base_values: dict[str, Any]) -> Any:
    from nokker.improve.overlay import validate_router_value, validate_tool_value

    def validate(key: str, value: Any) -> Any:
        if target == "tools":
            return validate_tool_value(base_values[key], value)
        if target == "router":
            return {**base_values[key], **validate_router_value(value)}
        if target == "skills":
            desc, body = " ".join(str(value.get("description", "")).split()), str(value.get("body", "")).strip()
            if not desc or len(desc) > 200 or not body or len(body) > 16000:
                raise ValueError("skill : description d'une ligne (≤ 200) et corps (≤ 16 000) attendus")
            return {"description": desc, "body": body}
        if not isinstance(value, str) or not value.strip() or len(value) > 20000:
            raise ValueError("bloc : texte non vide ≤ 20 000 caractères attendu")
        return value
    return validate


def _benches(args: argparse.Namespace, base_values: dict[str, Any], catalog: list[Any], chooser: Any,
             prices: Any, held_tasks: list[dict[str, Any]]) -> list[Any]:
    import yaml

    from nokker.improve import benches as b

    names = args.bench or DEFAULT_BENCHES[args.target]
    out: list[Any] = []
    for raw in names:
        name, command = b.parse_bench_arg(raw)
        if name == "choice":
            name = ("tool-choice" if args.target == "tools" else "skill-choice") if (args.tasks or args.demo) \
                else "trace-choice"
        index = b.tool_index_fn(catalog) if args.target == "tools" else b.skill_index_fn(base_values)
        if command is not None:
            out.append(b.CommandBench(name=name, command=command, fmt="keys" if name == "cmd:keys" else "json",
                                      corpus_paths=list(args.corpus or [])))
        elif name in ("tool-choice", "skill-choice"):
            tasks = args.tasks or (DEMO / "tool-choice.yaml" if args.demo and args.target == "tools" else None)
            if tasks is None:
                raise SystemExit(f"{name} : --tasks requis (tâches qui correspondent au catalogue)")
            out.append(b.ChoiceBench(name, args.target, b.ChoiceBench.load_tasks(tasks), index, chooser, prices))
        elif name == "trace-choice":
            if len(held_tasks) < MIN_TRACE_TASKS:
                raise SystemExit(f"trace-choice : {len(held_tasks)} tâche(s) tirée(s) des traces, il en faut "
                                 f"{MIN_TRACE_TASKS} (plus de traces, ou --tasks)")
            out.append(b.ChoiceBench("trace-choice", args.target, held_tasks, index, chooser, prices))
        elif name == "route-sim":
            eps = yaml.safe_load(Path(args.episodes or DEMO / "route-episodes.yaml").read_text(encoding="utf-8"))
            out.append(b.RouteSimBench(episodes=eps, base=base_values["route"], prices=prices))
        elif name == "route-labels":
            labels = REPO / "bench" / "route" / "labels.yaml"
            if labels.exists():  # banc de la branche p2/routing ; absent avant sa fusion
                out.append(b.RouteLabelsBench(rows=yaml.safe_load(labels.read_text(encoding="utf-8")),
                                              base=base_values["route"]))
        elif name == "incidents":
            if not args.no_incidents:
                out.append(b.IncidentsBench())
        else:
            raise SystemExit(f"banc inconnu : {raw}")
    out = [x for x in out if args.target in x.targets]
    if not any(getattr(x, "name", "") != "incidents" for x in out) and args.target != "blocks":
        raise SystemExit("aucun banc de qualité pour cette cible")
    return out


def run_target(args: argparse.Namespace, config: Any) -> dict[str, Any]:
    """Une cible, une nuit : rend le compte rendu (dict). Lève SystemExit avec un motif si la cible ne peut pas tourner."""
    from nokker.improve import guards, reflect
    from nokker.improve import traces as tr
    from nokker.improve.benches import LexicalChooser, ModelChooser
    from nokker.improve.loop import Budget, run_loop
    from nokker.improve.proposal import build_proposal, request_card, write_run
    from nokker.improve.store import ImproveStore
    from nokker.qa.secrets import KnownSecrets

    if args.demo:
        args.fake = True
        args.tools_file = args.tools_file or str(DEMO / "tools-demo.json")
        args.traces = args.traces or [str(REPO / "examples" / "improve" / "traces-demo")]
    settings = _settings(config)
    store = ImproveStore.from_config(config)
    run_id = f"improve-{args.target}-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}"
    prices = _prices(settings)
    if args.fake:
        reflector: Any = reflect.FakeReflector()
        chooser: Any = LexicalChooser()
    else:
        reflector = reflect.ModelReflector(_complete_fn(config, args.reflect_tier, run_id))
        chooser = ModelChooser(_complete_fn(config, args.choose_tier, run_id))
    reflector.prices = prices
    base_values, base_sha, catalog = _units(args, config, store)
    if not base_values:
        raise SystemExit(f"aucune unité à faire muter pour la cible {args.target}")
    # Corpus de TOUS les bancs connus du dépôt (et de --corpus) : une trace qui les recoupe est écartée, une variante qui
    # les recopie est rejetée.
    corpus = guards.Corpus.from_files([p for p in (REPO / "bench" / "conv" / "incidents",
                                                   REPO / "bench" / "conv" / "scenarios", REPO / "bench" / "improve",
                                                   REPO / "bench" / "route", *(map(Path, args.corpus or [])))
                                       if p.exists()])
    turns, counts = tr.turns_from_lines(tr.iter_lines(args.traces or []), corpus=corpus,
                                        testers_with_consent=bool(settings.get("testers_with_consent", False)))
    uses_traces_bench = args.target in ("tools", "skills") and not (args.tasks or args.demo) and \
        "choice" in (args.bench or DEFAULT_BENCHES[args.target])
    refl_turns, held_turns = tr.split_turns(turns) if uses_traces_bench else (turns, [])
    held = tr.tasks_from_turns(args.target, held_turns, base_values) if uses_traces_bench else []
    benches = _benches(args, base_values, catalog, chooser, prices, held)
    for bench in benches:
        if bench.name != "trace-choice":
            for text in bench.corpus_texts():
                corpus.add(text)
    examples = tr.failures_for(args.target, refl_turns, base_values)
    log = (lambda m: print(m, file=sys.stderr)) if not args.quiet else (lambda _m: None)
    log(f"{run_id} : {counts.get('turns', 0)} tours lus, {counts.get('failed_turns', 0)} en échec ; écartés : "
        f"banc {counts.get('excluded_bench', 0)}, recoupement de banc {counts.get('excluded_bench_overlap', 0)}, "
        f"testeurs {counts.get('excluded_tester', 0)}" + (f" ; tâches tirées des traces {len(held)}" if held else ""))
    known = KnownSecrets.load(env_prefix=config.known_secrets_env_prefix, file_path=config.known_secrets_file)
    budget = Budget(limit_eur=float(args.budget if args.budget is not None else settings.get("budget_eur", 2.0)),
                    max_candidates=int(args.max_candidates or settings.get("max_candidates", 12)))
    run = run_loop(args.target, base_values, examples, reflector, benches, budget, corpus=corpus, known=known,
                   validate=_validator(args.target, base_values),
                   skill_scan=guards.default_skill_scan() if args.target == "skills" else None, seed=args.seed,
                   log=log)
    run.trace_counts = dict(counts)
    proposal = build_proposal(run, base_sha256=base_sha, account=args.account if args.target == "skills" else None)
    out_dir = Path(args.out) / args.target if args.out and getattr(args, "nightly", False) else \
        Path(args.out) if args.out else store.root / "runs" / run_id
    files = write_run(run, proposal, out_dir)
    report: dict[str, Any] = {"run": run_id, "target": args.target, "stopped": run.stopped,
                              "spent_eur": round(run.spent_eur, 6), "front": run.front, "files": files,
                              "proposal": None}
    if proposal is not None:
        report["proposal"] = {"id": proposal.id, "candidate": proposal.candidate, "before": proposal.scores_before,
                              "after": proposal.scores_after, "frozen": proposal.frozen}
        if args.propose:
            from nokker.api.server import build_approvals

            if not config.approvals_dsn():
                report["card"] = "non posée : aucune base d'approbations (approvals_dsn) ; la proposition reste " \
                                 "dans le compte rendu"
            else:
                report["card"] = request_card(build_approvals(config), proposal)
        if args.show_diff:
            report["diff"] = proposal.diff
    return report


def _flag_off(config: Any) -> bool:
    return not getattr(config, "self_improve", False)


def _cmd_run(args: argparse.Namespace) -> int:
    from nokker.config import load_config

    config = load_config(args.config)
    if _flag_off(config):
        return _err("drapeau self_improve éteint : rien ne tourne (docs/SELF-IMPROVE.md)", 2)
    try:
        report = run_target(args, config)
    except SystemExit as e:
        return _err(str(e))
    diff = report.pop("diff", None)
    print(json.dumps(report, ensure_ascii=False, indent=1))
    if diff:
        print(diff)
    return 0


NIGHTLY_TARGETS = ("tools", "skills", "router")


def _cmd_nightly(args: argparse.Namespace) -> int:
    """La nuit : tools, skills puis router, budget partagé (ce qui reste passe à la cible suivante), une carte par cible
    qui a une proposition. Une cible qui ne peut pas tourner (pas de traces, pas d'apprentissage) est sautée avec son
    motif. Les blocs ne sont jamais dans la nuit (gel du format) : `run --target blocks` à la main."""
    import copy

    from nokker.config import load_config

    config = load_config(args.config)
    if _flag_off(config):
        return _err("drapeau self_improve éteint : rien ne tourne (docs/SELF-IMPROVE.md)", 2)
    total = float(args.budget if args.budget is not None else _settings(config).get("budget_eur", 2.0))
    left, reports = total, []
    for i, target in enumerate(NIGHTLY_TARGETS):
        a = copy.copy(args)
        a.target, a.nightly, a.bench, a.tasks = target, True, None, None
        a.tools_from_kernel = not args.tools_file
        a.budget = left / (len(NIGHTLY_TARGETS) - i)
        try:
            rep = run_target(a, config)
        except SystemExit as e:
            reports.append({"target": target, "skipped": str(e)})
            continue
        left = max(0.0, left - rep["spent_eur"])
        rep.pop("diff", None)
        reports.append(rep)
    print(json.dumps({"budget_eur": total, "spent_eur": round(total - left, 6), "targets": reports},
                     ensure_ascii=False, indent=1))
    return 0


def _cmd_history(args: argparse.Namespace) -> int:
    from nokker.config import load_config
    from nokker.improve.store import ImproveStore

    config = load_config(args.config)
    if _flag_off(config):
        return _err("drapeau self_improve éteint", 2)
    for v in ImproveStore.from_config(config).versions(args.target):
        print(json.dumps(v.to_json(overlay=False), ensure_ascii=False, sort_keys=True))
    return 0


def _cmd_revert(args: argparse.Namespace) -> int:
    from nokker.config import load_config
    from nokker.improve.store import ImproveStore

    config = load_config(args.config)
    if _flag_off(config):
        return _err("drapeau self_improve éteint", 2)
    try:
        v = ImproveStore.from_config(config).revert(args.target, args.version)
    except LookupError as e:
        return _err(str(e))
    print(f"{args.target} : version {v.version} (retour à {args.version}) ; effet au prochain démarrage du noyau")
    return 0


def _common(r: argparse.ArgumentParser) -> None:
    r.add_argument("--budget", type=float, default=None, help="EUR pour la nuit (défaut : self_improve_settings)")
    r.add_argument("--max-candidates", type=int, default=None, help="par cible")
    r.add_argument("--traces", action="append", help="lot(s) de `nokker traces export` (répétable)")
    r.add_argument("--no-incidents", action="store_true", help="sans le carnet d'incidents (plus rapide, moins sûr)")
    r.add_argument("--episodes", help="épisodes de route-sim (défaut : bench/improve/route-episodes.yaml)")
    r.add_argument("--tools-file", help="catalogue d'outils (JSON [{name, description, parameters}])")
    r.add_argument("--corpus", action="append", help="dossier de banc externe à protéger (ex. le banc B-1)")
    r.add_argument("--account", default="local-owner", help="compte des skills appris (Lokkal : local-owner)")
    r.add_argument("--fake", action="store_true", help="faux modèle (reflecteur et choix déterministes, 0 réseau)")
    r.add_argument("--reflect-tier", default="deep", help="palier de la config qui réfléchit (défaut : deep)")
    r.add_argument("--choose-tier", default="ship", help="palier de la config noté sur les bancs de choix (défaut : ship)")
    r.add_argument("--propose", action="store_true", help="pose la carte d'approbation de la meilleure variante")
    r.add_argument("--out", help="dossier du compte rendu (défaut : <état>/improve/runs/<passage>)")
    r.add_argument("--seed", type=int, default=0)
    r.add_argument("--show-diff", action="store_true")
    r.add_argument("--quiet", action="store_true")


def add_improve_subparser(sub: Any) -> None:
    p = sub.add_parser("improve", help="auto-amélioration hors ligne sous carte (docs/SELF-IMPROVE.md), drapeau "
                                       "self_improve")
    s = p.add_subparsers(dest="improve_command", required=True)
    r = s.add_parser("run", help="une cible : réflexion sur les traces, variantes notées sur les bancs, proposition")
    r.add_argument("--target", required=True, choices=TARGETS, help="blocks : proposition seulement (gel du format)")
    r.add_argument("--bench", action="append", help="choice | tool-choice | skill-choice | trace-choice | route-sim | "
                                                    "route-labels | incidents | cmd:<nom>=<commande> (répétable)")
    r.add_argument("--tasks", help="tâches de choix (YAML) pour tool-choice / skill-choice")
    r.add_argument("--tools-from-kernel", action="store_true", help="catalogue du noyau construit depuis la config")
    r.add_argument("--demo", action="store_true", help="démonstration : --fake, catalogue, tâches et traces de démo")
    _common(r)
    r.set_defaults(func=_cmd_run, unscoped=True, demo=False, nightly=False)
    n = s.add_parser("nightly", help="la nuit : tools, skills, router (budget partagé, une carte par cible)")
    _common(n)
    n.set_defaults(func=_cmd_nightly, unscoped=True, demo=False, nightly=True, tasks=None, bench=None,
                   tools_from_kernel=False)
    h = s.add_parser("history", help="versions appliquées d'une cible")
    h.add_argument("--target", required=True, choices=("tools", "router"))
    h.set_defaults(func=_cmd_history, unscoped=True)
    v = s.add_parser("revert", help="revient à une version (0 = plus aucune variante) ; une version de plus est créée")
    v.add_argument("--target", required=True, choices=("tools", "router"))
    v.add_argument("--version", required=True, type=int)
    v.set_defaults(func=_cmd_revert, unscoped=True)


__all__ = ["add_improve_subparser", "run_target"]
