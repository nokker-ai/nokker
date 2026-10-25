# Licences des dépendances — Nokker (base : bibliothèque d'agent amont v0.22.2, voir `NOTICE`)

> Analyse du 16.10.2026. Révision : commit amont `d923a5eb48` (v0.22.2). Les chemins cités (`Dockerfile:…`,
> `pm/lock.json`, `scripts/…`, `plugins/…`, `web/`…) sont ceux du dépôt amont.
> Objectif : vérifier qu'une distribution **Apache-2.0 open-core** (code Nokker + image Docker) ne
> transporte rien d'incompatible (GPL/AGPL/SSPL/BUSL/non commercial).

## Verdict

- **Python obligatoire (cœur, 66 paquets) et image Docker (extras `all`, `messaging`, `otlp`, `anthropic`, `bedrock`, `azure-identity`, `matrix`, `google-chat` — `Dockerfile:288-292` — 144 paquets) : aucun
  GPL/AGPL/SSPL/BUSL/NC.** Copyleft faible seulement : `python-telegram-bot` LGPL-3.0 (image, extra `telegram`),
  `certifi`/`pathspec`/`tqdm` MPL-2.0 (cœur), `mautrix` MPL-2.0 (image, extra `matrix`). Utilisés comme bibliothèques non modifiées : compatibles.
