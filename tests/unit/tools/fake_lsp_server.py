"""Faux serveur de langage sur stdio pour tests/unit/tools/test_lsp.py — déterministe : chaque ligne du document qui
contient `ERR <code> <message>` donne une erreur à cette ligne (colonne du marqueur), `WARN <code> <message>` un
avertissement ; `ERR - <message>` une erreur sans code. Options : `--no-version` (publications sans version),
`--delay S` (attente avant chaque publication), `--silent` (ne publie jamais), `--ask-config` (requête
workspace/configuration au client avant la première publication, dont la réponse est attendue), `--crash-init`
(sort à l'initialize)."""
from __future__ import annotations

import json
import re
import sys
import time

ARGS = sys.argv[1:]
MARK = re.compile(r"(ERR|WARN) (\S+) (.+)$")


def read() -> dict | None:
    length = None
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            break
        k, _, v = line.decode().partition(":")
        if k.lower() == "content-length":
            length = int(v)
    return json.loads(sys.stdin.buffer.read(length))


def send(msg: dict) -> None:
    body = json.dumps(msg).encode()
    sys.stdout.buffer.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
    sys.stdout.buffer.flush()


def diagnostics(text: str) -> list[dict]:
    out = []
    for i, line in enumerate(text.splitlines()):
        m = MARK.search(line)
        if not m:
            continue
        d = {"range": {"start": {"line": i, "character": m.start()}, "end": {"line": i, "character": len(line)}},
             "severity": 1 if m.group(1) == "ERR" else 2, "message": m.group(3).replace("\\n", "\n")}
        if m.group(2) != "-":
            d["code"] = m.group(2)
        out.append(d)
    return out


def main() -> None:
    delay = float(ARGS[ARGS.index("--delay") + 1]) if "--delay" in ARGS else 0.0
    asked = "--ask-config" not in ARGS
    while True:
        msg = read()
        if msg is None:
            return
        method = msg.get("method")
        if method == "initialize":
            if "--crash-init" in ARGS:
                return
            send({"jsonrpc": "2.0", "id": msg["id"], "result": {"capabilities": {"textDocumentSync": 1}}})
        elif method == "shutdown":
            send({"jsonrpc": "2.0", "id": msg["id"], "result": None})
        elif method == "exit":
            return
        elif method in ("textDocument/didOpen", "textDocument/didChange"):
            p = msg["params"]
            doc = p["textDocument"]
            text = doc["text"] if method == "textDocument/didOpen" else p["contentChanges"][-1]["text"]
            if not asked:
                send({"jsonrpc": "2.0", "id": 900, "method": "workspace/configuration",
                      "params": {"items": [{"section": "python"}]}})
                reply = read()
                assert reply and reply.get("id") == 900 and reply.get("result") == [None], reply
                asked = True
            if "--silent" in ARGS:
                continue
            if delay:
                time.sleep(delay)
            params = {"uri": doc["uri"], "diagnostics": diagnostics(text)}
            if "--no-version" not in ARGS:
                params["version"] = doc["version"]
            send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics", "params": params})


if __name__ == "__main__":
    main()
