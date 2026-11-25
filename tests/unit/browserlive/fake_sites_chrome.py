"""Faux `sites-chrome` (navigateur des sites connectés de Lokkal) qui reproduit les formes de la branche nokker/v3-frames
(39db2b2) — vrai HTTP, vrai MCP streamable (réponses JSON), pour que le chemin de production (`_McpCaller` → `_SyncMcp`)
soit exercé de bout en bout :

- `POST /s/{id}/mcp` : `initialize`, `notifications/initialized` (202), `tools/call`. Après chaque GESTE (navigate,
  navigate_back, click, hover, type, fill, fill_form, press_key, select_option, scroll, drag, handle_dialog, tabs) :
  `_meta.nokker_action = {seq, tool, label, url, t, ok, sensitive, frame, title, cursor}` (url = origine + chemin, sans
  requête ; label ≤ 80, « « Champ » : texte saisi (n car.) » pour type/fill) et un item `image` WebP 640 px, sauf si
  `sensitive` ou `SC_FRAMES=0` (`frames=False`) ; `_meta.nokker_block = {kind, vendor?, detail}` seulement si bloqué.
  `browser_snapshot`, `browser_evaluate`, `browser_close` : texte seul, pas de `_meta.nokker_action`.
- `GET /s/{id}/frame` : 200 + `X-Nokker-Frame-Seq`/`X-Nokker-Frame-Time`, 204 sans capture, 404 session inactive.
- `POST /s/{id}/screen-ticket` : `{ticket, path, expires_in}`, `path = /sites/screen/<id>/?ticket=…`.
- `POST /s/{id}/resume` : `{resumed: true, was: {needsHuman, block}}`, 404 si le site n'est pas actif.
- `title` ≤ 120 ; `cursor` seulement pour click, hover, drag, select_option, fill, type (1a4e320).
Toutes les routes exigent `Authorization: Bearer <jeton du gestionnaire>`.
"""
from __future__ import annotations

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

GESTURES = {"browser_navigate", "browser_navigate_back", "browser_click", "browser_hover", "browser_type",
            "browser_fill", "browser_fill_form", "browser_press_key", "browser_select_option", "browser_scroll",
            "browser_drag", "browser_handle_dialog", "browser_tabs"}
TOKEN = "sites-token"
CURSOR_TOOLS = {"browser_click", "browser_hover", "browser_drag", "browser_select_option", "browser_fill",
                "browser_type"}


def webp(w: int = 640, h: int = 400) -> bytes:
    body = b"VP8X" + (10).to_bytes(4, "little") + b"\x00\x00\x00\x00" + (w - 1).to_bytes(3, "little") \
        + (h - 1).to_bytes(3, "little")
    return b"RIFF" + (len(body) + 4).to_bytes(4, "little") + b"WEBP" + body


