# Sub-agents

For work that splits into independent pieces, the agent can delegate to sub-agents that run in parallel, each in its
own git worktree, so they do not step on each other or on your working tree.

## The `delegate` tool

```
delegate(tasks: [{goal, context?, role?: "worker" | "reviewer", output_schema?}], isolate = true, background = false)
```

- `worker` can edit files in its worktree; `reviewer` reads and runs tests and cannot write or patch.
- `output_schema`, if given, is validated. The sub-agent gets one retry; after that the task ends as
  `failed / output_schema_invalid`.
- The call blocks the parent's turn until the children return, and gives back one summary per child:
  `status`, `exit_reason`, `summary`, and the worktree `{path, branch, commits, dirty, kept}`. With
  `background: true` it returns at once instead (see below).

## Background sub-agents

With `background: true`, `delegate` returns right away with one entry per child (`child_id`, `thread_id`, `goal`,
`status: queued | running`, and the worktree `{path, branch}` when isolated). The children keep working after the
parent's turn ends; you can go on talking to the parent meanwhile, since each child runs on its own thread.

| Tool | Effect | What it does |
|---|---|---|
| `delegate_status(ids?, wait_s?)` | READ | State of the background children of this thread (`queued`, `running`, `stopping`, with the last tool and the elapsed time), or the full result once a child is done (same fields as a blocking call). `wait_s` waits up to that many seconds (at most 120) for them to finish. Results stay readable. |
| `delegate_cancel(ids?)` | READ | Stops background children of this thread (all of them without `ids`). Like `process_stop`, it only stops what the thread started, so it asks no card. |

- When a batch finishes, the parent's **next** turn gets a line saying so, with the child ids.
- Cancelling the parent thread (`POST /v1/threads/{id}/cancel`, `Esc` in the terminal) also stops its sub-agents,
  background and blocking ones; the response carries `subagents_cancelled`. A stopped child ends as
  `interrupted / cancelled`; its worktree is inspected and kept, never deleted.
- `delegate_status` can be called outside a turn through `POST /v1/local/tools/call` (without `wait_s`).
- Background children count against the same concurrency limit as blocking ones. At most 10 background children
  can be unfinished at once; a `delegate` beyond that is refused.
- Same gates, rules, approvals, tool filters and inactivity limit as a blocking child. A child that needs a card
  stops as `interrupted / approval_pending`, exactly as in the foreground.
- Background sub-agents run in the core's process: a restart of the core stops them. Their state is kept in the
  state folder (`subagents.json`: task, status, result, worktree; no secrets), so after a restart `delegate_status`
  still lists them. A child that had not finished (running, queued, or waiting for a card) shows as
  `interrupted / restart`, with its worktree **kept** and not inspected: read its branch before you merge or delete
  it. Finished results stay readable. A child is not resumed after a restart. Background sub-agents are not offered by
  `nokker code` (the one-shot command), whose process ends with the turn.

## A child's card decided later

A child that stops on a card (`interrupted / approval_pending`) waits for it, in the foreground as in the
background: its worktree is kept and its tools stay open. When you approve the card, the tool and the follow-up turn
run **as that child**, with its own restricted tools and rules, never with the parent's. When that turn ends, the
child is finalized as usual, `subagent.finished` carries `resumed: true`, and the parent's next turn gets a line with
the outcome.

If the child no longer exists (its thread was cancelled, `delegate_cancel`, the core restarted), an approved card is
**not run**: it ends as `failed` and its `tool.result` is an error. A rejected card has no follow-up turn. After a
restart this holds too: the child is listed as `interrupted / restart`, and its card is never run with the parent's
tools.

A child also inherits the restrictions of its parent's thread (the repository's and the team's stricter mode, `ask`
and `deny` rules). "For this session" approvals given in the parent do not extend to children.

## Worktrees

With a git repository and `isolate` true, each child works in `<folder>/.nokker/worktrees/<id>` on the branch
`nokker/sa-<id>`. All git commands run inside the sandbox, with the repository's hooks disabled
(`core.hooksPath=/dev/null`). `.nokker/` is added to `.git/info/exclude`, not to your `.gitignore`.

Worktrees use relative paths so the repository is reachable from inside the sandbox. With git 2.48 or later, Nokker uses
`git worktree add --relative-paths`; with an older git it creates the worktree and rewrites the links itself, and if
that fails the worktree is removed and the child does not run isolated.

Without a repository (or without a first commit) there is no isolation: the child works in the real folder and the
result says `isolated: false`.

A child's work is merged by the parent with a normal command, which goes through the usual approvals:

```sh
git merge --no-ff nokker/sa-<id>
```

A worktree is removed only when it has no commit and no change. An interrupted child is inspected, never deleted.

## Limits and safety

| Limit | Default |
|---|---|
| Children at the same time | 3 (the rest wait) |
| Depth | 1: a sub-agent cannot delegate |
| Inactivity | 600 s with no tool call or result, then the child is interrupted (`idle_timeout`) |

The same gates apply to children as to the parent: effects, rules and approvals. The effect of `delegate` is
decided per call: `WRITE` when children are confined to disposable worktrees, **`DESTRUCTIVE`** otherwise (`isolate`
false, or no repository), because the children then work in your real folder.

A child cannot call: `clarify`, `escalate_to_human`, `send_*`, `*_send`, `session_send`, `agent_*`, memory writes,
`skill_install`, `skill_remove`, `checkpoint_*`. If it needs a decision that has a card, the card is attached to the
child's thread (`<thread>/sa-<id>`) and the child stops (`interrupted / approval_pending`).

## In the terminal

`/agents` lists the sub-agents of the thread (goal, state, last tool, duration); children also appear as indented lines
under their `delegate` call. Events are `subagent.started`, `subagent.progress` and `subagent.finished`; for a
background child, `started` and `finished` carry `background: true`, and `finished` carries `exit_reason`.
