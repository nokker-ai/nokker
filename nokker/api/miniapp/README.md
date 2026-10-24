# Mini App Telegram — Approbations Nokker

Mini App statique (aucun framework, aucun build) qui fait vivre les approbations fortes de Nokker
(`docs/APPROVALS.md`, `docs/CLIENT-API.md`) dans Telegram : enrôlement d'un appareil (clé ECDSA P-256
protégée par biométrie) puis approbation/refus d'une carte par signature biométrique.

## Fichiers

Servis publiquement sous `/miniapp/` :

- `approve.html` — page unique, charge `https://telegram.org/js/telegram-web-app.js` (seul script
  externe autorisé) puis `approve.js` en module ES.
- `approve.js` — logique applicative : UI, `Telegram.WebApp` (thème, `BiometricManager`,
  `MainButton`/`SecondaryButton`, `HapticFeedback`), appels à l'API Nokker.
- `approve-core.js` — module **pur** (aucun DOM, aucun réseau) : JSON canonique, base64url, paire de
  clés ECDSA P-256 (génération, export PKCS#8/SPKI, import, signature/vérification). C'est ce qui est
  testé par `node --test`.
- `style.css` — thème piloté par `Telegram.WebApp.themeParams`, clair/sombre, pensé mobile (iPhone en
  premier).

**NE PAS servir publiquement** (outils de dev/tests, aucun secret dedans, mais inutiles en prod) :

- `package.json` — marqueur Node `{"type": "module"}`, uniquement pour que `node --test` et
  `node gen-signature-vector.mjs` traitent les `.js`/`.mjs` de ce dossier comme des modules ES.
- `approve-core.test.mjs` — tests `node --test`.
- `gen-signature-vector.mjs` — génère le vecteur figé utilisé par le test croisé Python (voir plus
  bas) ; ne stocke jamais de clé privée.

Si le serveur (branche `p1/approvals`, en parallèle) monte `nokker/api/miniapp/` tel quel en statique,
ces quatre fichiers seront exposés — sans risque (pas de secret, scripts de test seulement), mais
inutile. Le plus simple : monter explicitement les 4 fichiers publics plutôt qu'un dossier entier, ou
les exclure par extension (`*.test.mjs`, `gen-signature-vector.mjs`, `package.json`). **Je n'ai pas
touché à `server.py`** — c'est à la session serveur de choisir le point de montage ; cette note est le
seul endroit où je décris le besoin, comme convenu.

## Configuration BotFather

1. `@BotFather` → `/mybots` → choisir le bot Nokker (celui qui notifie les approbations) →
   **Bot Settings → Menu Button** (ou **Mini Apps → Edit Mini App** si l'app existe déjà en tant que
   Mini App nommée via `/newapp`).
2. **URL** : `https://<hôte>/miniapp/approve.html` — **doit être HTTPS** (Telegram refuse HTTP) et
   joignable publiquement (le tunnel Cloudflare existant suffit si `/miniapp/` est bien routé vers ce
   service).
3. Pour le **bouton qui accompagne chaque carte d'approbation** (ouverture avec
   `startapp=<approval_id>`) : le notificateur Telegram (côté serveur, hors scope de ce livrable)
   construit un bouton inline `web_app` avec
   `url = "https://<hôte>/miniapp/approve.html?startapp=<approval_id>"` **ou**, si le bot a une Mini
   App nommée (`/newapp`), un lien `t.me/<bot>/<nom_app>?startapp=<approval_id>`. Dans les deux cas,
   le client lit l'identifiant via `Telegram.WebApp.initDataUnsafe.start_param` (ce que fait
   `approve.js`) — pas via l'URL brute, qui peut être réécrite par Telegram.
4. Le **menu du bot** (`/setmenubutton`) peut pointer vers la même URL sans `start_param` : l'app
   affiche alors l'écran « Aucune approbation à afficher » (utile pour tester l'enrôlement seul).

## Flux

- **Première ouverture** (`BiometricManager.isBiometricTokenSaved == false`) : demande d'accès
  biométrique → génération ECDSA P-256 (WebCrypto, extractable) → export PKCS#8 → **ce secret est
  immédiatement confié à `BiometricManager.updateBiometricToken`** (jamais stocké ailleurs, jamais
  envoyé au serveur) → `POST /v1/devices` avec la clé **publique** SPKI + nom d'appareil + `initData`
  → affichage du code d'appairage → vérification automatique (toutes les 3 s, + bouton manuel) de
  `GET /v1/devices` jusqu'à ce que l'appareil ne soit plus `pending`.
