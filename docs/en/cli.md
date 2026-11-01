# The `nokker` command

`nokker` is a terminal client for your local Lokkal. Run it in a folder and you get a coding session on that folder.

> The terminal speaks English by default and French when your locale is French (`LANG=fr…`); `--lang` or `NOKKER_LANG`
> chooses explicitly (see [Language](#language)).

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
nokker acp                      Agent Client Protocol agent on stdio, for an editor (see editors.md)
nokker --server <url>           use another server
nokker --lang en|fr             interface language
nokker --sound                  terminal bell on bubbles (off by default)
nokker --help | --version
```

An unknown argument, or an invalid value (`--lang de`, `--since 3x`, `-n 0`), exits with code 2 and a message on stderr (`nokker --help`). Without a terminal (stdin or stdout not a TTY) the interactive
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
 "pending_approvals": [], "refused": [{"approval_id": "...", "tool": "terminal", "effect": "write", "target": "...", "reason": "..."}], "cancelled": false,
 "cost": {"thread_id": "th_...", "turn_id": "th_...:3", "calls": 4, "subagent_calls": 0,
          "input_tokens": 48210, "cached_tokens": null, "output_tokens": 1905, "reasoning_tokens": null,
          "total_tokens": 50115, "cost": {"EUR": 0.035784}, "unpriced_models": [], "models": [...]}}
```

`cost` is the turn's cost as the core reported it (see [Cost per turn](#cost-per-turn)). It is missing when the server
does not report costs.

`cwd` is `null` when the server offers no local session. On failure: `{"error": "...", "refused": []}` and exit code 1.
Without `--json`, the reply goes to stdout and each refusal to stderr. A device that is still pending is confirmed with
`NOKKER_TOTP` (the code to confirm is printed on stderr).

## Language

Labels are in **English by default** and in French when the language starts with `fr`. Order of priority: `--lang
en|fr`, then `NOKKER_LANG`, then `LC_ALL`, `LC_MESSAGES`, `LANG` (the first non-empty one wins, as in POSIX; `C` and
`POSIX` mean English). `NOKKER_LANG=en` forces English whatever the locale. Tool lines, cards, the cost line, `/help`,
`doctor`, `logs`, `update` and the editor bridge all follow.

## In a session

Under each turn, a dim line gives its cost: `12.4 k tokens · €0.03` (in French: `12,4 k jetons · 0,03 €`). See [Cost per turn](#cost-per-turn).

Each tool call shows its effect in plain words (read, write, destructive, send, spend, secret), then its duration.
Results are folded to three lines: press `o` (with an empty input) or type `/out` to unfold, `/tools` to unfold all.
Sub-agents appear as indented lines under the call that started them.
While the model writes a file (`write_file`, `patch`, `page_create`), its last six lines scroll in dim text under the
tool line, secrets already redacted; nothing is written before the call runs (or its card is approved), and the block
folds when the result arrives.

A new session on a local folder starts in **`accept_edits`** mode: edits inside the folder go through without a card,
commands ask, destructive actions ask with a stronger card. See [permissions.md](permissions.md).

### Images

Type `@capture.png` (a file inside the session folder) or paste an image from the clipboard to attach it to your
message: up to 4 images, png, jpeg, webp or gif, 5 MB each. If the model cannot read images, a line says so and the
model is told an image was attached. Status: the core accepts images; `@file` and paste in the terminal client are
**rolling out**. See [models.md](models.md#images-in-a-request).

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
- A destructive card can be approved here, once. Some strong cards cannot be approved in the terminal at all: a change
  on a `prod` host, a send or delete through a connector of your nokker.ai account, and a strong card that comes from
  the web through the gateway. They wait for an enrolled device, such as your phone (see [security.md](security.md));
  you can still refuse them here.

### Cancelling

Esc during a turn (or `/cancel`) asks the core to stop the turn; it is not only a display change. Ctrl+C refuses the
displayed card and cancels the turn, otherwise quits.

What you type while the client is still connecting is queued and sent once the session is open (`/confirm`, `/quit`
and `/help` run at once).

### Slash commands

| Command | What it does |
|---|---|
| `/sessions [query]` | Code sessions (folders); with a query, switches to the match |
| `/threads` | All threads |
| `/goto <session>` | Switch to a thread |
| `/thread [title]` | New permanent thread (`/new` is reserved for the core's episode boundary) |
| `/diff [hash\|n]` | Coloured diff since a restore point (default: the latest; `n` = its number in `/checkpoints`), cut at 300 lines (the rest is counted) |
| `/checkpoints` | Restore points, newest first: number, short hash, time, reason, files, and the turn when the core tied the point to one |
| `/rollback [hash\|n]` | Go back to a restore point (default: the latest), through an approval card; then lists the restored and the kept files. Safe restore: your own later edits are kept |
| `/skills [name]` | Trust state of the repository's skills, then skills by family (`[project]` = repository skill); with a name, its content |
| `/skills trust` | Reopen the trust card for the repository's skills (see [below](#repository-skills-trust-card)) |
| `/plan` | The session's work plan, with each step's state (`[x]` done, `[~]` in progress, `[ ]` to do) |
| `/agents [id]` | Sub-agents of the thread, background ones included: state, last tool, duration, commits, result; with an id (or a prefix), everything including the full result |
| `/agents cancel <id\|all>` | Stop a background sub-agent; `all` stops every sub-agent of the thread and cancels the turn in progress. The worktrees are kept |
| `/cost` | Cost of the session (thread and its sub-agents) and of the day (UTC): tokens, and the amount when the model has a price |
| `/memory [note]` | Project memory: learned notes (kind, scope, origin, always in context or recalled), sections of the instruction files, and the state of the long-term memory; with a name, that note or section. See [memory.md](memory.md) |
| `/memory forget <note>` | Forget a learned note, after a confirmation card. A section of an instruction file cannot be forgotten: edit the file |
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

`/diff`, `/checkpoints`, `/rollback`, `/skills`, `/plan`, `/agents` and `/memory` only work in a code session (elsewhere: "available in a code session"). They call the
tools directly, without a model turn, through a local route of the core (`POST /v1/local/tools/call`, local token,
the session's own thread only). Only an allow-listed set of tools can be called this way (restore points, skills, the plan, `memory` when project memory is on, `delegate_status` and `delegate_cancel` when sub-agents are available), through the same gates as
the agent: `/rollback` still shows its approval card. While a turn is running on the thread, the route answers 409.

`/agents` lists the sub-agents of the thread. Scheduled agents are another thing: see [agents.md](agents.md).

### Background sub-agents

A sub-agent started with `delegate(background: true)` keeps working after the turn ends ([subagents.md](subagents.md)).
`/agents` marks it `(background)` and refreshes its state from the core (`delegate_status`): `queued`, `running`,
`stopping`, then done, failed or interrupted. A child interrupted by a core restart shows "the core restarted: worktree
kept, not inspected". `/agents cancel <id>` asks the core to stop one child (`delegate_cancel`); if the core refuses a
direct call (403), the client says so and suggests `/agents cancel all`. While a turn is running on the thread the direct
route answers 409, and the state then comes from the events.

### Repository skills: trust card

A repository's skills (`.nokker/skills/`) are ignored until you trust this folder and this exact content
([skills.md](skills.md#project-skills-need-your-trust)). When the core reports them as untrusted (when the session opens,
or on a `skill.untrusted` event because the folder changed), the terminal opens a card **Trust this repository's
skills?** showing the root, the number of skills and the full fingerprint (sha256).

- `y` trusts that fingerprint. If the folder changed since the client read it (409), a new card follows.
- `n` or Esc does nothing: the skills stay ignored, and `/skills trust` reopens the card later.
- Enter never means "trust", and the card ignores keys for its first 500 ms, so a command you are typing cannot
  answer it. One card per fingerprint.

`/skills` also shows the trust state. For a resumed session the core only gives that state when it opens the session, so
`/skills` may say it is unknown; a core without repository skills says so too.

### Forgetting a memory note

`/memory forget <note>` opens a confirmation card: `y` or Enter forgets, `n` or Esc keeps. The note is moved aside, not
destroyed. In `default` permission mode the core may also ask for its own approval card.

## Editors

`nokker acp` runs the client as an Agent Client Protocol agent on stdio (nothing else is printed on stdout). See
[editors.md](editors.md).

## Cost per turn

At the end of every turn the core publishes a `turn.cost` event, just before `turn.done`. It counts the turn's model
calls, its sub-agents' calls included:

- tokens per model: input (`input_tokens`, cache included), of which cached (`cached_tokens`), output
  (`output_tokens`, reasoning included), of which reasoning (`reasoning_tokens`). Cached and reasoning tokens are
  `null` when the endpoint did not report them: unknown is not zero;
- the cost, in the currency of the core's price table (`nokker/ledger/prices.yaml`). A model in the table gets an
  amount; a model that is not in it gets **tokens only**: the core never guesses a price. Amounts are never converted
  between currencies, so `cost` is a map such as `{"EUR": 0.0312}` (empty when nothing has a price).

For example, Qwen3.5-397B-A17B on OVH AI Endpoints is priced €0.60 per million input tokens and €3.60 per million
output tokens (no cache discount).

Session and day totals come from a read-only local route, `GET /v1/local/cost?thread_id=<thread>&day=YYYY-MM-DD`
(local token; `day` defaults to today, UTC). It returns `{session, day}`, each with the same fields plus `turns`.
Without a database, the totals start again from zero when the core restarts.

## Choosing the server

Order of priority: `--server`, then `NOKKER_SERVER_URL`, then `api_url` from `lokkal.json`, then
`http://127.0.0.1:8742`. `nokker doctor` tells you which one answered and where the choice came from. The API port is
not fixed: the installer writes the real one into `api_url`.

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
| `NOKKER_LANG` | `en` or `fr`; beats the system locale (`--lang` beats it) |
| `NOKKER_CONFIG_DIR_OVERRIDE` | Replaces the client config folder (`~/.config/nokker` on Linux) |
| `NOKKER_USER` | User name declared when enrolling a device (default: your system user) |
| `NOKKER_TOTP` | Confirms a pending device straight away in `-p` mode |
| `XDG_CONFIG_HOME` | Linux and macOS: base of the client config folder |

The config folder is `~/.config/nokker` on all three systems (Windows: `%USERPROFILE%\.config\nokker`), where the
installer writes `lokkal.json`. Older locations (macOS `~/Library/Application Support/nokker`, Windows `%APPDATA%\nokker`)
are still read as a fallback, and `doctor` warns if it finds a file there.

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

- `doctor`: Docker daemon, Lokkal compose, web page (`<web_url>/healthz`), server, authentication, long-term memory
  (state, notes, embeddings, last recall), client version against the minimum the core announces, and `git` on the host
  (needed for `/diff` and restore points). `doctor` explains a missing Docker or an unknown compose without a stack trace, and keeps checking the rest.
  `--fix` runs `docker compose up -d` when Docker and a known compose are there.
- `logs`: `docker compose logs` of the core. `-f` follows, `-n` sets the number of lines (default 100), `--since`
  takes `30m`, `1h`, `2d`. `--component cli` reads the client's own log and needs no Docker.
- `update`: runs `install.sh --update` (so the signed package is verified; if it is not found, it writes
  `NOKKER_VERSION` into `env_file` and runs `docker compose pull` then `up -d`), waits for `/healthz`, then replaces the
  `nokker` binary with the one from the same release `lokkal-v<version>`: the `.sha256` file is mandatory, and when
  `cosign` is installed the signature must verify too. The new binary is written next to the old one and moved into
  place atomically. `--check` only compares the installed version with the latest `lokkal-v*` release and, offline, says so. On Windows a
running executable cannot be replaced: the new one is written next to it and you rename it after closing `nokker`.
A client that is not the standalone binary (npm or sources) is told which asset to download instead.
