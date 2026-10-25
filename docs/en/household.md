# Household: one plan for the family

A household lets up to six people share one nokker.ai subscription: the payer invites the others, everyone keeps their
own account and their own private space, and the turns of all members count against one shared quota.

Status: **beta**, off by default. It runs on nokker.ai (flags `team` and `household`); minor profiles are behind a
second flag, `household_minors`, also off, and wait for a legal review of their terms before they are opened.

## What a household is

A household is a shared project of type `household` (see [teams.md](teams.md)): the person who creates it is the
**payer** and its owner; everyone else joins as a member. Rules:

- at most **6 members, payer included** (pending invitations count); the database refuses a seventh;
- **one household per account**: an account that already belongs to a household cannot create or join another
  (`409 already_in_household`);
- invitations go by the e-mail address of a nokker.ai account, last 14 days, and answer the same whether the address
  has an account or not;
- a household never shows up in `/v1/projects`: it has its own routes, `/v1/household*`.

## What is shared, and what is not

| Shared with the household | Private to each member |
|---|---|
| The household memory: notes (`rule`, `feedback`, `fact`) any member writes, recalled in every member's turns | Conversations, threads, bubbles, pages |
| The quota (turns per day and per week) | Connected accounts (mail, calendar), secrets, devices |
| The member list, profiles, and the **number** of turns each member used | Profile memory (what the assistant learned about *you*) |

The payer sees how many turns each member used today and this week, never what they asked. A member sees their own
line and the household total. In the database each member stays in their own tenant: no household route, and no SQL
query run as another member, reads their threads, connectors, secrets or profile memory.

Household notes follow the rules of team notes: they are redacted on arrival, the last write received wins, and the
replaced version goes to a quarantine the payer can review and restore. A note scoped to a person (`scope: user`) is
refused: profile memory never goes through the household.

## The shared quota

Every turn of a member (web, Telegram, a run of one of their agents) is tagged with the household in the usage ledger,
the same way a team project budget is counted. The household counter is the number of turns read from that ledger
(sub-agent work belongs to the turn that started it). Windows are those of the account limits: the day resets at 12:00
UTC, the week on Monday at 06:00 UTC.

| Setting | Default | Who changes it |
|---|---|---|
| Household turns per day / per week | 110 / 550 | the payer's active household plan when billing is on; otherwise configuration (`household_settings`) |
| Turns per day for one member | 40 (the payer has none unless they set one) | the payer, per member |
| Members | 6 | the payer's household plan when billing is on; otherwise configuration (6 at most) |
| Mode | `hard`: refuse beyond a cap; `soft`: count, warn once, never cut | configuration |

A turn beyond a cap is refused **before** anything is sent to the model, with a clear answer that says when the counter
resets; the conversation is kept. A member's own account limits keep only the cost safety cap.

When billing is on and the payer's subscription no longer includes a household (another plan, cancelled, unpaid beyond
the retry period), the household is **suspended**: every member is back on their own account limits, nothing is
deleted (members, notes, settings), invitations are refused (`402 household_suspended`), and the payer's view carries a
`notice` that says how to reactivate it. Subscribing to a household plan again brings it back as it was.

## Minor profiles (beta, off)

The payer can declare a member `teen` (13 to 17) or `child` (under 13), at invitation or later. They confirm they are
the member's legal guardian; the confirmation is timestamped and tied to the version (a hash) of the text they read
(`GET /v1/household/consent`). Withdrawing it suspends the profile: no turn is processed until it is renewed. For a
minor profile:

- **no training consent**, ever: refused by the API and by the database, and withdrawn when the profile is declared;
- **no outbound sending and no irreversible action** without a strong approval decided by the **guardian**, from the
  guardian's own trusted devices (passkey with user verification, or the Telegram Mini App) — never by the minor, never
  by an allow-list, a written "go" or an agent rule;
- connectors and agents limited to a **household allow-list** set by the payer (empty by default);
- an age-adapted system prompt;
- no SSO sign-in, nothing sent to the mirror, no public page;
- a minor cannot leave the household alone; the guardian removes them.

Terms for minors exist as a draft and are not active.

## Routes

All routes need an account session; errors are `{"error": {"code", "message"}}`.

```
GET    /v1/household                         household view (members, turns per member, caps; never content)
POST   /v1/household {name?}                 create (you become the payer)          DELETE /v1/household (payer)
POST   /v1/household/invitations {email, profile?, attestation?}                  (payer)
GET    /v1/household/invitations/received    POST /v1/household/invitations/{id}/accept | /decline
PUT    /v1/household/members/{account}/cap {turns_per_day: n | null}               (payer; DELETE = default)
PUT    /v1/household/members/{account}/profile {profile, attestation?}             (payer, minors flag)
POST   /v1/household/members/{account}/consent {attestation}   DELETE …/consent    (payer: renew, withdraw)
GET    /v1/household/policy                  PUT {minor_connectors, minor_agents}  (payer)
GET    /v1/household/notes?since=            PUT /v1/household/notes/{name}        DELETE /v1/household/notes/{name}
GET    /v1/household/approvals               POST …/approvals/{id}/challenge | /decision   (payer: minors' cards)
```

`GET /v1/account/usage` of a member also carries a `household` block: the shared quota and the member's own cap.

## Example

```
POST /v1/household {"name": "Home"}
POST /v1/household/invitations {"email": "alex@example.com"}
PUT  /v1/household/members/us_0123456789ab/cap {"turns_per_day": 25}
PUT  /v1/household/notes/school-pickup {"kind": "fact", "title": "School pickup",
                                         "text": "Pickup is at 16:30 on Wednesdays."}
```
