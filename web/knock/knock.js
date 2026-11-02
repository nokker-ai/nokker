/*
 * Nokker — heurtoir côté navigateur : API du fil principal (module ES autonome). docs/KNOCK.md §7.
 *
 *   import { KnockListener } from "./knock.js";
 *   const knock = new KnockListener({
 *     workletUrl: "/knock/knock-worklet.js",          // servi tel quel, même origine
 *     onKnock: (ev) => openVoice(),                    // { t, intervals, confidence, probs }
 *     onScore: (h) => {},                              // facultatif : score de chaque attaque { t, prob, accepted }
 *     onState: (state) => showMicBadge(state),         // "off" | "starting" | "listening" | "muted" | "calibrating" | "error"
 *   });
 *   await knock.start();      // demande le micro (geste de l'utilisateur requis par les navigateurs)
 *   knock.mute(true);         // mute logiciel : la piste est coupée ET le détecteur oublie tout
 *   await knock.calibrate();  // « frappe trois fois » dans les 30 s → réglages resserrés (ou null)
 *   knock.stop();             // libère le micro
 *
 * Vie privée : l'audio reste dans l'AudioWorklet ; ce module ne reçoit que des faits (« trois coups à t »), n'envoie
 * rien sur le réseau et ne stocke rien (sauf, si on le lui demande, les RÉGLAGES calibrés : `settingsKey`). L'appli
 * doit montrer `state` à l'écran tant qu'il n'est pas "off" (exigence produit : un indicateur visible signale l'écoute).
 */

const PROCESSOR = "nokker-knock";

export class KnockListener {
  constructor(options = {}) {
    this.workletUrl = options.workletUrl || new URL("./knock-worklet.js", import.meta.url).href;
    this.onKnock = options.onKnock || (() => {});
    this.onState = options.onState || (() => {});
    this.settings = options.settings || null;
    this.cnnUrl = options.cnnUrl || new URL("./cnn.json", import.meta.url).href; // poids du réseau (≈ 60 ko)
    this.cnn = options.cnn || null;                 // ou les poids déjà chargés ({ weights } de cnn.json)
    this.tripleUrl = options.tripleUrl || new URL("./triple.json", import.meta.url).href; // juge des triplets (~3 ko)
    this.triple = options.triple || null;
    this.onScore = options.onScore || null;         // score de CHAQUE attaque : { t, prob, accepted, odf }
    this.settingsKey = options.settingsKey || null; // ex. "nokker.knock.settings" : réglages calibrés, jamais du son
    this.deviceId = options.deviceId || null;
    this.state = "off";
    this._ctx = null;
    this._stream = null;
    this._node = null;
    this._cal = null;
    if (!this.settings && this.settingsKey) {
      try { this.settings = JSON.parse(localStorage.getItem(this.settingsKey) || "null"); } catch (_) { /* privé */ }
    }
  }

  static supported() {
    return typeof window !== "undefined" && !!(window.AudioContext && window.AudioWorkletNode &&
      navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
  }

  _set(state) {
    if (state === this.state) return;
    this.state = state;
    try { this.onState(state); } catch (e) { console.error(e); }
  }

  async start() {
    if (this.state !== "off" && this.state !== "error") return;
    if (!KnockListener.supported()) { this._set("error"); throw new Error("AudioWorklet indisponible"); }
    this._set("starting");
    try {
      const audio = { echoCancellation: false, noiseSuppression: false, autoGainControl: false, channelCount: 1 };
      if (this.deviceId) audio.deviceId = { exact: this.deviceId };
      this._stream = await navigator.mediaDevices.getUserMedia({ audio });
      this._ctx = new AudioContext({ latencyHint: "interactive" });
      if (!this.cnn) {
        const r = await fetch(this.cnnUrl, { cache: "force-cache" });
        if (!r.ok) throw new Error(`poids du réseau introuvables (${r.status})`);
        this.cnn = await r.json();
      }
      if (!this.triple) {
        const r = await fetch(this.tripleUrl, { cache: "force-cache" });
        if (!r.ok) throw new Error(`juge des triplets introuvable (${r.status})`);
        this.triple = await r.json();
      }
      if (!this.settings && this.triple.threshold) this.settings = { triple_min: this.triple.threshold };
      await this._ctx.audioWorklet.addModule(this.workletUrl);
      const src = this._ctx.createMediaStreamSource(this._stream);
      this._node = new AudioWorkletNode(this._ctx, PROCESSOR, {
        numberOfInputs: 1, numberOfOutputs: 0, channelCount: 1, channelCountMode: "explicit",
        processorOptions: { settings: this.settings || {}, cnn: this.cnn.weights || this.cnn,
                            triple: this.triple.weights || this.triple },
      });
      this._node.port.onmessage = (e) => this._message(e.data || {});
      src.connect(this._node);
      if (this._ctx.state === "suspended") await this._ctx.resume();
    } catch (err) {
      this.stop();
      this._set("error");
      throw err;
    }
  }

  _message(m) {
    if (m.type === "state") this._set(m.state);
    else if (m.type === "hit") {
      if (this.onScore) try { this.onScore({ t: m.t, prob: m.prob, accepted: m.accepted, odf: m.odf }); } catch (e) {
        console.error(e);
      }
    } else if (m.type === "knock") {
      try {
        this.onKnock({ t: m.t, intervals: m.intervals, confidence: m.confidence, probs: m.probs });
      } catch (e) { console.error(e); }
    } else if (m.type === "calibrated" && this._cal) {
      const done = this._cal;
      this._cal = null;
      if (m.ok && m.settings) {
        this.settings = m.settings;
        if (this.settingsKey) {
          try { localStorage.setItem(this.settingsKey, JSON.stringify(m.settings)); } catch (_) { /* privé */ }
        }
      }
      done(m.ok ? m.settings : null);
    }
  }

  mute(muted) {
    if (!this._node) return;
    this._stream.getAudioTracks().forEach((t) => { t.enabled = !muted; });
    this._node.port.postMessage({ type: "mute", muted: !!muted });
  }

  setSettings(settings) {
    this.settings = settings || null;
    if (this._node) this._node.port.postMessage({ type: "settings", settings: this.settings || {} });
  }

  /** « Frappe trois fois » pendant `seconds` (30 par défaut) ; rend les réglages calibrés, ou null. */
  calibrate(seconds = 30) {
    if (!this._node) return Promise.reject(new Error("pas démarré"));
    return new Promise((resolve) => {
      this._cal = resolve;
      this._node.port.postMessage({ type: "calibrate" });
      setTimeout(() => this._node && this._node.port.postMessage({ type: "calibrate-end" }), seconds * 1000);
    });
  }

  /** Fin anticipée du calibrage (l'appli peut l'appeler dès qu'elle a vu trois coups). */
  finishCalibration() {
    if (this._node) this._node.port.postMessage({ type: "calibrate-end" });
  }

  stop() {
    try { if (this._node) this._node.port.onmessage = null; } catch (_) { /* rien */ }
    if (this._stream) this._stream.getTracks().forEach((t) => t.stop());
    if (this._ctx) this._ctx.close().catch(() => {});
    this._node = null;
    this._stream = null;
    this._ctx = null;
    if (this._cal) { const c = this._cal; this._cal = null; c(null); }
    this._set("off");
  }
}

export default KnockListener;
