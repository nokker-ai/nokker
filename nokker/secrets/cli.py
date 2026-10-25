"""`python -m nokker secret set|list|rm` et `python -m nokker vault migrate|status` (lot X4).

La valeur d'un secret n'est JAMAIS un argument (elle finirait dans l'historique du shell et dans `ps`) : elle est
lue sur l'entrée standard (`… secret set NOM < fichier`, ou un tube), sinon demandée deux fois sans écho. Aucune
commande n'affiche une valeur. Pas de `vault init` ni de `vault unseal` : le descellement d'OpenBao vit hors du
noyau (docs/LOKKAL.md §7.10).

Dans le compose : `docker compose exec core python3 -m nokker --config /etc/nokker/config.yaml secret set NOM`.
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path
from typing import Any

from nokker.secrets.base import SecretsError


def _local_cfg(args: argparse.Namespace) -> tuple[dict[str, Any], Path]:
    from nokker.config import load_config

    local = load_config(args.config).local or {}
    state = Path(local.get("state_dir") or os.environ.get("NOKKER_HOME") or "~/.config/nokker").expanduser()
    return dict(local.get("secrets") or {}), state


def _vault(args: argparse.Namespace):
    from nokker.secrets.vault import Vault

    cfg, state = _local_cfg(args)
    return Vault.from_config(cfg, state)


def _read_value(name: str) -> str:
    if not sys.stdin.isatty():
        raw = sys.stdin.read()  # un seul retour à la ligne final retiré (celui d'un `echo` ou d'un fichier)
        return raw.removesuffix("\r\n") if raw.endswith("\r\n") else raw.removesuffix("\n")
    first = getpass.getpass(f"valeur de {name} (sans écho) : ")
    if getpass.getpass("encore une fois : ") != first:
        raise SecretsError("les deux saisies diffèrent : rien n'a été rangé", "bad_value")
    return first


def _fail(e: SecretsError) -> int:
    print(f"[erreur: {e}]", file=sys.stderr)
    return 1


def _cmd_set(args: argparse.Namespace) -> int:
    try:
        vault = _vault(args)
        info = vault.set(args.name, _read_value(args.name))
    except SecretsError as e:
        return _fail(e)
    print(f"secret {info.name} rangé (fournisseur {vault.provider}).")
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    try:
        vault = _vault(args)
        items = vault.list()
    except SecretsError as e:
        return _fail(e)
    if vault.notice:
        print(f"[avis: {vault.notice}]", file=sys.stderr)
    for i in items:
        print(f"{i.name:<40}  {i.updated_at.isoformat() if i.updated_at else '-'}")
    if not items:
        print("(aucun secret)", file=sys.stderr)
    return 0


def _cmd_rm(args: argparse.Namespace) -> int:
    try:
        gone = _vault(args).delete(args.name)
    except SecretsError as e:
        return _fail(e)
    if not gone:
        print(f"[erreur: secret {args.name} absent]", file=sys.stderr)
        return 1
    print(f"secret {args.name} effacé.")
    return 0


def _cmd_migrate(args: argparse.Namespace) -> int:
    from nokker.secrets.migrate import migrate
    from nokker.secrets.openbao import session_from_config
    from nokker.secrets.vault import build_provider

    if args.src == args.dst:
        print("[erreur: --from et --to identiques]", file=sys.stderr)
        return 2
    cfg, state = _local_cfg(args)
    try:
        session = session_from_config(cfg.get("openbao") or {})
        pairs = [(build_provider(args.src, scope, cfg, state, session), build_provider(args.dst, scope, cfg, state,
                                                                                       session))
                 for scope in ("system", "user")]
        report = migrate(pairs, overwrite=args.overwrite, check=args.check)
    except SecretsError as e:
        return _fail(e)
    print(report.render())
    if args.check:
        print("(à blanc : rien n'a été écrit)")
    elif report.ok:
        print(f"migration vérifiée. Pour basculer : NOKKER_SECRETS_PROVIDER={args.dst} (ou local.secrets.provider), "
              "puis redémarrer le noyau.")
    return 0 if report.ok else 1


def _cmd_status(args: argparse.Namespace) -> int:
    try:
        vault = _vault(args)
    except SecretsError as e:
        return _fail(e)
    st = vault.status()
    print(f"fournisseur : {st['provider']}")
    if vault.session is not None:
        h = vault.session.health()
        print(f"OpenBao : {vault.session.address} — "
              + ("injoignable" if not h["reachable"] else "scellé" if h["sealed"] else "descellé"))
    if st.get("notice"):
        print(f"avis : {st['notice']}")
    return 0


def add_secret_subparsers(sub: Any) -> None:
    sec = sub.add_parser("secret", help="coffre local : set NOM (valeur sur stdin ou saisie masquée), list, rm")
    ss = sec.add_subparsers(dest="secret_command", required=True)
    p = ss.add_parser("set", help="range NOM ; valeur lue sur stdin, ou demandée sans écho (jamais en argument)")
    p.add_argument("name")
    p.set_defaults(func=_cmd_set, unscoped=True)
    p = ss.add_parser("list", help="noms et dates de mise à jour (jamais les valeurs)")
    p.set_defaults(func=_cmd_list, unscoped=True)
    p = ss.add_parser("rm", help="efface NOM")
    p.add_argument("name")
    p.set_defaults(func=_cmd_rm, unscoped=True)

    vault = sub.add_parser("vault", help="fournisseur du coffre : migrate, status (pas d'init ni d'unseal ici)")
    vs = vault.add_subparsers(dest="vault_command", required=True)
    p = vs.add_parser("migrate", help="copie un fournisseur dans l'autre (relu, idempotent, divergences signalées)")
    p.add_argument("--from", dest="src", choices=["file", "openbao"], required=True)
    p.add_argument("--to", dest="dst", choices=["file", "openbao"], required=True)
    p.add_argument("--overwrite", action="store_true", help="écrase une valeur divergente de la destination")
    p.add_argument("--check", action="store_true", help="à blanc : compte rendu sans rien écrire")
    p.set_defaults(func=_cmd_migrate, unscoped=True)
    p = vs.add_parser("status", help="fournisseur choisi, état d'OpenBao (joignable, scellé)")
    p.set_defaults(func=_cmd_status, unscoped=True)


__all__ = ["add_secret_subparsers"]
