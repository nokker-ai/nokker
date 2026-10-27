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
  parseTarget,
  machineStartParam,
  machineCardPath,
  machineDecisionPath,
  buildTelegramProof,
  machineDecisionBody,
  readMachineCard,
  classifyMachineStatus,
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

// ---------------------------------------------------------------------------
// Carte d'une machine Lokkal liée (lot X10)
// ---------------------------------------------------------------------------

const MACHINE = "mc_0123456789abcdef";

test("parseTarget : start_param d'une carte machine", () => {
  assert.deepEqual(parseTarget({ startParam: `${MACHINE}-ap_1-x_Y` }), {
    kind: "machine",
    machine: MACHINE,
    approval: "ap_1-x_Y",
  });
});

test("parseTarget : start_param d'une carte du compte reste une carte cloud", () => {
  assert.deepEqual(parseTarget({ startParam: "8b1e4f2a-0c" }), { kind: "cloud", approval: "8b1e4f2a-0c" });
});

test("parseTarget : paramètres machine et approval de l'URL", () => {
  assert.deepEqual(parseTarget({ search: `?machine=${MACHINE}&approval=abc123` }), {
    kind: "machine",
    machine: MACHINE,
    approval: "abc123",
  });
});

test("parseTarget : ?startapp= dans l'URL vaut start_param, et start_param passe avant l'URL", () => {
  assert.deepEqual(parseTarget({ search: `?startapp=${MACHINE}-abc` }), { kind: "machine", machine: MACHINE, approval: "abc" });
  assert.deepEqual(parseTarget({ startParam: "card1", search: `?machine=${MACHINE}&approval=abc` }), {
    kind: "cloud",
    approval: "card1",
  });
});

test("parseTarget : identifiant mal formé ignoré (jamais une route construite avec un texte libre)", () => {
  assert.equal(parseTarget({ search: "?machine=mc_zz&approval=abc" }), null);
  assert.equal(parseTarget({ search: `?machine=${MACHINE}&approval=../../x` }), null);
  assert.equal(parseTarget({ startParam: "a/b" }), null);
  assert.equal(parseTarget({ search: `?machine=${MACHINE}` }), null);
  assert.equal(parseTarget({}), null);
  assert.equal(parseTarget(), null);
});

test("machineStartParam / chemins : forme exacte et refus des identifiants invalides", () => {
  assert.equal(machineStartParam(MACHINE, "abc"), `${MACHINE}-abc`);
  assert.deepEqual(parseTarget({ startParam: machineStartParam(MACHINE, "abc") }), {
    kind: "machine",
    machine: MACHINE,
    approval: "abc",
  });
  assert.throws(() => machineStartParam("mc_x", "abc"));
  assert.equal(machineCardPath(MACHINE, "abc"), `/v1/link/machines/${MACHINE}/approvals/abc`);
  assert.equal(machineDecisionPath(MACHINE, "abc"), `/v1/link/machines/${MACHINE}/approvals/abc/decision`);
});

test("buildTelegramProof : exactement {device_id, signature, telegram_init_data}", () => {
  const proof = buildTelegramProof({ deviceId: "dev1", signature: "sig", initData: "query_id=x&hash=y" });
  assert.deepEqual(proof, { device_id: "dev1", signature: "sig", telegram_init_data: "query_id=x&hash=y" });
  assert.throws(() => buildTelegramProof({ deviceId: "dev1", signature: "", initData: "x" }));
  assert.throws(() => buildTelegramProof({ deviceId: "dev1", signature: "s", initData: "" }));
});

test("machineDecisionBody : décision connue seulement", () => {
  const proof = { device_id: "d", signature: "s", telegram_init_data: "i" };
  assert.deepEqual(machineDecisionBody("approve", proof), { decision: "approve", proof });
  assert.throws(() => machineDecisionBody("maybe", proof));
});

test("la signature d'une carte machine porte sur le défi exact de la carte lue, plus la décision", async () => {
  const { privateKey, publicKey } = await generateDeviceKeyPair();
  const challenge = buildChallengePayload({ approvalId: "ap1", argsSha256: "h", nonce: "n", exp: 1 });
  const sig = await signChallengeDecision(privateKey, challenge, "approve");
  assert.equal(await verifyChallengeDecision(publicKey, challenge, "approve", sig), true);
  assert.equal(await verifyChallengeDecision(publicKey, challenge, "reject", sig), false);
  assert.equal(await verifyChallengeDecision(publicKey, `${challenge} `, "approve", sig), false);
});

test("readMachineCard : carte décrite par nokker.ai (connecteur relayé)", () => {
  const card = readMachineCard({
    approval_id: "ap1",
    title: "mail_send : x",
    reason: "motif de la machine",
    advice: "[risk:high] vérifie le destinataire",
    strength: "strong",
    expires_at: "2026-10-26T12:00:00+00:00",
    challenge: "{...}",
    status: "pending",
    args_sha256: "abc",
    summary_source: "nokker.ai",
    summary: {
      headline: "Envoyer un e-mail à a@b.ch",
      lines: [
        { key: "to", label: "À", value: "a@b.ch", items: ["a@b.ch", "c@d.ch"] },
        { key: "subject", label: "Objet", value: "Bonjour" },
      ],
    },
  });
  assert.equal(card.describedBy, "nokker.ai");
  assert.equal(card.headline, "Envoyer un e-mail à a@b.ch");
  assert.deepEqual(card.lines, [
    { label: "À", value: "a@b.ch, c@d.ch" },
    { label: "Objet", value: "Bonjour" },
  ]);
  assert.equal(card.advice, "[risk:high] vérifie le destinataire");
  assert.equal(card.challenge, "{...}");
});

test("readMachineCard : carte locale = décrite par la machine, aucune ligne de nokker.ai", () => {
  const card = readMachineCard({
    approval_id: "ap2",
    title: "ssh_run : prod",
    reason: "déployer",
    strength: "strong",
    summary_source: "machine",
    summary: { headline: "ne doit pas être repris", lines: [{ label: "x", value: "y" }] },
    args_sha256: "deadbeef",
    status: "pending",
  });
  assert.equal(card.describedBy, "machine");
  assert.equal(card.headline, "");
  assert.deepEqual(card.lines, []);
  assert.equal(card.title, "ssh_run : prod");
  assert.equal(card.argsSha256, "deadbeef");
});

test("readMachineCard : réponse vide ou inattendue sans exception", () => {
  const card = readMachineCard(null);
  assert.equal(card.title, "");
  assert.equal(card.status, null);
  assert.deepEqual(card.lines, []);
});

test("classifyMachineStatus : attente, issue dite par la machine, preuve refusée", () => {
  assert.equal(classifyMachineStatus({ status: "forwarded" }), "waiting");
  assert.equal(classifyMachineStatus({ status: "pending" }), "waiting");
  assert.equal(classifyMachineStatus({ status: "approved" }), "approved");
  assert.equal(classifyMachineStatus({ status: "rejected" }), "rejected");
  assert.equal(classifyMachineStatus({ status: "expired" }), "expired");
  assert.equal(classifyMachineStatus({ status: "pending", error: "preuve refusée par la machine : x" }), "refused");
  assert.equal(classifyMachineStatus(null), "waiting");
});