- **À traiter avant de publier l'image** :
  1. **ffmpeg GPL** — l'image embarque le build BtbN `…-linux64-gpl-9.0` (`pm/lock.json`, paquet `ffmpeg`,
     installé par `Dockerfile:212`).
     Binaire séparé appelé en sous-processus (agrégation : ne contamine pas le code Apache), mais distribuer
     l'image oblige à fournir la source correspondante GPL-3.0. **Reco : passer à la variante `-lgpl` de BtbN**
     (même éditeur) ou à l'ffmpeg Debian, et le noter dans `NOTICE`.
  2. **WhatsApp Baileys (`scripts/whatsapp-bridge/`)** : dépend de `libsignal` **GPL-3.0** (npm). Bridge Node
     séparé, non embarqué dans l'image par défaut. Le design a tranché « WhatsApp = Business Cloud API, linké
     jamais » → **retirer `scripts/whatsapp-bridge/` et `plugins/platforms/whatsapp` (mode Baileys) du produit**.
  3. **GSAP** (`web/`, `gsap` 3.15.0) : « Standard no charge license », non OSI (gratuit, y compris commercial,
     mais restrictions d'usage). Disparaît avec le remplacement de l'UI web ; sinon le retirer de notre SPA.
  4. **Extras GPL / propriétaires, jamais dans l'image, installés à la demande par `pm`** : `piper` (piper-tts
     GPL-3.0+), `silk` (pilk GPL-3.0), `kittentts` (opt-in : phonemizer-fork GPL-3.0+, espeakng-loader qui
     embarque **libespeak-ng GPL-3.0**, torch + 9 bibliothèques **NVIDIA propriétaires**), `edge-tts` LGPL-3.0,
     `num2words` LGPL. OK tant qu'on ne les **pré-installe** pas dans une image ou un bundle ; à exclure des
     bundles desktop Nokker.
  5. **`bws` (Bitwarden Secrets Manager CLI)** téléchargé à la demande par `agent/secret_sources/bitwarden.py` :
     « Bitwarden SDK License Agreement » (propriétaire, réservé aux « Compatible Applications »). Ne pas
     l'embarquer ; acceptable en téléchargement à la demande par l'utilisateur.
  6. **Catalogue de plugins** (`plugin-catalog/*.yaml`, 615 entrées, rien d'embarqué) : au moins
     `limbic` est **PolyForm Noncommercial 1.0.0** (`plugin-catalog/limbic.yaml:4`) et
     `hermes-video-editor` référence du GPL. Le catalogue Nokker doit filtrer par licence.
- **npm** (front, TUI, desktop) : 1 422 paquets dans `package-lock.json` racine, 904 hors dev — MIT/ISC/BSD/Apache
  à > 95 % ; copyleft faible MPL-2.0 (`lightningcss`, `@novnc/novnc` desktop, `dompurify` en double licence) ;
  aucun GPL/AGPL. `website/` (docs Docusaurus, non distribué avec le produit) : `elkjs` EPL-2.0.

## Méthode

- Python : fermeture des dépendances calculée depuis `uv.lock` (marqueurs évalués pour Linux x86_64,
  Python 3.14) pour trois périmètres : *cœur* (`[project].dependencies`), *image* (cœur + les extras que le `Dockerfile:288-292` installe : `all`, `messaging`, `otlp`,
  `anthropic`, `bedrock`, `azure-identity`, `matrix`, `google-chat`), *complet* (tous les extras, 302 paquets). Licence lue sur l'API JSON PyPI à la version
  verrouillée (`license_expression`, puis `license`, puis classifiers) ; les 15 inconnus résolus par le
  `METADATA` de la wheel (PEP 658) ou l'API licence GitHub du dépôt source.
- npm : champ `license` des lockfiles v3 (`package-lock.json` racine = workspaces `apps/*`, `ui-tui`,
  `ui-tui/packages/*`, `web`, `tests-js` ; plus `scripts/whatsapp-bridge/`, `plugins/platforms/photon/sidecar/`,
  `website/`). Pas d'installation ni de `license-checker` : le lockfile porte déjà la licence de chaque paquet.
- Binaires : `Dockerfile` + `pm/lock.json` (le gestionnaire `pm` de la bibliothèque amont télécharge des binaires épinglés
  par sha256) ; licence du projet source via GitHub.
- Mesure faite dans un venv jetable, sur une machine de test.

## Python — décompte par licence

| Périmètre | Paquets | Répartition (normalisée) |
|---|---|---|
| Cœur | 66 | MIT (dont MIT-0, MIT-CMU) 33 · BSD 17 · Apache-2.0 7 · double licence permissive (Apache/BSD/MIT) 4 · MPL-2.0 3 (`certifi`, `pathspec`, `tqdm` MPL+MIT) · ISC 1 · PSF 1 |
| Image Docker | 144 | MIT 59 · Apache-2.0 42 · BSD 27 · double licence permissive 6 · PSF 4 · MPL-2.0 4 (+ `mautrix`) · ISC 1 · **LGPL-3.0 1** (`python-telegram-bot`) |
| Tous extras | 302 | + GPL-3.0 : `piper-tts`, `pilk`, `phonemizer-fork` · LGPL : `edge-tts`, `num2words` · NVIDIA propriétaire : 13 paquets `nvidia-*`/`cuda-toolkit` (via `torch`, extra `kittentts`) |

### Python — paquets signalés (tous périmètres)

| Paquet | Version | Licence | Périmètre | Extra(s) |
|---|---|---|---|---|
| python-telegram-bot | 22.8 | LGPL-3.0-only | image | messaging, telegram |
| certifi | 2026.5.20 | MPL-2.0 | cœur | — |
| pathspec | 1.1.1 | MPL-2.0 | cœur | — |
| tqdm | 4.70.0 | MPL-2.0 AND MIT | cœur | — |
| mautrix | 0.21.1 | MPL-2.0 | image | matrix |
| edge-tts | 7.2.7 | LGPL-3.0 | optionnel | edge-tts |
| num2words | 0.5.14 | LGPL | optionnel | kittentts |
| piper-tts | 1.8.0 | **GPL-3.0-or-later** | optionnel | piper |
| pilk | 0.2.4 | **GPL-3.0** | optionnel | silk |
| phonemizer-fork | 3.3.2 | **GPL-3.0-or-later** | optionnel (opt-in) | kittentts |
| espeakng-loader | 0.2.4 | MIT, embarque **libespeak-ng GPL-3.0** | optionnel (opt-in) | kittentts |
| nvidia-* (13), cuda-toolkit | 13.x | **NVIDIA EULA propriétaire** | optionnel (opt-in) | kittentts (via torch) |
| torch | 2.14.0 | BSD-3 + composants Apache/BSL/MIT | optionnel (opt-in) | kittentts |

### Python — liste complète du périmètre image (144 paquets)

| Paquet | Version | Licence | Périmètre |
|---|---|---|---|
| agent-client-protocol | 0.9.0 | Apache-2.0 (dépôt GitHub) | image (all) |
| aiohappyeyeballs | 2.7.1 | Python Software Foundation License | image (all, matrix, messaging) |
| aiohttp | 3.14.3 | Apache-2.0 AND MIT | image (all, matrix, messaging) |
| aiohttp-socks | 0.11.0 | Apache Software License | image (matrix) |
| aiosignal | 1.4.0 | Apache Software License | image (all, matrix, messaging) |
| aiosqlite | 0.22.1 | MIT License | image (matrix) |
| annotated-doc | 0.0.5 | MIT | cœur |
| annotated-types | 0.8.0 | MIT | cœur |
| anthropic | 0.87.0 | MIT License | image (anthropic) |
| anyio | 4.14.2 | MIT | cœur |
| asyncpg | 0.31.0 | Apache-2.0 | image (matrix) |
| attrs | 26.1.0 | MIT | image (all, matrix, messaging) |
| audioop-lts | 0.2.2 | PSF-2.0 | image (messaging) |
| azure-core | 1.41.0 | MIT (METADATA) | image (azure-identity) |
| azure-identity | 1.25.3 | MIT (METADATA) | image (azure-identity) |
| base58 | 2.1.1 | MIT License | image (matrix) |
| boto3 | 1.42.89 | Apache-2.0 | image (bedrock) |
| botocore | 1.42.97 | Apache-2.0 | image (bedrock) |
| brotlicffi | 1.2.0.2 | MIT | image (messaging) |
| browser-harness | 0.1.13 | MIT | cœur |
| cdp-use | 1.4.5 | MIT License | cœur |
| certifi | 2026.5.20 | Mozilla Public License 2.0 (MPL 2.0) | cœur |
| cffi | 2.1.1 | MIT-0 | cœur |
| charset-normalizer | 3.5.1 | MIT | cœur |
| click | 8.4.2 | BSD-3-Clause | cœur |
| croniter | 6.0.0 | MIT License | cœur |
| cryptography | 50.0.1 | Apache-2.0 OR BSD-3-Clause | cœur |
| davey | 0.1.6 | MIT License | image (messaging) |
| defusedxml | 0.7.1 | Python Software Foundation License | image (all) |
| discord-py | 2.7.1 | MIT License | image (messaging) |
| distro | 1.9.0 | Apache Software License | cœur |
| docstring-parser | 0.18.0 | MIT License | image (anthropic) |
| fastapi | 0.133.1 | MIT | cœur |
| fetch-use | 0.4.0 | MIT License | cœur |
| fire | 0.7.1 | Apache-2.0 | cœur |
| firecrawl-anydoc | 0.2.4 | MIT | cœur |
| frozenlist | 1.8.0 | Apache-2.0 | image (all, matrix, messaging) |
| google-api-core | 2.34.0 | Apache Software License | image (all, google-chat) |
| google-api-python-client | 2.194.0 | Apache Software License | image (all) |
| google-auth | 2.55.1 | Apache Software License | image (all, google-chat) |
| google-auth-httplib2 | 0.3.1 | Apache Software License | image (all) |
| google-auth-oauthlib | 1.3.1 | Apache Software License | image (all) |
| google-cloud-pubsub | 2.39.2 | Apache Software License | image (google-chat) |
| googleapis-common-protos | 1.75.1 | Apache Software License | image (all, google-chat, otlp) |
| grpc-google-iam-v1 | 0.14.5 | Apache Software License | image (google-chat) |
| grpcio | 1.83.0 | Apache-2.0 | image (google-chat) |
| grpcio-status | 1.83.0 | Apache-2.0 | image (google-chat) |
| h11 | 0.16.0 | MIT License | cœur |
| httpcore | 1.0.9 | BSD-3-Clause | cœur |
| httpcore2 | 2.12.0 | BSD-3-Clause | image (all) |
| httplib2 | 0.32.0 | MIT License | image (all) |
| httptools | 0.8.0 | MIT | cœur |
| httpx | 0.28.1 | BSD License | cœur |
| httpx2 | 2.12.0 | BSD-3-Clause | image (all) |
| idna | 3.19 | BSD-3-Clause | cœur |
| importlib-metadata | 8.7.1 | Apache-2.0 | image (all, google-chat, otlp) |
| jinja2 | 3.1.6 | BSD License | cœur |
| jiter | 0.16.0 | MIT | cœur |
| jmespath | 1.1.0 | MIT License | image (bedrock) |
| jsonschema | 4.26.0 | MIT | image (all) |
| jsonschema-specifications | 2025.9.1 | MIT | image (all) |
| markdown | 3.10.2 | BSD-3-Clause | cœur |
| markdown-it-py | 4.2.0 | MIT License | cœur |
| markupsafe | 3.0.3 | BSD-3-Clause | cœur |
| mautrix | 0.21.1 | Mozilla Public License 2.0 (MPL 2.0) | image (matrix) |
| mcp | 2.0.0 | MIT License | image (all) |
| mcp-types | 2.0.0 | MIT License | image (all) |
| mdurl | 0.1.2 | MIT License | cœur |
| msal | 1.38.0 | MIT License | image (azure-identity) |
| msal-extensions | 1.3.1 | MIT License | image (azure-identity) |
| multidict | 6.7.1 | Apache License 2.0 | image (all, matrix, messaging) |
| nemo-relay | 0.10.0 | Apache-2.0 | cœur |
| oauthlib | 3.3.1 | BSD-3-Clause | image (all) |
| openai | 2.24.0 | Apache Software License | cœur |
| opentelemetry-api | 1.39.1 | Apache-2.0 | image (all, google-chat, otlp) |
| opentelemetry-exporter-otlp-proto-common | 1.39.1 | Apache-2.0 | image (otlp) |
| opentelemetry-exporter-otlp-proto-http | 1.39.1 | Apache-2.0 | image (otlp) |
| opentelemetry-proto | 1.39.1 | Apache-2.0 | image (otlp) |
| opentelemetry-sdk | 1.39.1 | Apache-2.0 | image (google-chat, otlp) |
| opentelemetry-semantic-conventions | 0.60b1 | Apache-2.0 | image (google-chat, otlp) |
| packaging | 26.0 | Apache-2.0 OR BSD-2-Clause | cœur |
| pathspec | 1.1.1 | Mozilla Public License 2.0 (MPL 2.0) | cœur |
| pillow | 12.3.0 | MIT-CMU | cœur |
| pillow-heif | 1.6.0 | BSD-3-Clause | cœur |
| prompt-toolkit | 3.0.52 | BSD License | cœur |
| propcache | 0.5.2 | Apache Software License | image (all, matrix, messaging) |
| proto-plus | 1.28.3 | Apache Software License | image (all, google-chat) |
| protobuf | 6.33.6 | 3-Clause BSD License | image (all, google-chat, otlp) |
| psutil | 7.2.2 | BSD-3-Clause | cœur |
| ptyprocess | 0.7.0 | ISC License (ISCL) | cœur |
| pyasn1 | 0.6.4 | BSD-2-Clause | image (all, google-chat) |
| pyasn1-modules | 0.4.2 | BSD License | image (all, google-chat) |
| pycparser | 3.0 | BSD-3-Clause | cœur |
| pycryptodome | 3.23.0 | BSD License | image (matrix) |
| pydantic | 2.13.4 | MIT | cœur |
| pydantic-core | 2.46.4 | MIT | cœur |
| pygments | 2.21.0 | BSD-2-Clause | cœur |
| pyjwt | 2.13.0 | MIT | cœur |
| pynacl | 1.6.2 | Apache Software License | image (messaging) |
| pyparsing | 3.3.2 | MIT | image (all) |
| pypng | 0.20220715.0 | MIT License | image (messaging) |
| python-dateutil | 2.9.0.post0 | Apache Software License / BSD License | cœur |
| python-dotenv | 1.2.2 | BSD-3-Clause | cœur |
| python-multipart | 0.0.32 | Apache-2.0 | cœur |
| python-olm | 3.2.16 | Apache Software License | image (matrix) |
| python-socks | 2.8.2 | Apache Software License | image (matrix) |
| python-telegram-bot | 22.8 | LGPL-3.0-only | image (messaging) |
| pytz | 2026.3.post1 | MIT License | cœur |
| qrcode | 7.4.2 | BSD License | image (messaging) |
| referencing | 0.37.0 | MIT | image (all) |
| requests | 2.33.0 | Apache Software License | cœur |
| requests-oauthlib | 2.0.0 | BSD License | image (all) |
| resvg-py | 0.4.0 | MIT (dépôt GitHub) | cœur |
| rich | 14.3.3 | MIT License | cœur |
| rpds-py | 2026.6.3 | MIT | image (all) |
| ruamel-yaml | 0.18.16 | MIT License | cœur |
| s3transfer | 0.16.1 | Apache Software License | image (bedrock) |
| six | 1.17.0 | MIT License | cœur |
| slack-bolt | 1.30.0 | MIT License | image (messaging) |
| slack-sdk | 3.44.1 | MIT License | image (messaging) |
| sniffio | 1.3.1 | Apache Software License / MIT License | cœur |
| snowballstemmer | 3.1.1 | BSD-3-Clause | cœur |
| socksio | 1.0.0 | MIT License | cœur |
| sse-starlette | 3.4.8 | BSD-3-Clause | image (all) |
| starlette | 1.3.1 | BSD-3-Clause | cœur |
| tenacity | 9.1.4 | Apache Software License | cœur |
| termcolor | 3.3.0 | MIT | cœur |
| tomli-w | 1.2.0 | MIT License | cœur |
| tornado | 6.5.8 | Apache Software License | image (messaging) |
| tqdm | 4.70.0 | MPL-2.0 AND MIT | cœur |
| truststore | 0.10.4 | MIT | cœur |
| typing-extensions | 4.16.0 | PSF-2.0 | cœur |
| typing-inspection | 0.4.4 | MIT | cœur |
| unpaddedbase64 | 2.1.0 | Apache Software License | image (matrix) |
| uritemplate | 4.2.0 | BSD 3-Clause OR Apache-2.0 | image (all) |
| urllib3 | 2.7.0 | MIT | cœur |
| uvicorn | 0.41.0 | BSD-3-Clause | cœur |
| uvloop | 0.22.1 | Apache Software License / MIT License | image (all) |
| watchfiles | 1.2.0 | MIT License | cœur |
| wcwidth | 0.8.2 | MIT | cœur |
| websockets | 15.0.1 | BSD License | cœur |
| yarl | 1.24.5 | Apache-2.0 | image (all, matrix, messaging) |
| youtube-transcript-api | 1.2.4 | MIT License | image (all) |
| zipp | 4.1.0 | MIT | image (all, google-chat, otlp) |

## npm — décompte par lockfile

| Lockfile | Paquets | Hors dev | Répartition (tous) | Signalés |
|---|---|---|---|---|
| `package-lock.json` (web, ui-tui, apps/desktop, apps/bootstrap-installer, hermes-ink, tests-js) | 1 422 | 904 | MIT 1 123 · ISC 139 · Apache-2.0 36 · BSD-3 29 · BSD-2 21 · BlueOak-1.0.0 19 · MPL-2.0 14 · Apache-2.0 OR MIT 17 · CC-BY-4.0 2 · Python-2.0 1 · Unlicense/0BSD/CC0/MIT-0 | `gsap` 3.15.0 (licence GSAP non OSI, `web/package.json:30`) ; MPL-2.0 : `lightningcss` (+ binaires par plateforme), `@novnc/novnc` 1.7.0 (`apps/desktop/package.json:98`) ; `dompurify` MPL-2.0 OR Apache-2.0 ; `khroma` sans champ licence (MIT dans son dépôt) ; 6 workspaces internes sans champ |
| `scripts/whatsapp-bridge/package-lock.json` (Baileys) | 166 | 166 | MIT 115 · Apache-2.0 15 · BSD-3 14 · LGPL-3.0+ 14 (`@img/sharp-libvips-*`, optionnels) · **GPL-3.0 1** | **`libsignal` 6.0.0 GPL-3.0** ; `qrcode-terminal` sans champ |
| `plugins/platforms/photon/sidecar/package-lock.json` | 110 | 110 | MIT 59 · Apache-2.0 32 · BSD 11 · ISC 5 | `@photon-ai/slack`, `@photon-ai/whatsapp-business` 0.2.0 **sans licence déclarée** (à vérifier ou retirer) |
| `website/package-lock.json` (docs, non distribué) | 1 391 | 1 390 | MIT 1 144 · ISC 75 · MIT-0 65 · Apache-2.0 37 · BSD 50 | `elkjs` 0.9.3 EPL-2.0 ; 4 sans champ (`eval`, `format`, `khroma`, `require-like`) |

Dépendances directes par workspace (hors dev) : `web` 23 (MIT 18, ISC 2, Apache 1, GSAP 1),
`ui-tui` 8 (MIT), `apps/desktop` 80 (MIT 68, Apache 5, ISC 3, MPL 1, CC-BY-4.0 1), `apps/bootstrap-installer`
20 (MIT/Apache), `ui-tui/packages/hermes-ink` 20 (MIT/ISC).

## Binaires téléchargés (Dockerfile + `pm/lock.json`)

| Binaire | Version | Source | Licence | Dans l'image ? |
|---|---|---|---|---|
| Debian | 13.4 (digest épinglé) | base `debian:13.4@sha256:e2d0…` | mixte DFSG (paquets apt : python3, gcc, git GPL-2.0, docker-cli Apache-2.0, libs Chromium…) | oui |
| SQLite | 3.53.4 (`sqlite-autoconf-3530400`) | sqlite.org, compilé dans l'étage `sqlite_build` | domaine public | oui |
| s6-overlay | 3.2.3.0 | github just-containers | ISC | oui |
| uv | 0.12.3 | github astral-sh | MIT OR Apache-2.0 | oui (outil de build) |
| Python | 3.14.7 (python-build-standalone 20260901) | github astral-sh | PSF-2.0 (+ OpenSSL Apache-2.0, etc.) | oui (bundles) |
| Node / npm | 26.7.0 / 12.0.2 | nodejs.org / npm | MIT (+ composants) / Artistic-2.0 | oui |
| Chromium (Chrome for Testing) | 145.0.7632.6 | cdn.playwright.dev | BSD-3 + tiers Chromium | oui |
| **ffmpeg** | 9.0.1 | **BtbN FFmpeg-Builds `-gpl`** (Linux/Windows), martin-riedl (macOS) | **GPL-3.0** (build GPL) | **oui** → passer en `-lgpl` |
| ripgrep | 15.2.0 | github BurntSushi | MIT OR Unlicense | oui |
| agent-browser | 0.26.0 | npm | Apache-2.0 | à la demande |
| bws | 2.0.0 | github bitwarden/sdk-sm | **Bitwarden SDK License (propriétaire)** | à la demande |
| cua-driver | 0.21.0 | github trycua | MIT | à la demande (computer-use) |
| iron-proxy | 0.39.0 | github paradigmxyz | Apache-2.0 | à la demande |
| llama.cpp (cpu/cuda/hip/metal/vulkan) | b11370 | github ggml-org | MIT (CUDA runtime : EULA NVIDIA) | à la demande |
| whisper.cpp | b5130 | github ggml-org | MIT | à la demande |
| gh | 2.97.0 | github cli | MIT | à la demande |
| git (Windows) | 2.53.0 | git-for-windows | GPL-2.0 | Windows seulement |
| dmgbuild | 1.2.5 | electron-builder-binaries | MIT | build macOS |
| Bot Screen (`HERMES_BOT_DESKTOP=1`, `Dockerfile:92`) | apt | tigervnc (GPL-2.0), xfce4 (GPL), chromium apt | GPL | **variante `-desktop` seulement** |

## Ajout P1 — service d'aperçus (`nokker/previews/`, 18.10.2026, docs/BUBBLES.md §5)

Nouvelle dépendance de cœur (`pyproject.toml [project].dependencies`) : **`trafilatura`** (extrait de page
≤ 1 200 caractères pour l'aperçu). Vérifié via l'API JSON PyPI / métadonnées de wheel (même méthode que le
reste de ce document) : **Apache-2.0**, ainsi que toute sa fermeture runtime (pas d'extra activé) —
aucun GPL/AGPL/SSPL/BUSL/NC, compatible avec la distribution open-core Apache-2.0 de Nokker.

| Paquet | Version | Licence |
|---|---|---|
| trafilatura | 2.3.1 | Apache-2.0 |
| courlan | 1.4.0 | Apache-2.0 |
| htmldate | 1.11.0 | Apache-2.0 |
| justext | 3.0.2 | BSD-2-Clause |
| lxml | 6.1.3 | BSD-3-Clause |
| urllib3 | 2.8.0 | MIT |
| charset_normalizer | 3.5.2 | MIT |
| certifi | déjà listé (cœur) | MPL-2.0 |
| babel (via courlan) | — | BSD-3-Clause |
| tld (via courlan) | — | BSD-3-Clause |
| dateparser (via htmldate) | — | BSD-3-Clause |
| python-dateutil (via htmldate) | — | Apache-2.0 OU BSD-3-Clause (double licence) |

`httpcore` (déjà transitif via `httpx`) est déclaré en dépendance directe (`nokker/previews/ssrf.py`
importe `httpcore.SyncBackend` pour la connexion pinglée SSRF) — même licence que `httpx`, BSD-3-Clause,
déjà couverte par ce document (cœur). Aucun des deux ajouts ne change le verdict ci-dessus.

## Actions recommandées (P0/P1)

1. `pm/lock.json` : ffmpeg Linux/Windows → artefacts BtbN `-lgpl` ; ajouter à `NOTICE` la liste des binaires
   tiers de l'image et leurs licences.
2. Supprimer `scripts/whatsapp-bridge/` et le mode Baileys (décision « WhatsApp Business Cloud API »).
3. Ne pas publier la variante d'image `-desktop` (Xvnc/xfce GPL) sans offre de sources ; Nokker n'en a pas
   besoin (le navigateur passe par un service séparé).
4. Exclure des bundles Nokker les extras `piper`, `silk`, `kittentts` (et `bws` pré-installé).
5. Filtrer le catalogue de plugins par licence (refuser NC/GPL par défaut, afficher la licence sinon).
6. Vérifier ou retirer `@photon-ai/*` (sidecar Photon, licence non déclarée).
7. Contrôle continu : script de cette analyse à intégrer en CI (uv.lock + lockfiles npm → table, échec sur
   GPL/AGPL/SSPL/BUSL/NC hors liste d'exceptions).
