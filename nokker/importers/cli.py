"""`nokker import <claude-code|cursor|hermes|openclaw> [--project DOSSIER] [--apply]` : rapport à blanc par défaut, écriture avec
--apply. Le rapport (report.json, summary.md) est rangé sous `$NOKKER_HOME/imports/<origine>-<horodatage>/`."""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path
from typing import Any

from nokker.importers import claude_code, cursor, hermes, openclaw
from nokker.importers.apply import apply
from nokker.importers.base import ImportItem
from nokker.tools.mcp_config import FILE_NAME as MCP_FILE

SOURCES = ("claude-code", "cursor", "hermes", "openclaw")


def add_import_subparser(sub: Any) -> argparse.ArgumentParser:
    p = sub.add_parser("import", help="reprend l'installation d'un autre agent (à blanc par défaut ; --apply écrit)")
    p.add_argument("source", choices=SOURCES)
    p.add_argument("--project", help="racine d'un dépôt à reprendre aussi (règles, skills, hooks du dépôt)")
    p.add_argument("--home", help="dossier personnel où chercher l'installation (défaut : le vôtre)")
    p.add_argument("--apply", action="store_true", help="écrit ce qui est prévu (sinon : rapport seulement)")
    p.add_argument("--project-out", help="cursor : écrit NOKKER.md dans ce dossier au lieu du dépôt (le dépôt reste en lecture seule)")
    p.add_argument("--list-paths", action="store_true",
                   help="affiche en JSON les chemins lus (relatifs au dossier personnel et au projet) et ceux à ne jamais monter")
    p.add_argument("--state-dir", help="dossier d'état de Nokker (skills, serveurs MCP, mémoire) ; défaut : $NOKKER_HOME ou ~/.nokker")
    return p


def ask_terminal(it: ImportItem) -> bool:
    """Confirmation d'un serveur MCP par la personne, au terminal ; sans terminal, jamais de oui implicite."""
    if not sys.stdin.isatty():
        return False
    e = it.detail["entry"]
    secrets = ", ".join(it.detail.get("secret_names") or []) or "aucun"
    answer = input(f"Ajouter le serveur MCP {e['name']} ({e['url']}), secrets à poser : {secrets} ? [o/N] ")
    return answer.strip().lower() in ("o", "oui", "y", "yes")


def run(args: argparse.Namespace, *, nokker_home: Path, user_settings: Path, memory: Any = None) -> int:
    if getattr(args, "list_paths", False):
        import json

        from nokker.importers.paths import listing

        print(json.dumps(listing(args.source), ensure_ascii=False))  # ne lit ni n'écrit rien
        return 0
    if getattr(args, "state_dir", None):
        nokker_home = Path(args.state_dir).expanduser()
        # Mêmes réglages que le noyau de Lokkal (`local.settings_path`, sinon `<état>/settings.json`, LocalApi) :
        # sans quoi, dans le conteneur jetable, les permissions iraient dans un fichier que le noyau ne lit pas.
        user_settings = nokker_home / "settings.json"
    home = Path(args.home).expanduser() if args.home else Path.home()
    project = Path(args.project).expanduser().resolve() if args.project else None
    if args.source == "claude-code":
        report = claude_code.scan(home, project, nokker_home=nokker_home)
    elif args.source == "hermes":
        import os

        hh = os.environ.get("HERMES_HOME") if not args.home else None
        report = hermes.scan(home, Path(hh).expanduser() if hh else None, nokker_home=nokker_home)
    elif args.source == "openclaw":
        report = openclaw.scan(home, nokker_home=nokker_home)
    else:
        out_dir = Path(args.project_out).expanduser().resolve() if getattr(args, "project_out", None) else None
        report = cursor.scan(home, project, project_out=out_dir)
    out = nokker_home / "imports" / f"{args.source}-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}"
    if args.apply:
        if memory is None:
            from nokker.context.project_memory import ProjectMemory

            memory = ProjectMemory(root=nokker_home / "project-memory")  # indexée au prochain démarrage du noyau
        apply(report, user_settings=user_settings, backup_dir=out / "sauvegarde", mcp_path=nokker_home / MCP_FILE,
              confirm=ask_terminal, memory=memory)
    report.write(out)
    c = report.counts()
    print(" · ".join(f"{k} {v}" for k, v in c.items() if v) or "rien trouvé")
    print(f"rapport : {out / 'summary.md'}")
    if not args.apply and c["planned"]:
        print("rien n'a été écrit : relancez avec --apply pour reprendre ce qui est prévu")
    return 1 if c["error"] else 0
