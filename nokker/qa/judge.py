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
    llama.cpp, mesurés sur un banc interne)."""

    base_url: str
    model: str
    api_key: str = ""
    extra_body: dict[str, Any] = field(default_factory=dict)
    top_logprobs: int = 1
    timeout_s: float = 30.0
    # X8 : appelé après chaque réponse avec (modèle, `usage` de l'amont, secondes) — jetons du juge comptés à part.
    on_usage: Any = None
    retry_wait_s: float = 2.0  # une reprise sur 429/502/503/504 ; 0 = aucune
    # Coffre de valeurs connues (`DeterministicInvariants.known`) pour le filet du fil ; None : motifs seuls.
    known: Any = field(default=None, repr=False)
    _plain: bool = field(default=False, init=False, repr=False)  # amont qui a refusé `extra_body`/`top_logprobs`

    def __post_init__(self) -> None:
        import httpx

        self._client = httpx.Client(timeout=self.timeout_s)

    def post(self, body: dict[str, Any]) -> dict[str, Any]:
        import time as _time

        if isinstance(body.get("messages"), list):
            # Filet du fil (lot fix-batch-1) : la relecture voit le diff reconstruit depuis les arguments du modèle, l'avis
            # les arguments d'une carte, la porte les arguments de l'action — rien de tout ça n'est passé par une porte de
            # caviardage, et tout part en rôle `user`, que le Router refuse s'il y voit un secret (le juge se taisait).
            from nokker.qa.secrets import redact_messages

            body = {**body, "messages": redact_messages(body["messages"], self.known)}
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        t0 = _time.monotonic()
        resp = self._client.post(f"{self.base_url.rstrip('/')}/chat/completions", json=self._body(body, self._plain),
                                 headers=headers)
        if resp.status_code == 400 and not self._plain and (self.extra_body or body.get("logprobs")):
            # X8 : une clé de l'utilisateur ou un modèle local refuse souvent `reasoning_effort: none` (gpt-oss :
            # « Harmony does not support reasoning_effort='none' », mesuré le 25.10) ou `top_logprobs` : une seule
            # reprise sans eux, gardée pour la suite (verdict sans probabilité, D13), au lieu d'un juge muet.
            self._plain = True
            resp = self._client.post(f"{self.base_url.rstrip('/')}/chat/completions", json=self._body(body, True),
                                     headers=headers)
        if resp.status_code in (429, 502, 503, 504) and self.retry_wait_s > 0:
            # X8 : clé saturée (le Router plafonne les requêtes par clé) ou amont qui hoquette : une reprise courte,
            # sinon une relecture ou un avis se perdent en silence.
            _time.sleep(self.retry_wait_s)
            resp = self._client.post(f"{self.base_url.rstrip('/')}/chat/completions",
                                     json=self._body(body, self._plain), headers=headers)
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code} juge ({self.model}): {resp.text[:300]}")
        data = resp.json()
        if self.on_usage is not None:
            try:
                self.on_usage(self.model, data.get("usage") or {}, _time.monotonic() - t0)
            except Exception:  # noqa: BLE001 — un compteur ne fait jamais tomber le juge
                logger.debug("juge : compteur d'usage en échec", exc_info=True)
        return data

    def _body(self, body: dict[str, Any], plain: bool) -> dict[str, Any]:
        if plain:
            full = {"model": self.model, **{k: v for k, v in body.items() if k not in ("logprobs", "top_logprobs")}}
            return full
        full = {"model": self.model, **body, **self.extra_body}
        if self.top_logprobs and body.get("logprobs"):
            full["top_logprobs"] = self.top_logprobs
        return full

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
    # 1 (défaut) : verdict à un jeton avec logprobs. Plus pour un modèle qui raisonne d'abord (gpt-oss : son premier
    # jeton est le canal de raisonnement, mesuré le 25.10) : l'option est lue dans le texte final, sans probabilité.
    verdict_max_tokens: int = 1
    # Préfixe STABLE du juge (`questions.JUDGE_SYSTEM`, les règles qu'il fait respecter) : envoyé en message système à
    # chaque question. Trouvé par X8 (25.10) : il n'était envoyé nulle part, le juge des portes jugeait sans les règles
    # que le banc B-1 lui donnait (`judge_eval.py`). None : pas de message système.
    system: str | None = None
    _cache_store: dict[str, JudgeAnswer] = field(default_factory=dict, repr=False)

    def ask(self, question_id: str, prompt: str, options: list[str]) -> JudgeAnswer:
        key = _cache_key(question_id, prompt, options, self.model_name)
        if self.cache and key in self._cache_store:
            cached = self._cache_store[key]
            logger.debug("juge: cache hit %s (%s)", question_id, key[:12])
            return JudgeAnswer(choice=cached.choice, probabilities=cached.probabilities,
                               model=cached.model, latency_s=0.0)

        one = self.verdict_max_tokens <= 1
        messages = ([{"role": "system", "content": self.system}] if self.system else []) + \
            [{"role": "user", "content": prompt}]
        body = {"messages": messages, "max_tokens": max(1, self.verdict_max_tokens),
                "temperature": 0, "logprobs": one}
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

        probs = probability_from_top_logprobs(top, options) if one else None
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
        (risque, sauvegarde, retour arrière). Consigne produit du 17.10 : déclenché SEULEMENT quand la
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
