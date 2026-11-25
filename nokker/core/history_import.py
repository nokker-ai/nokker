"""Import d'un historique dans un fil VIDE (`POST /v1/local/sessions/{fil}/import`, docs/HOST-EMBED.md §16) : banc de reprise.

L'hôte rejoue une conversation passée pour mesurer la reprise d'un long fil. Rien n'est exécuté : les messages entrent dans le
store comme des tours déjà joués (`InputCheck.origin = "host_import"`), jamais au ledger (aucun appel modèle, aucun coût), jamais
dans le contexte comme une instruction nouvelle : texte scrubé comme une entrée (secrets, numéros de carte) et balises du noyau
(`[Environnement]`, `[Approbation]`) neutralisées. Les appels d'outils d'un message `assistant` sont de la structure historique :
aucun n'est rejoué, aucun outil n'est appelé."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from nokker.core.turn import InputCheck, Message, ToolCall, ToolResult, Turn

ORIGIN = "host_import"
MAX_TURNS = 200
MAX_BYTES = 256 * 1024
CEIL_TURNS = 2000  # plafond du réglage `local.import_max_turns`
CEIL_BYTES = 4 * 1024 * 1024  # plafond du réglage `local.import_max_bytes`
CODE_BAD = "bad_import"
CODE_TOO_LARGE = "import_too_large"
CODE_NOT_EMPTY = "thread_not_empty"
ROLES = ("user", "assistant", "tool")
_ID_MAX = 128
_MAX_CALLS = 32


class HistoryImportError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


@dataclass
class Entry:
    role: str
    content: str
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None


def _bad(message: str) -> HistoryImportError:
    return HistoryImportError(400, CODE_BAD, message)


def _optional_str(raw: dict, key: str, where: str) -> str | None:
    v = raw.get(key)
    if v is None:
        return None
    if not isinstance(v, str) or not v or len(v) > _ID_MAX:
        raise _bad(f"{where} : {key} doit être une chaîne de 1 à {_ID_MAX} caractères")
    return v


def clamp_limit(raw: Any, default: int, ceiling: int) -> int:
    """Réglage de plafond : illisible ou < 1 = défaut ; au-delà du maximum = le maximum."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        return default
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return default
    return default if v < 1 else min(v, ceiling)


def parse(body: Any, max_turns: int = MAX_TURNS, max_bytes: int = MAX_BYTES) -> list[Entry]:
    """`{turns: [{role, content, name?, tool_call_id?, tool_calls?}]}` validé : formes, plafonds (200 entrées, 256 Ko d'UTF-8
    de contenu, noms et appels compris) et ORDRE (un `user` d'abord ; un `tool` suit un `assistant` du même tour). Sans rien
    retenir de la valeur fautive dans le message d'erreur."""
    if not isinstance(body, dict) or not isinstance(body.get("turns"), list) or not body["turns"]:
        raise _bad("turns : liste non vide attendue")
    raw_turns = body["turns"]
    if len(raw_turns) > max_turns:
        raise HistoryImportError(400, CODE_TOO_LARGE, f"turns : {max_turns} entrées au plus ({len(raw_turns)} reçues ; "
                                                      "réglable : local.import_max_turns)")
    out: list[Entry] = []
    total = 0
    for i, raw in enumerate(raw_turns):
        where = f"turns[{i}]"
        if not isinstance(raw, dict):
            raise _bad(f"{where} : objet attendu")
        role = raw.get("role")
        if role not in ROLES:
            raise _bad(f"{where} : role « user », « assistant » ou « tool » attendu")
        content = raw.get("content", "")
        if content is None:
            content = ""
        if not isinstance(content, str):
            raise _bad(f"{where} : content doit être une chaîne")
        name = _optional_str(raw, "name", where)
        call_id = _optional_str(raw, "tool_call_id", where)
        calls = raw.get("tool_calls")
        if calls is not None:
            if role != "assistant" or not isinstance(calls, list) or len(calls) > _MAX_CALLS \
                    or not all(isinstance(c, dict) and isinstance(c.get("name"), str) and c["name"] for c in calls):
                raise _bad(f"{where} : tool_calls réservé à un tour assistant (≤ {_MAX_CALLS} appels {{id, name, arguments}})")
            total += len(json.dumps(calls, ensure_ascii=False, default=str).encode())
        if role == "tool" and not content.strip():
            raise _bad(f"{where} : un résultat d'outil sans contenu")
        if role == "user" and not content.strip():
            raise _bad(f"{where} : un tour user sans contenu")
        if role != "tool" and (name or call_id):
            raise _bad(f"{where} : name et tool_call_id sont réservés à un résultat d'outil")
        total += len(content.encode()) + len(name or "") + len(call_id or "")
        if total > max_bytes:
            raise HistoryImportError(400, CODE_TOO_LARGE, f"turns : {max_bytes // 1024} Ko au plus "
                                                          "(réglable : local.import_max_bytes)")
        out.append(Entry(role=role, content=content, name=name, tool_call_id=call_id,
                         tool_calls=[dict(c) for c in calls] if calls else None))
    if out[0].role != "user":
        raise _bad("turns[0] : l'historique commence par un tour user")
    seen_assistant = False
    for i, e in enumerate(out):
        if e.role == "user":
            seen_assistant = False
        elif e.role == "assistant":
            seen_assistant = True
        elif not seen_assistant:
            raise _bad(f"turns[{i}] : un résultat d'outil suit un tour assistant")
    return out


