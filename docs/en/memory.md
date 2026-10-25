# Project memory

An instruction file cannot hold everything a project needs. In Nokker Lokkal, `AGENTS.md`, `NOKKER.md` and
`CLAUDE.md` are only the entry point: the project's memory lives in CortHeXis, the memory engine that runs inside your
install, and it grows as you work.

Status: **rolling out**. It is on in the Lokkal compose (`local.project_memory: true`) and off by default in the core
itself. The `/memory` slash command is **planned** in the terminal client; the core route it uses is ready.

## Instruction files

When a code session opens, the core looks for `AGENTS.md`, `NOKKER.md` and `CLAUDE.md` in the session folder and in
each parent folder up to the root of the git repository. It never looks above the folder you mounted into Lokkal, and
it ignores a symbolic link that points outside of it. Files are read from the outermost (repository root) to the
innermost (session folder).

Each file is split by its headings (`#`, `##`, `###`, outside code blocks) and stored as notes of the **project** in
CortHeXis. A file is imported again as soon as its content changes (checked at the start of every turn).

What goes into the model's context:

- a **short core**, always present: the rules and feedback learned in this project, then the first sections of the
  instruction files, within a token budget (`local.project_memory_core_tokens`, default 800);
- **everything else is recalled by relevance**: at each turn, the sections and notes closest to your message are added
  to the turn's memory block, without repeating what is already in the core.

### A repository cannot grant permissions

A repository may come from someone else. Its instruction files guide method and style (how to run the tests, naming,
commit messages). They never grant a permission and never lift an approval: what an action needs is decided by your
[permissions](permissions.md) and the core's approval cards only. The memory block says so to the model in its first
lines.

## What the agent learns

The agent writes down what it learns with a `memory_note` tool:

| Kind | Meaning |
|---|---|
| `rule` | a lasting rule ("always run `make check` before concluding") |
| `feedback` | a correction or preference you gave |
| `fact` | a fact about the project the agent checked |

Each note has a scope: `project` (default) or `user` (valid in all your projects). Each note records where it comes
from (thread and turn) and can link to other notes. Secrets are redacted before anything is written.

Two rules hold whatever the model does:

- a note derived from the repository, a tool output or a sub-agent stays in the **project**;
- a `user` note must quote your own words from the current turn.

When you correct the agent or set a lasting rule, it is asked to note it right away. Every write is visible: the turn
emits a `memory.noted` event (and `memory.forgotten` when a note is forgotten), and the tool call shows in the session
like any other.

In the `default` permission mode, writing a note asks for approval like any other write; in `accept_edits` and `auto`
it does not (it only writes to Nokker's own state folder); in `plan` mode it is refused.

## Seeing and forgetting

`memory` lists, shows and forgets notes:

```
memory {"action": "list"}                    # learned notes and instruction sections, and which are in the core
memory {"action": "view", "name": "<note>"}
memory {"action": "forget", "name": "<note>"}
```

The terminal client calls it without a model turn through `POST /v1/local/tools/call` (planned slash command:
`/memory`, `/memory <note>`, `/memory forget <note>`). Forgetting moves the note out of the index into a `.forgotten`
folder: nothing is destroyed. A section of an instruction file cannot be forgotten: edit the file, the memory follows.

## Where it is stored

Notes are Markdown files under `~/.nokker/state/project-memory/` (one folder per project; one more, `u-local`, for
your `user` notes), indexed in the background in the Postgres database of the compose. A project is identified by its
git remote URL (without credentials), so two clones of the same repository share their memory; without a remote, by
the path of the repository root.

## Settings

In the core configuration, under `local`:

| Key | Default | Meaning |
|---|---|---|
| `project_memory` | `false` (`true` in the Lokkal compose) | turn project memory on |
| `project_memory_core_tokens` | `800` | token budget of the always-present core |
| `project_memory_dir` | `<state_dir>/project-memory` | where notes are stored |
| `project_memory_min_score` | `0.15` | relevance floor of the recall |

With `project_memory: false` nothing changes: no tool, no memory block, the same system prompt.
