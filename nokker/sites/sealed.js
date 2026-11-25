// Saisie scellée des sites connectés (docs/CONNECTED-SITES.md §5.5, §15) — référence pour la mini-app et le web.
// Même algorithme que nokker/sites/crypto.py : ECDH P-256 (clé éphémère de la page × clé éphémère de la machine),
// HKDF-SHA256 (sel vide, info = "nokker-sites-v1|" + rid), AES-256-GCM (AAD = rid) sur {"value": …}.
// La valeur ne quitte la page que chiffrée ; le champ est vidé par l'appelant après l'envoi.
const b64u = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
export async function sealValue(machineJwk, rid, value) {
  const enc = new TextEncoder();
  const machine = await crypto.subtle.importKey("jwk", { ...machineJwk, ext: true }, { name: "ECDH", namedCurve: "P-256" }, false, []);
  const eph = await crypto.subtle.generateKey({ name: "ECDH", namedCurve: "P-256" }, true, ["deriveBits"]);
  const shared = await crypto.subtle.deriveBits({ name: "ECDH", public: machine }, eph.privateKey, 256);
  const hk = await crypto.subtle.importKey("raw", shared, "HKDF", false, ["deriveKey"]);
  const key = await crypto.subtle.deriveKey(
    { name: "HKDF", hash: "SHA-256", salt: new Uint8Array(0), info: enc.encode("nokker-sites-v1|" + rid) },
    hk, { name: "AES-GCM", length: 256 }, false, ["encrypt"]);
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const ct = await crypto.subtle.encrypt({ name: "AES-GCM", iv, additionalData: enc.encode(rid) }, key,
    enc.encode(JSON.stringify({ value })));
  const epk = await crypto.subtle.exportKey("jwk", eph.publicKey);
  return { v: 1, epk: { kty: "EC", crv: "P-256", x: epk.x, y: epk.y }, iv: b64u(iv), ct: b64u(ct) };
}
