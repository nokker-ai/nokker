# Ops: ssh hosts and the audit journal

Status: on in the Lokkal install (flag `local.ops`), off by default in the core. See the limits at the end.

Nokker does code **and** ops: the agent can run commands on your servers and read files from them, by name, with
approvals that follow the risk. Every call that has an effect, here or on a server, goes into a **chained audit
journal** you can export and verify.

## Declaring your hosts

Hosts live in **your** settings, next to `settings.json`, never in a repository (a repository comes with its code,
possibly from someone else, and does not get to declare servers):

```yaml
# ~/.nokker/state/hosts.yaml   (Lokkal install; /opt/data/hosts.yaml inside the core container)
hosts:
  - name: web1                 # the only identifier the agent uses
    address: 203.0.113.5       # DNS name or IP
    port: 22                   # default 22
    user: deploy
    key: secret:SSH_WEB1       # private key in the vault, never a path or an inline key
    role: prod                 # prod | preprod | dev, required
  - name: bastion
    address: bastion.example.net
    user: jump
    key: secret:SSH_BASTION
    role: preprod
  - name: db1
    address: 203.0.113.9
    user: dba
    key: secret:SSH_DB1
    role: prod
    jump: bastion              # optional: another host of the list, one hop only
```

Put the keys in the vault ([secrets.md](secrets.md)), from a file so the value never shows in your shell:

```bash
docker compose -f ~/.nokker/docker-compose.yml exec -T core python3 -m nokker --config /etc/nokker/config.yaml secret set SSH_WEB1 < ~/.ssh/id_web1
```

The file is read **once, when a session opens** (the session answer carries `ops: {enabled, hosts, roles, notices}`);
edit it and open a new session to see the change. An invalid entry (no role, a `key` that is not `secret:NAME`, an
address starting with `-` or containing `@`, an unknown or chained `jump`, an unknown field) is ignored as a whole,
with a notice. A host without a role is never treated as `dev`.

## What the agent can do

| Tool | Effect | What it does |
|---|---|---|
| `ssh_hosts` | read | Lists the hosts: name, role, address, port, user, jump, whether the host key is known. Never the key. |
| `ssh_run {host, command, timeout?}` | the command's | Runs a command on a host, by name. Classified like `terminal`: a read-only command is a read, a dangerous one is destructive. |
| `ssh_copy {host, remote_path, local_path?, max_bytes?}` | read, or write here | Reads a remote file (up to 25 000 bytes into the answer); with `local_path`, copies it into your working folder instead. |
| `ssh_trust {host, fingerprint?}` | read, then a card | First contact: shows the fingerprint the host presents; with `fingerprint`, adds that key to `known_hosts` once you approve. |

A name that is not in your list is refused. A "hardline" command (`rm -rf /`, `mkfs`, fork bomb…) is never run, on
any host, approved or not.

## Approvals follow the role

| Host role | Read-only command | Any other command |
|---|---|---|
| `dev`, `preprod` | allowed | your mode and rules, as for `terminal` ([permissions.md](permissions.md)); destructive = strong card |
| `prod` | allowed | **strong card, always**: in every mode, whatever your `allow` rules (session or "always"), whatever is said in the message |

On `prod`, your `deny` rules and plan mode still refuse (they are stricter than a card). A `prod` card is approved
with a **device key** (a passkey with user verification), never with the local token: on your machine the token is a
file, and with the `local` backend the agent could read it. You can still **reject** a `prod` card from the terminal. Rules suggested on cards look like `ssh_run(dev1:git pull*)`
(`host:command`); a `prod` card and a first-contact card offer none.

## Host keys

`known_hosts` is yours, in OpenSSH format, next to `hosts.yaml` (`~/.nokker/state/known_hosts`). You can fill it
yourself (`ssh-keyscan -p 22 203.0.113.5 >> ~/.nokker/state/known_hosts`, after checking the fingerprint), or let the
agent do the first contact:

