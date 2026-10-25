"""Routes des sessions de code multi-utilisateur (docs/LOKKAL.md §7.28), derrière le bloc `code.enabled` :

    from nokker.code_sessions import http as code_http
    code_http.configure(CodeSessions(...))           # au démarrage, drapeau allumé seulement
    if code_http.handle(self, method, path): return  # dans le dispatcheur du serveur

`configure(None)` : `handle` rend False, le serveur répond comme avant (404).

Identité vérifiée d'un COMPTE (`us_…` : session d'appareil, y compris celle ouverte par le SSO), locataire fixé :
    GET    /v1/code/sessions                         → {sessions: [Session], limit, modes, default_mode, idle_ttl_s}
    POST   /v1/code/sessions {repo?: {url, credential?, ref?, depth?}, mode?}  → 201 Session
    GET    /v1/code/sessions/<cs_…>                  → Session
    DELETE /v1/code/sessions/<cs_…>[?purge=1]        → {id, closed: true, purged}
    POST   /v1/code/sessions/<cs_…>/hooks/trust {sha256}   → {thread_id, root, sha256, trusted: true}
    POST   /v1/code/sessions/<cs_…>/skills/trust {sha256}  → {thread_id, root, sha256, trusted: true}  (code.project_skills)
    POST   /v1/code/sessions/<cs_…>/tools/call {name, arguments}  → 200 {content, is_error} | 202 {approval_id}
           liste blanche TOOLS_CALL (points de retour, skills en lecture, état et arrêt des sous-agents) ; mêmes portes
           que la boucle (`kernel.direct_call`), 409 si un tour est en cours sur le fil ; une carte se décide par
           /v1/approvals/{id}/decision
    GET    /v1/code/credentials                      → {credentials: [{name, updated_at}]}  (jamais de valeur)
    PUT    /v1/code/credentials/<nom> {value}        → {name, set: true}
    DELETE /v1/code/credentials/<nom>                → {name, deleted: true}
Erreurs : `{"error": {"code", "message"}}` ; la session d'un autre utilisateur ou d'un autre locataire n'existe pas (404).
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from nokker.code_sessions.service import CodeError, CodeSessions
from nokker.tenancy.context import tenant_scope
from nokker.tenancy.resolve import is_account

_RE_SESSION = re.compile(r"^/v1/code/sessions/([^/]+)(?:/(hooks/trust|skills/trust|tools/call))?$")
# Outils qu'un écran appelle hors tour sur SA session (§7.28) : les points de retour, les skills (lecture), l'état et
# l'arrêt des sous-agents. Jamais terminal, write_file, patch, delegate : ceux-là restent au modèle, sous ses portes.
TOOLS_CALL = frozenset({"checkpoint_list", "checkpoint_diff", "checkpoint_restore", "skill_list", "skill_view",
                        "delegate_status", "delegate_cancel"})
_RE_CRED = re.compile(r"^/v1/code/credentials/([^/]+)$")

_service: CodeSessions | None = None


def configure(service: CodeSessions | None) -> None:
    global _service
    _service = service


def service() -> CodeSessions | None:
    return _service


def is_path(path: str) -> bool:
    return path in ("/v1/code/sessions", "/v1/code/credentials") or path.startswith(
        ("/v1/code/sessions/", "/v1/code/credentials/"))


def _err(h: Any, status: int, code: str, message: str) -> None:
    h._json(status, {"error": {"code": code, "message": message}})


def handle(h: Any, method: str, path: str) -> bool:
    svc = _service
    if svc is None or not is_path(path):
        return False
    body = h._read_json_body() if method in ("POST", "PUT") else {}
    body = body if isinstance(body, dict) else {}
    sender = h._resolve_identity(body)
    if sender is None:
        _err(h, 401, "unauthorized", "unauthorized")
        return True
    tid = h._tenant_for(sender)
    if tid is None:
        return True
    if not is_account(sender):
        _err(h, 403, "account_required", "sessions de code : compte requis")
        return True
    try:
        with tenant_scope(tid):
            out = _dispatch(svc, h, method, path, body, sender)
    except CodeError as e:
        _err(h, e.status, e.code, e.message)
        return True
    if out is None:
        _err(h, 404, "not_found", "not found")
    else:
        h._json(*out)
    return True


def _dispatch(svc: CodeSessions, h: Any, method: str, path: str, body: dict[str, Any], user: str
              ) -> tuple[int, Any] | None:
    if path == "/v1/code/sessions":
        if method == "GET":
            return 200, svc.list(user)
        if method == "POST":
            return 201, svc.create(user, body)
        return None
    m = _RE_SESSION.match(path)
    if m:
        sid = m.group(1)
        if m.group(2) == "tools/call" and method == "POST":
            return _tool_call(svc, h, sid, body, user)
        if m.group(2) == "hooks/trust" and method == "POST":
            return 200, svc.trust_hooks(user, sid, str(body.get("sha256") or ""))
        if m.group(2) == "skills/trust" and method == "POST":
            return 200, svc.trust_skills(user, sid, str(body.get("sha256") or ""))
        if m.group(2):
            return None
        if method == "GET":
            return 200, svc.get(user, sid)
        if method == "DELETE":
            q = parse_qs(urlsplit(h.path).query)
            purge = (q.get("purge") or ["0"])[0] in ("1", "true")
            return 200, svc.close(user, sid, purge=purge)
        return None
    if path == "/v1/code/credentials":
        return (200, svc.credentials()) if method == "GET" else None
    m = _RE_CRED.match(path)
    if m:
        name = unquote(m.group(1))
        if method == "PUT":
            return 200, svc.put_credential(name, body.get("value"))
        if method == "DELETE":
            return 200, svc.delete_credential(name)
    return None


def _tool_call(svc: CodeSessions, h: Any, sid: str, body: dict[str, Any], user: str) -> tuple[int, Any]:
    s = svc._own(user, sid)
    name, arguments = str(body.get("name") or ""), body.get("arguments") or {}
    if name not in TOOLS_CALL:
        raise CodeError(403, "tool_not_allowed", f"outil non appelable directement : {name}")
    if not isinstance(arguments, dict):
        raise CodeError(400, "bad_arguments", "arguments : objet attendu")
    if name == "delegate_status" and arguments.get("wait_s"):  # le verrou du fil serait tenu pendant l'attente
        raise CodeError(400, "bad_arguments", "delegate_status hors tour : sans wait_s")
    out = h._code_tool_call(s.thread_id, name, arguments, user)
    if out is None:
        raise CodeError(409, "turn_running", "un tour est en cours sur ce fil")
    if isinstance(out, str):
        return 202, {"approval_id": out}
    return 200, {"content": out.content, "is_error": out.is_error}


__all__ = ["TOOLS_CALL", "configure", "handle", "is_path", "service"]
