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

Réflexion (API hôte, docs/NOKKAN-HOST-API.md §3) : la réflexion que l'amont renvoie à côté de la réponse — champ
`reasoning` (vLLM 0.32, capture réelle du 11.11.2026 : tests/fixtures/providers/vllm-0.32-reasoning-stream.sse) ou
`reasoning_content` (amonts OpenAI-compatibles plus anciens) du message ou du delta — était jetée. Elle est maintenant
rendue dans `Completion.reasoning`, HORS du message (donc hors du contexte, du store et de la mémoire) et hors de
`raw` en flux ; avec `on_reasoning_delta`, chaque morceau brut est aussi remis dès réception. Un morceau de réflexion
remis compte comme un morceau émis (même règle de réessai que `on_delta`). Sans `on_reasoning_delta` et sans lecteur
de `Completion.reasoning` (drapeau `host_reasoning_stream` éteint) : rien ne change.

Connexion (e2e du 1.11, D5) : trois appels sont tombés après 180 s sur « _ssl.c:1064: The handshake operation timed
out », sans nouvel essai, alors que le journal annonçait « tentative 1/3 » — le délai de connexion (poignée de main TLS
comprise) était celui de lecture (180 s), qui épuisait à lui seul le budget de l'appel. Désormais : délai de connexion
court et distinct (`connect_timeout_s`, 10 s ; lecture, écriture et attente d'une place du pool gardent `timeout_s`), et
une erreur de CONNEXION (`httpx.ConnectError`, `httpx.ConnectTimeout` : refus, DNS, TLS, poignée de main trop longue)
est rejouée jusqu'à `max_retries` fois tant qu'il reste `MIN_RETRY_S` dans le budget de l'appel, sans compter dans
`total_budget_s` (qui borne l'attente d'une RÉPONSE de l'amont) ni dans les autres réessais. Une erreur de connexion
précède par nature tout octet de réponse : rien n'a pu être généré ni facturé. Le journal dit ce qui suit vraiment
(« nouvel essai n dans x s » ou « abandon : raison »), plus de « tentative 1/3 » suivie d'un abandon.
"""
from __future__ import annotations

import copy
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from nokker.core.model_served import clean_served, is_router_url, served_from_headers
from nokker.core.ports import Completion, StreamInterrupted
from nokker.core.quota_error import QUOTA_STATUSES, quota_body, tag_error
from nokker.core.turn import Message, ToolCall, Usage, new_call_id, strip_thinking
from nokker.providers.gate import headers_for as gate_headers

logger = logging.getLogger("nokker.providers.sokkan")

CALL_ID_HEADER = "X-Nokker-Call-Id"
CLIENT_HEADER = "X-Nokker-Client"
_client_tag = "nokker"  # `route.client_tag` (config.py::client_tag_of), posé au chargement de la config
_client_tag_fixed = False


def set_client_tag(tag: str) -> None:
    """Étiquette de déploiement envoyée au Router (déjà validée par `config.client_tag_of`). Posée UNE SEULE FOIS : le
    premier `load_config` la fixe ; un chargement suivant avec une valeur DIFFÉRENTE journalise un WARNING et garde la
    valeur d'origine (jamais de dérive en cours de processus) ; avec la même valeur, rien."""
    global _client_tag, _client_tag_fixed
    if _client_tag_fixed:
        if tag != _client_tag:
            logger.warning("route.client_tag : %r ignoré, l'étiquette %r est déjà posée pour ce processus", tag,
                           _client_tag)
        return
    _client_tag, _client_tag_fixed = tag, True


def reset_client_tag(tag: str = "nokker") -> None:
    """Pour les tests seulement : remet l'étiquette et lève le verrou « posée une fois »."""
    global _client_tag, _client_tag_fixed
    _client_tag, _client_tag_fixed = tag, False

_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
RETRY_AFTER_MAX_S = 30.0  # 503 du Router : au-delà, l'attente demandée ne vaut pas le coup, aucun rejeu
RETRY_AFTER_DEFAULT_S = 1.0  # `Retry-After` absent ou invalide
MIN_RETRY_S = 15.0  # un essai de plus ne part que s'il reste au moins ce temps dans le budget de l'appel
CONNECT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout)  # avant tout octet de réponse (TCP, DNS, TLS)


def _retry_after_s(headers: Any) -> float:
    """Attente demandée par `Retry-After` (secondes entières OU date HTTP, RFC 7231 §7.1.3) ; 1 s si absent ou invalide
    (texte, négatif, date passée). Non plafonnée ici : l'appelant refuse le rejeu au-delà de `RETRY_AFTER_MAX_S`."""
    raw = headers.get("retry-after") if headers is not None else None
    if not isinstance(raw, str) or not raw.strip():
        return RETRY_AFTER_DEFAULT_S
    raw = raw.strip()
    if raw.isascii() and raw.isdigit():
        return float(int(raw))
    try:
        delta = parsedate_to_datetime(raw).timestamp() - time.time()
    except (TypeError, ValueError, IndexError, OverflowError):
        return RETRY_AFTER_DEFAULT_S
    return delta if delta > 0 else RETRY_AFTER_DEFAULT_S


def _is_quota(text: str) -> bool:
    """Corps d'un refus de quota d'inférence : lu en entier (≤ 64 Ko), sinon par son préfixe."""
    probe = tag_error(Exception(), text)
    code = getattr(probe, "error_code", None)
    return code == "quota_exceeded" if isinstance(code, str) else quota_body(text) is not None


class _UpstreamStatus(Exception):
    def __init__(self, status: int, text: str, headers: Any = None) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status
        self.text = text
        self.headers = headers
        tag_error(self, text)


@dataclass
class SokkanProvider:
    """Client OpenAI-compatible d'UN palier (Router SOKKAN ou tout amont compatible)."""

    base_url: str
    api_key: str
    model: str
    reasoning_off_body: dict[str, Any] = field(default_factory=lambda: {"reasoning_effort": "none"})
    reasoning_on_body: dict[str, Any] = field(default_factory=dict)  # défaut amont = raisonnement actif
    timeout_s: float = 180.0
    # Délai de connexion, poignée de main TLS comprise (D5) : court et distinct du délai de lecture.
    connect_timeout_s: float = 10.0
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
    # Plafond de sortie quand l'appelant n'en donne pas (`tiers.<nom>.max_tokens`, sinon 2048 comme avant).
    max_tokens_default: int = 2048
    # Vrai quand ce plafond vient du palier (`tiers.<nom>.max_tokens`) : la boucle du noyau ne lui impose alors pas son défaut.
    max_tokens_pinned: bool = False
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
    # Échantillonnage fixe du palier (`tiers.<nom>.sampling`, ex. {"temperature": 0.3} pour le palier nina, entraîné et
    # mesuré à T=0,3) : ajouté à chaque corps AVANT le corps de raisonnement. Vide (défaut) = défaut de l'amont, inchangé.
    sampling_body: dict[str, Any] = field(default_factory=dict)
    # Client HTTP construit par l'appelant (`timeout` → `httpx.Client`) : BYOK cloud, client gardé contre le SSRF et
    # revérifié à chaque connexion (nokker/byok/guard.py). None = client httpx ordinaire (comportement historique).
    client_factory: Callable[[httpx.Timeout], httpx.Client] | None = None
    # En-tête `X-Session-Id` (affinité du Router) : coupé vers un amont tiers, qui n'a pas à voir nos identifiants de fil.
    session_header: bool = True
    # En-tête `X-Nokker-Call-Id` (identifiant de l'appel, `tiers.<nom>.send_call_id`) : éteint par défaut, jamais vers un
    # amont tiers (BYOK) sans que la config le demande. L'identifiant est inscrit au ledger dans tous les cas.
    send_call_id: bool = False
    # `tiers.<nom>.gate: true` : ce palier reçoit les en-têtes d'amont de la session (`providers/gate.py`), et lui seul.
    gate: bool = False
    # Ce palier parle-t-il au Router de Nokker (`tiers.<nom>.router`) ? None = déduit de l'hôte de `base_url`. Un Router
    # réécrit `model` au nom demandé : sans en-tête `X-Model-Served`, `model_served` reste None (le corps est trompeur).
    router: bool | None = None

    @property
    def behind_router(self) -> bool:
        return is_router_url(self.base_url) if self.router is None else bool(self.router)

    def __post_init__(self) -> None:
        self._client = self._new_client()
        self._closed = False

    def _new_client(self) -> httpx.Client:
        timeout = self._httpx_timeout(self.timeout_s)
        return self.client_factory(timeout) if self.client_factory is not None else httpx.Client(timeout=timeout)

    def _httpx_timeout(self, t: float) -> httpx.Timeout:
        """Lecture, écriture, pool : `t` ; connexion (TLS compris) : `connect_timeout_s`, jamais plus que `t`."""
        return httpx.Timeout(t, connect=min(self.connect_timeout_s, t))

    def detached(self) -> SokkanProvider:
        """Même palier, client HTTP propre : le fermer (`close()`) empêche tout réessai sans toucher aux autres
        appels (HedgedProvider ferme ainsi le primaire qu'il abandonne). La requête déjà en vol, elle, va à son terme
        côté amont : un `recv` bloqué ne se réveille pas d'un autre thread, et le Router ne libère la place qu'à la
        fin de la réponse."""
        twin = copy.copy(self)
        twin._client = self._new_client()
        twin._closed = False
        return twin

    def _deadline(self) -> float:
        return time.monotonic() + (self.call_budget_s if self.call_budget_s is not None else self.timeout_s)

    def _room(self, deadline: float, wait: float = 0.0) -> bool:
        """Reste-t-il de quoi lancer un essai de plus (après `wait` s d'attente) dans le budget de l'appel ?"""
        return deadline - time.monotonic() - wait >= MIN_RETRY_S

    def _timeout(self, deadline: float) -> httpx.Timeout:
        """Délai d'attente d'un essai : le sien, réduit à ce qui reste du budget de l'appel (connexion : la sienne)."""
        return self._httpx_timeout(max(0.001, min(self.timeout_s, deadline - time.monotonic())))

    def _transport_plan(self, e: Exception, n: dict[str, int], t_budget0: float, deadline: float
                        ) -> tuple[str | None, float, str]:
        """Que faire après une erreur réseau survenue AVANT tout morceau remis : (genre de réessai, attente, motif).
        Genre None = abandon (le motif dit pourquoi). `n` compte les réessais déjà faits par genre."""
        normal = n["attempt"] - n["connect"] - n["timeout"] - n.get("r503", 0)
        wait = self.retry_backoff_s * (n["attempt"] + 1)
        if n.get("r503"):  # un 503 rejoué une fois et c'est tout : l'essai de rejeu a échoué, on sort
            return None, 0.0, "rejeu après 503 déjà fait"
        if isinstance(e, CONNECT_ERRORS):
            if n["connect"] >= self.max_retries:
                return None, 0.0, f"{n['connect'] + 1} échecs de connexion"
            if not self._room(deadline, wait):
                return None, 0.0, "budget de l'appel épuisé"
            return "connect", wait, ""
        timed_out = isinstance(e, httpx.TimeoutException)
        if timed_out and n.get("tmo", 0) >= self.timeout_retries:
            # Un délai d'attente donne AU PLUS `timeout_retries` rejeu(x) au total : le rejeu « normal » et celui
            # « délai d'attente » ne s'additionnent pas (avant : 2 « normal » + 1 « timeout » = 3 rejeux).
            return None, 0.0, "réessais épuisés"
        if normal < self.max_retries and self._budget_left(t_budget0) and self._room(deadline, wait):
            n["tmo"] = n.get("tmo", 0) + timed_out
            return "retry", wait, ""
        if timed_out and n["timeout"] < self.timeout_retries and self._room(deadline):
            n["tmo"] = n.get("tmo", 0) + 1
            return "timeout", 0.0, ""
        if normal >= self.max_retries:
            return None, 0.0, "réessais épuisés"
        return None, 0.0, "budget de l'appel épuisé"

    def _log_transport(self, what: str, e: Exception, kind: str | None, wait: float, why: str, attempt: int,
                       deadline: float) -> None:
        """Journal juste (D5) : l'essai qui a échoué, puis ce qui suit VRAIMENT."""
        nature = "connexion" if isinstance(e, CONNECT_ERRORS) else "réseau"
        if kind is None:
            logger.warning("sokkan provider: %s — erreur de %s à l'essai %d %s: %s — abandon (%s)",
                           what, nature, attempt + 1, self.model, e, why)
        else:
            logger.warning("sokkan provider: %s — erreur de %s à l'essai %d %s: %s — nouvel essai %d dans %.0f s "
                           "(%.0f s restantes dans le budget de l'appel)", what, nature, attempt + 1, self.model, e,
                           attempt + 2, wait, deadline - time.monotonic())

    def _wait_503(self, status: int, text: str, headers: Any, n: dict[str, int], t_budget0: float,
                  deadline: float, attempt: int) -> float:
        """503 du Router (`upstream_timeout` / `no_upstream`, + `Retry-After`) : rejoué UNE fois, quel que soit
        `max_retries`. Rend l'attente avant ce rejeu, ou lève l'erreur (motif `error.code` dans le message ET le journal)
        si déjà rejoué, si `Retry-After` dépasse `RETRY_AFTER_MAX_S` ou si l'attente ne tient ni dans `total_budget_s`
        ni dans le budget de l'appel (`_room`)."""
        code = getattr(tag_error(Exception(), text), "error_code", None)
        rid = headers.get("x-request-id") if headers is not None else None
        wait = _retry_after_s(headers)
        why = None
        if n["r503"] >= 1:
            why = "déjà rejoué une fois"
        elif wait > RETRY_AFTER_MAX_S:
            why = f"Retry-After {wait:.0f}s > {RETRY_AFTER_MAX_S:.0f}s"
        elif time.monotonic() - t_budget0 + wait >= self.total_budget_s:
            why = f"budget total {self.total_budget_s}s"
        elif not self._room(deadline, wait):
            why = "budget de l'appel"
        motif = f" motif={code}" if code else ""
        if why is not None:
            logger.warning("sokkan provider: HTTP 503%s req=%s (tentative %d) %s — abandon (%s)",
                           motif, rid, attempt + 1, self.model, why)
            raise tag_error(RuntimeError(f"HTTP 503 de {self.base_url}{' (' + str(code) + ')' if code else ''} après "
                                         f"{attempt + 1} tentative(s) ({why}): {text[:200]}"), text)
        logger.warning("sokkan provider: HTTP 503%s req=%s (tentative %d) %s — un rejeu dans %.0fs",
                       motif, rid, attempt + 1, self.model, wait)
        return wait

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
                 session_key: str, max_tokens: int | None = None,
                 on_delta: Callable[[str], None] | None = None,
                 on_tool_delta: Callable[[int, str, str, str], None] | None = None,
                 on_reasoning_delta: Callable[[str], None] | None = None) -> Completion:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": strip_thinking(messages),  # les blocs de réflexion d'un fournisseur Anthropic ne vont qu'à lui
            "max_tokens": max_tokens or self.max_tokens_default,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        body.update(self.sampling_body)
        body.update(self.reasoning_on_body if reasoning else self.reasoning_off_body)

        headers = {"Content-Type": "application/json"}
        if self.session_header:
            headers["X-Session-Id"] = session_key
        if self.api_key:  # un amont sans auth (ex. llama.cpp local) refuse un Bearer vide
            headers["Authorization"] = f"Bearer {self.api_key}"
        headers.update(self.extra_headers)
        call_id = new_call_id()  # un par appel du noyau ; les réessais HTTP d'un même appel gardent le leur
        if self.send_call_id:
            headers[CALL_ID_HEADER] = call_id
        headers.update(gate_headers(self.gate, session_key))
        if self.behind_router and self.client_factory is None:  # le Router de Nokker seulement : jamais un amont tiers/BYOK
            headers[CLIENT_HEADER] = _client_tag

        t0 = time.monotonic()
        meta: dict[str, Any] = {}  # en-têtes de la réponse retenue (X-Model-Served, X-Sokkan-Upstream)
        if on_delta is not None:
            body["stream"] = True
            body["stream_options"] = {"include_usage": True}
            thought: list[str] = []  # réflexion du flux, gardée hors du corps reconstitué (`raw` inchangé)
            data, attempts = self._stream_with_retry(body, headers, on_delta, on_tool_delta, on_reasoning_delta,
                                                     thought, meta)
            reasoning_text = "".join(thought)
        else:
            data, attempts = self._post_with_retry(body, headers, meta)
            reasoning_text = _message_reasoning((data.get("choices") or [{}])[0].get("message"))
        latency_s = time.monotonic() - t0

        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        usage_raw = data.get("usage") or {}
        usage = _parse_usage(usage_raw)
        tool_calls = [_parse_tool_call(tc) for tc in (msg.get("tool_calls") or [])]
        # Un amont qui retire le bloc de raisonnement laisse ses « \n\n » en tête de réponse (Lokkal, 28.10).
        message = Message(role="assistant", content=(msg.get("content") or "").lstrip("\r\n"), tool_calls=tool_calls)
        served_h, upstream = served_from_headers(meta.get("headers"))
        # En-tête du Router d'abord ; sans lui, le corps seulement pour un amont direct (un Router le réécrit).
        served = served_h or (None if self.behind_router else clean_served(data.get("model")))
        return Completion(message=message, usage=usage, model=data.get("model", self.model),
                           latency_s=latency_s, finish_reason=choice.get("finish_reason"), raw=data,
                           attempts=attempts, reasoning=reasoning_text, call_id=call_id,
                           model_served=served, upstream=upstream)

    def _post_with_retry(self, body: dict[str, Any], headers: dict[str, str],
                         meta: dict[str, Any] | None = None) -> tuple[dict[str, Any], int]:
        last_exc: Exception | None = None
        t_budget0 = time.monotonic()
        deadline = self._deadline()
        attempt, busy = 0, 0
        n = {"attempt": 0, "connect": 0, "timeout": 0, "tmo": 0, "r503": 0}
        while attempt <= self.max_retries + n["timeout"] + n["connect"] + n["r503"]:
            self._check_open()
            try:
                resp = self._client.post(f"{self.base_url.rstrip('/')}/chat/completions", json=body,
                                          headers=headers, timeout=self._timeout(deadline))
            except httpx.TransportError as e:
                last_exc = e
                n["attempt"] = attempt
                kind, wait, why = self._transport_plan(e, n, t_budget0, deadline)
                self._log_transport("appel", e, kind, wait, why, attempt, deadline)
                if kind is None:
                    raise RuntimeError(f"échec réseau après {attempt + 1} tentative(s) : {e}") from e
                if kind in ("connect", "timeout"):
                    n[kind] += 1
                if wait:
                    time.sleep(wait)
                attempt += 1
                continue

            # Quota d'inférence atteint (429 `quota_exceeded`) : pas une panne, ni attente de clé saturée ni réessai.
            quota = resp.status_code in QUOTA_STATUSES and _is_quota(resp.text)
            if not quota and self._busy_wait(resp.status_code, busy, t_budget0, deadline):
                busy += 1
                continue
            if not quota and resp.status_code == 503:
                time.sleep(self._wait_503(503, resp.text, resp.headers, n, t_budget0, deadline, attempt))
                n["r503"] += 1
                attempt += 1
                continue
            if (not quota and resp.status_code in _RETRYABLE_STATUS and not n["r503"]
                    and attempt - n["connect"] - n["timeout"] < self.max_retries):
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
                raise tag_error(RuntimeError(f"HTTP {resp.status_code} de {self.base_url}: {resp.text[:500]}"), resp.text)
            if meta is not None:
                meta["headers"] = resp.headers
            return resp.json(), attempt + busy + 1  # les rejeux 429 sont des requêtes HTTP : visibles au ledger
        if last_exc:
            raise RuntimeError(f"échec réseau après {self.max_retries + 1} tentatives") from last_exc
        raise RuntimeError("échec sans réponse (ne devrait pas arriver)")

    def _stream_with_retry(self, body: dict[str, Any], headers: dict[str, str], on_delta: Callable[[str], None],
                           on_tool_delta: Callable[[int, str, str, str], None] | None = None,
                           on_reasoning_delta: Callable[[str], None] | None = None,
                           thought: list[str] | None = None,
                           meta: dict[str, Any] | None = None) -> tuple[dict[str, Any], int]:
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

        def _relay_reasoning(piece: str) -> None:
            emitted[0] = True  # le client a vu de la réflexion : plus de réessai silencieux
            assert on_reasoning_delta is not None
            on_reasoning_delta(piece)

        t_budget0 = time.monotonic()
        deadline = self._deadline()
        attempt, busy = 0, 0
        n = {"attempt": 0, "connect": 0, "timeout": 0, "tmo": 0, "r503": 0}
        while attempt <= self.max_retries + n["timeout"] + n["connect"] + n["r503"]:
            self._check_open()
            try:
                if thought is not None:
                    del thought[:]  # un essai rejoué repart d'une réflexion vide
                return (self._stream_once(body, headers, _relay, _relay_tool if on_tool_delta is not None else None,
                                          timeout=self._timeout(deadline),
                                          on_reasoning_delta=_relay_reasoning if on_reasoning_delta is not None
                                          else None, thought=thought, meta=meta),
                        attempt + busy + 1)
            except _UpstreamStatus as e:
                # Statut connu avant le moindre octet de corps : rien n'a pu être émis.
                quota = e.status in QUOTA_STATUSES and _is_quota(e.text)  # pas une panne : aucun rejeu
                if not quota and self._busy_wait(e.status, busy, t_budget0, deadline):
                    busy += 1
                    continue
                if not quota and e.status == 503:
                    time.sleep(self._wait_503(503, e.text, e.headers, n, t_budget0, deadline, attempt))
                    n["r503"] += 1
                    attempt += 1
                    continue
                if (not quota and e.status in _RETRYABLE_STATUS and not n["r503"]
                        and attempt - n["connect"] - n["timeout"] < self.max_retries):
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
                raise tag_error(RuntimeError(f"HTTP {e.status} de {self.base_url}: {e.text[:500]}"), e.text) from None
            except (httpx.TransportError, _StreamError) as e:
                if emitted[0]:
                    raise StreamInterrupted(f"flux amont interrompu après le premier morceau : {e}") from e
                n["attempt"] = attempt
                kind, wait, why = self._transport_plan(e, n, t_budget0, deadline)
                self._log_transport("flux en échec avant tout morceau", e, kind, wait, why, attempt, deadline)
                if kind is not None:
                    if kind in ("connect", "timeout"):
                        n[kind] += 1
                    if wait:
                        time.sleep(wait)
                    attempt += 1
                    continue
                raise RuntimeError(f"échec du flux après {attempt + 1} tentative(s) : {e}") from e
        raise RuntimeError("échec sans réponse (ne devrait pas arriver)")

    def _stream_once(self, body: dict[str, Any], headers: dict[str, str], on_delta: Callable[[str], None],
                     on_tool_delta: Callable[[int, str, str, str], None] | None = None,
                     timeout: float | None = None,
                     on_reasoning_delta: Callable[[str], None] | None = None,
                     thought: list[str] | None = None, meta: dict[str, Any] | None = None) -> dict[str, Any]:
        """Un appel SSE, reconstitué en corps non-streamé (`{"model", "choices": [{"message",
        "finish_reason"}], "usage"}`) pour passer par le même analyseur que `_post_with_retry`."""
        acc = _StreamAccumulator(model=self.model)
        with self._client.stream("POST", f"{self.base_url.rstrip('/')}/chat/completions", json=body,
                                 headers=headers, timeout=timeout if timeout is not None else self.timeout_s) as resp:
            if resp.status_code >= 400:
                resp.read()
                raise _UpstreamStatus(resp.status_code, resp.text, resp.headers)
            if meta is not None:
                meta["headers"] = resp.headers  # arrivent avec la réponse, avant le premier morceau
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
                think = _delta_reasoning(chunk)
                if think:
                    if thought is not None:
                        thought.append(think)
                    if on_reasoning_delta is not None:
                        on_reasoning_delta(think)
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
    model_seen: bool = False  # un chunk a porté `model` : sinon le corps reconstitué n'en porte pas (jamais le défaut)

    def add(self, chunk: dict[str, Any], on_tool: Callable[[int, str, str, str], None] | None = None) -> str:
        if chunk.get("model"):
            self.model = chunk["model"]
            self.model_seen = True
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
            if isinstance(text, str) and not self.content:
                text = text.lstrip("\r\n")  # cf. complete() : sauts de ligne de tête, ni affichés ni gardés
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
        data: dict[str, Any] = {"choices": [{"index": 0, "message": message, "finish_reason": self.finish_reason}]}
        if self.model_seen:  # absent du flux : absent du corps (`complete` retombe alors sur le modèle du palier)
            data = {"model": self.model, **data}
        if self.usage is not None:
            data["usage"] = self.usage
        return data


REASONING_KEYS = ("reasoning", "reasoning_content")  # vLLM récent, puis amonts OpenAI-compatibles plus anciens


def _message_reasoning(obj: Any) -> str:
    """Réflexion d'un message ou d'un delta : la première clé connue qui porte du texte (jamais les deux à la suite,
    un amont de transition envoie le même texte sous les deux noms)."""
    if not isinstance(obj, dict):
        return ""
    for key in REASONING_KEYS:
        value = obj.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _delta_reasoning(chunk: dict[str, Any]) -> str:
    out = ""
    for choice in chunk.get("choices") or []:
        if isinstance(choice, dict) and choice.get("index", 0) == 0:
            out += _message_reasoning(choice.get("delta"))
    return out


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
