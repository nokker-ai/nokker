"""Calibrage optionnel en 30 s : « frappe trois fois » (docs/KNOCK.md §3.2).

Le détecteur écoute avec des seuils larges, mesure les attaques, garde les trois plus fortes, et resserre les réglages
autour de CES coups (surface, pièce, micro). Seules des mesures (niveaux, intervalles) servent : aucun son n'est gardé.
Même règle que `calibrate` dans web/knock/knock-worklet.js.
"""
from __future__ import annotations

from dataclasses import replace

from nokker.homebridge.knock.detector import Hit, KnockSettings

LISTEN = KnockSettings(flux_min_db=15.0, snr_min_db=4.0, attack_min=-9, decay_min=-9, hf_min_db=-99)


def _clamp(v: float, a: float, b: float) -> float:
    return max(a, min(b, v))


def calibrate(hits: list[Hit], base: KnockSettings | None = None) -> KnockSettings | None:
    """Réglages resserrés, ou None si les trois coups n'ont pas été reconnus (on garde alors les réglages actuels)."""
    s = base or KnockSettings()
    good = [h for h in hits if h.features["attack"] > 0.4 and h.features["decay"] > 0.3]
    if len(good) < 3:
        return None
    g = sorted(sorted(good, key=lambda h: -h.features["level"])[:3], key=lambda h: h.t)
    ivs = [g[1].t - g[0].t, g[2].t - g[1].t]
    if min(ivs) < 0.08 or max(ivs) > 1.2:
        return None

    def lo(key: str) -> float:
        return min(h.features[key] for h in g)

    return replace(
        s,
        attack_min=_clamp(lo("attack") * 0.7, 0.4, s.attack_min),
        decay_min=_clamp(lo("decay") * 0.7, 0.3, s.decay_min),
        hf_min_db=_clamp(lo("hf") - 6, -30, -6),
        snr_min_db=_clamp(lo("snr") - 10, 6, 20),
        ioi_min_s=_clamp(min(ivs) * 0.5, 0.08, 0.3),
        ioi_max_s=_clamp(max(ivs) * 1.8, 0.4, 1.2),
    )


def calibrate_v2(hits: list[Hit], base=None):
    """Calibrage du détecteur v2 (même règle que `calibrate` de web/knock/knock-worklet.js) : les trois attaques les
    plus percussives, tempo resserré, seuils de percussivité et du réseau adaptés à CES coups. (Un gabarit spectral
    de la surface a été mesuré et écarté : dans le bruit, il ne sépare pas mieux les coups que le juge des triplets.)"""
    from nokker.homebridge.knock.v2 import KnockSettingsV2

    s = base or KnockSettingsV2()
    g = sorted(sorted(hits, key=lambda h: -h.features["odf"])[:3], key=lambda h: h.t)
    if len(g) < 3:
        return None
    ivs = [g[1].t - g[0].t, g[2].t - g[1].t]
    if min(ivs) < 0.08 or max(ivs) > 1.2:
        return None
    return replace(
        s,
        odf_min_db=_clamp(min(h.features["odf"] for h in g) * 0.4, 2.5, s.odf_min_db),
        prob_min=_clamp(min(h.features["prob"] for h in g) * 0.7, 0.2, s.prob_min),
        ioi_min_s=_clamp(min(ivs) * 0.5, 0.08, 0.3),
        ioi_max_s=_clamp(max(ivs) * 1.8, 0.4, 1.2),
    )
