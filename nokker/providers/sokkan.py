"""Provider OpenAI-compatible pour le Router SOKKAN (`providers.Provider`, PLAN-P1 §2.6/§2.8).

Un palier = une instance de `SokkanProvider` (base_url + modèle + clé propres à ce palier). La clé de
session (`session_key`) part en en-tête `X-Session-Id` pour l'affinité Router (F5, cache + slot sticky).

Raisonnement : désactivé par défaut via `reasoning_effort: "none"` — **vérifié en réel le 17.10.2026 sur
le Router** (`qwen/qwen3.8-27b` et `qwen/qwen3.5-397b-a17b`, amont OVH) : c'est la SEULE variante testée qui
coupe vraiment le raisonnement (`reasoning_tokens=0`) sans faire échouer l'appel — `chat_template_kwargs:
{"enable_thinking": false}` (gabarit vLLM/llama.cpp, ce qu'attendait le PLAN initial) renvoie `HTTP 400
upstream` sur ces deux modèles OVH, idem pour `reasoning: {"enabled": false}` ; `reasoning_effort: "low"`
réduit sans éliminer. Cf. le banc interne `nokker_bench.py::REASONING_VARIANTS`/D10 (même mesure,
même conclusion). Le point d'injection reste configurable (`reasoning_off_body`) : le llama.cpp partagé de
Un llama.cpp auto-hébergé (Qwen3.6-35B-A3B) accepte lui `chat_template_kwargs.enable_thinking`, donc un déploiement qui route
vers lui directement doit surcharger ce champ à la construction du `SokkanProvider`.

Réessai : 2 tentatives supplémentaires (3 appels au total) sur erreur réseau (timeout, connexion refusée,
DNS, 5xx) — jamais sur un refus applicatif (4xx hors 429) qui ne se résoudra pas en rejouant.

Budget total (P1-S1, diag latence, 17.10.2026) : un **504** de `router.sokkan.ch` ne veut PAS dire « amont
en panne », mais « le broker a déjà attendu tout son budget interne (`first_token_timeout_s`, 60 s par
défaut côté broker, confirmé en réel : 3 occurrences mesurées à 60,04-60,05 s) sans qu'aucun amont n'ait
répondu ». Pour le palier Ship (`qwen/qwen3.8-27b`), le catalogue n'a qu'UN SEUL amont effectivement
servable (`sokkan`, crédits OVHcloud, 4 requêtes simultanées max — le second, `ovh`, est écarté par le
garde-fou de marge) : un réessai après un 504 retape donc EXACTEMENT la même file d'attente, pas un amont
différent. Mesuré en réel (banc P1-S1, staging `nokker-canary-staging`) : sur 83 appels, 80 réussissent du
premier coup avec une latence très variable (0,8-109 s, propriété de l'amont partagé, pas un bug) ; les 3
qui subissent ce 504 réussissent au 2e essai en 8,5-20,9 s — mais SANS garde-fou, un 2e 504 d'affilée
enchaînerait un 3e essai après un nouveau budget de 60 s, poussant un seul tour au-delà de 3 minutes (le
mécanisme exact derrière les 64 s/135 s observés en F7 : un ou deux paliers de ~60 s). `total_budget_s`
plafonne le temps CUMULÉ de tous les essais : passé ce budget, on abandonne plutôt que de lancer un essai
supplémentaire — `core/loop.py` ne laisse alors JAMAIS un tour sans réponse (voir `Kernel.run_turn`,
dégradation propre + escalade + trace au ledger quand un palier échoue ou a dû réessayer).

Streaming (P1, 19.10.2026) : avec `on_delta`, l'appel part en `stream: true` (+ `stream_options:
{include_usage: true}`) et chaque morceau de `content` est remis à `on_delta` dès réception. Le flux est
reconstitué en la MÊME réponse que le chemin non-streamé (content, tool_calls recollés par `index`, usage
du dernier chunk, modèle, finish_reason) puis passe par le même analyseur → même `Completion`. Réessais :
mêmes règles et même budget, mais SEULEMENT tant qu'aucun morceau n'a été remis à `on_delta` — après le
premier, une coupure lève (`StreamInterrupted`) au lieu de rejouer un texte que le client a déjà vu.

Budget de l'appel (revue du train du 2.11, défaut 8) : `call_budget_s` (défaut = `timeout_s`, 180 s) borne le temps
d'attente TOTAL de l'appel, réessais compris. Un essai de plus (réseau, 5xx, 429, délai d'attente) ne part que s'il
reste au moins `MIN_RETRY_S` de ce budget, et son délai d'attente est réduit à ce qui reste. Avant, un délai d'attente
de lecture (180 s) suivi de son essai de plus portait un appel au-delà de 6 min, avec une double facturation possible
si l'amont finissait la génération abandonnée. Avec les valeurs par défaut, un premier essai qui a attendu tout son
délai de lecture n'est donc plus rejoué (le tour se dégrade proprement) ; un délai de connexion, court, l'est encore.

Écriture en direct (X22, 25.10.2026) : avec `on_tool_delta(index, call_id, name, morceau)`, chaque morceau BRUT des
`arguments` d'un appel d'outil est aussi remis dès réception (le noyau en lit le JSON partiel, `core/live_input.py`).
Un morceau d'arguments remis compte comme un morceau émis : même règle de réessai que pour `on_delta`.
"""
from __future__ import annotations

