---
name: social-setup
description: Connect a post scheduler and a LinkedIn MCP server by name
---
# Setting up the social pack

The pack talks to two MCP servers that **you** choose and run. It never ships an account, an address or a key.

## 1. A scheduling server (role `scheduler`)

Any tool that schedules posts and exposes an MCP server works. One possible choice is **Postiz**: open source, it
can be self-hosted, and its MCP server uses the tool names this pack expects by default (`integrationList`,
`postsListTool`, `integrationSchedulePostTool`). With another tool, note the names of its three equivalent tools.

## 2. A LinkedIn server (role `linkedin`)

An MCP server that you run with your own LinkedIn API access, able to list your posts, list their comments, reply
to a comment and publish a post. Note its tool names.

## 3. Credentials, by name only

1. The user stores each key in the local vault, themselves, outside the conversation:
   `nokker secret set SCHEDULER_API_KEY` and `nokker secret set LINKEDIN_TOKEN` (other names are fine).
2. Never ask the user to paste a key into the chat. If they do, tell them to rotate it.

## 4. Adding the servers

Call `mcp_server_add` once per server, with the URL of the server and the **reference** to the key:

```
mcp_server_add {name: "scheduler", url: "https://<your scheduler host>/mcp",
                token: "secret:SCHEDULER_API_KEY", purpose: "post scheduling"}
```

The call asks for an approval card. The server becomes active at the next restart of the core.

## 5. Installing the pack with your tool names

If your servers use other tool names or other secret names, install with a binding:

```
pack_install {name: "social", bind: {"scheduler.schedule_post": "<your tool>",
                                     "secret:LINKEDIN_TOKEN": "<your secret name>"}}
```

A binding to an empty secret name removes that secret from the proposed agents (for a server that needs none).

## 6. Check

`mcp_server_list` shows the servers, the secret names they use, and the names still missing from the vault.
