---
name: mcp-install
description: Add a tested MCP server to the config, secrets by name
locality: local
---
# Installing an MCP server

Adding a server changes what every later session can do: `mcp_server_add` always asks for an approval card, and
the server becomes active at the next restart of the core, which the user does themselves.

## 1. Where it runs

The core must reach the server. Choose with the user:

- a service next to the core (same machine or the same container network): `http://<service-name>:8765/mcp`;
- the loopback of the machine that runs the core: `http://127.0.0.1:8765/mcp`;
- anything else: behind `https://`.

The sandbox of a session is not a place to keep a server running: it stops with the thread.

## 2. Credentials, by name only

Two different credentials may exist:

- the key **the core sends to the server** (if the server checks one): stored in the vault by the user,
  `nokker secret set <NAME>`, then referenced as `token: "secret:<NAME>"`;
- the key **the server sends to an upstream API**: it belongs to the server's own environment where it is
  deployed, never in the core's configuration and never in a tool argument.

Never ask for a value in the chat, never write one in a file of the project. If the user pastes a key, tell them
to rotate it.

## 3. Add

```
mcp_server_add {name: "<name>", url: "<address>/mcp", token: "secret:<NAME>",
                purpose: "<one sentence: what it is for>"}
```

The call refuses a literal value where a `secret:NAME` is expected, credentials inside the URL, and plain
`http://` to anything but the loopback or a local service name. The result lists the secret names still missing
from the vault: the user sets them.

## 4. After the restart

1. `mcp_server_list`: the server is there, no missing secret.
2. Call one read tool of the new server in a session and check the result.
3. Every tool of an added server is treated as a change (it asks for a card) unless its effect is declared. A
   pack can declare effects of the tools it expects (see `pack_list`); a declared effect can only be stricter.

## Removing

`mcp_server_remove {name}` (approval card), then restart. Servers of the main configuration file are not touched
from a session.