import copy
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from nokker.core.ports import Completion, StreamInterrupted
from nokker.core.turn import Message, ToolCall, Usage

logger = logging.getLogger("nokker.providers.sokkan")

_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
MIN_RETRY_S = 15.0  # un essai de plus ne part que s'il reste au moins ce temps dans le budget de l'appel


class _UpstreamStatus(Exception):
    def __init__(self, status: int, text: str) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status
        self.text = text


@dataclass
class SokkanProvider:
    """Client OpenAI-compatible d'UN palier (Router SOKKAN ou tout amont compatible)."""

    base_url: str
    api_key: str
    model: str
    reasoning_off_body: dict[str, Any] = field(default_factory=lambda: {"reasoning_effort": "none"})
    reasoning_on_body: dict[str, Any] = field(default_factory=dict)  # défaut amont = raisonnement actif
    timeout_s: float = 180.0
    max_retries: int = 2  # tentatives SUPPLÉMENTAIRES après l'échec initial (3 appels au total)
    retry_backoff_s: float = 1.0
    # Budget total (tous essais confondus) avant d'abandonner — voir docstring du module. 80 s laisse
    # passer EXACTEMENT le cas mesuré en réel (un 504 à ~60 s + un 2e essai réussi en 8,5-20,9 s) sans
    # laisser un 2e 504 d'affilée lancer un 3e essai (qui pousserait un seul tour à ~180 s).
    total_budget_s: float = 80.0
    # Délai d'attente (connexion, lecture, écriture) AVANT tout morceau : un essai de plus, même budget total épuisé —
    # le délai de lecture (180 s) dépasse ce budget, donc le réessai normal ne partait jamais (banc X9 du 25.10, tour
    # 110 : « échec réseau après 1 tentative(s) : The read operation timed out », tour perdu). Jamais après un morceau.
    timeout_retries: int = 1
    # Temps total d'un appel, réessais compris (None = `timeout_s`) : voir la docstring du module.
    call_budget_s: float | None = None
    max_tokens_default: int = 2048
    extra_headers: dict[str, str] = field(default_factory=dict)
    # 429 « trop de requêtes simultanées pour cette clé » (Router, plafond par clé) : une place se libère quand un
    # autre appel finit — on ATTEND (1, 2, 4, 8, 8… s, dans `total_budget_s`) sans consommer les réessais réseau,
    # au lieu d'abandonner en 3 s et d'escalader (vu en réel le 20.10.2026 : « palier ship indisponible : 429 »).
    busy_backoff_cap_s: float = 8.0
    # Le modèle de ce palier lit-il les images (X7) ? Lu par le noyau (`core/loop.py::_wire_messages`) : avec vision,
    # les images jointes partent en parties `image_url` (data URL) ; sans, une note texte les remplace. Défaut : non.
    vision: bool = False
    # `vision: auto` (nokker/providers/vision_probe.py) : sonde unique au premier tour avec image ; None = pas de sonde.
    vision_probe: Any = None

    def __post_init__(self) -> None:
        self._client = httpx.Client(timeout=self.timeout_s)
        self._closed = False

    def detached(self) -> SokkanProvider:
        """Même palier, client HTTP propre : le fermer (`close()`) empêche tout réessai sans toucher aux autres
        appels (HedgedProvider ferme ainsi le primaire qu'il abandonne). La requête déjà en vol, elle, va à son terme
        côté amont : un `recv` bloqué ne se réveille pas d'un autre thread, et le Router ne libère la place qu'à la
        fin de la réponse."""
        twin = copy.copy(self)
        twin._client = httpx.Client(timeout=self.timeout_s)
        twin._closed = False
        return twin

    def _deadline(self) -> float:
        return time.monotonic() + (self.call_budget_s if self.call_budget_s is not None else self.timeout_s)

    def _room(self, deadline: float, wait: float = 0.0) -> bool:
        """Reste-t-il de quoi lancer un essai de plus (après `wait` s d'attente) dans le budget de l'appel ?"""
        return deadline - time.monotonic() - wait >= MIN_RETRY_S

    def _timeout(self, deadline: float) -> float:
        """Délai d'attente d'un essai : le sien, réduit à ce qui reste du budget de l'appel."""
        return max(0.001, min(self.timeout_s, deadline - time.monotonic()))

    def _busy_wait(self, status: int, busy: int, t_budget0: float, deadline: float | None = None) -> bool:
        """Vrai (après avoir attendu) si un 429 doit être rejoué sans compter comme un réessai."""
        if status != 429 or not self._budget_left(t_budget0):
            return False
        wait = min(self.busy_backoff_cap_s, self.retry_backoff_s * (2 ** busy))
        if deadline is not None and not self._room(deadline, wait):
            return False
        logger.warning("sokkan provider: 429 (clé saturée) %s — attente %.0fs, essai %d", self.model, wait, busy + 1)
        time.sleep(wait)
        return True

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], *, reasoning: bool,
                 session_key: str, max_tokens: int = 2048,
                 on_delta: Callable[[str], None] | None = None,
                 on_tool_delta: Callable[[int, str, str, str], None] | None = None) -> Completion:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens or self.max_tokens_default,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        body.update(self.reasoning_on_body if reasoning else self.reasoning_off_body)

        headers = {"Content-Type": "application/json", "X-Session-Id": session_key}
        if self.api_key:  # un amont sans auth (ex. llama.cpp local) refuse un Bearer vide
            headers["Authorization"] = f"Bearer {self.api_key}"
        headers.update(self.extra_headers)

        t0 = time.monotonic()
        if on_delta is not None:
            body["stream"] = True
            body["stream_options"] = {"include_usage": True}
            data, attempts = self._stream_with_retry(body, headers, on_delta, on_tool_delta)
        else:
            data, attempts = self._post_with_retry(body, headers)
        latency_s = time.monotonic() - t0

        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        usage_raw = data.get("usage") or {}
        usage = _parse_usage(usage_raw)
        tool_calls = [_parse_tool_call(tc) for tc in (msg.get("tool_calls") or [])]
        message = Message(role="assistant", content=msg.get("content") or "", tool_calls=tool_calls)
        return Completion(message=message, usage=usage, model=data.get("model", self.model),
                           latency_s=latency_s, finish_reason=choice.get("finish_reason"), raw=data,
                           attempts=attempts)

    def _post_with_retry(self, body: dict[str, Any], headers: dict[str, str]) -> tuple[dict[str, Any], int]:
        last_exc: Exception | None = None
        t_budget0 = time.monotonic()
        deadline = self._deadline()
        attempt, busy, timeouts = 0, 0, 0
        while attempt <= self.max_retries + timeouts:
            self._check_open()
            try:
                resp = self._client.post(f"{self.base_url.rstrip('/')}/chat/completions", json=body,
                                          headers=headers, timeout=self._timeout(deadline))
            except httpx.TransportError as e:
                last_exc = e
                logger.warning("sokkan provider: erreur réseau (tentative %d/%d) %s: %s",
                                attempt + 1, self.max_retries + 1, self.model, e)
                wait = self.retry_backoff_s * (attempt + 1)
                if attempt < self.max_retries and self._budget_left(t_budget0) and self._room(deadline, wait):
                    time.sleep(wait)
                    attempt += 1
                    continue
                if isinstance(e, httpx.TimeoutException) and timeouts < self.timeout_retries and self._room(deadline):
                    timeouts += 1
                    logger.warning("sokkan provider: délai d'attente dépassé %s — un essai de plus, %.0f s restantes "
                                   "dans le budget de l'appel", self.model, deadline - time.monotonic())
                    attempt += 1
                    continue
                raise RuntimeError(f"échec réseau après {attempt + 1} tentative(s) : {e}") from e

            if self._busy_wait(resp.status_code, busy, t_budget0, deadline):
                busy += 1
                continue
            if resp.status_code in _RETRYABLE_STATUS and attempt < self.max_retries:
                # Un 504 précisément = le broker a déjà attendu tout SON budget (first_token_timeout_s,
                # 60 s par défaut) sans qu'aucun amont ne réponde (voir docstring du module) : ne réessaie
                # que s'il reste du budget CÔTÉ NOUS pour un essai de plus, sinon un 2e 504 d'affilée
                # pousserait ce seul appel à ~180 s (mesure F7 d'origine).
                if not self._budget_left(t_budget0) or not self._room(deadline, self.retry_backoff_s * (attempt + 1)):
                    logger.warning("sokkan provider: HTTP %d (tentative %d/%d) %s — budget total épuisé "
                                    "(%.1fs), abandon sans réessayer", resp.status_code, attempt + 1,
                                    self.max_retries + 1, self.model, time.monotonic() - t_budget0)
                    raise RuntimeError(f"HTTP {resp.status_code} de {self.base_url} après "
                                       f"{attempt + 1} tentative(s) (budget total {self.total_budget_s}s "
                                       "épuisé, abandon avant un nouvel essai)")
                logger.warning("sokkan provider: HTTP %d (tentative %d/%d) %s",
                                resp.status_code, attempt + 1, self.max_retries + 1, self.model)
                time.sleep(self.retry_backoff_s * (attempt + 1))
                attempt += 1
                continue
            if resp.status_code >= 400:
                raise RuntimeError(f"HTTP {resp.status_code} de {self.base_url}: {resp.text[:500]}")
            return resp.json(), attempt + busy + 1  # les rejeux 429 sont des requêtes HTTP : visibles au ledger
        if last_exc:
            raise RuntimeError(f"échec réseau après {self.max_retries + 1} tentatives") from last_exc
        raise RuntimeError("échec sans réponse (ne devrait pas arriver)")

    def _stream_with_retry(self, body: dict[str, Any], headers: dict[str, str], on_delta: Callable[[str], None],
                           on_tool_delta: Callable[[int, str, str, str], None] | None = None
                           ) -> tuple[dict[str, Any], int]:
        """Comme `_post_with_retry`, en SSE. `emitted[0]` passe à True au premier morceau remis : dès lors,
        toute erreur lève `StreamInterrupted` (jamais de réessai, le client a déjà vu du texte)."""
        emitted = [False]

        def _relay(piece: str) -> None:
            emitted[0] = True
            on_delta(piece)

        def _relay_tool(index: int, call_id: str, name: str, piece: str) -> None:
            emitted[0] = True
            assert on_tool_delta is not None
            on_tool_delta(index, call_id, name, piece)

        t_budget0 = time.monotonic()
        deadline = self._deadline()
        attempt, busy, timeouts = 0, 0, 0
        while attempt <= self.max_retries + timeouts:
            self._check_open()
            try:
                return (self._stream_once(body, headers, _relay, _relay_tool if on_tool_delta is not None else None,
                                          timeout=self._timeout(deadline)),
                        attempt + busy + 1)
            except _UpstreamStatus as e:
                # Statut connu avant le moindre octet de corps : rien n'a pu être émis.
                if self._busy_wait(e.status, busy, t_budget0, deadline):
                    busy += 1
                    continue
                if e.status in _RETRYABLE_STATUS and attempt < self.max_retries:
                    if not self._budget_left(t_budget0) or not self._room(deadline,
                                                                          self.retry_backoff_s * (attempt + 1)):
                        raise RuntimeError(f"HTTP {e.status} de {self.base_url} après {attempt + 1} tentative(s) "
                                           f"(budget total {self.total_budget_s}s épuisé, abandon avant un "
                                           "nouvel essai)") from None
                    logger.warning("sokkan provider: HTTP %d en stream (tentative %d/%d) %s",
                                    e.status, attempt + 1, self.max_retries + 1, self.model)
                    time.sleep(self.retry_backoff_s * (attempt + 1))
                    attempt += 1
                    continue
                raise RuntimeError(f"HTTP {e.status} de {self.base_url}: {e.text[:500]}") from None
            except (httpx.TransportError, _StreamError) as e:
                if emitted[0]:
                    raise StreamInterrupted(f"flux amont interrompu après le premier morceau : {e}") from e
                logger.warning("sokkan provider: flux en échec avant tout morceau (tentative %d/%d) %s: %s",
                                attempt + 1, self.max_retries + 1, self.model, e)
                wait = self.retry_backoff_s * (attempt + 1)
                if attempt < self.max_retries and self._budget_left(t_budget0) and self._room(deadline, wait):
                    time.sleep(wait)
                    attempt += 1
                    continue
                if isinstance(e, httpx.TimeoutException) and timeouts < self.timeout_retries and self._room(deadline):
                    timeouts += 1  # rien n'a été émis (vérifié ci-dessus) : un essai de plus, dans le budget de l'appel
                    logger.warning("sokkan provider: délai d'attente avant tout morceau %s — un essai de plus, %.0f s "
                                   "restantes", self.model, deadline - time.monotonic())
                    attempt += 1
                    continue
                raise RuntimeError(f"échec du flux après {attempt + 1} tentative(s) : {e}") from e
        raise RuntimeError("échec sans réponse (ne devrait pas arriver)")

    def _stream_once(self, body: dict[str, Any], headers: dict[str, str], on_delta: Callable[[str], None],
                     on_tool_delta: Callable[[int, str, str, str], None] | None = None,
                     timeout: float | None = None) -> dict[str, Any]:
        """Un appel SSE, reconstitué en corps non-streamé (`{"model", "choices": [{"message",
        "finish_reason"}], "usage"}`) pour passer par le même analyseur que `_post_with_retry`."""
        acc = _StreamAccumulator(model=self.model)
        with self._client.stream("POST", f"{self.base_url.rstrip('/')}/chat/completions", json=body,
                                 headers=headers, timeout=timeout if timeout is not None else self.timeout_s) as resp:
            if resp.status_code >= 400:
                resp.read()
                raise _UpstreamStatus(resp.status_code, resp.text)
            for line in resp.iter_lines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue  # commentaires (`: ping`), `event:`, lignes vides
                payload = line[5:].strip()
                if payload == "[DONE]":
                    acc.done = True
                    break
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if not isinstance(chunk, dict):
                    continue
                if chunk.get("error"):
                    raise _StreamError(f"erreur amont dans le flux : {str(chunk['error'])[:300]}")
                piece = acc.add(chunk, on_tool_delta)
                if piece:
                    on_delta(piece)
        if not acc.done and acc.finish_reason is None:
            raise _StreamError("flux amont terminé sans [DONE] ni finish_reason")
        return acc.as_response()

    def _budget_left(self, t_budget0: float) -> bool:
        return time.monotonic() - t_budget0 < self.total_budget_s

    def close(self) -> None:
        self._closed = True  # lu par les boucles de réessai : un appel coupé exprès ne se relance pas
        self._client.close()

    def _check_open(self) -> None:
        if self._closed:
            raise RuntimeError(f"appel à {self.model} coupé (provider fermé)")


