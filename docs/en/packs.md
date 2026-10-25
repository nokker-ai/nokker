# Agent packs

A pack bundles [skills](skills.md) for one kind of work with ready-made **agent templates**. Installing a pack puts
its skills in your skills folder and *proposes* its agents as cards in the conversation. Nothing is created, armed
or run until you act on a card.

Status: available in the Lokkal install. In the web app (nokker.ai): **rolling out**, behind a server flag, with the
limits described in [Packs in the web app](#packs-in-the-web-app). The packs ship with Nokker; they contain no
account, address or key: you connect your own services and keep credentials in the [vault](secrets.md). Their agents
run on the agent engine ([agents.md](agents.md)). Nokker never downloads a third-party pack.

## The packs

| Pack | Skills | Agent templates | What you connect |
|---|---|---|---|
| `mail` | `mail-triage`, `mail-reply-drafts`, `mail-morning-brief`, `calendar-scheduling` | `morning-brief` (working days), `inbox-triage` (three times a day, saves drafts) | the mail and calendar connectors |
| `social` | `social-setup`, `social-post-planning`, `linkedin-watch` | `linkedin-watch` (comments and draft replies), `weekly-post-plan` (each post waits for its card) | a post-scheduling MCP server (Postiz, open source and self-hostable, is one possible choice) and a LinkedIn MCP server |
| `jobs` | `jobs-setup`, `job-search`, `cover-letter-drafts`, `application-tracking` | `daily-job-search` (scored shortlist), `application-followup` (follow-up drafts) | a job-offers API exposed as an MCP server, the mail connector |
| `brand` | `brand-charter`, `brand-visuals`, `brand-review` | `weekly-brand-review` (planned posts against the charter) | an image-generation MCP server; optionally the scheduler |
| `video` | `video-storyboard`, `video-shots`, `video-edit`, `video-mix` | `video-storyboard` (script and shot list) | ffmpeg in the sandbox; the pack declares `needs: {gpu: true}` |
| `mcp-builder` | `mcp-scaffold` (with runnable templates), `mcp-test`, `mcp-install` | `mcp-inventory` (servers and missing secret names) | nothing: it builds what you connect |
| `ops` | `ops-basics`, `ops-disk-full`, `ops-service-down`, `ops-cert-expiry` | `alert-triage` (on an alert: picks the runbook, runs nothing; needs alerts, **beta**, [incidents.md](incidents.md)) | your hosts in `hosts.yaml` ([ops.md](ops.md)) |

The charter of the `brand` pack lives in your project memory ([memory.md](memory.md)); the other packs read it when
it is there.

## Tools the agent has

| Tool | Effect | What it does |
|---|---|---|
| `pack_list(name?)` | READ | The packs, or one pack in detail: skills, agent templates, MCP roles with their tools, effects and secret names |
| `pack_install(name, bind?)` | DESTRUCTIVE | Installs the skills (scanned) and proposes the agents. Asks for a card. |
| `pack_remove(name)` | DESTRUCTIVE | Removes a pack in one call (below). Asks for a card. |
| `mcp_server_list()` | READ | MCP servers added from a session, the secret names they use, the names missing from the vault, the tools declared read-only |
| `mcp_server_add(name, url, token?, headers?, purpose?, read_tools?, confirm_writes?)` | DESTRUCTIVE | Adds an MCP server to the configuration. Asks for a card. |
| `mcp_server_read_tools(name, read_tools, confirm_writes?)` | DESTRUCTIVE | Declares which tools of an added server only read (below). Asks for a card. |
| `mcp_server_remove(name)` | DESTRUCTIVE | Removes a server added from a session. Asks for a card. |

## Installing

1. `pack_install` reads and checks the pack (below). An invalid pack is never offered.
2. Every skill, the manifest and every agent template go through the **skills guard** with the strictest trust
   level: only a clean verdict installs, and without the scanner nothing installs (the guard fails closed, as for
   `skill_install`).
3. The skills are copied to your skills folder. A skill of yours with the same path is never overwritten: the
   install is refused before anything is copied. Installing the same pack again updates its own skills.
4. Each agent template becomes an `agent_draft` card in the conversation, with the pack name and its needs.
   Activating a card creates the agent, with the card's `needs` (so placement only picks a machine that has them);
   scheduled agents and agents allowed to write start **paused**, and arming an agent that writes asks for a strong
   approval card ([agents.md](agents.md#arming)).

### Binding your own tool and secret names

Packs that talk to an MCP server describe it by **role** (`scheduler`, `linkedin`, `jobs`, `images`) with default
tool names. If your server names its tools differently, or you named a secret differently, bind them at install:

```
pack_install {name: "social", bind: {"scheduler.schedule_post": "create_post",
                                     "secret:SCHEDULER_API_KEY": "MY_SCHEDULER_KEY"}}
```

Binding a secret to an empty name drops it from the proposed agents (for a server that needs no key).

## Removing a pack

`pack_remove {name: "mail"}` undoes an install in one call:

- the skills this pack copied are removed (a skill of yours in the same folder stays);
- the agent cards of the pack that you have not activated are closed;
- agents you **already created** from the pack are never disarmed or deleted silently. They are listed in the result
  and a `pack_agents` card offers **Disarm**, **Delete** or **Keep** for all of them; only your click acts, and only on
  agents that still exist and still come from that pack. Each agent created from a pack card remembers where it came
  from (`origin: {pack, template}` in [agents.md](agents.md)), which is how the removal finds it;
- effects the pack raised for MCP tools stay raised: removing a pack never lowers an effect (an agent you kept may
  still call those tools).

Removing again, or removing a pack that is not installed, does nothing and says so (`already_removed: true`). From the
machine, `POST /v1/local/packs/{name}/remove {thread_id?}` (local token) does the same removal and posts the same card
in that thread; `GET /v1/local/packs` lists the packs.

## What every pack guarantees

Checked when a pack is loaded, and by the test suite:

- **Every tool is declared.** An agent template lists all the tools it may call; that list becomes the agent's
  allow-list for its runs.
- **Prudent auto-approval.** `auto_approve` only lifts reads, plus `mail_draft` (a draft stays in your mailbox). It
  never lifts an external send, a publication, a payment, a command, a host change or a configuration change, and
  an agent started by an event lifts no write at all.
- **No lowered effect.** The effect kept for a tool is the stricter of the core's and the pack's. Effects that a
  pack declares for MCP tools are applied at the next start of the core, and can only make the table stricter: a
  scheduling tool declared `send_external` asks for a card in every mode.
- **Credentials by name.** Packs and templates name secrets (`SCHEDULER_API_KEY`); they never hold a value. You
  set values yourself with `secret set NAME` ([secrets.md](secrets.md#storing-a-secret)).
- **Prod floor.** Changes on a `prod` host ask for a strong card, which no agent rule lifts.
- **Neutral content.** No account, address, private path or key ships in a pack.

## Adding an MCP server

The `mcp-builder` pack ends with `mcp_server_add`. The server is written to `mcp-servers.yaml` in the state folder
(mode 0600) and read when the core starts:

```
mcp_server_add {name: "tracker", url: "https://mcp.example.com/mcp", token: "secret:TRACKER_TOKEN",
                purpose: "issue tracker"}
```

- `token` must be a `secret:NAME` reference; a header value is a reference or a short label that does not look like
  a key. A pasted value is refused and never written.
- The address is `https://`, or `http://` to the loopback or a local service name without a dot. Credentials in the
  URL are refused.
- A server already named in `config.yaml` wins over one added from a session.
- Every tool of an added server is treated as a write (it asks) unless a pack declares a stricter effect, or you
  declare it read-only (below).

### Declaring read tools without a pack

If a server has tools that only read (list, search, fetch), name them so they stop asking:

```
mcp_server_add {name: "tracker", url: "https://mcp.example.com/mcp", token: "secret:TRACKER_TOKEN",
                read_tools: ["list_issues", "get_issue"]}
mcp_server_read_tools {name: "tracker", read_tools: ["list_issues", "get_issue", "search_issues"]}
```

The same list can be set from the machine with `PUT /v1/local/mcp-servers/{name}/read-tools {read_tools: [...]}`
(local token; `GET /v1/local/mcp-servers` lists the servers). An empty list makes every tool a write again. It
applies at the next start of the core, and:

- by default nothing changes: every tool stays a write;
- a declaration only lifts a tool the core knows nothing about. A tool the core, your `config.yaml` or a pack already
  classifies keeps that effect (a declaration never lowers one);
- a tool whose **name** reads like a write (send, delete, write, create, update, post, push, rm, remove, move,
  schedule, pay…) is refused unless you also name it in `confirm_writes`; the call then asks for a **strong card**
  that no mode, no `allow` rule and no written go-ahead lifts. The local route never accepts such a name;
- a tool whose **description**, as the server gives it, announces a write stays a write unless its name was
  confirmed that way.

## Writing a pack

```
nokker/packs/<name>/
  pack.yaml                         name, version, title, description, tools, connectors, mcp roles, needs
  skills/<family>/<skill>/SKILL.md  same format as any skill (description 60 characters or fewer)
  agents/<id>.yaml                  id, name, role, instructions, trigger, tools, auto_approve, secrets, budget, needs
```

An MCP role declares its purpose, examples, secret names, and each tool with a default name and an effect
(`read`, `write`, `send_external`, `spend`, `destructive`). A template refers to a role's tool as
`<role>.<tool>`. A budget is required.

## Packs in the web app

Status: **rolling out** with the nokker.ai launch (server flag `packs_web`, off by default).

From the web app, an account installs a pack into **its own scope**. The content is the one shipped with Nokker,
passed through the same skills guard (fails closed without the scanner). What can live on the web is installed, the
rest is left for a machine:

| Part of a pack | On the web |
|---|---|
| Skills that only need text and the web | installed for the account; `skill_list` / `skill_view` read them |
| Skills marked `locality: local` (files, terminal, ssh hosts, added MCP servers, project memory, ffmpeg) | never |
| Agent templates whose every tool exists on the web (skills, web search, the account's mail and calendar connectors) | proposed as cards, like in Lokkal |
| Agent templates that call an MCP role, the sandbox or your hosts, or need a GPU | never |

So `mail` installs entirely, `social`, `jobs`, `brand`, `video` and `ops` install in part, and `mcp-builder` does not
install on the web (409 `machine_only`). A partial install says so (`partial: true`, `machine: {skills, agents, linked,
handoff, next}`). If one of your linked machines declares a resource the rest needs (an MCP server with the role's
name, a host), the install can propose to hand the rest over to that machine (`handoff: true`: a
hand-over card, and nothing runs before you accept; the machine then runs `pack_install` with its own cards). Without a linked machine, the result tells you to install Lokkal and run `pack_install` there.

Nothing is copied: the account keeps the names and a fingerprint of each skill taken when it was scanned. If the
shipped content changes later (a Nokker update), the skill is hidden until you install the pack again, which scans it
again. Removing a pack on the web works as in Lokkal (cards for the agents you created). One account never sees,
installs into or removes from another account.

The web app talks to `/v1/packs` (list, detail, `POST /v1/packs/{name}/install`, `POST /v1/packs/{name}/propose`,
`DELETE /v1/packs/{name}`).

## Not there yet

- Agent runs do not open a code session yet: templates that need the sandbox or your hosts (`video`, `ops`)
  prepare the work and a session does it. Placing a run on a machine with a GPU (`needs.gpu`) is part of the
  multi-machine placement: **rolling out** ([agents.md](agents.md#several-machines)). The card's `needs` reach the
  agent when you activate it.
- Packs in the web app: **rolling out** (above). MCP servers are never added from the web: `mcp_server_add` stays a
  local-edition tool.
- The web install cannot bind MCP tool names (no MCP part runs there); secret names can be bound.