- **Approbation** (`start_param = <approval_id>`) : `GET /v1/approvals/{id}` → carte (outil,
  arguments repliables si longs, effet, raison, avis de Nina mis en avant, niveau, compte à rebours)
  → `MainButton` = Approuver, `SecondaryButton` = Refuser (repli en bouton HTML si le client Telegram
  n'a pas `SecondaryButton`, Bot API < 7.10) → `POST .../challenge` → `BiometricManager.authenticate`
  → la clé privée (biometricToken) est réimportée en mémoire **juste pour signer**, jamais
  réexportée → signature ECDSA P-256/SHA-256 de `challenge + "." + decision` (format P1363 natif de
  WebCrypto) → `POST .../decision` → écran de résultat + `HapticFeedback` + bouton Fermer
  (`Telegram.WebApp.close()`).
- Gérés explicitement, sans repli silencieux : biométrie refusée/indisponible, carte déjà
  décidée/expirée (lu depuis `status` au chargement **et** compte à rebours côté client), erreurs
  réseau (retry), `initData` absent (page ouverte hors Telegram).

## Limites connues

- **`BiometricManager` absent** sur certains clients (desktop Telegram notamment, certaines versions
  anciennes) : message explicite affiché, aucune tentative de repli vers un autre mécanisme de preuve
  depuis cette Mini App — l'utilisateur doit rouvrir la carte depuis un client mobile.
- **`SecondaryButton`** nécessite Bot API ≥ 7.10 ; en dessous, repli en bouton HTML « Refuser » sous
  la carte (le `MainButton` Telegram reste utilisé pour « Approuver »).
- Le niveau `standard` (TOTP) **n'est pas géré par cette Mini App** : le contrat ne décrit de flux
  TOTP que pour `POST .../decision` depuis un client sans passkey, pas pour cette interface Telegram
  centrée sur la biométrie d'appareil. Un utilisateur qui veut approuver par TOTP doit utiliser un
  autre client.
- Aucune pagination/limite n'est appliquée sur les arguments affichés au-delà du repli visuel
  (`<details>`) — un outil aux arguments extrêmement volumineux resterait lisible mais long à faire
  défiler.

## Ambiguïtés du contrat rencontrées (à trancher avec la session serveur)

Les docs lus en entier avant d'écrire ce client (`docs/APPROVALS.md`, `docs/CLIENT-API.md`) fixent le
protocole de preuve dans le détail, mais laissent trois points ouverts, documentés aussi en tête de
`approve.js` :

1. **Forme JSON de `GET /v1/approvals/{id}`** : le contrat liste les champs attendus en prose
   (« outil, arguments, effet, raison, avis de Nina, niveau, expiration ») sans donner les clés JSON.
   Ce client lit plusieurs noms plausibles avec repli (`tool.name`/`tool_name`/`name`,
   `strength`/`level`, `advice`/`nina_advice`, `expires_at`/`expiresAt`, `status`/`state`…) — voir
   `readApprovalFields()` dans `approve.js`.
2. **Authentification des appels Mini App** : le contrat décrit un jeton de session Bearer générique
   (`POST /v1/sessions`, signature de défi par la clé d'appareil) pour les clients CLI/natifs, mais
   dit seulement, pour Telegram, qu'« en plus, `initData` ... est validé ». Ce client envoie
   `initData` sur **chaque** appel (en-tête `X-Telegram-Init-Data` pour les `GET`, champ `init_data`
   dans le corps pour les `POST`) et **ne fait pas** la danse `POST /v1/sessions` — en supposant que
   `initData` suffit comme preuve d'identité pour un client Mini App, puisque Telegram la fraîchit à
   chaque ouverture. Si le serveur exige quand même un Bearer de session, tout passe par la fonction
   `apiFetch()` : un seul endroit à modifier.
3. **Réponse de `POST /v1/devices`** (code d'appairage à afficher) et de `GET /v1/devices` (statut
   par appareil, pour le polling de confirmation) : non spécifiées. Lues défensivement
   (`id`/`device_id`, `pairing_code`/`code`, `status`/`state`, liste nue ou `{devices: [...]}`).

Aucune de ces hypothèses ne change le format de preuve cryptographique lui-même (identique au
contrat) : seuls les noms de champs/en-têtes autour pourraient avoir besoin d'un ajustement ponctuel
une fois le serveur réel en face.

## Tests

- `node --test nokker/api/miniapp/approve-core.test.mjs` — 11 tests, aucun réseau : JSON canonique,
  base64url (aller-retour, absence de `+`/`/`/`=`), paire de clés (génération → export PKCS#8/SPKI →
  import → signature → vérification), format P1363 natif (64 octets, pas DER), décision liée au
  message signé.
- `tests/unit/api/test_miniapp_signature_vector.py` (`pytest`) — 5 tests, aucun réseau : prend un
  vecteur figé (produit une fois par `node nokker/api/miniapp/gen-signature-vector.mjs`) et le
  vérifie avec `cryptography` (conversion P1363 → DER puis `EllipticCurvePublicKey.verify`) — preuve
  que la signature produite par `approve-core.js` se valide avec la bibliothèque que le serveur
  Python utilisera.
