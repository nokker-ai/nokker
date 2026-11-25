"""Provider au format Anthropic Messages (`POST /v1/messages`) — même port que `SokkanProvider` (`core/ports.Provider`).

Choisi par la config du palier (`tiers.<nom>.format: anthropic`, `nokker/providers/select.py`) ; sans ce champ, rien de
ce module n'est importé. Le noyau reste en forme OpenAI : ce module traduit dans les deux sens, et rien d'autre ne
connaît le format de l'amont. Référence du contrat : documentation de l'API Messages (docs/NOKKAN-ANTHROPIC.md §Contrat).

Aller (forme OpenAI du noyau → corps `/v1/messages`) :
- messages `system` → champ `system` de tête (blocs texte), dans l'ordre ; le noyau n'en produit qu'en tête ;
- `user` (texte, parties `text` / `image_url`) → blocs `text` / `image` (data URL → source `base64`, URL → source `url`) ;
- `assistant` + `tool_calls` → blocs `text` + `tool_use` (`input` = arguments décodés) ;
- `tool` → bloc `tool_result` dans un message `user` ; les résultats qui se suivent partent dans UN message `user`,
  avant le texte de l'utilisateur s'il suit ; deux messages de même rôle qui se suivent sont fusionnés ;
- outils `{"type": "function", "function": {name, description, parameters}}` → `{name, description, input_schema}` ;
- réflexion : `reasoning=True` → `reasoning_on_body` (défaut `{"thinking": {"type": "adaptive"}}`) ; `False` →
  `reasoning_off_body` (défaut vide : aucune valeur « éteint » n'est acceptée par tous les modèles, voir la doc) ;
- cache (`cache: prefix` seulement) : `cache_control` sur le dernier outil et sur le préfixe stable du noyau (bloc de
  tête de `system`, quand le noyau le désigne par `stable_prefix`). Nulle part ailleurs.

Retour (`message` → `Completion`) : blocs `text` recollés, `tool_use` → `ToolCall`, `stop_reason` → `finish_reason`
(`end_turn`/`stop_sequence` → `stop`, `max_tokens` → `length`, `tool_use` → `tool_calls`, `refusal` →
`content_filter`), usage → `Usage` (`prompt` = non caché + écrit en cache + lu en cache, `cached` = lu, `cache_write` =
écrit).

Réflexion rejouée : avec la réflexion active (`reasoning=True` et `reasoning_on_body` qui porte `thinking`), l'API exige
que le dernier message assistant d'un tour d'outils rende ses blocs `thinking` (avec `signature`) ou `redacted_thinking`
(avec `data`) TELS QUELS devant les `tool_use`, sinon 400. `from_message` les garde dans `Message.thinking_blocks`
(texte + signature, ou `data`), étiquetés du modèle producteur (`_by`, jamais envoyé) ; `to_messages_body` les rejoue en
tête du message, dans l'ordre reçu, pour les messages assistant du tour d'outils en cours (après le dernier message
`user`, plus le dernier assistant à appels d'outils). Jamais affichés, jamais rejoués vers un autre modèle ou
fournisseur : si l'un de ces messages porte une réflexion d'un autre modèle (changement de palier en cours de fil) ou
omise (trop grosse : `MAX_THINKING_CHARS`), l'appel part sans réflexion plutôt qu'en 400.

Flux SSE (`on_delta`) : événements `message_start`, `content_block_start|delta|stop`, `message_delta`, `message_stop`,
`ping`, `error`, recollés en le MÊME message que le chemin non streamé. Réessais : structure de `SokkanProvider`
(budgets, connexion, délai d'attente, jamais après un morceau remis) ; statuts rejoués 408, 409, 429 et ≥ 500 (529
compris), `retry-after` respecté sur un 429.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from nokker.core.ports import Completion, StreamInterrupted
from nokker.core.model_served import clean_served
from nokker.core.turn import Message, ToolCall, Usage, new_call_id
from nokker.providers.gate import headers_for as gate_headers
from nokker.providers.sokkan import CALL_ID_HEADER, SokkanProvider, _StreamError

logger = logging.getLogger("nokker.providers.anthropic")

DEFAULT_VERSION = "2026-10-21"
MAX_THINKING_CHARS = 1_000_000  # borne de la réflexion gardée par message (texte + signatures + data) ; au-delà : omise
_THINKING_TYPES = ("thinking", "redacted_thinking")
# En-tête bêta qui ouvre `thinking.block_binding.prefix_mismatch_behavior` et le tableau `input_transformations` de la
# réponse (docs/NOKKAN-ANTHROPIC.md §Liaison des blocs de réflexion).
BINDING_BETA = "thinking-binding-controls-2026-08-01"
BINDING_ERROR = "bound to a different conversation"  # seule partie stable du message du 400 (la doc : ne rien apparier d'autre)
# À partir de cette version de l'API, `temperature`, `top_p` et `top_k` sont refusés (400) sur tous les modèles.
NO_SAMPLING_SINCE = "2026-10-21"
SAMPLING_KEYS = ("temperature", "top_p", "top_k")
_FINISH = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length", "tool_use": "tool_calls",
           "refusal": "content_filter", "pause_turn": "stop"}
# Types d'erreur rejouables quand ils arrivent DANS le flux (événement `error`), où il n'y a plus de statut HTTP.
_RETRYABLE_TYPES = {"rate_limit_error": 429, "api_error": 500, "overloaded_error": 529}
_DATA_URL = re.compile(r"^data:([\w.+-]+/[\w.+-]+);base64,(.+)$", re.S)
_ID_UNSAFE = re.compile(r"[^a-zA-Z0-9_-]")


def _retryable_status(status: int) -> bool:
    return status in (408, 409, 429) or status >= 500


class _Upstream(Exception):
    """Refus de l'amont : statut HTTP, ou événement `error` du flux (statut déduit du type)."""

    def __init__(self, status: int, kind: str, message: str, retry_after: float | None = None,
                 request_id: str = "") -> None:
        super().__init__(f"HTTP {status}")
        self.status, self.kind, self.message = status, kind, message
        self.retry_after, self.request_id = retry_after, request_id

    def summary(self) -> str:
        return f"{self.kind}: {self.message}"[:500] if self.kind else self.message[:500]


