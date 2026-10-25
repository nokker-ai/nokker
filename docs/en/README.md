# Nokker Lokkal documentation

Nokker Lokkal is the local, open-source edition of Nokker: an agent for code, ops and scheduled work that runs on your
machine, keeps its memory, and asks you before anything that has an effect.

Status: private beta / early access. What a page describes is in the Lokkal install unless it is marked:

- **rolling out**: built and tested, switched on with the nokker.ai side of the release (account link, gateway, phone
  approvals) or not published yet;
- **beta**: built, off by default; you turn it on with a flag;
- **planned**: not built yet.

## Get started

| Page | What it covers |
|---|---|
| [install.md](install.md) | Install on Linux, macOS and Windows (WSL 2), verify signatures, update, uninstall |
| [cli.md](cli.md) | The `nokker` command: sessions, unattended mode, slash commands, approval card, cost per turn, `lokkal.json` |
| [models.md](models.md) | Which model answers: Nokker Router (rolling out), your own OpenAI-compatible endpoint, a local model; tiers and reviewer; images; relayed connectors |
| [editors.md](editors.md) | `nokker acp` (Agent Client Protocol, Zed) and the VS Code extension (Marketplace: rolling out) |

## Working on code

| Page | What it covers |
|---|---|
| [permissions.md](permissions.md) | Effects, modes, `tool(pattern)` rules, settings files, hard limits, the sandbox |
| [hooks.md](hooks.md) | Commands run at session start, before and after a tool call, at the end of a turn; trusting repository hooks |
| [checkpoints.md](checkpoints.md) | Restore points and rollback |
| [subagents.md](subagents.md) | Delegating to sub-agents in git worktrees |
| [background.md](background.md) | Background commands (dev servers, watch) and private port previews |
| [memory.md](memory.md) | Long-term, project and session memory; `AGENTS.md` / `NOKKER.md` / `CLAUDE.md`; what the agent learns; health check |
| [skills.md](skills.md) | Skills: catalogue, guard, install and remove |
| [review.md](review.md) | `nokker review`: line-by-line pull request review, GitHub Action and Forgejo Actions (packaging: rolling out) |

## Ops and agents

| Page | What it covers |
|---|---|
| [ops.md](ops.md) | ssh hosts by name, approvals by role (`prod` = strong card), host keys, the chained audit journal |
| [secrets.md](secrets.md) | The local vault: `secret:NAME` references, redaction, `file` and OpenBao providers |
| [agents.md](agents.md) | Scheduled agents: triggers, arming, runs, budget; placement on several machines and the deck (rolling out) |
| [packs.md](packs.md) | Agent packs (mail, social, jobs, brand, video, MCP builder, ops): skills plus agent templates proposed as cards; adding MCP servers |
| [incidents.md](incidents.md) | Alerts, incidents, the plan and the morning brief (beta) |
| [teams.md](teams.md) | Shared projects: roles, what is shared and what is not (beta) |

## Trust

| Page | What it covers |
|---|---|
| [security.md](security.md) | The security model on one page: cards and strong cards, phone approval, what you see when you sign, redaction, sandbox, what never leaves your machine, audit |
| [gateway.md](gateway.md) | `<name>.nokker.app`, `/chat`, pages, phone approvals, several machines (rolling out) |
| [benchmarks.md](benchmarks.md) | SWE-bench Verified (50-task sample): results, method, reproduction |

Conventions: commands are shown as typed; paths starting with `~/.nokker` are on your machine (inside WSL on Windows).