def _scrub_json(kernel: Any, value: Any) -> Any:
    """Arguments d'un appel historique : mêmes filets que le texte (la chaîne JSON entière, puis relue ; illisible : vide)."""
    text = kernel.invariants.check_input(json.dumps(value, ensure_ascii=False, default=str)).text
    try:
        return json.loads(text)
    except ValueError:
        return {}


def build_turns(kernel: Any, thread_id: str, entries: list[Entry], first_index: int = 0) -> list[Turn]:
    """Tours du store : un `user` ouvre un tour ; les `assistant` et `tool` qui suivent en sont les messages ; la réponse du
    tour est le texte du dernier message assistant sans appel d'outil. Rien n'est exécuté ni compté."""
    from nokker.payments.cardmask import mask_card_text

    inv = kernel.invariants
    turns: list[Turn] = []
    cur: Turn | None = None
    seq = 0
    for e in entries:
        if e.role == "user":
            check = inv.check_input(e.content)
            cur = Turn(thread_id=thread_id, index=first_index + len(turns),
                       input=InputCheck(text=check.text, redacted=list(check.redacted), domain_flags=set(), origin=ORIGIN))
            turns.append(cur)
            continue
        assert cur is not None  # `parse` : le premier est un user
        if e.role == "assistant":
            text = inv.check_input(e.content).text if e.content else ""
            calls = []
            for c in e.tool_calls or []:
                seq += 1
                args = c.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        args = {}
                calls.append(ToolCall(id=str(c.get("id") or f"imp_{seq}")[:_ID_MAX], name=str(c["name"])[:_ID_MAX],
                                      arguments=_scrub_json(kernel, args if isinstance(args, dict) else {})))
            cur.messages.append(Message(role="assistant", content=text, tool_calls=calls))
            continue
        # Résultat d'outil : jamais rejoué ; rattaché à un appel de l'assistant précédent (créé s'il manque).
        last = next(m for m in reversed(cur.messages) if m.role == "assistant")
        call_id = e.tool_call_id
        if call_id is None or all(c.id != call_id for c in last.tool_calls):
            seq += 1
            call_id = call_id or f"imp_{seq}"
            last.tool_calls.append(ToolCall(id=call_id, name=e.name or "tool", arguments={}))
        res = inv.redact_result(ToolResult(call_id=call_id, name=e.name or "tool", content=e.content), None)
        from nokker.qa.untrusted import neutralize_approval, neutralize_date, neutralize_env

        cur.messages.append(Message(role="tool", content=mask_card_text(neutralize_date(neutralize_approval(neutralize_env(res.content)))),
                                    tool_call_id=call_id, name=e.name or "tool"))
    for t in turns:
        last_msg = t.messages[-1] if t.messages else None
        if last_msg is not None and last_msg.role == "assistant" and not last_msg.tool_calls:
            t.reply = last_msg.content
    return turns


def import_history(kernel: Any, thread_id: str, body: Any, max_turns: int = MAX_TURNS,
                   max_bytes: int = MAX_BYTES) -> dict[str, Any]:
    """Valide, puis range l'historique dans le fil s'il est VIDE (`HistoryImportError` 400 / 409). Jamais d'appel d'outil, de modèle,
    de ledger ni de mémoire : seulement des tours rangés au store."""
    from nokker.tenancy.context import tenant_of_thread, tenant_scope

    entries = parse(body, max_turns, max_bytes)
    lock = kernel.turn_locks.get(thread_id)
    if lock.busy():  # un tour tourne : le fil n'est pas vide
        raise HistoryImportError(409, CODE_NOT_EMPTY, "le fil n'est pas vide")
    with lock, tenant_scope(tenant_of_thread(thread_id)):
        if kernel.store.next_index(thread_id) != 0 or kernel.store.history(thread_id):
            raise HistoryImportError(409, CODE_NOT_EMPTY, "le fil n'est pas vide")
        turns = build_turns(kernel, thread_id, entries)
        for t in turns:
            kernel.store.append(t)
    return {"thread_id": thread_id, "imported": len(entries), "turns": len(turns), "origin": ORIGIN}


__all__ = ["CEIL_BYTES", "CEIL_TURNS", "clamp_limit", "CODE_BAD", "CODE_NOT_EMPTY", "CODE_TOO_LARGE", "MAX_BYTES", "MAX_TURNS", "ORIGIN", "Entry", "HistoryImportError",
           "build_turns", "import_history", "parse"]
