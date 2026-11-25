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
| POST | `/v1/chat/completions` | un tour (OpenAI, `stream: true` → SSE), en-tête `X-Session-Id` = fil ; images et pièces jointes du dernier message `user` : §Pièces jointes ; les `role: system` : §Prompt système |
| GET | `/v1/events` | SSE de l'utilisateur, tous fils confondus (voir ci-dessous) |
| GET | `/v1/approvals?status=pending` | cartes en attente |
| GET | `/v1/approvals/{id}` | une carte : outil, arguments, effet, raison, avis de Nina, niveau, expiration (forme JSON exacte : docs/APPROVALS.md §5); `target` : ce que l'outil va toucher, lisible, à afficher À LA PLACE de `arguments` — commande (`terminal`), chemin (`write_file`, `patch` en mode replace, `read_file`), URL (`web_fetch`, `browser_navigate`), « destinataire — sujet » (`mail_send`, envois), « domaine.service — entité » (`maison_action_forte`) ; caviardée comme l'historique (poignées `[secret:…]`, cartes, AVS), 200 caractères au plus, NFC, sans retour à la ligne ni caractère de contrôle ; **absente** (ni `null`) pour un outil sans cible connue ; calculée à la création de la carte et figée ; même champ dans `GET /v1/local/approvals` et dans `approval.requested` |
| POST | `/v1/approvals/{id}/challenge` | défi à signer |
| GET · POST | `/v1/approvals/suggestions` · `…/{id}/accept` · `…/{id}/dismiss` | règles « toujours autoriser » proposées depuis l'historique des cartes (drapeau `approval_suggestions`, éteint par défaut) ; accepter écrit la règle dans les réglages du propriétaire de la machine, sinon `409 no_rule_store` (docs/APPROVALS.md §7) |
| POST | `/v1/approvals/{id}/decision` | décision (signature brute, WebAuthn, ou TOTP — docs/APPROVALS.md §3.3-§3.5 ; sur un refus, `reason` facultatif : texte de 500 caractères au plus, sinon `400 bad_reason`, ignoré sur une approbation, docs/HOST-EMBED.md « Motif d'un refus »), déclenche l'exécution + le tour de continuation si approuvée (refusée : le tour de continuation seul, sans outil, `tool.result` `status: "refused"`, `reason: "declined"`) ; répond après l'exécution (`executed`/`failed`) si elle tient en 45 s, sinon avec la carte `approved` (l'exécution suit en fond) |
| POST | `/v1/devices` · GET `/v1/devices` · DELETE `/v1/devices/{id}` | appareils de l'utilisateur |
| POST | `/v1/devices/{id}/confirm` | confirme un appareil `pending` par TOTP (alternative à `python -m nokker devices approve <code>`) |
| POST | `/v1/devices/{id}/session-challenge` | défi de connexion (sans identité préalable — c'est lui qui en établit une) |
| POST | `/v1/sessions` | jeton de session contre ce défi signé (brut ou WebAuthn) |
| POST | `/v1/auth/email/start` | `{email, lang}` → **toujours** `200 {sent:true}` (même corps, même délai, même `429` que l'adresse ait un compte ou non) ; code à 6 chiffres envoyé par front1 seulement à un compte actif — 10 min, usage unique. **Langue du mail** (05.11.2026) : celle du compte, sinon celle de l'invitation, sinon `lang` reçu (langue du navigateur) ; `fr` à défaut. Le gabarit de front1 ne change pas, seule la langue passée suit cette règle |
| POST | `/v1/auth/email/verify` | `{email, code, name, public_key_spki}` → `200 {device_id, user}` ; toute erreur = `401 {error:"invalid_code"}` ; 5 essais puis code brûlé. L'appareil créé se connecte (`session-challenge` → `/v1/sessions`) mais **ne décide jamais** d'une approbation : `403 {"error":{"message":"strong_device_required"}}` |
| POST | `/v1/auth/oidc/start` · `/v1/auth/oidc/callback` | SSO OIDC d'une équipe (drapeau `sso`, docs/LOKKAL.md §7.24) : `start {connection \| email, public_key_spki, name}` → `{authorize_url, state}` ; `callback {state, code, signature de "<state>.oidc"}` → `{device_id, user, projects}`. Appareil de connexion seulement (`sso:<connexion>`), comme le code e-mail |
| GET | `/v1/account` | compte `us_…` exigé → `{email, first_name, lang, lang_is_default, created_at, status, terms: {version, accepted_at} \| null, terms_current: {version, lang}, terms_outdated: bool, consents: {training}}` ; `terms_outdated` = la dernière version acceptée n'est plus le texte courant d'aucune langue (conditions modifiées : écran bloquant de réacceptation) ; faux sans aucune acceptation ; `lang` = la langue RÉSOLUE du compte (la langue posée, sinon `fr`), jamais `null` ; `lang_is_default` (booléen, additif) = `true` quand aucune langue n'est posée (colonne NULL, « Automatique », la détection suit), `false` sinon : compte sans langue `{"lang": "fr", "lang_is_default": true}`, posé en `en` `{"lang": "en", "lang_is_default": false}`, posé en `fr` `{"lang": "fr", "lang_is_default": false}` ; la réponse de `PATCH {lang}` porte les mêmes champs |
| PATCH | `/v1/account` | compte `us_…` exigé ; corps `{first_name}` seul → `200 {first_name, name}` (prénom du profil, migration 072 ; vide l'efface, `name` retombe sur la partie locale de l'adresse) ; `400 bad_first_name` (lettres, espaces, `-`, `'`, `.`, 40 caractères au plus) ; `400 invalid` (autre champ ou corps vide). Règle du nom montré : docs/CONNECTORS-MULTI.md |
| PATCH | `/v1/account` | compte `us_…` exigé (comme `GET /v1/account` : le titulaire de SON compte seulement ; sans compte cloud, même refus que le GET) ; corps `{lang}` seul : `"fr"`, `"en"`, `"de"`, `"it"` ou `null` → `200` avec le compte, MÊME corps que `GET /v1/account` ; `null` = pas de langue de compte (la langue détectée du message, puis celle du fil, prennent le relais) ; `400 bad_lang` `{error: {code, valid: ["fr","en","de","it"], message}}` pour toute autre valeur (casse et espaces comptés, non chaîne comprise) ; `400 invalid` (corps non JSON, `lang` absent, autre champ avec `lang`). Effet : la langue de compte passe avant le message détecté dans la résolution de la langue d'un tour ; elle est relue à chaque tour, le tour SUIVANT la suit (aucun cache). Même valeur : `200` sans écriture. Un `fr` posé est un choix, distinct de `null` : `lang_is_default` les distingue |
| GET | `/v1/capabilities` | toute identité vérifiée (session cloud, jeton local, propriétaire relayé ; sinon `401`) → `200 {version: 1, mode: "cloud"\|"lokkal", features: {<drapeau>: bool, …}}` : fonctions actives POUR L'APPELANT, à lire au démarrage au lieu de sonder les routes — §Capacités |
| GET | `/v1/terms?lang=fr\|en` | public → `{version, lang, text}` ; `version` = SHA-256 des octets de `text` (langue inconnue : `fr`) |
| POST | `/v1/account/terms` | `{version}` → `200 {terms: {version, lang, accepted_at}, terms_current, terms_outdated}` : réacceptation, une ligne de plus (`terms_acceptances`, avec l'IP, sans invitation) ; `409 {error: "stale_version"}` si `version` n'est le texte courant d'aucune langue (écran périmé : relire `/v1/terms`) ; `503 {error: "terms_disabled"}` sans onboarding |
| PUT | `/v1/account/consent` | `{scope: "training", granted: bool}` → `200 {scope, granted}` ; session normale (pas d'appareil fort) ; une ligne `data_consents` seulement quand le choix change ; `400 {error:"invalid"}` |
| GET | `/v1/account/usage` | compte `us_…` exigé (sinon 403) → `{tz, mode: soft\|hard, plan, today: {day, resets_at, turns: {used, cap, over, hard_cap}}, week: {starts, resets_at, turns: {used, cap, over}}}` ; heures en Europe/Zurich, jour de quota 12:00 UTC → 12:00 UTC, semaine lundi 06:00 UTC ; `cap` null = sans limite ; `over` = used > cap |
| POST | `/v1/account/export` | compte `us_…`, session normale (tout appareil) → `202 {id, status: "ready", created_at, expires_at, size, download}` : l'export est construit tout de suite et gardé **24 h** ; **une demande par 24 h** → sinon `429 {error: "export_limit", retry_at}` ; `503 {error: "export_disabled"}` si le service n'est pas configuré |
| GET | `/v1/account/exports/{id}` | même corps (`status` : `ready` \| `expired`, `download` null une fois expiré) ; `404` pour un export d'un autre compte ou locataire |
| GET | `/v1/account/exports/{id}/download` | `200 application/json` (pièce jointe `nokker-export-<id>.json`, `Cache-Control: no-store`) ; `410 {error: "export_expired"}` après 24 h. Contenu : `format: "nokker-account-export/1"`, compte (adresse, langue, dates, statut), acceptations du règlement, historique des consentements, usage, **toutes les conversations** (`thread`, tours : message, réponse, messages d'outils, canal) ; avec le nœud téléphone : `phone` (téléphones, tâches avec opération, arguments, état et code d'erreur, mais sans le contenu rendu, inscriptions, `egress_hosts`) ; jamais de jeton, clé ou secret de connecteur |
| POST | `/v1/account/delete` | compte `us_…` → `202 {approval_id, strength: "strong", expires_at, reason}` : pose une carte d'approbation **forte** (une seule en attente : la même est rendue) ; le client la fait décider par `POST /v1/approvals/{id}/decision` (après `…/challenge`) avec une preuve d'appareil fort (Mini App + biométrie, ou passkey avec vérification) — une preuve trop faible rend `403 {error: {message, code: "strong_device_required"}}` et la carte reste en attente. Accordée **par le compte lui-même** : compte et locataire `suspended` tout de suite (toute requête suivante : `403`), effacement complet en moins de 24 h par l'outil d'opérateur (`nokker erase --pending --apply`). Proposer l'export AVANT : après l'accord, le compte ne peut plus le télécharger. **Sans appareil fort** (ni passkey ni Mini App) : écrire au support (l'opérateur efface sur demande, `nokker erase`) — pas de confirmation par e-mail (décision produit, 25.10) ; aucune route n'enrôle aujourd'hui une passkey sur un compte déjà connecté. **Abonnement** : l'accord résilie d'abord l'abonnement Stripe (tout de suite, sans remboursement) ; Stripe injoignable → rien n'est suspendu, la carte passe `failed`, nouvelle demande possible |

**Zones de l'app** (06.11.2026, docs/LOKKAL-PHONE.md §4.2, `nokker/zones/`), même contrat dans le noyau cloud et dans
Lokkal. Session de l'app : compte `us_…` au cloud, propriétaire sur Lokkal. Le jeton du nœud téléphone viendra en P7.

- `GET /v1/me/zones` et `PUT /v1/me/zones {zones: [{id, name, on_enter: [scenario_id], on_exit: [scenario_id],
  home: bool}]}` → `200 {zones}`. Au plus UNE zone `home` par membre, sinon `400 bad_zone`.
  - 18 zones au plus ; `name` de 1 à 40 caractères, neutralisé (`qa/untrusted.inline`).
  - **Aucune coordonnée** : une clé de plus (`lat`, `radius`…) donne `400 bad_zone`. Le centre et le rayon restent
    sur l'appareil.
  - Sur Lokkal, un scénario qui n'est pas au membre donne `400 unknown_scenario`.
- `POST /v1/me/zones/{id}/event {event: "enter"|"exit", at, event_id}` → `202 {queued: true, scenarios: [{id,
  status: started|card|skipped, reason?}]}`.
  - Un `event_id` déjà vu (7 jours) rend `200 {duplicate: true, …}` avec le résultat gardé.
  - `409 out_of_order` si l'événement est plus vieux que la dernière transition de la zone ; `404 zone_unknown`.
  - `at` : ISO 8601 avec fuseau, ou epoch ; au plus 5 min dans le futur.
  - Plus de 30 min : présence seulement, chaque scénario `skipped` avec la raison `stale`.
  - Présence : sur Lokkal, `device_tracker.see` du traceur `nokker_<membre>` par la passerelle (opération fermée
    `ha.tracker_see` : `dev_id` en fullmatch, des textes seulement, et seulement les traceurs des membres déclarés
    du foyer, `home_assistant.zone_members` ou à défaut les membres de la présence par pièce). Entrée dans la zone
    `home` → `home` ; sortie de toute zone → `not_home` ; entrée dans une autre zone → son nom. Aucune devinette
    par le nom. Partout : présence
    ambiante (`Presence.away` : sortis de leur zone `home`), « Qui fait quoi » (entrée `zone`, codes `zone_inside`/`zone_outside`, le membre
    lui-même seulement) et événement `zone.changed` au membre.
  - Scénarios, au nom du membre (registre de ha-full) :
    - propriétaire ou adulte seulement ;
    - FORT : carte forte (`card`, outil `zone_scenario`), jamais exécuté directement ; `skipped` (`not_allowed`)
      pour un rôle sans droit fort (mineurs) ;
    - sinon `automation.trigger` (`started`) ;
    - raisons de `skipped` : `unknown_scenario`, `absent`, `bridge_error`, `card_failed`, `no_bridge` (cloud),
      `stale`.
- `GET /v1/home/scenarios` → `200 {scenarios: [{id, name, strong}], bridge: "home-bridge"|"home-node"|null}`.
  `bridge: null` (cloud, Lokkal sans maison) : présence seulement.

**Appareils de l'app : agenda, contacts et rappels du téléphone** (P8, 06.11.2026, `nokker/devices/`), même contrat
dans le noyau cloud et dans Lokkal.
- Capacités : `PUT|GET|DELETE /v1/me/devices/{device_id}/caps`, avec `PUT {name, platform: ios|android|web,
  caps: [calendar.read, contacts.read, reminders.read]}` ; `GET /v1/me/devices`.
  - 5 appareils au plus par membre.
  - `name` neutralisé (`qa/untrusted.inline`) puis coupé à 40 caractères.
  - Le noyau ne garde aucune donnée lue.
- Demandes : `device.request {request_id, op, args, devices, expires_at}` sur le flux `/v1/events`, aux sessions du
  membre seulement et quand un de ses appareils a la capacité.
  - Délai : 20 s.
  - Débit : 30 par heure et par membre.
  - Opérations : `calendar.list {from, to, q?}` (31 jours au plus), `contacts.search {q, limit ≤ 10}`,
    `reminders.list {include_done, due_before?}`.
- Réponse : `POST /v1/me/device-requests/{request_id}/result {device_id, items | error: denied|unavailable|too_large}`
  → `204`. Autres statuts : `409 already_answered`, `410 expired`, `403 not_yours` (autre membre, ou appareil sans la
  capacité).
- Formes, par liste blanche (toute autre clé est retirée), 50 éléments au plus :
  - événement `{title, start, end, all_day, location?, calendar, notes ≤ 300}` ;
  - contact `{name, emails[], phones[], org?}` ;
  - rappel `{title, due?, done, list, notes ≤ 300}`.
- Côté modèle : AUCUNE nouvelle famille. Le téléphone est un agenda de plus dans 068 : « <nom> · ca_… (à toi,
  lecture seule, agenda du téléphone) », au membre seul.
  - Lecture par `calendar.list` ; événements à la forme des autres agendas, `"source": "device"` posé par le noyau,
    dans l'unique clôture `agenda`.
  - Erreurs `device_offline`, `device_denied`, `device_too_large`, `device_rate`, `calendar_read_only`.
  - En lecture fusionnée, clés d'état portant les libellés.
  - Textes relus, empreinte figée par `tests/unit/test_devices.py`.
  - `contacts.search` et `reminders.list` n'ont pas encore d'outil : ils se brancheront comme sources des familles
    contacts et tasks quand elles existeront.

**Nœud téléphone** (P7a, phase S, docs/LOKKAL-PHONE.md §3, `nokker/cloud/phone.py`). Contrat de nokker-front :
`packages/mock-server/src/phone.ts`. Drapeaux `cloud_link.phone_nodes` (éteint) et `cloud_link.phone_nodes_unattested`
(liste de comptes, ou `["*"]`). Erreurs au format `{error: {code, message}}`.

Côté membre (session de l'app) :
- `POST /v1/link/phone {name, platform, os_version?, app_version?, public_key_spki, enc_key_spki, attestation: null, key_storage:
  secure_enclave|strongbox|software, push_token: null}` → `202 {approval_id, key_fingerprint, expires_at}`.
  - Carte FORTE `phone_register` : arguments `{name, platform, key_fingerprint, key_storage, attested,
    requested_from}`.
  - Elle se signe sur un AUTRE appareil enrôlé : l'appareil de la session qui demande est refusé.
  - Erreurs : `unknown_field`, `bad_name`, `bad_platform`, `bad_key_storage`, `bad_public_key`,
    `attestation_unsupported`, `attestation_required`, `push_unavailable`, `409 too_many_phones` (3).
  - `enc_key_spki` : clé d'ACCORD P-256, distincte de la clé de signature. Sinon `enc_key_reused` ; illisible :
    `bad_enc_key`.
- `GET /v1/link/phone/registrations/{approval_id}` (alias `/v1/link/phone/{approval_id}`) → `202 {status: pending}`,
  puis `201 {machine_id, sealed_token: {v, epk, iv, ct}, member, caps_allowed}` UNE fois (JAMAIS `machine_token`),
  puis `410 gone`.
  `403 rejected` si la carte est refusée.
  - Le jeton est scellé pour la clé d'accord : ECDH P-256 éphémère, HKDF-SHA256 (sel vide,
    `info = "nokker-phone-v1|<approval_id>"`), AES-256-GCM (iv 12 o, AAD `approval_id`), clair `{"machine_token": …}`.
  - C'est la construction de `sealToken` du SDK : vérifiée croisée dans les deux sens, vecteur figé dans
    `tests/unit/test_phone_node.py`.
  - L'inscription en cours est gardée en base (`phone_registrations`, migration 078, RLS). Un redémarrage pendant
    l'approbation ne la perd pas.
  - Le jeton n'y est jamais en clair : seul son sceau attend le retrait, qui est atomique et l'efface.
  - Une inscription échue est supprimée à la demande suivante du membre.
- `GET /v1/link/machines` liste les téléphones avec `kind: "phone"` et `host: ""`, et les machines Lokkal avec
  `kind: "lokkal"`. Les téléphones restent hors du deck.
- `GET /v1/link/phone` → `{phones: [...]}` (alias).
- `GET /v1/link/phone/{machine}/tasks` : journal sans contenu.
- `POST /v1/link/machines/{id}/revoke` : retire le téléphone et annule sa file.

Côté nœud (jeton de machine `phone`) :
- Le jeton ne vaut QUE sur `/v1/gateway/phone/*` ; ailleurs `403 phone_scope`.
- Chaque requête porte `X-Nokker-Node-Proof: v1;ts=;nonce=;sig=`, ES256 sur
  `method\nhost\npath?query\nts\nnonce\nsha256hex(corps)`. Contrôles : ±60 s, nonce unique sur 120 s, sinon
  `401 proof_invalid`.
- `POST /v1/gateway/phone/beat` (v1 ; `reach: push` → `400 push_unavailable`) → `{ok, state: "live", next_beat_s: 60}`.
- `GET /v1/gateway/phone/tasks?wait=25` → `{tasks: [{task_id, op, args, args_sha256, origin, heavy, deadline,
  lease_until}]}`, 4 au plus, bail de 60 s.
- `POST …/tasks/{id}/lease` → `{lease_until}`, ou `409 task_<état>`.
- `POST …/tasks/{id}/result {status: done|failed, result | error}` → `{stored: true}`.
  - Envoi identique : `{stored: false}`. Envoi différent : `409 already_answered`.
  - Autres erreurs : `result_required`, `error_required`, `bad_status`, `413 result_too_large` (64 Kio).
- `GET /v1/gateway/phone/blobs/{jeton}` : média signé 2 min, à usage unique, lié au nœud. `410 gone` ou
  `403 not_your_blob`.
- Opérations fermées, en lecture : `zone.state`, `calendar.list`, `ocr.image`, `stt.audio`.
  - P8 : `calendar.list` passe par la file quand un téléphone du membre est en ligne, avec l'agenda ; sinon
    `device.request`.
  - Un téléphone n'est jamais une machine Lokkal : ni outils Lokkal, ni relais, ni placement (P7b).

**Pages par la connexion du téléphone** (P11, docs/LOKKAL-PHONE.md §4.5, `nokker/cloud/egress.py`). Contrat de front :
packages/sdk/src/egress.ts. Opération `fetch.get` de la file du nœud, capacité `egress`, coupée par défaut. Le modèle
ne choisit JAMAIS le téléphone : aucun champ dans `web_fetch`, aucun repli automatique ; jamais pour un agent, une
tâche planifiée ou une suite de carte.

- `GET|PUT /v1/me/egress/hosts {hosts}` : par membre (migration 079), ≤ 50, règle `normalizeEgressHost`.
  Erreurs : `bad_hosts`, `bad_host {index}`, `too_many_hosts`.
- (a) Mode liste : pendant un tour ouvert par la personne, un `web_fetch` vers un hôte de sa liste passe par son
  téléphone en ligne (`trigger: allowlist`). Le document porte `engine: "phone"`. En cas d'échec, il porte en
  `error` la phrase de la table, jamais un repli sur le cloud.
- (b) Le geste.
  - Un `web_fetch` refusé (403, 451, CAPTCHA, blocage) pose `phone_fetch_offers: [{call_id, url, host, reason}]` sur
    le message assistant, si un téléphone `egress` du membre est en ligne. Le texte vu par le modèle ne change pas.
  - `POST /v1/threads/{id}/phone-fetch {url, call_id}` → `202 {turn_id}`. Erreurs : `404 no_offer`,
    `410 offer_used`, `409 no_phone_egress`.
  - Le droit `phone_fetch_grant` (HMAC, 10 min, usage unique) permet seul le `fetch.get` explicite (`grant_id`).
  - Le tour de la personne (`origin: phone_fetch`) a pour texte « Ouvre cette page par mon téléphone : <url> » et
    joint la page : en-tête du noyau hors clôture, puis `web:page`. Ou l'échec `phone_fetch_failed : …`.
- `phone_fetched: [{host, ok, status? | error?}]` sur le message assistant ; `error` est le code brut.
- Limites : 30 fetch par heure et par nœud ; résultat jusqu'à 1 Mio (64 Kio pour les autres opérations).
- Offres et droits sont gardés en mémoire (un réplica).
- Effacement du compte (`nokker erase`, file `--pending`) : machines `phone`, tâches, inscriptions et liste d'hôtes partent avec lui ; le jeton du téléphone ne répond plus. Le téléphone d'un membre de foyer est rattaché au compte de ce membre : quitter le foyer ne le retire pas.
- Textes vus par le modèle : `TEXTS`, plus la table fermée `REASONS` (20 phrases ; un code inconnu donne « raison
  non précisée » et un avertissement au journal). Empreinte figée par `tests/unit/test_phone_egress.py`.

**Outils du nœud téléphone** (P7b, `nokker/cloud/phone_tools.py`, cloud) :
- Placement X15 (`nokker/cloud/placement.py`) : `Needs.caps`. Le téléphone déclare ses capacités par son battement
  (`on` seulement). Un outil à secret n'est jamais placé sur un téléphone. Un Lokkal passe avant un téléphone ; entre
  téléphones : live, puis mode home (lan_home, camera), puis en charge, puis Wi-Fi, puis nom et id. Le modèle ne choisit
  jamais la machine. P8 `calendar.list` passe par ce placement.
- `zone_state {zone?}` : depuis le magasin des zones du membre (075), sans le téléphone ; heure suisse ; offert aux
  comptes qui ont des zones.
- `ocr_image {back?}` : image X7 du fil (`img:<fil>|<sha256>`, URL signée pour le téléphone choisi) lue par
  `ocr.image` ; offert aux comptes dont un téléphone déclare `ocr`. `stt.audio` : plomberie seulement
  (`audio:<ref>`), aucun outil tant qu'il n'existe pas de pièce jointe audio.
- Au-delà de 20 s : `phone_tasks: [{task_id, op, machine_id, name, status}]` sur le message assistant (`status` :
  pending, done, failed, expired, cancelled ; bulle `phone_task` côté client). À la clôture, un tour de suite écrit par
  le noyau (`origin: phone_continue`, jamais accepté d'un client) porte le résultat clôturé comme donnée non fiable, ou
  l'échec, ou l'expiration ; une tâche annulée n'en a pas. Suivi en mémoire (un réplica).

**Découverte du réseau de la maison** (P10d, `nokker/cloud/phone_lan.py`) : op `lan.discover {types?, timeout_s ≤ 5}`
→ `{services: [{id, type | types, name}], at}` (lecture, sans attestation, cap `lan_home`, mode home d'abord). Liste
fermée : `_googlecast`, `_airplay`, `_raop`, `_home-assistant`, `_spotify-connect`, `_sonos` (`._tcp`) ; tout autre type
est écarté. `name` : `inline()`, 63 car. Une adresse, un port ou un TXT dans un service : résultat refusé en entier (502
`bad_result`). Jamais en base. Session du TITULAIRE : `GET /v1/link/phone/{machine_id}/lan/services?types=a,b` →
`{services: [{id, types, name}], at}` ; 409 `node_offline`, 504 `phone_timeout`, 400 `bad_op` (type hors liste),
autres codes de l'appareil (`not_home_wifi`, `local_network_denied`, `bad_args`) : 502 `{error: {code}}`.

**Caméra de veille du nœud maison** (P10c, `nokker/cloud/phone_camera.py`) — le noyau ne détient AUCUNE donnée de
caméra : il lit le nœud à la demande ; rétention sur l'appareil seulement.
- Ops (lectures, nœud non attesté permis, cap `camera`, mode home d'abord) : `camera.watch_event {after, limit ≤ 200}`
  → `{items, now}` ; `camera.thumb_url {id}` → `{id?, url, key, expires_in ≤ 300}`. Leurs résultats ne sont JAMAIS
  écrits en base (tâche close avec un résultat vide), jamais journalisés, jamais vus par le modèle.
- Items gardés : `{id, camera, start, end, severity, niveau, title, summary, objects, zones, has_thumb}` (forme courte
  ou longue ; tout autre champ ignoré ; `niveau` = « inconnu »).
- Session du TITULAIRE (404 pour un autre compte) : `GET /v1/link/phone/{machine_id}/camera/events?after=<epoch s>` →
  `{items, now}` ; `POST /v1/link/phone/{machine_id}/camera/thumb {id}` (alias `GET …/camera/{id}/thumb`) →
  `{id, url, key, expires_in}`. 10 s au plus ; `Cache-Control: no-store` ; 409 `node_offline`, 504 `phone_timeout`,
  410 `thumb_gone`, 404 `not_found`, autre code de l'appareil : 502 `{error: {code}}`.
- Brief (`maison_veille`, cloud) : les 24 dernières heures lues sur le nœud au moment du brief, gabarits de la veille
  (HOME-WATCH §7) ; nœud injoignable : « Relève en échec : cameras. ». Pas de notification immédiate en v1.

**Nœud maison** (P10a, `nokker/cloud/phone_home.py`, migration 081) :
- Opérations `ha.*` de home-bridge par la file (mêmes noms, arguments, résultats) : `ha.states`, `ha.registry`,
  `ha.query`, `ha.history`, `ha.logbook`, `ha.events {after, timeout ≤ 20}` (lectures ; 256 Kio pour states, registry,
  history et logbook, 64 Kio sinon) ; `ha.call_service`,
  `ha.notify`, `ha.tracker_see` (actions, nœud attesté seulement). Jamais : `ha.write_config`, `ha.delete_config`,
  `ha.camera_show` (403 `op_not_allowed`).
- `lan_home` ne compte que sur le Wi-Fi « maison » (`net.home_wifi: true`) ; mode `home` avant `pocket`.
- Une action porte `attestation` (JWT ES256 `typ: nokker-phone-task`, JWKS du lien) : `{aud: mc_…, task_id, op,
  args_sha256, decision, approval_id|null, iat, exp ≤ 120 s}`. `decision` = `{class: direct, domain, service,
  entity_ids}` (politique maison) ou `{class: strong, approval_id}` (carte forte). Le téléphone vérifie, recalcule
  args_sha256 et applique SA liste blanche ; une décision `direct` ne débloque jamais la liste sensible.
- Propriétaire seulement : le nœud sert le compte auquel il est rattaché (partage foyer : lot à part).
- Au cloud, les outils `maison` et `maison_action_forte` (mêmes cartes, mêmes portes) sont offerts au compte dont un
  téléphone déclare `lan_home` ; journal maison en base (`home_journal`), dans l'export (`phone.home_journal`) et
  effacé avec le compte.

`/v1/threads*`, `/v1/bubbles*`, `/v1/notifications`, `/v1/users`, `/v1/agents*`, `/v1/previews*` :
docs/BUBBLES.md (§1-§6 pour les fils/bulles/aperçus, §9 pour les comblements du 18.10.2026). Export d'un fil et liens
partagés (`/v1/threads/{id}/export`, `/v1/threads/{id}/shares`, `/v1/shares/{id}`, `/v1/public/shares/{jeton}`) :
§Export et partage d'un fil ci-dessous.

**Recherche dans les fils** (06.11.2026, docs/LOKKAL-PHONE.md §1.2 point 4, contrat nokker-apps APP-CORE-CONTRACTS.md
C3), **même contrat dans le noyau cloud et dans Lokkal** (`nokker/threads/search.py`) :
`GET /v1/threads/search?q=<texte>&limit=20` → `200 {results: [{thread_id, title, snippet, turn_index, at, channel,
copy_of?}], truncated}` ; `400 {error: "bad_query"}` (`q` de 2 à 200 caractères, `limit` de 1 à 50).
- Portée : les fils que l'appelant liste (`GET /v1/threads`), rien d'autre ; chez nokker.ai, pour le propriétaire du
  locataire, aussi les copies du miroir `lokkal-<machine>-<fil>` (`copy_of: {machine_id, thread_id}`, `title: null`).
- Recherche sans casse ni accents, dans le titre (`turn_index: null`) et le texte des tours (humain et réponse).
- `snippet` : 160 caractères au plus autour du terme, **caviardé** par le caviardage du noyau (celui du miroir)
  AVANT la recherche : un terme qui n'est que dans un secret ne rend rien.
- `channel` : `api | telegram | code | agent`. Plus récents d'abord. `truncated` : il y avait plus que `limit`, ou la
  base a coupé à 2 s (les titres restent).
- Aucune écriture, aucun journal du texte cherché.

**« Continuer dans le cloud »** (06.11.2026, LOKKAL-PHONE.md §1.2 point 3, `nokker/cloud/seed.py`), nokker.ai
seulement :
`POST /v1/threads {title?, seed_from: {machine_id, thread_id}}` → `201 {thread_id, title}`. Erreurs :
`403 owner_only` (pas le propriétaire du compte), `404 not_mirrored` (aucune copie en miroir), `400 bad_request`, et
`400 seed_unavailable` sur Lokkal.
- Le nouveau fil web reçoit, côté serveur, les 40 derniers messages de la copie `lokkal-<machine>-<fil>` : texte de
  la personne et réponse, caviardés une seconde fois. Sans outils, sans images, sans l'en-tête de la machine.
- Titre par défaut : `suite de <titre>`, ou sans titre connu `suite d'une session sur <machine>`.
- Ligne vue par le modèle, dans le bloc `[Contexte reçu]` du premier tour (textes relus, empreinte figée par
  `tests/unit/test_app_core.py`) : `suite de « <titre> » (session sur <machine>) ; messages recopiés sans appels
  d'outil ni pièces jointes`, ou sans titre `suite d'une session sur <machine> ; messages recopiés sans appels d'outil
  ni pièces jointes`. `<titre>` et `<machine>` passent par `qa/untrusted.inline()` avant la coupe (80 et 60
  caractères) ; machine déliée : `une machine déliée`.
- La copie du miroir n'est pas touchée.

**CORS de l'app native** (contrat C1, `nokker/api/cors.py`) : `capacitor://localhost` et `https://localhost` (liste
`cloud_link.cors_origins`), origine exacte renvoyée, `Expose-Headers: Retry-After`, flux SSE compris, et `Vary: Origin`
sur toute réponse de `/v1/*` concernée (origine connue ou non) ;
`OPTIONS /v1/*` → `204` sans authentification ; **jamais** `Allow-Credentials`. Hors champ : `/v1/gateway/authorize` et
les webhooks. Noyau cloud seulement (Lokkal : le relais).

**`X-Nokker-Surface`** (optionnel, `/v1/chat/completions`) : surface du client, `cli`, `acp`, `vscode`, `web` ou `pwa`
(casse et espaces tolérés ; toute autre valeur est ignorée). Elle remplit la ligne « Canal » du bloc `[Environnement]`
quand le canal est `api` ; listée dans `Access-Control-Allow-Headers` (noyau cloud et relais).

## Capacités : `GET /v1/capabilities` (05.11.2026, branche `p2/kernel-cloud-polish`)

Le web lit cette route une fois après connexion au lieu de sonder `/v1/house`, `/v1/local/cost`, `/v1/speech`… (404
en série). Authentifiée (même identité que `/v1/speech` : session cloud, jeton local, propriétaire relayé), `401` sinon,
`403` si l'expéditeur n'a pas de locataire. Corps :

```
{"version": 1, "mode": "cloud" | "lokkal",
 "features": {"accord": bool, "account": bool, "agents": bool, "approvals": bool, "billing": bool, "cloud_byok": bool, "code": bool,
              "connectors_multi": bool, "connectors_oauth": bool, "consolidation": bool, "family_calendar": bool, "folder": bool, "glance": bool, "guide": bool, "house": bool,
              "household": bool, "household_paper": bool, "learning": bool, "local": bool, "local_cost": bool,
              "long_memory": bool, "operate": bool, "packs": bool, "pay": bool, "pay_agent": bool, "plan": bool,
              "presence": bool, "share": bool, "show_me": bool, "speech": bool, "sso": bool, "steer": bool,
              "team": bool, "threads": bool, "turn_cost": bool, "who_does_what": bool}}
```

- `true` = les routes de la fonction répondent à CET appelant (installation et profil). Clés triées, toujours
  présentes ; une clé inconnue du client s'ignore, une clé absente vaut `false` (contrat additif, `version` monte
  seulement si un sens change).
- **Compte `us_…` exigé** (routes montées par `_account_route` ou qui refusent `account_required`) : `account`
  (`/v1/account*`), `code` (`/v1/code*`, sessions de code), `presence` (`/v1/presence`, « Qui fait quoi » ;
  `who_does_what` = même valeur, nom du drapeau de config), `team`, `packs`, `learning`, `long_memory`,
  `consolidation`, `sso`, `household_paper`, `family_calendar`, `pay` (`/v1/payments*`), `pay_agent`.
- **`glance`** (`/v1/glance/devices`, lunettes) : en Lokkal le propriétaire de la machine, en cloud un compte.
- **Toute identité vérifiée** : `operate` (`/v1/incidents*`, `/v1/alerts*`, drapeau `operate`), `plan` (`/v1/plan*`,
  cartes du plan), `guide`, `approvals`, `agents`, `threads`, `turn_cost`, `share`, `steer`, `show_me`, `speech`.
- Par appelant aussi : `billing` (compte + facturation configurée),
  `cloud_byok` (compte + bloc `cloud_byok.enabled`, jamais en Lokkal ; le droit selon la formule reste rendu par
  `/v1/account/models`), `household` (compte + foyer allumé), `house` (salle Maison allumée ET l'appelant est membre
  d'un foyer : seule lecture en base, faite dans le locataire de l'appelant ; foyer illisible → `false`), `local_cost`
  (Lokkal + propriétaire de la machine + ledger : `GET /v1/local/cost`).
- **Lokkal seulement** (jamais en cloud, même si la config les allume) : `accord` (`/v1/local/accord*`, bouton ACCORD,
  bloc `local.accord.enabled`), `folder` (`/v1/local/folder/*`, mode dossier, `local.folder_mode`).
- Détail : `local` (Lokkal), `speech` (voix web, `/v1/speech` donne le détail, dont `terms_outdated`), `pay_agent`
  (`/v1/payments/method*`), `approvals` (`/v1/approvals*`, `/v1/devices*`), `agents` (`/v1/agents*`), `threads`
  (`/v1/threads*`, `/v1/bubbles*`), `turn_cost` (événement `turn.cost`, docs/LOKKAL.md §7.8, avec `cost_chf`), `share`
  (export et liens partagés).
- Jamais : identifiant, réglage, quota, clé, ni rien d'un autre locataire. Aucun appel modèle, aucune écriture.

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

Fil connu mais vide : `GET /v1/threads/{id}`, `…/bubbles` et `…/export` rendent `200` (`messages: []`, `bubbles: []`) pour une session de code ouverte ou un tour rangé sans ligne de fil (lecture pure : rien n'est créé), **pour le propriétaire local de portée `owner` seulement** (jeton local, session d'accès `owner` ou jeton relayé du propriétaire ; la session de code ne mémorise pas d'autre propriétaire). Une session d'accès `present` ou `kiosk` (ou toute autre portée), un autre utilisateur, un membre du foyer et tout fil inconnu ou d'un autre locataire : le même `404`, au même corps. Un fil avec ligne au hub reste réservé à ses membres.

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

- un message `user` porte `"origin": "approval"` quand c'est le bloc `[Approbation]` écrit par le noyau pour la
  suite d'une carte décidée (docs/APPROVALS.md §2) : à afficher comme un repère du système, pas comme une bulle de la
  personne. Absent pour un message de la personne.
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

### Pièces texte faites sur l'appareil (P9 : OCR, transcription, dictée)

Dans le même `attachments`, sans `type` mais avec `kind: "text"` :
`{kind: "text", source: "device_ocr"|"device_stt"|"device_dictation", name, text, lang?, pages?, duration_s?}`.
Toujours acceptées (pas de drapeau). 4 pièces au plus et 20 000 caractères de `text` au total : au-delà
`400 {"error": {"code": "too_large", "message"}}` ; forme invalide (source inconnue, `text` non chaîne, `pages` < 1,
`duration_s` < 0) : `400 {"error": {"message"}}`. Rien ne tourne sur un refus.

- `device_ocr` → bloc `[Texte reconnu : …]` de `[Pièces jointes]` ; `device_stt` (enregistrement joint) →
  `[Transcription : …]` ; contenu caviardé, dans la clôture commune (`<untrusted-data kind="attachment:ocr|transcript">`),
  format exact docs/CONTEXT-FORMAT.md. Pièces de l'appareil après les autres pièces texte.
- `device_dictation` (note dictée qui EST le message) : son texte devient le texte du message de la personne (après
  `content` s'il y en a, séparé par une ligne vide), sans clôture.
- Rangées avec le message : `GET /v1/threads/{id}` → message `user` `attachments: [{id: "att_<16 hex>", kind: "text",
  source, name, chars}]` (dictée comprise) ; texte (caviardé) relu par `GET /v1/threads/{id}/attachments/{att_id}` →
  `{id, kind, source, name, chars, text, lang?, pages?, duration_s?}` ; membres du fil seulement, `404` si ce fil ne la
  porte pas. Effacées avec le fil ou le compte (persistées dans le tour).

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

**`mcp.server_unavailable {thread_id, turn_id, server, reason}`** : un serveur MCP ne répond pas ; ses outils (et seulement les siens) sont retirés pour ce tour, les autres continuent, et le tour suivant le retente. `reason` : classe de l'erreur et début du message, sans adresse. Le modèle reçoit une seule ligne dans l'index des outils.

**`route.model_mismatch {thread_id, turn_id, tier, model_expected, model_served, call_id, upstream?}`** : le modèle servi par l'amont (en-tête `X-Model-Served` du Router, sinon champ `model` du corps pour un amont direct ; `upstream` : id d'amont, seulement s'il est connu) n'est pas celui du palier, par exemple un routeur qui a basculé sur un repli. Information seulement, le tour continue ; une fois par (palier, modèle servi) et par tour ; comparaison insensible à la casse, au préfixe de fournisseur et au suffixe de date (`claude-sonnet-5-5-20261101` = `claude-sonnet-5-5`) ; rien sans modèle configuré sur le palier. La valeur servie est aussi au ledger (`model_served`), voir docs/HOST-EMBED.md §11.

**`route.fallback {thread_id, turn_id, from, to, reason}`** : le palier demandé (`from`, par exemple `deep`) n'a pas répondu et le noyau a répondu avec `to` (`ship`) pour cet appel. `reason` est un motif court et fermé (classe de panne en anglais + statut HTTP : `service unavailable (HTTP 502)`), jamais le texte de l'amont : ni adresse, ni jeton, ni trace. Une fois par (tour, from, to), même si plusieurs appels du tour se replient. Jamais pour un quota atteint (pas de repli), ni pour un tour BYOK, ni pour un tour Ship (il n'y a pas de repli Ship vers Deep). Information seulement : un observateur qui lève ne casse pas le tour. Le flux OpenAI de `chat/completions` ne change pas : l'événement est sur ce canal seulement. Le verdict `router:fallback` du ledger est inchangé.

**`mcp.required_unavailable {thread_id, turn_id, servers: [{server, reason}]}`** : un serveur MCP déclaré INDISPENSABLE par la session (`mcp_required`, docs/HOST-EMBED.md §24) ne répond pas : le tour échoue avant le modèle (503 `mcp_required_unavailable` sur chat/completions, `turn.done` en erreur dont le message, pour la personne, ne nomme aucun serveur ; les noms sont ici).

**`turn.text_toolcall_retry {thread_id, turn_id, kind}`** : le modèle a écrit un appel d'outil en TEXTE (réponse sans `tool_calls` dont le contenu entier commence par `[tool_input]` ou `[Appel de l'outil : `) ; le noyau n'exécute ni n'interprète ce texte, ne le garde pas dans l'historique et relance le même appel UNE fois par tour (la relance compte dans `max_calls`). `kind` : `tool_input` | `appel_outil`. Jamais le contenu. Une fois par tour, information seulement ; verdict `loop:text_toolcall` au ledger. Au second cas du même tour, la réponse est traitée normalement.

**`turn.truncated {thread_id, turn_id, reasoning, retries}`** : la réponse du modèle a été coupée par la limite de longueur (`finish_reason: "length"`) et les relances du noyau (`retries`, 2 par défaut) sont épuisées. `reasoning: true` : la réflexion était active, le texte coupé (qui peut être la fin du raisonnement) n'est jamais affiché, le tour échoue (`turn.done.error.code` = `length_truncated`, message pour la personne : « La réponse a été interrompue : elle était trop longue. Reformule ou découpe ta demande. », dans la langue du tour). `reasoning: false` : le texte coupé est affiché suivi de « (réponse interrompue : trop longue) » (traduit), sans échec. Une fois par tour, information seulement, jamais bloquant ; voir docs/HOST-EMBED.md §27.

**`turn.started {thread_id, turn_id, user_message?, truncated?}`** : début de tour, émis dès que le tour est accepté, avant le rappel, le contexte et tout appel du modèle (donc AVANT `tool.call`, `turn.delta` et `turn.done`) ; tout tour a un début ET une fin, y compris un tour refusé par un plafond ou un tour qui échoue avant le modèle (`mcp_required` : `turn.started`, puis `mcp.required_unavailable`, puis `turn.done` en erreur). `user_message` : le texte de la personne tel que le fil le garde (même caviardage que l'historique relu et que `turn.done` ; un secret masqué dans le fil l'est ici), pour qu'un AUTRE client branché sur le même fil l'affiche pendant le tour. Borné à 16 Kio en UTF-8, coupé sur une frontière de caractère, avec `truncated: true` ; absent sinon. Pièces jointes : aucun contenu, rien (leurs références restent dans l'historique). Champ absent pour un sous-agent (fil `<fil>/sa-…`), pour un tour écrit par le noyau (suite d'une carte décidée, page jointe, suite d'une tâche : `origin` non vide dans l'historique) et pour un message vide. Mêmes destinataires que les autres événements du fil, jamais un autre fil ni un autre locataire. Champs ajoutés, rien d'autre ne change ; un observateur qui lève ne casse pas le tour.

## Fin de tour : `turn.done` (champs du 01.11, branche `p2/fixes-e2e`)

`turn.done {thread_id, turn_id, usage, tier?, cost_chf?}` gagne trois champs, **présents seulement quand ils
s'appliquent** (absents sinon) :

- **`fallback: {from, to}`** (additif) : le tour a été servi par un autre palier que celui demandé (`{"from": "deep", "to": "ship"}`). **Absent** sans repli, et absent si Ship a échoué aussi après le repli (`tier_unavailable` : il n'y a pas de réponse).
- **`notice: string`** (nouveau champ, additif) : une ligne d'état POUR LA PERSONNE, dans la langue du tour (« Réponse en mode allégé : le mode approfondi est indisponible pour le moment. »), que le client affiche À PART. Présente seulement si le tour devait être profond (palier Deep au départ, demandé par la politique ou l'escalade) et qu'il s'est replié sur Ship ; absente pour un tour Ship, BYOK, un quota, ou un échec complet. Ce n'est jamais du texte de la réponse : ni dans `turn.delta`, ni dans le flux OpenAI, ni dans l'historique relu, ni dans le cache, ni vue du modèle.
- **`error: {code, message}`** — le tour a échoué. `message` est caviardé, ≤ 500 caractères. Codes :
  `tier_unavailable` (aucun palier n'a répondu, repli compris : la réponse du tour est l'escalade qui le dit) ;
  `provider_error` (l'appel de conclusion forcée a échoué chez l'amont) ; `max_calls` (limite d'appels sans
  conclusion) ; `stopped` (arrêté par le noyau, `stop_check`) ; `internal_error` (exception dans la boucle, tour
  enregistré sans réponse) ; **`quota_exceeded` (19.11)** : le quota d'inférence est atteint (l'amont a répondu 403 ou 429
  avec le corps `{"error": {"code": "quota_exceeded"}}`). Ce n'est pas une panne : un seul appel, ni repli de palier,
  ni rejeu du fournisseur, ni escalade ; la réponse du tour est la phrase « Quota d'utilisation atteint. Il se renouvelle {quand}. Tu peux aussi passer à une formule supérieure depuis ton compte. » (fr/en/de/it, `{quand}` : « le 19 novembre à 13:00 (heure suisse) », avec l'année si l'échéance est dans une autre année ; sans date : « … se renouvelle bientôt. … »), jamais « ne répond pas » ni « réessaie ». `error` porte alors
  **`retry_at`** : ISO 8601 UTC (`2026-11-19T12:00:00Z`), la fin de la période du quota, **absent** quand l'amont ne la
  donne pas (jamais inventé). Même `error` dans `nokker.error` de la réponse HTTP d'un tour (`host_extension`). Un
  corps illisible (non JSON, `error` en chaîne, `code` absent) retombe sur la classification par statut d'avant
  (403 : « accès refusé », 429 : « limite atteinte »). Un tour annulé, refusé par un plafond du noyau (quota local,
  budget de session) ou escaladé par une porte n'a PAS d'`error` (sauf les codes `session_budget_*`).
- **`refused: true`** — au moins un effet a été refusé d'office pendant le tour : refus d'une porte d'action (règle
  `deny`, mode plan, commande « hardline », invariant comme l'envoi de masse), ou carte posée par le tour et déjà
  rejetée à sa fin (le client sans surveillance `nokker -p` rejette toute carte). Limite : une carte rejetée APRÈS
  `turn.done` n'y figure pas (le client `-p` la connaît de toute façon). Le motif d'un refus est sur le `tool.result`
  de l'appel (`reason`, docs/LOKKAL.md §7.4.2).
  **`status: "refused"` (17.11)** : le `tool.result` d'un refus de porte (et la réponse de `tools/call`) porte en plus
  `status: "refused"` et `reason` (le code de la règle, ou son nom quand elle n'a pas de code), champs EN PLUS : `ok` et
  `is_error` de l'événement gardent leur valeur d'avant. Le MODÈLE, lui, reçoit toujours le même texte « Refusé : … » avec
  `is_error=False` (un refus n'est pas un échec d'outil : il ne fait pas monter le palier et ne compte pas comme erreur
  répétée) ; l'hôte lit `status`, pas `ok`.
  **`read_file` d'un dossier (18.11)** : `status: "ok"`, texte listant au plus 50 entrées visibles (voir HOST-EMBED) ; hors racine : `refused`/`outside_root`.
  **`reason: "outside_root"` (17.11)** : même champ pour un REFUS DE L'OUTIL, pas d'une porte : un outil de fichiers
  (`read_file`, `search_files`, `write_file`, `patch`, `ssh_copy` avec `local_path`) dont le chemin sort du dossier de travail
  (`..`, chemin absolu hors dossier, lien symbolique qui sort ; backend `local` ; en docker, tout chemin absolu hors
  `/workspace` et `..` qui en sort, 18.11) rend `status: "refused"`, `reason: "outside_root"`. Ici le MODÈLE reçoit le texte et l'`is_error=true` d'avant
  (« REFUSÉ : … hors du dossier de travail »). Un chemin de l'hôte sous le dossier (traduit vers `/workspace`) n'est pas un
  refus. Limite : en docker, un lien symbolique DANS le montage qui pointe dehors n'est jugé qu'à la lecture
  (côté noyau) ; en écriture le contrôle est lexical. Le terminal n'est pas concerné (commandes jamais réécrites).
- **`cancelled: true`** — le tour a été arrêté par `POST /v1/threads/{id}/cancel` (jeton local ; corps `{"keep_partial": true}`
  facultatif, voir plus bas). Depuis le 01.11, l'annulation
  coupe aussi un appel modèle **en flux** au prochain morceau reçu (réponse HTTP de l'amont fermée) ; le texte partiel
  de cet appel n'est pas gardé (sauf `keep_partial`) : le client reçoit un `reset` puis la réponse finale « Annulé à ta demande. »
  (docs/LOKKAL.md §7.4.4).

Le texte vu par le modèle ne change pas : ces champs sont calculés côté noyau pour les clients.

**Annulation avec `keep_partial` (hôte embarquant le noyau).** `POST /v1/threads/{id}/cancel {"keep_partial": true}` → `202
{cancelled, approvals_cancelled, running, keep_partial: true}`. Le texte DÉJÀ streamé de l'appel modèle en cours n'est plus effacé :
la réponse du tour est ce texte, puis une ligne vide, puis `[Réponse interrompue à la demande de la personne.]` (langue du tour ; un seul texte, montré à la personne ET rejoué au modèle). Le client reçoit
`reset` puis le chunk final, qui reprend le texte ; l'historique rejoué au modèle porte la même réponse ; `turn.done` garde
`cancelled: true`. Sans texte déjà streamé, la réponse reste « Annulé à ta demande. ». Corps absent ou `false` : comportement
d'avant. Non booléen : `400 {"error": {"code": "bad_keep_partial"}}`. Détail : docs/HOST-EMBED.md §10.

## Sessions de l'hôte : champs d'ouverture et annonce (docs/HOST-EMBED.md §7 à §10)

`POST /v1/local/sessions {cwd, …}` (jeton local) accepte, en plus de `thread_id`/`resume`, `deny_tools`, `surfaces`, `budget`,
`mcp_headers` :

| Champ | Forme | Effet | Erreurs |
|---|---|---|---|
| `upstream_headers` | `{gate_session?, gate_org?, gate_class?}` chaînes opaques (1 à 256 car. de `[A-Za-z0-9._:@/+=~-]`) | envoyées en `X-Gate-Session` / `X-Gate-Org` / `X-Gate-Class` aux seuls paliers `tiers.<nom>.gate: true`, gardées en mémoire, jamais journalisées | `400 bad_upstream_headers` ; `503 no_gate_tier` (aucun palier `gate: true`) |
| `model` | nom d'un palier de `tiers` | ce palier sert tout le fil, sans repli ; `model` dans la réponse 201 | `400 unknown_model` avec `error.valid` (noms valides) |
| `deny_tools` | noms d'outils : natif = nom nu, MCP = `<serveur>__<outil>` | retirés du fil ; un nom inconnu est accepté | réponse 201 : `unknown_denied: [...]` (présent si `deny_tools` est donné) |
| `lang_hint` | booléen strict, `true` par défaut | `false` : pas de ligne de langue dans l'en-tête du tour (HOST-EMBED §18) ; `lang_hint: false` dans la réponse 201 quand il est posé ; appel relayé : ignoré | `400 bad_lang_hint` |
| `memory` | `"off"` ou `"host"` | aucun rappel, aucune note apprise (les consignes du dépôt restent), outils `memory_*` retirés ; `memory` dans la réponse 201 | `400 bad_memory` |

`POST /v1/local/sessions/{fil}/announce {"text": "…"}` → `202 {thread_id, queued: true, pending, running}` : l'événement entre dans
le contexte du prochain tour sans en déclencher un (file si un tour tourne). `404 session_not_found`, `400 bad_text`, `409
announce_queue_full`. `PUT /v1/local/sessions/{fil}/mode` : le mode réglé survit au redémarrage du noyau (relu à la reprise du fil).

## Langue d'un tour (fr/en/de/it)

La langue d'un tour (`TurnInput.lang`) fixe la langue de TOUT ce que le noyau écrit au modèle ou à la personne : textes fixes,
notes système, suite d'une carte, bloc `[Environnement]`, en-tête de date. Ordre de résolution, le premier qui répond gagne :

1. la requête : `?lang=`, puis `Accept-Language` (ou `language_code` d'un canal) ;
2. la langue du compte (`account.lang`), quand la personne en a posé une (un Lokkal n'en a pas) ;
3. la langue DÉTECTÉE dans le message de la personne (`nokker/core/lang_detect.py` : mots-outils fr/en/de/it, au moins deux
   mots propres à la langue et deux fois plus que la suivante). Un message court ou ambigu (« ok », « oui », « merci ») ne dit
   rien : il ne fait jamais basculer. Les fragments de COMMANDE ne comptent pas comme prose (comme le code entre backticks) : la partie après « : » qui commence par un nom de commande (`ls`, `git`, `mkdir`…), les chemins, les options (`-p`, `--version`), les identifiants à plusieurs tirets, `&&`/`|` ; « dir » reste un mot allemand dans une phrase allemande. Un tour de contrôle (`metadata.cli_control`) n'est pas détecté ;
4. la dernière langue reconnue sur le fil (`Turn.lang_seen` : requête ou message détecté, jamais un repli) ;
5. la langue de l'installation (`NOKKER_LANG`) ;
6. `fr`.

L'en-tête de date du tour suit cette langue : fr `[Date et heure actuelles]` « mercredi 18 novembre 2026, 14:05 », en
`[Current date and time]` « Wednesday, November 18, 2026, 14:05 », de `[Aktuelles Datum und Uhrzeit]` « Mittwoch, 18. November
2026, 14:05 », it `[Data e ora attuali]` « mercoledì 18 novembre 2026, 14:05 », puis « (Europe/Zurich) — ISO … » ; balise
fermante `[/Date]` fixe. Il est figé par tour : un changement de langue ne réécrit pas les tours déjà envoyés.

Une personne qui change de langue en cours de fil est suivie (le message courant prime sur le fil) ; un fil déjà établi
dans une langue la garde pour un message court, quelle que soit l'installation. Seules les langues des points 1 et 3 sont
mémorisées sur le fil.

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

`approval.requested` {approval} · `approval.decided` {approval_id, decision, by, via[, decision_reason]} — plus les événements fils,
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


## Prompt système (`role: system`)

`POST /v1/chat/completions` lit comme tour le DERNIER message `user`. Les messages `role: system` du corps ne sont plus jetés :

- **Appelant hôte** (jeton local, ou clé d'API déclarée dans la config `api_keys`, arrivé en direct) : leurs textes, concaténés dans
  l'ordre (deux sauts de ligne), deviennent le prompt système de la session (le fil `X-Session-Id`, identique au `thread_id` d'une
  session ouverte par `POST /v1/local/sessions`). Posé si aucun ; s'il diffère de celui déjà posé, il le remplace pour les tours
  suivants (le préfixe change : une ligne d'avertissement au journal, sans le texte). Sans `role: system`, le prompt posé reste.
  Même plafond que `system_prompt` (`local.system_prompt_max_bytes`, 128 Ko par défaut) : au-delà, **400** `bad_system_prompt`.
- **Tout autre appelant** (jeton de session d'un compte, web d'un testeur, web relayé, serveur ouvert) : `role: system` IGNORÉ, une ligne
  de journal (jamais le texte), pas de 400 : un client générique peut en envoyer un sans effet.
- Ordre dans le message système envoyé au modèle : préfixe du noyau (invariants compris), prompt de l'hôte, index des outils. Le champ de
  session `system_prompt` de `POST /v1/local/sessions` est détaillé dans `docs/HOST-EMBED.md` §12.
