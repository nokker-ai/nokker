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
| `<name>--pages.nokker.app/<page>` | Pages the agent creates in a session, private. A different origin from the control interface, on purpose. |
| `<name>--pub.nokker.app/<public id>` | A page you chose to make public (below). Yet another origin, and an id that is not the page id. |
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
  asked from the web needs a **strong approval card**, signed on an enrolled device (a passkey on
  `https://app.nokker.ai`, or the Telegram Mini App) (see [Approving from your phone](#approving-from-your-phone)),
  never with the local token.
- **No cookies shared.** Every cookie the relay sets uses the `__Host-` prefix (no `Domain`, so it belongs to one
  name only); it sets no other cookie, strips `Cookie` toward your machine and `Set-Cookie` from it. Sign-in routes
  also require the exact `Origin` and `Sec-Fetch-Site: same-origin`, and the private pages cookie only counts for a
  page you open in a tab, never for a frame or a sub-request from another site.
- **Pages are sandboxed.** They are served with `Content-Security-Policy: sandbox allow-scripts allow-forms
  allow-popups` (no `allow-same-origin`: a page can read no cookie or storage), `X-Content-Type-Options: nosniff` and
  `Referrer-Policy: no-referrer`. They are **private**: you open them through a short-lived signed link. A private
  page is never served by its id alone. You can make a page public on purpose (below).
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

You can also approve in the **Telegram Mini App** (Face ID or your fingerprint, through Telegram's biometric manager):
the Telegram message has a button that opens that very card. Your machine does not hold the Telegram bot token, so it
cannot check Telegram's proof itself. **nokker.ai checks it** (the Telegram sign-in data, your enrolled device, its
signature on the exact challenge your machine created) and hands your machine a signed **attestation**: tied to that
machine, your account, that card, that challenge and that decision, valid for 120 seconds and usable once. For a card
approved this way, your machine therefore trusts nokker.ai; for a passkey it does not, because the machine verifies the
passkey itself. A card for a `prod` change, a page publication or a relayed connector always needs the passkey. Set
`local.remote_approvals_attested: false` to turn the Telegram path off: only a passkey then decides from your phone.

## Public pages

Status: **rolling out** (built and tested; it needs an account that can publish, said by nokker.ai, and the
early-access release train). Offered to subscribers and verified accounts.

- **Only on purpose, page by page.** Nothing is public by default and the model cannot publish: you choose "publish"
  on a page (`POST /v1/pages/{page_id}/publish {index?}`). This is an output to the public, so it waits for a
  **strong approval card**, signed on an enrolled device with a passkey; the local token alone cannot approve it, and
  neither can an access token through the gateway. Rejecting is always possible.
- **What you sign is what goes public.** The card shows the page, its path, its version and size; that exact version
  becomes public. Later edits stay private until you publish again (same address).
- **A separate address.** `https://<name>--pub.nokker.app/<public id>`: another origin than the control interface and
  the private pages, and a random id that is not the page id. A private link never opens the public page and the
  public address never opens a private page.
- **Strict headers, no cookies.** No cookie is read or set; nothing from the visitor (cookie, token, address) reaches
  your machine. `Content-Security-Policy: default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline';
  img-src data:; font-src data:; media-src data:; connect-src 'none'; form-action 'none'; base-uri 'none';
  frame-ancestors 'none'; sandbox allow-scripts allow-popups`: the page runs its own inline code only, loads nothing
  from elsewhere, makes no network call, submits no form and is never framed. Plus `X-Content-Type-Options: nosniff`,
  `Referrer-Policy: no-referrer`, `Cache-Control: no-store`, `X-Frame-Options: DENY`, and `X-Robots-Tag: noindex,
  nofollow` unless you ticked "let search engines index it".
- **A report link** is added by the relay at the bottom of the page (a plain link to `nokker.ai/report`, no script).
- **Rate limits**: 60 views per minute per page and per visitor address, 1200 per minute per page.
- **Served by your machine** while it is on; when it is off, by nokker.ai if you also chose to keep a copy of that page
  (below) and the copied version is the published one.
- **Unpublish** (`POST /v1/pages/{page_id}/unpublish`) is immediate and needs no card: the public id is erased, the
  address stops working for good (publishing again gives a new one) and the public copy on nokker.ai is removed at once
  (retried if nokker.ai cannot be reached).
- **A `local` page never becomes public.** Pin a page `local` (`PUT /v1/pages/{page_id}/locality {locality:
  "local"}`): it is unpublished, its copy is removed, and it can be neither copied nor published again until you unpin
  it.

## Pages when your machine is off

For subscribers, a page can be copied to nokker.ai so that it still opens when your machine is off. Status: **rolling
out** (built and tested; it works once the mirror is open on nokker.ai).

- **Only the pages you choose.** Nothing is copied by default: a page is `local` until you ask for a copy, page by
  page (`PUT /v1/local/mirror/pages/{page_id} {mirror: true}`, list with `GET /v1/local/mirror/pages`). A page that is
  not chosen never leaves your machine; nokker.ai also refuses a copy that does not say it is `both`.
- **Same address, same links, same headers.** The copy is served at `<name>--pages.nokker.app/<page_id>`, only when the
  tunnel is closed, with the same short-lived signed links (`/_auth`, then the `__Host-` cookie or the iframe URL) and
  the same headers as a page served by your machine (`sandbox` CSP and `frame-ancestors`, `nosniff`,
  `no-referrer`, `no-store`). Without a valid link nothing is served from the copy.
- **A banner.** The copy carries a banner at the top: "Copie du `<date>` UTC, machine hors ligne" (copy of `<date>`,
  machine offline), and the header `X-Nokker-Mirror-Copy: <date>`.
- **Latest version only, with a limit.** nokker.ai keeps the last version copied, up to a size limit per account
  (50 MB by default, all machines together). A page over the limit stays on your machine and shows `quota`.
- **Public pages.** A copy also serves the public address while your machine is off, if the copied version is the
  one you published; nokker.ai keeps only a fingerprint of the public id, never the id.
- **Purged.** The copy is deleted when you stop copying the page, when you delete the page (`DELETE /v1/pages/{id}`),
  when you erase the copies of your machine (`POST /v1/local/mirror/erase`), when the machine is revoked or unlinked,
  and with your account.

## Several machines

Each linked machine has its own tunnel and its own `<name>.nokker.app`. Machines report their load (free RAM, CPU,
GPU, sessions) every 60 s, so nokker.ai can place agent runs on the least loaded eligible machine and show one deck
for all of them: see [agents.md](agents.md#several-machines).

## Limits of the tunnel

Frames are JSON text messages of at most 1 MiB, a request body at most 50 MiB, 64 requests in flight per tunnel, 60 s to
the first response byte, and 1 h of inactivity for an open stream.

## Not yet

- The Public Suffix List registration of `nokker.app`, which makes every `<name>.nokker.app` a separate site for the
  browser: **planned**. Until then the code isolates each name itself (`__Host-` cookies only, exact origin on sign-in,
  private pages cookie for top-level tabs only).
- Sharing one gateway between several members of a company account: not in the first version.
