"""`nokker traces export|purge|verify` (docs/TRACES.md §6). Commandes d'opérateur : chaque locataire est lu dans son
propre `tenant_scope` (RLS), jamais par une valeur joker."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

from nokker.traces.recorder import TARGETS


def _day(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"date attendue AAAA-MM-JJ : {value!r}") from e


def _cmd_export(args: argparse.Namespace) -> int:
    from nokker.config import load_config
    from nokker.qa.secrets import KnownSecrets
    from nokker.traces.export import ExportOptions, PgTraceSource, export

    config = load_config(args.config)
    dsn = config.pg_dsn()
    if not dsn:
        print("[erreur: aucun DSN Postgres configuré]", file=sys.stderr)
        return 1
    until = args.until + timedelta(days=1) if args.until else datetime.now(timezone.utc)
    since = args.since or until - timedelta(days=7)
    opts = ExportOptions(since=since, until=until, include_eval=args.include_eval, targets=tuple(args.target or ()),
                         tenant_types=tuple(args.tenant_type or ()), reconstruct=not args.no_reconstruct,
                         assume_target=args.assume_target, pii_internal=args.pii_internal, max_lines=args.max_lines)
    known = KnownSecrets.load(env_prefix=config.known_secrets_env_prefix, file_path=config.known_secrets_file)
    res = export(PgTraceSource(dsn), args.out, opts, known=known)
    print(json.dumps({"lot": str(res.path), "counts": res.manifest["counts"]}, ensure_ascii=False))
    return 0


def _cmd_purge(args: argparse.Namespace) -> int:
    from nokker.config import load_config
    from nokker.store.conn import known_tenants
    from nokker.tenancy.context import tenant_scope
    from nokker.traces.recorder import PgTraceSink

    config = load_config(args.config)
    dsn = config.pg_dsn()
    if not dsn:
        print("[erreur: aucun DSN Postgres configuré]", file=sys.stderr)
        return 1
    days = args.days or int(config.traces_retention_days)
    sink, total = PgTraceSink(dsn), 0
    for tenant in known_tenants(dsn):
        with tenant_scope(tenant):
            total += sink.purge(days)
    print(f"{total} appel(s) de plus de {days} jours purgé(s)")
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    from nokker.traces.export import verify_lot

    bad = verify_lot(args.lot)
    print("lot intact" if not bad else "empreintes fausses : " + ", ".join(bad))
    return 0 if not bad else 1


def add_traces_subparser(sub: Any) -> None:
    p = sub.add_parser("traces", help="traces d'entraînement / d'évaluation : export en lots datés, purge, vérification")
    ts = p.add_subparsers(dest="traces_command", required=True)
    e = ts.add_parser("export", help="un lot traces-AAAAMMJJ-n/ : calls.jsonl, manifest.json, SHA256SUMS")
    e.add_argument("--out", required=True, help="dossier où créer le lot")
    e.add_argument("--since", type=_day, default=None, help="premier jour inclus (AAAA-MM-JJ, UTC ; défaut : 7 jours)")
    e.add_argument("--until", type=_day, default=None, help="dernier jour inclus (AAAA-MM-JJ, UTC ; défaut : maintenant)")
    e.add_argument("--include-eval", action="store_true",
                   help="ajoute les lignes training_ok=false (évaluation) ; PII des testeurs caviardées quand même")
    e.add_argument("--target", action="append", choices=TARGETS, help="filtre de cible (répétable)")
    e.add_argument("--tenant-type", action="append", choices=("internal", "tester"), help="filtre (répétable)")
    e.add_argument("--no-reconstruct", action="store_true", help="seulement les appels enregistrés (prompt exact)")
    e.add_argument("--assume-target", choices=TARGETS, default="unknown", help="cible des lignes reconstruites")
    e.add_argument("--pii-internal", action="store_true", help="passage PII aussi sur le locataire interne")
    e.add_argument("--max-lines", type=int, default=0, help="découpe calls-00001.jsonl… au-delà (0 = un fichier)")
    e.set_defaults(func=_cmd_export, unscoped=True)
    pg = ts.add_parser("purge", help="supprime les appels plus vieux que la rétention, pour chaque locataire")
    pg.add_argument("--days", type=int, default=0, help="défaut : traces_retention_days de la config")
    pg.set_defaults(func=_cmd_purge, unscoped=True)
    v = ts.add_parser("verify", help="relit SHA256SUMS d'un lot")
    v.add_argument("lot")
    v.set_defaults(func=_cmd_verify, unscoped=True)


__all__ = ["add_traces_subparser"]
