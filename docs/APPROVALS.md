# Approbations fortes — contrat (P1, 18.10.2026)

> Implémenté : `nokker/core/ports.py` (types + Protocol `Approvals`), `nokker/approvals/` (logique de
> preuve, backends, exécuteur, notificateur), `nokker/api/server.py` (routes HTTP), `nokker/qa/invariants.py`
> (`Verdict.strength`). Branche `p1/approvals`. Aligné avec la Mini App Telegram (`p1/miniapp`,
> `nokker/api/miniapp/`) et le chantier clients (`nokker-apps` : SDK, CLI, web, faux serveur) — §3/§5
> portent les noms RÉELS, pas des hypothèses.

Doctrine produit (17.10) : Nokker a tous les droits, l'humain reste maître de sa demande et de ses conséquences. Il n'y a
pas de classifieur bloquant. Une action à effet que les invariants mettent en `HITL` n'est **jamais exécutée** sans une
approbation **prouvée cryptographiquement** par un humain autorisé (un opérateur de la config, ou son suppléant s'il est injoignable). L'avis de
Nina (risques, sauvegarde, retour arrière) accompagne la carte et ne bloque rien.

## 1. Niveaux

| Niveau | Preuves acceptées | Effets |
|---|---|---|
| `strong` | clé d'appareil libérée par biométrie : Mini App Telegram (BiometricManager, signature + `initData` vérifié), WebAuthn/passkey (Touch ID, Face ID, Windows Hello) **avec flag UV**, app native (Secure Enclave / StrongBox) — une signature d'appareil SANS cette preuve biométrique (clé logicielle CLI : trousseau ou fichier 0600, sans `initData` Telegram) est refusée (`ProofInvalid`, durcissement du 18.10.2026) | `DESTRUCTIVE` |
| `standard` | `strong` **ou** TOTP (web sans passkey) **ou** simple signature d'appareil par une clé logicielle (CLI, sans biométrie) | `SEND_EXTERNAL` hors allowlist, `SPEND` |

`SEND_BULK` reste **toujours `DENY`** (décision produit du 17.10, `qa/invariants.py`) — jamais soumis à une approbation,
donc jamais de `strength` associée. Le niveau vient du `Verdict` (champ `strength`, posé par
`DeterministicInvariants.check_action`) ; un GO textuel dans le message ne remplace jamais une approbation `strong`.

## 2. Cycle de vie

`pending` → `approved` | `rejected` | `expired` → (`approved`) `executed` | `failed`

- **Demande** (porte action, `Decision.HITL`, `Kernel._tool_step`) : si un port `Approvals` est branché (par défaut,
  `NokkerConfig.approvals_enabled=True`), le noyau appelle `approvals.request(turn, call, verdict, advice)` — enregistre
  l'appel EXACT (`tool_name`, `arguments`), son empreinte `args_sha256` (JSON canonique de `arguments` SEUL : clés
  triées, sans espaces, UTF-8 — `nokker.approvals.crypto.args_sha256`), `strength` (`Verdict.strength`), `rule`/`reason`
  (`Verdict.rule`/`.reason`), `advice` (avis court et niveau de risque du modèle relecteur sur une carte forte — `nokker/qa/review.py`, derrière
  `judge_advice`, éteint par défaut — `None` sinon ; il conseille, il ne décide jamais), `expires_at` (`created_at + 24h`, fixe pour l'instant — pas de
  différenciation par effet). Le notificateur pousse la carte (`nokker.approvals.notify.TelegramNotifier`, désactivé
  par défaut : `sendMessage` + bouton `web_app` vers `<telegram_mini_app_url>?startapp=<approval_id>`).
- **Décision** : le client demande un défi (§3), le signe/le prouve, et renvoie la preuve. `Approvals.decide()`
  vérifie la preuve, l'utilisateur autorisé (`GoAuthorizedSenders`, même liste qu'un GO textuel), le niveau requis,
  l'expiration, le nonce (usage unique) et que `args_sha256` n'a pas changé depuis la demande — lève
  `nokker.approvals.errors.{ProofInvalid,ApprovalExpired,ApprovalNotPending,ApprovalNotFound}` sinon, jamais un
  « approuvé » dégradé en silence.
