# API client de Nokker — contrat (P1, 18.10.2026)

> Fils, bulles, sessions qui se parlent, aperçus et événements `turn.*`/`bubble.*` : **`docs/BUBBLES.md`**.

> Implémenté (branche `p1/approvals`, `nokker/api/server.py`) : les routes Approbations/Appareils/Sessions/Événements
> ci-dessous, alignées avec la Mini App Telegram (`p1/miniapp`) et le chantier clients (`nokker-apps` : SDK, CLI, web,
> faux serveur). Fils, bulles et événements `turn.*`/`bubble.*` : implémentés par la branche `p1/bubbles`
> (`nokker/threads/`, `nokker/bubbles/`), contrat et précisions dans `docs/BUBBLES.md` (§8).

Une seule API pour tous les clients : CLI, web, desktop (Linux/macOS/Windows), mobile (iOS/Android, tablettes,
iPhone Duo), Mini App Telegram. Base `https://<hôte>/v1`, JSON, flux en SSE. Compatible OpenAI pour la complétion
(`/v1/chat/completions` existant ; streaming : §Streaming) ; le reste est propre à Nokker.

## Authentification et identité

- `Authorization: Bearer <jeton de session>`. Un jeton de session est émis à un **appareil enrôlé** (docs/APPROVALS.md
  §3) après une connexion prouvée : `POST /v1/devices/{id}/session-challenge` → défi, signé par la clé de l'appareil
  (signature brute IEEE P1363, ou WebAuthn pour le web — docs/APPROVALS.md §3.4/§3.6), puis `POST /v1/sessions` contre
  ce défi signé. Durée 12 h, **pas encore de renouvellement** (refaire la danse à l'expiration — limite documentée).
  Le jeton porte l'utilisateur VÉRIFIÉ (`alice`, `bruno`) : c'est ce `sender` que voient les invariants, donc un GO
  peut venir d'un client vérifié (pas du proxy de la passerelle amont, qui reste `gateway-proxy-unverified`).
- Les clés d'API statiques (`api_keys` de la config) restent pour les bancs et les ponts serveur-à-serveur.
- La Mini App Telegram n'utilise PAS cette danse : elle présente `initData` (en-tête `X-Telegram-Init-Data` sur
  toute méthode, ou champ `init_data` du corps en POST), validé et résolu en `sender` via
  `NokkerConfig.telegram_ids` — un `initData` validé vaut une identité, au même titre qu'un jeton de session.

## Ressources

