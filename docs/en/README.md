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
| [skills.md](skills.md) | Skills: catalogue, guard, install and remove |
| [subagents.md](subagents.md) | Delegating to sub-agents in git worktrees |
| [checkpoints.md](checkpoints.md) | Restore points and rollback |
| [models.md](models.md) | Which model answers: Nokker Router, your own endpoint, a local model |
| [gateway.md](gateway.md) | `<name>.nokker.app`, `/chat` and pages (rolling out) |
| [README-public.md](README-public.md) | Draft of the public repository README |

Conventions: commands are shown as typed; paths starting with `~/.nokker` are on your machine (inside WSL on Windows).
