// Outil de dev, PAS servi publiquement : fabrique un vecteur de signature figé (clé publique SPKI,
// défi, décision, signature) pour prouver que le format produit par approve-core.js (WebCrypto,
// ECDSA P-256 SHA-256, sortie P1363 r||s, base64url) se vérifie bien côté serveur Python avec la lib
// `cryptography`. ECDSA est randomisé (le nonce k change à chaque signature) : ce script ne reproduit
// donc pas un vecteur BIT-À-BIT stable d'un run à l'autre — on exécute ce script UNE fois, et on gèle
// la sortie dans tests/unit/api/test_miniapp_signature_vector.py (voir le commentaire là-bas).
//
// Usage : node nokker/api/miniapp/gen-signature-vector.mjs
import {
  generateDeviceKeyPair,
  exportPublicKeySpkiBase64Url,
  buildChallengePayload,
  signChallengeDecision,
} from "./approve-core.js";

const { privateKey, publicKey } = await generateDeviceKeyPair();
const publicKeySpkiB64Url = await exportPublicKeySpkiBase64Url(publicKey);

const challenge = buildChallengePayload({
  approvalId: "appr_fixture_0001",
  argsSha256: "3f786850e387550fdab836ed7e6dc881de23001b7bcc908ac8c5e2e1c4abd5b",
  nonce: "fixture-nonce-abcdef",
  exp: 1760000000,
});
const decision = "approve";
const signatureB64Url = await signChallengeDecision(privateKey, challenge, decision);

console.log(
  JSON.stringify(
    { public_key_spki_b64url: publicKeySpkiB64Url, challenge, decision, signature_b64url: signatureB64Url },
    null,
    2,
  ),
);
