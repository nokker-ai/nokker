# Teams: shared projects

A project gathers what several people need to work on the same code: the repositories, the permission rules, the skills
to install, the names of the secrets, a budget, and (if you choose) the project memory. Members are nokker.ai
accounts.

Status: **beta**, off by default. It runs on nokker.ai (flag `team`) and needs an install linked to an account (see
[models.md](models.md)); a linked machine applies the project rules only with `local.team: true` in the core
configuration (off in the install).

A **household** (one family plan, shared quota and memory) is a project of its own type with its own routes: see
[household.md](household.md).

## Roles

| Role | Can |
|---|---|
| `viewer` | Read everything: project, members, skill catalogue, rules, secret names, mirrored notes. Write nothing. |
| `member` | Also push project notes, attach **their own** agents to the project, leave the project. |
| `maintainer` | Also edit the catalogue, the rules, the secret names, the repositories, the budget, the name; review notes in quarantine and restore a version; read the project journal. |
| `owner` | Also invite, change roles, remove a member, turn the mirror on or off, delete the project, disarm another member's agent. One owner: the creator. |

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
| A daily and/or monthly budget, counted against what the project spends (see below) | What members spend outside the project |
| Agents attached to the project: everyone sees their name, state, trigger, last run and owner | Their mission, secrets, rules, inputs and outputs: only their owner sees them |

On a member's machine (`local.team: true`), when a session opens in a repository that a project declares, the
project's rules are added to the repository's: the strictest mode wins, `ask` and `deny` add up. The machine reads the
list again at most every 5 minutes and keeps the last one it knew: offline, the last known rules still apply.

## Shared project memory

Project memory stays on each machine by default ([memory.md](memory.md)). Learned project notes (`rule`, `feedback`,
`fact`) are shared only when **both** choices are made:

1. the owner turns the project mirror on (`PATCH /v1/projects/{id} {mirror: true}`), for subscribed accounts;
2. each member turns the mirror on for that project **on their own machine** (`PUT /v1/local/mirror/projects/{p}
   {mirror: true}`, `p` = the project's `memory_project`). Until then the notes on that machine stay `local` and never
   leave it, whatever the project says.

The machine then pushes and pulls the project's notes with the subscriber mirror (`local.mirror` and `local.team`, both
off in the install):

- **Cursor per project.** Each machine pulls from where it stopped. A network cut, a restart or a lost answer resumes
  at the same point: nothing is lost, and a note sent twice is recognised by its fingerprint and skipped.
- **Conflicts.** Each note carries the version it was based on. If it still matches, it is a plain update. If not, the
  write **received last by nokker.ai wins** (nokker.ai's clock, never a machine's), and the version it replaces is kept
  in quarantine for a maintainer to review (`GET /v1/projects/{id}/notes/quarantine`). Nothing is deleted. A note you
  changed locally is never overwritten by a pull: it is pushed, and the rule above applies.
- **Forgetting.** A note forgotten on one machine is forgotten on the others (moved to `.forgotten`, as locally). The
  forgotten version is kept in quarantine too. Turning the mirror off on your machine does not forget anything for the
  team.
- **Rights.** `viewer` machines pull only; nokker.ai refuses (403) a push from a viewer even if the machine tries.
  A note that does not say it is `both` is refused by nokker.ai as well, as a second barrier.

- **Restoring a version.** An owner or maintainer can put a version from the quarantine back as the current note
  (`POST /v1/projects/{id}/notes/quarantine/{q}/restore`). The note that was current goes to the quarantine in its
  turn: still nothing is deleted. Machines pull the restored version on their next pass, including those where the
  note had been forgotten; a machine where the note was changed locally keeps its change and pushes it, and the rule
  above applies. Each restore is written to the project journal.

Status: **rolling out**. The machine and nokker.ai sides are built and tested together; they work once the mirror is
open on nokker.ai.

## Project budget

Status: **beta**, with the `team` flag.

An owner or maintainer sets a cap per day and/or per month (`PUT /v1/projects/{id}/budget {daily?, monthly?,
currency?}`, euros by default; days and months are calendar days and months, Swiss time). What counts is every model
call made for the project:

- turns of a session attached to a card of the project's plan ([incidents.md](incidents.md)), sub-agents included;
- runs of an agent attached to the project.

