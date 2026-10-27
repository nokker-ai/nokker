# Secrets

Nokker keeps your secrets (API keys, passwords, tokens, ssh keys) in a **local vault** and refers to them **by
name**. A secret's value never enters the model's context: your configs say `secret:NAME`, the core reads the value
at the moment it is used, and any vault value that comes back in a tool's output is redacted before the model sees
it.

## Storing a secret

```bash
# in the Lokkal install (the core container)
docker compose -f ~/.nokker/docker-compose.yml exec core python3 -m nokker --config /etc/nokker/config.yaml secret set GITHUB_TOKEN
# from a file or a pipe (one trailing newline is dropped)
docker compose -f ~/.nokker/docker-compose.yml exec -T core python3 -m nokker --config /etc/nokker/config.yaml secret set DEPLOY_KEY < ~/.ssh/deploy
```

| Command | What it does |
|---|---|
| `secret set NAME` | Stores `NAME`. The value is read from stdin, or asked twice without echo. Never an argument (it would end up in your shell history and in `ps`). |
| `secret list` | Names and last update dates. Never values. |
| `secret rm NAME` | Deletes `NAME`. |
| `vault status` | Which provider is in use; for OpenBao: reachable, sealed or unsealed. |
| `vault migrate --from file --to openbao` | Copies the vault to the other provider (see below). Also `--from openbao --to file`. |

The `nokker` command of the host talks to the same thing through the local API (planned: `nokker secret` on the
host client):

| Route | Body | Answer |
|---|---|---|
| `GET /v1/local/secrets` | | `{provider, notice?, sealed?, secrets: [{name, updated_at}]}` |
| `PUT /v1/local/secrets/{name}` | `{"value": "..."}` | `{name, updated_at}`: the value is never sent back |
| `DELETE /v1/local/secrets/{name}` | | `{name, deleted: true}`, or 404 |

They need the **local token** like every `/v1/local/*` route (401 without it). Through the gateway
(`<name>.nokker.app`), listing names is allowed, writing or deleting is refused (403): a value would cross the relay.
Errors: 400 (bad name or value), 503 (vault unavailable: key missing, OpenBao unreachable, or **sealed**: `code:
"sealed"`, message "coffre scellé").

Names: letters, digits, `_`, `.`, `-`, up to 128 characters, starting with a letter or a digit. Values: a non-empty
string up to 64 KiB.

## Referring to a secret

Wherever the **core** needs a credential, write `secret:NAME` instead of the value:

```yaml
# config.yaml: an MCP server whose token lives in the vault
mcp_tool_servers:
  - name: tracker
    url: https://mcp.example.com/mcp
    token: secret:TRACKER_TOKEN          # sent as "Authorization: Bearer <value>"
    headers: {X-Team: ops, X-Api-Key: secret:TRACKER_KEY}
```

| Where | Status |
|---|---|
| MCP servers (`token`, `headers`) | resolved each time a session to the server opens: a changed value is picked up without a restart |
| connectors (mail, calendar): the stored credential can be `secret:NAME` | resolved when the connector is built and when it is tested |
| the mailbox of the machine (`/v1/local/connectors/mail`, below) | ALWAYS a name: resolved at each mail tool call and each check of a `mail:` trigger |
| ssh hosts (`hosts.yaml`, `key: secret:NAME`) | read at each call, handed to the sandbox on stdin, never in the model's context ([ops.md](ops.md)) |
| [agents](agents.md) (`secrets: [NAME]`) | resolved before each run; a missing name skips the run. During a run, only the names the agent declares resolve; any other reference fails |
| environment of a process the core starts (stdio MCP) | the resolver is ready; injecting values into a sandbox environment is **planned** |

A missing name fails with a clear message that names it (`secret:NAME : nom absent du coffre`), never with a value.
A sealed OpenBao makes the reference fail the same way.

## What the model sees

- References, never values: a reference is resolved by the core, outside the model, just before use.
- Every value in the vault is added to the redaction of tool outputs and replies (the same redaction as any known
  secret, see [permissions](permissions.md)): a tool that echoes a value back shows `[secret:NAME]` to the model and
  in the stored thread. Keys of the core itself (Router key, machine token, BYOK keys) are redacted too, as
  `[secret:nokker:router]`.
- A value replaced or deleted stays redacted until the core restarts (it may have reached a thread before).
- Values shorter than 4 characters are not redacted (they would replace ordinary words everywhere).

## Providers

The provider is chosen **explicitly**: `NOKKER_SECRETS_PROVIDER=file|openbao` (set by the compose), else
`local.secrets.provider` in `config.yaml`, else `file`. An OpenBao address alone switches nothing: the core logs a
notice and `GET /v1/local/secrets` shows it.

### `file` (default)

The local vault of the first launch, `~/.nokker/state/vault.bin` (mode 0600), encrypted with Fernet. The key is
`NOKKER_LOCAL_SECRET_KEY` in `~/.nokker/.env`, generated at install; outside the compose, a key file
`<state>/vault.key` (0600) is created for a new vault only, never next to an existing one. Without a key, nothing is
written.

It protects a copy of the file alone (a backup, a mistaken share) and the folder from tools (sandboxes do not mount
it). It does not protect against someone who has your account on the machine: they can read the key. For that, use
OpenBao.

### `openbao`

