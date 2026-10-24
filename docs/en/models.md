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

The model chosen in onboarding serves both of Lokkal's tiers (the conversational one and the deep one). Routing a turn
across several different models, and a reviewer model checking the work, are **planned** for Lokkal: today the install
runs one model for everything, and the reviewer step is off.

## Memory and embeddings

Memory runs on your machine, in the install's database, with a small embedding service that downloads its model on first
start (about 330 MB, then works offline). Nothing about your memory leaves the machine unless you opt in to the
subscriber mirror, which is **planned** and not part of the early-access release.