| Méthode | Chemin | Rôle |
|---|---|---|
| POST | `/v1/chat/completions` | un tour (OpenAI, `stream: true` → SSE), en-tête `X-Session-Id` = fil ; images et pièces jointes du dernier message `user` : §Pièces jointes |
| GET | `/v1/events` | SSE de l'utilisateur, tous fils confondus (voir ci-dessous) |
| GET | `/v1/approvals?status=pending` | cartes en attente |
| GET | `/v1/approvals/{id}` | une carte : outil, arguments, effet, raison, avis de Nina, niveau, expiration (forme JSON exacte : docs/APPROVALS.md §5) |
| POST | `/v1/approvals/{id}/challenge` | défi à signer |
| GET · POST | `/v1/approvals/suggestions` · `…/{id}/accept` · `…/{id}/dismiss` | règles « toujours autoriser » proposées depuis l'historique des cartes (drapeau `approval_suggestions`, éteint par défaut) ; accepter écrit la règle dans les réglages du propriétaire de la machine, sinon `409 no_rule_store` (docs/APPROVALS.md §7) |
| POST | `/v1/approvals/{id}/decision` | décision (signature brute, WebAuthn, ou TOTP — docs/APPROVALS.md §3.3-§3.5), déclenche l'exécution + le tour de continuation si approuvée ; répond après l'exécution (`executed`/`failed`) si elle tient en 45 s, sinon avec la carte `approved` (l'exécution suit en fond) |
| POST | `/v1/devices` · GET `/v1/devices` · DELETE `/v1/devices/{id}` | appareils de l'utilisateur |
| POST | `/v1/devices/{id}/confirm` | confirme un appareil `pending` par TOTP (alternative à `python -m nokker devices approve <code>`) |
| POST | `/v1/devices/{id}/session-challenge` | défi de connexion (sans identité préalable — c'est lui qui en établit une) |
| POST | `/v1/sessions` | jeton de session contre ce défi signé (brut ou WebAuthn) |
| POST | `/v1/auth/email/start` | `{email, lang}` → **toujours** `200 {sent:true}` (même corps, même délai, même `429` que l'adresse ait un compte ou non) ; code à 6 chiffres envoyé par front1 seulement à un compte actif — 10 min, usage unique |
| POST | `/v1/auth/email/verify` | `{email, code, name, public_key_spki}` → `200 {device_id, user}` ; toute erreur = `401 {error:"invalid_code"}` ; 5 essais puis code brûlé. L'appareil créé se connecte (`session-challenge` → `/v1/sessions`) mais **ne décide jamais** d'une approbation : `403 {"error":{"message":"strong_device_required"}}` |
| POST | `/v1/auth/oidc/start` · `/v1/auth/oidc/callback` | SSO OIDC d'une équipe (drapeau `sso`, docs/LOKKAL.md §7.24) : `start {connection \| email, public_key_spki, name}` → `{authorize_url, state}` ; `callback {state, code, signature de "<state>.oidc"}` → `{device_id, user, projects}`. Appareil de connexion seulement (`sso:<connexion>`), comme le code e-mail |
| GET | `/v1/account` | compte `us_…` exigé → `{email, lang, created_at, status, terms: {version, accepted_at} \| null, consents: {training}}` |
| PUT | `/v1/account/consent` | `{scope: "training", granted: bool}` → `200 {scope, granted}` ; session normale (pas d'appareil fort) ; une ligne `data_consents` seulement quand le choix change ; `400 {error:"invalid"}` |
| GET | `/v1/account/usage` | compte `us_…` exigé (sinon 403) → `{tz, mode: soft\|hard, plan, today: {day, resets_at, turns: {used, cap, over, hard_cap}}, week: {starts, resets_at, turns: {used, cap, over}}}` ; heures en Europe/Zurich, jour de quota 12:00 UTC → 12:00 UTC, semaine lundi 06:00 UTC ; `cap` null = sans limite ; `over` = used > cap |
| POST | `/v1/account/export` | compte `us_…`, session normale (tout appareil) → `202 {id, status: "ready", created_at, expires_at, size, download}` : l'export est construit tout de suite et gardé **24 h** ; **une demande par 24 h** → sinon `429 {error: "export_limit", retry_at}` ; `503 {error: "export_disabled"}` si le service n'est pas configuré |
| GET | `/v1/account/exports/{id}` | même corps (`status` : `ready` \| `expired`, `download` null une fois expiré) ; `404` pour un export d'un autre compte ou locataire |
| GET | `/v1/account/exports/{id}/download` | `200 application/json` (pièce jointe `nokker-export-<id>.json`, `Cache-Control: no-store`) ; `410 {error: "export_expired"}` après 24 h. Contenu : `format: "nokker-account-export/1"`, compte (adresse, langue, dates, statut), acceptations du règlement, historique des consentements, usage, **toutes les conversations** (`thread`, tours : message, réponse, messages d'outils, canal) ; jamais de jeton, clé ou secret de connecteur |
| POST | `/v1/account/delete` | compte `us_…` → `202 {approval_id, strength: "strong", expires_at, reason}` : pose une carte d'approbation **forte** (une seule en attente : la même est rendue) ; le client la fait décider par `POST /v1/approvals/{id}/decision` (après `…/challenge`) avec une preuve d'appareil fort (Mini App + biométrie, ou passkey avec vérification) — une preuve trop faible rend `403 {error: {message, code: "strong_device_required"}}` et la carte reste en attente. Accordée **par le compte lui-même** : compte et locataire `suspended` tout de suite (toute requête suivante : `403`), effacement complet en moins de 24 h par l'outil d'opérateur (`nokker erase --pending --apply`). Proposer l'export AVANT : après l'accord, le compte ne peut plus le télécharger. **Sans appareil fort** (ni passkey ni Mini App) : écrire au support (l'opérateur efface sur demande, `nokker erase`) — pas de confirmation par e-mail (décision produit, 25.10) ; aucune route n'enrôle aujourd'hui une passkey sur un compte déjà connecté. **Abonnement** : l'accord résilie d'abord l'abonnement Stripe (tout de suite, sans remboursement) ; Stripe injoignable → rien n'est suspendu, la carte passe `failed`, nouvelle demande possible |

`/v1/threads*`, `/v1/bubbles*`, `/v1/notifications`, `/v1/users`, `/v1/agents*`, `/v1/previews*` :
docs/BUBBLES.md (§1-§6 pour les fils/bulles/aperçus, §9 pour les comblements du 18.10.2026). Export d'un fil et liens
partagés (`/v1/threads/{id}/export`, `/v1/threads/{id}/shares`, `/v1/shares/{id}`, `/v1/public/shares/{jeton}`) :
§Export et partage d'un fil ci-dessous.

## Export et partage d'un fil (01.11.2026, branche `p2/export-share`)

Code : `nokker/threads/export.py` (document et rendus), `nokker/threads/shares.py` (liens), routes dans
`nokker/api/server.py`, migration `044_thread_shares.sql`. Erreurs au format des fils (docs/BUBBLES.md §8) :
`{"error": {"message"}}`.

| Méthode | Chemin | Rôle |
|---|---|---|
| GET | `/v1/threads/{id}/export?format=json\|md\|html&include_tools=1\|0&before=<index>&limit=<n>` | export du fil, **membres** (comme `GET /v1/threads/{id}`) ; `401` sans identité, `403` non membre, `404` fil inconnu ou d'un autre locataire, `400` format/`before`/`limit` invalides. Pièce jointe `nokker-thread-<id>.<format>`, `Cache-Control: no-store`, `X-Content-Type-Options: nosniff` |
| POST | `/v1/threads/{id}/shares` | `{expires_in?: secondes (60 … 7 776 000, défaut 604 800 = 7 j), include_tools?: bool (défaut false)}` → `201 {share_id, thread_id, url, created_at, expires_at, revoked_at: null, include_tools, status: "active", turn_count, truncated}` ; **propriétaire du fil seulement** (`403` membre, `404` inconnu/autre locataire), `400` paramètre invalide ou 20 partages actifs déjà sur ce fil. `url` n'est rendue **qu'ici** |
| GET | `/v1/threads/{id}/shares` | propriétaire → `200 {shares: [{share_id, thread_id, created_at, expires_at, revoked_at, include_tools, status: active\|revoked\|expired}]}` (jamais l'URL ni le jeton) |
| DELETE | `/v1/shares/{share_id}` | propriétaire du fil → `200` (même objet, `status: "revoked"`) ; révocation **immédiate**, idempotente ; `403` membre non propriétaire ; `404` partage inconnu, d'un autre locataire, ou appelant non membre du fil |
| GET | `/v1/public/shares/{jeton}` · `….json` · `….html` · `….md` | **sans connexion** : l'instantané du partage. `404 {"error": {"message": "not found"}}` identique pour un jeton mal formé, inconnu, révoqué ou expiré (et pour un fil supprimé) ; `429 {"error": {"message": "rate_limited"}}` + `Retry-After: 60` au-delà de 30 lectures/min par IP (`CF-Connecting-IP`, sinon `X-Real-IP`, sinon le pair) ou 600/min au total ; `503` base injoignable. En-têtes : `Cache-Control: no-store`, `Referrer-Policy: no-referrer`, `X-Robots-Tag: noindex, nofollow`, CSP `default-src 'none'` (+ `style-src 'unsafe-inline'` et `sandbox` pour le html) |
| GET | `/v1/local/sessions/{id}/export?…` | Lokkal : même export, jeton local (`401` sinon), fils locaux seulement (`404` fil inconnu ou d'un locataire) |

**Document `json`** (`format: "nokker-thread-export/1"`) :

```json
{ "format": "nokker-thread-export/1", "exported_at": "…", "include_tools": true,
  "thread": {"id": "th_…", "title": "…", "created_at": "…", "updated_at": "…", "owner": "…", "members": ["…"]},
  "turn_count": 2,
  "messages": [
    {"index": 0, "role": "user", "content": "…", "channel": "api", "sender": "…"},
    {"index": 0, "role": "assistant", "content": "Je regarde.", "tool_calls": [{"id": "c1", "name": "…", "arguments": "{…}"}]},
    {"index": 0, "role": "tool", "tool_call_id": "c1", "name": "…", "sealed": false, "content": "…"},
    {"index": 0, "role": "assistant", "content": "<réponse du tour>"}
  ],
  "page": {"before": null, "limit": 100, "next_before": null, "truncated": false} }
```

- `messages` reprend `GET /v1/threads/{id}` (même `index` de tour) en y ajoutant, avec `include_tools=1` (défaut de
  l'export), les étapes d'outils du tour lues dans `Turn.messages` : message assistant porteur de `tool_calls`
  (`arguments` = JSON en texte), puis un message `tool` par résultat. Les notes internes du noyau et les brouillons
  écartés n'y sont pas. La réponse du tour est toujours le dernier message `assistant` de son `index`.
- **Secrets** : le résultat d'un outil d'effet `SECRET` (spec du registre, sinon table par défaut des effets) ou déjà
  scellé par le noyau (`qa/invariants.py::_seal_secret_result`) sort `sealed: true` avec un texte fixe — jamais la
  valeur, même pour un tour stocké avant le scellement. Tout le reste (messages, réponses, arguments, résultats,
  titre) passe par `Invariants.scrub_output`.
- **Taille** : `limit` ≤ 500 tours (défaut 100), derniers tours avant `before` (exclu) ; un message ou une réponse est
  tronqué à 40 000 caractères, un résultat d'outil à 8 000, des arguments à 4 000 (`truncated: true` sur l'élément) ;
  l'export entier à 4 Mo (JSON) : au-delà, les tours plus anciens passent à la page suivante (`page.truncated`,
  `page.next_before` = `before` à repasser ; `null` quand tout l'historique est rendu).
- **`md`** : un titre, une section par tour, appels et résultats en blocs de code (clôture plus longue que toute
  suite d'accents graves du contenu). **`html`** : page autonome, CSS dans la page, thème clair/sombre, **aucun
  script**, tout texte échappé (guillemets compris), aucun lien rendu cliquable, CSP `default-src 'none'; style-src
  'unsafe-inline'; … sandbox`.

**Liens partagés** :

- Jeton = `secrets.token_urlsafe(32)` (43 caractères, 256 bits), stocké **haché** (sha256) dans `thread_share_tokens`
  (hors RLS, lue sans locataire : empreinte → partage + locataire) ; l'instantané est dans `thread_shares` (RLS forcée,
  locataire du fil). La lecture publique ne lit QUE l'instantané de ce partage, dans ce locataire.
- **Instantané figé** au moment du partage (export `public`, 500 derniers tours, 4 Mo) : les tours suivants n'y
  entrent pas ; partager à nouveau = nouveau lien. Il ne porte ni id du fil, ni propriétaire, ni membres, ni
  expéditeur des messages, ni locataire ; `include_tools: false` par défaut (messages et réponses seulement), et avec
  `true` les secrets restent scellés comme dans l'export. Le document public ajoute `share: {created_at, expires_at}`.
- `url` = `share_base_url` (config, base publique de l'API) + `/v1/public/shares/<jeton>.html` ; vide = chemin
  relatif, à préfixer par l'hôte de l'API.
- Révocation (`DELETE`) et suppression du fil (`DELETE /v1/threads/{id}`) : `404` public immédiat. Expiration
  obligatoire (90 j au plus). L'effacement d'un compte (`nokker erase`) couvre les deux tables par `tenant_id`.
- Limites : le compteur de débit est par processus (mémoire) — deux répliques doublent le plafond ; pas de purge
  automatique des empreintes expirées (`PgShares.purge_expired`, à brancher sur un balayeur).

## Pièces jointes du message (`attachments`)

Dernier message `user` : `attachments: [{type: "image"|"text"|"dir"|"url", path?|content?|url?|data?, name?, depth?}]`
à côté de `content`. Sans `type` = image (forme X7, docs/LOKKAL.md §7.6, rétro-compatible). `text`/`dir`/`url` :
seulement si l'instance a `text_attachments_enabled` (défaut faux ; sinon la pièce suit le chemin des images et reçoit
le même 400 qu'avant). `text` : `content` (le noyau cloud n'a pas accès au disque de l'utilisateur) ou `path` (session
locale seulement, sous son dossier) ; `dir` : `path` + `depth` 1-3, liste des noms ; `url` : http(s), lue par le chemin
de `web_fetch` avec sa garde SSRF, contenu marqué NON FIABLE pour le modèle. Caviardage des secrets avant le contexte ;
plafonds 8 pièces (dont 3 url), 50 000 caractères par pièce, 150 000 par message, troncature signalée. Refus : 400
`{"error": {"message"}}`. Détail et format exact vu par le modèle : docs/LOKKAL.md §7.6.1.

## Pause et steer (branche `p2/pause-steer`, 01.11.2026)

| Méthode | Chemin | Rôle |
|---|---|---|
| POST | `/v1/threads/{id}/pause` | met le fil en pause → `202 {thread_id, paused: true, running}` (`running` : un tour tourne sur le fil) ; idempotent |
| POST | `/v1/threads/{id}/resume` | lève la pause du fil → `200 {thread_id, was_paused, paused}` (`paused` reste vrai si une pause du compte tient encore le fil) |
| POST | `/v1/threads/{id}/steer` | `{text}` (≤ 8000 car.) → `202 {thread_id, steer_id: "st_…", status: "queued", paused}` ; `409 {error: {code: "no_running_turn"}}` sans tour en cours ; `404 {error: {code: "steer_disabled"}}` tant que `steer_enabled` est faux (défaut) ; `400` texte vide ou trop long |
| POST | `/v1/pause` · `/v1/resume` | tous les fils du compte → `202 {scope: "account", paused: true}` · `200 {scope: "account", paused: false, was_paused}` |

**Qui** : sur le web, les routes de fil passent par l'identité des fils (`/v1/threads*`, docs/BUBBLES.md) : fil du
locataire de l'appelant (un id d'un autre locataire est préfixé par le sien, donc `404`), membre du fil sinon `403`,
`401` sans identité. La pause globale vise le **compte appelant dans son locataire** (`(locataire, compte)`) : les tours
dont il est l'expéditeur, jamais ceux d'un autre compte. Sur Lokkal (instance avec API locale), les mêmes chemins
demandent le jeton local ou le propriétaire relayé (`401` sinon), comme `cancel` ; la pause globale y vaut pour toute
la machine. Un jeton de délégation (§7.5 de docs/LOKKAL.md) ouvre `pause|resume|steer` sur SON fil, jamais `/v1/pause`.

**Pause** : vérifiée par la boucle à deux points de contrôle — avant chaque appel modèle et avant chaque appel d'outil
(`nokker/core/loop.py`, `nokker/core/controls.py`). Le tour en cours y attend : un outil commencé finit, le suivant ne
part pas ; un appel modèle en cours finit (sa réponse est gardée). Tout l'état du tour reste en mémoire (messages, cache
des lectures, compteurs) et il repart au même point à la reprise. Le tour garde la file de son fil (docs/LOKKAL.md
§7.4.9) : ce qui arrive ensuite sur le fil attend derrière lui ; l'appel direct (`/v1/local/tools/call`) prend `409`.
Un fil en pause sans tour le reste : son prochain tour s'arrête avant son premier appel modèle. Les sous-agents
`<fil>/sa-…` suivent la pause de leur fil. `cancel` réveille un tour en pause et l'arrête (« Annulé à ta demande. ») ;
la pause du fil reste posée. Pendant la pause, la requête de chat reste ouverte (en flux : `: ping` toutes les ~10 s).
Limites : en mémoire du processus (un redémarrage lève les pauses et perd le tour en cours, comme avant) ; aucune
durée maximale ; un appel modèle en vol n'est pas interrompu par une pause (il l'est par `cancel` s'il est en flux :
§Fin de tour).

**Steer** : un message de l'utilisateur pour le tour EN COURS. Il est consommé au point de contrôle qui précède le
prochain appel modèle, donc après les résultats de TOUS les outils déjà demandés par le modèle (jamais entre un appel
et son résultat, ce qui casserait le format). Plusieurs steers : dans l'ordre d'arrivée. Pendant une pause : consommé à
la reprise. Caviardé comme un message (invariants d'entrée), gardé dans l'historique. Sans tour en cours : **`409`,
pas de bascule automatique en message normal** — le client envoie alors un message ordinaire
(`/v1/chat/completions`) ; un tour qui ATTEND dans la file du fil ne compte pas comme en cours. Un steer accepté qui
arrive trop tard (le modèle écrit déjà sa réponse finale, ou le tour conclut sans outils) est rendu par
`turn.steer_dropped` : le client le renvoie en message normal. Jamais perdu en silence.

**Événements** (`/v1/events`, audience du fil) : `turn.paused {thread_id, turn_id, at: "model"|"tool", tool?}` quand
le tour s'arrête VRAIMENT à un point de contrôle (rien si le fil est en pause sans tour) ; `turn.resumed {thread_id,
turn_id, at, tool?, paused_ms}` quand il repart (pas après une annulation : `turn.done` suit) ; `turn.steered
{thread_id, turn_id, steer_id}` quand un steer entre dans le contexte ; `turn.steer_dropped {thread_id, turn_id,
steer_id, text}`. Au ledger, chaque pause laisse un verdict `loop:paused` (layer 0, ALLOW, durée). Ce que voit le
modèle (steer, mention de reprise) : docs/CONTEXT-FORMAT.md, derrière `steer_enabled`.

## Fin de tour : `turn.done` (champs du 01.11, branche `p2/fixes-e2e`)

`turn.done {thread_id, turn_id, usage, tier?, cost_chf?}` gagne trois champs, **présents seulement quand ils
s'appliquent** (absents sinon) :

- **`error: {code, message}`** — le tour a échoué. `message` est caviardé, ≤ 500 caractères. Codes :
  `tier_unavailable` (aucun palier n'a répondu, repli compris : la réponse du tour est l'escalade qui le dit) ;
  `provider_error` (l'appel de conclusion forcée a échoué chez l'amont) ; `max_calls` (limite d'appels sans
  conclusion) ; `stopped` (arrêté par le noyau, `stop_check`) ; `internal_error` (exception dans la boucle, tour
  enregistré sans réponse). Un tour annulé, refusé par un plafond (quota, budget) ou escaladé par une porte n'a PAS
  d'`error`.
- **`refused: true`** — au moins un effet a été refusé d'office pendant le tour : refus d'une porte d'action (règle
  `deny`, mode plan, commande « hardline », invariant comme l'envoi de masse), ou carte posée par le tour et déjà
  rejetée à sa fin (le client sans surveillance `nokker -p` rejette toute carte). Limite : une carte rejetée APRÈS
  `turn.done` n'y figure pas (le client `-p` la connaît de toute façon). Le motif d'un refus est sur le `tool.result`
  de l'appel (`reason`, docs/LOKKAL.md §7.4.2).
- **`cancelled: true`** — le tour a été arrêté par `POST /v1/threads/{id}/cancel`. Depuis le 01.11, l'annulation
  coupe aussi un appel modèle **en flux** au prochain morceau reçu (réponse HTTP de l'amont fermée) ; le texte partiel
  de cet appel n'est pas gardé : le client reçoit un `reset` puis la réponse finale « Annulé à ta demande. »
  (docs/LOKKAL.md §7.4.4).

Le texte vu par le modèle ne change pas : ces champs sont calculés côté noyau pour les clients.

## Streaming de `/v1/chat/completions` (`stream: true`, P1 19.10.2026)

Pourquoi : un tour réel du canari a duré ~2 min (dernier appel modèle : 68 s) ; sans un octet pendant 100 s,
Cloudflare (approve.nokker.ai → nginx :8743 → noyau :8742) coupe et Safari affiche « Load failed ».

**Pour tous les clients** : les en-têtes SSE partent tout de suite (`X-Accel-Buffering: no` pour nginx), le tour
tourne dans un fil de travail, et un **commentaire SSE `: ping`** part toutes les ~10 s tant que rien d'autre ne
sort. Les commentaires sont ignorés par tout parseur SSE (spec), par le SDK (`sse.ts`) et par la passerelle amont
(`_consume_sse_line` ne lit que les lignes `data: `). Sans opt-in, le corps est **octet pour octet** celui d'avant,
pings exceptés : rôle, réponse complète + `usage`, `finish_reason: "stop"`, `[DONE]` (vérifié contre `origin/main`
et par `tests/unit/api/test_stream.py`). Une erreur du tour APRÈS les en-têtes part en `data: {"error": {"message"}}`
sans `[DONE]` (avant : HTTP 500).

**Jetons en direct — opt-in `X-Nokker-Stream-Tokens: 1`** (le client web le pose ; la passerelle amont ne le pose
pas, elle concatène TOUT `delta.content` reçu et ne doit jamais voir de texte partiel) :

1. `delta: {"role": "assistant"}` d'emblée (`model: "nokker"` dans les chunks en direct) ;
2. `delta: {"content": "…"}` au fil de l'eau — **seul le contenu des appels modèles** est streamé, jamais un
   résultat d'outil ; caviardé par `Invariants.scrub_output` sur le texte CUMULÉ de l'appel, avec une retenue
   (≈ 80 caractères, 2 × le plus long secret connu s'il est plus long) et une coupe sur un blanc (jamais un jeton à
   moitié), rien après un `-----BEGIN` non fermé ; si le caviardage réécrit une partie déjà émise, plus aucun delta
   pour cet appel (`nokker/core/stream.py`) ;
3. `{"choices": [{"delta": {}}], "nokker": {"reset": true}}` — le texte reçu depuis le dernier reset **n'est pas
   la réponse** (l'appel a fini en tool_calls, a été relancé en Deep, ou a échoué) : le client le vide (le web
   repasse alors en animation de réflexion) ;
4. chunk final `{"choices": [{"delta": {"content": "<réponse FINALE caviardée>"}}], "usage": …, "nokker":
   {"reset": true, "final": true}}` — **fait foi** : le client vide le texte en cours PUIS écrit ce contenu (le reset
   d'un chunk s'applique avant son delta). C'est la réponse après la porte bascule (escalade, relance) ; la retenue
   de fin d'appel n'est jamais émise autrement ;
5. `finish_reason: "stop"`, puis `[DONE]`.

Choix assumé : le texte streamé d'un appel qui finit en outils a pu s'afficher un instant — pas grave (caviardé), le
reset le retire. Le texte d'un appel final s'affiche AVANT le verdict de la porte bascule ; si elle le remplace
(escalade), le chunk final corrige. Le web recharge de toute façon le fil à la fin du flux (`store.ts::send`).
`turn.delta` sur `/v1/events` reste unique par tour (réponse complète juste avant `turn.done`). Côté amont,
`SokkanProvider` passe en `stream: true` (+ `stream_options.include_usage`) seulement quand le noyau lui donne un
`on_delta`, reconstitue la même `Completion` (content, tool_calls par index, usage) et ne réessaie que tant
qu'aucun morceau n'a été remis — et seulement dans le budget de l'appel (`call_budget_s`, défaut 180 s, réessais
compris ; docs/LOKKAL.md §7.17 point 11).

## Événements (`/v1/events`, SSE, `id:` + `event:` + `data:` JSON)

`approval.requested` {approval} · `approval.decided` {approval_id, decision, by, via} — plus les événements fils,
tours et bulles de docs/BUBBLES.md §6 (`turn.*`, `tool.*`, `thread.updated`, `bubble.*`, `session.message`), filtrés
par membre (§8).

Chaque événement porte un `id:` SSE entier croissant. **Reprise** : un client qui se reconnecte avec l'en-tête
`Last-Event-ID` (reposé automatiquement par `EventSource` du navigateur à la reconnexion) reçoit d'abord les
événements manqués dans une fenêtre BORNÉE (`EventBus.max_buffer`=1000 événements, `max_age_s`=3600 s — pas un
journal infini : au-delà, relire l'état via `GET /v1/approvals`), puis bascule en direct.

Les clients s'abonnent une fois et rendent l'état de tous les fils (une carte qui arrive pendant une conversation
CLI s'affiche dans la CLI, sur le téléphone et dans Telegram ; la première décision valide gagne, les autres
reçoivent `approval.decided`).

## Apprentissage (drapeaux `learning`, `memory_pending` ; docs/LEARNING.md)

Éteints par défaut : ces routes répondent 404 comme avant. nokker.ai : session du compte (`us_…`). Lokkal : jeton
local ; par la passerelle (web), lecture seule (`403 machine_only` sur un POST). Erreurs `{"error": {"code",
"message"}}`.

| Méthode | Chemin | Rôle |
|---|---|---|
| GET | `/v1/learning/journal?limit=200` | chronologie de l'apprentissage du compte (écran `/journey`), du plus récent au plus ancien : `{entries: [{id, kind, subject, at, actor, version, thread_id, turn_index, detail}]}` ; `kind` ∈ `skill.created`, `skill.updated`, `skill.reverted`, `skill.disabled`, `memory.pending`, `memory.approved`, `memory.rejected` ; `actor` = `agent` (carte approuvée) ou `owner` (geste de l'humain) ; `detail` : `{sha256, active, reverted_to?, approval_id?}` pour un skill, `{source, kind, scope, title, note?}` pour une note |
| GET | `/v1/learning/skills` | `{skills: [{name, version, active, at}]}` |
| GET | `/v1/learning/skills/{nom}?version=&diff_from=` | `{name, current, active, versions: [{name, version, sha256, op, active, reverted_to, thread_id, turn_index, actor, approval_id, at}], version?: {…, content}, diff?}` ; 404 `unknown_skill` / `unknown_version` |
| POST | `/v1/learning/skills/{nom}/revert` `{version}` | revient à `version` (nouvelle version, rien d'effacé) ; `0` désactive. → `{skill, version, op, active, reverted_to}` ; 400 `bad_body`, 404, 409 `unchanged`, 422 `scan_refused`, 503 `no_scanner`. Geste de l'humain : pas de carte |
| GET | `/v1/memory/pending?status=pending\|approved\|rejected\|all` | `{notes: [{id, source: memory_note\|propose, kind, title, text, scope, derived_from, project, links, thread_id, turn_index, at, status, decided_at, decided_by, result}]}` |
| POST | `/v1/memory/pending/{id}/approve` | la note entre en mémoire (`result` = nom de la note écrite) → `{note}` ; 404 inconnue, 409 `already_decided`, 503 `no_memory` |
| POST | `/v1/memory/pending/{id}/reject` | rien n'est écrit → `{note}` ; 404, 409 |

Cartes : `skill_create`, `skill_update` et `skill_revert` du modèle posent toujours une carte (règle `learning:card`)
dont `arguments` est le skill complet ; décision comme toute carte (§Ressources). Événements sur `/v1/events` :
`skill.learned {account, name, version, op, active, thread_id, turn_index, actor}`, `memory.pending {account, id,
source, kind, scope, title, thread_id, turn_index}`, `memory.approved` / `memory.rejected {account, id, kind, scope,
title, note}`.
