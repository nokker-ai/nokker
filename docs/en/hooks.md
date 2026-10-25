# Hooks

A hook is a command of yours that Nokker runs at a given moment of a session: when it opens, before or after a tool
call, at the end of a turn. Use them to run a linter after each edit, refuse a push to a protected branch, or record
what happened.

Hooks are **enabled** in installs made by `install.sh` (`local.hooks: true` in the core config). For `nokker code` run
from a checkout, pass `--hooks` or set `local.hooks: true`.

## Events

| Event | When | What the exit code does |
|---|---|---|
| `session_start` | the session opens | nothing; reported as an event |
| `pre_tool` | a tool call is about to run (already allowed by a rule, the mode, or an approved card) | non-zero **blocks** the call; the tool is not run and its result is an error carrying the hook's output |
| `post_tool` | a tool call has run | its output (and a non-zero code) is **added to the result** as a note the model reads |
| `turn_end` | the turn is over | nothing; reported as an event |

## Settings

Hooks live under `hooks`, next to `permissions`, in the same settings files (see [permissions.md](permissions.md)):

```json
{"hooks": {
  "pre_tool":  [{"match": "terminal(git push*)", "command": "./scripts/check-branch.sh", "timeout_s": 10}],
  "post_tool": [{"match": "write_file(src/**)", "command": "ruff check --quiet src"},
                {"match": "patch(src/**)",      "command": "ruff check --quiet src"}],
  "turn_end":  [{"command": "git status --short"}],
  "subagents": false
}}
```

- `match` uses the permission rule syntax: `tool` or `tool(pattern)`, matched against the command for `terminal` and
  the normalised path for file tools. Missing or `*` means every tool. It is ignored for `session_start` and
  `turn_end`.
- `timeout_s`: default 30, between 1 and 300. A hook that times out exits with code 124; for `pre_tool` that blocks.
- An invalid block (unknown key, unreadable rule, empty command) is ignored as a whole, and the session says so.
- Several hooks for the same event run in order: yours first, then the repository's. The first `pre_tool` that fails
  stops the call.

## What a hook receives

The command runs **inside the session sandbox**, in the work folder (`/workspace`), never on your host (unless you
chose `--backend local`, where commands run on the host anyway). It gets a JSON document on stdin:

```json
{"event": "post_tool", "thread_id": "th_…", "tool": "write_file",
 "arguments": {"path": "src/app.py", "content": "…"},
 "result": {"content": "…", "is_error": false}}
```

`turn_end` gets `{event, thread_id, turn_index, error, reply}`; `session_start` gets `{event, thread_id}`. Arguments
and results go through the same secret redaction as the model's context. Output (stdout and stderr) is redacted too
and cut to 4,000 characters.

Each run is reported as an event `hook.ran {thread_id, event, match, source, exit_code, duration_ms, timed_out}`
(with `output` for `session_start` and `turn_end`).

## Repository hooks need your trust

A `.nokker/settings.json` comes with the repository's code, so it may come from someone else. Its hooks are
**ignored** until you trust them for **this folder** and **this exact block**:

1. When a session opens on a repository with hooks you have not trusted, the core says so (the `hooks.notice` of the
   session, and a `hook.untrusted` event), with the block's fingerprint (SHA-256 of its canonical JSON).
2. Read the block. Then trust it, from the same machine:
   - `POST /v1/local/hooks/trust {"thread_id": "th_…", "sha256": "<fingerprint>"}` with the local token; or
   - `python -m nokker.cli hooks show` then `python -m nokker.cli hooks trust --sha256 <fingerprint>` in the folder.
3. The trust is written to **your** settings (`trusted_project_hooks: [{root, sha256}]`), never to the repository's.
   The hooks are active right away for that session and for the next ones.

If the block changes, its fingerprint changes and the hooks are ignored again until you trust the new block. The
fingerprint you send must be the one you read, otherwise the request is refused. Trusting is not possible through the
gateway (`<name>.nokker.app`): only on the machine. A repository cannot trust itself: a `trusted_project_hooks` in
`.nokker/settings.json` is ignored.

Both blocks are read once, when the session opens: a hooks block written during the session (by the model, for
example) has no effect on it.

## What a hook never does

- **It never approves anything.** `pre_tool` runs only for a call that is already allowed or approved; it can block,
  never allow, and it cannot change the arguments. A call that is refused or waiting for a card does not run hooks.
- **No hooks in `plan` mode** (the session's mode, including a stricter mode set by the repository).
- **No hooks in sub-agents**, unless the block says `"subagents": true`; they then run in the parent session's
  sandbox.
- Hardline commands (`rm -rf /` and the like) are never run, as a hook or otherwise.

Known limit: a hook runs your command without a card. If it runs a script from the repository (`./scripts/lint.sh`,
`npm test`), it runs whatever that script contains now, including what the model may have written there. Prefer
hooks that call tools you installed, and keep in mind that this code runs in the sandbox, not on your host.