@dataclass(repr=False)
class AnthropicProvider(SokkanProvider):
    """Client `/v1/messages` d'UN palier (API Anthropic par clé, ou tout amont compatible)."""

    # Défauts propres au format : pas de valeur « réflexion éteinte » universelle (vide = défaut du modèle).
    reasoning_off_body: dict[str, Any] = field(default_factory=dict)
    reasoning_on_body: dict[str, Any] = field(default_factory=lambda: {"thinking": {"type": "adaptive"}})
    # Un amont tiers n'a pas à voir nos identifiants de fil : jamais d'en-tête `X-Session-Id` ici.
    session_header: bool = False
    anthropic_version: str = DEFAULT_VERSION
    # `x-api-key` (API Anthropic) ou `bearer` (`Authorization: Bearer`, passerelles et jetons OAuth).
    auth: str = "x-api-key"
    # `tiers.<nom>.cache` : « prefix » pose les points `cache_control` (outils + préfixe stable) ; « none » : aucun.
    cache: str = "none"
    # `tiers.<nom>.cache_ttl` : vide = deux points sans durée (comportement d'avant) ; « 5m » / « 1h » = durée des points
    # ET un 3e point sur le dernier message. Sans effet hors `cache: prefix`.
    cache_ttl: str = ""
    # `tiers.<nom>.effort` : `output_config.effort` envoyé à chaque appel (vide = rien) ; un corps de réflexion qui pose
    # `output_config` prime.
    effort: str = ""
    # `tiers.<nom>.binding_controls` : envoie l'en-tête bêta de liaison des blocs de réflexion et journalise, SANS texte, les
    # entrées `input_transformations` de la réponse (détecteur de réécriture de fenêtre). Vrai : l'en-tête ne change pas
    # ce que le modèle reçoit ; faux (défaut) : aucun en-tête bêta n'est envoyé, sauf pour le filet `drop_block`.
    binding_controls: bool = False
    # Fils (clé de session) qui ont déjà reçu un 400 de liaison : `drop_block` part d'emblée sur leurs appels suivants
    # (l'API ne garde pas le réglage d'une requête à l'autre).
    _drop_sessions: set = field(default_factory=set, init=False, repr=False)
    _tool_names: dict = field(default_factory=dict, init=False, repr=False)  # session -> noms d'outils du dernier appel

    @property
    def replays_thinking(self) -> bool:
        """Lu par le noyau (`core/loop.py::_wire_messages`) : ce fournisseur reçoit les blocs de réflexion rejoués."""
        return True

    def __repr__(self) -> str:  # jamais la clé (le repr de la dataclass la montrerait)
        return f"AnthropicProvider(base_url={self.base_url!r}, model={self.model!r})"

    def _url(self) -> str:
        base = self.base_url.rstrip("/")
        return f"{base}/messages" if base.endswith("/v1") else f"{base}/v1/messages"

    def _headers(self, call_id: str = "") -> dict[str, str]:
        headers = {"Content-Type": "application/json", "anthropic-version": self.anthropic_version}
        if self.api_key:  # un amont local sans authentification refuse une clé vide
            if self.auth == "bearer":
                headers["Authorization"] = f"Bearer {self.api_key}"
            else:
                headers["x-api-key"] = self.api_key
        headers.update(self.extra_headers)
        if call_id and self.send_call_id:
            headers[CALL_ID_HEADER] = call_id
        return headers

    def _mark_base_tools(self, body: dict[str, Any], session_key: str) -> None:
        """Quand le jeu d'outils du fil a GRANDI (un outil chargé à la suite), un point de cache est aussi posé sur le dernier
        outil de l'appel précédent : le bloc d'outils d'avant reste lu au cache (sur l'API Anthropic `tools` précède
        `system` : sans lui, ajouter un schéma réécrit tout). 4 points au plus (outils ×2, préfixe, dernier message)."""
        specs = body.get("tools")
        if not specs or self.cache != "prefix":
            return
        names = [s.get("name") for s in specs]
        prev = self._tool_names.get(session_key)
        self._tool_names[session_key] = names
        while len(self._tool_names) > 2000:
            self._tool_names.pop(next(iter(self._tool_names)), None)
        if prev and len(prev) < len(names) and names[:len(prev)] == prev:
            specs[len(prev) - 1]["cache_control"] = _cache_control(self.cache_ttl)

    def _sampling(self) -> dict[str, Any]:
        if not self.sampling_body or self.anthropic_version >= NO_SAMPLING_SINCE:
            return {}
        return {k: v for k, v in self.sampling_body.items() if k in SAMPLING_KEYS}

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], *, reasoning: bool,
                 session_key: str, max_tokens: int | None = None,
                 on_delta: Callable[[str], None] | None = None,
                 on_tool_delta: Callable[[int, str, str, str], None] | None = None,
                 stable_prefix: str | None = None) -> Completion:
        on_body = self.reasoning_on_body if reasoning else self.reasoning_off_body
        replay = _thinking_on(on_body)
        if replay and thinking_unusable(messages, self.model):
            logger.warning("anthropic provider: réflexion d'un autre modèle (ou omise) dans le tour d'outils : appel "
                           "sans réflexion (%s)", self.model)
            replay, on_body = False, {k: v for k, v in on_body.items() if k != "thinking"}
        body = to_messages_body(messages, tools, model=self.model, max_tokens=max_tokens or self.max_tokens_default,
                                cache=self.cache == "prefix", stable_prefix=stable_prefix,
                                cache_ttl=self.cache_ttl, thinking_model=self.model if replay else None)
        self._mark_base_tools(body, session_key)
        body.update(self._sampling())
        if self.effort:
            body["output_config"] = {"effort": self.effort}
        body.update(on_body)
        if on_delta is not None:
            body["stream"] = True
        call_id = new_call_id()  # un par appel du noyau ; les réessais HTTP d'un même appel gardent le leur
        headers = {**self._headers(call_id), **gate_headers(self.gate, session_key)}
        if self.binding_controls:
            _add_beta(headers)
        if session_key in self._drop_sessions:
            _set_drop_block(body, headers)
        t0 = time.monotonic()
        try:
            data, attempts = self._call_with_retry(body, headers, on_delta, on_tool_delta)
        except RuntimeError as e:
            # Filet : un 400 de liaison (un message d'avant un bloc rejoué a changé) ne casse pas le tour. Le correctif est
            # d'être append-only ; ceci ne sert qu'à ne pas perdre le tour, et reste journalisé (sans texte).
            if BINDING_ERROR not in str(e) or not _thinking_adaptive(body) or "block_binding" in body["thinking"]:
                raise
            logger.warning("anthropic provider: 400 de liaison des blocs de réflexion (%s, session %s) : nouvel essai avec "
                           "drop_block, la fenêtre envoyée n'est pas append-only", self.model, session_key)
            self._drop_sessions.add(session_key)
            _set_drop_block(body, headers)
            data, attempts = self._call_with_retry(body, headers, on_delta, on_tool_delta)
            attempts += 1
        latency_s = time.monotonic() - t0
        message, finish = from_message(data, by=self.model)
        stamp_thinking(message, body)
        _log_transformations(data, self.model, session_key)
        usage = parse_usage(data.get("usage") or {})
        if self.cache == "prefix" and self.cache_ttl == "1h":  # durée des points de cache posés : choisit le prix 1 h
            usage.cache_ttl = "1h"
        return Completion(message=message, usage=usage,
                          model=data.get("model") or self.model, latency_s=latency_s, finish_reason=finish, raw=data,
                          attempts=attempts, call_id=call_id, model_served=clean_served(data.get("model")))

    # ── réessais : même plan que `SokkanProvider` (budgets hérités), un seul chemin pour JSON et flux ──

    def _call_with_retry(self, body: dict[str, Any], headers: dict[str, str],
                         on_delta: Callable[[str], None] | None,
                         on_tool_delta: Callable[[int, str, str, str], None] | None) -> tuple[dict[str, Any], int]:
        emitted = [False]

        def relay(piece: str) -> None:
            emitted[0] = True
            assert on_delta is not None
            on_delta(piece)

        def relay_tool(index: int, call_id: str, name: str, piece: str) -> None:
            emitted[0] = True
            assert on_tool_delta is not None
            on_tool_delta(index, call_id, name, piece)

        t_budget0 = time.monotonic()
        deadline = self._deadline()
        attempt, busy = 0, 0
        n = {"attempt": 0, "connect": 0, "timeout": 0}
        while attempt <= self.max_retries + n["timeout"] + n["connect"]:
            self._check_open()
            try:
                if on_delta is None:
                    return self._post_once(body, headers, self._timeout(deadline)), attempt + busy + 1
                return (self._stream_messages(body, headers, relay, relay_tool if on_tool_delta is not None else None,
                                              self._timeout(deadline)), attempt + busy + 1)
            except _Upstream as e:
                if emitted[0]:  # événement `error` après un morceau remis : jamais de rejeu d'un texte déjà vu
                    raise StreamInterrupted(f"flux amont interrompu après le premier morceau : {e.summary()}") from e
                if e.status == 429 and self._rate_wait(e, busy, t_budget0, deadline):
                    busy += 1
                    continue
                wait = self.retry_backoff_s * (attempt + 1)
                if _retryable_status(e.status) and attempt - n["connect"] - n["timeout"] < self.max_retries:
                    if not self._budget_left(t_budget0) or not self._room(deadline, wait):
                        raise RuntimeError(f"HTTP {e.status} de {self.base_url} après {attempt + 1} tentative(s) "
                                           f"(budget total {self.total_budget_s}s épuisé, abandon avant un nouvel "
                                           "essai)") from None
                    logger.warning("anthropic provider: HTTP %d %s (tentative %d/%d) %s %s", e.status, e.kind,
                                   attempt + 1, self.max_retries + 1, self.model, e.request_id)
                    time.sleep(wait)
                    attempt += 1
                    continue
                raise RuntimeError(f"HTTP {e.status} de {self.base_url}: {e.summary()}") from None
            except (httpx.TransportError, _StreamError) as e:
                if emitted[0]:
                    raise StreamInterrupted(f"flux amont interrompu après le premier morceau : {e}") from e
                n["attempt"] = attempt
                kind, wait, why = self._transport_plan(e, n, t_budget0, deadline)
                self._log_transport("appel" if on_delta is None else "flux en échec avant tout morceau", e, kind, wait,
                                    why, attempt, deadline)
                if kind is None:
                    raise RuntimeError(f"échec réseau après {attempt + 1} tentative(s) : {e}") from e
                if kind in ("connect", "timeout"):
                    n[kind] += 1
                if wait:
                    time.sleep(wait)
                attempt += 1
        raise RuntimeError("échec sans réponse (ne devrait pas arriver)")

    def _rate_wait(self, e: _Upstream, busy: int, t_budget0: float, deadline: float) -> bool:
        """429 : attend `retry-after` (sinon 1, 2, 4, 8… s) sans consommer un réessai, tant que les budgets tiennent."""
        if not self._budget_left(t_budget0):
            return False
        wait = e.retry_after if e.retry_after is not None else min(self.busy_backoff_cap_s,
                                                                   self.retry_backoff_s * (2 ** busy))
        if not self._room(deadline, wait) or time.monotonic() - t_budget0 + wait >= self.total_budget_s:
            return False
        logger.warning("anthropic provider: 429 %s — attente %.0fs, essai %d", self.model, wait, busy + 1)
        time.sleep(wait)
        return True

    def _post_once(self, body: dict[str, Any], headers: dict[str, str], timeout: httpx.Timeout) -> dict[str, Any]:
        resp = self._client.post(self._url(), json=body, headers=headers, timeout=timeout)
        if resp.status_code >= 400:
            raise _upstream_error(resp)
        data = resp.json()
        if not isinstance(data, dict) or data.get("type") == "error":
            raise _Upstream(502, "api_error", "réponse amont illisible")
        return data

    def _stream_messages(self, body: dict[str, Any], headers: dict[str, str], on_delta: Callable[[str], None],
                         on_tool_delta: Callable[[int, str, str, str], None] | None,
                         timeout: httpx.Timeout) -> dict[str, Any]:
        """Un appel SSE, reconstitué en `message` non streamé pour passer par le même analyseur."""
        acc = _MessageAccumulator(model=self.model)
        with self._client.stream("POST", self._url(), json=body, headers=headers, timeout=timeout) as resp:
            if resp.status_code >= 400:
                resp.read()
                raise _upstream_error(resp)
            for line in resp.iter_lines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue  # `event:` (le type est répété dans `data`), commentaires, lignes vides
                try:
                    event = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("type") == "error":
                    err = event.get("error") if isinstance(event.get("error"), dict) else {}
                    kind = str(err.get("type") or "api_error")
                    raise _Upstream(_RETRYABLE_TYPES.get(kind, 400), kind, str(err.get("message") or ""))
                acc.add(event, on_delta, on_tool_delta)
                if acc.done:
                    break
        if not acc.done:
            raise _StreamError("flux amont terminé sans message_stop")
        return acc.as_message()