class FakeSitesChrome:
    def __init__(self):
        self.frames = True  # SC_FRAMES
        self.url = "https://www.linkedin.com/login"
        self.title = "Connexion | LinkedIn"
        self.secret_refs: set[str] = set()
        self.filled: dict[str, str] = {}
        self.block: dict | None = None
        self.seq = 0
        self.last_frame: bytes | None = None
        self.calls: list[tuple[str, dict]] = []
        self.routes: list[tuple[str, str]] = []
        self.active: set[str] = set()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self) -> None:
        self.httpd.shutdown()

    # ── comportement du navigateur ──
    def _sensitive(self) -> bool:
        return any(self.filled.get(r) for r in self.secret_refs)

    def tool(self, name: str, args: dict) -> dict:
        self.calls.append((name, dict(args)))
        if name == "browser_evaluate":
            fn = str(args.get("function") or "")
            if "data-nk-secret" in fn and args.get("ref"):
                self.secret_refs.add(str(args["ref"]))
            return {"content": [{"type": "text", "text": "ok"}]}
        if name == "browser_snapshot":
            return {"content": [{"type": "text", "text": f"- page : {self.title} — {self.url}\n"
                                                         '- textbox "E-mail" [ref=e1]\n'
                                                         '- textbox "Mot de passe" [ref=e2] [password]\n'
                                                         '- button "S\'identifier" [ref=e3]\n'}]}
        if name == "browser_box":  # 00814ab : élément amené dans la vue, capture fraîche, rien n'est cliqué
            ref = str(args.get("ref"))
            if ref in self.secret_refs or ref == "e2":
                return {"content": [{"type": "text", "text": "boîte refusée : élément sensible"}], "isError": True}
            if ref not in ("e1", "e3"):
                return {"content": [{"type": "text", "text": "référence inconnue : refaire un browser_snapshot"}],
                        "isError": True}
            self.seq += 1
            sensitive = self._sensitive()
            fs = None if sensitive else self.seq
            box = {"ref": ref, "x": 0.3, "y": 0.55, "w": 0.4, "h": 0.06, "in_view": True, "frame_seq": fs}
            content = [{"type": "text", "text": json.dumps(box)}]
            if not sensitive:
                self.last_frame = webp()
                content.append({"type": "image", "data": base64.b64encode(self.last_frame).decode(),
                                "mimeType": "image/webp", "_meta": {"frame_seq": fs}})
            return {"content": content, "_meta": {"nokker_box": box, "nokker_action": {
                "seq": self.seq, "tool": "box", "url": self.url, "ok": True, "sensitive": sensitive, "frame": not sensitive,
                "frame_seq": fs, "title": self.title}}}
        if name in ("browser_close", "sites_profile_import"):
            return {"content": [{"type": "text", "text": "ok"}]}
        if name not in GESTURES:
            return {"content": [{"type": "text", "text": "ok"}]}
        label = {"e1": "E-mail", "e2": "Mot de passe", "e3": "S'identifier"}.get(str(args.get("ref")), "")
        if name == "browser_navigate":
            u = urlsplit(str(args.get("url")))
            self.url = f"{u.scheme}://{u.netloc}{u.path}"
            self.filled.clear()
            label = self.url
        if name in ("browser_type", "browser_fill"):
            self.filled[str(args.get("ref"))] = str(args.get("text") or "")
            label = f"« {label} » : texte saisi ({len(str(args.get('text') or ''))} car.)"
        self.seq += 1
        sensitive = self._sensitive()
        action = {"seq": self.seq, "tool": name, "label": label[:80], "url": self.url, "t": 1_800_000_000_000,
                  "ok": True, "sensitive": sensitive, "frame": not sensitive, "title": self.title[:120]}
        if name in CURSOR_TOOLS:  # au point réel du geste ; absent pour navigate, scroll, press_key
            action["cursor"] = {"x": 0.42, "y": 0.61}
        meta: dict = {"nokker_action": action}
        if self.block:
            meta["nokker_block"] = dict(self.block)
        content = [{"type": "text", "text": f"{name} : ok"}]
        if not sensitive:
            self.last_frame = webp()
            if self.frames:
                content.append({"type": "image", "data": base64.b64encode(self.last_frame).decode(),
                                "mimeType": "image/webp"})
        return {"content": content, "_meta": meta, "isError": False}

    # ── HTTP ──
    def _handler(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, body=b"", ctype="application/json", headers=None):
                self.send_response(status)
                if body:
                    self.send_header("Content-Type", ctype)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def _auth(self) -> bool:
                if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    self._send(401, b'{"error":"unauthorized"}')
                    return False
                return True

            def do_GET(self):
                if not self._auth():
                    return
                parts = urlsplit(self.path).path.strip("/").split("/")
                fake.routes.append(("GET", self.path))
                if len(parts) == 3 and parts[0] == "s" and parts[2] == "frame":
                    if parts[1] not in fake.active:
                        return self._send(404, b'{"error":"inactive"}')
                    if fake.last_frame is None:
                        return self._send(204)
                    return self._send(200, fake.last_frame, "image/webp",
                                      {"X-Nokker-Frame-Seq": str(fake.seq), "X-Nokker-Frame-Time": "1800000000000"})
                self._send(405)  # pas de flux SSE serveur

            def do_DELETE(self):
                self._send(200)

            def do_POST(self):
                if not self._auth():
                    return
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                parts = urlsplit(self.path).path.strip("/").split("/")
                if len(parts) == 3 and parts[0] == "s" and parts[2] == "screen-ticket":
                    fake.routes.append(("POST", self.path))
                    body = {"ticket": "tk123", "path": f"/sites/screen/{parts[1]}/?ticket=tk123", "expires_in": 900}
                    return self._send(200, json.dumps(body).encode())
                if len(parts) == 3 and parts[0] == "s" and parts[2] == "resume":
                    fake.routes.append(("POST", self.path))
                    if parts[1] not in fake.active:
                        return self._send(404, b'{"error":"not active"}')
                    was = {"needsHuman": fake.block is not None, "block": fake.block}
                    fake.block = None
                    return self._send(200, json.dumps({"resumed": True, "was": was}).encode())
                if len(parts) == 3 and parts[0] == "s" and parts[2] == "mcp":
                    fake.active.add(parts[1])
                    msg = json.loads(raw or b"{}")
                    if "id" not in msg:  # notification
                        return self._send(202)
                    if msg.get("method") == "initialize":
                        result = {"protocolVersion": msg["params"]["protocolVersion"],
                                  "capabilities": {"tools": {}}, "serverInfo": {"name": "sites-chrome", "version": "t"}}
                    elif msg.get("method") == "tools/call":
                        result = fake.tool(msg["params"]["name"], msg["params"].get("arguments") or {})
                    elif msg.get("method") == "tools/list":
                        result = {"tools": []}
                    else:
                        result = {}
                    out = json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}).encode()
                    return self._send(200, out, headers={"mcp-session-id": f"sess-{parts[1]}"})
                self._send(404, b'{"error":"not found"}')

        return H
