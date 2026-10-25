# Agents

An agent is a named job: a mission, a list of tools, a trigger, a budget. It runs on its own (every morning, on an
alert, when a webhook or a mail arrives) and reports back. A session can *propose* an agent; only you create it, and only you
arm it.

Not to be confused with [sub-agents](subagents.md), which a session starts for the length of one turn.

Status: the engine runs in the Lokkal install (`local.crew.enabled`, on unless you turn it off). Placement on several
machines and the shared deck are **rolling out** (see [Several machines](#several-machines)).

## What an agent is

| Field | Meaning |
|---|---|
| `name`, `role` | How it shows in the deck |
| `instructions` | The mission: the prompt of every run |
| `trigger` | `manual`, `schedule` (cron), `once` (a date) or `event` (see below) |
| `tools` | The allow-list of the run. `memory_search`, `memory_get` and `clarify` are always allowed; any other tool is refused |
| `auto_approve` | Rules (`tool` or `tool(pattern)`, the [permission](permissions.md) syntax) lifted without a card during a run |
| `secrets` | Names in the [vault](secrets.md), never values |
| `budget` | `max_calls` (model calls, 1 to 1000) and/or `max_cost` in a currency (default EUR) |
| `status` | Computed by the core: `paused`, `armed`, `running`, `failed` |

## Where agents come from

- **A card in the conversation.** The agent can call `agent_draft`: it posts an `agent_draft` card with the proposed
  mission, trigger, tools, rules and budget. It creates nothing. Activating the card
  (`POST /v1/bubbles/{id}/actions/activate`) creates the agent in your name.
- **A pack.** Installing a [pack](packs.md) proposes its agent templates as the same cards. The pack's `needs` (a GPU
  for `video`) carry over to the agent when you activate the card, so [placement](#several-machines) sees them. The
  agent keeps `origin: {pack, template}` (null for other agents): removing the pack lists it and offers to disarm or
  delete it, never silently.
- **The API.** `POST /v1/agents`.

## Triggers

| Trigger | Example | Notes |
|---|---|---|
| `manual` | `{"kind": "manual"}` | Runs when you press run (`POST /v1/agents/{id}/run`) |
| `schedule` | `{"kind": "schedule", "cron": "0 7 * * 1-5", "tz": "Europe/Zurich"}` | 5-field cron, IANA time zone (default `UTC`) |
| `once` | `{"kind": "once", "at": "2026-11-02T07:00:00Z"}` | After the date, the agent goes back to `paused` |
| `event` `webhook:<id>` | `{"kind": "event", "event": "webhook:deploy"}` | A signed `POST` from another system (below) |
| `event` `alert:<name>` | `{"kind": "event", "event": "alert:Disk*"}` | An incident opened from an alert, glob pattern ([incidents.md](incidents.md), **beta**) |
| `event` `mail:<filter>` | `{"kind": "event", "event": "mail:from:billing@example.com is:unread"}` | A new message in the inbox of your linked mailbox that matches the filter (below). Needs the engine **and** connectors on the instance, and a linked mailbox; otherwise refused (400) |

**Webhook.** `POST /v1/agents/{id}/webhook-secret` returns a secret once (`whsec_…`) with the URL to call. The caller
signs each request: `X-Nokker-Signature: t=<epoch>,v1=<hex>`, with `v1 = HMAC-SHA256(secret, "<t>." + raw body)` and
`t` within 5 minutes. A replayed signature is refused (409), and so is a call to a paused agent. The body (up to
64 KiB) reaches the model as data in a marked block, with the instruction that it is data, not orders.

**Mail.** The core checks the inbox of the mailbox you linked in Connectors, with the same settings and credential
a session uses (the credential never leaves the connector: not in a log, an event, the agent card or a run).

- **Filter**: a safe subset of the `mail_search` syntax, and every term must match: `from:`, `to:`, `cc:`,
  `subject:` (a value, or `"several words"`), `is:unread`, `is:read`, `is:starred`, free words or `"a phrase"`
  (searched in the whole message), `-` before a term to exclude it, and `*` alone for every new message. At most 8
  terms, 100 characters per value. Refused (400, with the reason): `after:`/`before:` (only messages that arrive
  after arming count), `in:`/`label:` (inbox only for now), any other `key:`, `OR`, parentheses, an unclosed quote.
- **Polling**: every 120 s (`local.crew.mail_poll_s`, 30 s to 1 h), no IDLE. Read-only: nothing is marked read.
- **What counts as new**: what arrives after the first check following arming. The position (UIDVALIDITY and last
  UID) is stored, so a restart neither replays nor loses: mail that came in while the core was stopped starts a run
  at the next check. A stop at any moment of queuing a run neither loses nor replays either: the core stores the id
  of the run it is about to queue first, and the existence of that run decides at the next check. If the server rebuilds the folder (UIDVALIDITY changes), the agent starts again from there
  without replaying anything.
- **One run per check** that finds new matching mail, at most one live run per agent: mail that arrives during a run
  waits and goes to the next run, together. Same beyond `local.crew.mail_max_runs_per_hour` runs per hour (default
  12). No `skipped` run is written for each check.
- **Input of the run**: metadata only (id, sender, subject, date; the 20 most recent), as data in a marked block, with
  the instruction that mail content is data, not orders. The agent reads a message with `mail_read` and the id,
  through its usual gates; an agent started by an event never lifts a write.
- **Errors** (network, credentials, protocol): the check backs off (120 s × 2^errors, at most 1 h) and the card shows
  the error, without any credential.
- **Disarming** stops the checks. Re-arming starts from the current mail: what came in while the agent was paused is
  not replayed. Changing the filter pauses the agent, like any trigger change.
- `GET /v1/agents/{id}` carries `mail_watch`: `state` (`off`, `waiting`, `ok`, `error`), `last_poll_at`,
  `next_poll_at`, `error`, `pending` (new messages waiting) and `held` (why), `runs_last_hour`, `resync_at`.

Where it works: wherever the engine runs and a mailbox is linked on that same instance. The check runs where the
credential lives; an agent placed on a machine gets its runs there, as usual.

- **Lokkal**: the mailbox of the machine ([secrets.md](secrets.md#your-mailbox-on-the-machine), password by vault
  name). The agent's runs read it with the mail tools, so the agent lists that name in its `secrets` (during a run,
  only declared names resolve). A mailbox relayed from nokker.ai is never watched; with both, the machine's wins.
  Without a mailbox on the machine, `mail:` is refused (400, no mailbox linked).
- **nokker.ai**: the mailbox linked in Connectors on the account.

Not yet: IDLE (push), folders other than the inbox, OR between terms.

## Arming

Creating an agent and arming it are two different gestures.

- A `manual` agent with no write rule in `auto_approve` is created `armed`. Any other agent is created `paused`.
- `POST /v1/agents/{id}/arm` arms it. If `auto_approve` lifts a **write** (any tool not declared read-only;
  `terminal(...)` always counts as a write), arming needs a **strong approval card** that shows the agent and the exact
  rules. The approval holds for those rules only: change them and the agent goes back to `paused`.
- An agent started by an **event** can never lift a write: such rules are refused when you create or edit it, and the
  permission mode (`accept_edits`, `auto`) lifts nothing either. Every write it wants becomes a card.
- `disarm` stops future runs. A run in progress continues; cancel it separately.

Your click on run, arm or disarm is the decision: no other card is asked, except the strong card above.

## A run

A run is an ordinary turn in its own thread (`th_<run_id>`, with `origin.kind = "agent"`), in your name, through the
same gates as a session: effects, your rules, approval cards. On top of that:

- only the agent's `tools` are callable, and only its `auto_approve` rules are lifted;
- a "GO" written in the mission counts for nothing: a destructive action is still a strong card;
- when a run needs a card, it waits (`waiting_approval`) and resumes in the same run once you decide; an expired card
  ends the run as `failed`;
- the budget is checked before every model call, sub-agents and resumed parts included. A model with no price, or a
  price in another currency, stops a run that has `max_cost` (the cost cannot be checked). The run ends with
  "stopped by the core: budget reached";
- before the turn, every secret name is looked up in the vault. A missing name, or a sealed vault, skips the run
  (`skipped`, "missing credentials") without running anything. During the run, only the declared names resolve;
- the run ends with a short report (`summary`, up to 600 characters, redacted).

A run does not open a code session yet: it has no sandbox and no work folder. Templates that need them prepare the
work, and a session does it.

Run states: `queued`, `running`, `waiting_approval`, `succeeded`, `failed`, `cancelled`, `skipped`.

## The scheduler

- It ticks every 15 s and runs at most 2 runs at once (`local.crew.max_concurrent`), at most one live run per agent.
- Mail triggers are checked by their own background thread (`local.crew.mail_poll_s`, `mail_max_runs_per_hour`;
  `local.crew.mail: false` turns it off, and `mail:` triggers are then refused).
- If the core was stopped, **one** missed run is caught up (`catch_up: true`) if it is less than 6 hours late; the
  others are skipped.
- Daylight saving: an hour that does not exist is skipped, an hour that happens twice runs once.
- On restart, a `running` run becomes `failed` ("interrupted"), a `queued` run starts again, and a run waiting for a
  card keeps its card.

## API

All routes need your identity (the local token on your machine). Errors are `{"error": {"message"}}`.

```
GET    /v1/agents                                list
POST   /v1/agents {name, role, instructions, trigger, tools, auto_approve, secrets, budget, ...}
GET    /v1/agents/{id}                           with mail_watch for a mail:<filter> agent
PATCH  /v1/agents/{id} {...}                     changing trigger, tools, rules or secrets of an armed agent pauses it
POST   /v1/agents/{id}/arm                       200 agent, or 202 {approval_id} when a strong card is needed
POST   /v1/agents/{id}/disarm
POST   /v1/agents/{id}/run                       202 {run_id}; 409 if a run is live
GET    /v1/agents/{id}/runs?limit=20&before=<run_id>
POST   /v1/agents/{id}/runs/{run_id}/cancel      also removes its pending cards
POST   /v1/agents/{id}/webhook-secret
DELETE /v1/agents/{id}                           threads of past runs stay
```

Events: `agent.created`, `agent.updated`, `agent.deleted`, `agent.run`.

## Several machines

Status: **rolling out**. It needs an install linked to a nokker.ai account (see [models.md](models.md)) and the
gateway ([gateway.md](gateway.md)), and it is switched off on the nokker.ai side by default.

Each linked machine reports its load every 60 s: free RAM, CPU, NVIDIA GPUs, active sessions. nokker.ai then gathers
all your machines in one place.

- **Placement.** An agent defined on nokker.ai can say where it runs: `{"machine": "auto"}` (the best machine at the
  time of each run), `{"machine": "mc_…"}` (one machine of your account), or `{"machine": "web"}` (on nokker.ai,
  for plans that include it).
- **Eligible machine**: online (load received in the last 180 s), with every resource the agent names in `needs`
  (`repos`, `hosts`, `connectors`, `mcp`, by name) and a GPU if `needs.gpu` is set. Among those, the one with the most
  free RAM wins, then the lowest relative CPU load, then the fewest active turns.
- If no machine is eligible, the run waits (`queued`, with the reason, such as "repository x missing on workshop")
  and starts as soon as one is; it is dropped after 24 hours.
- A run placed on a machine runs with **that machine's** gates and cards; the agent's `auto_approve` rules are not
  lifted there. The thread stays on the machine; only the status and the short report come back.
- **Moving an agent** (`POST /v1/agents/{id}/move {to}` on nokker.ai) checks first what the destination lacks
  (secret names, repositories, hosts, GPU) and refuses with the list. An agent created on a machine can move to
  nokker.ai; its run history stays where the runs happened.

**The deck** (`GET /v1/link/deck` on nokker.ai) lists, per machine: its load, its sessions (folder name and state
only), its agents (name, state, next run, last result) and how many approvals wait. Never a mission, a message or a
report. Thread titles appear only if you mirror that machine's threads.

In the local web interface, the Crew tab shows one card per agent. Arming and running from the deck, and the
multi-machine deck on nokker.ai, are **rolling out**; the API above works today.