def _upstream_error(resp: httpx.Response) -> _Upstream:
    """Corps d'erreur `{"type": "error", "error": {"type", "message"}, "request_id"}` ; tout autre corps : son début."""
    kind, message, request_id = "", resp.text[:500], resp.headers.get("request-id", "")
    try:
        data = resp.json()
    except ValueError:
        data = None
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        kind = str(data["error"].get("type") or "")
        message = str(data["error"].get("message") or "")
        request_id = str(data.get("request_id") or request_id)
    retry_after: float | None = None
    try:
        retry_after = max(0.0, float(resp.headers.get("retry-after", "")))
    except ValueError:
        pass
    return _Upstream(resp.status_code, kind, message, retry_after, request_id)


# ───────────────────────── aller : forme OpenAI du noyau → corps /v1/messages ─────────────────────────


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text") or "" for p in content if isinstance(p, dict) and p.get("type") == "text")
    return ""


def _image_block(part: dict[str, Any]) -> dict[str, Any] | None:
    raw = part.get("image_url")
    url = raw.get("url") if isinstance(raw, dict) else raw
    if not isinstance(url, str) or not url:
        return None
    m = _DATA_URL.match(url)
    if m:
        return {"type": "image", "source": {"type": "base64", "media_type": m.group(1), "data": m.group(2)}}
    if url.startswith(("http://", "https://")):
        return {"type": "image", "source": {"type": "url", "url": url}}
    return None


