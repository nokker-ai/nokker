/*
 * Nokker — heurtoir, détecteur « toc toc toc » v2 pour le navigateur (AudioWorklet). docs/KNOCK.md §7.
 *
 * Fichier AUTONOME, sans dépendance. Même algorithme que nokker/homebridge/knock/v2.py (parité vérifiée par
 * tests/unit/knock/test_js_parity.py) : mel-spectrogramme (32 bandes, trames de 5 ms, fenêtre de Hann de 25 ms),
 * fond par bande = médiane glissante des 400 ms passées, percussivité = médiane sur les bandes des hausses au-dessus
 * du fond (séparation percussive par filtrage médian), petit réseau convolutif sur le patch de 100 ms de chaque
 * attaque (poids `cnn.json`, passés par `processorOptions.cnn`), puis rythme de trois coups.
 *
 * Vie privée : l'audio ne quitte JAMAIS ce processeur. Il ne garde que ~100 trames de 32 niveaux (aucun échantillon
 * au-delà des 25 ms de la fenêtre), et ne poste au fil principal que des faits : { type: "knock", t, intervals,
 * confidence, probs }, { type: "hit", t, prob, accepted } (score de chaque attaque, pour la page de démonstration),
 * l'état. Jamais du son.
 *
 * Chargé par `audioContext.audioWorklet.addModule(url)` ; dans Node (tests), expose `globalThis.NokkerKnockDSP`.
 */
