# Bulles, fils et sessions qui se parlent — contrat (P1, 18.10.2026)

> Complète `docs/CLIENT-API.md`. Vision produit : Nick, 18.10.2026.
> Maquettes : canvas Design « Nokker — interface bulles » (Main, Crew, Mobile, Duo, DuoPaysage).
> Ce document fait foi pour le noyau (`nokker/bubbles/`, `nokker/threads/`, `nokker/api/server.py`) ET pour les
> clients (`nokker-apps` : sdk, mock-server, web, CLI). Un écart = on corrige ce fichier d'abord.

## 0. Les trois objets

- **Fil** (*thread*, « session » dans l'UI) : une conversation **permanente** (jamais de `clear` ; les épisodes et la
  consolidation sont l'affaire du Context Engine, A-4). Identifiant = la valeur de `X-Session-Id` déjà utilisée par
  `/v1/chat/completions`. Un fil a un propriétaire et des membres.
- **Bulle** : un **objet vivant** rattaché à un fil (une recherche web, un monitoring éphémère, un timer, un script
  qui tourne, un aperçu média, une notification d'une autre session, une carte de création d'agent…). Ce n'est ni un
  fil ni un message. Elle apparaît, vit (`working` → glow), se réduit, se ferme, se **déplace** ou se **copie** vers un
  autre fil (ce qui **modifie le contexte** du fil d'arrivée), se **partage** avec des membres ou des fils.
- **Message entre sessions** : un fil peut écrire à un autre (comme deux sessions d'agent qui se parlent). Le fil destinataire le
  reçoit dans son contexte ; l'utilisateur est notifié **dans le fil où il se trouve** par une bulle `notify`.

## 1. Fil

```json
{ "id": "th_…", "title": "Infra", "owner": "alice", "members": ["alice", "bruno"],
  "created_at": "…", "updated_at": "…", "working": false, "unread": 0,
  "bubble_count": 3, "last_snippet": "Disque hote21 à 91 %" }
```

| Méthode | Chemin | Rôle |
|---|---|---|
| GET | `/v1/threads` | fils de l'utilisateur (membre ou propriétaire), triés par `updated_at` desc |
| POST | `/v1/threads` | `{title}` → fil créé (id `th_` + 12 hex) |
| GET | `/v1/threads/{id}` | `{thread, messages, bubbles}` ; `messages` = derniers tours (user/assistant, `?before=<index>&limit=50` : `before` = index de TOUR exclu, `limit` = nombre de TOURS, chaque message porte `index`), `bubbles` = bulles ouvertes (+ partagées avec ce fil, `shared: true`) |
| PATCH | `/v1/threads/{id}` | `{title?, members?}` — `members` : propriétaire seulement, utilisateurs connus seulement (400 sinon) |
| POST | `/v1/threads/{id}/messages` | message d'une session à une autre (§4) |

Un `X-Session-Id` inconnu sur `/v1/chat/completions` crée le fil à la volée (titre provisoire = 48 premiers
caractères du premier message), comme aujourd'hui pour les tours — rétrocompatible avec la gateway Hermes
(`X-Hermes-Session-Id` reste accepté ; `X-Session-Id` prime). Propriétaire = identité vérifiée de l'appel. Sur un fil
EXISTANT, une identité qui n'en est pas membre reçoit 403 (serveur ouvert sans identité : pas de contrôle, dev).
`unread` vaut toujours 0 en P1 (pas de suivi de lecture côté noyau).

## 2. Bulle

```json
{ "id": "bu_…", "thread_id": "th_…", "kind": "search", "title": "Libérer de l'espace",
  "status": "working", "state": { … selon kind … }, "actions": [ {"id": "purge", "label": "Proposer une purge", "primary": true} ],
  "origin": null, "shared_with": {"members": [], "threads": []},
  "created_by": "nina", "created_at": "…", "updated_at": "…", "closed_at": null, "version": 3 }
```

- `kind` ∈ `monitor` (cyan #5FD4C8) · `search` (violet #B49CFF) · `notify` (laiton #E3B06A) · `timer` (vert #5BD39A) ·
  `script` (bleu #6FA8FF) · `media` (rose #FF8BB5) · `agent_draft` (cyan) · `approval` (laiton) · `episode` (mémoire de session délestée, couleur côté client) · `artifact` (page créée en session, §10, couleur côté client). Couleurs côté client
  (`packages/brand`), jamais dans l'API.
- `status` ∈ `idle` · `working` (le client glow) · `done` · `failed`.
- `version` : entier croissant, chaque modification l'incrémente ; un `PATCH` peut porter `if_version` (409 sinon).
- `origin` : `{thread_id, bubble_id, mode: "copy"|"move"}` quand la bulle vient d'un transfert.
- Réduite/déployée et position à l'écran = **état de vue du client** (localStorage / préférences), pas l'API.

`state` par `kind` (champs inconnus ignorés par les clients) :

| kind | state |
|---|---|
| `search` | `{query, queries?: [..] (plusieurs recherches d'un même tour fusionnées dans UNE bulle), took_ms, engines: [..], results: [{url, title, snippet, site, preview?}]}` — `preview` = objet §5, rempli après coup (`bubble.updated`) |
| `monitor` | `{target, metric, value, unit, max?, series?: [[ts, v]…], items?: [{label, value}]}` |
| `timer` | `{label, ends_at, started_at}` |
| `script` | `{command, host?, log_tail: [..≤40 lignes], exit_code?}` |
| `media` | `{url, mime, poster?, preview?}` |
| `notify` | `{from_thread, from_title, text, approval_id?, actions}` — action implicite `goto` (« Aller à la session ») |
| `agent_draft` | `{name, role, model, tools: [..], fill: 0..1}` — à l'activation : `bubble.closed` + `agent.created` (Crew) |
| `approval` | `{approval_id}` — la carte elle-même se lit par `/v1/approvals/{id}` |
| `artifact` | `{page_id, path, title, mime, version, size, public, updated_at}` — page créée en session (§10) ; jamais le contenu de la page ; jamais créable par un client (400) |
| `episode` | `{episode_id, title, summary, turns, from_turn, to_turn, tokens_raw, closed_reason, day, keywords}` — créée par le noyau à la **clôture d'un épisode** (docs/NEVER-CLEAR.md §3) ; jamais créable par un client (400). Actions : `reload` (épingle l'épisode dans la mémoire de session du fil), `archive` (désépingle et ferme). `move`/`copy` vers un autre fil copie la transcription (caviardée) dans le **réservoir du fil d'arrivée** et injecte `[Contexte reçu]` + `recall_context("rs_…")` : la mémoire voyage sans lecture inter-fils |

| Méthode | Chemin | Rôle |
|---|---|---|
| GET | `/v1/threads/{id}/bubbles?include_closed=0` | bulles du fil |
| POST | `/v1/threads/{id}/bubbles` | bulle créée par le client (`kind`, `title`, `state`) — ex. timer posé à la main |
| GET | `/v1/bubbles/{id}` | une bulle |
| PATCH | `/v1/bubbles/{id}` | `{title?, state?, status?, if_version?}` |
| POST | `/v1/bubbles/{id}/close` | ferme (la bulle reste lisible, `closed_at` posé) |
| POST | `/v1/bubbles/{id}/transfer` | `{to_thread, mode: "move"\|"copy"}` (§3) |
| POST | `/v1/bubbles/{id}/share` | `{members?: [..], threads?: [..]}` remplace la liste ; un membre doit être un utilisateur connu (`telegram_ids`/sessions) |
| POST | `/v1/bubbles/{id}/actions/{action_id}` | déclenche l'action ; une action à effet passe par l'exécuteur et les approbations (APPROVALS.md), jamais en direct |

Droits : lire/modifier une bulle = être membre de son fil **ou** figurer dans `shared_with.members` (lecture seule
dans ce cas) ; un fil listé dans `shared_with.threads` la voit dans `GET /v1/threads/{id}/bubbles` (marquée
`shared: true`, lecture seule).

**Qui crée les bulles** : le noyau, à partir des résultats d'outils (`nokker/bubbles/producers.py` : table
outil → kind, ex. `web_search` → `search`, `infra_disk_*`/`infra_gpu_info` → `monitor`, `session_send` → `notify`
côté destinataire), et le modèle par l'outil `bubble_update` (effet READ, met à jour une bulle de SON fil : titre,
state, status). Une bulle créée par un producteur porte `created_by: "nina"`.

## 3. Transfert (Déplacer / Copier) — modifie le contexte du fil d'arrivée

- `move` : la bulle change de `thread_id` (même id), `origin` posé ; elle disparaît du fil de départ.
- `copy` : nouvelle bulle (nouvel id) dans le fil d'arrivée, `origin` = la source ; la source reste.
- Dans les deux cas le noyau **ajoute au contexte du fil d'arrivée** un bloc système compact, au prochain tour :
  `[Bulle reçue de « <titre du fil source> » — <kind> « <titre> »] <résumé ≤ 600 caractères de state>`
  (rendu par `nokker/bubbles/context.py::render_for_context`, borné, sans secret — passe par le scrubber des
  invariants). Le fil de départ, en `move`, reçoit `[Bulle envoyée vers « … »]`.
- Événement `bubble.transferred` `{bubble_id, new_bubble_id, from_thread, to_thread, mode}` puis `bubble.created`
  (copy) ou `bubble.updated` (move).

## 4. Sessions qui se parlent

- `POST /v1/threads/{to}/messages` `{from_thread, text, ask?: bool}` (ou l'outil `session_send(to_thread, text,
  ask=false)`, effet READ sur son propre périmètre, refusé vers un fil dont l'utilisateur n'est pas membre).
- Effet : le texte entre dans le contexte du fil `to` comme `[Message de la session « <titre> »] …` ; si `ask`, un tour
  est lancé dans `to` (en tâche de fond, `turn.*` publiés avec `thread_id: to`) et sa réponse revient à `from` de la
  même façon.
- Notification : une bulle `notify` est créée **sans fil** (`thread_id: null`, `state.from_thread` posé) et publiée en
  `bubble.created` ; le client l'affiche dans le fil **où se trouve l'utilisateur** avec les actions de `state.actions`
  + `goto`. Fermée à la première action ou au bout de 10 min (`bubble.closed`).
- Un fil qui travaille publie `thread.updated` avec `working: true` (le tiroir affiche le point vivant).

## 5. Aperçus (« montrer les sources au lieu de les citer »)

`GET /v1/previews?url=<https://…>` →

```json
{ "url": "…", "final_url": "…", "title": "…", "site_name": "…", "description": "…",
  "image": "/v1/previews/img/<sha256>?exp=<epoch>&sig=<hmac>", "favicon": "/v1/previews/img/<sha256>?exp=…&sig=…",
  "excerpt": "≤ 1 200 caractères (Trafilatura)", "lang": "fr", "kind": "article|video|repo|doc|other",
  "embed": {"provider": "youtube", "id": "…"} , "fetched_at": "…" }
```

- Sources : OpenGraph/Twitter cards, oEmbed (YouTube, Vimeo, X), favicon, extrait Trafilatura. Les images sont
  **re-servies par le noyau** (`/v1/previews/img/…`, cache disque, ≤ 2 Mo, `image/*` seulement) : le client ne contacte
  jamais le site tiers (vie privée, CSP). L'URL est **signée** (`?exp=…&sig=…`, détail §9.3) pour qu'un simple
  `<img src=…>` puisse la charger sans jamais poser d'en-tête `Authorization`.
- Garde : `https` et `http` seulement, **refus des IP privées/loopback/link-local après résolution** (SSRF), 5 s max,
  ≤ 3 redirections, corps ≤ 3 Mo, cache 24 h ; même garde PII que `web_search` sur l'URL.
- Le producteur `search` demande les aperçus des 5 premiers résultats en tâche de fond et publie `bubble.updated`.
- Capture d'écran Chromium : P2, hors de ce contrat.

**Implémenté** (branche `p1/previews`, `nokker/previews/` + `nokker/api/previews_routes.py`) : détails tranchés à
l'implémentation, non couverts ci-dessus —

- **Garde SSRF**, précisée : refus explicite en plus de « privée/loopback/link-local » — **CGNAT
  (`100.64.0.0/10`, réseau CGNAT, celui de Tailscale)**, multicast, IPv6 ULA (`fc00::/7`), adresses
  IPv4-mappées-IPv6 (`::ffff:a.b.c.d`, démappées avant classification). Les formes décimales/octales/hexadécimales
  d'IP (`http://2130706433/` == `127.0.0.1`) n'ont rien de spécial à coder : `socket.getaddrinfo` les résout déjà en
  IP avant toute requête DNS, donc elles passent par la même classification. **Chaque redirection est re-vérifiée**
  (nouvelle résolution + nouvelle classification) et la **connexion se fait à l'IP déjà vérifiée** (`network_backend`
  httpcore pinglé), jamais une seconde résolution DNS entre la vérification et la connexion réelle (ferme la fenêtre
  de DNS rebinding) — le SNI/la vérification TLS continuent de porter sur le vrai nom d'hôte. Le délai de 5 s est un
  budget GLOBAL sur tous les hops (une suite de redirections ne peut pas additionner 5 s par saut).
- `lang` : sous-tag principal de `<html lang="…">` (ou repli `og:locale`), **`null` si indétectable** — pas de
  détection de langue sur le texte, volontairement (hors scope P1).
- `kind` : `video` si un `embed` a été reconnu (YouTube/Vimeo/X, par forme d'URL) ; `repo` si l'hôte est
  `github.com`/`gitlab.com`/`codeberg.org` ET le chemin a ≥ 2 segments (pas la page d'accueil) ;
  `doc` si `Content-Type: application/pdf` ou extension `.pdf`/`.docx`/`.odt` ; `article` si `og:type: article` **ou**
  un extrait Trafilatura non vide a été produit ; sinon `other`.
- `image`/`favicon` : `null` si l'URL déclarée (OpenGraph/Twitter/favicon) ne répond pas, dépasse 2 Mo, ou ne
  renvoie pas un `Content-Type: image/*` — jamais une erreur sur l'aperçu entier pour une image en échec.
- Cache image : disque, borné par **taille totale** (config `previews_image_cache_max_bytes`, défaut 200 Mo),
  éviction la plus ancienne d'abord, **pas de TTL** (contrairement à l'aperçu JSON, 24 h) — une image déjà en cache
  reste servable même après l'expiration de l'aperçu qui l'a produite.
- Auth : identique au reste des ressources client `/v1/*` (§ Authentification et identité ci-dessus — jeton de
  session, clé API statique, ou `initData` Telegram), pas le schéma plus permissif de `/v1/chat/completions`.
- Point d'accroche producteur `search` : `nokker.previews.preview_many(urls, limit=5)` (singleton paresseux construit
  depuis `NokkerConfig`), ou `PreviewService.preview_many(...)` sur une instance dédiée — synchrone, parallélisé par
  fils, une URL en échec ne fait jamais échouer le lot (`{"url": …, "error": "…"}`).

## 6. Événements ajoutés à `/v1/events`

`turn.started` {thread_id, turn_id} · `turn.delta` {thread_id, turn_id, text} · `tool.call` {thread_id, turn_id,
call_id, name, args_preview} · `tool.result` {thread_id, turn_id, call_id, name, ok, ms} · `turn.cost` {thread_id,
turn_id, calls, subagent_calls, input_tokens, cached_tokens, output_tokens, reasoning_tokens, total_tokens, cost
{devise: montant}, unpriced_models, models} (juste avant `turn.done` ; forme et règles : docs/en/cli.md « Cost per
turn ») · `turn.done` {thread_id, turn_id, usage, tier?, cost_chf?} · `thread.updated` {thread} · `bubble.created` {bubble} · `bubble.updated` {bubble}
· `bubble.closed` {bubble_id, thread_id} · `bubble.transferred` {…§3} · `session.message` {from_thread, to_thread,
text} · `agent.created` {agent}.

Filtrage : un abonné ne reçoit que les événements des fils dont il est membre (et des bulles partagées avec lui).

## 7. Côté client — rappel des règles d'UI (note `feedback-nokker-ui-bulles`)

Chat + tiroir gauche (fils). Bulles **flottantes**, **légèrement translucides** (fond teinté ≈ 50 % d'opacité +
`backdrop-filter: blur(8px) saturate(150%)`, plus transparentes pendant le glisser, Nick 18.10.2026 : « ça fait de
l'effet si on les déplace, et ça ne masque pas complètement le texte du chat »), couleur + icône + libellé par type,
glow quand `working`, animées à l'ouverture et à la fermeture, barre Réduire · Partager · Fermer, glisser vers un fil
du tiroir → choix **Déplacer / Copier**. Signature toc toc toc (`packages/brand/src/signature.ts`) à chaque
`bubble.created`, son coupé par défaut, vibration sur mobile.

## 8. Précisions d'implémentation du noyau (branche `p1/bubbles`, 18.10.2026) — font foi pour les clients

Tranché au plus simple là où le contrat ci-dessus ne disait rien :

- **Codes** : 401 sans identité ; 404 fil/bulle inconnu **ou illisible** (on ne révèle pas l'existence d'une bulle
  qu'on ne peut pas lire) ; 403 lecture seule, pas membre, ou membres changés par un non-propriétaire ; 409
  `if_version` périmé, corps `{"error": {"message", "current_version"}}` ; 400 sinon.
- **Création client** (`POST /v1/threads/{id}/bubbles`) : tout `kind` sauf `notify` (réservé au noyau) ; champ
  `status` optionnel ; `created_by` = l'identité de l'appel.
- **Transfert** : membre du fil de la bulle ET du fil d'arrivée ; même fil → 400 ; une `notify` ne se transfère ni
  ne se partage. `share.threads` : seulement des fils dont l'appelant est membre.
- **`GET /v1/bubbles/{id}`** porte `shared: true` quand l'appelant n'a que la lecture.
- **Contexte reçu** : les blocs (§3 bulle reçue/envoyée, §4 message/réponse de session) sont mis dans la boîte du fil
  et vidés au début de SON prochain tour, rendus `[Contexte reçu]\n<un bloc par ligne>\n[/Contexte]` dans le message
  `user` du tour, après le rappel mémoire (docs/CONTEXT-FORMAT.md) ; gardés dans le tour (`Turn.injected`) et donc
  rejoués par l'historique. Message de session borné à 2 000 caractères, résumé de bulle à 600, tout passe par le
  scrubber de sortie des invariants.
- **Sessions** : `POST /v1/threads/{to}/messages` → `{"delivered": true, "ask": bool}`, **202** si `ask`. Il faut
  être membre de `from_thread` ET de `to`. Le tour `ask` tourne sur le canal `session` et ne peut pas relancer `ask`
  lui-même (pas de ping-pong) ; sa réponse revient à `from` comme `[Réponse de la session « … »] …` (même chemin :
  `session.message` + `notify`). Pas de bloc dans la boîte de `to` quand `ask` (le bloc EST l'entrée du tour).
- **`notify`** : destinataires = membres du fil qui reçoit le message (y compris l'auteur s'il en est membre),
  posés dans `shared_with.members` — ce sont eux seuls qui la lisent et agissent dessus. `state` porte aussi
  `to_thread` ; `state.actions` vaut `[]` par défaut (seul `goto`, implicite). Toute action la ferme ;
  `POST …/actions/goto` → `{"closed": true, "goto": "<from_thread>"}`. Balayage des expirées toutes les 30 s.
- **Action d'une autre bulle** (`POST /v1/bubbles/{id}/actions/{action_id}`) : l'action doit figurer dans
  `bubble.actions` (404 sinon) ; elle devient un **tour** du fil de la bulle (`[Action « libellé » demandée sur la
  bulle …]`, en tâche de fond, identité de l'appelant) → **202** `{"accepted": true, "thread_id"}`. Jamais exécutée en
  direct : le tour passe par les portes et les approbations comme un message.
- **Producteurs** : `search` = 10 résultats au plus (`took_ms` = durée de l'appel d'outil) ; `monitor` = aussi tout
  `infra_*_{usage,info,status,stats}` d'effet READ ; une bulle n'est produite que si l'outil a été VRAIMENT exécuté
  (pas sur un rejeu du mémo READ du tour). Le résultat d'outil vu par le modèle est suffixé
  `[bulle bu_… (<kind>) affichée dans le fil — bubble_update pour la modifier]` (sans id, il ne pourrait pas appeler
  `bubble_update`). Aperçus : `Producers.request_previews(bubble)`, no-op tant que `nokker/previews/` ne le branche
  pas ; le service met à jour par `BubbleHub.update_from_kernel`.
- **Événements** : `turn.delta` est UNIQUE par tour (réponse complète juste avant `turn.done`) — y compris depuis
  le streaming du 19.10.2026 : les jetons en direct ne passent QUE par le SSE de `/v1/chat/completions`, sur opt-in
  `X-Nokker-Stream-Tokens: 1` (docs/CLIENT-API.md §Streaming), pas par `/v1/events`. `turn_id` =
  `<thread_id>:<index du tour>` ; `tool.call.args_preview` ≤ 200 caractères, caviardé. Filtrage : chaque événement
  fils/bulles a une audience (membres du fil, + membres/fils de `shared_with`, + ancienne audience pour un
  `move`/`share`) ; `approval.*` ET `agent.created` (§9.4) restent diffusés à toute identité vérifiée (pas filtrés
  par membre, docs/CLIENT-API.md). Le SSE OpenAI de `/v1/chat/completions` : docs/CLIENT-API.md §Streaming.
- **Limites P1** : bus d'événements mémoire mono-processus ; verrou « un tour à la fois par fil » mono-processus ;
  pas de `DELETE` de fil ni de bulle. ~~`agent_draft`/`agent.created` (Crew) non produits par le noyau~~ comblé le
  18.10.2026 (§9.4) : reste en P2 l'exécution RÉELLE d'un agent (`status` reste `armed`).

## 9. Comblements relevés par le client web (18.10.2026) — notifications, utilisateurs, aperçus signés, Crew

Quatre écarts que `nokker-apps` (client web) a buté sur en implémentant le contrat ci-dessus. §8 reste la
référence pour le reste du noyau ; ici, quatre routes/mécanismes de plus.

### 9.1 `GET /v1/notifications`

Les bulles `notify` **ouvertes** adressées à l'appelant (`thread_id: null`, §4), pour qu'un rechargement de page
ne les perde pas — même forme que les éléments de `bubbles` ailleurs (`bubble_to_json`, §2). Identité résolue
requise (comme le reste de `/v1/*`, 401 sinon) ; dépend de `hub` (404 si les bulles sont désactivées, même esprit
que `/v1/threads*`) ; balayées par le même sweeper que les autres `notify` (10 min, §4).

```
GET /v1/notifications → [ { "id": "bu_…", "thread_id": null, "kind": "notify",
                            "state": {"from_thread", "from_title", "to_thread", "text", "actions"}, … }, … ]
```

### 9.2 `GET /v1/users`

Les utilisateurs connus de la config (`NokkerConfig.known_users()` : identités des clés d'API + `telegram_ids`),
pour la liste de partage côté client (`POST /v1/bubbles/{id}/share`, `PATCH /v1/threads/{id}` membres) — **rien
d'autre**, jamais une identité hors de cette liste. Identité résolue requise (401 sinon) ; **indépendant de
`hub`/`bubbles_enabled`** (sert aussi à afficher qui est qui, pas seulement à partager).

```json
GET /v1/users → [ {"id": "alice", "name": "Alice", "initials": "AL"},
                   {"id": "bruno", "name": "Bruno", "initials": "BR"} ]
```

`name` = identifiant mis en forme (`_`/`-`/espace → mots capitalisés) ; `initials` = 2 premières lettres du
premier mot (ou 1 lettre de chacun des 2 premiers mots s'il y en a plusieurs), en majuscules. Volontairement
simple pour P1 : pas de table de vrais prénoms en config — un identifiant qui n'est pas un prénom lisible a un
rendu brut (ex. `alice_s` → `Alice S` → `AS`).

### 9.3 Aperçus — images signées (`<img>` ne pose jamais d'`Authorization`)

`image`/`favicon` de `GET /v1/previews` (§5) sont des URL **signées** :
`/v1/previews/img/<sha256>?exp=<epoch>&sig=<hmac-sha256 tronqué à 32 caractères hex>`. Un `<img src=…>` ne peut
jamais poser d'en-tête `Authorization` : `GET /v1/previews/img/{sha}` accepte donc DEUX preuves — l'auth normale
(`_resolve_identity`, inchangée) OU une signature valide (`exp` non expiré + `sig` correct). `GET /v1/previews`
lui-même (le JSON) reste identité SEULE, comme avant — la signature ne s'applique qu'à l'image re-servie.

- **TTL** : 24 h (`nokker.previews.cache.IMG_SIGN_TTL_S`), posée au moment où l'URL est produite
  (`PreviewService._cache_remote_image` → `signed_image_url`) — indépendante du TTL de l'aperçu JSON (§5, 24 h
  aussi, mais un cycle de vie différent : une image déjà en cache reste servable après l'expiration du JSON qui
  la référence, §5 ; sa signature, elle, expire à SA propre échéance).
- **Clé HMAC** : `NokkerConfig.previews_sign_secret_env` (`NOKKER_PREVIEWS_SIGN_SECRET`, variable d'environnement
  — jamais dans le dépôt, comme le reste de `config.py`) si l'opérateur en fournit une ; **sinon générée au
  démarrage** (`secrets.token_bytes(32)`) et gardée en mémoire par le `PreviewService` pour la durée du
  processus. Limite connue : sans secret d'environnement, un redémarrage du serveur invalide tous les liens
  signés déjà distribués — acceptable en P1 (le client relit `GET /v1/previews`, qui re-signe avec le nouveau
  secret). Signature et vérification passent TOUJOURS par la même instance de `PreviewService`
  (`PreviewsRoutes.service`), donc le secret généré est cohérent des deux côtés.
- `hmac.compare_digest`, jamais une comparaison `==` sur la signature (`nokker/previews/cache.py::sign_image`/
  `verify_image_signature`).

### 9.4 Crew minimal — `GET`/`POST /v1/agents`, activation d'un `agent_draft`

```
GET  /v1/agents → [ {"id": "ag_…", "name": "…", "role": "…", "model": "…", "tools": [..],
                     "status": "armed", "created_by": "nina", "created_at": "…", "updated_at": "…"}, … ]
POST /v1/agents {"name", "role", "model"?, "tools"?: [..]} → 201, même forme — `status` vaut TOUJOURS `armed`
                 (pas d'exécution réelle d'agent : P2, hors de ce contrat). `name`/`role` requis (400 sinon).
```

Stockage mémoire ou Postgres (même bascule que fils/bulles/approbations, `NokkerConfig.pg_dsn()`), migration
`nokker/agents/migrations/0001_agents.sql`. Indépendant de `hub`/`bubbles_enabled` (toggle propre,
`NokkerConfig.agents_enabled`, défaut `True`) : Crew peut exister sans fils/bulles. Chaque création (par
`POST /v1/agents` OU par activation, ci-dessous) publie `agent.created` **à toute identité vérifiée** — pas de
filtre par membre de fil (même principe que `approval.*`, docs/CLIENT-API.md) : Crew est une vue d'équipe
partagée, pas propre à un fil.

Outil du modèle `agent_draft` (effet READ, `nokker/bubbles/tools.py`) : propose une carte — une bulle
`kind: agent_draft`, `state: {name, role, model, tools, fill}` (`fill` ∈ [0,1], borné, défaut 1), action
`activate` posée d'office — dans le fil courant. Ne crée AUCUN agent par lui-même. `POST
/v1/bubbles/{id}/actions/activate` sur une bulle `agent_draft` (SEULE action acceptée pour ce `kind`, 404 pour
toute autre) crée l'agent (`created_by` = l'identité qui active, pas celle qui a proposé la carte), **ferme la
bulle** (`bubble.closed`) et publie `agent.created` — réponse `200 {"activated": true, "agent": {...}}` (pas de
`202` : contrairement à l'action générique §8, celle-ci s'exécute en direct, pas comme un tour — créer un agent
`armed` n'a pas d'effet à soumettre aux portes). Sans Crew configuré (`agents_enabled=False`), l'activation lève
une erreur 400 (« crew (agents) non configuré ») plutôt que de fermer la bulle sans rien créer.


### 9.5 Crew avec moteur (lot X14, contrat PROVISOIRE du 25.10.2026, dev ↔ onboarding)

> Provisoire : le moteur n'est pas encore codé (vague 2, après le coffre X4 et l'approbation au téléphone X10). Le
> web code en face de ce contrat ; un changement passe par ce paragraphe, daté, pas par un message.

**Agent** (forme de `GET /v1/agents` et de toute réponse qui porte un agent ; champs §9.4 conservés) :

```
{ …§9.4…,
  "status": "paused" | "armed" | "running" | "failed",   // paused = au repos/désarmé (bleu), armed = prêt (vert),
                                                          // running = un passage en cours (orange), failed = dernier
                                                          // passage en échec (rouge) ; jamais la couleur seule à l'écran
  "instructions": "…",                                    // la mission (le prompt de chaque passage)
  "trigger": {"kind": "manual"}
           | {"kind": "schedule", "cron": "0 7 * * 1-5", "tz": "Europe/Zurich"}
           | {"kind": "once", "at": "2026-11-02T07:00:00Z"}
           | {"kind": "event", "event": "alert:<nom>" | "webhook:<id>" | "mail:<filtre>"},
  "next_run_at": "…" | null,                              // calculé par le noyau (schedule, once) ; null sinon
  "last_run": {"run_id", "status", "ended_at"} | null,
  "secrets": ["NOM", …],                                  // NOMS du coffre (X4), jamais de valeur
  "budget": {"max_calls": 100, "max_cost": 2.0, "currency": "EUR"} | null,
  "auto_approve": ["outil(motif)", …],                    // règles levées sans carte pendant un passage ; une règle
                                                          // d'écriture sur un agent déclenché par événement : refusée
  "placement": {"machine": "auto" | "mc_…"} | null        // X15 ; null = la machine qui porte le noyau
}
```

**Actions** (identité vérifiée du locataire ; erreurs `{"error": {"message"}}`) :

```
POST   /v1/agents                      {…}  → 201 agent ; trigger manual → "armed", sinon → "paused" (à armer)
PATCH  /v1/agents/{id}                 {…}  → 200 agent ; changer trigger, tools, auto_approve ou secrets d'un agent
                                              armé le remet en "paused"
POST   /v1/agents/{id}/arm                  → 200 agent ; 202 {"approval_id"} si auto_approve contient une écriture
                                              (carte FORTE, clé d'appareil, docs/APPROVALS.md) ; l'agent passe
                                              "armed" à l'approbation
POST   /v1/agents/{id}/disarm               → 200 agent ("paused") ; un passage en cours continue (l'annuler à part)
POST   /v1/agents/{id}/run                  → 202 {"run_id"} : lancer maintenant (tout statut sauf running)
DELETE /v1/agents/{id}                      → 204 ; un passage en cours est annulé d'abord ; les fils des passages
                                              restent (turns append-only)
GET    /v1/agents/{id}/runs?limit=20&before=<run_id>
       → {"runs": [{"run_id", "agent_id", "trigger": "manual|schedule|once|event", "scheduled_for", "started_at",
                    "ended_at", "status": "queued|running|waiting_approval|succeeded|failed|cancelled|skipped",
                    "thread_id", "cost": {"tokens_in", "tokens_out", "amount", "currency"} | null,
                    "summary": "…" | null}], "next_before": "…" | null}
POST   /v1/agents/{id}/runs/{run_id}/cancel → 200 run
```

Un passage est un tour ordinaire dans son propre fil (`thread_id`) : ses effets passent par les portes et les cartes
comme partout (les cartes vont au téléphone, X10) ; `waiting_approval` = le passage attend une carte. Lancer,
armer et désarmer depuis le deck ne demandent pas d'autre carte que ci-dessus : le clic de l'humain EST la
décision (« une session propose, l'humain approuve »).

**Événements** (diffusés comme `agent.created`, à toute identité vérifiée du locataire) :
`agent.updated {agent}` (tout changement, dont `status`, `next_run_at`, `last_run`), `agent.deleted {id}`,
`agent.run {agent_id, run}` (chaque changement d'état d'un passage). La carte respire tant que `status` vaut
`running`.

**Fils des passages** (onboarding, 25.10) : chaque passage a son propre fil ; ce fil porte `origin: {"kind": "agent",
"agent_id", "run_id"}` dans la liste des fils et dans `GET /v1/threads/{id}`, pour que les clients les rangent sous
l'agent plutôt que dans la liste des sessions (les autres fils : `origin` absent).

**Proposition par une session** : toujours l'outil `agent_draft` (§9.4) ; `state` porte en plus `instructions`,
`trigger`, `secrets`, `budget`, `auto_approve`. L'activation crée l'agent avec les mêmes règles que `POST
/v1/agents` (manual → armed, sinon paused ; armer reste un geste à part, avec la carte forte si une écriture est
levée).

## 10. Bulle `artifact` et pages créées en session (contrat du 24.10.2026, implémentation noyau : dev, avant le 27.10)

**État (24.10, p2/lokkal)** : 10.1, 10.2, 10.3, 10.5, 10.6 codés (`nokker/pages/`, `/v1/pages/{id}` et `/link` dans le
serveur local). Éteint par défaut dans le noyau (`local.pages`), **allumé par la config Lokkal** (`deploy/lokkal/core-config.yaml`)
depuis l'APPS_REF nokker-apps 8e01708 (25.10 ; ef9440d depuis, CSS de la bulle réparé), qui porte le correctif « kind inconnu » de la CLI : une machine qui
garderait une CLI rc.5 plantait sur `artifact`.
10.4 (publish/unpublish) : à faire avec le jeton de machine (`pages_public`).

Le pourquoi (autre origine, privée par défaut, publique sur choix) : docs/LOKKAL.md §6.6 et docs/GATEWAY.md §5. Ici,
le contrat entre le noyau et les clients.

### 10.1 Créer une page

- **Explicite** : outil `page_create {path, title}` (effet WRITE, dans le dossier de travail de la session). Une page,
  c'est **un fichier HTML autonome du dossier de travail** (CSS, JS et images en ligne ou en `data:`) ; pas de
  fichiers annexes en v1 (servir un dossier exposerait le dépôt). Pas de détection implicite « tout HTML écrit est
  une page » : le modèle décide de montrer, l'utilisateur voit pourquoi une bulle apparaît.
- `page_id` = `pg_` + 16 hex aléatoires (jamais dérivé du chemin : un id devinable ou qui révèle l'arborescence ne
  sert à rien) ; une page par (fil, chemin) : `page_create` sur un chemin déjà page rend la même `page_id`.
- Le noyau garde une **copie** de chaque version (`$NOKKER_DATA/pages/<page_id>/v<N>.html`, 5 Mo max par version,
  `text/html` seulement en v1 ; pas de SVG, qui exécute du script) : la page reste servable si le fichier bouge ou si
  le dépôt change de branche, et le miroir (L5) n'a qu'à copier ce dossier.
- Le producteur (`nokker/bubbles/producers.py`) crée la bulle sur le résultat de `page_create` (une bulle par
  `page_id` et par fil), `created_by: "nina"`, `status: done`.

### 10.2 Suivre le fichier

- Chaque `write_file`/`patch` réussi sur le chemin d'une page du fil prend une nouvelle copie, `version + 1`, et pousse
  `bubble.updated` (pas de polling client). Pendant le tour qui modifie la page, `status: working` ; `done` à la fin du
  tour. Un fichier supprimé ne supprime pas la page (la dernière copie reste) ; `close` sur la bulle la retire.
- Le `state` ne porte jamais le HTML : l'aperçu vivant est un `<iframe>` vers l'URL de la page (§10.3), rechargé à
  chaque `bubble.updated`.

### 10.3 Servir une page

| Méthode | Chemin | Rôle |
|---|---|---|
| GET | `/v1/pages/{page_id}` | la dernière version ; `?v=N` une version précise |
| POST | `/v1/pages/{page_id}/link` | `{embed?, download?}` → `{url, expires_at, public_url?}` : lien signé pour l'`<iframe>`, « ouvrir » et « télécharger » (un iframe ne pose pas d'`Authorization`) ; `public_url` (`https://<nom>--pages.nokker.app/<page_id>`, « copier le lien ») seulement si la page est publique et la machine liée |

- Preuves acceptées par `GET` : l'auth normale, OU une signature `?exp=&sig=` (même schéma que §9.3, TTL 1 h,
  `hmac.compare_digest`), OU `public: true`. Sinon 404 (pas 401 : une page privée n'existe pas pour un inconnu).
- `download: true` : lien dont le `GET` porte `Content-Disposition: attachment; filename="<titre>.html"` (la page est
  autonome, elle marche hors ligne telle quelle) ; en local la signature couvre le téléchargement (`dl=1`), par la
  passerelle le relais redirige vers `/<page_id>?dl=1` et laisse passer l'en-tête.
- En-têtes posés par le noyau lui-même (défense en profondeur, le relais les pose aussi) : `Content-Security-Policy:
  sandbox allow-scripts allow-forms allow-popups allow-modals allow-downloads` (la même que le relais ; origine opaque : ni cookie, ni stockage, ni appel authentifié à
  l'API, même servie depuis `127.0.0.1` à côté de l'interface locale), `X-Content-Type-Options: nosniff`,
  `Referrer-Policy: no-referrer`, `Cache-Control: no-store`. Côté client : `<iframe sandbox="allow-scripts allow-forms
  allow-popups allow-modals allow-downloads">`, jamais `allow-same-origin`.
- **Par la passerelle** : l'interface (servie par `<nom>.nokker.app`) appelle la MÊME route `POST /v1/pages/{id}/link
  {embed?}` ; le Lokkal, qui a vérifié le jeton d'accès du compte, demande le lien à nokker.ai avec son jeton de machine
  (`POST /v1/gateway/pages-token {page_id, embed?}`) et le rend tel quel. Depuis app.nokker.ai (« mes machines ») :
  `POST /v1/link/machines/{machine_id}/pages-token {page_id, embed?}` (session du compte). Les deux rendent `{url,
  expires_at}` ; `url` = `https://<nom>--pages.nokker.app/_auth?token=<jeton
  pages>&to=/<page_id>` (+ `&embed=1` pour l'iframe : le relais redirige alors vers une capacité d'URL, sans cookie
  tiers). Jeton pages : ES256, `iss https://app.nokker.ai`, `aud nokker.app`, `scope: pages`, `machine`, `tenant`,
  `jti`, 60 s au plus, usage unique tenu par le relais. Le relais vérifie, puis passe `X-Nokker-Pages: 1` et
  `X-Nokker-Account` ; le noyau local ne croit ce compte que si la requête porte `X-Nokker-Tunnel` = le secret de
  SON tunnel (posé par le client de tunnel à chaque requête rejouée : un processus local qui imiterait les en-têtes
  du relais n'a pas ce secret). Localement, sans passerelle : `POST /v1/pages/{id}/link`.
- Les pages ne doivent utiliser que des URL **relatives** ou `data:` : elles sont servies depuis
  `<nom>--pages.nokker.app` (ou `127.0.0.1` en local), jamais depuis l'origine où elles ont été écrites.
- URL publique : `https://<nom>--pages.nokker.app/<page_id>` (le relais traduit vers `/v1/pages/<page_id>` du
  Lokkal) ; **la même URL** pour un abonné, servie depuis le miroir (L5) : un lien partagé ne casse pas quand on
  s'abonne ni quand la machine s'éteint. Localement, sans passerelle : le lien signé de `/link`.

### 10.4 Publier

- Actions de la bulle : `open` (client seul : `/link` puis nouvel onglet), `publish`, `unpublish`.
- `publish` passe par une **carte d'approbation forte** (APPROVALS.md) et n'est proposée que si le compte y a droit :
  abonné ou compte vérifié, dit par le jeton de machine de nokker.ai (`pages_public: true`) ; sinon l'action est
  absente (pas grisée). `unpublish` : sans carte, immédiat. `public` dans le `state` suit.
- Une page publique porte un lien de signalement ajouté par le relais (pas par le noyau).

### 10.5 Glisser vers une autre session

- `transfer` existant (§3). `move` : la bulle part, la page suit le fil d'arrivée (ses modifications futures viennent
  de ce fil). `copy` : **même `page_id`**, aucun fichier dupliqué ; la bulle copiée est figée à la version copiée
  (`origin` posé) et « ouvrir » montre la dernière version. Le résumé injecté (§3) donne titre, chemin et version,
  jamais le contenu.

### 10.6 Ce que voient les journaux et les événements

- `tool.call` d'un `write_file` qui écrit une page : `args_preview` comme pour tout fichier (coupé, caviardé des
  secrets par les invariants) ; le contenu de la page n'entre ni dans le `state`, ni dans `bubble.*`, ni au ledger.
