/**
 * approve-core.js — logique pure crypto / canonique de la Mini App d'approbation Nokker.
 *
 * Aucun DOM, aucun `fetch`, aucun `Telegram.*` ici : ce module ne fait que (1) sérialiser du JSON
 * canonique identique au contrat serveur (docs/APPROVALS.md §2-3), (2) encoder/décoder du base64url,
 * et (3) manipuler les clés ECDSA P-256 via WebCrypto (génération, export/import PKCS#8/SPKI,
 * signature et vérification). C'est ce qui permet de le tester avec `node --test`, sans réseau et
 * sans navigateur (Node ≥ 18 expose `globalThis.crypto.subtle`, `atob`/`btoa`).
 *
 * Tout le reste (DOM, Telegram.WebApp, BiometricManager, fetch des routes /v1/...) vit dans
 * approve.js, qui importe ce module.
 */

// Algorithme utilisé pour TOUTES les clés d'appareil (enrôlement ET décision).
export const ECDSA_P256 = { name: "ECDSA", namedCurve: "P-256" };

// `aud` fixe du défi, cf docs/APPROVALS.md §3.
export const CHALLENGE_AUD = "nokker-approval";

// ---------------------------------------------------------------------------
// JSON canonique — clés triées (récursivement), sans espaces, UTF-8.
// ---------------------------------------------------------------------------

/**
 * Trie récursivement les clés d'un objet (les tableaux gardent leur ordre). `JSON.stringify` sans
 * argument d'indentation ne met aucun espace, ce qui correspond à `separators=(",", ":")` côté
 * Python. Le texte produit est ensuite encodé en UTF-8 par `TextEncoder` avant tout hash/signature —
 * jamais par un `charCodeAt` brut, pour rester correct sur des caractères hors ASCII.
 */
function sortDeep(value) {
  if (Array.isArray(value)) return value.map(sortDeep);
  if (value !== null && typeof value === "object") {
    const out = {};
    for (const key of Object.keys(value).sort()) out[key] = sortDeep(value[key]);
    return out;
  }
  return value;
}

/** JSON canonique (clés triées, sans espaces) d'une valeur JS sérialisable. */
export function canonicalJson(value) {
  return JSON.stringify(sortDeep(value));
}

/** Octets UTF-8 d'une chaîne, pour signature/hash — jamais de charCodeAt direct. */
export function utf8Bytes(str) {
  return new TextEncoder().encode(str);
}

// ---------------------------------------------------------------------------
// base64url (RFC 4648 §5) — sans padding.
// ---------------------------------------------------------------------------

/** Encode des octets (ArrayBuffer | TypedArray | Array) en base64url sans padding. */
export function bytesToBase64Url(bytesLike) {
  const bytes = bytesLike instanceof Uint8Array ? bytesLike : new Uint8Array(bytesLike);
  let binary = "";
  const CHUNK = 0x8000;
  for (let i = 0; i < bytes.length; i += CHUNK) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
  }
  const b64 = btoa(binary);
  return b64.replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/** Décode une chaîne base64url (padding optionnel) en `Uint8Array`. */
