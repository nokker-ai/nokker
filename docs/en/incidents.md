# Alerts, incidents and the plan

Two pieces for running work over time:

- **Incidents**: an alert from your monitoring opens an incident, and an [agent](agents.md) picks the runbook and
  starts on it. Every action that changes something waits for your card.
- **The plan**: goals, milestones and tasks as a tree of cards. A session or an agent working on a card receives the
  context of every card above it; progress is computed from the bottom up. A morning brief sums it up.

Status: **beta**, off by default. Two flags of the core configuration turn them on: `operate` (alerts and incidents)
and `plan`. Both are off in the install. Off, the routes answer 404, no tool appears and nothing is added to the
context. Agent runs (on an alert, the brief) need the agent engine, which runs in the install.

## Alert sources

Each source has its own signed URL.

```
GET    /v1/alerts/sources
POST   /v1/alerts/sources {name, format: alertmanager|generic, auth?: hmac|bearer}
       → 201 {source, secret, url: "/v1/alerts/as_…", header, signature}
POST   /v1/alerts/sources/{id}/secret       new secret; the old one stops working
DELETE /v1/alerts/sources/{id}              its incidents stay
```

The secret is shown **once**. Two ways to authenticate a source:

- `hmac` (default): `X-Nokker-Signature: t=<epoch>,v1=<hex>`, `v1 = HMAC-SHA256(secret, "<t>." + raw body)`, `t`
  within 5 minutes. A replayed signature is refused (409).
- `bearer`: `Authorization: Bearer <token>`, for Prometheus Alertmanager, which cannot sign a body
  (`http_config.authorization: {type: Bearer, credentials: <token>}`). The token is stored hashed. Replays are not
  detected in this mode: a replayed alert only adds an occurrence to the live incident.

Formats: `alertmanager` (webhook version 4) or `generic`: one alert, or `{alerts: [...]}`, each with
`name`, `status` (`firing` or `resolved`), `severity`, `title`, `summary`, `description`, `labels`, `fingerprint`.
Everything is bounded and secrets in it are redacted. Limits: 256 KiB per request, 200 alerts, 120 requests per
minute per source.

## Incidents

- **Grouping.** One live incident per source and fingerprint (the source's own fingerprint, or a hash of the name and
  labels). A repeated `firing` only counts an occurrence; it starts nothing new. `resolved` resolves it. An alert that
  comes back after resolution opens a new incident.
- **States**: `open` → `in_progress` (an agent run starts, or someone takes it) → `resolved` (by the source, or by
  you).
- **Agents.** A new incident fires `alert:<name>`: every armed agent whose trigger matches (glob, `alert:Disk*`) gets a
  run. The alert reaches the model as data, in a marked block, never as instructions. A run started by an alert
  **never lifts a write**: not through its rules, not through the permission mode. Every change becomes a card. Runs
  do not open a code session yet, so they do not reach your hosts: the agent prepares the steps and a session runs
  them, with the `prod` floor of [ops.md](ops.md). The runbook is the agent's mission, or a skill it names (the
  `ops` [pack](packs.md) proposes an `alert-triage` agent and runbook skills for a full disk, a service down and an expiring certificate).
- **Timeline.** Each incident keeps what happened: opened, each agent run and its report, each card and its outcome,
  taken, resolved. A run is an ordinary thread you can open.

```
GET  /v1/incidents?state=open|in_progress|resolved|live
GET  /v1/incidents/{id}                     with its timeline
POST /v1/incidents/{id}/ack                 "I'm on it"
POST /v1/incidents/{id}/resolve {note?}
```

Event: `incident.updated`.

## The plan

A card is a goal, a milestone or a task (up to 6 levels). It has a title, an intent, constraints and decisions. It can
belong to your account or to a team project ([teams.md](teams.md)).

- **Context goes down.** The intent, constraints and decisions of every ancestor, from the root to the card, form a
  block (up to 6,000 characters) added to the system message of a thread that works on the card: a session you attach
  (`POST /v1/plan/cards/{id}/threads`) or a run of an agent listed on the card. It is read again at each model call: a
  changed constraint applies from the next call.
- **Progress goes up.** Only a leaf has a status you set (`todo`, `in_progress`, `blocked`, `done`). A card with
  children computes it: all done gives `done`, one blocked gives `blocked`, one started gives `in_progress`.
- **Splitting a card.** The model can only *propose* children (`plan_propose_split`): a card with "Create these cards"
  and "No" appears in the thread. Only a person decides, and the cards are created in their name. The model can read
  the plan (`plan_get`, `plan_brief`), never write it.

```
GET/POST           /v1/plan/cards
GET/PATCH/DELETE   /v1/plan/cards/{id}           deleting a card moves its children up one level
GET                /v1/plan/cards/{id}/context   what a thread on this card receives
POST/DELETE        /v1/plan/cards/{id}/threads[/{thread_id}]
GET                /v1/plan/proposals            POST .../{id}/accept | .../{id}/refuse
```

## The morning brief

`POST /v1/plan/brief {cron?, tz?}` creates your "morning brief" agent, armed (it writes nothing). Default: 07:30 on
working days, Europe/Zurich time. Each run reads the plan (goals, work in progress, what is blocked, next tasks, live
incidents when `operate` is on) and your calendar if the connector is set up, and posts a `brief` card in its thread
with a notification. `DELETE /v1/plan/brief` removes it.

## Limits

- Progress reads declared statuses only, not signals such as a failed run or an open incident.
- Resolving an incident does not cancel an agent run that is still going.
- The brief is delivered as a card and a notification in the app, nowhere else yet.
- Plan cards of a team project are visible only to the member who created them.
