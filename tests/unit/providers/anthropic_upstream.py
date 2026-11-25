"""Faux amont HTTP local au format Anthropic Messages, pour les tests de `providers/anthropic.py`.

Un vrai serveur HTTP (boucle locale, port éphémère) qui rejoue des réponses conformes à la documentation de l'API
Messages : `message` JSON, flux SSE (`message_start`, `content_block_start|delta|stop`, `message_delta`,
`message_stop`, `ping`, `error`) et corps d'erreur `{"type": "error", "error": {"type", "message"}, "request_id"}`.
Chaque requête reçue est gardée (chemin, en-têtes en minuscules, corps décodé). Aucun appel vers l'extérieur.
"""
from __future__ import annotations

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

MODEL = "claude-test-1"
BINDING_BETA = "thinking-binding-controls-2026-08-01"


def _canon(x: Any) -> str:
    return json.dumps(_canon_val(x), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _canon_val(x: Any) -> Any:
    if isinstance(x, dict):
        return {k: _canon_val(v) for k, v in x.items() if k != "cache_control"}
    if isinstance(x, list):
        return [_canon_val(v) for v in x]
    return x


def _sid(sig: str) -> str:
    return hashlib.sha1(sig.encode()).hexdigest()[:12]


def _is_thinking(b: Any) -> bool:
    return isinstance(b, dict) and b.get("type") in ("thinking", "redacted_thinking")


def prefix_fp(body: dict, upto: int | None = None) -> str:
    """Empreinte de ce que précède un bloc : `system` (texte), `tools` et les messages avant `upto`, SANS les blocs de
    réflexion ni les `cache_control` (la règle de la doc : « system, tools et chaque message antérieur »)."""
    h = hashlib.sha256()
    system = "\x1f".join(str(b.get("text") or "") for b in body.get("system") or [])
    h.update(_canon({"system": system, "tools": body.get("tools") or []}).encode())
    msgs = body.get("messages") or []
    for m in msgs[:len(msgs) if upto is None else upto]:
        c = m.get("content")
        if isinstance(c, list):
            c = [b for b in c if not _is_thinking(b)]
        h.update(b"\x00" + _canon({"role": m.get("role"), "content": c}).encode())
    return h.hexdigest()[:24]


def _units(body: dict) -> tuple[list[str], list[int]]:
    """Blocs de la requête dans l'ordre de l'API (outils, système, messages), sans `cache_control` ni réflexion, et les
    positions (nombre de blocs AVANT le point) des points de cache posés : un point couvre tout ce qui précède."""
    units: list[str] = []
    marks: list[int] = []
    tools = body.get("tools") or []
    units.extend("T" + _canon(t) for t in tools)
    if tools and "cache_control" in tools[-1]:
        marks.append(len(units))
    for b in body.get("system") or []:
        units.append("S" + _canon(b))
        if "cache_control" in b:
            marks.append(len(units))
    for m in body.get("messages") or []:
        c = m.get("content")
        blocks = c if isinstance(c, list) else [{"type": "text", "text": c}]
        for b in blocks:
            if _is_thinking(b):
                continue
            units.append("M" + m.get("role", "") + _canon(b))
            if isinstance(b, dict) and "cache_control" in b:
                marks.append(len(units))
    return units, marks


def _tokens(unit: str) -> int:
    from nokker.context.tokens import estimate_tokens

    return estimate_tokens(unit[1:])  # la même estimation que les budgets du noyau


class CacheModel:
    """Compteur de cache d'après la STABILITÉ DU PRÉFIXE, octet pour octet, entre appels successifs (règle de la doc :
    tout ce qui précède un point de cache — outils, système, messages antérieurs — doit être identique pour être lu).
    Chaque requête écrit les préfixes qui finissent à ses points de cache ; la requête suivante LIT le plus long préfixe
    déjà écrit qui est encore un préfixe d'elle-même, ÉCRIT ce qui va jusqu'à son dernier point, et paie le reste au
    tarif d'entrée. Jetons : `nokker.context.tokens.estimate_tokens` (déterministe)."""

    def __init__(self) -> None:
        self.written: list[list[str]] = []  # préfixes déjà écrits (liste de blocs)
        self.log: list[dict[str, int]] = []

    def account(self, body: dict) -> dict[str, int]:
        units, marks = _units(body)
        total = sum(_tokens(u) for u in units)
        read_n = 0
        for w in self.written:
            if len(w) > read_n and units[:len(w)] == w:
                read_n = len(w)
        last = max(marks) if marks else 0
        read = sum(_tokens(u) for u in units[:read_n])
        write = max(0, sum(_tokens(u) for u in units[:last]) - read)
        for k in marks:
            self.written.append(units[:k])
        row = {"call": len(self.log) + 1, "total": total, "cache_read": read, "cache_write": write,
               "fresh": total - read - write}
        self.log.append(row)
        return row


def message(content: list[dict], stop_reason: str = "end_turn", usage: dict | None = None,
            model: str = MODEL) -> dict:
    return {"id": "msg_01FauxAmont", "type": "message", "role": "assistant", "model": model, "content": content,
            "stop_reason": stop_reason, "stop_sequence": None,
            "usage": usage if usage is not None else {"input_tokens": 12, "output_tokens": 5}}


def error_body(kind: str, text: str) -> dict:
    return {"type": "error", "error": {"type": kind, "message": text}, "request_id": "req_011FauxAmont"}


def sse(events: list[dict]) -> bytes:
    """Événements SSE : `event: <type>` puis `data: <json>`, comme l'amont."""
    return b"".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode() for e in events)


