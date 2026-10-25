---
name: mcp-scaffold
description: Create an MCP server skeleton with tools and tests
---
# Scaffolding an MCP server

You write a small Python MCP server (streamable HTTP) in the project folder, with unit tests. The templates of
this skill run as they are: start from them.

## 1. Design the tools first

Write `mcp/<name>/DESIGN.md` with one line per tool before any code:

| Tool | Reads or changes | Arguments (typed) | Returns | Errors |
|---|---|---|---|---|

Guidelines:

- 3 to 8 tools. One verb per tool (`search_orders`, `get_order`, `create_refund`), not one "do anything" tool.
- Mark each tool as a read or a change. Changes that reach other people (send, publish, pay) are named so
  (`send_invoice`, not `process_invoice`): the user's approval rules rely on it.
- Arguments: plain types, small enums, explicit limits (`limit` 1-100). No free-form "options" object.
- Never take a credential as a tool argument. The server reads its own credential from its environment.

## 2. Copy the templates

Read each template with `skill_view` (`file: templates/<name>`) and write it into `mcp/<name>/`:

- `templates/server.py`: the server (in-memory example tools; replace them);
- `templates/test_server.py`: unit tests that call the tool functions directly;
- `templates/smoke_client.py`: connects to a running server, lists tools, calls one;
- `templates/requirements.txt`.

Rename `SERVER_NAME`, then replace the example tools with the ones of `DESIGN.md`. Keep the docstrings precise:
they are what the agent reads to choose a tool.

## 3. Wrapping an HTTP API

For a server in front of an existing API, add `httpx` to the requirements and build one client:

```python
def _client() -> httpx.Client:
    headers = {"Accept": "application/json"}
    credential = os.environ.get("UPSTREAM_API_KEY", "")
    if credential:
        headers["Authorization"] = "Bearer " + credential
    return httpx.Client(base_url=os.environ.get("UPSTREAM_URL", ""), headers=headers, timeout=20)
```

Each tool calls one endpoint, checks the status, and returns only the fields the agent needs (not the whole
payload). Map API errors to clear messages; never include headers or the credential in an error.

## 4. Tests

One test per tool for the normal case, one for bad input, and one that lists the declared tools. For an HTTP API,
replace the client with a fake (`httpx.MockTransport`) so tests run without the network.

Next: the `mcp-test` skill.
