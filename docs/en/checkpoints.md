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
| `checkpoint_list` | READ | `hash`, `short_hash`, `timestamp`, `reason`, `files_changed`, `insertions`, `deletions`, newest first; plus `thread_id`, `turn_index` and `tool` for points the kernel took itself (see below) |
| `checkpoint_diff(hash?)` | READ | What changed since a point; without a hash, since the first point of the session (everything the agent changed). Truncated at 200 KiB with a note. |
| `checkpoint_restore(hash, file?, safe = true)` | DESTRUCTIVE | Go back to a point, for the whole folder or one file. Asks for a card (once only). |

## Points and turns

Each point the kernel takes (before a tool call, by `checkpoint_create`, or the "before restore" point of a restore)
remembers the thread, the turn and the tool it was taken for. `checkpoint_list` shows them as `thread_id`, `turn_index`
and `tool`; older points, and points taken by another program, have none. The links are kept in the state folder
(`checkpoints/turn-links.json`, the last 2000 points), so they survive a restart. For a call made outside a turn
(`/rollback`, `/checkpoints` in the terminal), `turn_index` is the index of the thread's next turn.

When a point is taken, clients receive the event `checkpoint.created {thread_id, turn_index, hash, tool}`: an editor
can show "what this turn changed" from the point of that turn, without comparing lists.

## Restoring one file

`checkpoint_restore(hash, file)` puts that file back as it was at the point. If the file **did not exist** at the point
(the turn created it), restoring it means deleting it. Nokker first takes a "before restore" point and checks that this
point holds the file's current content, then deletes the file; you can bring it back from that point. If the points
cannot hold the file (excluded from snapshots, such as `build/` or `node_modules/`, ignored by your `.gitignore`, or
larger than 10 MB), or if it is a folder or a symbolic link, nothing is deleted and the call says why. The approval
card is the same as for any restore.

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
