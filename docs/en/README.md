# Nokker Lokkal documentation

Nokker Lokkal is the local, open-source edition of Nokker: an agent that works in a folder of yours, from a terminal,
with your own approvals in front of anything that has an effect.

Status: private beta / early access. Items marked **planned** or **rolling out** are described so you know where the
product is going; they are not available in every install yet.

| Page | What it covers |
|---|---|
| [install.md](install.md) | Install on Linux, macOS and Windows (WSL 2), verify signatures, update, uninstall |
| [cli.md](cli.md) | The `nokker` command: sessions, unattended mode, slash commands, approval card, `lokkal.json` |
| [permissions.md](permissions.md) | Effects, modes, `tool(pattern)` rules, settings files, hard limits, sandbox |
| [hooks.md](hooks.md) | Commands run at session start, before and after a tool call, at the end of a turn; trusting repository hooks |
| [skills.md](skills.md) | Skills: catalogue, guard, install and remove |
| [subagents.md](subagents.md) | Delegating to sub-agents in git worktrees |
| [checkpoints.md](checkpoints.md) | Restore points and rollback |
| [background.md](background.md) | Background commands (dev servers, watch) and private port previews (rolling out) |
| [memory.md](memory.md) | Project memory: `AGENTS.md` / `NOKKER.md` / `CLAUDE.md`, what the agent learns, `/memory` (rolling out) |
| [models.md](models.md) | Which model answers: Nokker Router, your own endpoint, a local model |
| [secrets.md](secrets.md) | The local vault: `secret:NAME` references, redaction, `file` and OpenBao providers |
| [gateway.md](gateway.md) | `<name>.nokker.app`, `/chat` and pages (rolling out) |
| [benchmarks.md](benchmarks.md) | SWE-bench Verified (50-task sample): results, method, reproduction |

Conventions: commands are shown as typed; paths starting with `~/.nokker` are on your machine (inside WSL on Windows).
