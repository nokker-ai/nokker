# Permissions

Nokker can do a lot in your folder; you stay in charge of what has an effect. Everything below is enforced by the
core, not by the model.

## Effects

Every tool call carries an effect, and the effect decides what happens.

| Effect | Examples |
|---|---|
| `READ` | read a file, search, read the plan, a read-only command (`ls`, `cat`, `grep`, `git status`, `git diff`, `git log`...) |
| `WRITE` | write or patch a file, a command that is not read-only and not dangerous |
| `DESTRUCTIVE` | a command classified dangerous (deletes, overwrites, pushes), restoring a restore point, installing or removing a skill |
| `SEND` | sends something outside |
| `SPEND` | spends money |

A tool with no declared effect is treated as `WRITE`. A shell command is classified per call; a compound command is
`READ` only if every segment is, with no redirection or substitution. Bulk sending is always refused.

## Modes

| Mode | Behaviour |
|---|---|
| `default` | Reads are free. Every write and every command asks. |
| `accept_edits` | Also edits files in the folder (`write_file`, `patch`) without asking. Commands that write still ask. |
| `auto` | Reads, writes and commands without asking. Only `DESTRUCTIVE` asks. |
| `plan` | Read-only. Any call with an effect is refused. |

Local sessions installed by Lokkal start in `accept_edits`: every write to the folder is snapshotted first (see
[checkpoints.md](checkpoints.md)), so it is reversible. Commands and destructive actions still ask. In unattended mode
(`nokker -p`), what would ask is refused.

## Rules

A rule is `tool` or `tool(pattern)`. The pattern is a case-sensitive glob (`fnmatch`).

- `terminal(pattern)` is matched against the whole command, trimmed.
- `terminal(pattern)` also covers the same command started in the background (`process_start`, see
  [background.md](background.md)); `process_start(pattern)` covers background starts only.
- `write_file(pattern)`, `read_file(pattern)`, `patch(pattern)` are matched against the normalised path
  (`./src/a.py` becomes `src/a.py`; `../x` stays `../x`, so `src/**` never covers it). For a multi-file patch, every
  path in the patch must match.

Examples:

```
terminal(npm test*)
terminal(pytest -q)
terminal(git status)
write_file(src/**)
patch(docs/*.md)
read_file
```

Order of evaluation: `deny`, then `plan` mode, then `ask`, then `allow` (including "for this session" allows), then
the mode. A `deny` always wins. An `allow` can cover a command you name yourself even if it would otherwise ask
(that is what "Always" does).

## Settings files

```json
{"permissions": {"mode": "accept_edits",
                 "allow": ["terminal(npm test*)", "write_file(src/**)"],
                 "ask":   ["terminal(git push*)"],
                 "deny":  ["terminal(curl*)", "write_file(.env)"]}}
```

| File | Who writes it | What it may say |
|---|---|---|
| User settings | you ("Always" on a card writes here) | anything |
| `.nokker/settings.json` in a repository | whoever wrote the repository | `deny`, `ask` and a stricter mode only |

A repository comes with its code, so it may come from someone else: its `allow` rules and any wider mode are ignored.

Where the user file is:

- Lokkal installed by `install.sh`: `~/.nokker/state/settings.json` (the state folder is mounted into the core).
- `nokker code` run directly (headless, from a checkout): `~/.config/nokker/settings.json`.

The repository file is read when the session opens, by `nokker code` and by sessions served by the installed core.

The same files can also declare **hooks** (commands run before or after a tool call, at the start of a session, at
the end of a turn): see [hooks.md](hooks.md). A hook never approves a call; repository hooks run only once you trust
them.

## Hard limits

- **Hardline commands** (`rm -rf /`, `mkfs`, fork bombs and the like) are refused with no card and cannot be approved,
  even by a rule.
- A destructive card can only be decided **once**. No session or always scope.
- Some strong cards are never decided with the local token: a change on a `prod` host ([ops.md](ops.md)), a send or
  delete through a connector of your nokker.ai account ([models.md](models.md)), and a strong card coming from the web
  (gateway). They are signed on an enrolled device (a passkey). See [security.md](security.md).
- Approvals are bound to the exact arguments: if they change after the card is shown, the decision is refused.
- Secrets handled by tools are replaced by handles before they reach the model's context.

## The sandbox

Commands do not run on your host. Each session gets a disposable container:

- your work folder is mounted at `/workspace`, and nothing else from the host;
- outbound traffic goes through an egress proxy on an internal network: package managers, `git` and `curl` work,
  but private ranges (your LAN, the database, the core, the Docker proxy, cloud metadata endpoints, `127.0.0.0/8`)
  are refused;
- the core talks to Docker only through a filtering proxy that accepts labelled sandboxes only and refuses
  privileged containers, extra capabilities, other mounts and other networks;
- reads and searches are served straight from the mounted folder (they execute no repository code); writes, patches
  (and their lint) and commands stay in the sandbox.

Running commands on the host (`--backend local` of `nokker code`) exists for tests and for people who choose it
explicitly; it is not what the installer sets up.

## The local API

The core's API is published on `127.0.0.1` only. Every `/v1/local/*` request needs the **local token** (mode 0600, readable
by your user), and its `Host` header must be `127.0.0.1` or `localhost` on the served port. This is what stops a web
page you visit from driving your machine (CSRF, DNS rebinding).

Local approvals (`GET /v1/local/approvals`, `POST /v1/local/approvals/<id>/decide`) take scope `once`, `session` or
`always`, and for the last two a rule chosen among the `suggested_rules` the core returned with the card.
