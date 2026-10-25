"""Publication d'une relecture `nokker review` sur une PR (lot X13) : GitHub et Forgejo (API compatible Gitea).

  - GitHub : `POST {api}/repos/{o}/{r}/pulls/{n}/reviews` avec `comments[] = {path, line, side, body}`, jeton
    `Authorization: Bearer` (le `GITHUB_TOKEN` du workflow, permission `pull-requests: write`) ;
  - Forgejo : `POST {serveur}/api/v1/repos/{o}/{r}/pulls/{n}/reviews` avec `comments[] = {path, body, new_position,
    old_position}` (numéros de ligne du nouveau / de l'ancien fichier, 0 pour l'autre côté), `Authorization: token`.

Jamais `APPROVE` : verdict `ok` → revue `COMMENT` ; `changes_requested` → `REQUEST_CHANGES` seulement si l'appelant
le demande (`--request-changes`), sinon `COMMENT`. Un relecteur automatique conseille, il ne fait pas passer une PR.

Rien ne part sans `--post` (à blanc par défaut : `plan` rend la requête, sans le jeton). Le jeton n'est lu que dans
l'environnement (jamais en argument : il finirait dans `ps` et dans les journaux du runner) et ne part jamais en clair
vers un hôte qui n'est pas la boucle locale.

Replis sur `422` (une seule fois chacun) : `REQUEST_CHANGES` refusé (PR de l'auteur du jeton) → `COMMENT` ; position
refusée malgré la validation (diff de la plateforme différent du nôtre) → revue sans commentaires de ligne, les
commentaires recopiés dans le corps.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlparse

from nokker.review.engine import ReviewReport, to_markdown

logger = logging.getLogger("nokker.review.publish")

ALLOWED_EVENTS = ("COMMENT", "REQUEST_CHANGES")
PLATFORMS = ("github", "forgejo")
_SLUG = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
_LOOPBACK = ("127.0.0.1", "localhost", "::1")


class PublishError(RuntimeError):
    pass


def review_event(verdict: str, request_changes: bool = False) -> str:
    """Jamais `APPROVE`."""
    return "REQUEST_CHANGES" if verdict == "changes_requested" and request_changes else "COMMENT"


@dataclass
class Target:
    platform: str  # github | forgejo
    api_url: str  # https://api.github.com | https://git.example.org/api/v1
    repo: str  # owner/name
    number: int
    commit: str | None = None

    def reviews_url(self) -> str:
        owner, name = self.repo.split("/", 1)
        return f"{self.api_url.rstrip('/')}/repos/{quote(owner, safe='')}/{quote(name, safe='')}/pulls/{self.number}/reviews"


def _event_payload(env: dict[str, str]) -> dict[str, Any]:
    path = env.get("GITHUB_EVENT_PATH") or ""
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def resolve_target(platform: str, *, repo: str | None = None, number: int | None = None, api_url: str | None = None,
                   commit: str | None = None, env: dict[str, str] | None = None) -> Target:
    """La PR visée : arguments, sinon l'environnement du runner (`GITHUB_REPOSITORY`, l'événement
    `GITHUB_EVENT_PATH`, `GITHUB_API_URL` / `GITHUB_SERVER_URL` ; Forgejo Actions pose les mêmes noms)."""
    env = dict(os.environ if env is None else env)
    if platform not in PLATFORMS:
        raise PublishError(f"plateforme inconnue : {platform!r} ({', '.join(PLATFORMS)})")
    event = _event_payload(env)
    pr = event.get("pull_request") if isinstance(event.get("pull_request"), dict) else {}
    repo = repo or env.get("GITHUB_REPOSITORY") or ""
    if not _SLUG.match(repo):
        raise PublishError(f"dépôt invalide : {repo!r} (attendu propriétaire/nom ; --repo-slug ou GITHUB_REPOSITORY)")
    if number is None:
        number = pr.get("number") or event.get("number")
    try:
        number = int(number)  # type: ignore[arg-type]
    except (TypeError, ValueError) as e:
        raise PublishError("numéro de PR introuvable (--pr, ou un événement pull_request)") from e
    if number <= 0:
        raise PublishError(f"numéro de PR invalide : {number}")
    commit = commit or ((pr.get("head") or {}).get("sha") if isinstance(pr.get("head"), dict) else None)
    if platform == "github":
        api = api_url or env.get("GITHUB_API_URL") or "https://api.github.com"
    else:
        server = env.get("GITHUB_SERVER_URL") or env.get("FORGEJO_SERVER_URL") or env.get("GITEA_SERVER_URL") or ""
        api = api_url or (server.rstrip("/") + "/api/v1" if server else "")
        if not api:
            raise PublishError("URL de l'API Forgejo introuvable (--api-url, ou GITHUB_SERVER_URL)")
    return Target(platform, api, repo, number, commit if commit and re.fullmatch(r"[0-9a-f]{40,64}", commit) else None)


def token_from_env(platform: str, token_env: str | None = None, env: dict[str, str] | None = None) -> tuple[str, str]:
    """(nom de la variable, jeton). GitHub : `GITHUB_TOKEN` ; Forgejo : `NOKKER_REVIEW_TOKEN`, puis `FORGEJO_TOKEN`,
    puis `GITHUB_TOKEN` (le jeton automatique de Forgejo Actions, si l'instance lui donne le droit d'écrire)."""
    env = os.environ if env is None else env
    names = [token_env] if token_env else (["GITHUB_TOKEN"] if platform == "github" else
                                           ["NOKKER_REVIEW_TOKEN", "FORGEJO_TOKEN", "GITHUB_TOKEN"])
    for n in names:
        if env.get(n):
            return n, env[n]
    return names[0], ""


def build_payload(target: Target, report: ReviewReport, *, request_changes: bool = False, lang: str = "en",
                  inline: bool = True) -> dict[str, Any]:
    event = review_event(report.verdict, request_changes)
    # Commentaires de ligne publiés à part : le corps n'en recopie pas la liste ; sans eux (repli), il la porte.
    body = to_markdown(report, lang, inline=not inline)
    payload: dict[str, Any] = {"body": body, "event": event}
    if target.commit:
        payload["commit_id"] = target.commit
    if inline:
        if target.platform == "github":
            payload["comments"] = [{"path": c.path, "line": c.line, "side": c.side,
                                    "body": f"**{c.severity}**: {c.body}"} for c in report.comments]
        else:
            payload["comments"] = [{"path": c.path, "body": f"**{c.severity}**: {c.body}",
                                    "new_position": c.line if c.side == "RIGHT" else 0,
                                    "old_position": c.line if c.side == "LEFT" else 0} for c in report.comments]
    return payload


def _headers(platform: str, token: str) -> dict[str, str]:
    h = {"Content-Type": "application/json", "User-Agent": "nokker-review"}
    if platform == "github":
        h.update({"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                  "Authorization": f"Bearer {token}"})
    else:
        h.update({"Accept": "application/json", "Authorization": f"token {token}"})
    return h


@dataclass
class PublishResult:
    posted: bool
    dry_run: bool
    platform: str
    url: str
    event: str
    comments: int
    status: int | None = None
    html_url: str | None = None
    fallbacks: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


def publish(target: Target, report: ReviewReport, *, token: str, post: bool = False, request_changes: bool = False,
            lang: str = "en", client: Any = None, timeout_s: float = 30.0) -> PublishResult:
    url = target.reviews_url()
    payload = build_payload(target, report, request_changes=request_changes, lang=lang)
    if payload["event"] not in ALLOWED_EVENTS:  # garde explicite : rien ne doit jamais approuver
        raise PublishError(f"événement interdit : {payload['event']}")
    res = PublishResult(posted=False, dry_run=not post, platform=target.platform, url=url, event=payload["event"],
                        comments=len(payload.get("comments") or []))
    if not post:
        return res
    if not token:
        raise PublishError("aucun jeton dans l'environnement (GITHUB_TOKEN, NOKKER_REVIEW_TOKEN…)")
    parsed = urlparse(url)
    if parsed.scheme != "https" and parsed.hostname not in _LOOPBACK:
        raise PublishError(f"refus d'envoyer le jeton en clair vers {parsed.scheme}://{parsed.hostname}")
    import httpx

    own = client is None
    client = client or httpx.Client(timeout=timeout_s, follow_redirects=False)
    try:
        headers = _headers(target.platform, token)
        resp = client.post(url, json=payload, headers=headers)
        if resp.status_code == 422 and payload["event"] == "REQUEST_CHANGES":
            res.fallbacks.append("request_changes_refused")
            payload["event"] = res.event = "COMMENT"
            resp = client.post(url, json=payload, headers=headers)
        if resp.status_code == 422 and payload.get("comments"):
            res.fallbacks.append("inline_refused")
            payload = build_payload(target, report, request_changes=False, lang=lang, inline=False)
            payload["event"] = res.event
            res.comments = 0
            resp = client.post(url, json=payload, headers=headers)
        res.status = resp.status_code
        if resp.status_code >= 300:
            raise PublishError(f"HTTP {resp.status_code} en publiant la revue : {resp.text[:300]}")
        try:
            res.html_url = (resp.json() or {}).get("html_url")
        except ValueError:
            res.html_url = None
        res.posted = True
        return res
    finally:
        if own:
            client.close()


__all__ = ["ALLOWED_EVENTS", "PLATFORMS", "PublishError", "PublishResult", "Target", "build_payload", "publish",
           "resolve_target", "review_event", "token_from_env"]