def _user_blocks(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    blocks: list[dict[str, Any]] = []
    for part in content if isinstance(content, list) else []:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind == "text":
            if part.get("text"):
                blocks.append({"type": "text", "text": part["text"]})
        elif kind == "image_url":
            block = _image_block(part)
            if block is None:
                logger.warning("anthropic provider: image non transmise (source ni data URL base64 ni http)")
            else:
                blocks.append(block)
        else:
            logger.warning("anthropic provider: partie de message non transmise (type %s)", kind)
    return blocks


def _tool_id(raw: Any) -> str:
    """Identifiant d'appel d'outil, borné à `[a-zA-Z0-9_-]` : un fil peut porter des identifiants émis par un autre
    amont (changement de palier en cours de fil). Même transformation des deux côtés (appel et résultat)."""
    return _ID_UNSAFE.sub("_", str(raw or ""))


def _assistant_blocks(msg: dict[str, Any]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    text = _text_of(msg.get("content"))
    if text.strip():
        blocks.append({"type": "text", "text": text})
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") if isinstance(tc, dict) and isinstance(tc.get("function"), dict) else {}
        raw_args = fn.get("arguments")
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) and raw_args.strip() else raw_args
        except json.JSONDecodeError:
            args = None
        blocks.append({"type": "tool_use", "id": _tool_id(tc.get("id") if isinstance(tc, dict) else ""),
                       "name": str(fn.get("name") or ""), "input": args if isinstance(args, dict) else {}})
    return blocks


def _tool_result_block(msg: dict[str, Any]) -> dict[str, Any]:
    block: dict[str, Any] = {"type": "tool_result", "tool_use_id": _tool_id(msg.get("tool_call_id"))}
    text = _text_of(msg.get("content"))
    if text:
        block["content"] = text
    return block


def _tool_spec(tool: dict[str, Any]) -> dict[str, Any] | None:
    fn = tool.get("function") if isinstance(tool.get("function"), dict) else None
    if fn is None or not fn.get("name"):
        return None
    return {"name": fn["name"], "description": fn.get("description") or "",
            "input_schema": fn.get("parameters") or {"type": "object", "properties": {}}}


def _cache_control(ttl: str = "") -> dict[str, Any]:
    """`cache_control` d'un point de cache : la durée n'est écrite que pour « 1h » (« 5m » et vide = défaut de l'API,
    corps identique à celui d'avant)."""
    return {"type": "ephemeral", "ttl": "1h"} if ttl == "1h" else {"type": "ephemeral"}


def _system_blocks(texts: list[str], cache: bool, stable_prefix: str | None,
                   ttl: str = "") -> list[dict[str, Any]]:
    """Blocs `system`. Avec le cache et un préfixe stable désigné par le noyau, le premier texte est coupé en deux :
    le préfixe (marqué `cache_control`) puis le reste, à l'octet près. Sinon aucun point dans `system`."""
    blocks: list[dict[str, Any]] = []
    for i, text in enumerate(texts):
        if i == 0 and cache and stable_prefix and text.startswith(stable_prefix):
            blocks.append({"type": "text", "text": stable_prefix, "cache_control": _cache_control(ttl)})
            rest = text[len(stable_prefix):]
            if rest.strip():
                blocks.append({"type": "text", "text": rest})
        elif text.strip():
            blocks.append({"type": "text", "text": text})
    return blocks


def _thinking_adaptive(body: dict[str, Any]) -> bool:
    t = body.get("thinking")
    return isinstance(t, dict) and t.get("type") == "adaptive"


def _add_beta(headers: dict[str, str]) -> None:
    have = [v.strip() for v in headers.get("anthropic-beta", "").split(",") if v.strip()]
    if BINDING_BETA not in have:
        headers["anthropic-beta"] = ",".join([*have, BINDING_BETA])


def _set_drop_block(body: dict[str, Any], headers: dict[str, str]) -> None:
    """`thinking.block_binding.prefix_mismatch_behavior: drop_block` (adaptive seulement : la doc l'exige) + en-tête bêta."""
    if _thinking_adaptive(body):
        body["thinking"] = {**body["thinking"], "block_binding": {"prefix_mismatch_behavior": "drop_block"}}
        _add_beta(headers)


def _log_transformations(data: dict[str, Any], model: str, session_key: str) -> None:
    """Détecteur : `input_transformations` de la réponse (présent avec l'en-tête bêta). Une entrée de liaison de préfixe
    est une fenêtre réécrite par NOUS : journal d'avertissement, chemin et raison, jamais de texte. Les autres raisons
    (changement de modèle ou de compte) ne sont pas à corriger : niveau debug."""
    entries = data.get("input_transformations")
    if not isinstance(entries, list):
        return
    for e in entries:
        if not isinstance(e, dict):
            continue
        level = logging.WARNING if e.get("reason") == "prefix_binding_mismatch" else logging.DEBUG
        logger.log(level, "anthropic provider: input_transformations %s %s %s (%s, session %s)", e.get("type"),
                   e.get("path"), e.get("reason"), model, session_key)


def _thinking_on(body: dict[str, Any]) -> bool:
    t = body.get("thinking") if isinstance(body, dict) else None
    return isinstance(t, dict) and t.get("type") not in (None, "disabled")


def _replay_scope(messages: list[dict[str, Any]]) -> set[int]:
    """Indices des messages assistant dont la réflexion est rejouée : ceux qui suivent le dernier message `user`
    (tour d'outils en cours) et le dernier assistant à appels d'outils (celui que l'API vérifie)."""
    last_user = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=-1)
    scope = {i for i, m in enumerate(messages) if i > last_user and m.get("role") == "assistant"}
    calls = [i for i, m in enumerate(messages) if m.get("role") == "assistant" and m.get("tool_calls")]
    if calls:
        scope.add(calls[-1])
    return scope