(function (root) {
  "use strict";

  var HOP_S = 0.005, WIN_S = 0.025, N_MELS = 32, FMIN = 100.0, FMAX = 7500.0;
  var SERIES_SIM = 0.95, MERGE_S = 0.12, BG_FRAMES = 80, BG_EVERY = 8, PRE = 4, POST = 15, WARMUP = 40, DB_EPS = 1e-10, EVAL_S = 0.1;

  var DEFAULTS = {
    odf_min_db: 4.0,
    prob_min: 0.3,
    refractory_s: 0.03,
    ioi_min_s: 0.10,
    ioi_max_s: 0.80,
    regularity: 0.45,
    triple_min: 0.99,
    level_spread_db: 12.0,
    guard_before_s: 0.8,
    guard_after_s: 0.6,
    hits: 3
  };

  function merge(a, b) {
    var o = {}, k;
    for (k in a) o[k] = a[k];
    if (b) for (k in b) if (Object.prototype.hasOwnProperty.call(a, k)) o[k] = Number(b[k]);
    return o;
  }

  var SCRATCH = new Float64Array(BG_FRAMES);
  function median(arr) { // copie triée (tri numérique des tableaux typés) ; moyenne des deux du milieu si pair
    var n = arr.length, s = SCRATCH.subarray(0, n), h = n >> 1;
    s.set(arr);
    s.sort();
    return n % 2 ? s[h] : 0.5 * (s[h - 1] + s[h]);
  }

  function hzToMel(f) { return 2595.0 * Math.log10(1.0 + f / 700.0); }
  function melToHz(m) { return 700.0 * (Math.pow(10, m / 2595.0) - 1.0); }

  function melFilterbank(sr, nfft) {
    var fmax = Math.min(FMAX, sr / 2 * 0.95), lo = hzToMel(FMIN), hi = hzToMel(fmax), pts = [], m, k;
    for (m = 0; m < N_MELS + 2; m++) pts.push(melToHz(lo + (hi - lo) * m / (N_MELS + 1)));
    var nb = nfft / 2 + 1, fb = [];
    for (m = 0; m < N_MELS; m++) {
      var a = pts[m], c = pts[m + 1], b = pts[m + 2], row = [];
      for (k = 0; k < nb; k++) {
        var f = k * sr / nfft, v = 0;
        if (f > a && f < b) v = f <= c ? (f - a) / (c - a) : (b - f) / (b - c);
        if (v) row.push([k, v]);
      }
      fb.push(row);
    }
    return fb;
  }

  // FFT complexe radix-2 en place, tables précalculées (permutation, cos/sin)
  function makeFFT(n) {
    var rev = new Uint32Array(n), cos = new Float64Array(n / 2), sin = new Float64Array(n / 2), i, j = 0;
    for (i = 1; i < n; i++) {
      var bit = n >> 1;
      for (; j & bit; bit >>= 1) j ^= bit;
      j ^= bit;
      rev[i] = j;
    }
    for (i = 0; i < n / 2; i++) { cos[i] = Math.cos(-2 * Math.PI * i / n); sin[i] = Math.sin(-2 * Math.PI * i / n); }
    return function (re, im) {
      var i, k, len, t;
      for (i = 1; i < n; i++) {
        var r = rev[i];
        if (i < r) { t = re[i]; re[i] = re[r]; re[r] = t; t = im[i]; im[i] = im[r]; im[r] = t; }
      }
      for (len = 2; len <= n; len <<= 1) {
        var half = len >> 1, step = n / len;
        for (i = 0; i < n; i += len) {
          for (k = 0; k < half; k++) {
            var a = i + k, b = a + half, cr = cos[k * step], ci = sin[k * step];
            var xr = re[b] * cr - im[b] * ci, xi = re[b] * ci + im[b] * cr;
            re[b] = re[a] - xr; im[b] = im[a] - xi; re[a] += xr; im[a] += xi;
          }
        }
      }
    };
  }

  /** Réseau (nokker/homebridge/knock/v2.py::KnockCNN) : poids { w1, b1, w2, b2, w3, b3, wd, bd }. */
  function makeCNN(d) {
    if (!d || !d.w1) return null;
    function conv(x, h, w, W, B) { // x[c][i][j] → ReLU(conv « same » 3×3) ; canaux lus dans les poids
      var cout = B.length, cin = W[0].length, out = [], o, c, i, j, di, dj;
      for (o = 0; o < cout; o++) {
        var plane = [];
        for (i = 0; i < h; i++) {
          var row = new Float64Array(w);
          for (j = 0; j < w; j++) {
            var z = B[o];
            for (c = 0; c < cin; c++) {
              var Wc = W[o][c], X = x[c];
              for (di = 0; di < 3; di++) {
                var ii = i + di - 1;
                if (ii < 0 || ii >= h) continue;
                var Xi = X[ii], Wd = Wc[di];
                for (dj = 0; dj < 3; dj++) {
                  var jj = j + dj - 1;
                  if (jj >= 0 && jj < w) z += Wd[dj] * Xi[jj];
                }
              }
            }
            row[j] = z > 0 ? z : 0;
          }
          plane.push(row);
        }
        out.push(plane);
      }
      return out;
    }
    function pool(x, h, w) {
      var h2 = h >> 1, w2 = w >> 1;
      return x.map(function (P) {
        var out = [];
        for (var i = 0; i < h2; i++) {
          var row = new Float64Array(w2);
          for (var j = 0; j < w2; j++) {
            row[j] = Math.max(P[2 * i][2 * j], P[2 * i][2 * j + 1], P[2 * i + 1][2 * j], P[2 * i + 1][2 * j + 1]);
          }
          out.push(row);
        }
        return out;
      });
    }
    return {
      prob: function (patch) { // patch : tableau [20][32]
        var h = patch.length, w = patch[0].length;
        var x = conv([patch], h, w, d.w1, d.b1);
        x = pool(x, h, w); h >>= 1; w >>= 1;
        x = conv(x, h, w, d.w2, d.b2);
        x = pool(x, h, w); h >>= 1; w >>= 1;
        x = conv(x, h, w, d.w3, d.b3);
        var z = d.bd;
        for (var c = 0; c < x.length; c++) {
          var s = 0;
          for (var i = 0; i < h; i++) for (var j = 0; j < w; j++) s += x[c][i][j];
          z += d.wd[c] * s / (h * w);
        }
        z = Math.max(-40, Math.min(40, z));
        return 1 / (1 + Math.exp(-z));
      }
    };
  }

  function round4(x) { return Math.round(x * 10000) / 10000; }

  function cos(a, b) {
    var ab = 0, aa = 0, bb = 0;
    for (var i = 0; i < a.length; i++) { ab += a[i] * b[i]; aa += a[i] * a[i]; bb += b[i] * b[i]; }
    var na = Math.sqrt(aa), nb = Math.sqrt(bb);
    return na > 0 && nb > 0 ? ab / (na * nb) : 0;
  }

  /** Mesures d'un triplet (nokker/homebridge/knock/v2.py::triple_features). */
  function tripleFeatures(tri, burst, win, ioi, spread, ext) {
    var pr = tri.map(function (h) { return h.features.prob; });
    var lv = tri.map(function (h) { return h.features.level; });
    var sg = tri.map(function (h) { return h.features.sig; });
    var sims = [cos(sg[0], sg[1]), cos(sg[1], sg[2]), cos(sg[0], sg[2])];
    var others = burst.filter(function (h) { return tri.indexOf(h) < 0; });
    var span = (tri[2].t - tri[0].t) + 1.6;
    var around = win.filter(function (w) {
      return tri.every(function (t) { return Math.abs(w[0] - t.t) > 1e-9; });
    });
    var top = around.length ? Math.max.apply(null, around.map(function (w) { return w[2]; })) : null;
    var meanLv = (lv[0] + lv[1] + lv[2]) / 3, maxP = 0, maxS = 0, minOdf = Infinity, i, j;
    for (i = 0; i < 3; i++) minOdf = Math.min(minOdf, tri[i].features.odf);
    for (i = 0; i < others.length; i++) {
      maxP = Math.max(maxP, others[i].features.prob);
      for (j = 0; j < 3; j++) maxS = Math.max(maxS, cos(others[i].features.sig, sg[j]));
    }
    return [
      Math.min(pr[0], pr[1], pr[2]), (pr[0] + pr[1] + pr[2]) / 3,
      Math.min(3, minOdf / 20),
      (Math.max(lv[0], lv[1], lv[2]) - Math.min(lv[0], lv[1], lv[2])) / 10,
      spread,
      ioi,
      Math.min(sims[0], sims[1], sims[2]), (sims[0] + sims[1] + sims[2]) / 3,
      Math.min(2, others.length / 5),
      Math.min(4, win.length / span / 5),
      maxP,
      maxS,
      ext ? 1 : 0,
      top === null ? 3 : Math.max(-3, Math.min(3, (meanLv - top) / 10))
    ];
  }

  /** Juge des triplets (v2.py::TripleScorer) : { mean, std, w1, b1, w2, b2 }. */
  function makeScorer(d) {
    if (!d || !d.w1) return null;
    return {
      score: function (f) {
        var x = f.map(function (v, i) { return (v - d.mean[i]) / (d.std[i] || 1); }), z = d.b2;
        for (var k = 0; k < d.w1.length; k++) {
          var h = d.b1[k];
          for (var i = 0; i < x.length; i++) h += d.w1[k][i] * x[i];
          if (h > 0) z += d.w2[k] * h;
        }
        z = Math.max(-40, Math.min(40, z));
        return 1 / (1 + Math.exp(-z));
      }
    };
  }

  /** Rythme (nokker/homebridge/knock/v2.py::RhythmV2). */
  function Rhythm(s, scorer) { this.s = s; this.scorer = scorer; this.hits = []; this.next = 0; this.all = []; }

  Rhythm.prototype.add = function (hit) {
    this.all.push([hit.t, hit.features.prob, hit.features.level]);
    if (hit.accepted) this.hits.push(hit);
  };

  Rhythm.prototype.tick = function (now) {
    var s = this.s;
    var horizon = now - (s.guard_before_s + s.hits * s.ioi_max_s + s.guard_after_s + 2.0);
    if (this.all.length && this.all[0][0] < horizon) this.all = this.all.filter(function (w) { return w[0] >= horizon; });
    if (this.hits.length && this.hits[0].t < horizon) {
      var keep = this.hits.filter(function (h) { return h.t >= horizon; });
      this.next = Math.max(0, this.next - (this.hits.length - keep.length));
      this.hits = keep;
    }
    var closed = now - (s.guard_after_s + EVAL_S + POST * HOP_S), out = null;
    while (this.next < this.hits.length && this.hits[this.next].t <= closed) {
      var k = this.next;
      this.next += 1;
      var ev = this.judgeLast(k, now);
      if (ev) {
        out = ev;
        this.hits = this.hits.slice(k + 1);
        this.next = 0;
      }
    }
    return out;
  };

  Rhythm.prototype.judgeLast = function (k, now) {
    var s = this.s, hs = this.hits, last = hs[k];
    var start = last.t - (s.hits - 1) * s.ioi_max_s - s.guard_before_s;
    var burst = hs.filter(function (h) { return h.t >= start && h.t <= last.t + s.guard_after_s + EVAL_S; });
    var prior = burst.filter(function (h) { return h.t < last.t; });
    var best = null, n = prior.length, a, b, j, all = this.all;
    for (a = 0; a < n; a++) for (b = a + 1; b < n; b++) {
      var tri = [prior[a], prior[b], last];
      var ivs = [round4(tri[1].t - tri[0].t), round4(tri[2].t - tri[1].t)];
      if (Math.min(ivs[0], ivs[1]) < s.ioi_min_s || Math.max(ivs[0], ivs[1]) > s.ioi_max_s) continue;
      var mx = Math.max(ivs[0], ivs[1]), spread = (mx - Math.min(ivs[0], ivs[1])) / mx;
      if (spread > s.regularity) continue;
      var lv = tri.map(function (h) { return h.features.level; });
      if (Math.max.apply(null, lv) - Math.min.apply(null, lv) > s.level_spread_db) continue;
      var ioi = (ivs[0] + ivs[1]) / 2, mean = (lv[0] + lv[1] + lv[2]) / 3, ext = false;
      for (j = 0; j < burst.length; j++) {
        var h = burst[j];
        if (tri.indexOf(h) >= 0 || Math.abs(h.features.level - mean) > s.level_spread_db) continue;
        if ((Math.abs(h.t - (tri[0].t - ioi)) < 0.35 * ioi || Math.abs(h.t - (tri[2].t + ioi)) < 0.35 * ioi) &&
            Math.max(cos(h.features.sig, tri[0].features.sig), cos(h.features.sig, tri[1].features.sig),
                     cos(h.features.sig, tri[2].features.sig)) >= SERIES_SIM) {
          ext = true; // même tempo, même niveau, même son : une série (pas, batterie), pas « toc toc toc »
          break;
        }
      }
      if (ext) continue;
      var score;
      if (!this.scorer) {
        score = (tri[0].features.prob + tri[1].features.prob + tri[2].features.prob) / 3;
      } else {
        var t0 = tri[0].t, t2 = tri[2].t;
        var win = all.filter(function (w) { return w[0] >= t0 - 1.0 && w[0] <= t2 + 0.6; });
        score = this.scorer.score(tripleFeatures(tri, burst, win, ioi, spread, ext));
      }
      if (best === null || score > best[0]) best = [score, tri, ivs];
    }
    if (best === null || best[0] < s.triple_min) return null;
    return { type: "knock", t: best[1][2].t, fired_at: now, intervals: best[2], confidence: Math.round(best[0] * 1000) / 1000,
      probs: best[1].map(function (h) { return Math.round(h.features.prob * 1000) / 1000; }) };
  };

  /** Détecteur en flux. `process(Float32Array)` → événements (souvent aucun). */
  function lowpass(fc, sr) {
    var w0 = 2 * Math.PI * fc / sr, alpha = Math.sin(w0) / (2 * 0.7071067811865476), cw = Math.cos(w0);
    var a0 = 1 + alpha;
    return { b0: (1 - cw) / 2 / a0, b1: (1 - cw) / a0, b2: (1 - cw) / 2 / a0, a1: -2 * cw / a0, a2: (1 - alpha) / a0 };
  }

  function KnockDSP(sampleRate, settings, options) {
    options = options || {};
    this.inSr = sampleRate;
    // 32 kHz et plus : passe-bas (2 biquads) puis 1 échantillon sur `dec` (calcul à ~16 kHz), comme v2.py
    this.dec = sampleRate >= 32000 ? Math.max(1, Math.round(sampleRate / 16000)) : 1;
    sampleRate = sampleRate / this.dec;
    this.lp = this.dec > 1 ? lowpass(0.42 * sampleRate, this.inSr) : null;
    this.sr = sampleRate;
    this.s = merge(DEFAULTS, settings);
    this.onHit = options.onHit || null;
    this.cnn = options.cnn || null; // objet { prob(patch) } (makeCNN) ou null : toute attaque passe
    this.scorer = options.scorer || null; // objet { score(f) } (makeScorer) ou null : probabilité moyenne
    this.hop = Math.max(1, Math.round(sampleRate * HOP_S));
    this.win = Math.max(8, Math.round(sampleRate * WIN_S));
    this.nfft = 1;
    while (this.nfft < this.win) this.nfft <<= 1;
    this.hopS = this.hop / sampleRate;
    this.window = new Float64Array(this.win);
    for (var i = 0; i < this.win; i++) this.window[i] = 0.5 - 0.5 * Math.cos(2 * Math.PI * i / (this.win - 1));
    this.fb = melFilterbank(sampleRate, this.nfft);
    this.re = new Float64Array(this.nfft);
    this.im = new Float64Array(this.nfft);
    this.fft = makeFFT(this.nfft);
    this.rise = new Float64Array(N_MELS);
    this.col = new Float64Array(BG_FRAMES);
    this.reset();
  }

  KnockDSP.prototype.reset = function () {
    this.z = [0, 0, 0, 0]; // états des deux biquads (z1, z2)
    this.phase = 0;
    this.buf = new Float64Array(this.win); // fenêtre glissante des `win` derniers échantillons
    this.pos = 0;                            // position d'écriture (anneau)
    this.fill = 0;
    this.n = 0;
    this.keep = BG_FRAMES + PRE + 1 + POST + 2;
    this.mel = [];
    this.bgHist = [];
    this.odf = new Float64Array(this.keep);
    for (var i = 0; i < this.keep; i++) { this.mel.push(new Float64Array(N_MELS)); this.bgHist.push(null); }
    this.bg = null;
    this.lastCand = -1e9;
    this.lastHitT = -1e9;
    this.rhythm = new Rhythm(this.s, this.scorer);
  };

  KnockDSP.prototype.process = function (x) {
    var out = [], i, ev, lp = this.lp, z = this.z, v;
    for (i = 0; i < x.length; i++) {
      v = x[i];
      if (lp) {
        var y = lp.b0 * v + z[0];
        z[0] = lp.b1 * v - lp.a1 * y + z[1];
        z[1] = lp.b2 * v - lp.a2 * y;
        var y2 = lp.b0 * y + z[2];
        z[2] = lp.b1 * y - lp.a1 * y2 + z[3];
        z[3] = lp.b2 * y - lp.a2 * y2;
        var keepIt = this.phase === 0;
        this.phase = (this.phase + 1) % this.dec;
        if (!keepIt) continue;
        v = y2;
      }
      this.buf[this.pos] = v;
      this.pos = (this.pos + 1) % this.win;
      if (++this.fill === this.hop) {
        this.fill = 0;
        ev = this.frame();
        if (ev) out.push(ev);
      }
    }
    return out;
  };

  KnockDSP.prototype.frame = function () {
    var win = this.win, re = this.re, im = this.im, i, k, m;
    for (i = 0; i < this.nfft; i++) { re[i] = 0; im[i] = 0; }
    for (i = 0; i < win; i++) re[i] = this.buf[(this.pos + i) % win] * this.window[i]; // du plus ancien au plus récent
    this.fft(re, im);
    var n = this.n, keep = this.keep;
    var row = this.mel[n % keep]; // réutilise la ligne la plus ancienne de l'anneau (plus nécessaire)
    for (m = 0; m < N_MELS; m++) {
      var f = this.fb[m], e = 0;
      for (k = 0; k < f.length; k++) {
        var b = f[k][0];
        e += f[k][1] * (re[b] * re[b] + im[b] * im[b]) / win;
      }
      row[m] = 10 * Math.log10(e + DB_EPS);
    }
    if (this.bg === null) this.bg = Float64Array.from(row);
    else if (n % BG_EVERY === 0 && n > 0) {
      var cnt = Math.min(n, BG_FRAMES), bg = new Float64Array(N_MELS), col = this.col.subarray(0, cnt);
      for (m = 0; m < N_MELS; m++) {
        for (k = 0; k < cnt; k++) col[k] = this.mel[((n - 1 - k) % keep + keep) % keep][m];
        bg[m] = median(col);
      }
      this.bg = bg;
    }
    this.bgHist[n % keep] = this.bg;
    var rise = this.rise;
    for (m = 0; m < N_MELS; m++) rise[m] = Math.max(row[m] - this.bg[m], 0);
    this.odf[n % keep] = median(rise);
    this.n += 1;
    var c = n - POST, s = this.s, o = this.odf;
    if (c >= WARMUP) {
      var oc = o[c % keep];
      if (oc >= s.odf_min_db && oc >= o[(c - 1) % keep] && oc >= o[(c - 2) % keep] && oc > o[(c + 1) % keep] &&
          oc > o[(c + 2) % keep] && (c - this.lastCand) * this.hopS >= s.refractory_s) {
        this.lastCand = c;
        this.judge(c, oc);
      }
    }
    return this.rhythm.tick(this.n * this.hopS);
  };

  KnockDSP.prototype.judge = function (c, odf) {
    var keep = this.keep, bg = this.bgHist[c % keep], patch = [], d, m;
    for (d = -PRE; d <= POST; d++) {
      var row = this.mel[(c + d) % keep], p = new Float64Array(N_MELS);
      for (m = 0; m < N_MELS; m++) p[m] = Math.min(50, Math.max(-20, row[m] - bg[m])) / 20;
      patch.push(p);
    }
    var prob = this.cnn ? this.cnn.prob(patch) : 1.0;
    var sum = 0;
    for (m = 0; m < N_MELS; m++) {
      var mx = -Infinity;
      for (d = 0; d < 3; d++) mx = Math.max(mx, this.mel[(c + d) % keep][m]);
      sum += Math.pow(10, mx / 10);
    }
    var sig = new Float64Array(N_MELS);
    for (m = 0; m < N_MELS; m++) {
      var pk = -Infinity;
      for (d = 0; d < 3; d++) pk = Math.max(pk, this.mel[(c + d) % keep][m]);
      sig[m] = Math.min(50, Math.max(0, pk - bg[m]));
    }
    var f = { odf: odf, snr: odf, prob: prob, level: 10 * Math.log10(sum + DB_EPS), sig: sig };
    var hit = { t: c * this.hopS, accepted: prob >= this.s.prob_min, features: f };
    if (this.onHit) this.onHit(hit);
    if (hit.accepted && hit.t - this.lastHitT < MERGE_S) { // rebond du même coup : un seul, le plus fort
      var hs = this.rhythm.hits;
      if (hs.length && hs[hs.length - 1].t === this.lastHitT && f.level > hs[hs.length - 1].features.level) {
        hs[hs.length - 1] = hit;
        this.lastHitT = hit.t;
      }
      return;
    }
    if (hit.accepted) this.lastHitT = hit.t;
    this.rhythm.add(hit);
  };

  /**
   * Calibrage (« frappe trois fois ») : sur les attaques mesurées pendant ~30 s (réseau compris, seuils larges),
   * les trois plus percussives ; resserre le tempo et adapte les seuils. Même règle que
   * nokker/homebridge/knock/calibrate.py::calibrate_v2.
   */
  function calibrate(hits, base) {
    var s = merge(DEFAULTS, base);
    var good = hits.slice().sort(function (a, b) { return b.features.odf - a.features.odf; }).slice(0, 3);
    if (good.length < 3) return null;
    var g = good.sort(function (a, b) { return a.t - b.t; });
    var ivs = [g[1].t - g[0].t, g[2].t - g[1].t];
    if (Math.min.apply(null, ivs) < 0.08 || Math.max.apply(null, ivs) > 1.2) return null;
    function lo(key) { return Math.min.apply(null, g.map(function (h) { return h.features[key]; })); }
    function clamp(v, a, b) { return Math.max(a, Math.min(b, v)); }
    s.odf_min_db = clamp(lo("odf") * 0.4, 2.5, s.odf_min_db);
    s.prob_min = clamp(lo("prob") * 0.7, 0.2, s.prob_min);
    s.ioi_min_s = clamp(Math.min.apply(null, ivs) * 0.5, 0.08, 0.3);
    s.ioi_max_s = clamp(Math.max.apply(null, ivs) * 1.8, 0.4, 1.2);
    return s;
  }

  var api = { KnockDSP: KnockDSP, DEFAULTS: DEFAULTS, calibrate: calibrate, makeCNN: makeCNN, makeScorer: makeScorer,
    tripleFeatures: tripleFeatures, version: "2" };
  root.NokkerKnockDSP = api;

  if (typeof registerProcessor === "function" && typeof AudioWorkletProcessor === "function") {
    class NokkerKnockProcessor extends AudioWorkletProcessor {
      constructor(options) {
        super();
        var o = (options && options.processorOptions) || {};
        this.muted = false;
        this.calibrating = false;
        this.calHits = [];
        this.settings = o.settings || {};
        this.cnn = makeCNN(o.cnn);
        this.scorer = makeScorer(o.triple);
        this.make(this.settings);
        var self = this;
        this.port.onmessage = function (e) { self.command(e.data || {}); };
        this.port.postMessage({ type: "state", state: "listening" });
      }
      make(settings) {
        var self = this;
        this.dsp = new KnockDSP(sampleRate, settings, {
          cnn: this.cnn,
          scorer: this.scorer,
          onHit: function (h) {
            if (self.calibrating) self.calHits.push(h);
            self.port.postMessage({ type: "hit", t: h.t, prob: Math.round(h.features.prob * 1000) / 1000,
              accepted: h.accepted, odf: Math.round(h.features.odf * 10) / 10 });
          }
        });
      }
      command(m) {
        if (m.type === "mute") {
          this.muted = !!m.muted;
          this.dsp.reset(); // rien de l'avant-mute ne survit
          this.port.postMessage({ type: "state", state: this.muted ? "muted" : "listening" });
        } else if (m.type === "settings") {
          this.settings = m.settings || {};
          this.make(this.settings);
        } else if (m.type === "calibrate") {
          this.calibrating = true;
          this.calHits = [];
          this.make(Object.assign({}, this.settings, { odf_min_db: 2.5, prob_min: 0 }));
          this.port.postMessage({ type: "state", state: "calibrating" });
        } else if (m.type === "calibrate-end") {
          var s = calibrate(this.calHits, this.settings);
          this.calibrating = false;
          this.calHits = [];
          if (s) this.settings = s;
          this.make(this.settings);
          this.port.postMessage({ type: "calibrated", ok: !!s, settings: s });
          this.port.postMessage({ type: "state", state: this.muted ? "muted" : "listening" });
        }
      }
      process(inputs) {
        var ch = inputs[0] && inputs[0][0];
        if (!ch || this.muted) return true;
        var ev = this.dsp.process(ch);
        if (this.calibrating) return true; // pendant le calibrage, on mesure, on ne déclenche pas
        for (var i = 0; i < ev.length; i++) this.port.postMessage(ev[i]);
        return true;
      }
    }
    registerProcessor("nokker-knock", NokkerKnockProcessor);
  }
})(typeof globalThis !== "undefined" ? globalThis : this);
