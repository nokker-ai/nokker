# Memory

An instruction file cannot hold everything a project needs. In Nokker Lokkal, `AGENTS.md`, `NOKKER.md` and
`CLAUDE.md` are only the entry point: the memory lives in CortHeXis, the memory engine that runs inside your install
(in the core process, on the Postgres database and the embedding server of the compose), and it grows as you work.

CortHeXis holds three memories, kept apart from each other:

| Memory | What it holds | Where it is recalled |
|---|---|---|
| **long-term** | your `user` notes: what you said holds everywhere | every thread, with or without a folder |
| **project** | the instruction files of a repository and what was learned in it | the code sessions of that repository |
| **session** | the closed episodes of a thread (summary and your messages) | that thread only |

Status: all three are on in the Lokkal install (`memory_mode: direct`, `local.project_memory: true`,
`context_corthexis.enabled: true`) and off by default in the core itself. In the terminal client, `/memory` shows
what it knows. Sharing project memory with a team is **beta**
([teams.md](teams.md)).

## Long-term memory

At every turn, the notes of your long-term memory closest to your message are added to the turn's memory block
(`[Rappel mémoire]`), in every thread: a code session, or a plain conversation in the local web app. Nothing is
recalled when nothing is close enough. The agent can also search it (`memory_search`, `memory_get`).

It is written with `memory_note` at the `user` scope (see [What the agent learns](#what-the-agent-learns)): the note
must quote your own words from the current turn. In a thread without a code session, the `user` scope is the only one
available. The agent does not yet write anything to it on its own from the conversation: no automatic extraction of
facts, no nightly consolidation. What it remembers is what was noted, and you can list or forget it with `memory`.

Only the owner of the install gets this memory: any other account the core serves recalls nothing from it and does
not see the `memory_*` tools.

### Is it working?

When the core starts, it checks the database and the embedding server in the background and logs either "ready" or
"UNAVAILABLE" with the reason (`nokker logs`). A memory that cannot answer never fails silently:

- `GET /healthz` carries `"memory": "ok" | "down" | "off"`; the core itself keeps answering;
- `GET /v1/local/memory` (local token) gives the detail: mode, projects read, number of notes, embedding model and
  dimension, the last error if any, the duration of the last recall.

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

A client can call it without a model turn through `POST /v1/local/tools/call` (the terminal slash commands
`/memory`, `/memory <note>`, `/memory forget <note>` use it). Forgetting moves the note out of the index into a `.forgotten`
folder: nothing is destroyed. A section of an instruction file cannot be forgotten: edit the file, the memory follows.

## Where it is stored

Notes are Markdown files under `~/.nokker/state/project-memory/` (one folder per project; one more, `u-local`, for
your `user` notes, which is your long-term memory), indexed in the background in the Postgres database of the
compose. The files are the source of truth: the index can be rebuilt from them. A project is identified by its
git remote URL (without credentials), so two clones of the same repository share their memory; without a remote, by
the path of the repository root.

## Settings

Long-term memory, at the top level of the core configuration:

| Key | Default | Meaning |
|---|---|---|
| `memory_mode` | `mcp` (`direct` in the Lokkal compose) | `direct`: CortHeXis inside the core; `off`: none. The `NOKKER_MEMORY_MODE` variable is read when the key is absent |
| `memory_direct.projects` | `[u-local]` | the CortHeXis projects recalled at every turn |
| `memory_direct.min_score` | `0.15` | relevance floor of the recall (with `relative_floor`, `0.6` of the best result) |

The database and the embedding server are the compose's (`CORTHEXIS_DATABASE_URL`, `CORTHEXIS_EMBED_URLS`).

Project memory, under `local`:

| Key | Default | Meaning |
|---|---|---|
| `project_memory` | `false` (`true` in the Lokkal compose) | turn project memory on |
| `project_memory_core_tokens` | `800` | token budget of the always-present core |
| `project_memory_dir` | `<state_dir>/project-memory` | where notes are stored |
| `project_memory_min_score` | `0.15` | relevance floor of the recall |

With `project_memory: false` nothing changes: no tool, no memory block, the same system prompt; `memory_note` is then
unavailable, so nothing new reaches the long-term memory either.
