# Code sessions on a shared server (enterprise deployment)

Status: **beta**. Built, off by default: you turn it on with the `code` block of the core configuration.

On your own machine, Nokker Lokkal runs code sessions for one owner ([cli.md](cli.md)). On a shared server, several
people of several organisations use the same core: tenants, row-level security in Postgres, enrolled devices, SSO
([teams.md](teams.md)). This page is about code sessions in that setting: each signed-in user opens sessions in their
own tenant, each session works in its own folder and its own sandbox (a Docker container or a Kubernetes pod), and
nobody reaches another user's folder.

## Turning it on

```yaml
code:
  enabled: true
  backend: kubernetes            # docker | kubernetes; "local" is refused outside Lokkal
  workspace_root: /workspaces    # the workspace volume, mounted in the core
  layout: tenant/user/session    # each session: <workspace_root>/<tenant>/<user>/<session>
  max_sessions_per_user: 3
  idle_ttl_s: 1800               # sandbox released after this much inactivity; files are kept
  instance: my-release           # stable id of this core (default: NOKKER_INSTANCE, then HOSTNAME)
  modes: [default, accept_edits, plan]
  default_mode: accept_edits
  hooks: false                   # repository hooks, once trusted by the user
  forge:
    allowed_hosts: [git.example.org]
  secrets:                       # same shape as the local vault (secrets.md); NOKKER_SECRETS_PROVIDER wins
    provider: openbao
    openbao: {address: https://bao.example.org, mount: nokker, prefix: corp, auth: approle,
              role_id_file: /run/bao/role_id, secret_id_file: /run/bao/secret_id}
  kubernetes:                    # passed as is to the Kubernetes sandbox module
    namespace: ""                # empty: the core's namespace
    image: ghcr.io/nokker-ai/sandbox:<version>
    pvc: workspaces
    resources: {cpu: "2", memory: 4Gi}
    exec_timeout_s: 600
    network: deny                # deny | egress
```

The core checks this block when it starts and refuses to start on a mistake: an unknown backend, `backend: local`,
a relative or missing `workspace_root`, a wrong type in `kubernetes`, or the Kubernetes backend without its sandbox
module in the image. With `enabled: false` (the default) nothing changes: no `/v1/code` route, no code tool.

## Sessions

An account signed in with an enrolled device or through SSO calls:

| Route | What it does |
|---|---|
| `POST /v1/code/sessions {repo?, mode?}` | Opens a session, optionally cloning a repository; returns its `id` |
| `GET /v1/code/sessions` | Your live sessions, and your limit |
| `GET /v1/code/sessions/{id}` | One session |
| `DELETE /v1/code/sessions/{id}` | Closes it; `?purge=1` also deletes its files |
| `POST /v1/code/sessions/{id}/hooks/trust {sha256}` | Trusts the repository hooks you reviewed |

A session is a conversation thread: send your messages to `/v1/chat/completions` with `X-Session-Id: <id>`. The model
gets the same code tools as in Lokkal (read, search, write, patch, run a command, a task list, restore points), working
in `/workspace` inside the sandbox. Another user's session, or a session of another tenant, does not exist for you
(404).

What protects the folders:

- each session folder is created part by part and checked: a symbolic link anywhere in the path is refused;
- the sandbox mounts only the folder of its own session;
- the core reads files with resolved paths bound to the session folder: `../` or a symbolic link that points outside
  is refused;
- writes and commands run in the sandbox.

After `idle_ttl_s` without a call, the sandbox is released and the session becomes `idle`; your files stay, and the next
call starts a new sandbox. At start-up the core removes the sandboxes it left behind (same `instance`).

## Restore points

Each session has its own restore point store, under `code.state_dir/<tenant>/<user>/checkpoints/<session>`: never
shared between tenants, outside every folder a sandbox can see. Closing a session keeps its restore points;
`?purge=1` deletes them with the files.

## Erasing an account

`nokker erase --account <id> --apply --confirm <id>` also erases the tenant's code sessions, their folders, their
restore points, trusted hooks and the forge credentials stored for the tenant. The dry run (without `--apply`) counts
them.

## Approvals and modes

The rules are those of Lokkal ([permissions.md](permissions.md)): the session mode, the rules of the repository's
`.nokker/settings.json` (they can only make things stricter, and the file is ignored if it is a symbolic link), and the
rules of the team projects that declare the repository ([teams.md](teams.md)). Cards go through the server's approval
flow, decided on an enrolled device; a decision applies once.

## Repositories and forge credentials

`repo.url` must be an `https://` URL on a host listed in `code.forge.allowed_hosts`. Never put a credential in the URL:
store it by name, then refer to the name.

```
PUT /v1/code/credentials/forge-bot {"value": "bot:<token>"}
POST /v1/code/sessions {"repo": {"url": "https://git.example.org/team/app.git", "credential": "forge-bot", "ref": "main"}}
```

The value is `token` or `user:token`. It is stored in the core's vault under your tenant (OpenBao:
`<mount>/data/<prefix>/tenants/<tenant>/forge/<name>`; file provider: one encrypted file per tenant), it is never
returned by the API, and it never enters the sandbox: the core clones the repository itself, with git hooks off,
https only, no submodules, no redirects, and the credential passed as a header through git's environment. Error
messages are redacted.

The AppRole of the core needs:

```hcl
path "<mount>/data/<prefix>/tenants/*"     { capabilities = ["create", "read", "update"] }
path "<mount>/metadata/<prefix>/tenants/*" { capabilities = ["read", "list", "delete"] }
```

## Deploying on Kubernetes

The Helm chart provides: the workspace volume mounted in the core at `workspace_root` and named in
`code.kubernetes.pvc` (ReadWriteMany, or ReadWriteOnce with sandboxes placed on the core's node); the same user or a
common `fs_group` for the core and the sandboxes; `code.instance` set to the release name (pod names change on each
rollout); a Role limited to pods, pods/exec and persistent volume claims; git in the core image; network access from
the core to your forges; the vault configuration and its AppRole files; a persistent `code.state_dir`; one replica.

Not in this beta: sub-agents and repository skills inside these sessions, and approvals "for the session" or "always".
