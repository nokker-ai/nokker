// Fait tourner le détecteur JS (web/knock/knock-worklet.js) sur des fichiers float32 bruts, par blocs de 128
// échantillons comme un AudioWorklet.
// Usage : node run_js_dsp.mjs <worklet.js> <sr> <settings.json|-> <cnn.json|-> <triple.json|-> <f1.f32> [...]
// Sortie : une ligne JSON par fichier : { file, events: [...], hits: [...] }.
import { readFileSync } from "node:fs";
import vm from "node:vm";

const [, , worklet, srArg, settingsArg, cnnArg, tripleArg, ...files] = process.argv;
const sandbox = {};
sandbox.globalThis = sandbox;
vm.runInNewContext(readFileSync(worklet, "utf8"), sandbox);
const { KnockDSP, makeCNN, makeScorer } = sandbox.NokkerKnockDSP;
const scorer = tripleArg === "-" ? null : makeScorer(JSON.parse(readFileSync(tripleArg, "utf8")).weights);
const cnn = cnnArg === "-" ? null : makeCNN(JSON.parse(readFileSync(cnnArg, "utf8")).weights);
const settings = settingsArg === "-" ? {} : JSON.parse(readFileSync(settingsArg, "utf8"));
const sr = Number(srArg);
for (const f of files) {
  const buf = readFileSync(f);
  const x = new Float32Array(buf.buffer, buf.byteOffset, buf.byteLength / 4);
  const hits = [];
  const dsp = new KnockDSP(sr, settings, { onHit: (h) => hits.push(h), cnn, scorer });
  const events = [];
  const t0 = process.cpuUsage();
  for (let i = 0; i < x.length; i += 128) events.push(...dsp.process(x.subarray(i, i + 128)));
  const cpu = process.cpuUsage(t0);
  process.stdout.write(JSON.stringify({ file: f, events, hits, cpu_s: (cpu.user + cpu.system) / 1e6,
    dur_s: x.length / sr }) + "\n");
}