An OpenBao (or a compatible Vault) server, KV v2 mounted at `nokker/`: a secret is `nokker/data/<scope>/<NAME>`
(`{value}`), `<scope>` being `user` (yours) or `system` (keys of the core). The core authenticates with AppRole read
from **files** (`NOKKER_OPENBAO_ROLE_ID_FILE`, `NOKKER_OPENBAO_SECRET_ID_FILE`), never from variables, which would
leak into child processes. Its policy needs `nokker/data/*` and `nokker/metadata/*` only. On a 403 or an expired
token it logs in again once, then reports the error. No transit engine: the values are protected by OpenBao's own
encryption at rest.

Plain `http://` is accepted only to the loopback or to a host name without a dot (the `openbao` service of the
compose network); elsewhere use `https://` (`ca_file` for your own CA) or set `allow_http` explicitly.

The core **never unseals** OpenBao and never sees an unseal key share. Install with `install.sh --vault` (the
companion `vault-unseal` unseals it after a reboot from a key file on your machine) or `--vault-manual` (the key is
shown once at init; after a reboot run `docker compose -f ~/.nokker/docker-compose.yml exec openbao bao operator unseal`, it asks for the
key without echo). While sealed, secret routes answer 503 "coffre scellé", references fail, and nothing falls back
to the file. Without the `vault` profile the credential files are absent: choosing `openbao` then answers 503
"coffre OpenBao non configuré", and the core keeps running.

```yaml
local:
  secrets:
    provider: openbao                    # or NOKKER_SECRETS_PROVIDER
    openbao:
      address: http://openbao:8200       # or NOKKER_OPENBAO_ADDR
      mount: nokker
      role_id_file: /run/nokker-vault/role_id       # or NOKKER_OPENBAO_ROLE_ID_FILE
      secret_id_file: /run/nokker-vault/secret_id   # or NOKKER_OPENBAO_SECRET_ID_FILE
```

### Moving from `file` to `openbao`

```bash
docker compose -f ~/.nokker/docker-compose.yml exec core python3 -m nokker --config /etc/nokker/config.yaml vault migrate --from file --to openbao --check
docker compose -f ~/.nokker/docker-compose.yml exec core python3 -m nokker --config /etc/nokker/config.yaml vault migrate --from file --to openbao
```

Both scopes are copied, each value is read back and compared. Running it again changes nothing. A name that already
exists in OpenBao with another value is reported (`divergent`) and **not** overwritten unless you pass
`--overwrite`. The source is never modified. Then set `NOKKER_SECRETS_PROVIDER=openbao` and restart the core; delete
`vault.bin` yourself once you have checked. The reverse (`--from openbao --to file`) works the same way.

## Your mailbox on the machine

The owner of a Lokkal install can link one IMAP/SMTP mailbox **on the machine**. The password goes in the vault; the
connector keeps only its name. It never leaves the machine: not to nokker.ai, not through the gateway, not in the
mirror.

```bash
# 1. the password (an app password), in the vault
nokker secret set MAIL_PASSWORD
# 2. the mailbox, with the local token (the gateway gets 403 on these routes)
curl -X PUT http://127.0.0.1:8742/v1/local/connectors/mail -H "Authorization: Bearer $(cat ~/.nokker/state/local-token)" \
  -d '{"settings": {"preset": "custom", "address": "me@example.com", "imap_host": "imap.example.com",
                    "imap_port": 993, "smtp_host": "smtp.example.com", "smtp_port": 465},
       "secret": "MAIL_PASSWORD"}'
```

```
GET    /v1/local/connectors              {connectors: [{kind: "mail", state, settings, secret: "NAME", locality: "local"}], presets}
PUT    /v1/local/connectors/mail         {settings, secret: "NAME" | "secret:NAME"}   tested (IMAP and SMTP) before it is kept
POST   /v1/local/connectors/mail/test    {ok, message, code}
DELETE /v1/local/connectors/mail         {revoked}
```

- `secret` is a **name** in the vault. A value, a name that is not in the vault, or a `password` field is refused
  (400), and what you sent is not repeated in the answer. Presets: `gmail`, `icloud`, `infomaniak`, `fastmail`, or
  `custom` with hosts and ports. Mail only for now.
- The settings live in `connectors.json` under the core state folder (mode 0600), with `secret:NAME` and nothing else
  secret.
- Sessions of the owner get the mail tools (`mail_search`, `mail_read`, `mail_draft`, `mail_move`, `mail_trash`,
  `mail_send`), with locality `local`. `mail_send` and `mail_trash` are **always a strong card**, whatever the
  permission mode or `allow` rules; no agent rule lifts it.
- During an [agent](agents.md) run, only the names the agent declares resolve: an agent that reads this mailbox lists
  the password name in its `secrets`.
- A `mail:<filter>` trigger of the owner's agents watches this mailbox.
- If the machine also has the relayed mailbox of your nokker.ai account (connectors relayed, see the Lokkal docs), the
  mailbox of the machine wins: its tools take the `mail_*` names, and `mail:` triggers watch it, never the relayed one.
  Remove it (`DELETE`) to use the relayed one again.
- Mirror: a turn that used this mailbox, and every turn of a run started by `mail:`, stays on the machine (the mirror
  skips it). The machine tells nokker.ai only that it has a `mail` connector (for agent placement), never the address,
  the host or the name of the secret.
