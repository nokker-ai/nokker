// Tests de approve-core.js — `node --test nokker/api/miniapp/approve-core.test.mjs`
// Node ≥ 20 : WebCrypto (`globalThis.crypto.subtle`) et `atob`/`btoa` sont globaux, aucune dépendance.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  canonicalJson,
  bytesToBase64Url,
  base64UrlToBytes,
  sha256HexOfCanonicalJson,
  generateDeviceKeyPair,
  exportPrivateKeyPkcs8Base64Url,
  exportPublicKeySpkiBase64Url,
  importPrivateKeyForSigning,
  importPublicKeyForVerifying,
  buildChallengePayload,
  signChallengeDecision,
  verifyChallengeDecision,
  CHALLENGE_AUD,
} from "./approve-core.js";

test("canonicalJson trie les clés et n'ajoute aucun espace", () => {
  const value = { b: 2, a: 1, c: { z: 1, y: 2 } };
  assert.equal(canonicalJson(value), '{"a":1,"b":2,"c":{"y":2,"z":1}}');
});

test("canonicalJson préserve l'ordre des tableaux (seules les clés d'objet sont triées)", () => {
  assert.equal(canonicalJson({ b: [3, 1, 2], a: "x" }), '{"a":"x","b":[3,1,2]}');
});

test("canonicalJson est déterministe quel que soit l'ordre d'insertion", () => {
  const v1 = canonicalJson({ nonce: "n", aud: "a", exp: 1, approval_id: "id", args_sha256: "h" });
  const v2 = canonicalJson({ approval_id: "id", args_sha256: "h", aud: "a", exp: 1, nonce: "n" });
  assert.equal(v1, v2);
});

test("base64url ne contient jamais +, / ni = (longueurs 1..8 mod 4 variées)", () => {
  for (let len = 1; len <= 37; len += 1) {
    const bytes = new Uint8Array(len).map((_, i) => (i * 37 + 251) % 256);
    const b64url = bytesToBase64Url(bytes);
    assert.ok(!/[+/=]/.test(b64url), `fuite de charset standard pour len=${len}: ${b64url}`);
    assert.deepEqual(base64UrlToBytes(b64url), bytes, `aller-retour cassé pour len=${len}`);
  }
});

test("buildChallengePayload reconstruit exactement le format du contrat (clés triées)", () => {
  const payload = buildChallengePayload({
    approvalId: "appr_123",
    argsSha256: "deadbeef",
    nonce: "nonce-abc",
    exp: 1760000000,
  });
  assert.equal(
    payload,
    '{"approval_id":"appr_123","args_sha256":"deadbeef","aud":"nokker-approval","exp":1760000000,"nonce":"nonce-abc"}',
  );
  assert.ok(payload.includes(`"aud":"${CHALLENGE_AUD}"`));
});

test("sha256HexOfCanonicalJson est un hex sha256 déterministe (64 car.) indépendant de l'ordre des clés", async () => {
  const h1 = await sha256HexOfCanonicalJson({ a: 1, b: "x" });
  const h2 = await sha256HexOfCanonicalJson({ b: "x", a: 1 });
  assert.equal(h1, h2);
  assert.match(h1, /^[0-9a-f]{64}$/);
});

test("aller-retour complet : génération -> export PKCS#8/SPKI -> import -> signature -> vérification", async () => {
  const { privateKey, publicKey } = await generateDeviceKeyPair();

  const privB64 = await exportPrivateKeyPkcs8Base64Url(privateKey);
  const pubB64 = await exportPublicKeySpkiBase64Url(publicKey);
  assert.ok(!/[+/=]/.test(privB64));
  assert.ok(!/[+/=]/.test(pubB64));

  // Simule : le biometricToken (privB64) est libéré par Face ID, puis réimporté juste pour signer.
  const signingKey = await importPrivateKeyForSigning(privB64);
  const verifyingKey = await importPublicKeyForVerifying(pubB64);

  const challenge = buildChallengePayload({
    approvalId: "appr_456",
    argsSha256: "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcd",
    nonce: "nonce-xyz",
    exp: 1760000120,
  });

  const signature = await signChallengeDecision(signingKey, challenge, "approve");
  assert.ok(!/[+/=]/.test(signature));

  const ok = await verifyChallengeDecision(verifyingKey, challenge, "approve", signature);
  assert.equal(ok, true);
});

test("la signature est au format IEEE P1363 natif (r||s, 64 octets) — pas DER", async () => {
  const { privateKey } = await generateDeviceKeyPair();
  const challenge = buildChallengePayload({ approvalId: "a", argsSha256: "b", nonce: "n", exp: 1 });
  const signature = await signChallengeDecision(privateKey, challenge, "reject");
  const raw = base64UrlToBytes(signature);
  // DER encoderait une séquence ASN.1 de longueur variable (souvent 70-72 octets, préfixe 0x30) ;
  // P1363 pour P-256 est toujours exactement 32+32 = 64 octets, sans en-tête ASN.1.
  assert.equal(raw.length, 64);
  assert.notEqual(raw[0], 0x30);
});

test("la décision fait partie du message signé : approve et reject ne sont pas interchangeables", async () => {
  const { privateKey, publicKey } = await generateDeviceKeyPair();
  const challenge = buildChallengePayload({ approvalId: "a", argsSha256: "b", nonce: "n", exp: 1 });

  const sigApprove = await signChallengeDecision(privateKey, challenge, "approve");

  assert.equal(await verifyChallengeDecision(publicKey, challenge, "approve", sigApprove), true);
  assert.equal(await verifyChallengeDecision(publicKey, challenge, "reject", sigApprove), false);
});

test("signChallengeDecision refuse une décision autre que approve/reject", async () => {
  const { privateKey } = await generateDeviceKeyPair();
  await assert.rejects(() => signChallengeDecision(privateKey, "chal", "maybe"), /decision invalide/);
});

test("deux signatures d'un même défi ne divulguent pas de clé réutilisée en clair (sanity: pas identiques bit à bit n'est pas garanti par ECDSA randomisé, mais les deux doivent rester valides)", async () => {
  const { privateKey, publicKey } = await generateDeviceKeyPair();
  const challenge = buildChallengePayload({ approvalId: "a", argsSha256: "b", nonce: "n", exp: 1 });
  const sig1 = await signChallengeDecision(privateKey, challenge, "approve");
  const sig2 = await signChallengeDecision(privateKey, challenge, "approve");
  assert.equal(await verifyChallengeDecision(publicKey, challenge, "approve", sig1), true);
  assert.equal(await verifyChallengeDecision(publicKey, challenge, "approve", sig2), true);
});