1. `ssh_run` on a host whose key is unknown is refused: "call `ssh_trust` first";
2. `ssh_trust {host}` connects without authenticating and shows `SHA256:…`;
3. `ssh_trust {host, fingerprint}` is **always** a card showing the host, address and fingerprint: check it another
   way, then approve; the key is added only if the host still presents that exact key.

Nokker never connects with `StrictHostKeyChecking=no` and never adds a key to your file on its own. A key that is
already known is never replaced: if a host's key changed, the call fails with "possible interception", and you fix
`known_hosts` yourself. A jump host must be trusted before the hosts behind it.

## Where the key goes

At the moment of the call, the core reads the key from the vault and hands it to the session's sandbox **on standard
input** (never on a command line), where it is written to a `0600` file in a private temporary folder, used by the ssh
client, then deleted when the command ends. The client runs in the sandbox like `terminal`, with your user config
ignored, no agent, no forwarding, no password prompts, no connection sharing, and a `known_hosts` reduced to the hosts
it touches. The key never appears in a tool argument, a result or the model's context; if a server sends it back, it
is replaced by `[secret:NAME]`.

## The audit journal

Every executed call whose effect is not a read (files, commands, sub-agents, approved cards) and every `ssh_run` or
`ssh_copy` (reads included: they touch another machine) is appended to `~/.nokker/state/audit/audit.jsonl`, one JSON
line per call, each carrying the hash of the previous one:

```json
{"v":1,"seq":12,"at":"2026-10-25T10:00:00+00:00","thread_id":"th_…","turn_index":3,"channel":"api",
 "actor":"local-owner","tool":"ssh_run","effect":"write","target":"ssh:web1","role":"prod","address":"203.0.113.5:22",
 "args":{"host":"web1","command":"systemctl reload nginx"},"args_sha256":"…","is_error":false,
 "result_sha256":"…","preview":"…","ms":412,"prev":"…","hash":"…"}
```

Arguments and preview are redacted; long strings are cut (the full arguments' hash is kept). `actor` is who approved
the card when the call came from one.

```bash
docker compose -f ~/.nokker/docker-compose.yml exec core python3 -m nokker --config /etc/nokker/config.yaml audit verify
#  journal intact : 128 entrée(s), ancre 128:5f1c…
docker compose -f ~/.nokker/docker-compose.yml exec core python3 -m nokker --config /etc/nokker/config.yaml audit export --format csv --since 2026-10-01 > audit.csv
```

| Command | What it does |
|---|---|
| `audit export --format jsonl\|csv [--since DATE]` | JSONL is the journal as is (verifiable again); CSV is flat, `args` as JSON |
| `audit verify [--anchor seq:hash]` | Exit 0 if intact; otherwise lists each problem: modified entry, removed entry, truncated journal |

Keep an anchor (the last line of `verify`) somewhere else from time to time: someone who can write to the state folder
could rewrite the whole chain consistently; the anchor is what catches that.

## Alerts

An alert from your monitoring can open an incident and start an agent: see [incidents.md](incidents.md) (**beta**).
Agent runs do not open a code session yet, so they do not reach your hosts: the agent picks the runbook and prepares
the steps, and you run them in a session, where the rules above apply.

## Limits

- The Lokkal compose puts sandboxes on a network that reaches the Internet only: hosts on your local network are not
  reachable from the docker sandbox yet. The `local` backend reaches them.
- The ssh client refuses to run under a user id the sandbox image does not know: today's image knows uid 1000.
- Approving a `prod` card from your phone arrives with the gateway: **rolling out** ([security.md](security.md)).
  Meanwhile, use a device enrolled on this machine.
- One jump only; no port forwarding, no interactive sessions, no file upload.

## Trying it

The test suite has a real run against a throwaway sshd: `NOKKER_TEST_SSHD=127.0.0.1:<port>`,
`NOKKER_TEST_SSHD_USER`, `NOKKER_TEST_SSHD_KEY=<private key made for the test>`, then
`pytest tests/unit/tools/test_ssh.py -k real` (add `NOKKER_TEST_SSHD_BACKEND=docker` and `NOKKER_TEST_DOCKER_ROOT` to
run the client in the docker sandbox). Never point it at a real machine.