def thinking_unusable(messages: list[dict[str, Any]], model: str) -> bool:
    """Un message du tour d'outils porte une réflexion qu'on ne peut pas rejouer à CE modèle (producteur différent,
    ou réflexion omise faute de place) : l'appel doit partir sans réflexion."""
    for i in _replay_scope(messages):
        for b in messages[i].get("thinking_blocks") or []:
            if not isinstance(b, dict) or b.get("type") not in _THINKING_TYPES or b.get("_by") != model:
                return True
    return False


def _replay_blocks(msg: dict[str, Any], model: str) -> list[dict[str, Any]]:
    """Blocs de réflexion d'un message assistant, tels que reçus (`_by`, `_fp`, `_prev` retirés), dans l'ordre."""
    out: list[dict[str, Any]] = []
    for b in msg.get("thinking_blocks") or []:
        if not isinstance(b, dict) or b.get("_by") != model:
            continue
        if b.get("type") == "thinking" and b.get("signature"):
            out.append({"type": "thinking", "thinking": str(b.get("thinking") or ""), "signature": b["signature"]})
        elif b.get("type") == "redacted_thinking" and b.get("data"):
            out.append({"type": "redacted_thinking", "data": b["data"]})
    return out


# ── liaison d'un bloc de réflexion à son préfixe ──
# Un bloc `thinking` est lié à TOUT ce qui le précède (`system`, `tools`, chaque message antérieur) et, sauf le premier
# envoyé, au bloc de réflexion qui le précédait dans la requête qui l'a produit (docs/NOKKAN-ANTHROPIC.md). Le noyau
# garde donc, sur chaque bloc reçu, l'empreinte du préfixe de la requête qui l'a produit (`_fp`) et l'identité du bloc
# envoyé juste avant (`_prev`), et ne rejoue que les blocs dont le préfixe est resté identique : une fenêtre réécrite
# (délestage, clôture d'épisode) abandonne les blocs d'avant la réécriture au lieu de partir en 400.


