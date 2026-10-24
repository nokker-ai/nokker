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
  `script` (bleu #6FA8FF) · `media` (rose #FF8BB5) · `agent_draft` (cyan) · `approval` (laiton) · `episode` (mémoire de session délestée, couleur côté client). Couleurs côté client
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
call_id, name, args_preview} · `tool.result` {thread_id, turn_id, call_id, name, ok, ms} · `turn.done` {thread_id,
turn_id, usage, tier?, cost_chf?} · `thread.updated` {thread} · `bubble.created` {bubble} · `bubble.updated` {bubble}
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