class _StreamError(Exception):
    """Flux amont invalide ou coupé (avant ou après le premier morceau — l'appelant tranche)."""


@dataclass
class _StreamAccumulator:
    """Recolle les chunks `chat.completion.chunk` en un message complet (content, tool_calls par index, usage)."""

    model: str
    content: str = ""
    finish_reason: str | None = None
    usage: dict[str, Any] | None = None
    done: bool = False
    tool_calls: dict[int, dict[str, Any]] = field(default_factory=dict)

    def add(self, chunk: dict[str, Any], on_tool: Callable[[int, str, str, str], None] | None = None) -> str:
        if chunk.get("model"):
            self.model = chunk["model"]
        if chunk.get("usage"):
            self.usage = chunk["usage"]
        piece = ""
        for choice in chunk.get("choices") or []:
            if not isinstance(choice, dict) or choice.get("index", 0) != 0:
                continue
            if choice.get("finish_reason"):
                self.finish_reason = choice["finish_reason"]
            delta = choice.get("delta") or {}
            if not isinstance(delta, dict):
                continue
            text = delta.get("content")
            if isinstance(text, str) and text:
                self.content += text
                piece += text
            for i, tc in enumerate(delta.get("tool_calls") or []):
                if not isinstance(tc, dict):
                    continue
                idx = tc.get("index", i)
                slot = self.tool_calls.setdefault(idx, {"id": "", "type": "function",
                                                        "function": {"name": "", "arguments": ""}})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                if tc.get("type"):
                    slot["type"] = tc["type"]
                fn = tc.get("function") or {}
                name = fn.get("name")
                if name and name != slot["function"]["name"]:
                    # Le nom arrive en général d'un bloc au premier chunk ; certains amonts le répètent tel quel.
                    slot["function"]["name"] += name
                if fn.get("arguments"):
                    args = fn["arguments"]
                    args = args if isinstance(args, str) else json.dumps(args)
                    slot["function"]["arguments"] += args
                    if on_tool is not None and isinstance(idx, int):
                        on_tool(idx, slot["id"], slot["function"]["name"], args)
        return piece

    def as_response(self) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            message["tool_calls"] = [self.tool_calls[i] for i in sorted(self.tool_calls)]
        data: dict[str, Any] = {"model": self.model,
                                "choices": [{"index": 0, "message": message, "finish_reason": self.finish_reason}]}
        if self.usage is not None:
            data["usage"] = self.usage
        return data


def _parse_usage(raw: dict[str, Any]) -> Usage:
    prompt = raw.get("prompt_tokens", 0) or 0
    completion = raw.get("completion_tokens", 0) or 0
    details = raw.get("prompt_tokens_details")
    cached = 0
    cached_reported = False
    if isinstance(details, dict) and "cached_tokens" in details:
        cached = details.get("cached_tokens") or 0
        cached_reported = True
    completion_details = raw.get("completion_tokens_details") or {}
    if not isinstance(completion_details, dict):
        completion_details = {}
    reasoning_tokens = completion_details.get("reasoning_tokens") or 0
    return Usage(prompt=prompt, completion=completion, cached=cached, reasoning=reasoning_tokens,
                 cached_reported=cached_reported, reasoning_reported="reasoning_tokens" in completion_details)


def _parse_tool_call(raw: dict[str, Any]) -> ToolCall:
    fn = raw.get("function") or {}
    raw_args = fn.get("arguments") or "{}"
    try:
        args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
    except (json.JSONDecodeError, TypeError):
        args = {}
    return ToolCall(id=raw.get("id", ""), name=fn.get("name", ""), arguments=args, raw_arguments=str(raw_args))


__all__ = ["SokkanProvider", "StreamInterrupted"]
