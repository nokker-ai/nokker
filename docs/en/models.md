# Models

Lokkal is model-agnostic. The model that answers is chosen in the local onboarding page (`http://127.0.0.1:7420`) and
can be changed there at any time, without a restart. There are three ways to get a model.

## 1. Nokker Router (default path)

Link the install to your nokker.ai account. The link uses a device flow: Lokkal shows a short code and a
verification address, you approve it in your account, and the install receives an inference key limited to inference.

- The key is stored in the **local vault**: an encrypted file in `~/.nokker/state`, encrypted with a key generated at
  install (`NOKKER_LOCAL_SECRET_KEY` in `~/.nokker/.env`). Without that key the vault refuses to write: a key is never
  stored in clear. Keys never come back out through the API.
- The local API routes behind this are `POST /v1/local/link/start`, `GET /v1/local/link/status` and
  `DELETE /v1/local/link` (unlink: the key is revoked on a best-effort basis and removed locally either way).
- Which models the Router serves, and the quotas of your plan, are shown in your account.

Linking also brings your account's online connectors (mail, calendar) into Lokkal sessions as **relayed connectors**:
the call goes through the link and runs on nokker.ai, which keeps the password; the machine never receives it.

- Reading (search, read a message, list events) needs no card.
- Writing inside your account (a draft, moving a message, an event without guests) asks for a card on every call,
  whatever the permission mode and your `allow` rules.
- Sending to someone else (a mail, an invitation to guests) and deleting (including moving a message to the trash)
  need a **strong card signed on your phone**, which nokker.ai checks itself before it runs the call
  ([security.md](security.md)).
- Bulk sending, spending and secrets are never relayed.

nokker.ai decides the class of each tool, not the machine; a tool it does not know is treated as a send. Each call is
logged on both sides, without its arguments or result. Flag `local.relayed_connectors` (on in the install).
**Rolling out** with the same train.

Status: the local side is in the install. The account side (link approval page, inference relay) ships on nokker.ai
with the early-access release train: **rolling out**. Until then the link answers with a clear error and the page
offers "my own key or a local model".

## 2. Your own endpoint (bring your own key)

Any OpenAI-compatible endpoint: a base URL ending in `/v1`, an API key, a model name. In onboarding, "Test" calls
`GET <base_url>/models` and lists what answers, so you see it works before saving. The key goes to the local vault.

The endpoint can be a hosted provider or a server you run (llama.cpp's server, vLLM, any gateway that speaks the same
protocol).

## 3. A model on your machine

- **Ollama** is detected automatically, on `host.docker.internal:11434` or `127.0.0.1:11434`, and its models are
  listed. Choosing one sets the default to `local:<url>|<model>`, served through `<url>/v1`.
- **llama.cpp, vLLM and others** work the same way as a custom endpoint (case 2). There is no automatic detection for
  them.
- An optional compose profile that starts a local inference runtime for you is **planned**.

Coding quality depends on the model you pick. Small local models are fine for trying Lokkal; the agent loop, sandbox and
approvals do not depend on the model.

## What the default model drives

Lokkal routes each turn to one of three tiers:

- **small** (`tiers.small`): greetings and thanks on their own ("thanks!", "hello");
- **conversational** (`tiers.ship`): questions, explanations, writing, actions on your tools;
- **deep** (`tiers.deep`): writing, fixing or testing code.

The tier is picked from the message by fixed rules, once per turn. A short reply such as "ok", "yes" or "go ahead" is
never small talk: it may be an approval, so it never goes to the small tier. A conversational turn that starts writing
files moves to the deep tier for the rest of the turn. If a tier is not configured, the turn goes to the nearest one
that is, without an error.

**Reviewer.** Before a coding turn says it is done, a reviewer model reads what the turn actually wrote (the diff sent
to the file tools, plus the output of the last commands) and answers "OK" or a short list of problems. With problems,
the writing model gets one chance to fix them (`judge_review_rounds`). The reviewer advises, it does not block: if it
fails or times out, the answer goes out as written.

**Advice on strong approvals.** A strong approval card (an action that is hard to undo, such as a destructive command)
carries a short advice from the reviewer and a risk level: `advice` and `advice_risk` (`low`, `medium` or `high`) in
`GET /v1/approvals/{id}`.

Settings in the core config: `route.policy: v1` (three tiers), `tiers.small`, `judge` (the reviewer model; without it
the conversational model reviews), `judge_review`, `judge_advice`. All are off in the core's code defaults and on in the
Lokkal install.

Status:

- routing, the reviewer and the card advice are in the install, on by default;
- on the Router, the install uses Qwen3.6-35B-A3B (EU) for the small tier, Qwen3.5-397B-A17B for the conversational
  and deep tiers, and Qwen3.5-397B-A17B with reasoning off as the reviewer: it caught the most planted bugs and gave
  the best risk levels on strong cards in our measurement. The reviewer is the same model as the writer; a reviewer
  from another model family is an option you can set with `judge`;
- with your own endpoint or a local model, everything runs on that one model (the small tier folds into the
  conversational one);
- the advice shown on approvals from your phone ships with phone approvals: **rolling out**.

## Images in a request

A message can carry up to **4 images** (png, jpeg, webp or gif, **5 MB each**). The type is checked from the file's
bytes, not from its name. Images are stored on your machine, under `~/.nokker/state/images`, once per content hash;
the conversation keeps only a reference. Deleting a session deletes its images.

Whether the model reads them depends on the model:

- the default Router model reads images;
- a model that cannot read images never receives them: it gets a short note saying an image was attached but not
  read, and the client tells you so (event `attachment.skipped`, reason `no_vision`);
- your own endpoint and local models are treated as text-only for now.

Only the most recent images of a conversation are sent in full (the current message's, then the latest ones, up to 4);
older ones are replaced by a one-line mention, like long tool results.

For API clients: send OpenAI-style content parts (`{"type": "image_url", "image_url": {"url":
"data:image/png;base64,..."}}`) on `/v1/chat/completions`, or an `attachments` list next to `content`: `{"data":
"<base64>"}`, or `{"path": "<file>"}` for a file inside the session folder. Remote `http(s)` image addresses are refused.
A refused image answers `400` with `{"error": {"message": ...}}`.

## What a turn costs

Every turn reports its tokens per model, and its cost when the model is in the core's price table (see
[cli.md](cli.md#cost-per-turn)). With your own endpoint or a local model, a model the table does not know shows tokens
only: Lokkal does not guess what your provider charges. Qwen3.5-397B-A17B on OVH AI Endpoints is in the table, in euros.

## Memory and embeddings

Memory runs on your machine, in the install's database, with a small embedding service that downloads its model on first
start (about 330 MB, then works offline). Nothing about your memory leaves the machine unless you turn on the
subscriber mirror (`local.mirror`, off in the install, **rolling out**), which copies your threads and your `user`
notes, never your project notes by default and never your work folder.
