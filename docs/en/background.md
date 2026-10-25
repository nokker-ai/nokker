# Background commands

Some commands never return: a dev server, a `watch` build, `tail -f`. The `terminal` tool waits for a command to
finish, so the agent starts these in the background instead, keeps working, and reads their output when it needs to.

Status: on in the Lokkal install (`local.background_processes: true` in the core config); off in a core started
without that setting. Port previews are a first version, with the limits listed below.

## Tools the agent has

| Tool | Effect | What it does |
|---|---|---|
| `process_start(command, name?, port?)` | same as the command | Start the command in the background in the work folder (inside the sandbox). Returns an id `bg_…` and the first lines it printed. |
| `process_list` | READ | The session's background processes: `running`, `exited` (with its exit code), `stopped` or `lost`. |
| `process_logs(id, tail = 200)` | READ | The end of the process's output (stdout and stderr), at most 2000 lines. |
| `process_stop(id)` | READ | Stop one of the session's own processes: SIGTERM to its process group, SIGKILL after 5 seconds. No card. |

- `process_start` is classified exactly like `terminal`: a read-only command is `READ`, a dangerous one is
  `DESTRUCTIVE`, anything else `WRITE`, so it asks for the same cards in the same modes. Hardline commands are
  refused.
- Rules: a `terminal(pattern)` rule also covers the same command started with `process_start`; a
  `process_start(pattern)` rule only covers background starts. Example: `"allow": ["process_start(npm run dev*)"]`.
- At most **4** running processes per session. Long output is cut; the full result goes to the session's context
  reservoir, like any long tool result.
- Everything stops with the session. With the sandbox, the container is removed, and whatever ran in it goes too; the
  core also removes leftover sandboxes when it starts.

## Where it runs

- **Sandbox (default)**: inside the session's container; the log lives in the container (`/tmp/.nokker-bg`). No port is
  published on your host.
- **Host (`--backend local`)**: in its own process group on your machine; logs under the state folder
  (`~/.nokker/state/processes`). A process is only ever signalled after checking it still carries its Nokker marker, so
  a recycled process id is never touched.

## Previewing a port

When the agent passes `port` (for example `process_start("npm run dev -- --port 5173", port=5173)`), the core can show
you that server privately, without publishing anything on your host: each request is replayed from inside the
sandbox to `127.0.0.1:<port>`.

```
GET  /v1/local/processes                      background processes of open sessions (local token)
POST /v1/local/processes/<id>/link            {url, expires_at, port}: a signed link, valid one hour (local token)
GET  <url><path>                              the app, with the same sandbox headers as session pages
```

The link carries its signature in the path, so the app's relative URLs (styles, scripts, images) keep working in an
`<iframe>`. Limits of this first version:

- local only: not through the gateway (`<name>.nokker.app`);
- `GET` and `HEAD` only, no WebSocket (no hot reload);
- each request goes through the sandbox (under a second each): fine to look at a page, slow for an app with hundreds
  of files;
- the page runs in an opaque origin (no cookies, no storage);
- an app that uses absolute paths (`/assets/app.js`) breaks out of the link: start it with a relative base
  (`vite --base ./`, `PUBLIC_URL=.`, and so on).
