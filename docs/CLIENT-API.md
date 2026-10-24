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
  peut venir d'un client vérifié (pas du proxy Hermes, qui reste `gateway-proxy-unverified`).
- Les clés d'API statiques (`api_keys` de la config) restent pour les bancs et les ponts serveur-à-serveur.
- La Mini App Telegram n'utilise PAS cette danse : elle présente `initData` (en-tête `X-Telegram-Init-Data` sur
  toute méthode, ou champ `init_data` du corps en POST), validé et résolu en `sender` via
  `NokkerConfig.telegram_ids` — un `initData` validé vaut une identité, au même titre qu'un jeton de session.

## Ressources

| Méthode | Chemin | Rôle |
|---|---|---|
| POST | `/v1/chat/completions` | un tour (OpenAI, `stream: true` → SSE), en-tête `X-Session-Id` = fil |
| GET | `/v1/events` | SSE de l'utilisateur, tous fils confondus (voir ci-dessous) |
| GET | `/v1/approvals?status=pending` | cartes en attente |
| GET | `/v1/approvals/{id}` | une carte : outil, arguments, effet, raison, avis de Nina, niveau, expiration (forme JSON exacte : docs/APPROVALS.md §5) |
| POST | `/v1/approvals/{id}/challenge` | défi à signer |
| POST | `/v1/approvals/{id}/decision` | décision (signature brute, WebAuthn, ou TOTP — docs/APPROVALS.md §3.3-§3.5), déclenche l'exécution + le tour de continuation si approuvée |
| POST | `/v1/devices` · GET `/v1/devices` · DELETE `/v1/devices/{id}` | appareils de l'utilisateur |
| POST | `/v1/devices/{id}/confirm` | confirme un appareil `pending` par TOTP (alternative à `python -m nokker devices approve <code>`) |
| POST | `/v1/devices/{id}/session-challenge` | défi de connexion (sans identité préalable — c'est lui qui en établit une) |
| POST | `/v1/sessions` | jeton de session contre ce défi signé (brut ou WebAuthn) |
| POST | `/v1/auth/email/start` | `{email, lang}` → **toujours** `200 {sent:true}` (même corps, même délai, même `429` que l'adresse ait un compte ou non) ; code à 6 chiffres envoyé par front1 seulement à un compte actif — 10 min, usage unique |
| POST | `/v1/auth/email/verify` | `{email, code, name, public_key_spki}` → `200 {device_id, user}` ; toute erreur = `401 {error:"invalid_code"}` ; 5 essais puis code brûlé. L'appareil créé se connecte (`session-challenge` → `/v1/sessions`) mais **ne décide jamais** d'une approbation : `403 {"error":{"message":"strong_device_required"}}` |
| GET | `/v1/account` | compte `us_…` exigé → `{email, lang, created_at, status, terms: {version, accepted_at} \| null, consents: {training}}` |
| PUT | `/v1/account/consent` | `{scope: "training", granted: bool}` → `200 {scope, granted}` ; session normale (pas d'appareil fort) ; une ligne `data_consents` seulement quand le choix change ; `400 {error:"invalid"}` |
| GET | `/v1/account/usage` | compte `us_…` exigé (sinon 403) → `{tz, mode: soft\|hard, plan, today: {day, resets_at, turns: {used, cap, over, hard_cap}}, week: {starts, resets_at, turns: {used, cap, over}}}` ; heures en Europe/Zurich, jour de quota 12:00 UTC → 12:00 UTC, semaine lundi 06:00 UTC ; `cap` null = sans limite ; `over` = used > cap |

`/v1/threads*`, `/v1/bubbles*`, `/v1/notifications`, `/v1/users`, `/v1/agents*`, `/v1/previews*` :
docs/BUBBLES.md (§1-§6 pour les fils/bulles/aperçus, §9 pour les comblements du 18.10.2026).

## Streaming de `/v1/chat/completions` (`stream: true`, P1 19.10.2026)

Pourquoi : un tour réel du canari a duré ~2 min (dernier appel modèle : 68 s) ; sans un octet pendant 100 s,
Cloudflare (approve.nokker.ai → nginx :8743 → noyau :8742) coupe et Safari affiche « Load failed ».

**Pour tous les clients** : les en-têtes SSE partent tout de suite (`X-Accel-Buffering: no` pour nginx), le tour
tourne dans un fil de travail, et un **commentaire SSE `: ping`** part toutes les ~10 s tant que rien d'autre ne
sort. Les commentaires sont ignorés par tout parseur SSE (spec), par le SDK (`sse.ts`) et par la gateway Hermes
(`_consume_sse_line` ne lit que les lignes `data: `). Sans opt-in, le corps est **octet pour octet** celui d'avant,
pings exceptés : rôle, réponse complète + `usage`, `finish_reason: "stop"`, `[DONE]` (vérifié contre `origin/main`
et par `tests/unit/api/test_stream.py`). Une erreur du tour APRÈS les en-têtes part en `data: {"error": {"message"}}`
sans `[DONE]` (avant : HTTP 500).

**Jetons en direct — opt-in `X-Nokker-Stream-Tokens: 1`** (le client web le pose ; la gateway Hermes ne le pose
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
qu'aucun morceau n'a été remis.

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
