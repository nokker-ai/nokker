# Checkpoints

Before the agent changes anything, Nokker takes a snapshot of your folder, so you can see what it changed and go back.

## How it works

- The snapshots live in a **shadow git repository** under the state folder (`~/.nokker/state/checkpoints`). Nothing is
  written in your own repository, and your repository's hooks are never read or run (`core.hooksPath=/dev/null`).
- A snapshot is taken before any tool call whose effect is not `READ`, at most once per folder and per turn. A
  read-only shell command does not take one. If a snapshot fails, the failure is logged; it never blocks your work.
- Nokker keeps a ledger of the files the agent wrote.
- Old snapshots are pruned at startup: older than 7 days, folders that no longer exist, and a cap of 500 MB in total
  (at most once a day).

## Tools the agent has

| Tool | Effect | What it does |
|---|---|---|
| `checkpoint_create(reason?)` | WRITE | Take a snapshot now. Replies `created: false` if nothing is new since the last one. |
| `checkpoint_list` | READ | `hash`, `short_hash`, `timestamp`, `reason`, `files_changed`, `insertions`, `deletions`, newest first |
| `checkpoint_diff(hash?)` | READ | What changed since a point; without a hash, since the first point of the session (everything the agent changed). Truncated at 200 KiB with a note. |
| `checkpoint_restore(hash, file?, safe = true)` | DESTRUCTIVE | Go back to a point, for the whole folder or one file. Asks for a card (once only). |

## Safe restore

With `safe` (the default), a restore only reverts files **the agent wrote last**. It removes files the agent created,
and leaves alone what you edited yourself afterwards, reporting them as `skipped_user_edits`. With `safe=false`, files
created since are not deleted, and your edits are overwritten for the files covered.

## In the terminal

```
/checkpoints          list points, newest first (number 1 is the latest)
/diff [hash|n]        coloured diff since a point, cut at 300 lines (the rest is counted)
/rollback [hash|n]    go back to a point; shows an approval card, then lists restored and kept files
```

These slash commands call the tools above directly, without a model turn (see [cli.md](cli.md#slash-commands)). You
can also ask in plain words: "roll back to the previous snapshot".

`git` must be installed on the host for diffs and restore points; `nokker doctor` checks it.

Sub-agents cannot call `checkpoint_*`.
