# The `nokker` command

`nokker` is a terminal client for your local Lokkal. Run it in a folder and you get a coding session on that folder.

> Terminal labels are currently in French (tool lines, approval card). English labels are planned. This page uses the
> English names.

```sh
cd ~/nokker/my-project
nokker
```

The folder must be under a work folder given at install time (`--workspace`, default `~/nokker`). Otherwise the core
refuses (HTTP 403, "folder outside the declared roots"). Opening `nokker` again in the same folder resumes that
folder's thread.

## Commands

```
nokker                          interactive session on the current folder
nokker -p "request"             one turn, answer on stdout (unattended)
nokker -p "request" --json      same, as JSON
nokker doctor [--fix]           checks
nokker logs [-f] [-n N] [--since 1h|30m|2d] [--component core|cli]
nokker update [--check]         update the core, then the binary
nokker --server <url>           use another server
nokker --sound                  terminal bell on bubbles (off by default)
nokker --help | --version
```

An unknown argument exits with code 2 (`nokker --help`). Without a terminal (stdin or stdout not a TTY) the interactive
UI never starts; use `-p`.

### Unattended mode: `-p` and `--json`

`nokker -p "fix the failing test in tests/test_add.py"` runs one turn in the current folder and prints the answer.
Nobody can answer an approval card in this mode, so **every effect that no rule already covers is refused**. Refusals
are listed on stderr (`tool`, `effect`, `target`). Rules and the active mode decide what is covered (see
[permissions.md](permissions.md)); to let a script run its test command, add a rule such as `terminal(npm test*)` to
your settings.

`--json` prints one object:

```json
{"reply": "...", "thread_id": "th_...", "cwd": "/home/you/nokker/my-project",
 "pending_approvals": [], "refused": [{"tool": "terminal", "effect": "write", "target": "..."}], "cancelled": false}
```

On failure: `{"error": "...", "refused": []}` and exit code 1.

## In a session

Each tool call shows its effect in plain words (read, write, destructive, send, spend, secret), then its duration.
Results are folded to three lines: press `o` (with an empty input) or type `/out` to unfold, `/tools` to unfold all.
Sub-agents appear as indented lines under the call that started them.

A new session on a local folder starts in **`accept_edits`** mode: edits inside the folder go through without a card,
commands ask, destructive actions ask with a stronger card. See [permissions.md](permissions.md).

### Approval card

When something needs your decision, the card shows the tool, its effect, and the command or path:

```
1. Once
2. For this session
3. Always this command           (writes the exact rule shown)
3b. Always commands that start with ...*
4. Refuse
```

Arrow keys, digits and Enter. For a test command (`pytest`, `unittest`, `npm test`, `pnpm test`, `yarn test`,
`go test`, `cargo test`, `make test`, `vitest`, `jest`), "For this session" comes first.

- "Always" writes an `allow` rule into your user settings file (see [permissions.md](permissions.md)). The rule is one
  of the suggestions the core computed for that card; the client cannot invent one.
- A destructive card offers **once** only: no session or always scope.
- Esc does not decide. Ctrl+C refuses the card and cancels the turn. A card expires after 300 s and counts as a refusal.
- A card that comes from the web (through the gateway) and is strong must be decided on an enrolled device, never in
  the terminal.

### Cancelling

Esc during a turn (or `/cancel`) asks the core to stop the turn; it is not only a display change. Ctrl+C refuses the
displayed card and cancels the turn, otherwise quits.

### Slash commands