The spending is read from the same ledger as the cost of each turn ([cli.md](cli.md#cost-per-turn)); there is no
second counter. A call is counted for the project only when the person who spends (the sender of the turn, the owner
of the agent) is a `member` at least. A viewer, a former member or an outsider never spends for the project, and the
database refuses to attribute a call to a project for anyone else.

- **Cap reached**: the next turn is refused **before** any model call, with a clear answer, like the account limits.
  A run of an agent of the project is skipped without opening its thread. A turn that started under the cap finishes.
- **80 %**: the turn that crosses 80 % of a cap ends with a short notice, once.
- **Currencies**: costs keep the currency of the price list. USD and CHF convert at the price list rate; other
  conversions need a rate set by the server. Spending that cannot be converted makes the budget unverifiable, and
  turns are refused to be safe.
- **View**: `GET /v1/projects/{id}/budget` gives what was spent today and this month, the caps, the state (`ok`,
  `warning`, `over`) and when each counter resets. Owners and maintainers see one line per member (former members
  included, their spending stays in the total); other members see the total and their own line. Spending of members
  outside the project never appears.
- Setting or removing the cap is written to the project journal (`GET /v1/projects/{id}/audit`, owner and
  maintainer).

## Agents in the project

`GET /v1/projects/{id}/agents` lists the agents attached to the project by every current member. For your own agents
you get their full card. For the others: name, state, trigger (for mail and webhook triggers, only the kind), last
run (status and time) and owner; never their mission, secrets, rules, inputs or outputs. Only the owner of an agent
acts on it, with one exception: the project owner can disarm any agent of the project
(`POST /v1/projects/{id}/agents/{agent}/disarm`), and that is written to the project journal. Its owner can arm it
again as usual. An agent whose owner leaves the project is no longer listed.

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
POST               /v1/projects/{id}/notes/quarantine/{q}/restore
GET                /v1/projects/{id}/agents         POST .../agents/{agent}/disarm
GET/PUT/DELETE     /v1/projects/{id}/budget
GET                /v1/projects/{id}/audit
GET/PUT/DELETE     /v1/projects/{id}/sso                     (flag `sso`)
GET/POST           /v1/sso/connections                       GET/PATCH/DELETE /v1/sso/connections/{sc}
PUT                /v1/sso/connections/{sc}/client-secret    GET /v1/sso/connections/{sc}/audit
POST               /v1/sso/connections/{sc}/domains          POST .../domains/{domain}/verify   DELETE .../domains/{domain}
POST               /v1/auth/oidc/start | /v1/auth/oidc/callback   (no session yet)
```

## Single sign-on (OIDC)

Status: **beta**, off by default (flag `sso` on nokker.ai, which needs `team`).

A project owner can let people sign in with their organisation's identity provider (any OpenID Connect provider:
Okta, Entra ID, Google Workspace, Keycloak…) and have their **groups decide their role** in the project.

1. **Create a connection** (`POST /v1/sso/connections {name, issuer, client_id, client_secret_name}`). Register the
   connection's `redirect_uri` at your provider as an allowed redirect.
2. **Store the client secret** with `PUT /v1/sso/connections/{id}/client-secret {value}`. It goes to the server's
   vault under the name you chose. It is never stored in the database and never shown again: answers only say
   `{"name": …, "set": true}`. A request that puts a secret value anywhere else is refused (400).
3. **Prove your e-mail domains**: `POST …/domains {domain}` gives a TXT record (`_nokker-sso.example.com` =
   `nokker-sso=…`); add it to your DNS, then `POST …/domains/example.com/verify`. Only addresses of proved domains
   can sign in, and a domain belongs to one connection only. Without this proof, any provider could claim
   `alice@your-domain` and take over her account.
4. **Link the connection to a project**: `PUT /v1/projects/{id}/sso {connection, mappings: [{group, role}],
   required, max_age_s}`. Roles are `maintainer`, `member` or `viewer`. **`owner` cannot be mapped**: the owner is
   the person who created the project, and no group ever makes anyone owner.

### Signing in

People open the sign-in link (by connection, or by typing their work e-mail). Nokker uses the Authorization Code flow
with PKCE; the attempt is bound to the browser that started it (it signs the answer with its device key) and can be
used once, within 10 minutes. Nokker checks the ID token strictly: signature (RS256 or ES256 only; `none` is refused),
issuer, audience, expiry, and the nonce of this attempt. The provider must say the e-mail address is verified.

- **Account**: the first sign-in links to the nokker.ai account with the same e-mail, or creates one. Each member
  keeps their own account and their own data; joining a project never moves them into the owner's space.
- **Roles follow groups at every sign-in**: the highest role among the mapped groups. Removed from the group, the
  role is removed at the next sign-in. A member who was invited by hand is only changed when a group gives them a
  role. The owner is never changed.
- A device that signed in with SSO can use the app; it never approves a strong card.

### SSO required

With `required: true`, members reach the project's resources (rules, secret names, catalogue, notes, agents) only
with a session that comes from this connection, signed in less than `max_age_s` ago (default 12 hours). Other ways of
signing in get `403 sso_required`, and the project list shows the project's name with a "sign in with SSO" hint only.
A linked machine needs a recent SSO sign-in of its account too. The owner is not bound by it, so that a broken
configuration can always be fixed.

### Audit

`GET /v1/sso/connections/{id}/audit` lists sign-ins (accepted, and refused with the reason), members added, roles
changed or removed, and configuration changes. It never contains a token, a code or a secret.

## Not there yet

- Removing access the moment a group changes (SCIM): **planned**. Today it applies at the next sign-in, and
  `required` bounds access to `max_age_s`.
- Counting a linked machine's own sessions in a repository of the project against the project budget: **planned**.
  Today the budget counts what runs on nokker.ai (sessions attached to a plan card, agents of the project).
- A turn that starts just under the cap can go past it; two turns started at the same moment can both pass.
