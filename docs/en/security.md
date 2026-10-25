# Security model

Nokker has a lot of power: it edits your code, runs commands, reaches your servers and your mailbox. This page says,
in one place, what keeps that power under your control. Everything here is enforced by the core, not by the model:
what the model writes, or what a repository says, cannot change it.

## 1. Every action has an effect, and the effect decides

Every tool call carries an effect: `read`, `write`, `destructive`, `send`, `spend`. A tool that declares none counts as
a write. A shell command is classified on each call. Your mode (`default`, `accept_edits`, `auto`, `plan`) and your
rules (`allow`, `ask`, `deny`) decide what goes through, what asks, what is refused. A repository can only make them
stricter. Details: [permissions.md](permissions.md).

Some things are never possible, whatever the rules: hardline commands (`rm -rf /`, `mkfs`, fork bombs and the like)
and bulk sending are refused, without a card.

## 2. Cards and strong cards

When an action needs you, the core shows an **approval card**: the tool, its effect, the exact command or path. You
approve once, for the session, or always (which writes a rule into **your** settings, chosen among the rules the core
suggests). An approval is bound to the exact arguments: if they change, the decision no longer applies. A card
expires; an expired card is a refusal.

A **strong card** is for what is hard to undo or reaches other people:

| Action | Card |
|---|---|
| A destructive command, restoring a restore point, installing a skill or a pack | Strong: **once** only, never "for the session" or "always" |
| Any change on a host with role `prod` ([ops.md](ops.md)) | Strong, in every mode, whatever your `allow` rules. Never approved with the local token. |
| Arming an agent that may write without asking ([agents.md](agents.md)) | Strong, for those exact rules |
| Sending or deleting through a connector of your nokker.ai account (mail, calendar) | Strong, signed on your phone, checked by nokker.ai before it runs |
| A destructive action asked from the web, through the gateway | Strong, decided on an enrolled device |

A written "go ahead" in a message never replaces a strong card. On strong cards, a reviewer model adds a short advice
and a risk level ([models.md](models.md)); it advises, it never decides.

## 3. Approving from your phone

Status: **rolling out** with the gateway ([gateway.md](gateway.md)); it needs an install linked to a nokker.ai account.

When a strong card waits on your machine, the machine posts it to your account. You get a notification (Telegram if
you paired it) with a link to `app.nokker.ai/approve/…`. Nothing is decided in the notification. You sign on the page
with a **passkey that verifies you** (Face ID, Touch ID, Windows Hello).

nokker.ai is only a mailbox: it has no device key and cannot decide. **Your machine checks the signature itself**,
against the public key of your device (pinned the first time it sees it), the challenge it created for that card,
the hash of the exact arguments, and a one-time nonce. A replayed or altered signature is refused. A device that only
logs in (e-mail code, Google) cannot decide.

## 4. What you see when you sign

The page puts first what is about to happen, and says who described it:

- **A connector of your account** (mail, calendar): the machine sends the arguments with the card. nokker.ai
  recomputes their hash, refuses them if it does not match the signed challenge, and writes the summary itself:
  recipients one by one, subject, the start of the body, guests, dates. Nothing is hidden: unknown arguments are
  listed under their own name. Secrets are redacted, invisible and direction-changing characters are removed, so a
  value cannot pose as another line. nokker.ai then runs the call only if its arguments are those you saw.
- **A local action** (a `prod` command, a rollback): the arguments never leave your machine. The page shows the
  machine's own description, labelled **"described by your machine"**, with the hash of the arguments. Only your
  machine can run it.

After the decision, the details are erased; the headline, the status and the hash remain.

## 5. Secrets are redacted before any model

- Secrets live in a local vault and are referred to **by name** (`secret:NAME`): the core reads the value at the
  moment of use, outside the model ([secrets.md](secrets.md)).
- Every value in the vault, the core's own keys, and known key formats (cloud and forge tokens, private keys, URLs
  with credentials, passwords in code…) are replaced by a handle in tool results, replies and stored threads.
- A last filter redacts **every message just before any model call**: the main model, the reviewer, the summariser.
- The same redaction applies to hook input and output, memory notes, audit entries, alert bodies and `nokker review`.

## 6. The sandbox

Commands do not run on your host. Each session gets a disposable container with your work folder mounted at
`/workspace` and nothing else from the host. Outbound traffic goes through an egress proxy that refuses private
ranges (your LAN, the database, the core, cloud metadata). The core reaches Docker only through a filtering proxy that
refuses privileged containers, extra mounts and other networks. Hooks run in the sandbox too. Details:
[permissions.md](permissions.md#the-sandbox).

## 7. What never leaves your machine

Every resource has a locality: `local`, `cloud` or `both`. What is `local` never goes to nokker.ai, through the
mirror or the gateway: your repositories and files, your ssh hosts and keys, your vault, connectors you set up in
Lokkal, project notes by default. Anything that does not declare a locality is treated as `local`. A filter applies
this just before anything is written to the network.

- Your code runs and stays on your machine. Memory runs on your machine, with a local embedding model
  ([memory.md](memory.md)).
- The local API listens on `127.0.0.1` only and needs the local token (a `0600` file); a web page you visit cannot
  drive it.
- What goes out: the requests to the model you chose, and, only if you link an account, what the link needs: the
  load of the machine, the **names** of its resources, the deck (folder names and states), the cards to sign. The
  subscriber mirror (threads and your `user` notes, never the work folder) is off unless you turn it on
  (`local.mirror`), and is **rolling out**.
- A web session that needs something local hands over to a session on your machine, with that machine's gates; the
  web side only receives the result ([gateway.md](gateway.md)).
- Credentials of your nokker.ai connectors stay on nokker.ai; the machine never receives them ([models.md](models.md)).

## 8. The audit journal

Every executed call whose effect is not a read, and every call to a server (`ssh_run`, `ssh_copy`), is appended to
`~/.nokker/state/audit/audit.jsonl`: one JSON line per call, with who approved it, redacted arguments, and the hash of
the previous line. `audit verify` detects a modified, removed or truncated entry; keep an anchor elsewhere to detect a
rewritten chain. Export as JSONL or CSV. Details: [ops.md](ops.md#the-audit-journal).

Calls relayed to your nokker.ai connectors are logged on both sides, without arguments or results (tool, effect, card,
outcome, hashes).

## 9. Reversible by default

Before any write, the folder is snapshotted in a shadow repository outside your own, so you can see what changed and
roll back ([checkpoints.md](checkpoints.md)). Sub-agents work in separate git worktrees ([subagents.md](subagents.md)).

## Known limits

- The local vault file is encrypted with a key on the same machine: it protects a copied file, not an account that
  can already read your files. Use the OpenBao provider for that ([secrets.md](secrets.md)).
- A hook runs your command without a card; if it calls a script in the repository, it runs what that script contains
  now ([hooks.md](hooks.md)).
- Someone who can write to the state folder can rewrite the whole audit chain consistently; the external anchor is
  what catches it.