| Command | What it does |
|---|---|
| `/sessions [query]` | Code sessions (folders); with a query, switches to the match |
| `/threads` | All threads |
| `/goto <session>` | Switch to a thread |
| `/thread [title]` | New permanent thread (`/new` is reserved for the core's episode boundary) |
| `/diff [hash]` | Diff since a restore point (default: the last one) |
| `/checkpoints` | Restore points: short hash, time, reason, files |
| `/rollback [hash\|n]` | Go back to a restore point, through an approval card |
| `/skills [name]` | Skills by family; with a name, its content |
| `/plan` | The session's work plan, with each step's state |
| `/agents` | Sub-agents of the thread: goal, state, last tool, duration |
| `/cancel` | Cancel the running turn (Esc too) |
| `/out` | Unfold or fold the preview of the last tool call (`o` too) |
| `/tools` | Unfold or fold all previews |
| `/bubbles` | Open bubbles of this thread |
| `/bubble <id\|title>` | Detail of a bubble |
| `/send <bubble> <session> [--copy]` | Move (or copy) a bubble to another thread |
| `/ask <session> <text>` | Ask another session a question; the answer comes back as a bubble |
| `/approvals` | Approval cards |
| `/confirm <TOTP code>` | Confirm this device when it is pending |
| `/login` | Reconnect |
| `/help` | The list above |
| `/quit` (`/exit`) | Leave |

`/diff`, `/checkpoints`, `/rollback`, `/skills`, `/plan` and `/agents` only work in a code session. They call a local
tool route of the core (`POST /v1/local/tools/call`) that is **planned**: the client side is ready, and the core side
is being merged. Until then, ask the agent in plain words ("list the restore points", "roll back to the previous
one"): the same tools are available to it.

## Choosing the server

Order of priority: `--server`, then `NOKKER_SERVER_URL`, then `api_url` from `lokkal.json`, then
`http://127.0.0.1:8742`. `nokker doctor` tells you which one answered.

## Connecting

1. On the same machine, the core writes a **local token** (`~/.nokker/state/local-token`, mode 0600; its path is in
   `lokkal.json`). The client sends it as `Authorization: Bearer`. The core accepts it only with a `Host` of
   `127.0.0.1` or `localhost`, which a web page cannot forge. No enrolment needed.
2. Otherwise (another server), the client enrols a device key (P-256, in your keychain or a 0600 file) and, if the
   device is pending, prints a short code to confirm on the host, or with `/confirm <TOTP>` / `NOKKER_TOTP`.

## Environment variables

| Name | Role |
|---|---|
| `NOKKER_SERVER_URL` | Server URL (default `http://127.0.0.1:8742`) |
| `NOKKER_CONFIG_DIR_OVERRIDE` | Replaces the client config folder (`~/.config/nokker` on Linux) |
| `NOKKER_USER` | User name declared when enrolling a device (default: your system user) |
| `NOKKER_TOTP` | Confirms a pending device straight away in `-p` mode |
| `XDG_CONFIG_HOME` | Linux: base of the client config folder |

Client files, in the config folder: `lokkal.json`, `cli.log` (read it with `nokker logs --component cli`), and, when
you enrol a device, `device-key.json` and `session.json`.

## `lokkal.json`

Written by the installer (mode 0600).

```json
{"compose": "/home/you/.nokker/docker-compose.yml", "project": "nokker", "version": "0.1.0",
 "core_service": "core", "api_url": "http://127.0.0.1:8742", "web_url": "http://127.0.0.1:7420",
 "token_path": "/home/you/.nokker/state/local-token", "env_file": "/home/you/.nokker/.env",
 "install_dir": "/home/you/.nokker"}
```

`compose`, `project`, `version`, `core_service`, `api_url`, `web_url`, `token_path` and `env_file` are required;
`compose`, `token_path` and `env_file` must be absolute. On Windows an extra `wsl_distro` makes `update`, `logs` and
the Docker check of `doctor` go through `wsl.exe -d <distro> -- ...`, and `compose`, `env_file` and `install_dir`
are Linux paths.

## `doctor`, `logs`, `update`

- `doctor`: Docker daemon, Lokkal compose, web page (`<web_url>/healthz`), server, authentication, client version
  against the minimum the core announces, and `git` on the host (needed for `/diff` and restore points).
  `--fix` runs `docker compose up -d` when Docker and a known compose are there.
- `logs`: `docker compose logs` of the core. `-f` follows, `-n` sets the number of lines (default 100), `--since`
  takes `30m`, `1h`, `2d`. `--component cli` reads the client's own log and needs no Docker.
- `update`: runs `install.sh --update` (so the signed package is verified), waits for `/healthz`, then replaces the
  `nokker` binary with the one from the same release `lokkal-v<version>`: the `.sha256` file is mandatory, and when
  `cosign` is installed the signature must verify too. The new binary is written next to the old one and moved into
  place atomically. `--check` only compares versions and works offline (it says so).
