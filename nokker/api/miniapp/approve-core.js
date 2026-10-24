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
