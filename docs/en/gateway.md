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
  asked from the web needs a **strong approval card**, decided on an enrolled device (phone or passkey), with the
  WebAuthn origin `https://<name>.nokker.app`.
- **No cookies shared.** Control cookies use the `__Host-` prefix. The relay strips `Cookie` toward your machine and
  `Set-Cookie` from it.
- **Pages are sandboxed.** They are served with `Content-Security-Policy: sandbox allow-scripts allow-forms
  allow-popups` (no `allow-same-origin`: a page can read no cookie or storage), `X-Content-Type-Options: nosniff` and
  `Referrer-Policy: no-referrer`. They are **private by default**: you open them through a short-lived signed link. A
  page can be made **public** page by page, on purpose, for subscribers or verified accounts, and then carries a report
  link.
- **A web session never touches your machine directly.** A hosted session that needs something local opens a Lokkal
  session through the gateway; that session has its own local gates and approvals, and the web side only receives the
  result.

## Limits of the tunnel

Frames are JSON text messages of at most 1 MiB, a request body at most 50 MiB, 64 requests in flight per tunnel, 60 s to
the first response byte, and 1 h of inactivity for an open stream.

## Not yet

- Public pages and the Public Suffix List registration of `nokker.app` (so two gateways cannot share cookies): **planned**.
  Until then pages are private and cookies are `__Host-`, which already isolates each name.
- Pages served from a subscriber mirror when your machine is off: **planned**.
- Sharing one gateway between several members of a company account: not in the first version.