- **Exécution** (`nokker.approvals.executor.ApprovalExecutor.run`, appelé par la route HTTP juste après un `decide()`
  réussi — PAS par `decide()` lui-même, qui ne fait que vérifier la preuve) : recalcule `args_sha256` en défense en
  profondeur, appelle l'outil via le registre du noyau (`ToolContext` du fil d'origine, `sender=decided_by`), passe le
  résultat par `redact_result`, pose le statut `executed`/`failed` (`mark_executed`/`mark_failed`), puis lance un
  **tour de continuation** sur le même fil (`Kernel.run_turn`, `TurnInput.user_text` = le bloc ci-dessous, pas de
  message humain séparé — `docs/CONTEXT-FORMAT.md` place les blocs du tour [Date/Rappel/Outils] avant « le message »,
  qui est ici directement ce bloc) :
  ```
  [Approbation]
  Outil : <tool_name>
  Décision : executed|failed|rejected
  Décideur : <decided_by>
  Moyen : signature|signature+telegram|telegram-attested|webauthn|totp|rejected
  Résultat : <contenu caviardé, tronqué à 2000 caractères>
  [/Approbation]
  ```
  Un refus produit le même tour de continuation, sans appel d'outil (`Résultat : refusé par <decided_by>.`).
  **Tour du noyau, pas de la personne (03.11)** : l'exécuteur pose `TurnInput.control = "approval_continue"`
  (`core/turn.py::APPROVAL_CONTINUE`, jamais accepté d'un client : `metadata.cli_control` n'admet que
  `goals.service.CONTROLS`). Ce bloc redit une décision déjà prise sur la carte : il ne lève AUCUN drapeau de domaine
  (sans quoi le montant du résultat de `pay_request` escaladait la suite, `domaine:montant`), et le tour porte
  `InputCheck.origin = "approval"`, exposé en `origin` sur le message `user` de `GET /v1/threads/{id}` pour que les
  clients ne l'affichent pas comme un message de la personne. Le même bloc TAPÉ par quelqu'un reste un message
  ordinaire (drapeaux comme avant).
  **File par fil (25.10, docs/LOKKAL.md §7.4.9)** : l'outil et le tour de continuation passent par la file du fil,
  après le tour en cours (jamais en parallèle : même index, contexte perdu) ; la sortie de l'outil est publiée en
  `tool.result` (même `call_id` que l'appel en attente, `approval_id`) avant la continuation. La décision locale
  répond tout de suite (`202`, `continuation`) ; cette route-ci attend encore la fin de l'exécution.
- **Journal** : chaque transition est append-only (table `approval_events`, `_append_event`). Rien n'est effacé.

## 3. Preuve

Trois chemins, un seul `device_id` enrôlé par appareil (CLI, Mini App Telegram ET web partagent le MÊME enrôlement) :

### 3.1 Enrôlement d'appareil

`POST /v1/devices` `{"public_key_spki": "<SPKI DER, base64url SANS padding — base64 standard accepté en repli>",
"name": "...", "init_data": "<initData Telegram, si c'est la Mini App qui enrôle>"}` → `201`
`{"id", "code", "name", "user", "status": "pending", "created_at", "approved_at": null, "approved_via": null}`
(§5 pour le détail des champs). `code` = identifiant COURT (6 car. hex), pour la confirmation humaine.

L'appareil reste `pending` jusqu'à confirmation par un moyen déjà de confiance :
- `python -m nokker devices approve <code>` sur l'hôte (`nokker/cli.py` → `Approvals.confirm_device_cli`) ;
- ou `POST /v1/devices/{id}/confirm` `{"totp": "123456", "user": "alice"}` (TOTP déjà enrôlé côté serveur via
  `Approvals.enroll_totp_secret`, hors API — provisionné par un outil d'exploitation, jamais relu/renvoyé).
  Le TOTP doit être celui du **propriétaire** de l'appareil (`device.user`).

### 3.1bis Enrôlement d'un navigateur sans identité préalable (décision produit du 18.10.2026)

Un navigateur neuf n'a ni clé d'API, ni session, ni initData. Il envoie `POST /v1/devices` sans preuve avec en plus
`"user": "alice"` (utilisateur DÉCLARÉ, qui doit être un utilisateur autorisé — `GoAuthorizedSenders`). L'appareil
est créé `pending` pour cet utilisateur, sans lien Telegram, et le code est journalisé côté serveur. Il ne donne
AUCUN droit (pas de session possible) tant qu'il n'est pas confirmé par `devices approve <code>` sur l'hôte ou par
le TOTP de CE propriétaire (§3.1). Débit borné : 10 enrôlements sans preuve par 10 min, tous clients confondus.
Une fois confirmé, son jeton de session (§3.6) vaut identité sur toute l'API, `/v1/chat/completions` compris.

### 3.2 Défi (approbation)

`POST /v1/approvals/{id}/challenge` (identité résolue requise, §5) → `200` `{"challenge", "nonce", "exp"}` où
`challenge` = JSON canonique **exact** à resigner tel quel :
`{"approval_id","args_sha256","aud":"nokker-approval","exp","nonce"}` (clés triées). TTL 120 s, nonce à usage unique
(table `used_nonces`).

### 3.3 Décision — signature brute (CLI, Mini App Telegram)

`POST /v1/approvals/{id}/decision` `{"decision": "approve"|"reject", "device_id", "challenge", "signature",
"init_data"?}` — `signature` : ECDSA P-256/SHA-256, base64url, **IEEE P1363** (WebCrypto natif : r‖s, 64 octets bruts,
PAS un DER ASN.1). La décision est **incluse dans la signature** : on signe `challenge + "." + decision`.
`init_data` (Mini App) : validé EN PLUS (HMAC-SHA256, clé dérivée `HMAC_SHA256("WebAppData", jeton du bot)`,
`auth_date` < 10 min), `user.id` doit correspondre à `device.telegram_user_id` posé à l'enrôlement — `decided_via`
devient alors `"signature+telegram"` au lieu de `"signature"`. Pour une approbation `strength == "strong"`, seul
`"signature+telegram"` (ou WebAuthn/passkey avec UV, §3.4) est accepté : une simple `"signature"` (clé logicielle
CLI, sans `init_data`) lève `ProofInvalid` — elle ne vaut que pour `strength == "standard"`.

### 3.4 Décision — WebAuthn/passkey (web)

ADR du chantier clients : `nokker-apps/docs/adr/0001-webauthn-vs-ecdsa-contrat.md` (le navigateur ne laisse
manipuler aucune clé privée biométrique directement ; WebAuthn impose sa propre forme de signature). Variante
retenue, validée côté noyau : `{"decision", "device_id", "challenge", "webauthn": {"client_data_json",
"authenticator_data", "signature"}}` (les trois champs WebAuthn en base64url). Le `challenge` WebAuthn passé à
`navigator.credentials.get` — et donc retrouvé décodé dans `clientDataJSON.challenge` — est
**`SHA-256(challenge + "." + decision)`** (32 octets bruts), PAS le texte du défi lui-même : la décision reste ainsi
cryptographiquement liée à la signature, juste transportée différemment (évite qu'un refus intercepté soit rejoué
comme une approbation). Le serveur (`nokker.approvals.crypto.verify_webauthn_assertion`) vérifie dans l'ordre :
`clientDataJSON.type == "webauthn.get"`, `clientDataJSON.origin == NokkerConfig.webauthn_origin` (`None` = WebAuthn
refusé — pas de vérification sans origine légitime connue), `clientDataJSON.challenge` décodé == l'empreinte
attendue ci-dessus, le flag **UP** (user present) toujours exigé, le flag **UV** (user verified) exigé SEULEMENT pour
`strength == "strong"`, puis la signature — **DER** (format natif WebAuthn, PAS de conversion P1363→DER côté
serveur : c'est l'inverse, la signature WebCrypto brute du §3.3, qui est en P1363) — sur
`authenticatorData ‖ SHA-256(clientDataJSON)` avec la clé publique SPKI enrôlée (même routine d'enrôlement que la
CLI : `response.getPublicKey()` rend déjà du SPKI DER côté client). `decided_via` devient `"webauthn"`.
**`rpIdHash`** (sur TOUS les chemins : `decide`, `decide_remote`, le relais de nokker.ai et la connexion
`create_session`) : les 32 premiers octets de `authenticatorData` doivent valoir `SHA-256(rpId)`, où `rpId` est le nom
d'hôte de l'origine configurée (`urlsplit(webauthn_origin).hostname`) — une assertion valide mais produite pour un
autre `rpId` est refusée ; une origine sans hôte refuse WebAuthn. **`signCount`** (octets 33..36 de
`authenticatorData`, big-endian) : le dernier compteur vu est retenu par appareil (`devices.sign_count`, `BIGINT`, en
mémoire ou Postgres) et un compteur qui ne le dépasse pas strictement est refusé (« compteur de signature non
croissant : clé possiblement clonée »), connexion comprise. **Limite** : un compteur toujours à 0 (passkeys
synchronisés iCloud/Google) est accepté et n'offre donc aucune protection anti-clonage ; l'appareil d'un compte lié que
nokker.ai rend à la machine (`decide_remote`) n'a pas de ligne locale, son compteur n'est tenu que par processus.
Safari non supporté côté client tant qu'il n'expose pas `getPublicKey()` (WebAuthn niveau 3).

### 3.4bis Décision d'une carte MACHINE dans la Mini App — attestation (26.10, lot X10)

Une carte forte déposée par une machine Lokkal liée (docs/LOKKAL.md §7.3) peut se signer dans la Mini App Telegram,
comme une carte du compte (§3.3), mais **la preuve Telegram ne va pas à la machine** : elle n'a pas le jeton du bot.
`POST /v1/link/machines/{m}/approvals/{id}/decision {decision, proof: {device_id, signature, telegram_init_data}}` ;
`signature` : P1363 sur `"<challenge>.<decision>"`, `challenge` = celui que la machine a déposé pour cette carte.
nokker.ai vérifie l'`initData` (HMAC du bot, âge), l'appareil (du compte qui possède la machine, `approved`, pas de
simple connexion, `telegram_user_id` = `initData.user.id`) et la signature, puis signe une **attestation** (JWT ES256,
clé de passerelle, `typ` `nokker-approval-attest`) : `{iss, aud: <machine_id>, sub: <compte>, approval_id,
challenge_sha256, args_sha256, decision, via: "telegram", device_id, iat, exp ≤ iat + 120 s, jti}`. La machine la vérifie
(JWKS du lien, `aud`, `sub`, carte, défi déposé, `jti` jamais vu, non expirée) et décide :
`BaseApprovals.decide_attested`, `decided_via` = `"telegram-attested"`. Réglage machine
`local.remote_approvals_attested` (vrai par défaut) ; faux = attestation refusée. Refusée aussi : cartes `ops:prod*`,
`pages:publish`, `household:guardian` et connecteurs relayés (clé d'appareil exigée). La machine fait donc confiance à
nokker.ai pour cette carte (courte, à usage unique) ; une passkey (§3.4), vérifiée par la machine, n'en dépend pas.

### 3.5 Décision — TOTP (niveau `standard` SEULEMENT)

`{"decision", "challenge", "totp": "123456", "totp_user": "alice"}` (alias accepté côté API : `"user"`) à la place de
`device_id`/`signature` — fenêtre ±1 pas de 30 s (RFC 6238), code non réutilisable (`last_step` par utilisateur TOTP,
`totp_secrets.last_step`). Levée `ProofInvalid` si présenté pour une approbation `strength == "strong"`.

### 3.6 Session (connexion, pas une décision)

`POST /v1/devices/{id}/session-challenge` (aucune identité requise — c'est CE défi, une fois signé, qui EN établit
une) → `{"challenge", "nonce", "exp"}` (même structure que §3.2, `approval_id` interne = `"login:<device_id>"`,
`aud`="`nokker-login`"). Puis `POST /v1/sessions` `{"device_id", "challenge", "signature"}` (signe
`challenge + ".login"`, IEEE P1363) **ou** `{"device_id", "challenge", "webauthn": {...}}` (même structure que §3.4,
challenge WebAuthn = `SHA-256(challenge)` SANS suffixe — une connexion n'a pas de « décision ») → `200`
`{"token", "user", "device_id", "expires_at"}`. Durée 12 h (`NokkerConfig`/`BaseApprovals.session_ttl_s`), pas encore
de renouvellement implémenté (refaire la danse de connexion à l'expiration).

## 4. Port

```python
class Approvals(Protocol):
    def request(self, turn: Turn, call: ToolCall, verdict: Verdict, advice: str | None) -> Approval: ...
    def get(self, approval_id: str) -> Approval | None: ...
    def pending(self, user: str | None = None) -> list[Approval]: ...
    def challenge(self, approval_id: str) -> Challenge: ...
    def decide(self, approval_id: str, decision: str, proof: Proof) -> Approval: ...   # lève si preuve invalide
```

Implémentations : `nokker.approvals.pg.PgApprovals` (prod, migration `nokker/approvals/migrations/0001_approvals.sql`
— tables `approvals`, `approval_events` (append-only), `devices`, `sessions`, `used_nonces`, `totp_secrets`),
`nokker.approvals.memory.InMemoryApprovals` (tests). Toute la logique de preuve (signature, WebAuthn, TOTP, Telegram,
anti-rejeu, anti-falsification) vit UNE SEULE FOIS dans `nokker.approvals.base.BaseApprovals`, dont les deux héritent
— jamais dupliquée. Enrôlement d'appareil/sessions/TOTP : méthodes supplémentaires du même backend (pas dans le
Protocol ci-dessus, qui ne porte que ce dont `Kernel` a besoin), consommées par `nokker/api/server.py`/`nokker/cli.py`.
Les tests de l'hostile model restent vrais : sans preuve valide, `tools.called == []`
(`tests/unit/qa/test_hostile_model.py`, section « approbations fortes »).

## 5. Forme JSON canonique (fixée pour les clients — `nokker/api/server.py::approval_to_json`/`device_to_json`)

`GET /v1/approvals/{id}` et chaque élément de `GET /v1/approvals?status=pending` et de la réponse de
`POST /v1/approvals/{id}/decision` :

```json
{
  "id": "…", "thread_id": "…", "turn_index": 0, "channel": "telegram",
  "tool_name": "infra_lvextend", "arguments": {"path": "/srv/share", "size": "50G"},
  "args_sha256": "…", "effect": "destructive", "rule": "effect:destructive", "reason": "…",
  "advice": null, "strength": "strong", "status": "pending",
  "created_at": "2026-10-18T10:00:00+00:00", "expires_at": "2026-10-19T10:00:00+00:00",
  "decided_by": null, "decided_via": null, "decided_at": null,
  "result_summary": null, "error": null
}
```

`effect` est DÉRIVÉ de `rule` (`"effect:xxx[:...]"` → `"xxx"`) pour l'affichage — ce n'est pas un champ propre
d'`Approval`. Dates toujours en ISO 8601 UTC (jamais un epoch nu). Forme canonique ; un client qui lisait déjà
plusieurs noms plausibles (`tool.name`/`tool_name`/`name`…) continue de fonctionner, mais c'est CETTE forme qui fait
foi désormais.

`POST`/`GET /v1/devices`, et la réponse de `POST /v1/devices/{id}/confirm` :

```json
{"id": "…", "code": "a1b2c3", "name": "…", "user": "alice", "status": "pending",
 "created_at": "…", "approved_at": null, "approved_via": null}
```

Jamais `public_key_spki` dans une réponse (inutile au client une fois l'enrôlement fait). `GET /v1/devices` renvoie
un TABLEAU JSON brut (pas `{"devices": [...]}`), filtré sur l'identité résolue de l'appelant.

`POST /v1/approvals/{id}/challenge` et `POST /v1/devices/{id}/session-challenge` : `{"challenge": "<JSON canonique,
chaîne>", "nonce": "…", "exp": 1760000000.0}`.

`POST /v1/sessions` : `{"token": "…", "user": "alice", "device_id": "…", "expires_at": "2026-10-19T…+00:00"}`.

### Identité (`Authorization`/`X-Telegram-Init-Data`/`init_data`)

Toute route `/v1/approvals*`/`/v1/devices*` (sauf `POST /v1/devices/{id}/session-challenge`, qui n'en a pas besoin)
exige une identité résolue, dans cet ordre : (1) clé Bearer statique (`api_keys`) ; (2) jeton de session
(`POST /v1/sessions`) ; (3) `initData` Telegram validé, résolu en `sender` via `NokkerConfig.telegram_ids`.
L'`initData` est accepté en en-tête `X-Telegram-Init-Data` (toute méthode) OU en champ `init_data` du corps JSON
(POST) — la Mini App (`approve.js`) envoie l'en-tête sur chaque appel et AJOUTE le champ du corps sur
l'enrôlement/la décision ; les deux voies sont vérifiées pour ne décevoir aucun des deux usages réels.

### Fichiers statiques de la Mini App (`/miniapp/*`)

Allowlist STRICTE (`nokker/api/server.py::MINIAPP_FILES`), servis depuis `nokker/api/miniapp/` (branche
`p1/miniapp`) : `approve.html`, `approve.js`, `approve-core.js`, `style.css`. Tout le reste — `package.json`,
`*.test.mjs`, `gen-signature-vector.mjs` (outillage de build/test du client, jamais un asset servi) — `404`, même
si le fichier existe sur disque.

## 6. Limites connues

- `advice` n'est rempli que sur une carte forte et seulement si `judge_advice` est allumé (éteint par défaut) :
  `Kernel._card_advice` appelle alors le modèle relecteur avant `approvals.request`, sinon `None`.
- `expires_at` est fixe (24 h) quel que soit l'effet — la mention « `SEND_BULK` 1 h » d'une version antérieure de ce
  document n'a jamais pu s'appliquer : `SEND_BULK` reste `DENY`, jamais soumis à approbation (§1).
- Le renouvellement d'un jeton de session (`POST /v1/sessions`) n'est pas implémenté : à l'expiration (12 h), le
  client refait la danse `session-challenge` → signature.
- Un `signCount` WebAuthn toujours à 0 (passkeys synchronisés) n'offre aucune protection anti-clonage (voir §3.4) ; Safari non supporté côté client.
- Le bus d'événements (`/v1/events`, `EventBus`) est en mémoire, un seul processus — pas de reprise si le serveur
  redémarre entre deux connexions (fenêtre de reprise bornée à `max_buffer`/`max_age_s`, pas un journal persistant).
- `PgApprovals` est câblée et testée par construction (même logique que `InMemoryApprovals`, voir §4), mais son
  SQL réel n'a été exercé qu'ponctuellement contre un Postgres jetable, pas en continu par la CI de ce chantier.

## 7. Règles suggérées (lot P2, `nokker/approvals/suggestions.py`)

Quand le même type de carte a été approuvé `threshold` fois **sans aucun refus** dans la fenêtre, le noyau propose au
propriétaire « toujours autoriser X » ; rien ne change tant qu'il n'accepte pas. Drapeau `approval_suggestions` de la
config : `{enabled: false (défaut), threshold: 5 (≥ 2), window_days: 30}` ; éteint, les routes répondent `404`.

- **Type de carte** = une règle candidate, celle que la décision locale proposait déjà (`suggested_rules`, docs/LOKKAL.md
  §7.4.3 : `terminal(npm test)`, `terminal(npm test*)`, `patch(src/**)`…). Une carte approuvée (`approved`, `executed`,
  `failed` après approbation) compte pour chacune de ses candidates ; une carte **refusée** bloque toutes les siennes.
  Seules comptent les cartes du locataire courant, **décidées par l'appelant** (`decided_by`), visibles de lui (session
  de code d'un autre : non). Une règle large n'est proposée que si elle couvre plus de cartes que la plus étroite déjà
  retenue ; une règle déjà présente dans les `allow` n'est pas reproposée. 20 propositions au plus.
- **Jamais proposées** (`suggestions.forbidden`) : carte `strong` (destructif), effet `destructive` ou `send_bulk`
  (`SEND_BULK` reste `DENY`, il n'a pas de carte), plancher prod `ops:prod*`, `pages:publish`, `household:guardian`,
  connecteurs relayés et tours venus du web (`permissions:relayed_*`), boîte mail locale
  (`permissions:local_connector_strong`), premier contact `ssh_trust`. `suggested_rules` ne rend déjà rien pour la
  plupart ; la liste est vérifiée en plus, carte par carte.
- **Rien n'est visible par le modèle** : ni outil, ni bloc de contexte, ni événement de fil.

Routes (identité résolue comme au §5 ; le jeton local vaut « propriétaire local ») :

- `GET /v1/approvals/suggestions` → `200 {"suggestions": [{"id": "sg_…", "rule", "tool", "approved", "rejected": 0,
  "approval_ids": [10 plus récentes], "first_at", "last_at"}], "rule_store": "user_settings" | null, "threshold",
  "window_days"}`.
- `POST /v1/approvals/suggestions/{id}/accept` → la règle est écrite par le **mécanisme existant** de la portée
  `always` (docs/LOKKAL.md §7.4.3) : `allow` des réglages de l'UTILISATEUR (`local.settings_path`), jamais le
  `.nokker/` du dépôt, et ajoutée tout de suite aux `allow` en vigueur. Provenance rangée à côté, hors de `permissions` :
  `rule_provenance[<règle>] = {source: "approval_suggestion", suggestion_id, approved, approval_ids, accepted_by,
  accepted_at}`, et dans la marque de la suggestion. `200 {id, state: "accepted", rule, provenance}`.
  Réservé au propriétaire de la machine (Lokkal, `rule_store` non nul) : ailleurs `409 {"code": "no_rule_store"}`. Les
  comptes multi-locataires n'ont pas de règles durables (docs/LOKKAL.md §7.28.7 : décision `once` seulement) ; ils
  voient leurs propositions mais ne peuvent pas les accepter tant qu'un tel magasin n'existe pas.
- `POST /v1/approvals/suggestions/{id}/dismiss` → masquée pour cet utilisateur (même id stable pour compte, utilisateur
  et règle : elle ne revient pas), `200 {id, state: "dismissed", rule}`. Rien n'est écrit dans les réglages.
- Id inconnu, plus valable (un refus est arrivé depuis, sortie de la fenêtre), ou d'un autre compte : `404`.

Stockage des marques : `InMemoryApprovals` (dict par locataire) ou Postgres, table `approval_suggestion_marks`
(migration `nokker/store/migrations/047_approval_suggestions.sql`,
RLS par `tenant_id`, effacée avec le locataire). Les propositions elles-mêmes ne sont jamais stockées : elles se
recalculent depuis `approvals` à chaque lecture (index `approvals_created_at_idx`).

Limites : le calcul relit les cartes de la fenêtre à chaque `GET` (pas de cache) ; une règle acceptée puis retirée à
la main des réglages n'est plus reproposée (sa marque `accepted` reste) ; la retirer aussi de la table, à la main.

## 8. Relecteur automatique des cartes `standard` (lot P2, `nokker/approvals/auto_review.py`)

Drapeau `auto_review` de la config : `{enabled: false (défaut), tier: judge | <palier de tiers>, policy: <texte>,
max_tokens: 120, trajectory_calls: 8, max_approvals_per_turn: 20, tz: Europe/Zurich, own_addresses: []}`. Éteint : aucune carte relue, aucun texte vu par le
modèle ne change, la route de révocation répond `404`.

Allumé, une carte que les portes posent au fil d'un tour (`Kernel._tool_step`) et que la **liste fermée** laisse au
relecteur (`auto_review.excluded`) n'est pas poussée tout de suite aux humains : le relecteur (un palier modèle) voit la
carte, la trajectoire récente du tour (appels précédents et début de leurs résultats) et la politique, et répond
`APPROUVER`, `REFUSER` ou `ESCALADER` avec une raison.

- **Approuver** : `decide_auto` → `approved` (`decided_by = decided_via = "auto-review"`), l'outil s'exécute dans le
  tour (le modèle voit son résultat, comme une action permise), puis `executed` | `failed`.
- **Refuser** : `rejected` ; le modèle voit `REFUSÉ par le relecteur automatique (<raison>) [approval:<id>]. L'action
  n'a pas été exécutée. L'utilisateur peut revenir sur ce refus ; ne la relance pas autrement.`
- **Escalader**, et tout doute (réponse hors format ou ambiguë, raison vide, relecteur en panne) : la carte reste
  `pending`, part aux humains, et le modèle voit le texte d'attente habituel.
- **Dates, heures et adresses avant tout appel** (déterministe, `nokker/approvals/dates.py`) : chaque date et heure des
  arguments (normalisées `AAAA-MM-JJ`, `HH:MM` ; décalage de fuseau ignoré ; heure de FIN d'un créneau non exigée, son
  jour si) doit se retrouver dans la demande, relatifs résolus dans `auto_review.tz` (défaut `Europe/Zurich`, le fuseau
  de la date donnée au modèle) : « aujourd'hui », « demain », « après-demain », « jeudi », « vendredi prochain »,
  « le 24 », « 24 octobre », `24.10`. Un jour de semaine qui est aujourd'hui sans « prochain » est ambigu : rien n'est
  résolu. Chaque adresse de courriel des arguments doit figurer dans la demande ou dans `auto_review.own_addresses`.
  Sinon : escalade sans appel au modèle.
- **Règles de trajectoire avant tout appel** (déterministes) : un secret lu plus tôt dans le tour, une action du tour
  déjà refusée ou escaladée, le plafond d'approbations du tour, ou une approbation automatique de ce même outil révoquée
  sur ce fil dans les 30 jours → escalade, sans appel au modèle.

**Liste fermée (jamais relue)** : carte forte ; `ops:prod*` ; effets `destructive`, `send_external`, `send_bulk`,
`spend`, `secret` (seuls `read` et `write` sont relus) ; règles `effect:*` de ces effets, `pages:publish`,
`household:guardian`, `permissions:relayed_*`, `permissions:local_connector_strong`, `permissions:go_sandbox_only`,
`permissions:hardline`, `ops:trust_host`, `account:delete`, `improve:proposal` ; outils `ssh_trust`, `account_delete`,
`forge_push`, `send_bulk_email`, `agent_arm`, `improve_apply` ; commande `terminal`/`process_start`/`ssh_run` qui supprime, publie ou envoie (`rm`,
`git push`, `git reset --hard`, `curl`, `scp`, `npm publish`, `kubectl delete`…). `decide_auto` revérifie la liste
(défense en profondeur) et lève `ProofInvalid`. Une carte d'auto-amélioration (`improve:proposal` / `improve_apply`,
docs/SELF-IMPROVE.md) n'est JAMAIS approuvée par le relecteur : en plus de la liste fermée, `review_text` l'escalade par
règle (`IMPROVE_ESCALATION`), sans appel au modèle.

**Clôture des données** : dans le prompt du relecteur, la demande, la trajectoire, les arguments et le motif de la carte
sont chacun dans un bloc `<untrusted-data kind=demande|trajectoire|arguments|motif id=…>` (`nokker/qa/untrusted.py`,
id = sha256 du contenu, balises internes neutralisées), annoncés comme des données. Une réponse qui porte plus d'une
ligne « DÉCISION » (un modèle qui recopierait une décision injectée) escalade.

**Trace** : `Approval.auto_review = {decision, reason, model, at, by_rule[, revoked_by, revoked_at]}` (colonne
`approvals.auto_review`, migration `051_approval_auto_review.sql`), événements `auto_review`,
`approved`/`rejected`, `auto_review_revoked` ; verdict `approvals:auto_review` au ledger ; jetons du relecteur comptés
au tour (« relecteur automatique : carte standard »). L'API rend `auto_review` dans la carte (absent si non relue).

**Révocation** : `POST /v1/approvals/{id}/auto-review/revoke` (identité résolue, propriétaire de la carte dans son
locataire) → la carte. Refus révoqué : la carte revient `pending` (ou `expired`) et part aux humains, qui la décident
par le chemin habituel (exécuteur, tour de continuation). Approbation révoquée : l'effet a eu lieu, la carte reste
`executed` et le dit ; le relecteur n'approuve plus cet outil sur ce fil. `409` si rien à révoquer ou déjà révoquée.

**Restauration après révocation** (`nokker/tools/card_snapshots.py`) : juste avant qu'une écriture de fichier approuvée
automatiquement (`write_file`, `patch`) s'exécute, l'enveloppe des points de retour copie chaque fichier visé (ou note
« fichier absent ») hors du bac à sable, puis note l'empreinte de chaque fichier après l'outil. Ce point PAR CARTE ne
dépend pas des points git (pris une fois par tour, absents sans la bibliothèque amont) ; il est limité au dossier de
travail (chemin résolu, lien symbolique refusé), à 5 Mo par fichier et 20 Mo par carte, gardé 30 jours. Lokkal :
`<état>/checkpoints/cards/<carte>/` ; sessions de code multi-utilisateur : `<home des points de la session>/cards/`.

**Pas de point de retour, pas d'approbation automatique d'écriture** : avant de relire une carte `write_file`/`patch`,
le noyau vérifie que le point est possible (`card_snapshots.preflight` : fichier trop grand, hors du dossier de
travail, lien symbolique, aucun magasin pour le fil). Sinon la carte reste `pending` pour l'humain, escaladée par
règle sans appel au modèle ; la raison est rangée dans `auto_review.reason` de la carte, le modèle voit le texte
d'attente habituel.

- La réponse de révocation d'une approbation porte `rollback: {available, checkpoint_id, files, reason?}`
  (`available: false` et la raison si le point manque, est incomplet ou a déjà servi). Rien n'est restauré à la
  révocation.
- `POST /v1/approvals/{id}/auto-review/rollback` (même identité, approbation DÉJÀ révoquée) applique le point :
  `{approval_id, checkpoint_id, restored, conflicts}`. Un fichier modifié depuis l'écriture (empreinte différente de
  celle d'après l'outil) n'est pas écrasé et figure dans `conflicts`. Une seule application ; `409` sinon (pas
  révoquée, pas de point, point incomplet, déjà appliqué). Chaque restauration est inscrite au journal des
  approbations (`record_rollback` : événement `auto_review_rollback` avec qui, quand, `checkpoint_id`, `restored`,
  `conflicts` ; `approval.auto_review_rollback` sur le bus).
- `GET /v1/approvals/{id}` (et la liste) porte le même `rollback` pour une approbation automatique révoquée, avec
  `applied` : `false` tant que rien n'est restauré ; après la restauration `applied: true`, `applied_at`, `restored`,
  `conflicts` (et `available: false`).
- **Bulle du fil** : une approbation automatique ouvre dans le fil une bulle `approval` (docs/BUBBLES.md : état
  approuvé, `decided_via: "auto-review"`, raison), mise à jour à la révocation et à la restauration. Pas de
  notification push ; aucun texte ajouté au tour.
- Les autres écritures (`write` hors fichiers : agenda, tâches…) n'ont pas de point de retour : `available: false`.

Mesure : banc B-1 (`nokker-judge-bench/auto_review_eval.py`, hors du dépôt), 01.11.2026, 49 cartes (21 fautives). Sans
le contrôle des dates : 3 faux « approuver » (qwen3.6-35b-a3b). Avec le contrôle des dates et adresses :
critère bêta = 0 faux « approuver », atteint sur les deux paliers mesurés :

| palier | accord (49) | faux « approuver » | justes approuvées (28) | escalades |
|---|---|---|---|---|
| qwen3.6-35b-a3b (llama.cpp, sans raisonnement) | 83,7 % | 0 / 21 | 20 | 24 |
| Ship du Router `qwen/qwen3.8-27b` (`reasoning_effort: none`) | 63,3 % | 0 / 21 | 10 | 39 |

Remesure après la clôture `<untrusted-data>` du prompt (02.11.2026, même banc, `results/auto-review-*-fence-2026-11-02.json`) :

| palier | accord (49) | faux « approuver » (tous) | faux « approuver » (éligibles, 9) | justes approuvées (28) | escalades |
|---|---|---|---|---|---|
| qwen3.6-35b-a3b (llama.cpp, sans raisonnement) | 69,4 % | 1 / 21 | 0 / 9 | 14 | 32 |
| Ship du Router `qwen/qwen3.8-27b` (`reasoning_effort: none`, clé de banc) | 65,3 % | 0 / 21 | 0 / 9 | 11 | 38 |

Le faux « approuver » du 35B (B1-A-035, `send_email_reply`, effet `send_external`) est une carte de la liste fermée :
en production elle n'atteint jamais le relecteur. Critère bêta tenu sur le périmètre réel (éligibles : 0 / 9 sur les
deux paliers) et sur les 49 avec Ship ; le 35B devient plus prudent (accord 83,7 % → 69,4 %, plus d'escalades).

Le relecteur est donc **allumable** ; Ship reste prudent (beaucoup d'escalades, donc peu de fatigue évitée).
