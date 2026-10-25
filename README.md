# Nokker

A multi-model coding agent that works in your folder, never clears its session, and asks before it does anything
that matters.

Nokker Lokkal is the local, open-source edition: one command installs it on your machine, your code stays there, and
you choose the model.

> Status: private beta / early access. Some features below are marked **planned** or **rolling out**.

## What it is

- **An agent harness for code.** It reads, searches, edits and patches files, runs your tests and commands, keeps a
  work plan, delegates to sub-agents, and talks to you from a terminal (`nokker`) or the local web page.
- **Multi-model.** Works with the Nokker Router, with any OpenAI-compatible endpoint (your own key), or with a model
  running on your machine. Routing across several models within one turn is **planned**; today one model serves the
  install.
- **It never clears.** A session is an endless journal. What the model sees is a window computed from it: heavy tool
  results are moved aside and can be recalled, finished episodes become signed summaries, and a new topic opens a new
  episode instead of erasing anything. Memory runs on your machine.
- **Approvals you control.** Every tool call has a declared effect (read, write, destructive, send, spend). Rules,
  modes and approval cards decide what goes through; a few commands can never be approved. Commands run in a
  disposable sandbox container, with outbound traffic only.
- **Reversible.** Each write is preceded by a snapshot kept outside your repository, and you can go back.

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

> /diff
```

Terminal labels are currently in French; English labels are planned.

## Documentation

- [Install](docs/en/install.md)
- [The `nokker` command](docs/en/cli.md)
- [Permissions](docs/en/permissions.md), [hooks](docs/en/hooks.md)
- [Skills](docs/en/skills.md), [sub-agents](docs/en/subagents.md), [checkpoints](docs/en/checkpoints.md)
- [Models](docs/en/models.md)
- [Gateway](docs/en/gateway.md) (rolling out)

## How it compares

On a 50-task sample of SWE-bench Verified, judged by the official harness, Nokker Lokkal resolves 35 tasks, on par
with a reference coding agent driven by the same model (37), with 40 % fewer input tokens. Method, every run and how
to reproduce them: [docs/en/benchmarks.md](docs/en/benchmarks.md).

## Licence

Apache-2.0 (see `LICENSE`).

Nokker includes code derived from an MIT-licensed open-source agent project by Nous Research. Its licence text and
copyright notice are kept in `NOTICE` and `LICENSES/`. Third-party components keep their own licences.

## Status

Private beta / early access. Rolling out: account link and Nokker Router from the app, the gateway
(`<name>.nokker.app`). Planned: reading `.nokker/settings.json` in installed sessions, project skills, routing across
models, the subscriber memory mirror, background sub-agents.