def stream_of(msg: dict, *, text_chunks: int = 2, json_chunks: int = 2, ping: bool = True) -> list[dict]:
    """Le flux conforme d'un `message` complet : usage d'entrée dans `message_start`, `output_tokens` cumulé et
    `stop_reason` dans `message_delta`, arguments d'outil en `input_json_delta` (JSON partiel)."""
    usage = dict(msg["usage"])
    out_tokens = usage.pop("output_tokens", 0)
    start = {**msg, "content": [], "stop_reason": None, "usage": {**usage, "output_tokens": 1}}
    events: list[dict] = [{"type": "message_start", "message": start}]
    for i, block in enumerate(msg["content"]):
        kind = block["type"]
        if kind == "text":
            events.append({"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}})
            if ping and i == 0:
                events.append({"type": "ping"})
            text = block["text"]
            step = max(1, len(text) // text_chunks)
            for at in range(0, len(text), step):
                events.append({"type": "content_block_delta", "index": i,
                               "delta": {"type": "text_delta", "text": text[at:at + step]}})
        elif kind == "thinking":
            events.append({"type": "content_block_start", "index": i,
                           "content_block": {"type": "thinking", "thinking": "", "signature": ""}})
            events.append({"type": "content_block_delta", "index": i,
                           "delta": {"type": "thinking_delta", "thinking": block.get("thinking", "")}})
            events.append({"type": "content_block_delta", "index": i,
                           "delta": {"type": "signature_delta", "signature": block.get("signature", "")}})
        elif kind == "redacted_thinking":
            events.append({"type": "content_block_start", "index": i,
                           "content_block": {"type": "redacted_thinking", "data": block["data"]}})
        elif kind == "tool_use":
            events.append({"type": "content_block_start", "index": i,
                           "content_block": {"type": "tool_use", "id": block["id"], "name": block["name"],
                                             "input": {}}})
            raw = json.dumps(block["input"])
            step = max(1, len(raw) // json_chunks)
            for at in range(0, len(raw), step):
                events.append({"type": "content_block_delta", "index": i,
                               "delta": {"type": "input_json_delta", "partial_json": raw[at:at + step]}})
        events.append({"type": "content_block_stop", "index": i})
    events.append({"type": "message_delta", "delta": {"stop_reason": msg["stop_reason"], "stop_sequence": None},
                   "usage": {"output_tokens": out_tokens}})
    events.append({"type": "message_stop"})
    return events


class FakeAnthropic:
    """`with FakeAnthropic() as up:` puis `up.reply(...)` (file de réponses) ; `up.requests` = ce qui a été reçu."""

    def __init__(self, require_thinking: bool = False, binding: str = "off", count_cache: bool = False) -> None:
        # `count_cache` : chaque réponse JSON porte un `usage` qui rend `cache_read_input_tokens`,
        # `cache_creation_input_tokens` et `input_tokens` d'après la stabilité du préfixe (`CacheModel`) ; `cache_log`
        # en garde les lignes (appel, total, lu, écrit, frais).
        self.cache = CacheModel() if count_cache else None
        # `binding` : règle de liaison des blocs de réflexion de l'API (docs « preserved thinking »). Les blocs `thinking` des
        # réponses JSON reçoivent une signature qui porte l'empreinte du préfixe de la requête (`prefix_fp`) et le bloc
        # précédent ; à chaque requête, un bloc dont un message antérieur a changé, ou dont le bloc précédent n'est plus le
        # même (sauf le premier envoyé), échoue : `error` = HTTP 400 « bound to a different conversation » (comptes
        # soumis au contrôle) ; `allow` = accepté, avec `input_transformations` (`thinking_mismatch_allowed`) si l'en-tête
        # bêta est envoyé ; `off` (défaut) = aucune liaison. Le réglage `thinking.block_binding.prefix_mismatch_behavior`
        # de la requête (`error` | `drop_block`) prime, et exige l'en-tête bêta (sinon 400, comme l'API).
        self.binding = binding
        self.dropped: list[dict] = []  # entrées `thinking_dropped` rendues
        # `require_thinking` : comme l'API avec la réflexion active — le dernier message assistant qui porte un `tool_use`
        # doit ouvrir par un bloc `thinking` (avec signature) ou `redacted_thinking` (avec `data`), sinon 400.
        self.require_thinking = require_thinking
        self.requests: list[dict[str, Any]] = []
        self._replies: list[tuple[int, dict[str, str], bytes]] = []
        self._lock = threading.Lock()
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt: str, *args: Any) -> None:
                pass

            def do_POST(self) -> None:  # noqa: N802
                n = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(n) if n else b""
                with upstream._lock:
                    upstream.requests.append({"path": self.path, "body": json.loads(raw or b"{}"),
                                              "headers": {k.lower(): v for k, v in self.headers.items()}})
                    refusal = upstream._thinking_refusal(upstream.requests[-1]["body"])
                    bound = None if refusal else upstream._bind(upstream.requests[-1])
                    if refusal:
                        status, headers, body = refusal
                    elif bound is not None and bound[0] == "refuse":
                        status, headers, body = bound[1]
                    elif upstream._replies:
                        status, headers, body = upstream._replies.pop(0)
                        if bound is not None and status == 200 and headers.get("content-type") == "application/json":
                            body = upstream._bind_reply(body, upstream.requests[-1], bound[1])
                        if upstream.cache is not None and status == 200 and headers.get("content-type") == "application/json":
                            body = upstream._cache_reply(body, upstream.requests[-1])
                    else:
                        status, headers, body = (500, {"content-type": "application/json"},
                                                 json.dumps(error_body("api_error", "aucune réponse scriptée")).encode())
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("content-length", str(len(body)))
                self.send_header("connection", "close")
                self.close_connection = True
                self.end_headers()
                self.wfile.write(body)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._httpd.server_address[1]}"

    def _thinking_refusal(self, body: dict[str, Any]) -> tuple[int, dict[str, str], bytes] | None:
        thinking = body.get("thinking")
        if not self.require_thinking or not isinstance(thinking, dict) or thinking.get("type") == "disabled":
            return None
        last = next((m for m in reversed(body.get("messages") or []) if m.get("role") == "assistant"), None)
        blocks = last.get("content") if last else None
        if not isinstance(blocks, list) or not any(b.get("type") == "tool_use" for b in blocks):
            return None
        first = blocks[0]
        ok = (first.get("type") == "thinking" and first.get("signature") and "thinking" in first) \
            or (first.get("type") == "redacted_thinking" and first.get("data"))
        if ok:
            return None
        text = ("messages.1.content.0.type: Expected `thinking` or `redacted_thinking`, but found `" + str(first.get("type"))
                + "`. When `thinking` is enabled, a final `assistant` message must start with a thinking block.")
        return 400, {"content-type": "application/json"}, json.dumps(error_body("invalid_request_error", text)).encode()

    def _bind(self, req: dict) -> tuple[str, Any] | None:
        """None = aucune liaison. ("refuse", réponse 400) ou ("ok", entrées `input_transformations` de la requête)."""
        body, headers = req["body"], req["headers"]
        setting = ((body.get("thinking") or {}).get("block_binding") or {}).get("prefix_mismatch_behavior")
        beta = BINDING_BETA in headers.get("anthropic-beta", "")
        if setting and not beta:
            text = ("thinking.block_binding: That setting requires the `thinking-binding-controls-2026-08-01` value in the "
                    "`anthropic-beta` header.")
            return "refuse", (400, {"content-type": "application/json"},
                              json.dumps(error_body("invalid_request_error", text)).encode())
        if self.binding == "off" and not setting:
            return None
        mode = setting or ("error" if self.binding == "error" else "allow")
        entries: list[dict] = []
        prev_sig = ""
        msgs = body.get("messages") or []
        bad_at: tuple[int, int] | None = None
        for i, m in enumerate(msgs):
            blocks = m.get("content") if isinstance(m.get("content"), list) else []
            for j, b in enumerate(blocks):
                if not _is_thinking(b) or not str(b.get("signature") or b.get("data") or "").startswith("bind|"):
                    continue
                sig = str(b.get("signature") or b.get("data"))
                _tag, fp, prev, _n = sig.split("|", 3)
                ok = fp == prefix_fp(body, i) and (not prev_sig or prev == _sid(prev_sig))
                if not ok:
                    entries.append({"type": "thinking_dropped" if mode == "drop_block" else "thinking_mismatch_allowed",
                                    "path": f"messages.{i}.content.{j}", "reason": "prefix_binding_mismatch"})
                    bad_at = bad_at or (i, j)
                prev_sig = sig if ok or mode != "drop_block" else prev_sig
        if bad_at and mode == "error":
            text = (f"messages.{bad_at[0]}.content.{bad_at[1]}: Invalid `signature` in `thinking` block. The block is bound "
                    "to a different conversation. Remove the block, or set `thinking.block_binding."
                    "prefix_mismatch_behavior` to \"drop_block\".")
            return "refuse", (400, {"content-type": "application/json"},
                              json.dumps(error_body("invalid_request_error", text)).encode())
        self.dropped.extend(e for e in entries if e["type"] == "thinking_dropped")
        return "ok", entries if beta else None

    def _cache_reply(self, raw: bytes, req: dict) -> bytes:
        row = self.cache.account(req["body"])
        data = json.loads(raw)
        usage = data.setdefault("usage", {})
        usage.update({"input_tokens": row["fresh"], "cache_read_input_tokens": row["cache_read"],
                      "cache_creation_input_tokens": row["cache_write"]})
        return json.dumps(data).encode()

    @property
    def cache_log(self) -> list[dict[str, int]]:
        return self.cache.log if self.cache is not None else []

    def _bind_reply(self, raw: bytes, req: dict, entries: list[dict] | None) -> bytes:
        """Signe les blocs de réflexion de la réponse (préfixe de CETTE requête + bloc précédent) et, avec l'en-tête bêta,
        ajoute `input_transformations`."""
        data = json.loads(raw)
        body = req["body"]
        prev = ""
        for m in body.get("messages") or []:
            for b in m.get("content") if isinstance(m.get("content"), list) else []:
                if _is_thinking(b) and str(b.get("signature") or b.get("data") or "").startswith("bind|"):
                    prev = str(b.get("signature") or b.get("data"))
        fp = prefix_fp(body)
        for n, b in enumerate(b for b in data.get("content") or [] if _is_thinking(b)):
            key = "signature" if b["type"] == "thinking" else "data"
            b[key] = f"bind|{fp}|{_sid(prev) if prev else ''}|{n}.{len(self.requests)}"
        if entries is not None:
            data["input_transformations"] = entries
        return json.dumps(data).encode()

    def __enter__(self) -> FakeAnthropic:
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def reply(self, body: dict, status: int = 200, headers: dict[str, str] | None = None) -> None:
        self._replies.append((status, {"content-type": "application/json", "request-id": "req_011FauxAmont",
                                       **(headers or {})}, json.dumps(body).encode()))

    def reply_stream(self, events: list[dict], *, cut: bool = False) -> None:
        """`cut` : la connexion se ferme sans `message_delta` ni `message_stop` (flux coupé)."""
        if cut:
            events = [e for e in events if e["type"] not in ("message_delta", "message_stop")]
        self._replies.append((200, {"content-type": "text/event-stream", "cache-control": "no-cache"}, sse(events)))

    def reply_raw(self, status: int, body: bytes, content_type: str = "text/html") -> None:
        self._replies.append((status, {"content-type": content_type}, body))
