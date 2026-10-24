# Sub-agents

For work that splits into independent pieces, the agent can delegate to sub-agents that run in parallel, each in its
own git worktree, so they do not step on each other or on your working tree.

## The `delegate` tool

```
delegate(tasks: [{goal, context?, role?: "worker" | "reviewer", output_schema?}], isolate = true)
```

- `worker` can edit files in its worktree; `reviewer` reads and runs tests and cannot write or patch.
- `output_schema`, if given, is validated. The sub-agent gets one retry; after that the task ends as
  `failed / output_schema_invalid`.
- The call blocks the parent's turn until the children return, and gives back one summary per child:
  `status`, `exit_reason`, `summary`, and the worktree `{path, branch, commits, dirty, kept}`.

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
under their `delegate` call. Events are `subagent.started`, `subagent.progress` and `subagent.finished`. Background
sub-agents that outlive a turn are **planned**.