export function base64UrlToBytes(b64url) {
  const b64 = b64url.replace(/-/g, "+").replace(/_/g, "/");
  const pad = b64.length % 4 === 0 ? "" : "=".repeat(4 - (b64.length % 4));
  const binary = atob(b64 + pad);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

// ---------------------------------------------------------------------------
// Empreinte des arguments (args_sha256) — même recette que le défi côté serveur.
// ---------------------------------------------------------------------------

/** sha256 hex du JSON canonique d'une valeur — pour vérifier côté client l'empreinte `args_sha256`. */
export async function sha256HexOfCanonicalJson(value) {
  const digest = await crypto.subtle.digest("SHA-256", utf8Bytes(canonicalJson(value)));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// ---------------------------------------------------------------------------
// Paire de clés d'appareil (ECDSA P-256).
// ---------------------------------------------------------------------------

/**
 * Génère la paire ECDSA P-256 de l'appareil. `extractable: true` est nécessaire pour pouvoir
 * exporter la clé privée en PKCS#8 et la confier à `BiometricManager.updateBiometricToken` — c'est
 * Face ID/Touch ID, pas WebCrypto, qui protège ensuite ce secret (cf docs/APPROVALS.md §3).
 */
export async function generateDeviceKeyPair() {
  return crypto.subtle.generateKey(ECDSA_P256, true, ["sign", "verify"]);
}

/** Exporte la clé privée en PKCS#8, encodée base64url — c'est exactement le `biometricToken`. */
export async function exportPrivateKeyPkcs8Base64Url(privateKey) {
  const raw = await crypto.subtle.exportKey("pkcs8", privateKey);
  return bytesToBase64Url(raw);
}

/** Exporte la clé publique en SPKI, encodée base64url — c'est ce qui part dans `POST /v1/devices`. */
export async function exportPublicKeySpkiBase64Url(publicKey) {
  const raw = await crypto.subtle.exportKey("spki", publicKey);
  return bytesToBase64Url(raw);
}

/**
 * Réimporte une clé privée PKCS#8 (base64url) pour signer UNE décision. Volontairement
 * `extractable: false` : une fois importée pour signer, elle ne doit plus pouvoir ressortir de
 * WebCrypto sous aucune forme.
 */
export async function importPrivateKeyForSigning(base64url) {
  const raw = base64UrlToBytes(base64url);
  return crypto.subtle.importKey("pkcs8", raw, ECDSA_P256, false, ["sign"]);
}

/** Réimporte une clé publique SPKI (base64url) pour vérifier une signature (tests, diagnostics). */
export async function importPublicKeyForVerifying(base64url) {
  const raw = base64UrlToBytes(base64url);
  return crypto.subtle.importKey("spki", raw, ECDSA_P256, true, ["verify"]);
}

// ---------------------------------------------------------------------------
// Défi — construction canonique (tests uniquement) et signature de la décision.
// ---------------------------------------------------------------------------

/**
 * Reconstruit le JSON canonique du défi tel que défini par le contrat (docs/APPROVALS.md §3) :
 * `{"approval_id","args_sha256","aud","exp","nonce"}` (ordre alphabétique après tri des clés).
 *
 * N'est utilisée QUE pour fabriquer des défis de test ici et dans la fixture Python. En vrai, le
 * client ne reconstruit JAMAIS le défi : il signe le champ `challenge` renvoyé tel quel par
 * `POST /v1/approvals/{id}/challenge`, pour éviter tout écart de canonicalisation avec le serveur.
 */
export function buildChallengePayload({ approvalId, argsSha256, nonce, exp }) {
  return canonicalJson({ approval_id: approvalId, args_sha256: argsSha256, aud: CHALLENGE_AUD, exp, nonce });
}

/**
 * Signe `challenge + "." + decision` (la décision est incluse dans la signature, cf contrat §3).
 * `challenge` DOIT être la chaîne brute reçue du serveur, pas un JSON reconstruit côté client.
 * Le format produit par `crypto.subtle.sign` sur une clé ECDSA P-256 est déjà IEEE P1363
 * (concatenation brute r || s, 64 octets) — pas DER — donc aucune conversion n'est nécessaire avant
 * l'encodage base64url.
 */
export async function signChallengeDecision(privateKey, challenge, decision) {
  assertDecision(decision);
  const message = `${challenge}.${decision}`;
  const signature = await crypto.subtle.sign({ name: "ECDSA", hash: "SHA-256" }, privateKey, utf8Bytes(message));
  return bytesToBase64Url(signature);
}

/** Vérifie une signature produite par `signChallengeDecision` (tests, diagnostics côté client). */
export async function verifyChallengeDecision(publicKey, challenge, decision, signatureBase64Url) {
  assertDecision(decision);
  const message = `${challenge}.${decision}`;
  const signature = base64UrlToBytes(signatureBase64Url);
  return crypto.subtle.verify({ name: "ECDSA", hash: "SHA-256" }, publicKey, signature, utf8Bytes(message));
}

function assertDecision(decision) {
  if (decision !== "approve" && decision !== "reject") {
    throw new Error(`decision invalide: ${JSON.stringify(decision)} (attendu "approve" ou "reject")`);
  }
}

// ---------------------------------------------------------------------------
// Carte d'une MACHINE Lokkal liée (lot X10, docs/LOKKAL.md §7.3) — partie pure.
//
// La machine dépose sa carte forte chez nokker.ai ; la Mini App la lit et la signe ici comme une carte du compte, mais
// la preuve part vers nokker.ai (`POST /v1/link/machines/{m}/approvals/{id}/decision`) qui la vérifie et remet à la
// machine une attestation signée. Le défi à signer est celui que la machine a déposé : il vient de la carte lue, jamais
// reconstruit ici.
// ---------------------------------------------------------------------------

const RE_MACHINE_ID = /^mc_[0-9a-f]{16}$/;
const RE_APPROVAL_ID = /^[A-Za-z0-9_-]{1,64}$/;

/**
 * Cible de la Mini App : `{kind: "machine", machine, approval}`, `{kind: "cloud", approval}` ou `null`.
 * Sources, par ordre : `start_param` Telegram (`mc_<16 hex>-<approval_id>` pour une carte machine, sinon l'id d'une carte
 * du compte), puis l'URL (`?machine=…&approval=…`, ou `?startapp=…` comme `start_param`). Un identifiant qui ne colle pas
 * au format est ignoré : la page ne construit jamais une route avec un texte libre.
 */
export function parseTarget({ startParam, search } = {}) {
  const params = new URLSearchParams(search || "");
  const fromParam = (value) => {
    if (!value) return null;
    const m = /^(mc_[0-9a-f]{16})-([A-Za-z0-9_-]{1,64})$/.exec(value);
    if (m) return { kind: "machine", machine: m[1], approval: m[2] };
    return RE_APPROVAL_ID.test(value) ? { kind: "cloud", approval: value } : null;
  };
  const viaStart = fromParam(startParam);
  if (viaStart) return viaStart;
  const machine = params.get("machine");
  const approval = params.get("approval");
  if (machine && approval && RE_MACHINE_ID.test(machine) && RE_APPROVAL_ID.test(approval)) {
    return { kind: "machine", machine, approval };
  }
  return fromParam(params.get("startapp"));
}

/** `start_param` d'une carte machine (le bouton de la notification et les tests croisés s'appuient sur la même forme). */
export function machineStartParam(machine, approval) {
  if (!RE_MACHINE_ID.test(machine || "") || !RE_APPROVAL_ID.test(approval || "")) {
    throw new Error("identifiant de machine ou de carte invalide");
  }
  return `${machine}-${approval}`;
}

export function machineCardPath(machine, approval) {
  return `/v1/link/machines/${machine}/approvals/${approval}`;
}

export function machineDecisionPath(machine, approval) {
  return `${machineCardPath(machine, approval)}/decision`;
}

/**
 * Preuve Telegram d'une carte machine : l'appareil (clé libérée par BiometricManager) a signé `<défi>.<décision>`,
 * `initData` prouve que c'est bien ce Telegram. nokker.ai refuse tout autre champ.
 */
export function buildTelegramProof({ deviceId, signature, initData }) {
  if (!deviceId || !signature || !initData) throw new Error("preuve incomplète (appareil, signature, initData)");
  return { device_id: deviceId, signature, telegram_init_data: initData };
}

/** Corps de `POST …/decision` d'une carte machine. */
export function machineDecisionBody(decision, proof) {
  assertDecision(decision);
  return { decision, proof };
}

/**
 * Lecture de la carte rendue par `GET /v1/link/machines/{m}/approvals/{id}` (`nokker/cloud/approvals.py::view`).
 * `headline` et `lines` : ce que nokker.ai décrit lui-même (cartes de connecteurs relayés) ; sans cela (carte locale), la
 * carte est « décrite par ta machine » : titre et motif de la machine, avec l'empreinte des arguments.
 */
export function readMachineCard(view) {
  const summary = view && typeof view.summary === "object" && view.summary ? view.summary : {};
  const lines = Array.isArray(summary.lines) ? summary.lines : [];
  const described = view?.summary_source === "nokker.ai" && !!summary.headline;
  return {
    approvalId: view?.approval_id ?? null,
    title: String(view?.title ?? ""),
    reason: String(view?.reason ?? ""),
    advice: view?.advice ?? null,
    strength: view?.strength ?? "standard",
    expiresAt: view?.expires_at ?? null,
    challenge: view?.challenge ?? null,
    status: view?.status ?? null,
    error: view?.error ?? null,
    argsSha256: view?.args_sha256 ?? null,
    describedBy: described ? "nokker.ai" : "machine",
    headline: described ? String(summary.headline) : "",
    lines: described ? lines.map((l) => ({ label: String(l?.label ?? l?.key ?? ""), value: lineValue(l) })) : [],
  };
}

function lineValue(line) {
  if (Array.isArray(line?.items) && line.items.length) return line.items.join(", ");
  return String(line?.value ?? "");
}

/**
 * Où en est une carte machine après l'envoi de la décision :
 *  - `waiting`  : transmise (`forwarded`) ou en attente, la machine n'a pas encore répondu ;
 *  - `approved` / `rejected` / `expired` : dit par la machine ;
 *  - `refused`  : la machine a refusé la preuve (la carte est revenue en attente avec `error`) : on peut signer à nouveau.
 */
export function classifyMachineStatus(view) {
  const status = view?.status ?? null;
  if (status === "approved" || status === "rejected" || status === "expired") return status;
  if (status === "pending" && view?.error) return "refused";
  return "waiting";
}
