"""`nokker` CLI — PLAN-P1 §5.1 (F2) : `nokker serve` (API), `nokker chat "<message>"` (un tour local),
`nokker costs` (F6, branché via `nokker.ledger.cli.add_costs_subparser`), `nokker devices approve <code>`
(P1, docs/APPROVALS.md §3.1 : confirmation locale d'un appareil `pending` — chemin de confiance qui ne
dépend d'aucune identité réseau, celui qui a un shell root sur l'hôte EST déjà un opérateur de confiance).
`nokker erase` / `nokker devices remove <code>` : effacement d'un compte testeur, retrait d'un appareil
(nokker/admin/erase.py, à blanc par défaut). `nokker secret set|list|rm`, `nokker vault migrate|status` : coffre local
(nokker/secrets/cli.py, lot X4). `nokker migrate` : migrations Postgres en propriétaire (job d'init de la bêta, `NOKKER_PG_OWNER_DSN`).
Volontairement minimal pour le reste (pas de `setup`/`doctor` — chantiers périphérie, §5.5).

La CLI est une frontière (docs/TENANCY.md §2) : les commandes qui touchent des données s'exécutent dans le locataire de
`--tenant` (défaut : l'interne, celui de l'opérateur qui a un shell sur l'hôte).
"""
from __future__ import annotations

import argparse
import logging
import sys
import uuid

from nokker.api.server import build_approvals, build_kernel, build_ledger, serve
from nokker.approvals.errors import ApprovalError, DeviceError
from nokker.config import load_config
from nokker.core.turn import TurnInput
from nokker.ledger.cli import add_costs_subparser
from nokker.store.pg import apply_migrations
from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope


