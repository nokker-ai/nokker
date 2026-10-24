"""Implémentation du port `Judge` (`core.ports.Judge`) — couche 2 du juge Nina QA, PLAN-P1 §2.5/§5.3
(chantier B-2). Questions atomiques à `max_tokens=1` ; probabilité déduite des logprobs quand le
backend les rend. Jamais d'import de `core/*` au-delà du port — `nokker/qa/gates.py` est le seul
appelant en production.

## Approximation de probabilité à logprob unique (à documenter PARTOUT où elle est utilisée)

Mesuré le 17.10.2026 (F5, et revérifié pour B-2) : le Router SOKKAN plafonne `top_logprobs=1` sur
Ship (`qwen/qwen3.8-27b`) ET Deep (`qwen/qwen3.5-397b-a17b`) — `top_logprobs=5` renvoie `HTTP 400`
sur les deux. Avec une seule option de logprob rendue (le token réellement choisi, probabilité `p`
après le softmax du modèle — PAS après un échantillonnage, `temperature=0`), on ne peut PAS
reconstruire une distribution complète. L'approximation retenue :

  - **Question Noul** (2 options, Oui/Non) : `P(Oui) = p` si le token choisi est « Oui », sinon
    `P(Oui) = 1 - p` (et symétriquement pour Non). Elle suppose que toute la masse hors du token
    choisi se reporte sur l'unique autre option — faux dès qu'un 3e token (ponctuation, autre
    casse, autre langue) capte une part de cette masse, mais c'est la seule mesure possible sans
    `top_logprobs > 1`. Le cas réel (llama.cpp auto-hébergé, qui accepte `top_logprobs` plus large, mesuré
    jusqu'à 2) sert de repère pour quantifier l'écart quand les deux mesures sont disponibles.
  - **Question Choice/Score** (3 à 5 options) : un seul logprob ne donne QUE la confiance dans le
    token choisi, jamais une distribution sur les autres lettres/chiffres. `probabilities` ne
    contient alors que `{choix: p}`. L'ECE/Brier (`calibrate.py`) se calculent sur (confiance du
    choix, le choix est-il correct ?), ce qui reste une mesure de calibration valide quel que soit
    le nombre d'options (définition standard de l'ECE par confiance du top-1), mais ce n'est PAS
    une calibration de chaque option individuellement.

Si le backend ne rend aucun logprob (`--no-logprobs`, ou un amont qui ne les supporte pas du tout) :
`probabilities=None`, le verdict ne s'appuie que sur le token (pas de seuil de probabilité possible,
`calibrate.py` l'écrit « non mesurable », jamais déduit).

## Cache par contenu

Clé = hash(question_id, prompt, options, modèle) — PAS par position dans un fil : deux appels
identiques (même question posée deux fois dans un tour, tour rejoué) ne déclenchent qu'UN appel
réseau. Le cache est en mémoire pour un process ; il ne traverse pas les redémarrages (pas un souci
de sûreté, seulement de coût/latence).
"""
from __future__ import annotations

import hashlib
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from nokker.core.ports import JudgeAnswer
from nokker.qa.questions import norm_token, parse_choice

logger = logging.getLogger("nokker.qa.judge")


class JudgeTransport(Protocol):
    """Un POST OpenAI-compatible qui renvoie le JSON déjà décodé. Permet de tester `ask()` sans
    réseau (transport scripté) et de changer d'amont (Router, llama.cpp auto-hébergé, Magnitude) sans
    toucher `OpenAICompatJudge`."""

    def post(self, body: dict[str, Any]) -> dict[str, Any]: ...


@dataclass
class HttpJudgeTransport:
    """Transport réel (httpx), un par palier/amont. `extra_body` est fusionné dans CHAQUE requête
    (ex. `{"reasoning_effort": "none"}` sur le Router, ou les échantillonneurs neutralisés de
    llama.cpp — cf PLAN §2.5, note `nina-qa`/`juges-conformite-banc-2026-09-21`)."""

    base_url: str
    model: str
    api_key: str = ""
    extra_body: dict[str, Any] = field(default_factory=dict)
    top_logprobs: int = 1
    timeout_s: float = 30.0

    def __post_init__(self) -> None:
        import httpx

        self._client = httpx.Client(timeout=self.timeout_s)

    def post(self, body: dict[str, Any]) -> dict[str, Any]:
        full = {"model": self.model, **body, **self.extra_body}
        if self.top_logprobs:
            full["top_logprobs"] = self.top_logprobs
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        resp = self._client.post(f"{self.base_url.rstrip('/')}/chat/completions", json=full, headers=headers)
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code} juge ({self.model}): {resp.text[:300]}")
        return resp.json()

    def close(self) -> None:
        self._client.close()


def _cache_key(question_id: str, prompt: str, options: list[str], model: str) -> str:
    h = hashlib.sha256()
    h.update(question_id.encode())
    h.update(b"\0")
    h.update(prompt.encode())
    h.update(b"\0")
    h.update(",".join(options).encode())
    h.update(b"\0")
    h.update(model.encode())
    return h.hexdigest()


