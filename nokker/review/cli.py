"""`python -m nokker review` (lot X13, docs/LOKKAL.md §7.20, docs/en/review.md) : relecture d'un diff ou d'une PR par
le juge, sans interface, commentaires par ligne ; à blanc sauf `--post`.

Codes de sortie : 0 relu (ou rien à relire) ; 1 `--fail-on-changes` et verdict `changes_requested` ; 2 entrée ou
réglage invalide (diff illisible, référence inconnue, modèle non configuré, publication impossible) ; 3 rien n'a pu
être relu (modèle injoignable ou réponses illisibles sur tous les appels) : rien n'est publié.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

from nokker.qa.secrets import KnownSecrets
from nokker.review.engine import Reviewer, ReviewOptions, scrub_code, to_markdown
from nokker.review.source import (
    SourceError,
    Sources,
    disk_file,
    git_blob,
    git_diff,
    read_instructions,
    resolve,
)

logger = logging.getLogger("nokker.review")

MAX_DIFF_BYTES = 20_000_000
_SECRET_ENV = re.compile(r"(?:TOKEN|KEY|SECRET|PASSWORD|PASSWD|PAT)$", re.IGNORECASE)
_BASE_SECRET_NAMES = ("OPENAI_API_KEY", "GITHUB_TOKEN", "NOKKER_REVIEW_TOKEN", "FORGEJO_TOKEN", "GITEA_TOKEN",
                      "ROUTER_KEY", "ACTIONS_RUNTIME_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_TOKEN")


class ReviewConfigError(RuntimeError):
    pass


def add_review_arguments(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
    src = p.add_argument_group("entrée")
    src.add_argument("--diff", default=None, help="fichier de diff unifié, ou - pour stdin")
    src.add_argument("--base", default=None, help="référence de base (avec --head) dans le dépôt --repo")
    src.add_argument("--head", default=None, help="référence de tête (avec --base)")
    src.add_argument("--repo", default=None,
                     help="dépôt git local, lu sans hooks (défaut : dossier courant) ; avec --diff : dossier des "
                          "fichiers de contexte et des consignes")
    src.add_argument("--files", nargs="*", default=None, metavar="PATH",
                     help="fichiers entiers pour le contexte ; sans chemin : chaque fichier modifié")
    src.add_argument("--no-instructions", action="store_true",
                     help="ne pas lire AGENTS.md / NOKKER.md / CLAUDE.md (lus sur la base en mode --base/--head)")
    src.add_argument("--exclude", action="append", default=[], metavar="GLOB", help="fichiers à ne pas relire")

    model = p.add_argument_group("modèle")
    model.add_argument("--config", dest="review_config", default=None,
                       help="config du noyau (juge : `judge:`, sinon le palier Ship) ; défaut $NOKKER_CONFIG")
    model.add_argument("--base-url", default=None, help="point d'accès OpenAI-compatible (défaut $OPENAI_BASE_URL)")
    model.add_argument("--model", default=None, help="modèle (défaut $NOKKER_REVIEW_MODEL, sinon celui de la config)")
    model.add_argument("--api-key-env", default="OPENAI_API_KEY",
                       help="variable qui porte la clé du point d'accès (défaut OPENAI_API_KEY)")
    model.add_argument("--max-tokens", type=int, default=100_000, help="budget d'entrée total (défaut 100000)")
    model.add_argument("--chunk-tokens", type=int, default=16_000, help="entrée d'un appel au plus (défaut 16000)")
    model.add_argument("--max-output-tokens", type=int, default=2_000, help="sortie d'un appel (défaut 2000)")
    model.add_argument("--max-comments", type=int, default=25, help="commentaires au plus (défaut 25)")
    model.add_argument("--timeout", type=float, default=120.0, help="délai d'un appel au modèle, en secondes")
    model.add_argument("--lang", choices=["en", "fr"], default="en", help="langue du compte rendu (défaut en)")

    out = p.add_argument_group("sortie et publication")
    out.add_argument("--json", action="store_true", help="compte rendu JSON (sinon Markdown)")
    out.add_argument("--platform", choices=["github", "forgejo"], default=None,
                     help="plateforme de la PR (défaut : détectée dans l'environnement du runner)")
    out.add_argument("--post", action="store_true", help="publie la revue (sinon à blanc)")
    out.add_argument("--pr", type=int, default=None, help="numéro de la PR (défaut : l'événement du runner)")
    out.add_argument("--repo-slug", default=None, help="propriétaire/nom (défaut $GITHUB_REPOSITORY)")
    out.add_argument("--api-url", default=None, help="API de la plateforme (défaut : environnement du runner)")
    out.add_argument("--commit", default=None, help="sha relu (défaut : --head résolu, sinon l'événement)")
    out.add_argument("--token-env", default=None,
                     help="variable du jeton (défaut GITHUB_TOKEN ; Forgejo : NOKKER_REVIEW_TOKEN, FORGEJO_TOKEN)")
    out.add_argument("--request-changes", action="store_true",
                     help="verdict changes_requested → REQUEST_CHANGES (sinon une revue COMMENT ; jamais APPROVE)")
    out.add_argument("--fail-on-changes", action="store_true", help="code de sortie 1 si changes_requested")
    out.add_argument("-v", "--verbose", action="store_true", help="journal détaillé sur stderr")
    return p


def known_secrets(env: dict[str, str], extra_names: tuple[str, ...] = ()) -> KnownSecrets:
    """Les secrets que ce processus détient (clé du modèle, jeton de publication, secrets du runner passés en
    variables `…_TOKEN`, `…_KEY`, `…_SECRET`) : si l'un d'eux apparaît dans le diff ou la réponse, il est caviardé."""
    ks = KnownSecrets()
    for name, value in env.items():
        if not value or len(value) < 8:
            continue
        if name in _BASE_SECRET_NAMES or name in extra_names or _SECRET_ENV.search(name):
            ks.add(name, value)
    return ks


def model_settings(args: argparse.Namespace, env: dict[str, str]) -> dict[str, Any]:
    """{base_url, model, api_key, api_key_env, extra_body} : arguments, puis variables, puis la config Lokkal."""
    base_url = args.base_url or env.get("OPENAI_BASE_URL") or ""
    model = args.model or env.get("NOKKER_REVIEW_MODEL") or ""
    cfg_path = args.review_config or env.get("NOKKER_CONFIG") or ""
    judge = None
    if cfg_path:
        if not Path(cfg_path).is_file():
            if args.review_config:
                raise ReviewConfigError(f"config introuvable : {cfg_path}")
        else:
            from nokker.config import load_config

            cfg = load_config(cfg_path)
            if cfg.judge_explicit or cfg.tiers:
                judge = cfg.judge_config()
    if base_url:
        key_env = args.api_key_env
        try:
            extra = json.loads(env.get("NOKKER_REVIEW_EXTRA_BODY") or "{}")
        except ValueError as e:
            raise ReviewConfigError("NOKKER_REVIEW_EXTRA_BODY n'est pas du JSON") from e
        if not model and judge and judge.base_url.rstrip("/") == base_url.rstrip("/"):
            model = judge.model
    elif judge:
        base_url, key_env = judge.base_url, judge.api_key_env
        model = model or judge.model
        extra = judge.reasoning_off_body if judge.reasoning_off_body is not None else {"reasoning_effort": "none"}
    else:
        raise ReviewConfigError("aucun modèle : pose OPENAI_BASE_URL et NOKKER_REVIEW_MODEL (ou --base-url/--model), "
                                "ou une config Lokkal (--config, $NOKKER_CONFIG)")
    if not model:
        raise ReviewConfigError("aucun modèle : NOKKER_REVIEW_MODEL ou --model")
    if not isinstance(extra, dict):
        raise ReviewConfigError("NOKKER_REVIEW_EXTRA_BODY doit être un objet JSON")
    return {"base_url": base_url, "model": model, "api_key_env": key_env, "api_key": env.get(key_env, ""),
            "extra_body": extra}


def read_sources(args: argparse.Namespace) -> Sources:
    if args.diff and (args.base or args.head):
        raise SourceError("--diff OU --base/--head, pas les deux")
    if args.diff:
        if args.diff == "-":
            text = sys.stdin.read()
        else:
            p = Path(args.diff)
            if not p.is_file():
                raise SourceError(f"diff introuvable : {args.diff}")
            if p.stat().st_size > MAX_DIFF_BYTES:
                raise SourceError(f"diff trop gros (> {MAX_DIFF_BYTES} octets)")
            text = p.read_text(encoding="utf-8", errors="replace")
        src = Sources(diff=text)
        if args.repo is not None or args.files is not None:  # lus sur le disque : la copie de travail, pas la base
            from nokker.review.diff import parse_diff

            root = args.repo or "."
            paths = [f.path for f in parse_diff(text)]
            if args.files is not None:
                for rel in (args.files or paths):
                    content = disk_file(root, rel)
                    if content is not None:
                        src.files[rel] = content
            if not args.no_instructions:
                src.instructions = read_instructions(lambda rel: disk_file(root, rel), paths)
                src.instructions_from = "working tree"
        return src
    if not (args.base and args.head):
        raise SourceError("donne --diff <fichier|->, ou --base et --head")
    repo = args.repo or "."
    base, head = resolve(repo, args.base), resolve(repo, args.head)
    text = git_diff(repo, base, head)
    src = Sources(diff=text, base_sha=base, head_sha=head)
    from nokker.review.diff import parse_diff

    files = parse_diff(text)
    paths = [f.path for f in files]
    if args.files is not None:
        for rel in (args.files or [f.new_path for f in files if f.new_path]):
            content = git_blob(repo, head, rel)
            if content is not None:
                src.files[rel] = content
    if not args.no_instructions:
        # Sur la BASE : la PR ne réécrit pas les règles de sa propre relecture.
        src.instructions = read_instructions(lambda rel: git_blob(repo, base, rel), paths)
        src.instructions_from = f"base {base[:12]}"
    return src


def detect_platform(env: dict[str, str]) -> str | None:
    if env.get("FORGEJO_ACTIONS") or env.get("GITEA_ACTIONS"):
        return "forgejo"
    if env.get("GITHUB_ACTIONS"):
        return "github"
    return None


def run(args: argparse.Namespace, env: dict[str, str] | None = None, transport: Any = None) -> int:
    env = dict(os.environ if env is None else env)
    try:
        sources = read_sources(args)
        settings = None if transport is not None else model_settings(args, env)
    except (SourceError, ReviewConfigError) as e:
        return _fail(args, str(e), 2)
    known = known_secrets(env, (settings["api_key_env"],) if settings else ())
    if transport is None:
        from nokker.qa.judge import HttpJudgeTransport

        transport = HttpJudgeTransport(base_url=settings["base_url"], model=settings["model"],
                                       api_key=settings["api_key"], extra_body=dict(settings["extra_body"]),
                                       timeout_s=args.timeout)
    opts = ReviewOptions(max_tokens=args.max_tokens, chunk_tokens=args.chunk_tokens,
                         max_output_tokens=args.max_output_tokens, max_comments=args.max_comments,
                         exclude=tuple(args.exclude or ()), lang=args.lang)
    reviewer = Reviewer(transport=transport, model_name=(settings or {}).get("model", ""), known=known, options=opts)
    report = reviewer.review(sources.diff, files=sources.files, instructions=sources.instructions,
                             instructions_from=sources.instructions_from or "base")
    out: dict[str, Any] = report.as_dict()
    if sources.instructions:
        out["instructions"] = {"from": sources.instructions_from, "files": [i.path for i in sources.instructions]}

    platform = args.platform or detect_platform(env)
    code = 0
    if not report.reviewed:
        out["post"] = None
        code = 3
    elif not (report.reviewed_files or report.comments or report.unreviewed):
        out["post"] = None  # diff vide : rien à publier
    elif platform and (args.post or args.pr or env.get("GITHUB_EVENT_PATH")):
        from nokker.review.publish import PublishError, publish, resolve_target, token_from_env

        try:
            target = resolve_target(platform, repo=args.repo_slug, number=args.pr, api_url=args.api_url,
                                    commit=args.commit or sources.head_sha, env=env)
            _, token = token_from_env(platform, args.token_env, env)
            known.add("publish-token", token)
            res = publish(target, report, token=token, post=args.post, request_changes=args.request_changes,
                          lang=args.lang)
            out["post"] = res.as_dict()
        except PublishError as e:
            out["post"] = {"error": scrub_code(str(e), known), "dry_run": not args.post}
            code = 2 if args.post else 0  # à blanc : le plan manque (pas de PR), la relecture reste valable
    if code == 0 and args.fail_on_changes and report.verdict == "changes_requested":
        code = 1
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
    else:
        print(to_markdown(report, args.lang))
        post = out.get("post")
        if isinstance(post, dict):
            what = "publiée" if post.get("posted") else ("à blanc" if post.get("dry_run") else "échec")
            print(f"[revue {what}] {post.get('event', '')} {post.get('url', '')} {post.get('error', '')}".rstrip(),
                  file=sys.stderr)
    return code


def _fail(args: argparse.Namespace, message: str, code: int) -> int:
    if getattr(args, "json", False):
        print(json.dumps({"error": message}, ensure_ascii=False))
    print(f"[erreur: {message}]", file=sys.stderr)
    return code


def build_parser() -> argparse.ArgumentParser:
    return add_review_arguments(argparse.ArgumentParser(
        prog="nokker review", description="Relecture d'un diff ou d'une PR par le juge de Nokker, par ligne."))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s", stream=sys.stderr)
    return run(args)


__all__ = ["add_review_arguments", "build_parser", "known_secrets", "main", "model_settings", "read_sources", "run"]
