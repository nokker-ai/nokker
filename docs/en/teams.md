# Teams: shared projects

A project gathers what several people need to work on the same code: the repositories, the permission rules, the skills
to install, the names of the secrets, a budget, and (if you choose) the project memory. Members are nokker.ai
accounts.

Status: **beta**, off by default. It runs on nokker.ai (flag `team`) and needs an install linked to an account (see
[models.md](models.md)); a linked machine applies the project rules only with `local.team: true` in the core
configuration (off in the install).

## Roles

| Role | Can |
|---|---|
| `viewer` | Read everything: project, members, skill catalogue, rules, secret names, mirrored notes. Write nothing. |
| `member` | Also push project notes, attach **their own** agents to the project, leave the project. |
| `maintainer` | Also edit the catalogue, the rules, the secret names, the repositories, the budget, the name; review notes in quarantine. |
| `owner` | Also invite, change roles, remove a member, turn the mirror on or off, delete the project. One owner: the creator. |

Rights are checked on every request. To someone who is not a member, a project does not exist: every route answers
404, exactly as for an unknown project. A member whose role is too low gets 403. The database enforces the same
separation (row-level security).

## Inviting

Invite by the e-mail address of a nokker.ai account (`POST /v1/projects/{id}/invitations {email, role}`). The answer
is the same whether the address has an account or not. No e-mail is sent: the invited person sees the invitation in
their account and accepts or declines it. An invitation lasts 14 days; a project has at most 50 members.

## What is shared, and what is not

| Shared | Not shared |
|---|---|
| The list of repositories of the project (`host/path` keys) | Your code. Each member has their own clone. |
| **Names** of secrets, with a description | Secret **values**. A request that carries a value is refused (400) before anything is stored. Each member puts the value in their own [vault](secrets.md); agents and MCP servers refer to it as `secret:NAME`. |
| Permission rules that **restrict**: a stricter mode (`plan`, `default`, `accept_edits`), `ask`, `deny` | `allow` rules and `auto` mode: refused (400), a project can only restrict. Your own `allow` rules stay yours, outside what the project forbids. |
| A skill catalogue: **sources** (`github:…`, an https URL, `official/…`) | Skill content. Each member installs on their own machine, scanned and behind their own card ([skills.md](skills.md)). |
| Learned project notes (`rule`, `feedback`, `fact`), when the owner turns the mirror on and for subscribed accounts | Sections of instruction files (`AGENTS.md`…: every member has the repository), your `user` notes, your threads |
| A monthly budget, stored and shown | It is not counted against spending yet |
| Agents attached to the project | In this version you only see your own agents in the project view |

On a member's machine (`local.team: true`), when a session opens in a repository that a project declares, the
project's rules are added to the repository's: the strictest mode wins, `ask` and `deny` add up. The machine reads the
list again at most every 5 minutes and keeps the last one it knew: offline, the last known rules still apply.

## Shared project memory

Project memory stays on each machine by default ([memory.md](memory.md)). When the owner sets `mirror: true`, learned
notes go through nokker.ai to the other members. If two members change the same note, the most recent wins and the
other version goes to quarantine for a maintainer to review.

Status: the nokker.ai side (storage, conflicts, quarantine) is in place; the machine does not push or pull project
notes yet: **planned**.

## API (on nokker.ai, account session)

```
GET/POST           /v1/projects
GET/PATCH/DELETE   /v1/projects/{id}
GET                /v1/projects/{id}/members        PATCH/DELETE /v1/projects/{id}/members/{account}
GET/POST           /v1/projects/{id}/invitations    DELETE /v1/projects/{id}/invitations/{iv}
GET                /v1/projects/invitations         POST .../{iv}/accept | .../{iv}/decline
GET                /v1/projects/{id}/skills         PUT/DELETE .../skills/{name}
GET/PUT            /v1/projects/{id}/rules
GET                /v1/projects/{id}/secrets        PUT/DELETE .../secrets/{NAME}
GET                /v1/projects/{id}/notes          GET .../notes/quarantine
GET                /v1/projects/{id}/agents
```

## Not there yet

- The machine pushing and pulling project notes: **planned**.
- Counting the project budget: **planned**.
- Single sign-on (OIDC, groups mapped to roles): **planned**.
- Other members' agents in the project view: **planned**.