def probability_from_top_logprobs(top: list[dict[str, Any]], options: list[str]) -> dict[str, float] | None:
    """Masse des tokens de chaque option. Avec PLUSIEURS options observées dans `top` (amont qui
    rend `top_logprobs > 1` et couvre plusieurs options, ex. llama.cpp auto-hébergé jusqu'à 2) : masse
    normalisée sur les options présentes (`judge_eval.py::ask`, même méthode) — une vraie
    distribution partielle, pas une approximation.

    Avec UNE SEULE option observée (cas du Router SOKKAN, plafonné à `top_logprobs=1` sur Ship ET
    Deep, B-2/F5) : piège évité ICI — normaliser une masse unique par elle-même donnerait
    TOUJOURS 1.0 et effacerait la vraie confiance du modèle. `exp(logprob)` de ce seul token EST
    déjà sa probabilité réelle (softmax du modèle, avant toute troncature de `top_logprobs`) : elle
    est renvoyée telle quelle (plafonnée à 1.0 par sécurité numérique), SANS renormalisation — c'est
    ce `p` que `_apply_single_logprob_approximation` complète ensuite pour une question Noul
    (approximation documentée en tête de module)."""
    if not top:
        return None
    mass: dict[str, float] = {}
    for t in top:
        opt = parse_choice(t.get("token", ""), tuple(options))
        if opt:
            mass[opt] = mass.get(opt, 0.0) + math.exp(float(t.get("logprob", -99)))
    if not mass:
        return None
    if len(mass) == 1:
        (opt, p), = mass.items()
        return {opt: min(p, 1.0)}
    tot = sum(mass.values())
    if tot <= 0:
        return None
    return {o: mass.get(o, 0.0) / tot for o in options}


def _apply_single_logprob_approximation(raw_probs: dict[str, float] | None, chosen: str | None,
                                        options: list[str]) -> dict[str, float] | None:
    """Si une seule option a reçu de la masse (cas `top_logprobs=1`) ET que la question est Noul
    (2 options), déduit l'autre par complément à 1 — approximation documentée en tête de module.
    Sinon renvoie `raw_probs` tel quel (Choice/Score : seule la confiance du choix est connue)."""
    if raw_probs is None or chosen is None:
        return raw_probs
    if len(raw_probs) >= 2 or len(options) != 2:
        return raw_probs
    p = raw_probs.get(chosen)
    if p is None:
        return raw_probs
    other = next(o for o in options if o != chosen)
    return {chosen: p, other: 1.0 - p}


@dataclass
class OpenAICompatJudge:
    """`core.ports.Judge` — un backend OpenAI-compatible quelconque derrière `JudgeTransport`."""

    transport: JudgeTransport
    model_name: str = "judge"
    cache: bool = True
    _cache_store: dict[str, JudgeAnswer] = field(default_factory=dict, repr=False)

    def ask(self, question_id: str, prompt: str, options: list[str]) -> JudgeAnswer:
        key = _cache_key(question_id, prompt, options, self.model_name)
        if self.cache and key in self._cache_store:
            cached = self._cache_store[key]
            logger.debug("juge: cache hit %s (%s)", question_id, key[:12])
            return JudgeAnswer(choice=cached.choice, probabilities=cached.probabilities,
                               model=cached.model, latency_s=0.0)

        body = {"messages": [{"role": "user", "content": prompt}], "max_tokens": 1, "temperature": 0,
                "logprobs": True}
        t0 = time.monotonic()
        try:
            data = self.transport.post(body)
        except Exception as e:  # noqa: BLE001 — un juge qui tombe ne doit jamais planter le tour
            logger.warning("juge: échec transport %s: %s", question_id, e)
            answer = JudgeAnswer(choice=options[0] if options else "", probabilities=None,
                                 model=self.model_name, latency_s=time.monotonic() - t0)
            if self.cache:
                self._cache_store[key] = answer
            return answer
        latency_s = time.monotonic() - t0

        choice_block = (data.get("choices") or [{}])[0]
        content = (choice_block.get("message") or {}).get("content") or ""
        lp = (choice_block.get("logprobs") or {}).get("content") or []
        top = (lp[0] or {}).get("top_logprobs") or [] if lp else []

        probs = probability_from_top_logprobs(top, options)
        chosen = (max(probs, key=probs.get) if probs else None) or parse_choice(content, tuple(options))
        probs = _apply_single_logprob_approximation(probs, chosen, options)
        if chosen is None:
            chosen = options[0] if options else norm_token(content)
            logger.warning("juge: réponse non reconnue pour %s (%r) — repli sur %r", question_id, content, chosen)

        answer = JudgeAnswer(choice=chosen, probabilities=probs, model=data.get("model", self.model_name),
                             latency_s=latency_s)
        if self.cache:
            self._cache_store[key] = answer
        return answer

    def advise(self, prompt: str, max_tokens: int = 150) -> str:
        """2e appel, PAS du port `Judge` (qui n'admet qu'un verdict à 1 token) : un texte court
        (risque, sauvegarde, retour arrière). Consigne Nick 17.10 : déclenché SEULEMENT quand la
        porte a déjà décidé HITL (`gates.py::GatesV1._with_advice`), jamais pour peupler un champ
        `Verdict.advice` qui n'existe pas (cf. docstring de `gates.py`) — l'appelant met ce texte
        dans `Verdict.reason`. Pas de cache : chaque avis est sur mesure (contexte + action)."""
        try:
            data = self.transport.post({"messages": [{"role": "user", "content": prompt}],
                                        "max_tokens": max_tokens, "temperature": 0.2, "logprobs": False})
        except Exception as e:  # noqa: BLE001 — un avis qui échoue ne doit jamais faire planter le tour
            logger.warning("juge: avis indisponible: %s", e)
            return ""
        choice_block = (data.get("choices") or [{}])[0]
        return ((choice_block.get("message") or {}).get("content") or "").strip()


__all__ = [
    "HttpJudgeTransport", "JudgeTransport", "OpenAICompatJudge", "probability_from_top_logprobs",
]
