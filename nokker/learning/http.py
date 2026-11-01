"""Routes de l'apprentissage (docs/LEARNING.md §6), drapeau `learning` et/ou `memory_pending` (sinon 404 comme avant).

nokker.ai : session du compte (`us_…`), locataire fixé par `_account_route`. Lokkal : jeton local (le propriétaire de
la machine, compte `local-owner`) ; par la passerelle (web), lecture seule — décider se fait sur la machine.

    GET  /v1/learning/journal[?limit=200]          → {entries: [{id, kind, subject, at, actor, version, thread_id,
                                                      turn_index, detail}]}  (du plus récent au plus ancien)
    GET  /v1/learning/skills                        → {skills: [{name, version, active, at}]}
    GET  /v1/learning/skills/{nom}[?version=&diff_from=]  → {name, current, active, versions: […], version?, diff?}
    POST /v1/learning/skills/{nom}/revert {version} → {skill, version, op, active, reverted_to}  (0 = désactiver)
    GET  /v1/memory/pending[?status=pending|approved|rejected|all] → {notes: [{id, source, kind, title, text, scope,
                                                      derived_from, project, links, thread_id, turn_index, at, status,
                                                      decided_at, decided_by, result}]}
    POST /v1/memory/pending/{id}/approve            → {note}   (la note entre en mémoire)
    POST /v1/memory/pending/{id}/reject             → {note}   (rien n'est écrit)

Erreurs `{"error": {"code", "message"}}` : 400 corps, 404 inconnu ou fonction éteinte, 409 déjà décidée / inchangé,
422 refus du scanner, 503 scanner ou mémoire absents. Le revert du propriétaire est son propre geste : pas de carte."""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

from nokker.learning.pending import PendingError, PendingMemory
from nokker.learning.skills import LearnedSkills, LearningError

_SKILL = re.compile(r"^/v1/learning/skills/([a-z0-9][a-z0-9-]{1,63})(/revert)?$")
_PENDING = re.compile(r"^/v1/memory/pending/(mp_[0-9a-f]{16})/(approve|reject)$")


@dataclass
class Learning:
    store: Any
    skills: LearnedSkills | None = None
    pending: PendingMemory | None = None


_learning: Learning | None = None


def configure(learning: Learning | None) -> None:
    global _learning
    _learning = learning


def enabled() -> bool:
    return _learning is not None


def service() -> Learning | None:
    return _learning


def is_learning_path(path: str) -> bool:
    route = path.split("?", 1)[0].rstrip("/")
    return route == "/v1/memory/pending" or route.startswith(("/v1/learning/", "/v1/memory/pending/"))


def _err(status: int, code: str, message: str) -> tuple[int, dict[str, Any]]:
    return status, {"error": {"code": code, "message": message}}


def _int(q: dict[str, list[str]], key: str) -> int | None:
    raw = (q.get(key) or [None])[0]
    if raw is None or raw == "":
        return None
    if not re.fullmatch(r"\d{1,6}", raw):
        raise LearningError(400, "bad_query", f"{key} : entier")
    return int(raw)


def dispatch(method: str, full_path: str, account: str, body: Callable[[], Any],
             writes: bool = True) -> tuple[int, dict[str, Any]] | None:
    """(statut, corps) pour une route de l'apprentissage, ou None (pas une route d'ici). `writes` faux : POST refusé
    (403), lecture permise (Lokkal par la passerelle)."""
    lrn = _learning
    split = urlsplit(full_path)
    path, q = split.path.rstrip("/"), parse_qs(split.query)
    if lrn is None or not is_learning_path(path):
        return None
    if method == "POST" and not writes:
        return _err(403, "machine_only", "décider se fait sur la machine (jeton local)")
    try:
        if path == "/v1/learning/journal" and method == "GET":
            limit = min(max(_int(q, "limit") or 200, 1), 1000)
            return 200, {"entries": [e.to_json() for e in lrn.store.journal(account, limit)]}
        if path == "/v1/learning/skills" and method == "GET":
            if lrn.skills is None:
                return _err(404, "learning_off", "skills appris désactivés (learning)")
            return 200, lrn.skills.history(account)
        m = _SKILL.match(path)
        if m and lrn.skills is not None:
            name = m.group(1)
            if not m.group(2) and method == "GET":
                return 200, lrn.skills.history(account, name, _int(q, "version"), _int(q, "diff_from"))
            if m.group(2) and method == "POST":
                data = body()
                if not isinstance(data, dict) or not isinstance(data.get("version"), int) \
                        or isinstance(data.get("version"), bool):
                    return _err(400, "bad_body", "corps {version: entier} (0 = désactiver)")
                v = lrn.skills.revert({"name": name, "version": data["version"]}, account, None, actor="owner")
                return 200, {"skill": v.name, "version": v.version, "op": v.op, "active": v.active,
                             "reverted_to": v.reverted_to}
        if path == "/v1/memory/pending" and method == "GET":
            if lrn.pending is None:
                return _err(404, "pending_off", "quarantaine de la mémoire désactivée (memory_pending)")
            status = (q.get("status") or ["pending"])[0]
            if status not in ("pending", "approved", "rejected", "all"):
                return _err(400, "bad_query", "status : pending | approved | rejected | all")
            notes = lrn.pending.list(account, None if status == "all" else status)
            return 200, {"notes": [n.to_json() for n in notes]}
        m = _PENDING.match(path)
        if m and method == "POST":
            if lrn.pending is None:
                return _err(404, "pending_off", "quarantaine de la mémoire désactivée (memory_pending)")
            note = lrn.pending.decide(account, m.group(1), approve=m.group(2) == "approve", by=account)
            return 200, {"note": note.to_json()}
    except (LearningError, PendingError) as e:
        return _err(e.status, e.code, e.message)
    return _err(404, "not_found", "not found")


def handle(h: Any, method: str, path: str, account: Any) -> bool:
    """nokker.ai : dans `_account_route` (session vérifiée, locataire fixé)."""
    if _learning is None or not is_learning_path(path):
        return False
    out = dispatch(method, getattr(h, "path", None) or path, account.account_id, h._read_json_body)
    h._json(*(out or _err(404, "not_found", "not found")))
    return True


__all__ = ["Learning", "configure", "dispatch", "enabled", "handle", "is_learning_path", "service"]
