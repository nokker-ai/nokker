# Install

One command, containers, nothing to configure by hand. The first launch opens a local web page where you choose your
model.

## Requirements

| System | You need |
|---|---|
| Linux (x86_64, arm64) | `curl`, and Docker Engine with the Compose plugin. If Docker is missing, the installer offers to install it with Docker's official script (it asks for `sudo`). |
| macOS (Intel, Apple silicon) | `curl`, and Docker Desktop or OrbStack, running. The installer does not install Docker on macOS. |
| Windows 10 (build 19041+) or 11, 64-bit | WSL 2 with a Linux distribution. Lokkal runs inside WSL 2. Docker Desktop with WSL integration, or Docker Engine inside the distribution. |

## Linux and macOS

```sh
curl -fsSL https://nokker.ai/install.sh | sh
```

Options go after `-s --`:

```sh
curl -fsSL https://nokker.ai/install.sh | sh -s -- --workspace ~/code --yes
```

| Option | Meaning |
|---|---|
| `--version X.Y.Z` | Version to install (default: latest release tagged `lokkal-v*`) |
| `--workspace DIR` | Work folder(s) the agent may open, separated by `:` (default `~/nokker`). `/` and your home folder itself are refused: pick a sub-folder. |
| `--dir DIR` | Install folder (default `~/.nokker`) |
| `--yes` | Ask nothing (this includes installing Docker on Linux) |
| `--update` | Keep `.env` and state, change version, restart |
| `--uninstall` | Stop and remove the containers (add `--purge` to erase data too) |
| `--no-open` | Do not open the browser |

`--workspace` is applied on first install only. To change it later, edit `NOKKER_HOST_WORKSPACE` in `~/.nokker/.env`
and run `docker compose -p nokker --env-file ~/.nokker/.env -f ~/.nokker/docker-compose.yml up -d`.

## Windows

In PowerShell:

```powershell
irm https://nokker.ai/install.ps1 | iex
```

With options:

```powershell
& ([scriptblock]::Create((irm https://nokker.ai/install.ps1))) -Workspace ~/code -Yes
```

Parameters: `-Version`, `-Workspace` (a path inside the distribution), `-Distro`, `-Yes`, `-Update`, `-Uninstall`,
`-Purge`, `-NoOpen`.

The script checks WSL 2 and a distribution (and offers `wsl --install`, which needs administrator rights and a reboot),
finds Docker in the distribution, runs `install.sh` there, then places `nokker.exe` in `%LOCALAPPDATA%\Nokker\bin`
(added to your user PATH). Your folders stay visible from Explorer under `\\wsl.localhost\<distribution>\home\<you>`.

The Windows installer is new: it is checked for syntax and for the WSL logic, and validation on real Windows machines
is in progress.

## What the installer does

1. Checks Docker (see above).
2. Downloads the release package for your version and verifies it (see [Verifying downloads](#verifying-downloads)).
3. Writes the files into `~/.nokker`:
   - `docker-compose.yml`, `config/`, `web/`, `sql/`, `initdb/`, `VERSION`, and a copy of `install.sh`;
   - `.env` (mode 0600) with the local secrets it generates: database passwords, the key that encrypts the local vault,
     the approvals key. Nothing is shared; they never leave the machine;
   - `state/` (mode 0700): the local token, `settings.json`, the encrypted vault, skills, restore points;
   - `bin/nokker`: the command-line client.
4. Writes `~/.config/nokker/lokkal.json`, which tells the client where the install lives (see [cli.md](cli.md)).
5. Pulls the signed images from `ghcr.io/nokker-ai` and starts the stack with `docker compose -p nokker`. Nothing is
   built on your machine.
6. Waits for the core to answer on `/healthz`, adds `~/.nokker/bin` to your `PATH` (in `~/.zshrc`, `~/.bashrc` or
   `~/.profile` if they exist), and opens the onboarding page.

The stack: the core (API and agent loop), the local web interface, a database, a small embedding service for memory, a
filtered Docker proxy, and an egress proxy. Ports are published on `127.0.0.1` only: the web interface on 7420 and
the API on 8742 (the next free port if those are taken; the real values are in `lokkal.json`).

Your work folder is mounted into a disposable sandbox container per session (see [permissions.md](permissions.md)). The
agent can only open folders under the `--workspace` you gave.

## Verifying downloads

Every release (tag `lokkal-vX.Y.Z` on `github.com/nokker-ai/nokker`) carries:

- `nokker-lokkal-<version>.tar.gz` and `nokker-<os>-<arch>` binaries (`linux`, `darwin`, `windows`; `x64`, `arm64`),
- a `.sha256` file for each,
- a `.sigstore.json` cosign bundle for each.

The installer always checks the sha256 and aborts on mismatch. If `cosign` is installed it also requires a valid
signature from this repository's release workflow and aborts if it is missing or wrong. Without `cosign`, only the
sha256 is checked, and it is served from the same place as the file: install `cosign` if you want authenticity and not
just integrity.

By hand:

```sh
sha256sum -c nokker-linux-x64.sha256

cosign verify-blob --bundle nokker-linux-x64.sigstore.json \
  --certificate-identity-regexp '^https://github\.com/nokker-ai/nokker/\.github/workflows/lokkal-images\.yml@refs/tags/lokkal-v' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  nokker-linux-x64

cosign verify ghcr.io/nokker-ai/nokker:<version>-lokkal \
  --certificate-identity-regexp '^https://github\.com/nokker-ai/nokker/\.github/workflows/lokkal-images\.yml@refs/tags/lokkal-v' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

## Check, update, logs

```sh
nokker doctor          # Docker, Lokkal compose, web page, server, authentication, client version, git
nokker doctor --fix    # also runs `docker compose up -d` when that can help
nokker update          # new core images, then replaces the nokker binary
nokker update --check  # only compares your version with the latest release
nokker logs -f         # follow the core logs
```

`nokker update` runs `~/.nokker/install.sh --update` (your `.env` and data are kept), then replaces the `nokker`
binary with the one from the same release. The new binary is accepted only if its `.sha256` matches and, when `cosign`
is installed, its signature verifies. Details in [cli.md](cli.md).

## Uninstall

```sh
sh ~/.nokker/install.sh --uninstall           # stop and remove containers, keep data in ~/.nokker
sh ~/.nokker/install.sh --uninstall --purge   # also erase database, memory and state (asks to confirm)
```

On Windows: re-run the installer with `-Uninstall` (and `-Purge`). Your work folders are never touched.

## Known issues

- On macOS and Windows, the installer writes `lokkal.json` under `~/.config/nokker` (Windows: `%USERPROFILE%\.config\nokker`),
  while the client looks for its config in `~/Library/Application Support/nokker` (Windows: `%APPDATA%\nokker`). Until
  this is aligned, if `nokker doctor` says no Lokkal is installed, set
  `NOKKER_CONFIG_DIR_OVERRIDE` to the folder the installer used (for example `~/.config/nokker`).