def _cmd_serve(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    serve(config)
    return 0


def _cmd_chat(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    kernel = build_kernel(config)
    thread_id = args.thread or f"cli-{uuid.uuid4().hex[:8]}"
    # Pas de repli par défaut (sécurité, cf. api/server.py) : sans --sender explicite, aucune identité
    # de confiance -> aucun GO possible sur une action à effet pendant ce tour local.
    turn = kernel.run_turn(TurnInput(thread_id=thread_id, user_text=args.message, channel="cli",
                                     sender=args.sender))  # locataire : `main` (--tenant)
    if turn.error:
        print(f"[erreur: {turn.error}]", file=sys.stderr)
    print(turn.reply)
    return 1 if turn.error else 0


def _cmd_code(args: argparse.Namespace) -> int:
    """Une tâche de code dans un dossier (Nokker Lokkal L0, docs/LOKKAL.md), sans serveur : le noyau avec les outils
    de code (`nokker/tools/code.py`) et le préfixe de code, un tour jusqu'à la réponse. Sortie : la réponse sur
    stdout ; avec --json, un compte rendu (réponse, appels d'outils, appels modèles, jetons, erreur) pour le banc."""
    import dataclasses
    import json
    from pathlib import Path

    from nokker.context.project_memory import ProjectMemoryTools, code_prefix, from_local_config, install
    from nokker.qa.permissions import LocalPermissions, Mode, PermissionGates
    from nokker.tools.code import DEFAULT_SANDBOX_IMAGE, CodeToolSource

    config = load_config(args.config)
    local = config.local or {}
    # Mémoire de projet (X1) : `local.project_memory` de la config, comme le serveur local.
    project_memory = from_local_config(local, Path(local["state_dir"]).expanduser() if local.get("state_dir") else None)
    prefix = args.prefix or str(Path(__file__).parent / "prompts" / "prefix-lokkal-code.md")
    if not args.prefix:  # le préfixe nomme le point de montage (le vrai, pas /workspace) et la règle de la mémoire
        prefix = code_prefix(prefix, project_memory=project_memory is not None, mount=args.mount)
    config = dataclasses.replace(config, prefix_path=prefix, product_prefix_path=prefix, profile_path=None)
    task = args.task if args.task != "-" else sys.stdin.read()
    source = CodeToolSource(root=args.workspace, backend=args.backend, image=args.image or DEFAULT_SANDBOX_IMAGE,
                            network=not args.no_network, network_name=args.network_name, host_root=args.host_root,
                            mount=args.mount)
    from nokker.tools.checkpoints import checkpoint_sources, code_root_of

    from nokker.tools.delegate import DelegateToolSource

    wrapper, checkpoints = checkpoint_sources(source, code_root_of(source))  # photo avant chaque écriture
    parent: dict = {}
    hooks: dict = {}
    delegate = DelegateToolSource(  # sous-agents : copies du noyau parent sur le registre filtré (hooks : héritage
        lambda registry, thread_id, observer: dataclasses.replace(  # explicite seulement, voir nokker/tools/hooks.py)
            parent["k"], observer=observer, tools=hooks["r"].wrap(registry) if "r" in hooks else registry),
        source)
    memory_tools = [ProjectMemoryTools(project_memory)] if project_memory is not None else []
    kernel = build_kernel(config, extra_sources=[wrapper, checkpoints, delegate, *memory_tools])
    parent["k"] = kernel
    delegate.bind(getattr(kernel.tools, "internal", kernel.tools))
    kernel.max_calls = args.max_calls
    kernel.max_tokens = 16384
    kernel.budget_notice_at = None if args.no_budget_notice else 0.8
    kernel.stall_limit = args.stall_limit or None
    from nokker.qa.escalate import LocalEscalator

    kernel.escalator = LocalEscalator()
    if not args.no_reasoning:  # banc L0 (L01, 23.10) : sans raisonnement, le 397B pense dans sa réponse et s'arrête
        getattr(kernel.policy, "inner", kernel.policy).ship_reasoning = True
    # Permissions locales (L1, nokker/qa/permissions.py) devant les portes : réglages de l'utilisateur, puis du dépôt
    # (restreindre seulement), puis la ligne de commande.
    # Sans interface, personne ne répond à une carte : repli accept_edits (modifier le dossier, demander le reste).
    perms = LocalPermissions.load(mode=args.mode, allow=args.allow, fallback=Mode.ACCEPT_EDITS,
                                  user_settings=Path.home() / ".config" / "nokker" / "settings.json",
                                  project_settings=Path(args.workspace) / ".nokker" / "settings.json")
    kernel.gates = PermissionGates(inner=kernel.gates, permissions=perms)
    thread_id = args.thread or f"code-{uuid.uuid4().hex[:8]}"
    from datetime import datetime, timezone

    started = datetime.now(timezone.utc)
    if getattr(args, "hooks", False) or (config.local or {}).get("hooks", False):  # lot X3 : local.hooks ou --hooks
        from nokker.tools.hooks import HookRunner

        hooks["r"] = HookRunner(sandbox=source.run_hook, user_settings=Path.home() / ".config" / "nokker" /
                                "settings.json", scrub=kernel.invariants.scrub_output,
                                redact=kernel.invariants.redact_result, mode_of=perms.mode_for, background=False)
        kernel.tools = hooks["r"].wrap(kernel.tools)
        state = hooks["r"].open_session(thread_id, args.workspace)
        if state.get("notice"):
            print(f"[hooks] {state['notice']}", file=sys.stderr)
    if project_memory is not None:
        install(kernel, project_memory)
        project_memory.open_session(thread_id, args.workspace, args.workspace)
    try:
        turn = kernel.run_turn(TurnInput(thread_id=thread_id, user_text=task, channel="cli", sender=args.sender))
    finally:
        source.close()
        if project_memory is not None:
            project_memory.flush(30.0)  # l'index de fond finit avant la sortie du processus
    if args.json:
        def brief(c) -> dict:  # arguments coupés : assez pour rejouer un appel aux portes, pas un fichier entier
            return {"name": c.name, "arguments": {k: (v[:400] if isinstance(v, str) else v)
                                                  for k, v in (c.arguments or {}).items()}}

        tool_calls = [c.name for m in turn.messages for c in m.tool_calls]
        print(json.dumps({
            "thread_id": thread_id, "reply": turn.reply, "error": turn.error, "escalated": turn.escalated,
            "pending_approvals": [c.name for c in turn.pending_approvals], "tool_calls": tool_calls,
            "pending_calls": [brief(c) for c in turn.pending_approvals],
            "tool_calls_full": [brief(c) for m in turn.messages for c in m.tool_calls],
            "loop_rules": [v.rule for v in turn.verdicts if v.rule.startswith("loop:")],
            "llm_calls": [{"tier": c.tier.value, "model": c.model, "prompt": c.usage.prompt,
                           "completion": c.usage.completion, "reasoning": c.usage.reasoning,
                           "cached": c.usage.cached, "latency_s": c.latency_s, "reason": c.route_reason}
                          for c in turn.calls],
            "cost": _code_turn_cost(kernel, thread_id, turn, started),
        }, ensure_ascii=False, default=str))
    else:
        if turn.error:
            print(f"[erreur: {turn.error}]", file=sys.stderr)
        print(turn.reply)
    return 1 if turn.error else 0


def _code_turn_cost(kernel, thread_id: str, turn, started) -> dict:
    """Même corps que l'événement `turn.cost` (docs/LOKKAL.md §7.8) : les appels du tour, plus ceux de ses sous-agents
    (fils `<fil>/sa-…`, relus au ledger depuis le début de CE tour)."""
    from nokker.ledger.turn_cost import row_from_record, turn_cost

    try:
        children = [row_from_record(r) for r in kernel.ledger.fetch_calls(thread_id=thread_id, include_subagents=True)
                    if r.thread_id != thread_id and r.created_at >= started]
    except Exception:  # noqa: BLE001 — le compte rendu part sans les sous-agents plutôt que pas du tout
        children = []
    return {"thread_id": thread_id, "turn_id": f"{thread_id}:{turn.index}",
            **turn_cost(turn.calls, getattr(kernel.ledger, "prices", None), subagents=children)}
def _cmd_hooks(args: argparse.Namespace) -> int:
    """`nokker hooks show|trust` : les hooks du `.nokker/settings.json` d'un dépôt ne tournent qu'une fois déclarés de
    confiance par l'utilisateur pour CE dossier et CETTE empreinte (nokker/tools/hooks.py). `trust` exige l'empreinte
    recopiée : on accepte ce qu'on a relu, pas ce que le fichier est devenu depuis."""
    import json
    import os
    from pathlib import Path

    from nokker.tools.hooks import (
        hooks_sha256, is_trusted, parse_hooks, project_hooks_block, read_settings, trust_project_hooks,
    )

    home = os.environ.get("NOKKER_HOME")
    settings = Path(args.settings or (Path(home) / "settings.json" if home else
                                      Path.home() / ".config" / "nokker" / "settings.json")).expanduser()
    root = os.path.realpath(args.workspace)
    try:
        block = project_hooks_block(root)
        hooks = parse_hooks(block, "project")
    except ValueError as e:
        print(f"hooks du dépôt illisibles : {e}", file=sys.stderr)
        return 2
    if not hooks:
        print(f"aucun hook dans {root}/.nokker/settings.json")
        return 0 if args.hooks_command == "show" else 1
    sha = hooks_sha256(block)
    if args.hooks_command == "show":
        trusted = is_trusted(read_settings(settings), root, sha)
        print(json.dumps({"root": root, "sha256": sha, "trusted": trusted, "hooks": block},
                         ensure_ascii=False, indent=1))
        return 0
    if args.sha256 != sha:
        print(f"empreinte différente : le bloc actuel est {sha} (relis-le avec `nokker hooks show`)", file=sys.stderr)
        return 1
    trust_project_hooks(settings, root, sha)
    print(f"confiance enregistrée dans {settings} : {root} ({sha[:12]}…)")
    return 0


def _cmd_costs(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    ledger = build_ledger(config)
    return args._nokker_ledger_cmd(ledger, args)


def _cmd_migrate(args: argparse.Namespace) -> int:
    dsn = load_config(args.config).pg_dsn()
    if not dsn:
        print("[erreur: aucun DSN Postgres configuré]", file=sys.stderr)
        return 1
    apply_migrations(dsn, strict=True)  # sans les droits : exception, le job d'init échoue franchement
    print("migrations appliquées.")
    return 0


def _cmd_devices_approve(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    approvals = build_approvals(config)
    try:
        device = approvals.confirm_device_cli(args.code)
    except (ApprovalError, DeviceError) as e:
        print(f"[erreur: {e}]", file=sys.stderr)
        return 1
    print(f"appareil {device.id} ({device.name!r}, utilisateur {device.user}) : {device.status}.")
    return 0


def _admin_dsn(args: argparse.Namespace) -> str | None:
    dsn = load_config(args.config).pg_dsn()
    if not dsn:
        print("[erreur: aucun DSN Postgres configuré]", file=sys.stderr)
    return dsn


def _cmd_erase(args: argparse.Namespace) -> int:
    from nokker.admin.erase import EraseError, erase_account

    dsn = _admin_dsn(args)
    if not dsn:
        return 1
    try:
        report = erase_account(dsn, email=args.email, account_id=args.account, apply=args.apply, confirm=args.confirm,
                               images_root=load_config(args.config).images_root() or None)
    except EraseError as e:
        print(f"[erreur: {e}]", file=sys.stderr)
        return 1
    print(report.render())
    return 0


def _cmd_devices_remove(args: argparse.Namespace) -> int:
    from nokker.admin.erase import EraseError, remove_device

    dsn = _admin_dsn(args)
    if not dsn:
        return 1
    try:
        print(remove_device(dsn, args.code, apply=args.apply))
    except EraseError as e:
        print(f"[erreur: {e}]", file=sys.stderr)
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="nokker", description="Noyau Nokker — API et chat local.")
    p.add_argument("--config", default=None, help="chemin du config.yaml (défaut: $NOKKER_CONFIG ou /etc/nokker/config.yaml)")
    p.add_argument("--tenant", default=INTERNAL_TENANT,
                   help=f"locataire des commandes qui touchent des données (défaut: {INTERNAL_TENANT})")
    sub = p.add_subparsers(dest="command", required=True)

    serve_p = sub.add_parser("serve", help="démarre l'API /v1/chat/completions + /healthz + /metrics")
    serve_p.set_defaults(func=_cmd_serve, unscoped=True)

    migrate_p = sub.add_parser("migrate", help="applique les migrations Postgres (rôle propriétaire)")
    migrate_p.set_defaults(func=_cmd_migrate, unscoped=True)

    chat_p = sub.add_parser("chat", help="exécute un tour local (pas de serveur)")
    chat_p.add_argument("message")
    chat_p.add_argument("--thread", default=None, help="thread_id à réutiliser (défaut: nouveau)")
    chat_p.add_argument("--sender", default=None, help="identité de l'expéditeur (défaut: aucune -> pas de GO possible)")
    chat_p.set_defaults(func=_cmd_chat)

    code_p = sub.add_parser("code", help="tâche de code dans un dossier (Nokker Lokkal), sans serveur")
    code_p.add_argument("task", help="la consigne, ou - pour la lire sur stdin")
    code_p.add_argument("--workspace", default=".", help="dossier de travail (défaut : dossier courant)")
    code_p.add_argument("--backend", choices=["docker", "local"], default="docker",
                        help="docker (défaut) : bac à sable jetable ; local : sur l'hôte")
    code_p.add_argument("--image", default=None, help="image du bac à sable")
    code_p.add_argument("--host-root", default=None, help="chemin du dossier vu par le démon docker, s'il diffère")
    code_p.add_argument("--mount", default="/workspace",
                        help="chemin du dossier dans le bac à sable (banc SWE-bench : /testbed)")
    code_p.add_argument("--network-name", default=None, help="réseau docker du bac à sable (compose : nokker-sandbox)")
    code_p.add_argument("--no-network", action="store_true", help="bac à sable sans réseau")
    code_p.add_argument("--max-calls", type=int, default=60, help="appels modèle par tour (défaut 60)")
    code_p.add_argument("--no-reasoning", action="store_true", help="raisonnement du modèle coupé (défaut : actif)")
    code_p.add_argument("--no-budget-notice", action="store_true",
                        help="pas de rappel « termine maintenant » à 80 %% des appels")
    code_p.add_argument("--stall-limit", type=int, default=6,
                        help="appels répétés à l'identique avant de conclure (0 : pas de détection ; défaut 6)")
    code_p.add_argument("--mode", choices=["default", "accept_edits", "auto", "plan"], default=None,
                        help="permissions (défaut : réglages de l'utilisateur, sinon accept_edits)")
    code_p.add_argument("--allow", action="append", default=[], help="règle allow en plus, ex. 'terminal(npm test*)'")
    code_p.add_argument("--prefix", default=None, help="préfixe système (défaut : nokker/prompts/prefix-lokkal-code.md)")
    code_p.add_argument("--thread", default=None)
    code_p.add_argument("--sender", default=None, help="identité de l'expéditeur (défaut : aucune -> pas de GO)")
    code_p.add_argument("--json", action="store_true", help="compte rendu JSON (banc)")
    code_p.add_argument("--hooks", action="store_true",
                        help="hooks des réglages (session_start, pre_tool, post_tool, turn_end), comme local.hooks")
    code_p.set_defaults(func=_cmd_code)

    hooks_p = sub.add_parser("hooks", help="hooks du dépôt : voir leur empreinte, leur faire confiance (lot X3)")
    hooks_sub = hooks_p.add_subparsers(dest="hooks_command", required=True)
    for name, help_text in (("show", "hooks du dépôt, empreinte et confiance"),
                            ("trust", "fait confiance aux hooks du dépôt (cette empreinte, ce dossier)")):
        hp = hooks_sub.add_parser(name, help=help_text)
        hp.add_argument("--workspace", default=".", help="dossier du dépôt (défaut : dossier courant)")
        hp.add_argument("--settings", default=None,
                        help="réglages de l'utilisateur (défaut : $NOKKER_HOME/settings.json, sinon "
                             "~/.config/nokker/settings.json)")
        if name == "trust":
            hp.add_argument("--sha256", required=True, help="l'empreinte relue (nokker hooks show), recopiée")
        hp.set_defaults(func=_cmd_hooks, unscoped=True)

    from nokker.secrets.cli import add_secret_subparsers

    add_secret_subparsers(sub)  # coffre local (X4) : secret set|list|rm, vault migrate|status

    costs_p = add_costs_subparser(sub)
    costs_p.set_defaults(func=_cmd_costs)

    devices_p = sub.add_parser("devices", help="appareils enrôlés (approbations fortes, docs/APPROVALS.md)")
    devices_sub = devices_p.add_subparsers(dest="devices_command", required=True)
    approve_p = devices_sub.add_parser("approve", help="confirme un appareil `pending` par son code court")
    approve_p.add_argument("code", help="code d'appairage à 6 caractères affiché par le client à l'enrôlement")
    approve_p.set_defaults(func=_cmd_devices_approve)
    remove_p = devices_sub.add_parser("remove", help="retire un appareil (sessions, nonces, identité) par son code court")
    remove_p.add_argument("code")
    remove_p.add_argument("--apply", action="store_true", help="valide (sinon : à blanc, compte rendu seulement)")
    remove_p.set_defaults(func=_cmd_devices_remove, unscoped=True)

    erase_p = sub.add_parser("erase", help="efface un compte testeur et son locataire (docs/BACKUP-DRP.md §6), à blanc par défaut")
    who = erase_p.add_mutually_exclusive_group(required=True)
    who.add_argument("--email")
    who.add_argument("--account", help="identifiant us_…")
    erase_p.add_argument("--apply", action="store_true", help="valide l'effacement (exige --confirm)")
    erase_p.add_argument("--confirm", help="l'identifiant us_… du compte, recopié")
    erase_p.set_defaults(func=_cmd_erase, unscoped=True)

    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "unscoped", False):  # serve fixe le locataire par requête ; migrate n'en a pas
        return args.func(args)
    with tenant_scope(args.tenant):
        return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_parser", "main"]
