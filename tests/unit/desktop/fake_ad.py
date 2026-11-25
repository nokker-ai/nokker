"""Faux agent-desktop pour les tests du Bureau : rend des enveloppes JSON de la forme relevée au spike (06.11.2026),
à partir de vrais arbres (tests/fixtures/desktop/), et note chaque appel (argv, stdin, AGENT_DESKTOP_HOME)."""
from __future__ import annotations

import copy
import gzip
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FIX = Path(__file__).resolve().parents[2] / "fixtures" / "desktop"


def fixture(name: str) -> dict[str, Any]:
    p = FIX / f"{name}.json"
    if p.is_file():
        return json.loads(p.read_text(encoding="utf-8"))
    return json.loads(gzip.decompress((FIX / f"{name}.json.gz").read_bytes()))


def walk(node: dict[str, Any]):
    yield node
    for c in node.get("children") or ():
        yield from walk(c)


@dataclass
class Out:
    stdout: str
    returncode: int = 0


@dataclass
class FakeAD:
    """`windows` : id → (appli, titre, nom de fixture) ; `fail` : commande → code d'erreur (headless seulement si
    `headless_only`) ; `status` : enveloppe de status ; `calls` : (argv, stdin, home)."""

    windows: dict[str, tuple[str, str, str]] = field(default_factory=lambda: {
        "w-gtk4": ("python3.12", "Login Demo", "gtk4-i"),
        "w-gte": ("gnome-text-editor", "New Document (Draft) - Text Editor", "gte-i"),
        "w-term": ("gnome-terminal-server", "Terminal", "gte-i"),
        "w-lo": ("soffice", "Untitled 1 — LibreOffice Writer", "lo-i"),
    })
    status: dict[str, Any] = field(default_factory=lambda: {
        "version": "2.6", "ok": True, "command": "status",
        "data": {"permissions": {"accessibility": {"state": "granted"}, "automation": {"state": "not_required"}},
                 "platform": "linux", "supported_surfaces": ["window", "sheet", "alert", "menu"], "version": "1.2.0"}})
    fail: dict[str, str] = field(default_factory=dict)
    headless_only: bool = True
    list_windows_error: dict[str, Any] | None = None
    find_override: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    calls: list[tuple[list[str], str | None, str | None]] = field(default_factory=list)
    focused: str | None = None
    focus_works: bool = True

    def __call__(self, argv, input=None, capture_output=True, text=True, timeout=None, env=None):  # noqa: A002
        env = env or {}
        self.calls.append((list(argv), input, env.get("AGENT_DESKTOP_HOME")))
        args = list(argv[1:])
        headed = False
        if args and args[0] == "--headed":
            headed, args = True, args[1:]
        cmd = args[0]
        return Out(json.dumps(self.run(cmd, args[1:], headed, input)))

    # ── commandes ──
    def run(self, cmd: str, a: list[str], headed: bool, stdin: str | None) -> dict[str, Any]:
        code = self.fail.get(cmd)
        if code and not (headed and self.headless_only):
            return {"version": "2.6", "ok": False, "command": cmd,
                    "error": {"code": code, "disposition": {"retry": "safe"}}}
        if cmd == "status":
            return self.status
        if cmd == "list-windows":
            if self.list_windows_error is not None:
                return {"ok": False, "error": self.list_windows_error}
            # forme réelle (B1b) : app_name, pas app
            # forme réelle (B1b) : liste directe dans data, app_name, is_focused
            return {"ok": True, "data": [{"id": k, "app_name": v[0], "title": v[1], "pid": 25,
                                          "is_focused": k == self.focused, "visible": True}
                                         for k, v in self.windows.items()]}
        if cmd == "snapshot":
            wid = a[a.index("--window-id") + 1]
            app, _title, fx = self.windows[wid]
            env = copy.deepcopy(fixture("lo-sk" if fx == "lo-i" and "--skeleton" in a else fx))
            env["data"]["app"] = app
            return env
        if cmd == "find":
            wid = a[a.index("--window-id") + 1]
            key = "|".join(a)
            if key in self.find_override:
                return {"ok": True, "data": {"matches": self.find_override[key]}}
            for k, v in self.find_override.items():
                if k == wid or k == "*":
                    return {"ok": True, "data": {"matches": v}}
            role = a[a.index("--role") + 1] if "--role" in a else None
            name = a[a.index("--name") + 1] if "--name" in a else None
            exact = "--exact" in a
            tree = fixture(self.windows[wid][2])
            out = []
            for n in walk(tree["data"]["tree"]):
                if not n.get("ref_id"):
                    continue
                if role and n.get("role") != role:
                    continue
                if name is not None and (n.get("name") != name if exact else name not in (n.get("name") or "")):
                    continue
                m = {k: n[k] for k in ("role", "name", "states") if k in n}
                m["ref_id"] = "@sfind0001:" + n["ref_id"].split(":")[1]
                out.append(m)
            return {"ok": True, "data": {"matches": out}}
        if cmd == "press":  # sous X11 : XTEST, même sans --headed (B1b)
            return {"ok": True, "command": cmd, "data": {
                "action": cmd, "disposition": {"delivery": "delivered_unverified", "retry": "unsafe"},
                "steps": [{"label": "XTEST.press_key", "mechanism": "physical_synthetic", "outcome": "succeeded"}]}}
        if cmd in ("click", "type", "set-value", "select", "check", "uncheck", "press", "clipboard-set"):
            return {"ok": True, "command": cmd, "data": {
                "action": cmd, "disposition": {"delivery": "delivered_verified", "retry": "unsafe"},
                "post_state": {"role": "textfield", "name": "Utilisateur", "states": ["focused"]}}}
        if cmd == "clipboard-clear":
            return {"ok": True, "data": {}}
        if cmd == "get":
            return {"ok": True, "data": {"value": "écrire à alice@example.com ou IBAN CH93 0076 2011 6238 5295 7"}}
        if cmd == "launch":
            return {"ok": True, "data": {"window": {"id": "w-new", "title": "Nouveau"}}}
        if cmd == "focus-window":
            if self.focus_works:
                self.focused = a[a.index("--window-id") + 1]
            return {"ok": True, "data": {}}
        if cmd == "wait":
            return {"ok": True, "data": {"window": {"title": "Save As"}}}
        return {"ok": False, "error": {"code": "INVALID_ARGS"}}

    def argvs(self, cmd: str) -> list[list[str]]:
        out = []
        for argv, _stdin, _home in self.calls:
            a = argv[1:]
            if a and a[0] == "--headed":
                a = a[1:]
            if a and a[0] == cmd:
                out.append(argv)
        return out
