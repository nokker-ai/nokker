# Approbations fortes — contrat (P1, 18.10.2026)

> Implémenté : `nokker/core/ports.py` (types + Protocol `Approvals`), `nokker/approvals/` (logique de
> preuve, backends, exécuteur, notificateur), `nokker/api/server.py` (routes HTTP), `nokker/qa/invariants.py`
> (`Verdict.strength`). Branche `p1/approvals`. Aligné avec la Mini App Telegram (`p1/miniapp`,
> `nokker/api/miniapp/`) et le chantier clients (`nokker-apps` : SDK, CLI, web, faux serveur) — §3/§5
> portent les noms RÉELS, pas des hypothèses.

Doctrine Nick (17.10) : Nokker a tous les droits, l'humain reste maître de sa demande et de ses conséquences. Il n'y a
pas de classifieur bloquant. Une action à effet que les invariants mettent en `HITL` n'est **jamais exécutée** sans une
approbation **prouvée cryptographiquement** par un humain autorisé (un opérateur de la config, ou son suppléant s'il est injoignable). L'avis de
Nina (risques, sauvegarde, retour arrière) accompagne la carte et ne bloque rien.

## 1. Niveaux

| Niveau | Preuves acceptées | Effets |
|---|---|---|
| `strong` | clé d'appareil libérée par biométrie : Mini App Telegram (BiometricManager, signature + `initData` vérifié), WebAuthn/passkey (Touch ID, Face ID, Windows Hello) **avec flag UV**, app native (Secure Enclave / StrongBox) — une signature d'appareil SANS cette preuve biométrique (clé logicielle CLI : trousseau ou fichier 0600, sans `initData` Telegram) est refusée (`ProofInvalid`, durcissement du 18.10.2026) | `DESTRUCTIVE` |
| `standard` | `strong` **ou** TOTP (web sans passkey) **ou** simple signature d'appareil par une clé logicielle (CLI, sans biométrie) | `SEND_EXTERNAL` hors allowlist, `SPEND` |

`SEND_BULK` reste **toujours `DENY`** (décision Nick 17.10, `qa/invariants.py`) — jamais soumis à une approbation,
donc jamais de `strength` associée. Le niveau vient du `Verdict` (champ `strength`, posé par
`DeterministicInvariants.check_action`) ; un GO textuel dans le message ne remplace jamais une approbation `strong`.

## 2. Cycle de vie

`pending` → `approved` | `rejected` | `expired` → (`approved`) `executed` | `failed`

- **Demande** (porte action, `Decision.HITL`, `Kernel._tool_step`) : si un port `Approvals` est branché (par défaut,
  `NokkerConfig.approvals_enabled=True`), le noyau appelle `approvals.request(turn, call, verdict, advice)` — enregistre
  l'appel EXACT (`tool_name`, `arguments`), son empreinte `args_sha256` (JSON canonique de `arguments` SEUL : clés
  triées, sans espaces, UTF-8 — `nokker.approvals.crypto.args_sha256`), `strength` (`Verdict.strength`), `rule`/`reason`
  (`Verdict.rule`/`.reason`), `advice` (pas encore produit par un pilier amont : toujours `None` aujourd'hui — un futur
  chantier le remplira, le champ existe déjà), `expires_at` (`created_at + 24h`, fixe pour l'instant — pas de
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
  Moyen : signature|signature+telegram|webauthn|totp|rejected
  Résultat : <contenu caviardé, tronqué à 2000 caractères>
  [/Approbation]
  ```
  Un refus produit le même tour de continuation, sans appel d'outil (`Résultat : refusé par <decided_by>.`).
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

### 3.1bis Enrôlement d'un navigateur sans identité préalable (autorisé par Nick le 18.10.2026)

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
**Limite connue** : pas de vérification du `rpIdHash` dans `authenticatorData` (confiance dans l'origine déjà
vérifiée) ; Safari non supporté côté client tant qu'il n'expose pas `getPublicKey()` (WebAuthn niveau 3).

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

- `advice` (avis de Nina) n'est jamais rempli aujourd'hui : `Kernel._tool_step` appelle `approvals.request(turn, tc,
  rule, None)` — un futur chantier (pilier Nina QA/risques) devra produire ce texte avant l'appel.
- `expires_at` est fixe (24 h) quel que soit l'effet — la mention « `SEND_BULK` 1 h » d'une version antérieure de ce
  document n'a jamais pu s'appliquer : `SEND_BULK` reste `DENY`, jamais soumis à approbation (§1).
- Le renouvellement d'un jeton de session (`POST /v1/sessions`) n'est pas implémenté : à l'expiration (12 h), le
  client refait la danse `session-challenge` → signature.
- WebAuthn ne vérifie pas `rpIdHash` dans `authenticatorData` (voir §3.4) ; Safari non supporté côté client.
- Le bus d'événements (`/v1/events`, `EventBus`) est en mémoire, un seul processus — pas de reprise si le serveur
  redémarre entre deux connexions (fenêtre de reprise bornée à `max_buffer`/`max_age_s`, pas un journal persistant).
- `PgApprovals` est câblée et testée par construction (même logique que `InMemoryApprovals`, voir §4), mais son
  SQL réel n'a été exercé qu'ponctuellement contre un Postgres jetable, pas en continu par la CI de ce chantier.