def _strip_marks(x: Any) -> Any:
    """Contenu comparé par l'API : sans `cache_control` (ne dit que où mettre en cache)."""
    if isinstance(x, dict):
        return {k: _strip_marks(v) for k, v in x.items() if k != "cache_control"}
    if isinstance(x, list):
        return [_strip_marks(v) for v in x]
    return x


def _canon(x: Any) -> bytes:
    return json.dumps(_strip_marks(x), sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _without_thinking(msg: dict[str, Any]) -> dict[str, Any]:
    content = msg.get("content")
    if isinstance(content, list):
        content = [b for b in content if not (isinstance(b, dict) and b.get("type") in _THINKING_TYPES)]
    return {"role": msg.get("role"), "content": content}


def _prefix_hasher(system_blocks: list[dict[str, Any]] | None, specs: list[dict[str, Any]] | None):
    h = hashlib.sha256()
    h.update(_canon({"system": "\x1f".join(str(b.get("text") or "") for b in system_blocks or []), "tools": specs or []}))
    return h


def _feed(h: Any, msg: dict[str, Any]) -> None:
    h.update(b"\x00" + _canon(_without_thinking(msg)))


def body_fingerprint(body: dict[str, Any]) -> str:
    """Empreinte du préfixe qu'un bloc produit PAR cette requête aura : tout le corps envoyé (réflexion rejouée exclue)."""
    h = _prefix_hasher(body.get("system"), body.get("tools"))
    for m in body.get("messages") or []:
        _feed(h, m)
    return h.hexdigest()[:24]


def _block_id(block: dict[str, Any]) -> str:
    return hashlib.sha1(str(block.get("signature") or block.get("data") or "").encode("utf-8")).hexdigest()[:16]


def last_thinking_id(body: dict[str, Any]) -> str:
    """Identité du dernier bloc de réflexion que ce corps envoie (le précédent du premier bloc qu'il fera produire)."""
    for m in reversed(body.get("messages") or []):
        for b in reversed(m.get("content") or [] if isinstance(m.get("content"), list) else []):
            if isinstance(b, dict) and b.get("type") in _THINKING_TYPES:
                return _block_id(b)
    return ""


def stamp_thinking(message: Message, body: dict[str, Any]) -> None:
    """Pose sur les blocs reçus l'empreinte du préfixe de la requête `body` et le bloc envoyé juste avant."""
    if not message.thinking_blocks:
        return
    fp, prev = body_fingerprint(body), last_thinking_id(body)
    for b in message.thinking_blocks:
        b["_fp"], b["_prev"] = fp, prev


def _insert_thinking(out: list[dict[str, Any]], slots: list[tuple[int, int, dict[str, Any], list[dict[str, Any]]]],
                     system_blocks: list[dict[str, Any]], specs: list[dict[str, Any]], legacy: set[int]) -> int:
    """Rejoue en tête des messages assistant de `out` (positions de `slots`) les blocs qui restent valides, et renvoie le
    nombre de messages dont la réflexion n'est PAS rejouée (préfixe modifié ou chaîne rompue).

    `slots` : (position dans `out`, rang dans `messages`, message du noyau, blocs propres), dans l'ordre. Le plus long
    suffixe valide est rejoué : un bloc d'avant une réécriture n'est jamais envoyé, et aucun trou au milieu (l'API
    exige que chaque bloc envoyé, sauf le premier, ait le même bloc précédent que lors de sa production)."""
    h = _prefix_hasher(system_blocks, specs)
    at_pos = {pos: i for i, (pos, *_rest) in enumerate(slots)}
    fp_at: dict[int, str] = {}
    for pos, m in enumerate(out):
        if pos in at_pos:
            fp_at[pos] = h.hexdigest()[:24]
        _feed(h, m)
    n = len(slots)
    pv, link = [True] * n, [True] * n
    for i, (pos, at, msg, blocks) in enumerate(slots):
        raw = [b for b in msg.get("thinking_blocks") or [] if isinstance(b, dict)]
        stamped = raw and all(b.get("_fp") for b in raw)
        if stamped:
            pv[i] = raw[0]["_fp"] == fp_at[pos]
            if i > 0:
                link[i] = raw[0].get("_prev", "") == _block_id(slots[i - 1][3][-1])
        else:  # bloc d'avant le marquage (ou écrit à la main) : seulement l'ancienne portée (tour d'outils en cours)
            pv[i] = at in legacy
    s = n
    while s > 0 and pv[s - 1] and (s == n or link[s]):
        s -= 1
    for pos, _at, _msg, blocks in slots[s:]:
        out[pos]["content"][0:0] = blocks
    return s


def to_messages_body(messages: list[dict[str, Any]], tools: list[dict[str, Any]], *, model: str, max_tokens: int,
                     cache: bool = False, stable_prefix: str | None = None, cache_ttl: str = "",
                     thinking_model: str | None = None) -> dict[str, Any]:
    """Corps `/v1/messages` (sans `stream`, échantillonnage ni réflexion : `AnthropicProvider.complete` les ajoute).
    `thinking_model` (modèle du fournisseur, réflexion active) : rejoue les blocs de réflexion encore valides de TOUS les
    messages assistant du même modèle de la fenêtre (`_insert_thinking`) ; None (défaut) : aucun bloc de réflexion ne
    part. `cache_ttl` (avec `cache`) : durée des points (« 1h » écrite, « 5m »
    = défaut de l'API) ; non vide, un 3e point `cache_control` est posé sur le dernier bloc du dernier message (4 points
    permis par l'API : outils, préfixe, dernier message = 3) ; vide : corps identique à celui d'avant."""
    system: list[str] = []
    out: list[dict[str, Any]] = []
    legacy = _replay_scope(messages) if thinking_model else set()
    slots: list[tuple[int, int, dict[str, Any], list[dict[str, Any]]]] = []

    def push(role: str, blocks: list[dict[str, Any]]) -> int | None:
        """Position du message qui porte `blocks` dans `out` ; None si fusionné dans un message de même rôle."""
        if not blocks:
            return None
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
            return None
        out.append({"role": role, "content": list(blocks)})
        return len(out) - 1

    for at, msg in enumerate(messages):
        role = msg.get("role")
        if role == "system":
            text = _text_of(msg.get("content"))
            if text:
                system.append(text)
        elif role == "assistant":
            pos = push("assistant", _assistant_blocks(msg))
            think = _replay_blocks(msg, thinking_model) if thinking_model and pos is not None else []
            if think:
                slots.append((pos, at, msg, think))
        elif role == "tool":
            push("user", [_tool_result_block(msg)])
        elif role == "user":
            push("user", _user_blocks(msg.get("content")))
        else:
            logger.warning("anthropic provider: message non transmis (rôle %s)", role)

    body: dict[str, Any] = {"model": model, "max_tokens": max_tokens, "messages": out}
    system_blocks = _system_blocks(system, cache, stable_prefix, cache_ttl)
    if system_blocks:
        body["system"] = system_blocks
    specs = [s for s in (_tool_spec(t) for t in tools or [] if isinstance(t, dict)) if s is not None]
    if slots:
        dropped = _insert_thinking(out, slots, system_blocks, specs, legacy)
        if dropped:
            logger.info("anthropic provider: réflexion de %d message(s) non rejouée (préfixe modifié depuis sa production)",
                        dropped)
    if specs:
        if cache:
            specs[-1]["cache_control"] = _cache_control(cache_ttl)
        body["tools"] = specs
    if cache and cache_ttl and out:  # 3e point : le dernier message (le préfixe de la conversation jusqu'ici)
        out[-1]["content"][-1]["cache_control"] = _cache_control(cache_ttl)
    return body


# ───────────────────────── retour : message Anthropic → Completion du noyau ─────────────────────────


def parse_usage(raw: dict[str, Any]) -> Usage:
    """`input_tokens` ne compte QUE le non-caché : le prompt entier est la somme des trois compteurs d'entrée."""
    fresh = raw.get("input_tokens") or 0
    written = raw.get("cache_creation_input_tokens") or 0
    read = raw.get("cache_read_input_tokens") or 0
    return Usage(prompt=fresh + written + read, completion=raw.get("output_tokens") or 0, cached=read,
                 cached_reported=raw.get("cache_read_input_tokens") is not None, cache_write=written)


def _thinking_blocks(blocks: list[dict[str, Any]], by: str) -> list[dict[str, Any]]:
    """Blocs de réflexion reçus, avec signature (ou `data`), dans l'ordre, étiquetés du modèle producteur. Un bloc sans
    signature (flux coupé) est inutilisable à l'API : ignoré. Trop gros : un seul bloc `omitted` à la place."""
    kept: list[dict[str, Any]] = []
    for b in blocks:
        if b.get("type") == "thinking" and b.get("signature"):
            kept.append({"type": "thinking", "thinking": str(b.get("thinking") or ""), "signature": b["signature"],
                         "_by": by})
        elif b.get("type") == "redacted_thinking" and b.get("data"):
            kept.append({"type": "redacted_thinking", "data": b["data"], "_by": by})
    size = sum(len(str(b.get("thinking") or "")) + len(str(b.get("signature") or "")) + len(str(b.get("data") or ""))
              for b in kept)
    if size > MAX_THINKING_CHARS:
        logger.warning("anthropic provider: réflexion de %d caractères > %d, non gardée", size, MAX_THINKING_CHARS)
        return [{"type": "omitted", "_by": by}]
    return kept


def from_message(data: dict[str, Any], by: str | None = None) -> tuple[Message, str | None]:
    """(message du noyau, `finish_reason`). Blocs inconnus ignorés ; la réflexion va dans `thinking_blocks` (producteur
    `by`, défaut : le modèle de la réponse), sur tout message assistant. Un appel d'outil n'est jamais rendu
    quand la réponse est un refus, ni quand `max_tokens` l'a coupé (dernier bloc : ses arguments sont incomplets)."""
    stop = data.get("stop_reason")
    blocks = [b for b in data.get("content") or [] if isinstance(b, dict)]
    if stop == "max_tokens" and blocks and blocks[-1].get("type") == "tool_use":
        blocks = blocks[:-1]
    texts: list[str] = []
    calls: list[ToolCall] = []
    for b in blocks:
        kind = b.get("type")
        if kind == "text":
            texts.append(b.get("text") or "")
        elif kind == "tool_use" and stop != "refusal":
            raw = b.get("_raw_input")
            args = b.get("input")
            if raw is None:
                raw = json.dumps(args if isinstance(args, dict) else {}, ensure_ascii=False)
            calls.append(ToolCall(id=str(b.get("id") or ""), name=str(b.get("name") or ""),
                                  arguments=args if isinstance(args, dict) else {}, raw_arguments=raw))
    message = Message(role="assistant", content="".join(texts).lstrip("\r\n"), tool_calls=calls)
    # Gardée sur TOUT message assistant (pas seulement un tour d'outils) : l'API lie chaque bloc au précédent, la chaîne
    # rejouée ne doit pas avoir de trou (les messages sans appel d'outil sont rejoués aussi, tant qu'ils sont en fenêtre).
    message.thinking_blocks = _thinking_blocks(blocks, by or str(data.get("model") or ""))
    return message, (_FINISH.get(stop, stop) if isinstance(stop, str) else None)


@dataclass
class _MessageAccumulator:
    """Recolle les événements SSE en un `message` complet (blocs par `index`, usage, `stop_reason`)."""

    model: str
    message_id: str = ""
    stop_reason: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    done: bool = False
    blocks: dict[int, dict[str, Any]] = field(default_factory=dict)
    _tool_order: dict[int, int] = field(default_factory=dict)  # index du bloc → rang de l'appel d'outil
    _text_started: bool = False
    model_seen: bool = False  # `message_start` a porté `message.model` : sinon le message reconstitué n'en porte pas

    def _merge_usage(self, raw: Any) -> None:
        if isinstance(raw, dict):
            self.usage.update({k: v for k, v in raw.items() if v is not None})

    def add(self, event: dict[str, Any], on_delta: Callable[[str], None],
            on_tool: Callable[[int, str, str, str], None] | None) -> None:
        kind = event.get("type")
        if kind == "message_start":
            msg = event.get("message") if isinstance(event.get("message"), dict) else {}
            self.message_id = str(msg.get("id") or "")
            if msg.get("model"):
                self.model, self.model_seen = msg["model"], True
            self._merge_usage(msg.get("usage"))
        elif kind == "content_block_start":
            block = dict(event.get("content_block") or {})
            index = event.get("index", len(self.blocks))
            if block.get("type") == "tool_use":
                block["_json"] = ""
                self._tool_order[index] = len(self._tool_order)
            self.blocks[index] = block
            if block.get("type") == "text" and block.get("text"):
                self._text(block, block.pop("text"), on_delta)
        elif kind == "content_block_delta":
            block = self.blocks.get(event.get("index", -1))
            delta = event.get("delta") if isinstance(event.get("delta"), dict) else {}
            if block is None:
                return
            if delta.get("type") == "text_delta" and block.get("type") == "text":
                self._text(block, delta.get("text") or "", on_delta)
            elif delta.get("type") == "input_json_delta" and block.get("type") == "tool_use":
                piece = delta.get("partial_json") or ""
                block["_json"] += piece
                if piece and on_tool is not None:
                    on_tool(self._tool_order[event["index"]], str(block.get("id") or ""),
                            str(block.get("name") or ""), piece)
            elif delta.get("type") == "thinking_delta" and block.get("type") == "thinking":
                block["thinking"] = (block.get("thinking") or "") + (delta.get("thinking") or "")  # jamais affiché
            elif delta.get("type") == "signature_delta" and block.get("type") == "thinking":
                block["signature"] = (block.get("signature") or "") + (delta.get("signature") or "")
            # tout autre delta : ignoré
        elif kind == "message_delta":
            delta = event.get("delta") if isinstance(event.get("delta"), dict) else {}
            if delta.get("stop_reason"):
                self.stop_reason = delta["stop_reason"]
            self._merge_usage(event.get("usage"))
        elif kind == "message_stop":
            self.done = True
        # `ping`, `content_block_stop` et types à venir : rien à faire

    def _text(self, block: dict[str, Any], piece: str, on_delta: Callable[[str], None]) -> None:
        if not self._text_started:
            piece = piece.lstrip("\r\n")  # comme `SokkanProvider` : sauts de ligne de tête ni affichés ni gardés
        if not piece:
            return
        self._text_started = True
        block["text"] = (block.get("text") or "") + piece
        on_delta(piece)

    def as_message(self) -> dict[str, Any]:
        content: list[dict[str, Any]] = []
        for index in sorted(self.blocks):
            block = dict(self.blocks[index])
            if block.get("type") == "tool_use":
                raw = block.pop("_json")
                if raw.strip():
                    try:
                        parsed = json.loads(raw)
                    except json.JSONDecodeError:
                        parsed = None
                    block["input"] = parsed if isinstance(parsed, dict) else {}
                    if not isinstance(parsed, dict):
                        block["_raw_input"] = raw  # gardé pour le journal du noyau (`ToolCall.raw_arguments`)
                elif not isinstance(block.get("input"), dict):
                    block["input"] = {}
            content.append(block)
        out = {"id": self.message_id, "type": "message", "role": "assistant", "content": content,
               "stop_reason": self.stop_reason, "usage": dict(self.usage)}
        if self.model_seen:
            out["model"] = self.model
        return out


__all__ = ["AnthropicProvider", "StreamInterrupted", "from_message", "parse_usage", "to_messages_body"]
