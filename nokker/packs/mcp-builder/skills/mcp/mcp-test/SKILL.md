---
name: mcp-test
description: Run an MCP server in the sandbox and test it end to end
---
# Testing an MCP server in the sandbox

Everything here runs in the sandbox of the session. Nothing is installed in the configuration yet.

## 1. Environment

```
cd mcp/<name>
python -m venv .venv
.venv/bin/pip install -r requirements.txt
```

If the sandbox has no network for pip, say so and stop; do not try other package sources on your own.

## 2. Unit tests

```
.venv/bin/python -m pytest -q
```

All green before going further. A failing test is fixed in the code, not by editing the test to match.

## 3. Start the server in the background

Use `process_start` with a clear name, so it is stopped at the end of the thread:

```
process_start {name: "mcp-<name>", command: "cd mcp/<name> && MCP_PORT=8765 .venv/bin/python server.py"}
```

Check it listens with `process_logs` (look for the address it prints). The server binds to 127.0.0.1 only.

## 4. End-to-end smoke test

```
cd mcp/<name> && .venv/bin/python smoke_client.py http://127.0.0.1:8765/mcp <read tool> '<json arguments>'
```

Exit code 0 and the expected output: the protocol, the tool list and one real call work. Test one read tool, then
one bad call (wrong argument) and check that the error is clear and contains no secret.

For a server in front of a real API, the end-to-end test needs its credential in the sandbox environment. Do not
put it there yourself: ask the user, and prefer testing against a fake or a sandbox account of the API.

## 5. Stop

`process_stop` the server. Report: tests passed, tools listed, calls made and their results.

Next: the `mcp-install` skill.
