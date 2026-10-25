# Nokker

[![ci](https://github.com/nokker-ai/nokker/actions/workflows/ci.yml/badge.svg)](https://github.com/nokker-ai/nokker/actions/workflows/ci.yml)

An agent for code, ops and the work around them. It runs on your machine, remembers what it learned, and asks you
before it does anything that matters.

Nokker Lokkal is the local, open-source edition: one command installs it, your code stays on your machine, and you
choose the model.

> Status: private beta / early access. Features marked **rolling out** arrive with the nokker.ai side of the release;
> **beta** means built but off by default; **planned** means not built yet.

## What it does

- **Code.** It reads, searches, edits and patches files, runs your tests and commands in a disposable sandbox, keeps a
  work plan, delegates to sub-agents in separate git worktrees, runs dev servers in the background, and takes a
  restore point before every write so you can roll back. Use it from a terminal (`nokker`), from Zed (Agent Client
  Protocol) or VS Code, or in CI with `nokker review`.
- **Ops.** It runs commands on your servers by name, with approvals that follow the role of the host: any change on
  `prod` needs a strong card. ssh keys come from a local vault and never reach the model. Every action with an effect
  goes into a chained audit journal you can verify.
- **Agents.** Scheduled or event-driven jobs (every morning, on a webhook, on an alert in beta) with their own tools,
  budget and secrets by name. A session proposes an agent as a card; you create it and you arm it. Ready-made packs:
  mail, social, jobs, brand, video, MCP builder, ops.
- **Memory that never starts from zero.** A session is an endless journal: heavy results are moved aside and recalled
  when needed, finished episodes become summaries, and nothing is erased. `AGENTS.md`, `NOKKER.md` and `CLAUDE.md`
  are only the entry point: the agent writes down the rules and corrections you give it, per project, and recalls them
  by relevance. Memory runs on your machine.
- **Approval from your phone** (rolling out). Strong cards (a `prod` change, an e-mail sent in your name, a deletion)
  are signed with a passkey on your phone. Your machine checks the signature itself, and the page shows what you are
  signing, and who described it.
- **Several machines** (rolling out). Link several machines to one account: agent runs go to the least loaded
  machine that has what they need (RAM first, GPU when asked), and one deck shows all your sessions and agents.
- **Any model.** The Nokker Router (rolling out), any OpenAI-compatible endpoint with your own key, or a model running
  on your machine (Ollama is detected). Each turn goes to a tier by kind (small talk, conversation, code), a reviewer
  reads the diff before a coding turn says it is done, and every turn shows its tokens and cost.
- **Open.** Apache-2.0: the agent, the sandbox and the approvals are in this repository.

## Install

```sh
curl -fsSL https://nokker.ai/install.sh | sh
```

Windows (PowerShell, Lokkal runs in WSL 2):

```powershell
irm https://nokker.ai/install.ps1 | iex
```

You need Docker. Downloads are checked with sha256, and with cosign signatures when `cosign` is installed. Details,
options and uninstall: [docs/en/install.md](docs/en/install.md).

## A session

```
$ cd ~/nokker/shop-api
$ nokker
  Nokker Lokkal            shop-api            mode: accept_edits

> make test_add pass, don't touch the public API

  v read_file   src/calc.py                                   (read)         14 ms
  v search_files  "def add"                                   (read)         31 ms
  v patch       src/calc.py                                   (write)       120 ms
  ~ terminal    pytest -q tests/test_calc.py                  (write)

  +- Action to approve ------------------------------------------------+
  |  terminal   pytest -q tests/test_calc.py                  write     |
  |                                                                    |
  |  1. Once         2. For this session                               |
  |  3. Always this command      4. Refuse                             |
  +--------------------------------------------------------------------+
> 2
  v terminal    pytest -q tests/test_calc.py                  (write)      2.4 s

  Fixed: add() now returns int for int inputs. 1 file changed, tests pass.
  12.4 k tokens · €0.03

> /diff
```

Terminal labels are currently in French; English labels are planned.

## Documentation

- Start: [install](docs/en/install.md), [the `nokker` command](docs/en/cli.md), [models](docs/en/models.md),
  [editors](docs/en/editors.md)
- Code: [permissions](docs/en/permissions.md), [hooks](docs/en/hooks.md), [checkpoints](docs/en/checkpoints.md),
  [sub-agents](docs/en/subagents.md), [background commands](docs/en/background.md), [memory](docs/en/memory.md),
  [skills](docs/en/skills.md), [pull request review](docs/en/review.md)
- Ops and agents: [ops](docs/en/ops.md), [secrets](docs/en/secrets.md), [agents](docs/en/agents.md),
  [packs](docs/en/packs.md), [incidents and plan](docs/en/incidents.md) (beta), [teams](docs/en/teams.md) (beta)
- Trust: [security model](docs/en/security.md), [gateway](docs/en/gateway.md) (rolling out),
  [benchmarks](docs/en/benchmarks.md)

All pages: [docs/en/](docs/en/README.md).

## How it compares

On a 50-task sample of SWE-bench Verified, judged by the official harness, Nokker Lokkal resolves 35 tasks, on par
with a reference coding agent driven by the same model (37), with 40 % fewer input tokens. Method, every run and how
to reproduce them: [docs/en/benchmarks.md](docs/en/benchmarks.md).

## Tests

Tests run on every push to `main` and on every pull request: 2,354 tests (2,208 unit tests, and 146 integration tests
against a real Postgres with pgvector), plus `ruff`. Tests that need a hosted model or a local llama.cpp server skip
themselves there. Workflow: [.github/workflows/ci.yml](.github/workflows/ci.yml).

## Licence

Apache-2.0 (see `LICENSE`).

Nokker includes code derived from an MIT-licensed open-source agent project by Nous Research. Its licence text and
copyright notice are kept in `NOTICE` and `LICENSES/`. Third-party components keep their own licences.

## Status

Private beta / early access.

- Rolling out: account link and Nokker Router, the gateway (`<name>.nokker.app`), approval from your phone, relayed
  mail and calendar connectors, placement on several machines and the shared deck, the subscriber mirror, the VS Code
  extension on the Marketplace, the `nokker review` GitHub Action.
- Beta (off by default): incidents and the plan, team projects.
- Planned: project skills, `/memory` in the terminal, background sub-agents, `mail:` agent triggers, English
  terminal labels.
