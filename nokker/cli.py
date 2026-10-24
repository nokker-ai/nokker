"""`nokker` CLI — PLAN-P1 §5.1 (F2) : `nokker serve` (API), `nokker chat "<message>"` (un tour local),
`nokker costs` (F6, branché via `nokker.ledger.cli.add_costs_subparser`), `nokker devices approve <code>`
(P1, docs/APPROVALS.md §3.1 : confirmation locale d'un appareil `pending` — chemin de confiance qui ne
dépend d'aucune identité réseau, celui qui a un shell root sur l'hôte EST déjà un opérateur de confiance).
`nokker erase` / `nokker devices remove <code>` : effacement d'un compte testeur, retrait d'un appareil
(nokker/admin/erase.py, à blanc par défaut). `nokker migrate` : migrations Postgres en propriétaire (job d'init de la bêta, `NOKKER_PG_OWNER_DSN`).
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

    from nokker.qa.permissions import LocalPermissions, Mode, PermissionGates
    from nokker.tools.code import DEFAULT_SANDBOX_IMAGE, CodeToolSource

    config = load_config(args.config)
    prefix = args.prefix or str(Path(__file__).parent / "prompts" / "prefix-lokkal-code.md")
    config = dataclasses.replace(config, prefix_path=prefix, product_prefix_path=prefix, profile_path=None)
    task = args.task if args.task != "-" else sys.stdin.read()
    source = CodeToolSource(root=args.workspace, backend=args.backend, image=args.image or DEFAULT_SANDBOX_IMAGE,
                            network=not args.no_network, network_name=args.network_name, host_root=args.host_root)
    from nokker.tools.checkpoints import checkpoint_sources, code_root_of

    from nokker.tools.delegate import DelegateToolSource

    wrapper, checkpoints = checkpoint_sources(source, code_root_of(source))  # photo avant chaque écriture
    parent: dict = {}
    delegate = DelegateToolSource(  # sous-agents : copies du noyau parent sur le registre filtré
        lambda registry, thread_id, observer: dataclasses.replace(parent["k"], tools=registry, observer=observer),
        source)
    kernel = build_kernel(config, extra_sources=[wrapper, checkpoints, delegate])
    parent["k"] = kernel
    delegate.bind(getattr(kernel.tools, "internal", kernel.tools))
    kernel.max_calls = args.max_calls
    kernel.max_tokens = 16384
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
    try:
        turn = kernel.run_turn(TurnInput(thread_id=thread_id, user_text=task, channel="cli", sender=args.sender))
    finally:
        source.close()
    if args.json:
        tool_calls = [c.name for m in turn.messages for c in m.tool_calls]
        print(json.dumps({
            "thread_id": thread_id, "reply": turn.reply, "error": turn.error, "escalated": turn.escalated,
            "pending_approvals": [c.name for c in turn.pending_approvals], "tool_calls": tool_calls,
            "llm_calls": [{"tier": c.tier.value, "model": c.model, "prompt": c.usage.prompt,
                           "completion": c.usage.completion, "cached": c.usage.cached, "latency_s": c.latency_s,
                           "reason": c.route_reason} for c in turn.calls],
        }, ensure_ascii=False))
    else:
        if turn.error:
            print(f"[erreur: {turn.error}]", file=sys.stderr)
        print(turn.reply)
    return 1 if turn.error else 0


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
        report = erase_account(dsn, email=args.email, account_id=args.account, apply=args.apply, confirm=args.confirm)
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
    code_p.add_argument("--network-name", default=None, help="réseau docker du bac à sable (compose : nokker-sandbox)")
    code_p.add_argument("--no-network", action="store_true", help="bac à sable sans réseau")
    code_p.add_argument("--max-calls", type=int, default=60, help="appels modèle par tour (défaut 60)")
    code_p.add_argument("--no-reasoning", action="store_true", help="raisonnement du modèle coupé (défaut : actif)")
    code_p.add_argument("--mode", choices=["default", "accept_edits", "auto", "plan"], default=None,
                        help="permissions (défaut : réglages de l'utilisateur, sinon accept_edits)")
    code_p.add_argument("--allow", action="append", default=[], help="règle allow en plus, ex. 'terminal(npm test*)'")
    code_p.add_argument("--prefix", default=None, help="préfixe système (défaut : nokker/prompts/prefix-lokkal-code.md)")
    code_p.add_argument("--thread", default=None)
    code_p.add_argument("--sender", default=None, help="identité de l'expéditeur (défaut : aucune -> pas de GO)")
    code_p.add_argument("--json", action="store_true", help="compte rendu JSON (banc)")
    code_p.set_defaults(func=_cmd_code)

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
