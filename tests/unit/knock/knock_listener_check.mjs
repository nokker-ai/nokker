// Contrôle de `KnockListener` (web/knock/knock.js) avec des API navigateur simulées : permission micro sans
// traitement, états, relais d'un « knock », mute (piste coupée + détecteur remis à zéro), calibrage, arrêt.
// Lancé par tests/unit/knock/test_js_parity.py::test_knock_listener_main_thread. Sort en code 1 au premier écart.
import assert from "node:assert/strict";

const posted = [];
const tracks = [{ enabled: true, stopped: false, stop() { this.stopped = true; } }];
let constraints = null;
let node = null;
class FakeNode {
  constructor(ctx, name, opts) {
    this.name = name; this.opts = opts; node = this;
    this.port = { postMessage: (m) => posted.push(m), onmessage: null };
  }
}
globalThis.window = {
  AudioContext: class {
    constructor() { this.state = "running"; this.audioWorklet = { addModule: async (u) => { this.url = u; } }; }
    createMediaStreamSource() { return { connect: () => {} }; }
    close() { this.closed = true; return Promise.resolve(); }
  },
  AudioWorkletNode: FakeNode,
};
globalThis.AudioContext = window.AudioContext;
globalThis.AudioWorkletNode = FakeNode;
Object.defineProperty(globalThis, "navigator", { configurable: true, value: { mediaDevices: { getUserMedia:
  async (c) => {
    constraints = c;
    return { getAudioTracks: () => tracks, getTracks: () => tracks };
  } } } });
globalThis.localStorage = { store: {}, getItem(k) { return this.store[k] ?? null; }, setItem(k, v) { this.store[k] = v; } };

const { KnockListener } = await import(process.argv[2]);
const states = [];
const knocks = [];
const scores = [];
let fetched = null;
const fetchedAll = [];
globalThis.fetch = async (u) => {
  fetched = fetchedAll.length ? fetched : u;
  fetchedAll.push(u);
  return { ok: true, json: async () => (u.includes("triple") ? { threshold: 0.9, weights: { w1: [2] } } : { weights: { w1: [1] } }) };
};
const k = new KnockListener({ workletUrl: "/knock/knock-worklet.js", cnnUrl: "/knock/cnn.json", tripleUrl: "/knock/triple.json",
  onState: (s) => states.push(s), onKnock: (e) => knocks.push(e), onScore: (h) => scores.push(h),
  settingsKey: "nokker.knock.settings" });
assert.equal(KnockListener.supported(), true);
await k.start();
assert.deepEqual(constraints.audio, { echoCancellation: false, noiseSuppression: false, autoGainControl: false,
  channelCount: 1 });
assert.equal(node.name, "nokker-knock");
assert.equal(node.opts.numberOfOutputs, 0); // aucune sortie audio : le son ne va nulle part
assert.equal(fetched, "/knock/cnn.json");
assert.deepEqual(node.opts.processorOptions.cnn, { w1: [1] }); // poids du réseau passés au processeur
assert.deepEqual(fetchedAll, ["/knock/cnn.json", "/knock/triple.json"]);
assert.deepEqual(node.opts.processorOptions.triple, { w1: [2] });
assert.deepEqual(node.opts.processorOptions.settings, { triple_min: 0.9 }); // seuil appris, sauf réglages calibrés
node.port.onmessage({ data: { type: "hit", t: 1.5, prob: 0.93, accepted: true, odf: 12.1 } });
assert.deepEqual(scores, [{ t: 1.5, prob: 0.93, accepted: true, odf: 12.1 }]);
node.port.onmessage({ data: { type: "state", state: "listening" } });
node.port.onmessage({ data: { type: "knock", t: 2.1, intervals: [0.3, 0.31], confidence: 0.9, probs: [0.9, 0.8, 0.95],
  extra: "x" } });
assert.deepEqual(knocks, [{ t: 2.1, intervals: [0.3, 0.31], confidence: 0.9, probs: [0.9, 0.8, 0.95] }]);
k.mute(true);
assert.equal(tracks[0].enabled, false);
assert.deepEqual(posted.at(-1), { type: "mute", muted: true });
const cal = k.calibrate(0.01);
assert.deepEqual(posted.at(-1), { type: "calibrate" });
node.port.onmessage({ data: { type: "calibrated", ok: true, settings: { ioi_max_s: 0.6 } } });
assert.deepEqual(await cal, { ioi_max_s: 0.6 });
assert.equal(localStorage.getItem("nokker.knock.settings"), JSON.stringify({ ioi_max_s: 0.6 }));
k.stop();
assert.equal(tracks[0].stopped, true);
assert.deepEqual(states, ["starting", "listening", "off"]);
console.log("ok");
