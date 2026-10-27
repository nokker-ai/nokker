# What is ours, what comes from upstream

Nokker runs on top of an agent runtime that we do not write from scratch. `hermes-nokker` is a **patched fork** of the
open-source agent from Nous Research (MIT); the Nokker harness uses it as its runtime. This page says where the line is.

## Ours (this repository, `nokker/`)

- The core: ports (the interfaces the rest depends on), the turn loop, routing and tiers.
- Approvals and signed approval cards, and the gates that decide what may run.
- The Nokker tools (memory, mail, code sessions, web search, team notes…).
- The HTTP API under `/v1`, its tenancy and quotas.
- Lokkal (the local machine bridge and its relay) and the gateway that serves the web app.

## From upstream (the pinned fork)

- The agent runtime library and its tool-calling plumbing.
- The `nokker` binary (a brand alias of the upstream gateway) and the channel gateway adapters (`messaging` extra).
- Local speech-to-text (`stt-whisper` extra, kept behind our own interface) and the MCP client.

Upstream pieces sit behind our interfaces: replacing one does not change the core. Nothing upstream is vendored in
this repository.

## Where the pin lives

`HERMES.lock` at the repository root is the single source: `repo`, `tag`, `commit` (the commit of that tag in the
public repository) and `extras`. The GitHub CI, the image workflow, the Dockerfile and the `pyproject.toml` comment
repeat the tag; `tests/unit/test_hermes_lock.py` fails when any of them drifts from the lock, and the GitHub CI also
checks that the cloned tag resolves to `commit`. Mirrors may carry a different sha for the same tag, so the tag is the
pin and only the public commit is recorded.

## Changing the pin

1. Tag the fork (a patch, or a rebase on a new upstream release). The fork's own `UPSTREAM.md` has the procedure.
2. Edit `HERMES.lock` (`tag`, `commit`), then make the same tag change in `.github/workflows/ci.yml`
   (`HERMES_TAG`, `HERMES_COMMIT`), `.github/workflows/lokkal-images.yml` (`ref`), the `Dockerfile` comment and the
   `pyproject.toml` comment.
3. Run `pytest tests/unit/test_hermes_lock.py`: it names the file that still carries the old tag.
4. Run the unit tests and the smoke test of the image before promoting. Never rebase this repository onto the fork:
   the fork is a dependency, not a branch.

The runtime cannot be installed with a plain `pip install git+…`: upstream blocks wheel and sdist builds on purpose.
Clone the tag, then `pip install -e "./hermes-src[messaging,stt-whisper]"`.
