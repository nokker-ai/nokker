# Gateway

> Status: **rolling out**. The pieces exist and are tested against each other; they are switched on with the
> early-access release train and are not active in a fresh install yet. Nothing here works until your install is linked
> to an account (see [models.md](models.md)).

The gateway lets the Nokker web interface reach **your** Lokkal, for people who take inference from Nokker, without
opening any port on your network.

```
browser --https--> <name>.nokker.app --> relay --(outbound websocket, opened by your Lokkal)--> your machine
```

Your Lokkal opens an **outbound** connection to the relay (`wss://relay.nokker.app/v1/tunnel`, port 443, it follows
`HTTPS_PROXY`). It reconnects with a growing delay (1 to 60 s). Only one tunnel per machine is active at a time.

## Addresses

| Address | What it serves |
|---|---|
| `<name>.nokker.app` | Control interface of your Lokkal (the local web interface, relayed) |
| `<name>.nokker.app/chat` | The chat of the local web interface, for people on the free plan who take inference from Nokker. Works while your machine is on, on your local memory. |
| `<name>--pages.nokker.app/<page>` | Pages the agent creates in a session. A different origin from the control interface, on purpose. |
| nokker.ai | The hosted chat (always on; plans with included inference under quotas) |

`<name>` is chosen when you link: 3 to 30 characters of `a-z`, `0-9` and `-`; no `--` (reserved for pages), no leading
or trailing `-`; reserved names such as `login`, `support`, `admin`, `api`, `www` and well-known brands are refused.

## Who can use it

The tunnel only exists for an account that has inference with Nokker. The relay checks this when the tunnel opens and
every 10 minutes after; if the account no longer qualifies, the token is revoked or the name changes, the tunnel is
closed. The exact eligibility rule (for example whether a positive balance is required) is not final.

The code is open source (Apache-2.0); the relay is a Nokker service, so the gate is on our side, not in the licence.

## Protections

- **Filtering before your machine.** The relay forwards `/v1/*` only with a valid Nokker-signed access token of the
  same account as the machine; otherwise it answers 401 itself and nothing reaches your machine. Anything else is
  limited to `GET` and `HEAD` of the web interface files. A local token is refused by the relay, and the core also
  refuses a local token on a relayed request.
- **The core checks again.** It re-verifies the token itself and keeps its own approval cards. Any destructive action
  asked from the web needs a **strong approval card**, signed on an enrolled device with a passkey on
  `https://app.nokker.ai` (see [Approving from your phone](#approving-from-your-phone)), never with the local token.
- **No cookies shared.** Control cookies use the `__Host-` prefix. The relay strips `Cookie` toward your machine and
  `Set-Cookie` from it.
- **Pages are sandboxed.** They are served with `Content-Security-Policy: sandbox allow-scripts allow-forms
  allow-popups` (no `allow-same-origin`: a page can read no cookie or storage), `X-Content-Type-Options: nosniff` and
  `Referrer-Policy: no-referrer`. They are **private**: you open them through a short-lived signed link. Making a page
  public is **planned** (below).
- **A web session never touches your machine directly.** A hosted session that needs something that lives on your
  machine (a repository, an ssh host, a connector set up in Lokkal) never receives it. It proposes a hand-over: a card
  in the web thread that names the machine and the task. If you accept, a Lokkal session opens on that machine with the
  task, works with that machine's permissions and cards, and only its final answer comes back to the web thread. The
  machine tells nokker.ai the **names** of its resources, never paths or contents.

## Approving from your phone

When a strong card waits on a linked machine, the machine posts it to your account and you get a link to
`app.nokker.ai/approve/…` (and a Telegram message with that link, if you paired Telegram). You sign with a passkey
that verifies you. nokker.ai only carries the signature: your machine checks it against your device's public key, the
card's challenge and the hash of the exact arguments, and uses it once. What the page shows, and who wrote it: see
[security.md](security.md#4-what-you-see-when-you-sign).

## Several machines

Each linked machine has its own tunnel and its own `<name>.nokker.app`. Machines report their load (free RAM, CPU,
GPU, sessions) every 60 s, so nokker.ai can place agent runs on the least loaded eligible machine and show one deck
for all of them: see [agents.md](agents.md#several-machines).

## Limits of the tunnel

Frames are JSON text messages of at most 1 MiB, a request body at most 50 MiB, 64 requests in flight per tunnel, 60 s to
the first response byte, and 1 h of inactivity for an open stream.

## Not yet

- Public pages (made public page by page, on purpose, with a report link) and the Public Suffix List registration of
  `nokker.app` (so two gateways cannot share cookies): **planned**. Until then pages are private and cookies are
  `__Host-`, which already isolates each name.
- Pages served from a subscriber mirror when your machine is off: **planned**.
- Sharing one gateway between several members of a company account: not in the first version.
