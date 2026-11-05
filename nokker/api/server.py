"""`/v1/chat/completions` OpenAI-compatible en SSE + `/healthz` + `/metrics` — PLAN-P1 §2.8.

Contrat consommé par la passerelle amont en mode proxy (`gateway/run_turn.py` dans hermes-nokker) :
  - `GATEWAY_PROXY_URL` + `_run_agent_inner` (`:4264`) aiguillent vers `_run_agent_via_proxy` (`:2752`) ;
  - requête : `POST {proxy_url}/v1/chat/completions`, `Authorization: Bearer <GATEWAY_PROXY_KEY>`,
    `X-Hermes-Session-Id: <session_id>`, corps `{"model": "hermes-agent", "messages": [...], "stream": true}` ;
  - réponse attendue : SSE, chaque ligne `data: {...}` avec `choices[0].delta.content`, terminée par
    `data: [DONE]`. `_consume_sse_line` (`:2827`) ignore tout frame malformé (on peut donc streamer en
    plusieurs chunks sans risquer de casser le proxy).

`X-Hermes-Session-Id` devient le `thread_id` du noyau (continuité de fil, F7 bascule le canari dessus).
Streaming (P1, 19.10.2026, `_stream_turn`) : en `stream: true` les en-têtes SSE partent TOUT DE SUITE, le tour
tourne dans un fil de travail et un commentaire `: ping` part toutes les ~10 s tant que rien d'autre ne sort
(Cloudflare coupe à 100 s sans octet ; tour réel de ~2 min mesuré sur le canari). Sans opt-in, le corps est,
pings exceptés, celui d'avant (rôle, réponse + usage, stop, `[DONE]`) : `_consume_sse_line` ne lit que les
lignes `data: ` — la passerelle amont ne voit aucune différence. Les jetons en direct sont réservés aux clients qui
posent `X-Nokker-Stream-Tokens: 1` (le web) : la gateway concatène TOUT `delta.content` reçu, elle ne doit donc
jamais recevoir de texte partiel ni de reset. Contrat : docs/CLIENT-API.md §Streaming.

**Sécurité de `Turn.sender`** (consigne produit F2, 17.10.2026) : il ne vient JAMAIS d'un champ que le
client contrôle (`user` du corps, un en-tête arbitraire) — seulement de la clé Bearer effectivement
présentée, résolue via `NokkerConfig.api_key_map()` (secret que seul le vrai appelant détient). La
passerelle amont en mode proxy ne transmet AUCUNE identité vérifiable aujourd'hui (corps strictement
`{model, messages, stream}`, aucun en-tête d'identité — vérifié dans son code) : tant qu'elle n'en
transmet pas, un appel qui arrive par ce chemin avec la clé Bearer PARTAGÉE du proxy (si c'est la seule
configurée) résout `sender` à l'identité attachée à CETTE clé en config — pas à « n'importe qui » ; il
n'y a aucun GO possible venant d'une identité que la config n'a pas explicitement liée à la clé présentée.

Implémentation en `http.server` stdlib (pas de dépendance supplémentaire pour un serveur qui ne fait que
ça) : `ThreadingHTTPServer` + encodage manuel du SSE en `Transfer-Encoding: chunked`.
"""
from __future__ import annotations

import contextvars
import hashlib
import hmac
import json
import logging
import os
import queue
import re
import threading
import time
import uuid
from contextlib import ExitStack
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from nokker.agents.hub import AgentsHub
from nokker.agents.memory import InMemoryAgents
from nokker.agents.types import (
    AgentConflict, AgentError, AgentMissing, AgentNotFound, agent_to_json, run_to_json,
)
from nokker.approvals import crypto as approvals_crypto
from nokker.approvals.base import BaseApprovals
from nokker.approvals.errors import (
    ApprovalError, ApprovalExpired, ApprovalNotFound, ApprovalNotPending, DeviceError, DeviceNotApproved, DeviceNotFound,
    ProofInvalid,
    StrongDeviceRequired,
)
from nokker.approvals.executor import ApprovalExecutor
from nokker.approvals.memory import InMemoryApprovals
from nokker.approvals.notify import ApprovalNotifier, NullNotifier, TelegramNotifier
from nokker.approvals.pg import PgApprovals
from nokker.approvals.types import Device
from nokker.api.events import EventBus
from nokker.api.previews_routes import PreviewsRoutes, register_previews_routes
from nokker.bubbles.hub import BubbleHub
from nokker.bubbles.memory import InMemoryBubbles
from nokker.bubbles.tools import BubbleToolSource
from nokker.bubbles.types import BubbleError, BubbleForbidden, BubbleNotFound, VersionConflict, bubble_to_json
from nokker.channels import http as channels_http
from nokker.config import NokkerConfig, load_config
from nokker.context.engine import ContextEngineV0
from nokker.context.engine_v1 import ContextEngineV1
from nokker.context.manager import ContextManager
from nokker.context.tools import RecallContextSource
from nokker.context.types import ContextBudgets
from nokker.core.controls import ANY_ACCOUNT, STEER_MAX_CHARS
from nokker.core.loop import DIRECT_CHANNEL, Kernel
from nokker.core.ports import Approval, Invariants, Proof
from nokker.core.turn import RELAY_CHANNEL, Decision, Gate, Tier, Turn, TurnInput, Verdict
from nokker.core.turn_lock import TurnLocks
from nokker.ledger.costs import InMemoryLedger, LedgerBackend, PostgresLedger
from nokker.ledger.metrics import render as render_metrics
from nokker.ledger.turn_cost import root_thread
from nokker.memory.corthexis import CorthexisMemory  # noqa: F401 — nom public du module (tests, appelants)
from nokker.memory.corthexis import from_config as memory_from_config
from nokker.memory.corthexis import status_of as memory_status
from nokker.attachments import TextAttachments, render_block, split_kinds
from nokker.images import ImageError, ImageStore, parse_attachments, sniff_media_type, split_content
from nokker.providers.sokkan import SokkanProvider
from nokker.qa.calibrate import DEFAULT_THRESHOLDS, load_thresholds_file
from nokker.qa.escalate import EscalatorV0
from nokker.qa.gates import GatesV0, GatesV1
from nokker.qa.invariants import Allowlist, DeterministicInvariants, GoAuthorizedSenders
from nokker.qa.judge import HttpJudgeTransport, OpenAICompatJudge
from nokker.qa.provenance import build_provenance
from nokker.qa.review import CardAdvisor, DiffReviewer, parse_advice
from nokker.qa.secrets import KnownSecrets
from nokker.route.classify import ModelClassifier
from nokker.route.policy import PolicyV0, PolicyV1
from nokker.store.pg import MemoryStore, PgStore, apply_migrations
from nokker import feedback as feedback_mod
from nokker.connectors import http as connectors_http
from nokker.cloud import http as cloud_http
from nokker.billing import http as billing_http
from nokker.onboarding import http as onboarding_http
from nokker.incidents import http as incidents_http
from nokker.homewatch import http as home_watch_http
from nokker.plan import http as plan_http
from nokker.house import http as house_http
from nokker.presence import http as presence_http
from nokker.guide import http as guide_http
from nokker.household import http as household_http
from nokker.ambient import http as ambient_http
from nokker.household.paper import http as paper_http
from nokker.household.famcal import http as famcal_http
from nokker.payments import http as payments_http
from nokker.team import http as team_http
from nokker.webpacks import http as packs_http
from nokker.learning import http as learning_http
from nokker.memory import long_http
from nokker.memory.consolidation import http as consolidation_http
from nokker.sso import http as sso_http
from nokker.code_sessions import http as code_http
from nokker.account import http as account_http
from nokker.telegram import http as telegram_http
from nokker.connectors.provider import ConnectorProvider as ConnectorsForTenant
from nokker.connectors.service import Connectors
from nokker.traces.recorder import build_recorder
from nokker.tenancy.connectors import ConnectorProvider
from nokker.tenancy.context import (
    INTERNAL_TENANT, current_tenant, current_tenant_or_none, scoped_thread_id, tenant_of_thread, tenant_scope,
)
from nokker.tenancy.resolve import UnknownSender, account_tenant_fn, is_account, resolve_tenant
from nokker.tenancy.limits import LimitedPolicy, TenantLimits
from nokker.tenancy.resources import NullMemory, TenantEscalator, TenantMemory, TenantRegistry
from nokker.tenancy.store import MemoryTenancy, PgTenancy, Tenancy
from nokker.threads import export as thread_export
from nokker.threads.memory import InMemoryThreads
from nokker.threads.shares import MemoryShares, ShareError, ShareNotFound, ThreadShares, share_to_json
from nokker.threads.types import ThreadError, ThreadForbidden, ThreadNotFound
from nokker.tools.checkpoints import direct_result_body
from nokker.tools.effects import EffectsTable, SecretScrubbingToolSource
from nokker.tools.mcp import McpToolSource
from nokker.tools.registry import Registry, builtin_core_source
from nokker.ledger.audit import AuditedTools
from nokker.local.api import LOCAL_OWNER, LocalApi
from nokker.tools.web import WebToolSource
from nokker.voice import speak_lexicon as speak_lexicon_mod

logger = logging.getLogger("nokker.api.server")

# Fichiers de la Mini App Telegram servis sous `/miniapp/<nom>` (branche `p1/miniapp`,
# `nokker/api/miniapp/`) — ALLOWLIST STRICTE : jamais `package.json`, `*.test.mjs`,
# `gen-signature-vector.mjs` (outillage de build/test, pas des assets servis au client).
MINIAPP_DIR = Path(__file__).parent / "miniapp"
MINIAPP_FILES: dict[str, str] = {
    "approve.html": "text/html; charset=utf-8",
    "approve.js": "text/javascript; charset=utf-8",
    "approve-core.js": "text/javascript; charset=utf-8",
    "style.css": "text/css; charset=utf-8",
}

# Enrôlements sans identité (§3.1bis) : au plus ANON_ENROLL_MAX par fenêtre glissante, tous clients confondus — un
# appareil pending ne donne aucun droit, la borne évite seulement de remplir la table.
ANON_ENROLL_MAX, ANON_ENROLL_WINDOW_S = 10, 600.0
_anon_enrolls: list[float] = []

# `stream: true` : un commentaire SSE `: ping` part toutes les STREAM_PING_S tant que le tour ne produit rien
# (Cloudflare coupe à 100 s sans octet — tour réel de ~2 min mesuré sur le canari le 19.10.2026).
STREAM_PING_S = 10.0
_anon_lock = threading.Lock()


def _anon_enroll_allowed(now: float | None = None) -> bool:
    t = time.time() if now is None else now
    with _anon_lock:
        _anon_enrolls[:] = [x for x in _anon_enrolls if t - x < ANON_ENROLL_WINDOW_S]
        if len(_anon_enrolls) >= ANON_ENROLL_MAX:
            return False
        _anon_enrolls.append(t)
        return True


_RE_APPROVAL_ONE = re.compile(r"^/v1/approvals/([^/]+)$")
_RE_APPROVAL_CHALLENGE = re.compile(r"^/v1/approvals/([^/]+)/challenge$")
_RE_APPROVAL_KNOCK_ARM = re.compile(r"^/v1/approvals/([^/]+)/knock-arm$")  # heurtoir (docs/KNOCK.md §5.3)
_RE_APPROVAL_DECISION = re.compile(r"^/v1/approvals/([^/]+)/decision$")
_RE_APPROVAL_AUTO_REVOKE = re.compile(r"^/v1/approvals/([^/]+)/auto-review/revoke$")
# Attente maximale de `POST /v1/approvals/{id}/decision` (Mini App, web) : sous les 100 s de Cloudflare, tour en cours
# sur le fil compris ; au-delà, la carte part telle qu'elle est (`approved`) et l'exécution suit en fond.
DECISION_WAIT_S = 45.0
_RE_DEVICE_ONE = re.compile(r"^/v1/devices/([^/]+)$")
_RE_DEVICE_CONFIRM = re.compile(r"^/v1/devices/([^/]+)/confirm$")
_RE_DEVICE_SESSION_CHALLENGE = re.compile(r"^/v1/devices/([^/]+)/session-challenge$")
_RE_MINIAPP = re.compile(r"^/miniapp/([^/]+)$")
_RE_LOG_SECRET = re.compile(r"([?&](?:t|token|code|init_data|key)=)[^&\s\"]+")
_RE_LOG_DEVICE_TOKEN = re.compile(r"\b(nk[dg]_)[A-Za-z0-9_-]+")  # jetons d'appareil (e-ink dans l'URL, glance)
_RE_THREAD_ONE = re.compile(r"^/v1/threads/([^/]+)$")
# Un fil de sous-agent `<parent>/sa-<id>` s'annule aussi (le « / » en clair ou encodé %2F, §7.22).
_RE_THREAD_CANCEL = re.compile(r"^/v1/threads/([^/]+(?:/sa-[A-Za-z0-9_-]+)?)/cancel$")
# Pause, reprise et steer d'un fil (docs/CLIENT-API.md §Pause et steer) ; `/v1/pause` et `/v1/resume` : tout le compte.
_RE_THREAD_CONTROL = re.compile(r"^/v1/threads/([^/]+(?:/sa-[A-Za-z0-9_-]+)?)/(pause|resume|steer)$")
_RE_PAGE = re.compile(r"^/v1/pages/(pg_[0-9a-f]{16})(/link|/publish|/unpublish|/locality)?$")  # BUBBLES §10, §7.26
_RE_PUBLIC_PAGE = re.compile(r"^/v1/pages/public/([A-Za-z0-9_-]{24})$")  # page publique (docs/LOKKAL.md §7.26)
# Aperçu du port d'une commande en arrière-plan (lot X6, docs/LOKKAL.md §7.7) : capacité signée DANS le chemin.
_RE_PROC_LINK = re.compile(r"^/v1/local/processes/(bg_[0-9a-f]{8})/link$")
_RE_PREVIEW = re.compile(r"^/v1/local/preview/(bg_[0-9a-f]{8})/(\d+\.[0-9a-f]{32})(/.*)?$")
# Outils qu'un client peut appeler hors tour (docs/LOKKAL.md §7.4.4) : points de retour, skills, lecture du plan. Jamais
# terminal, write_file, patch : ceux-là restent au modèle, sous ses portes. `memory` (lister, lire, oublier) s'y ajoute
# quand la mémoire de projet est allumée (`local.project_memory`, X1) ; `delegate_status` (sans attente) quand les
# sous-agents en arrière-plan existent (`/agents`) ; `lsp_diagnostics` quand les diagnostics de langage sont allumés
# (`local.lsp.enabled`, lot 13).
DIRECT_TOOLS = frozenset({"checkpoint_list", "checkpoint_diff", "checkpoint_create", "checkpoint_restore", "skill_list",
                          "skill_view", "todo"})
NOKKER_VERSION = "0.1.0"
MIN_CLIENT_VERSION = "0.1.0"
_RE_THREAD_SUB = re.compile(r"^/v1/threads/([^/]+)/(messages|bubbles)$")
_RE_THREAD_IMAGE = re.compile(r"^/v1/threads/([^/]+)/images/([0-9a-f]{64})$")
# Export d'un fil et liens partagés (docs/CLIENT-API.md « Export et partage d'un fil », nokker/threads/shares.py).
_RE_THREAD_EXPORT = re.compile(r"^/v1/threads/([^/]+)/export$")
_RE_THREAD_SHARES = re.compile(r"^/v1/threads/([^/]+)/shares$")
# Objectif et rubrique du fil (docs/OUTCOMES-GOAL.md), drapeaux `outcomes_enabled` / `goals_enabled`.
_RE_THREAD_GOAL = re.compile(r"^/v1/threads/([^/]+)/goal(?:/(stop|resume|freeze))?$")
_RE_SHARE_ONE = re.compile(r"^/v1/shares/(sh_[0-9a-f]{20})$")
_RE_PUBLIC_SHARE = re.compile(r"^/v1/public/shares/([^/.]+)(\.html|\.md|\.json)?$")
_RE_LOCAL_EXPORT = re.compile(r"^/v1/local/sessions/([^/]+)/export$")
# Corps de /v1/chat/completions (X7) : 4 images de 5 Mo en base64 (~27 Mo) + texte et historique client.
MAX_CHAT_BODY = 32 * 1024 * 1024
_RE_BUBBLE_ONE = re.compile(r"^/v1/bubbles/([^/]+)$")
_RE_BUBBLE_OP = re.compile(r"^/v1/bubbles/([^/]+)/(close|transfer|share)$")
_RE_BUBBLE_ACTION = re.compile(r"^/v1/bubbles/([^/]+)/actions/([^/]+)$")
_RE_APP_CALL = re.compile(r"^/v1/apps/([A-Za-z0-9_-]+)/tools/call$")  # MCP Apps (docs/MCP-NEXT.md §4)
# Crew (docs/BUBBLES.md §9.4, §9.5)
_RE_AGENT = re.compile(r"^/v1/agents/(ag_[0-9a-f]{12})(?:/(arm|disarm|run|runs|webhook-secret|move))?$")
_RE_AGENT_RUN_CANCEL = re.compile(r"^/v1/agents/(ag_[0-9a-f]{12})/runs/(ar_[0-9a-f]{16})/cancel$")
_RE_AGENT_WEBHOOK = re.compile(r"^/v1/agents/(ag_[0-9a-f]{12})/webhooks/([A-Za-z0-9_.-]{1,64})$")
MAX_WEBHOOK_BODY = 64 * 1024


def build_invariants(config: NokkerConfig) -> DeterministicInvariants:
    known = KnownSecrets.load(env_prefix=config.known_secrets_env_prefix, file_path=config.known_secrets_file)
    allowlist = Allowlist.load(domains=config.allowlist_domains, addresses=config.allowlist_addresses)
    go_senders = GoAuthorizedSenders(senders=config.go_senders())
    return DeterministicInvariants(known=known, allowlist=allowlist, go_senders=go_senders)


def build_ledger(config: NokkerConfig) -> LedgerBackend:
    dsn = config.ledger_dsn()
    return PostgresLedger(dsn) if dsn else InMemoryLedger()


def build_notifier(config: NokkerConfig) -> ApprovalNotifier:
    """`NullNotifier` par défaut (`approvals_telegram_enabled=False`, ou jeton de bot absent) —
    JAMAIS de vrai appel réseau tant que les deux conditions ne sont pas remplies explicitement."""
    if not config.approvals_telegram_enabled:
        return NullNotifier()
    token = config.telegram_bot_token()
    if not token:
        logger.warning("approvals_telegram_enabled=True mais %s est vide — notificateur nul",
                       config.telegram_bot_token_env)
        return NullNotifier()
    if not config.telegram_mini_app_url:
        logger.warning("approvals_telegram_enabled=True mais telegram_mini_app_url est vide — notificateur nul")
        return NullNotifier()
    return TelegramNotifier(bot_token=token, mini_app_url=config.telegram_mini_app_url,
                            chat_ids=tuple(config.telegram_ids.keys()))


def build_approvals(config: NokkerConfig, on_event: Any | None = None) -> BaseApprovals:
    """`PgApprovals` si un DSN est configuré (`approvals_dsn()`, par défaut le même Postgres que le
    `Store`), `InMemoryApprovals` sinon (dev, CLI locale, tests). `is_authorized` réutilise EXACTEMENT
    la même liste que `build_invariants` (`GoAuthorizedSenders`, go_senders() de la config) — un GO
    textuel et une décision d'approbation obéissent à la même autorité."""
    go_senders = GoAuthorizedSenders(senders=config.go_senders())
    kwargs: dict[str, Any] = {
        "notifier": build_notifier(config),
        "is_authorized": go_senders.authorized,
        "telegram_bot_token": config.telegram_bot_token() or None,
        "webauthn_origin": config.webauthn_origin,
        "on_event": on_event,
    }
    dsn = config.approvals_dsn()
    if dsn:
        return PgApprovals(dsn=dsn, **kwargs)
    return InMemoryApprovals(**kwargs)


def build_hub(config: NokkerConfig, events: EventBus | None = None) -> BubbleHub:
    """Fils + bulles (docs/BUBBLES.md) : Postgres si un DSN est configuré (même base que le `Store`),
    mémoire sinon. `scrub` et `kernel` sont posés par `build_kernel` (dépendance circulaire outils ↔ hub)."""
    dsn = config.pg_dsn()
    if dsn:
        from nokker.bubbles.pg import PgBubbles
        from nokker.threads.pg import PgThreads

        threads, bubbles = PgThreads(dsn=dsn), PgBubbles(dsn=dsn)
    else:
        threads, bubbles = InMemoryThreads(), InMemoryBubbles()
    hub = BubbleHub(threads=threads, bubbles=bubbles, publish=events.publish if events is not None else None,
                    known_users=config.known_users())
    if config.previews_enabled:
        from nokker.bubbles.previews_hook import make_preview_requester
        from nokker.previews import get_default_service

        svc = get_default_service(config)  # même instance que les routes /v1/previews* (clé de signature)
        hub.producers.request_previews = make_preview_requester(hub, lambda urls, n: svc.preview_many(urls, limit=n))
    return hub


def build_agents(config: NokkerConfig, events: EventBus | None = None) -> AgentsHub:
    """Crew minimal (docs/BUBBLES.md §9.4) : Postgres si un DSN est configuré (même base que le
    `Store`), mémoire sinon — même pattern que `build_hub`. `agent.created` diffusé à toute identité
    vérifiée (pas de filtrage par membre, voir `AgentsHub`)."""
    dsn = config.pg_dsn()
    if dsn:
        from nokker.agents.pg import PgAgents

        store: Any = PgAgents(dsn=dsn)
    else:
        store = InMemoryAgents()
    return AgentsHub(store=store, publish=events.publish if events is not None else None)


def judge_transport(config: NokkerConfig, timeout_s: float = 30.0, known: Any = None) -> HttpJudgeTransport:
    """Transport du juge (`config.judge_config()` : `judge:` du YAML, sinon le palier Ship). Raisonnement coupé par
    `reasoning_off_body` du juge s'il est posé, sinon `reasoning_effort: "none"` (SEULE variante qui coupe vraiment le
    raisonnement sur le Router SOKKAN, cf `providers/sokkan.py`) ; un amont qui le refuse (gpt-oss, clé de
    l'utilisateur) est repris sans (`HttpJudgeTransport`, X8). `top_logprobs=1` (plafond mesuré du Router). `known` :
    coffre des invariants, pour le caviardage de tout ce que le juge envoie (motifs seuls sans lui)."""
    j = config.judge_config()
    extra = j.reasoning_off_body if j.reasoning_off_body is not None else {"reasoning_effort": "none"}
    return HttpJudgeTransport(base_url=j.base_url, model=j.model, api_key=j.api_key(), extra_body=dict(extra),
                              top_logprobs=1, timeout_s=timeout_s, known=known)


def build_judge(config: NokkerConfig, known: Any = None) -> OpenAICompatJudge:
    """B-2 : backend par défaut mesuré sur le banc B-1 (`nokker-judge-bench/README.md` § B-2 mesures)."""
    from nokker.qa.questions import JUDGE_SYSTEM

    return OpenAICompatJudge(transport=judge_transport(config, known=known), model_name=config.judge_config().model,
                             verdict_max_tokens=config.judge_verdict_max_tokens, system=JUDGE_SYSTEM)


def judge_tier(config: NokkerConfig) -> Tier:
    """Palier sous lequel les jetons du juge sont comptés : celui qui sert le même modèle, sinon Ship."""
    model = config.judge_config().model
    for name, t in config.tiers.items():
        if t.model == model:
            try:
                return Tier(name)
            except ValueError:
                continue
    return Tier.SHIP


def _warn_judge_without_key(config: NokkerConfig, flag: str) -> None:
    j = config.judge_config()
    if config.local:  # Lokkal : la clé vient du lien de compte (coffre local), posée après coup par `serve`
        return
    if j.api_key_env and not j.api_key():
        logger.warning("%s sans clé du juge (%s) : chaque appel échouera, carte sans avis et diff non relu",
                       flag, j.api_key_env)


def build_reviewer(config: NokkerConfig, known: Any = None) -> DiffReviewer | None:
    """X8 : relecture du diff avant « terminé » (`judge_review`), None si éteinte."""
    if not config.judge_review:
        return None
    _warn_judge_without_key(config, "judge_review")
    return DiffReviewer(transport=judge_transport(config, timeout_s=60.0, known=known),
                        model_name=config.judge_config().model,
                        rounds=max(0, int(config.judge_review_rounds)), max_tokens=int(config.judge_max_tokens),
                        tier=judge_tier(config))


def build_advisor(config: NokkerConfig, known: Any = None) -> CardAdvisor | None:
    """X8 : avis et niveau de risque sur les cartes fortes (`judge_advice`), None si éteint."""
    if not config.judge_advice:
        return None
    _warn_judge_without_key(config, "judge_advice")
    return CardAdvisor(transport=judge_transport(config, timeout_s=20.0, known=known),
                       model_name=config.judge_config().model,
                       max_tokens=max(160, int(config.judge_max_tokens) // 2), tier=judge_tier(config))


def build_grader(config: NokkerConfig, known: Any = None) -> Any:
    """Correcteur de la rubrique (docs/OUTCOMES-GOAL.md) : le transport du juge X8 (`HttpJudgeTransport`, caviardage
    compris) sur le palier `outcomes_grader` — `judge` (le juge : `judge:` ou Ship) ou un nom de palier de `tiers`."""
    from nokker.qa.outcomes import OutcomeGrader, check_grader_model

    name = str(config.outcomes_grader or "judge")
    if name == "judge":
        transport, model, tier = judge_transport(config, timeout_s=60.0, known=known), config.judge_config().model, \
            judge_tier(config)
    else:
        t = config.tiers.get(name)
        if t is None:
            raise ValueError(f"outcomes_grader : palier inconnu « {name} » (judge ou un palier de tiers)")
        extra = t.reasoning_off_body if t.reasoning_off_body is not None else {"reasoning_effort": "none"}
        transport = HttpJudgeTransport(base_url=t.base_url, model=t.model, api_key=t.api_key(),
                                       extra_body=dict(extra), top_logprobs=1, timeout_s=60.0, known=known)
        model = t.model
        try:
            tier = Tier(name)
        except ValueError:
            tier = Tier.SHIP
    check_grader_model(model)
    return OutcomeGrader(transport=transport, model_name=model, tier=tier, tier_name=name,
                         threshold=min(1.0, max(0.0, float(config.outcomes_threshold))),
                         max_passes=max(0, int(config.outcomes_max_passes)),
                         max_tokens=max(200, int(config.outcomes_max_tokens)))


def build_goals(config: NokkerConfig, known: Any = None) -> Any:
    """Rubriques et objectifs (docs/OUTCOMES-GOAL.md) : None si les deux drapeaux sont éteints (défaut)."""
    if not (config.outcomes_enabled or config.goals_enabled):
        return None
    from nokker.goals.service import GoalService
    from nokker.goals.store import MemoryGoals, PgGoals
    from nokker.store.conn import known_tenants

    dsn = config.pg_dsn()
    return GoalService(store=PgGoals(dsn) if dsn else MemoryGoals(), grader=build_grader(config, known),
                       outcomes_enabled=bool(config.outcomes_enabled), goals_enabled=bool(config.goals_enabled),
                       default_budget=dict(config.goals_default_budget or {}),
                       checkpoint_every=max(1, int(config.goals_checkpoint_every)),
                       checkpoint_s=float(config.goals_checkpoint_s),
                       tenants=(lambda: known_tenants(dsn)) if dsn else None)



def build_auto_reviewer(config: NokkerConfig, known: Any = None) -> Any:
    """Relecteur automatique des cartes `standard` (docs/APPROVALS.md §8, bloc `auto_review`), None si éteint. Palier
    `tier` : `judge` (transport du juge) ou un palier de `tiers` (raisonnement coupé comme le juge)."""
    from nokker.approvals.auto_review import AutoReviewer, Settings

    st = Settings.from_config(config.auto_review)
    if not st.enabled:
        return None
    if st.tier == "judge":
        _warn_judge_without_key(config, "auto_review")
        transport, model, tier = judge_transport(config, timeout_s=20.0, known=known), config.judge_config().model, \
            judge_tier(config)
    else:
        t = config.tiers.get(st.tier)
        if t is None:
            raise ValueError(f"auto_review.tier : palier inconnu {st.tier!r} (judge ou un palier de tiers)")
        extra = t.reasoning_off_body if t.reasoning_off_body is not None else {"reasoning_effort": "none"}
        transport = HttpJudgeTransport(base_url=t.base_url, model=t.model, api_key=t.api_key(), extra_body=dict(extra),
                                       top_logprobs=0, timeout_s=20.0, known=known)
        model = t.model
        try:
            tier = Tier(st.tier)
        except ValueError:
            tier = Tier.SHIP
    return AutoReviewer(transport=transport, model_name=model, tier=tier, policy=st.policy, max_tokens=st.max_tokens,
                        trajectory_calls=st.trajectory_calls, max_approvals_per_turn=st.max_approvals_per_turn,
                        tz=st.tz, own_addresses=st.own_addresses)


def build_policy(config: NokkerConfig, providers: dict) -> PolicyV0:
    """Politique de palier : v0 (défaut) ou v1, trois paliers (X8, `route.policy: v1`). `providers` est lu à chaque
    tour (un palier retiré à chaud, ex. modèle choisi à l'onboarding, se replie sans redémarrage)."""
    route = config.route or {}
    if str(route.get("policy", "v0")) != "v1":
        return PolicyV0()
    classifier = None
    small = config.tiers.get("small")
    if str(route.get("classifier", "rules")) == "model" and small is not None:
        extra = small.reasoning_off_body if small.reasoning_off_body is not None else {"reasoning_effort": "none"}
        classifier = ModelClassifier(
            transport=HttpJudgeTransport(base_url=small.base_url, model=small.model, api_key=small.api_key(),
                                         extra_body=dict(extra), top_logprobs=0, timeout_s=5.0),
            max_tokens=int(route.get("classifier_max_tokens", 1)), model_name=small.model)
    nina = route.get("nina") or {}
    nina_kinds = frozenset()
    if str(nina.get("mode", "off")) == "route":
        from nokker.route.classify import NINA_KINDS_DEFAULT, TURN_KINDS

        nina_kinds = frozenset(k for k in (nina.get("kinds") or NINA_KINDS_DEFAULT) if k in TURN_KINDS
                               and k not in ("code", "long_reasoning", "content"))
    return PolicyV1(available=lambda: tuple(providers), classifier=classifier,
                    vision=lambda: tuple(t for t, p in providers.items() if getattr(p, "vision", False) is True),
                    code_to_deep=bool(route.get("code_to_deep", True)),
                    deep_tool_failures=int(route.get("deep_tool_failures", 3)), nina_kinds=nina_kinds)


def build_gates(config: NokkerConfig, invariants: Invariants) -> GatesV0 | GatesV1:
    if not config.judge_enabled:
        return GatesV0(invariants=invariants)  # juge < 90 % au banc B-1 : invariants seuls (cf config.py)
    thresholds = load_thresholds_file(config.judge_thresholds_path) if config.judge_thresholds_path \
        else dict(DEFAULT_THRESHOLDS)
    return GatesV1(invariants=invariants, judge=build_judge(config, known=getattr(invariants, "known", None)),
                   thresholds=thresholds)


def build_context_manager(config: NokkerConfig, store: Any, hub: BubbleHub | None = None) -> ContextManager:
    """Never clear (docs/NEVER-CLEAR.md) : épisodes et réservoir en Postgres si un DSN est configuré (même base que
    le `Store`, tables de `migrations/002_context.sql`), en mémoire sinon (dev, tests)."""
    dsn = config.pg_dsn()
    if dsn:
        from nokker.context.pg import PgEpisodeStore, PgReservoirStore

        episodes: Any = PgEpisodeStore(dsn)
        reservoir: Any = PgReservoirStore(dsn)
    else:
        from nokker.context.memory import MemoryEpisodeStore, MemoryReservoirStore

        episodes, reservoir = MemoryEpisodeStore(), MemoryReservoirStore()
    known = set(ContextBudgets.__dataclass_fields__)
    budgets = ContextBudgets(**{k: v for k, v in config.context_budgets.items() if k in known})
    mgr = ContextManager(store=store, episodes=episodes, reservoir=reservoir, budgets=budgets)
    cx = config.context_corthexis or {}
    if cx.get("enabled"):
        from nokker.context.corthexis_index import DEFAULT_MIN_RERANK, DEFAULT_MIN_SCORE, CorthexisSemanticIndex

        db_env = cx.get("database_url_env", "CORTHEXIS_DATABASE_URL")
        missing = [n for n, v in ((db_env, os.environ.get(db_env, "")),
                                  ("CORTHEXIS_EMBED_URLS", cx.get("embed_urls") or os.environ.get("CORTHEXIS_EMBED_URLS", "")))
                   if not v]
        if missing:
            logger.warning("context_corthexis sans %s : épisodes non indexés, rappel lexical seulement", " ni ".join(missing))

        # Base CortHeXis sous locataire et RLS forcée (docs/MEMORY-TENANCY.md §5) : lignes d'avant rattachées au
        # locataire de leur fil ; instance à testeurs (`require_rls`) : une base sans RLS n'est pas servie.
        from nokker.context import corthexis_tenancy

        corthexis_tenancy.set_strict(config.require_rls)
        apply_corthexis_schema(config)
        cx_dsn = os.environ.get(db_env, "")
        if corthexis_tenancy.schema_mode() == corthexis_tenancy.EXTERNAL:
            # Base possédée par le rôle de migration (`nokker migrate`, CORTHEXIS_OWNER_DATABASE_URL) : le pod ne crée
            # ni ne modifie rien, il vérifie locataire, RLS forcée et propriété (docs/MEMORY-TENANCY.md §5.1).
            try:
                problems = corthexis_tenancy.verify(cx_dsn) if cx_dsn else []
            except Exception as e:  # noqa: BLE001 — base injoignable : même sort qu'une base non conforme
                from nokker.memory.corthexis import _no_credentials

                cause = _no_credentials(str(e)).splitlines()[0] if str(e) else ""
                problems = [f"base injoignable ({type(e).__name__}) {cause}"]
            if problems:
                (logger.error if config.require_rls else logger.warning)(
                    "context_corthexis (corthexis_schema: external) : base CortHeXis non conforme — %s%s",
                    " ; ".join(problems[:8]), " — index de session coupé (lexical)" if config.require_rls else "")
                if config.require_rls:
                    return _finish_manager(mgr, hub)
        elif cx_dsn and not corthexis_tenancy.ensure_managed(  # migrations du paquet AVANT (course de la rc.9.3)
                cx_dsn, corthexis_tenancy.project_tenants_from(dsn) if dsn else None) and config.require_rls:
            logger.error("context_corthexis : RLS non posée sur la base CortHeXis — index de session coupé (lexical)")
            return _finish_manager(mgr, hub)
        mgr.semantic = CorthexisSemanticIndex(
            cx.get("root_dir") or "/var/lib/nokker/session-memory",
            database_url=os.environ.get(cx.get("database_url_env", "CORTHEXIS_DATABASE_URL"), ""),
            embed_urls=cx.get("embed_urls", ""), profile=cx.get("profile", "leger"), rerank_url=cx.get("rerank_url", ""),
            min_score=float(cx.get("min_score", DEFAULT_MIN_SCORE)),
            min_rerank=float(cx.get("min_rerank", DEFAULT_MIN_RERANK)), chunk_target=int(cx.get("chunk_target", 300)))
    return _finish_manager(mgr, hub)


def _finish_manager(mgr: Any, hub: Any) -> Any:
    if hub is not None:
        mgr.on_episode_closed = hub.create_episode_bubble
        hub.context_manager = mgr
    return mgr


def apply_corthexis_schema(config: NokkerConfig) -> str:
    """`corthexis_schema` (managed | external, docs/MEMORY-TENANCY.md §5.1) : qui crée et migre la base CortHeXis. Une
    valeur inconnue arrête le démarrage (une faute de frappe ne rend pas au pod le droit de migrer)."""
    from nokker.context import corthexis_tenancy

    try:
        return corthexis_tenancy.set_mode(config.corthexis_schema)
    except ValueError as e:
        raise SystemExit(str(e)) from None


def check_rls_role(config: NokkerConfig) -> None:
    """`require_rls` : arrêt net si le rôle Postgres du noyau échappe à la RLS (docs/TENANCY.md §4)."""
    from nokker.store.conn import role_bypasses_rls

    dsn = config.pg_dsn()
    if config.require_rls and (not dsn or role_bypasses_rls(dsn)):
        raise SystemExit("require_rls : le rôle Postgres du noyau doit être nokker_app (ni superutilisateur ni "
                         "BYPASSRLS), sinon les testeurs se verraient entre eux — démarrage refusé")


def build_connector_store(config: NokkerConfig) -> Any:
    """Identifiants des connecteurs des testeurs, chiffrés (NOKKER_CONNECTOR_KEY) et sous RLS. Sans clé : pas de
    connecteurs sur cette instance (le canari), jamais de repli en clair."""
    from nokker.connectors.crypto import SecretBox
    from nokker.connectors.errors import ConnectorError
    from nokker.connectors.store import MemoryConnectorStore, PgConnectorStore

    try:
        box = SecretBox.from_env()
    except ConnectorError:
        logger.info("connecteurs éteints sur cette instance (clé de chiffrement absente ou illisible)")
        return None
    dsn = config.pg_dsn()
    if not dsn:
        return MemoryConnectorStore(box)
    from psycopg.rows import dict_row

    from nokker.store.conn import connect

    return PgConnectorStore(dsn, box, connect=lambda: connect(dsn, row_factory=dict_row))


def configure_cloud_link(config: NokkerConfig, tenancy: Tenancy, approvals: Any, ledger: Any,
                         store: Any = None, connector_store: Any = None, invariants: Any = None,
                         limits: Any = None) -> None:
    """nokker.ai, côté cloud du lien Lokkal (nokker/cloud/) : seulement si `cloud_link` est configuré ET la clé de
    signature présente ; sinon aucune route (la bêta d'avant ne change pas). Connecteurs relayés (lot X20) : en plus
    `cloud_link.relayed_connectors: true` et un magasin de connecteurs (`connector_store`).

    Inférence relayée (`/v1/inference/*`) : plafonds `limits` (ceux des tours du noyau, `kernel.limits`), modèles des
    paliers qui partagent le point d'accès de Deep (même URL, même clé), débit `cloud_link.inference_rate_max` par
    machine et `cloud_link.inference_calls_per_day` par compte."""
    cl = config.cloud_link or {}
    if not cl:
        cloud_http.configure(None)
        return
    import os

    from nokker.cloud.http import CloudLink
    from nokker.cloud.link import LinkService, Signer, SigningKeyError

    key_env = cl.get("signing_key_env") or "NOKKER_GATEWAY_SIGNING_KEY"
    pem = os.environ.get(key_env, "")
    if not pem:
        logger.warning("cloud_link configuré sans clé de signature : routes du lien Lokkal désactivées")
        cloud_http.configure(None, state="no_key")
        return
    try:
        signer = Signer(private_pem=pem.encode(), issuer=(cl.get("base_url") or "https://app.nokker.ai").rstrip("/"))
    except SigningKeyError as e:
        # Clé mal collée (PEM tronqué, guillemets, autre type de clé) : le noyau démarre quand même, `cloud_link` reste
        # inerte comme sans clé (aucune route, aucun outil de plus) et /healthz dit `gateway: invalid_key`. La cause
        # est dite, la valeur jamais (ni ici ni par une exception chaînée).
        logger.error("clé de passerelle illisible : lien Lokkal éteint (%s ; variable %s, valeur non affichée)",
                     e, key_env)
        cloud_http.configure(None, state="invalid_key")
        return
    deep = config.tiers.get("deep") or next(iter(config.tiers.values()))

    def identity(h: Any) -> str | None:
        auth = h.headers.get("Authorization", "")
        token = auth[7:].strip() if auth.startswith("Bearer ") else ""
        session = approvals.verify_session(token) if (token and approvals is not None) else None
        return session.user if session is not None and is_account(session.user) else None

    def tenant_of(account: str) -> str | None:
        acc = tenancy.account(account) if hasattr(tenancy, "account") else None
        return getattr(acc, "tenant_id", None)

    # Inférence relayée au ledger du noyau (fil `lokkal-<machine>`, un indice par tour, étiquette du foyer posée par
    # nokker/cloud/http.py) ; appelé dans tenant_scope(locataire de la machine) : le ledger Postgres est sous RLS.
    record_usage = cloud_http.ledger_usage_recorder(ledger, deep.model) if ledger is not None else None

    dsn = config.pg_dsn()
    from nokker.cloud.gateway_auth import GatewayAuth, MemoryGatewayAuthStore

    # `link_store`, pas `store` : `store` est le magasin du NOYAU (miroir) — le même nom écrasait l'un par l'autre et
    # /v1/mirror/erase appelait erase_mirror_copies sur le magasin des liens (trouvé le 24.10).
    from nokker.cloud.approvals import MemoryRelayStore, RelayService

    if dsn:
        from nokker.cloud.pg import PgGatewayAuthStore, PgLinkStore, PgRelayStore

        link_store: Any = PgLinkStore(dsn)
        auth_store: Any = PgGatewayAuthStore(dsn)
        relay_store: Any = PgRelayStore(dsn)
    else:
        from nokker.cloud.link import MemoryLinkStore

        link_store, auth_store, relay_store = MemoryLinkStore(), MemoryGatewayAuthStore(), MemoryRelayStore()
    links = LinkService(base_url=cl.get("base_url") or "https://app.nokker.ai",
                        gateway_domain=cl.get("gateway_domain") or "nokker.app", store=link_store)
    relay = None
    handoffs = mirror_notes = None
    if cl.get("handoffs", True):
        # Délégation web → Lokkal par besoin (X21, docs/LOKKAL.md §7.16) ; `cloud_link.handoffs: false` l'éteint.
        from nokker.cloud.handoff import HandoffService, MemoryHandoffStore
        from nokker.cloud.mirror import MemoryMirrorNotes

        if dsn:
            from nokker.cloud.pg import PgHandoffStore, PgMirrorNotes

            handoff_store: Any = PgHandoffStore(dsn)
            mirror_notes = PgMirrorNotes(dsn)
        else:
            handoff_store, mirror_notes = MemoryHandoffStore(), MemoryMirrorNotes()
        handoffs = HandoffService(links=links, signer=signer, store=handoff_store)
    mirror_pages = None
    if cl.get("mirror") and cl.get("mirror_pages", True):
        # Pages servies depuis le miroir, machine éteinte (§7.23, migration 033) : avec le miroir des abonnés ;
        # `cloud_link.mirror_pages: false` l'éteint. Le relais lit une copie avec SA clé (env, jamais dans la config).
        from nokker.cloud.mirror_pages import MemoryMirrorPages

        if dsn:
            from nokker.cloud.pg import PgMirrorPages

            mirror_pages = PgMirrorPages(dsn)
        else:
            mirror_pages = MemoryMirrorPages()
    # Pages publiques (§7.26) : drapeau `pages_public` (éteint par défaut) + comptes (`pages_public_accounts`, « * » =
    # tous) tant que l'abonnement et la vérification du compte ne sont pas lus. Registre des jti des jetons de pages
    # pour un relais à plusieurs répliques : toujours là (route fermée sans la clé du relais).
    pp_accounts = cl.get("pages_public_accounts", [])
    pages_public_allowed = (lambda acc: "*" in pp_accounts or acc in pp_accounts) if cl.get("pages_public") else None
    if dsn:
        from nokker.cloud.moderation import PgPublicPages
        from nokker.cloud.pg import PgUsedJti

        used_jti: Any = PgUsedJti(dsn)
        public_pages: Any = PgPublicPages(dsn)
    else:
        from nokker.cloud.mirror_pages import MemoryUsedJti
        from nokker.cloud.moderation import MemoryPublicPages

        used_jti, public_pages = MemoryUsedJti(), MemoryPublicPages()
    if cl.get("relayed_connectors") and connector_store is not None:
        from nokker.cloud.relay import ConnectorRelay, MemoryRelayAudit, PgRelayAudit

        def record_relay(tenant: str, machine: str, entry: dict) -> None:
            # Compté au ledger du locataire, à côté de l'inférence de la machine (fil `lokkal-<machine>`).
            if ledger is None:
                return
            refused = entry.get("outcome") == "refused"
            ledger.record_verdict(scoped_thread_id(tenant, f"lokkal-{machine}"), 0, Verdict(
                gate=Gate.ACTION, decision=Decision.DENY if refused else Decision.ALLOW, layer=1,
                rule=f"relay:{entry.get('connector')}:{entry.get('tool')}",
                reason=str(entry.get("code") or entry.get("outcome") or ""), subject=entry.get("audit_id")))

        # Envoi externe et destructif (X20b) : carte forte vérifiée ICI contre les appareils du compte de la machine,
        # avec la routine de la machine (`verify_signed_decision`) et le défi que la machine a déposé (boîte aux lettres
        # X10). Sans backend d'approbations, aucun de ces outils ne passe.
        relay = ConnectorRelay(store=connector_store, audit=PgRelayAudit(dsn) if dsn else MemoryRelayAudit(),
                               redact=invariants.redact_result if invariants is not None else None,
                               record=record_relay, verifier=approvals,
                               devices_of=(lambda acc: approvals.list_devices(acc)) if approvals is not None else None,
                               cards=relay_store, origin=links.base_url)
    mirror_allowed = (lambda acc: "*" in cl.get("mirror_accounts", []) or acc in cl.get("mirror_accounts", [])) \
        if cl.get("mirror") else None
    crew = None
    relay_service = RelayService(store=relay_store, known=getattr(invariants, "known", None))
    if cl.get("crew", False):  # opt-in explicite : la bêta ne lance aucun ordonnanceur sans décision (arbitrage dev 25.10)
        # Deck multi-machines et agents placés (X15, docs/LOKKAL.md §7.19) ; `cloud_link.crew: true` l'allume (éteint par défaut). Agents
        # « web » : drapeau d'offre `web_agents` + comptes (`web_agents_accounts`, « * » = tous) tant que l'abonnement
        # n'est pas lu, et moteur NOKKER_CREW_ENGINE=1.
        from nokker.cloud.crew import CloudCrew, MemoryDeckStore

        web = cl.get("web_agents_accounts", [])
        if dsn:
            from nokker.cloud.pg import PgDeckStore

            deck_store: Any = PgDeckStore(dsn)
        else:
            deck_store = MemoryDeckStore()
        crew = CloudCrew(links=links, signer=signer, deck_store=deck_store,
                         web_allowed=lambda acc: bool(cl.get("web_agents")) and ("*" in web or acc in web),
                         web_engine=crew_engine_enabled(config), titles_allowed=mirror_allowed,
                         approvals_relay=relay_service,
                         agent_tenant=lambda a: tenant_of(a.created_by) if is_account(a.created_by) else None)
    relay_key_env = str(cl.get("relay_key_env") or "NOKKER_RELAY_CORE_KEY")
    relay_key = os.environ.get(relay_key_env, "")
    if 0 < len(relay_key) < 32:  # une clé tronquée dans le Secret se lirait sinon comme « miroir des pages éteint »
        logger.warning("%s fait %d caractères (32 au moins) : pages du miroir servies machine éteinte DÉSACTIVÉES",
                       relay_key_env, len(relay_key))
    # Décision d'une carte machine par la Mini App Telegram : nokker.ai vérifie la preuve (initData du bot, appareil du
    # compte, signature) et signe pour la machine une attestation (nokker/cloud/attest.py). Sans jeton de bot ou sans
    # backend d'approbations, cette voie est fermée (la page web et la passkey restent).
    attestor = None
    bot_token = config.telegram_bot_token()
    if bot_token and approvals is not None:
        from nokker.cloud.attest import TelegramAttestor

        def account_of_telegram(tg_user_id: str) -> str | None:
            acc = tenancy.account_for_identity("telegram", tg_user_id) if tg_user_id else None
            return acc.account_id if acc is not None else None

        attestor = TelegramAttestor(signer=signer, bot_token=bot_token, account_of=account_of_telegram,
                                    devices_of=lambda acc: approvals.list_devices(acc),
                                    max_age_s=approvals.telegram_max_age_s)
    cloud_http.configure(CloudLink(
        relay=relay, handoffs=handoffs, mirror_notes=mirror_notes, crew=crew,
        mirror_pages=mirror_pages, mirror_pages_quota=int(float(cl.get("mirror_pages_quota_mb", 50)) * 1024 * 1024),
        pages_public_allowed=pages_public_allowed, used_jti=used_jti,
        public_pages=public_pages,  # modération des pages publiques (§7.26 point 6) : demandé par le relais
        relay_key=relay_key,
        links=links, signer=signer, identity=identity, tenant_of=tenant_of,
        gateway_auth=GatewayAuth(links=links, signer=signer, store=auth_store),
        # Approbations fortes par la passerelle (lot X10) : boîte aux lettres ; appareils du compte = ceux du backend
        # d'approbations (mêmes enrôlements que le web), dont la machine vérifie elle-même les preuves.
        # X20c : le résumé d'une carte relayée est caviardé avec les secrets connus du noyau de nokker.ai.
        approvals_relay=relay_service,
        devices_of=(lambda acc: approvals.list_devices(acc)) if approvals is not None else None,
        attestor=attestor,
        router_base=deep.base_url, router_key=deep.api_key, record_usage=record_usage, store=store,
        # Inférence relayée : modèles des paliers servis par le même point d'accès que Deep, plafonds du noyau.
        models=frozenset(t.model for t in config.tiers.values() if t.base_url == deep.base_url and t.model),
        default_model=deep.model, limits=limits,
        inference_rate_max=int(cl.get("inference_rate_max", 60)),
        inference_calls_per_day=int(cl.get("inference_calls_per_day", 2000)),
        # Connexion par la passerelle : `token|refresh|revoke` par minute et par IP (celle du relais, §7.3).
        gateway_rate_max=int(cl.get("gateway_rate_max", cloud_http.GATEWAY_RATE_MAX)),
        # Miroir (L5, abonnés) : drapeau `mirror` + liste de comptes (« * » = tous) tant que l'abonnement n'est pas lu.
        mirror_allowed=mirror_allowed))


def configure_team(config: NokkerConfig, tenancy: Tenancy, agents: Any = None, scrub: Any = None) -> None:
    """Équipe (lot X17, nokker/team/) : seulement drapeau `team` allumé ; sinon aucune route, rien ne change. Le miroir
    des notes de projet suit la même règle d'abonnement que celui des tours (`cloud_link.mirror`)."""
    if not config.team:
        team_http.configure(None)
        return
    from nokker.team.service import Team
    from nokker.team.store import MemoryTeamStore, PgTeamStore

    dsn = config.pg_dsn()
    cloud = cloud_http._cloud
    if config.household:  # foyer (X23) : le magasin de l'équipe étendu (mêmes tables, mêmes transactions, + 039)
        from nokker.household.store import MemoryHouseholdStore, PgHouseholdStore

        store: Any = PgHouseholdStore(dsn) if dsn else MemoryHouseholdStore()
    else:
        store = PgTeamStore(dsn) if dsn else MemoryTeamStore()
    team_http.configure(Team(store=store, tenancy=tenancy, agents=agents,
                             mirror_allowed=cloud.mirror_allowed if cloud is not None else None, scrub=scrub))


def configure_team_budget(config: NokkerConfig, kernel: Kernel, agents: Any = None) -> None:
    """Budget des projets d'équipe (docs/LOKKAL.md §7.15) : décompté sur le ledger du noyau (`fetch_project_calls`).
    Seulement avec l'équipe allumée et un ledger qui range le projet d'un appel ; sinon rangé, non décompté."""
    team = team_http.service()
    ledger = getattr(kernel, "ledger", None)
    if team is None or not hasattr(ledger, "fetch_project_calls"):
        kernel.project_budget = None
        if team is not None:
            team.budget = None
        return
    from nokker.team.budget import ProjectBudget, thread_project_resolver

    rates = dict((config.team_settings or {}).get("budget_rates") or {})
    budget = ProjectBudget(team=team, ledger=ledger, prices=getattr(ledger, "prices", None),
                           resolve=thread_project_resolver(agents=agents, plan=plan_http.service()),
                           rates={str(k).upper(): float(v) for k, v in rates.items()} or None)
    team.budget = budget
    kernel.project_budget = budget


def configure_household(config: NokkerConfig, kernel: Kernel, tenancy: Tenancy, approvals: Any) -> None:
    """Foyer (lot X23, nokker/household/, docs/LOKKAL.md §7.29) : drapeau `household` ET équipe allumée. Sinon aucune
    route, rien ne change. Branche : le quota commun (ledger du noyau, `fetch_household_calls`), le rappel des notes du
    foyer dans les tours des membres, le palier du payeur (facturation allumée : `Household.attach_billing`, appelée
    après `configure_billing`), et pour les profils mineurs (`household_minors`) les portes (carte forte du
    représentant), le registre (liste blanche), le préfixe adapté, le décideur des cartes, les refus aux frontières
    (consentement d'entraînement, SSO, miroir). Les cartes des mineurs passent par `make_handler` (GuardianCards)."""
    team = team_http.service()
    if not config.household or team is None or not hasattr(team.store, "hh_membership"):
        if config.household:
            logger.warning("drapeau household sans équipe (team) : foyer éteint")
        household_http.configure(None)
        kernel.household = None
        return
    from nokker.household.boundaries import no_minor_mirror, no_minor_public_pages
    from nokker.household.guard import MinorGates, TenantMinorCache, minor_tool_filter
    from nokker.household.memory import HouseholdRecall
    from nokker.household.model import Settings
    from nokker.household.quota import HouseholdQuota
    from nokker.household.service import Household

    settings = Settings.from_config(config.household_settings, billing_plans=getattr(config, "billing_plans", None),
                                    minors=config.household_minors,
                                    minors_mode=config.household_minors_mode)
    ob = onboarding_http._onboarding
    house = Household(team=team, settings=settings, consents=getattr(ob, "consents", None) if ob else None)
    billing = billing_http.service()
    if billing is not None:  # quota et sièges du palier ACTIF du payeur ; hors palier de foyer : foyer suspendu
        house.attach_billing(billing)
    profiles = TenantMinorCache(house)

    def changed(tenant_id: str) -> None:
        profiles.forget(tenant_id)
        forget = getattr(kernel.tools, "forget", None)
        if forget is not None:
            forget(tenant_id)

    house.on_change = changed
    ledger = getattr(kernel, "ledger", None)
    if hasattr(ledger, "fetch_household_calls"):
        house.quota = HouseholdQuota(household=house, ledger=ledger)
        kernel.household = house.quota
        if cloud_http._cloud is not None:  # inférence relayée d'une machine liée : même quota commun (§7.29.3)
            cloud_http._cloud.household = house.quota
    else:
        logger.warning("foyer : ledger sans fetch_household_calls, quota commun non décompté")
        kernel.household = None
    if hasattr(kernel.memory, "tenant_memory"):
        kernel.memory.tenant_memory = HouseholdRecall(household=house).for_tenant
    # Profils mineurs : les garde-fous sont posés dès que le foyer est allumé ; sans `household_minors`, aucun profil
    # ne peut être déclaré, et un profil déjà déclaré est suspendu (Household.minor_state).
    kernel.gates = MinorGates(inner=kernel.gates, minor_state=house.minor_state)
    if code_http.service() is not None:  # un profil mineur n'ouvre pas de session de code (§7.28)
        code_http.service().minor = house.is_minor
    if hasattr(kernel.tools, "restrict"):
        kernel.tools.restrict = minor_tool_filter(house)
    if hasattr(kernel.context, "young_profile"):
        kernel.context.young_profile = profiles
    if approvals is not None:
        approvals.guardian_of = house.guardian_for_tenant
    onboarding_http.configure_minors(house.is_minor)
    sso = sso_http.service()
    if sso is not None:
        sso.is_minor = house.is_minor
    team.mirror_allowed = no_minor_mirror(team.mirror_allowed, house.is_minor)
    cloud = cloud_http._cloud
    if cloud is not None and getattr(cloud, "mirror_allowed", None) is not None:
        cloud.mirror_allowed = no_minor_mirror(cloud.mirror_allowed, house.is_minor)
    if cloud is not None and getattr(cloud, "pages_public_allowed", None) is not None:  # §7.26 : pas de page publique
        cloud.pages_public_allowed = no_minor_public_pages(cloud.pages_public_allowed, house.is_minor)
    household_http.configure(house)


def configure_ambient(config: NokkerConfig, kernel: Any) -> None:
    """Ambiant du foyer (docs/AMBIENT.md, nokker/ambient/) : bloc `ambient`, éteint par défaut (aucune route, aucun
    fil). La passerelle du foyer n'est jointe que si NOKKER_HOME_BRIDGE_URL et NOKKER_HOME_BRIDGE_TOKEN sont posés
    (Lokkal, profil compose `home`) ; la bêta cloud n'en a jamais."""
    raw = getattr(config, "ambient", None) or {}
    if speak_lexicon_mod.service() is None:  # Lokkal sans Telegram : le lexique se monte aussi ici
        speak_lexicon_mod.setup(getattr(config, "speak_lexicon", None), config.pg_dsn() or None)
    if not any((raw.get(k) or {}).get("enabled") for k in ("room_audio", "eink", "glance")):
        ambient_http.configure(None)
        return
    from nokker.ambient.bridge import HomeBridge
    from nokker.ambient.wiring import build

    def household_name(tenant_id: str, household_id: str) -> str:
        return ""  # le nom du foyer n'est pas affiché (il peut porter un nom de famille)

    ambient_http.configure(build(raw, scrub=kernel.invariants.scrub_output, tts_raw=config.tts,
                                 dsn=config.pg_dsn() or None, bridge=HomeBridge.from_env(),
                                 household_name=household_name))


def configure_household_paper(config: NokkerConfig, kernel: Kernel, tenancy: Tenancy) -> None:
    """Papiers du foyer (docs/HOUSEHOLD-PAPER.md, nokker/household/paper/) : drapeau `household_paper`, nokker.ai
    seulement (comptes `us_…`). Éteint (défaut) : aucune route /v1/paper*, aucun outil `paper_*`, portes inchangées.
    Allumé : outils des comptes adultes (`available_for`), portes des cartes (relecture d'agenda, partage, paiement
    FORT) autour des portes du noyau, routes du compte. Le partage entre membres suit le foyer s'il est allumé."""
    if not config.household_paper or not hasattr(kernel.tools, "tester_sources"):
        paper_http.configure(None)
        return
    from nokker.household.paper.gates import PaperGates
    from nokker.household.paper.service import Paper
    from nokker.household.paper.store import MemoryPaperStore, PgPaperStore
    from nokker.household.paper.tools import PaperTools, tenant_ok

    def account_of(tid: str) -> str | None:
        acc = tenancy.account_for_tenant(tid)
        return acc.account_id if acc is not None and acc.status == "active" else None

    house = household_http.service()

    def members(account_id: str) -> list[tuple[str, str, str]] | None:
        if house is None:
            return None
        found = house.membership(account_id)
        if found is None:
            return None
        acc, p, _ = found
        out = []
        for row in house.store.hh_members(acc, p.id):
            other = tenancy.account(row.account_id)
            if other is not None and row.status == "active" and other.status == "active":
                out.append((other.account_id, other.email, other.tenant_id))
        return out

    connectors = getattr(kernel.tools, "connectors", None)

    def calendar_for(tid: str) -> Any:
        for src in (connectors.sources_for(tid) if connectors is not None else []):
            if any(spec.name == "calendar_create" for spec in src.specs()):
                return src
        return None

    dsn = config.pg_dsn()
    svc = Paper(store=PgPaperStore(dsn) if dsn else MemoryPaperStore(), account_of=account_of,
                household_members=members, calendar_for=calendar_for)
    kernel.tools.tester_sources.append(PaperTools(service=svc, allowed=tenant_ok(
        tenancy.account_for_tenant, house.is_minor if house is not None else None)))
    kernel.tools.refresh()
    kernel.gates = PaperGates(inner=kernel.gates, service=svc)
    paper_http.configure(svc)


def configure_family_calendar(config: NokkerConfig, kernel: Kernel, tenancy: Tenancy) -> None:
    """Calendrier familial (docs/FAMILY-CALENDAR.md, nokker/household/famcal/) : drapeau `family_calendar`, nokker.ai
    seulement (comptes `us_…`). Éteint (défaut) : aucune route /v1/family-calendar*, aucun outil `family_calendar_*`,
    portes inchangées. Allumé : outils des comptes adultes (`available_for`), porte de la carte de relecture autour des
    portes du noyau, routes du compte. L'agenda lu et écrit est toujours celui du locataire du tour."""
    if not config.family_calendar or not hasattr(kernel.tools, "tester_sources"):
        famcal_http.configure(None)
        return
    from nokker.household.famcal.gates import FamilyCalendarGates
    from nokker.household.famcal.service import FamilyCalendar
    from nokker.household.famcal.store import MemoryFamcalStore, PgFamcalStore
    from nokker.household.famcal.tools import FamilyCalendarTools
    from nokker.household.paper.tools import tenant_ok

    def account_of(tid: str) -> str | None:
        acc = tenancy.account_for_tenant(tid)
        return acc.account_id if acc is not None and acc.status == "active" else None

    connectors = getattr(kernel.tools, "connectors", None)

    def calendars_for(tid: str) -> list[Any]:
        return [src for src in (connectors.sources_for(tid) if connectors is not None else [])
                if any(spec.name in ("calendar_events", "calendar_create") for spec in src.specs())]

    house = household_http.service()
    dsn = config.pg_dsn()
    svc = FamilyCalendar(store=PgFamcalStore(dsn) if dsn else MemoryFamcalStore(), account_of=account_of,
                         calendars_for=calendars_for, paper=paper_http.service())
    kernel.tools.tester_sources.append(FamilyCalendarTools(service=svc, allowed=tenant_ok(
        tenancy.account_for_tenant, house.is_minor if house is not None else None)))
    # Une seule voie vers l'agenda pour un document : `paper_calendar_add` sort du catalogue des Papiers (s'ils sont
    # allumés) ; `family_calendar_*` couvre aussi la date isolée d'un papier (docs/FAMILY-CALENDAR.md §2).
    from nokker.household.paper.tools import PaperTools

    for src in kernel.tools.tester_sources:
        if isinstance(src, PaperTools):
            src.hidden = src.hidden | {"paper_calendar_add"}
    kernel.tools.refresh()
    kernel.gates = FamilyCalendarGates(inner=kernel.gates, service=svc)
    famcal_http.configure(svc)


def configure_payments(config: NokkerConfig, kernel: Kernel, tenancy: Tenancy, approvals: Any,
                       events: Any = None) -> None:
    """Paiements « l'agent prépare, l'humain paie » (docs/PAYMENTS.md, nokker/payments/) : drapeau `payments`, éteint
    par défaut (aucun outil, aucune porte, aucune route, défis d'approbation inchangés). Allumé : outil `pay_request`
    des comptes, portes d'arrêt au paiement et de budget (APRÈS le foyer : elles enveloppent celles des mineurs), veille
    des pages de paiement sur le navigateur du pool, mandats scellés par la passkey (crochet des approbations), routes
    /v1/payments*. Demande les approbations et `webauthn_origin` (sans origine, aucun mandat ne se signe)."""
    if not config.payments or approvals is None:
        if config.payments:
            logger.warning("drapeau payments sans approbations : paiements éteints")
        payments_http.configure(None)
        return
    from nokker.payments.gates import PaymentGates
    from nokker.payments.service import HouseholdPeople, Payments, Settings, SoloPeople
    from nokker.payments.store import MemoryPaymentStore, PgPaymentStore
    from nokker.payments.tools import CheckoutWatch, PayTools
    from nokker.tools.browser_pool import BrowserPoolSource

    if not config.webauthn_origin:
        logger.warning("paiements : webauthn_origin absent, aucune carte « à payer » ne pourra s'approuver")
    dsn = config.pg_dsn()
    house = household_http.service()
    svc = Payments(store=PgPaymentStore(dsn) if dsn else MemoryPaymentStore(),
                   settings=Settings.from_config(config.payments_settings),
                   people=HouseholdPeople(household=house, tenancy=tenancy) if house is not None else SoloPeople(),
                   publish=events.publish if events is not None else None)
    approvals.payment_mandates = svc
    kernel.gates = PaymentGates(inner=kernel.gates, payments=svc)
    if hasattr(kernel.tools, "tester_sources"):
        srcs = kernel.tools.tester_sources
        for i, src in enumerate(srcs):
            if isinstance(src, BrowserPoolSource):
                srcs[i] = CheckoutWatch(inner=src, payments=svc)
        srcs.append(PayTools(payments=svc))
        kernel.tools.refresh()

    def device_key(device_id: str) -> str | None:
        dev = approvals._load_device(device_id)
        return dev.public_key_spki if dev is not None and dev.status == "approved" else None

    payments_http.configure(svc, device_key=device_key, origin=config.webauthn_origin)


def house_room_on(config: NokkerConfig) -> bool:
    """Salle Maison réellement allumée : son drapeau ET le foyer (`configure_house_room` l'éteint sans foyer)."""
    return bool(getattr(config, "house_room", False) and getattr(config, "household", False))


def register_house_providers(*, home: Any = None, home_watch: Any = None) -> None:
    """Fournisseurs d'outils des agents de la salle Maison (nokker/house/providers.py, docs/HOUSE-ROOM.md §2.2) : une
    ligne par chantier de la maison fusionné. Rien n'est lu tant que `house_room` est éteint (`providers_for` n'est
    appelé que par `configure_house_room`). Repart de zéro à chaque démarrage (aucun fournisseur d'une instance
    précédente). Non branchés au train 0311 : `courrier` (les outils `paper_*` servent les papiers PERSONNELS d'un
    compte, la salle n'admet que du contenu commun au foyer : il faut une vue « papiers partagés au foyer ») ;
    `memoire` (la consolidation n'a pas d'outil : la salle sert déjà `memoire_foyer` et reçoit `memory.report`) ;
    `agenda` (aucun fournisseur)."""
    from nokker.house import providers

    providers.clear()
    providers.register("maison", lambda _config, _kernel: home)  # p2/home-assistant (Lokkal : passerelle home-bridge)
    providers.register("veille", lambda _config, _kernel: home_watch)  # p2/home-watch (Lokkal : passerelle home-bridge)


def configure_house_room(config: NokkerConfig, kernel: Kernel, events: Any = None) -> Any:
    """Salle Maison (chantier p2/house-room, nokker/house/, docs/HOUSE-ROOM.md) : drapeau `house_room` ET foyer allumé.
    Sinon aucune route /v1/house*, aucun outil `ask_house`, rien ne change. Branche : le journal de la salle (Postgres
    sous RLS, migration 057, ou mémoire), les fournisseurs d'outils des agents (`nokker.house.providers`), le rappel des
    notes du foyer (agent Mémoire), `ask_house` dans le registre des membres d'un foyer, et la publication
    `house.message` / `house.agent`. La suite d'une carte posée dans la salle est branchée dans `make_handler`."""
    house = household_http.service()
    if not config.house_room or house is None:
        if config.house_room:
            logger.warning("drapeau house_room sans foyer (household) : salle Maison éteinte")
        house_http.configure(None)
        return None
    from nokker.house.providers import providers_for
    from nokker.house.room import AskHouseSource, HouseRoom, background_spawn
    from nokker.house.roster import Settings as RoomSettings
    from nokker.house.store import MemoryRoomStore, PgRoomStore
    from nokker.household.memory import HouseholdRecall

    dsn = config.pg_dsn()
    store: Any = PgRoomStore(dsn, migrate_now=False) if dsn else MemoryRoomStore()
    recall = HouseholdRecall(household=house)
    room = HouseRoom(household=house, kernel=kernel, store=store,
                     settings=RoomSettings.from_config(config.house_room_settings),
                     providers=providers_for(config, kernel), recall=recall.recall_for_account,
                     publish=events.publish if events is not None else None, spawn=background_spawn)
    if hasattr(kernel.tools, "tester_sources"):
        kernel.tools.tester_sources.append(AskHouseSource(room))
        kernel.tools.refresh()
    house_http.configure(room)
    return room


def configure_presence(config: NokkerConfig, kernel: Any, *, events: Any = None, hub: Any = None, approvals: Any = None,
                       agents: Any = None, room: Any = None) -> Any:
    """Vue « Qui fait quoi » (nokker/presence/, docs/WHO-DOES-WHAT.md) : drapeau `who_does_what`. Éteint : aucune route
    /v1/presence, aucun observateur sur le bus. Allumé : lecture seule de l'état des fils, cartes, objectifs, sessions de
    code, agents Crew et salle Maison ; `presence.changed` sur le bus. Rien n'est ajouté au registre d'outils."""
    if not config.who_does_what:
        presence_http.configure(None)
        return None
    from nokker.presence.service import PresenceService
    from nokker.presence.tracker import ActivityTracker

    tracker = ActivityTracker(audience_of=hub.thread_audience if hub is not None else None)
    if events is not None:
        tracker.publish = events.publish
        events.observers[:] = [o for o in events.observers if not getattr(o, "presence", False)]

        def observe(event_type: str, data: dict) -> None:
            tracker.observe(event_type, data)

        observe.presence = True  # type: ignore[attr-defined]
        events.observers.append(observe)
    svc = PresenceService(hub=hub, approvals=approvals, goals=getattr(kernel, "goals", None),
                          code=code_http.service(), agents=agents, room=room, tracker=tracker)
    presence_http.configure(svc)
    return svc


def configure_guide(config: NokkerConfig, kernel: Any, *, events: Any = None, tenancy: Any = None) -> Any:
    """Guide (nokker/guide/, docs/GUIDE.md) : drapeau `guide`. Éteint : aucune route /v1/guide, aucun observateur sur
    le bus, rien de vu par le modèle. Allumé : statuts des fonctions pour le compte, compteurs d'usage (par code),
    questions au palier `guide_settings.tier` (défaut `small`, sinon Ship). Rien n'est ajouté au registre d'outils."""
    if not config.guide:
        guide_http.configure(None)
        return None
    from nokker.core.turn import LLMCall, Tier, Usage
    from nokker.guide.service import AccountFacts, GuideService, prefix_sha
    from nokker.guide.store import MemoryUsage, PgUsage
    from nokker.guide.texts import GUIDE_SYSTEM

    st = dict(config.guide_settings or {})
    dsn = config.pg_dsn()
    tier_name = str(st.get("tier") or "small")
    t = config.tiers.get(tier_name) or config.tiers.get("ship") or next(iter(config.tiers.values()), None)
    if tier_name not in config.tiers:
        tier_name = "ship" if "ship" in config.tiers else next(iter(config.tiers), "ship")
    transport = None
    if t is not None:
        extra = t.reasoning_off_body if t.reasoning_off_body is not None else {"reasoning_effort": "none"}
        transport = HttpJudgeTransport(base_url=t.base_url, model=t.model, api_key=t.api_key(), extra_body=dict(extra),
                                       top_logprobs=0, timeout_s=30.0,
                                       known=getattr(getattr(kernel, "invariants", None), "known", None))
    try:
        tier = Tier(tier_name)
    except ValueError:
        tier = Tier.SHIP

    def facts_of(sender: str | None) -> AccountFacts:
        if not is_account(sender):
            return AccountFacts()
        house = household_http.service()
        member = minor = False
        if house is not None:
            try:
                member = house.membership(sender) is not None
                minor = house.minor_state(sender) is not None
            except Exception:  # noqa: BLE001 — un foyer illisible : profil seul
                logger.warning("guide : foyer illisible", exc_info=True)
        telegram = False
        if tenancy is not None:
            try:
                telegram = bool(tenancy.identities_of(str(sender), "telegram"))
            except Exception:  # noqa: BLE001
                telegram = False
        return AccountFacts(household=member, minor=minor, telegram=telegram)

    seq = {"n": 0}

    def on_call(tenant: str, model: str, usage: dict, latency: float) -> None:
        ledger = getattr(kernel, "ledger", None)
        if ledger is None:
            return
        seq["n"] += 1
        u = Usage(prompt=int(usage.get("prompt_tokens") or 0), completion=int(usage.get("completion_tokens") or 0))
        call = LLMCall(tier=tier, model=model, reasoning=False, prefix_sha256=prefix_sha(GUIDE_SYSTEM), layer_tokens={},
                       usage=u, latency_s=latency, route_reason="guide : réponse du Guide")
        ledger.record(scoped_thread_id(tenant, "guide"), seq["n"], call)

    inv = getattr(kernel, "invariants", None)
    svc = GuideService(config=config, usage=PgUsage(dsn) if dsn else MemoryUsage(), facts_of=facts_of,
                       transport=transport, model_name=t.model if t is not None else "",
                       max_tokens=max(100, int(st.get("max_tokens", 400))),
                       max_asks_per_day=max(1, int(st.get("max_asks_per_day", 40))),
                       scrub_in=(lambda s: inv.check_input(s).text) if inv is not None else (lambda s: s),
                       scrub_out=inv.scrub_output if inv is not None else (lambda s: s), on_call=on_call)
    if events is not None:
        events.observers[:] = [o for o in events.observers if not getattr(o, "guide", False)]

        def observe(event_type: str, data: dict) -> None:
            svc.observe(event_type, data)

        observe.guide = True  # type: ignore[attr-defined]
        events.observers.append(observe)
    guide_http.configure(svc)
    return svc


def configure_packs(config: NokkerConfig, kernel: Any, hub: Any, *, local: bool) -> None:
    """Packs d'agents chez nokker.ai (nokker/webpacks/, docs/LOKKAL.md §7.30) : drapeau `packs_web`, jamais dans Lokkal
    (qui a ses outils de packs locaux, lot X16). Allumé : routes /v1/packs* (session du compte) et outils `pack_*`,
    `skill_list`, `skill_view` du modèle des testeurs ; relais X21 si le lien Lokkal est configuré."""
    if not config.packs_web or local:
        packs_http.configure(None)
        return
    from nokker.webpacks.pg import PgPackStore
    from nokker.webpacks.service import MemoryPackStore, WebPacks
    from nokker.webpacks.tools import WebPackTools

    dsn = config.pg_dsn()
    cloud = cloud_http._cloud
    svc = WebPacks(store=PgPackStore(dsn) if dsn else MemoryPackStore(), hub=hub,
                   links=cloud.links if cloud is not None else None,
                   handoffs=cloud.handoffs if cloud is not None else None)
    packs_http.configure(svc)
    if hasattr(kernel.tools, "tester_sources"):
        kernel.tools.tester_sources.append(WebPackTools(service=svc))
        kernel.tools.refresh()


def configure_learning(config: NokkerConfig, kernel: Any, events: Any, *, local_api: Any = None,
                       tenancy: Any = None) -> None:
    """Apprentissage sous contrôle humain (docs/LEARNING.md) — drapeaux éteints par défaut : aucune route, aucun outil,
    aucune ligne de plus. Lokkal : `local.learning|learning_nudges|memory_pending` (magasin en fichiers, outils posés
    par `LocalApi.tool_sources`) ; nokker.ai : `learning|learning_nudges|memory_pending` de la config (magasin sous RLS,
    outils des comptes `us_…`). Les portes de la carte obligatoire enveloppent TOUTES les autres : appelé en dernier."""
    from nokker.learning import wiring

    emit = events.publish if events is not None else None
    if local_api is not None:
        lrn = local_api.learning
        learning_http.configure(lrn)
        wiring.install(kernel, lrn, account_of=lambda _ctx: wiring.LOCAL_ACCOUNT, nudger=wiring.nudger_from(config.local),
                       emit=emit)
        return
    flags = {"learning": config.learning, "learning_nudges": config.learning_nudges,
             "memory_pending": config.memory_pending, **dict(config.learning_settings or {})}
    account_for_tenant = getattr(tenancy, "account_for_tenant", None)
    account_of = wiring.cloud_account_of(account_for_tenant)
    lrn = wiring.cloud_learning(config, account_for_tenant)
    learning_http.configure(lrn)
    if lrn is not None and lrn.skills is not None and hasattr(kernel.tools, "tester_sources"):
        from nokker.learning.skills import LearningToolSource

        packs = packs_http.service()
        if packs is not None:  # skill_list / skill_view des packs lisent aussi les skills appris du compte
            packs.learned = lrn.skills
            lrn.skills.taken = lambda account, name: any(
                e.get("name") == name for e in packs.list_skills(account).get("skills", []))
        kernel.tools.tester_sources.append(LearningToolSource(service=lrn.skills, account_of=account_of,
                                                              catalog=packs is None))
        kernel.tools.refresh()
    wiring.install(kernel, lrn, account_of=account_of, nudger=wiring.nudger_from(flags, sender_prefix="us_"),
                   emit=emit)


def configure_long_memory(config: NokkerConfig, kernel: Any, tenancy: Any) -> None:
    """Mémoire longue multi-locataire (docs/MEMORY-TENANCY.md) — drapeau `tenant_long_memory`, éteint par défaut :
    aucune route, rien de rappelé de plus. nokker.ai seulement, APRÈS le foyer et l'apprentissage : le port mémoire
    des testeurs devient profil + foyer (les notes du foyer de l'app y restent), l'approbation d'une proposition au
    foyer (quarantaine) écrit dans la mémoire du foyer, l'export du compte porte sa mémoire."""
    if not getattr(config, "tenant_long_memory", False):
        long_http.configure(None)
        return
    import os

    from nokker.household.memory import HouseholdRecall
    from nokker.memory.tenant_long import (CorthexisEmbedder, LongMemory, LongMemoryTools, MemoryLongStore,
                                           PgLongMemoryStore, Settings, household_membership)

    house = household_http.service()
    membership = household_membership(house) if house is not None else (lambda _a: None)
    dsn = config.pg_dsn()
    store: Any = PgLongMemoryStore(dsn) if dsn else MemoryLongStore(membership)
    embedder: Any = None
    if os.environ.get("CORTHEXIS_EMBED_URLS"):
        cx = CorthexisEmbedder()
        if cx.available():
            embedder = cx
        else:
            logger.warning("mémoire longue : paquet corthexis absent — rappel lexical")
    else:
        logger.warning("mémoire longue : CORTHEXIS_EMBED_URLS absent — rappel lexical")
    lrn = learning_http.service()
    pending = getattr(lrn, "pending", None) if lrn is not None else None
    svc = LongMemory(store=store, membership=membership,
                     account_for_tenant=getattr(tenancy, "account_for_tenant", None), embedder=embedder,
                     household_recall=HouseholdRecall(household=house) if house is not None else None,
                     settings=Settings.from_config(config.tenant_long_memory_settings),
                     scrub=kernel.invariants.scrub_output, review=pending is not None)
    if hasattr(kernel.memory, "tenant_memory"):
        kernel.memory.tenant_memory = svc.for_tenant
    if pending is not None:  # proposition d'un membre au foyer, approuvée par le payeur : elle entre au foyer
        inner_commit = pending.commit_fact

        def commit_fact(note: Any) -> Any:
            if note.source == "household" and note.scope == "household":
                from nokker.learning.pending import PendingError
                from nokker.memory.tenant_long import LongMemoryError

                try:
                    return svc.commit_pending(note)
                except LongMemoryError as e:  # la note reste en attente, le payeur voit le motif
                    raise PendingError(e.status, e.code, e.message) from None
            return inner_commit(note) if inner_commit is not None else None

        pending.commit_fact = commit_fact
    if svc.settings.tools and hasattr(kernel.tools, "tester_sources"):
        kernel.tools.tester_sources.append(LongMemoryTools(svc))
        kernel.tools.refresh()
    exports = getattr(account_http, "_exports", None)
    if exports is not None and hasattr(exports, "long_memory"):
        exports.long_memory = svc.export
    long_http.configure(svc)


def configure_consolidation(config: NokkerConfig, kernel: Any, tenancy: Any, hub: Any = None) -> None:
    """Consolidation nocturne de la mémoire (docs/CONSOLIDATION.md) — drapeau `memory_consolidation`, éteint par
    défaut : aucune route, aucun passage, aucun texte de plus. Demande la mémoire longue (`tenant_long_memory`). Le
    planificateur tourne dans le noyau (réglage `scheduler`), à l'heure locale de chaque compte."""
    if not getattr(config, "memory_consolidation", False):
        consolidation_http.configure(None)
        return
    long_svc = long_http.service()
    if long_svc is None:
        logger.warning("consolidation : demande tenant_long_memory — éteinte")
        consolidation_http.configure(None)
        return
    from nokker.memory.consolidation.scheduler import ConsolidationScheduler, tenant_accounts
    from nokker.memory.consolidation.wiring import build, notifier

    plan = plan_http.service()
    dsn = config.pg_dsn()
    manager = getattr(getattr(kernel, "context", None), "manager", None) or getattr(hub, "context_manager", None)
    svc = build(config, long_svc, dsn=dsn, episodes=getattr(manager, "episodes", None),
                history_from=getattr(kernel.store, "history_from", None), scrub=kernel.invariants.scrub_output,
                ledger=getattr(kernel, "ledger", None),
                notify=notifier(hub, getattr(plan, "delivery", None) if plan is not None else None))
    consolidation_http.configure(svc)
    if svc.settings.scheduler and dsn:
        ConsolidationScheduler(service=svc, accounts=tenant_accounts(dsn, tenancy)).start(svc.settings.tick_s)


def configure_sso(config: NokkerConfig, tenancy: Tenancy, approvals: Any) -> None:
    """SSO OIDC des équipes (nokker/sso/, docs/LOKKAL.md §7.24) : drapeau `sso` ET équipe allumée ET approbations
    (l'appareil du navigateur s'y enrôle) ET `sso_settings.redirect_uri`. Sinon aucune route, rien ne change, et
    `Team.sso_gate` reste vide. Secrets des clients : coffre du noyau, portée `system` (`sso_settings.secrets`, même
    forme que `local.secrets` : file | openbao). Preuve des domaines : `sso_settings.dns_over_https` (URL d'un
    résolveur DoH JSON) ; absent, la vérification d'un domaine répond 503 `dns_unavailable`."""
    team = team_http.service()
    settings = dict(config.sso_settings or {})
    redirect_uri = str(settings.get("redirect_uri") or "")
    if not config.sso or team is None or approvals is None or not redirect_uri.startswith("https://"):
        if config.sso:
            logger.warning("drapeau sso sans équipe, sans approbations ou sans sso_settings.redirect_uri https : SSO éteint")
        sso_http.configure(None)
        if team is not None:
            team.sso_gate = None
        return
    from pathlib import Path

    from nokker.secrets.vault import build_provider, chosen_provider
    from nokker.sso.idp import Idp, SafeHttp
    from nokker.sso.secrets import VaultSsoSecrets
    from nokker.sso.service import Sso
    from nokker.sso.store import MemorySsoStore, PgSsoStore
    from nokker.sso.txt import doh_resolver

    dsn = config.pg_dsn()
    scfg = dict(settings.get("secrets") or {})
    state_dir = Path(str(settings.get("state_dir") or "~/.config/nokker/sso")).expanduser()
    provider = build_provider(chosen_provider(scfg), "system", scfg, state_dir)
    if getattr(provider, "kind", "") == "file" and not getattr(provider, "create_key", True) \
            and provider._box(for_write=False) is None:
        logger.warning("SSO : clé du coffre des secrets de clients absente (%s) : enregistrer un secret de client "
                       "échouera", getattr(provider, "key_env", "?"))
    http = SafeHttp()
    doh = str(settings.get("dns_over_https") or "")
    sso = Sso(store=PgSsoStore(dsn) if dsn else MemorySsoStore(), team=team, tenancy=tenancy, approvals=approvals,
              secrets=VaultSsoSecrets(provider), idp=Idp(http=http), redirect_uri=redirect_uri,
              resolve_txt=doh_resolver(http, doh) if doh else None)
    sso_http.configure(sso)
    team.sso_gate = sso.gate_reason


def configure_plan_operate(config: NokkerConfig, kernel: Kernel, *, agents: AgentsHub | None, hub: BubbleHub | None,
                           approvals: BaseApprovals | None, tenancy: Tenancy, events: EventBus | None,
                           connector_store: Any = None) -> None:
    """Plan et incidents (lot X18, docs/LOKKAL.md §7.18) : drapeaux `plan` et `operate`, éteints par défaut (aucune
    route, aucun outil, aucun bloc de contexte). Les passages d'agents (alertes, brief) passent par le moteur Crew."""
    dsn = config.pg_dsn()
    publish = events.publish if events is not None else None
    scrub = kernel.invariants.scrub_output
    operate = None
    if config.operate:
        from nokker.incidents.service import Operate
        from nokker.incidents.store import MemoryIncidents, PgIncidents
        from nokker.store.conn import known_tenants

        operate = Operate(store=PgIncidents(dsn) if dsn else MemoryIncidents(), agents=agents, approvals=approvals,
                          publish=publish, scrub=scrub, tenants=(lambda: known_tenants(dsn)) if dsn else None)
    incidents_http.configure(operate)
    if not config.plan:
        plan_http.configure(None)
        return
    from nokker.plan.service import Plan
    from nokker.plan.store import MemoryPlan, PgPlan
    from nokker.plan.tools import PlanToolSource
    from nokker.plan.types import PlanError

    check_project = None
    if config.team:
        def check_project(sender: str, pid: str) -> str:
            from nokker.team.model import TeamError

            team = team_http.service()
            account = tenancy.account(sender) if is_account(sender) else None
            if team is None or account is None:
                raise PlanError("projet inconnu", status=404, code="not_found")
            try:
                return team.check_agent_project(account, pid)
            except TeamError as e:
                raise PlanError(e.message, status=e.status, code=e.code) from None

    plan = Plan(store=PgPlan(dsn) if dsn else MemoryPlan(), agents=agents, hub=hub, history=kernel.store.history,
                operate=lambda: incidents_http.service(), has_tool=lambda name: kernel.tools.spec(name) is not None,
                check_project=check_project, publish=publish, scrub=scrub)
    # Brief hors de l'app (docs/LOKKAL.md §7.18.5) : Telegram (bot posé par `configure_telegram`) et e-mail (connecteur
    # mail du compte, armé par carte forte) ; chaque personne choisit, rien ne part par défaut.
    from nokker.plan.delivery import BriefDelivery, RealBriefTransports

    plan.delivery = BriefDelivery(store=plan.store, transports=RealBriefTransports(tenancy=tenancy,
                                                                                   connectors=connector_store),
                                  approvals=approvals, scrub=scrub, app_url=config.app_url or "")
    plan_http.configure(plan)
    kernel.context.plan = plan  # consignes héritées dans le message système des fils qui travaillent sur une carte
    if hub is not None:
        hub.plan = plan  # bulles `plan_split` : accepter / refuser
    tools = PlanToolSource(plan=plan)
    registry = getattr(kernel.tools, "internal", None)
    if registry is not None and hasattr(kernel.tools, "tester_sources"):
        registry.sources.append(tools)
        kernel.tools.tester_sources.append(tools)
    elif hasattr(kernel.tools, "sources"):
        kernel.tools.sources.append(tools)
    kernel.tools.refresh()


def _team_rules_for_code(account_id: str, repo_url: str) -> list[dict[str, Any]]:
    """Règles des projets d'équipe (X17) qui déclarent ce dépôt, pour ce compte : restreindre seulement. Équipe
    éteinte ou compte inconnu : aucune."""
    from nokker.context.project_memory import normalize_remote

    team = team_http.service()
    actor = team.actor_for(account_id) if team is not None else None
    if actor is None:
        return []
    key = normalize_remote(repo_url)
    return [dict(p.get("rules") or {}) for p in team.machine_projects(actor).get("projects") or []
            if any(isinstance(r, dict) and r.get("key") == key for r in p.get("repos") or [])]


def configure_code_sessions(config: NokkerConfig, kernel: Kernel, *, hub: Any = None, events: Any = None) -> Any:
    """Sessions de code multi-utilisateur (nokker/code_sessions/, docs/LOKKAL.md §7.28) : bloc `code.enabled`, éteint
    par défaut — alors aucune route /v1/code/*, aucun outil, portes inchangées. Allumé : outils de code (enveloppés
    par les points de retour) et outils des points de retour dans le jeu des COMPTES (jamais l'interne), portes des
    sessions devant celles du noyau, hooks du dépôt (`code.hooks`), ménage des bacs à sable de CETTE instance au
    démarrage, balayeur d'inactivité ; skills du dépôt (`code.project_skills`) et sous-agents (`code.max_subagents`)
    dans le jeu des comptes, cartes de ces fils au propriétaire de la session seulement (`approvals.thread_owner`).
    Bloc refusé : `CodeConfigError` (le noyau ne démarre pas)."""
    import dataclasses as _dc

    from nokker.code_sessions.config import CodeConfig
    from nokker.code_sessions.secrets import TenantSecrets
    from nokker.code_sessions.service import CodeSessions
    from nokker.code_sessions.store import MemoryCodeStore, PgCodeStore
    from nokker.code_sessions.checkpoints import SessionCheckpoints
    from nokker.tools.checkpoints import CheckpointingToolSource, CheckpointToolSource
    from nokker.tools.code import reap_sandboxes

    cfg = CodeConfig.from_config(config.code, lokkal=config.local is not None)
    if cfg is None:
        code_http.configure(None)
        return None
    dsn = config.pg_dsn()
    secrets = TenantSecrets.from_config(cfg.secrets, Path(cfg.state_dir) / "secrets") \
        if cfg.secrets is not None else None
    inv = kernel.invariants
    svc = CodeSessions(cfg=cfg, store=PgCodeStore(dsn) if dsn else MemoryCodeStore(), secrets=secrets, hub=hub,
                       note=getattr(kernel, "add_owner_note", None), team_rules=_team_rules_for_code,
                       scrub=getattr(inv, "scrub_output", lambda s: s), redact=getattr(inv, "redact_result", None))
    if cfg.egress_secrets is not None:  # drapeau `code.egress_secrets` (docs/EGRESS-SECRETS.md)
        from nokker.egress.kernel import EgressSecrets

        svc.egress = EgressSecrets(cfg.egress_secrets)
    # Points de retour : un magasin PAR SESSION sous code.state_dir (SessionCheckpoints), jamais le magasin commun de
    # l'instance ; amont sans home par contexte : enveloppe transparente, outils « indisponibles ».
    mgr = SessionCheckpoints(svc)
    svc.checkpoints = mgr
    wrapper = CheckpointingToolSource(svc.tools(), svc.root_of, manager=mgr)
    checkpoints = CheckpointToolSource(svc.root_of, manager=mgr)
    if not mgr.available():
        wrapper.manager = checkpoints.manager = None
        logger.warning("sessions de code : points de retour indisponibles (amont sans home par contexte)")
    skills, subagents = svc.skill_tools(), svc.subagent_tools()
    # `skill_list`/`skill_view` : AVANT les packs du web (configure_packs, plus tard) qui portent les mêmes noms ; hors
    # session, la source des packs répond telle quelle (`skills_fallback`).
    kernel.tools.tester_sources.extend([wrapper, checkpoints, skills, subagents])
    if config.execute_code_enabled:
        # execute_code (docs/LOKKAL.md §7.31) : dans le bac à sable de la session du fil, propriétaire seulement.
        from nokker.tools.execute_code import ExecuteCodeSource

        kernel.tools.tester_sources.append(ExecuteCodeSource.from_config(svc.tools().script_source, config.execute_code))
    kernel.tools.refresh()

    def packs_source() -> Any:
        return next((src for src in kernel.tools.tester_sources if src is not skills
                     and any(sp.name == "skill_list" for sp in src.specs())), None)

    def child_kernel(registry: Any, thread_id: str, observer: Any) -> Any:
        # L'enfant : une copie du noyau TEL QU'IL EST quand l'enfant part (portes du foyer posées plus tard à
        # l'extérieur comprises), autour du registre filtré ; hooks du dépôt autour de lui aussi (bloc `subagents`).
        return _dc.replace(kernel, observer=observer, tools=svc.wrap_hooks(registry) if cfg.hooks else registry)

    svc.skills_fallback = packs_source
    svc.kernel_factory = child_kernel
    svc.delegate_fork = bool(getattr(config, "delegate_fork", False))
    svc.parent_registry = lambda: kernel.tools.current()  # le jeu du locataire courant (TenantRegistry)
    svc.emit = events.publish if events is not None else None
    svc.parent_observer = hub
    if hasattr(getattr(kernel, "approvals", None), "thread_owner"):
        kernel.approvals.thread_owner = svc.owner_of_thread
    kernel.gates = svc.gates(kernel.gates)
    if cfg.hooks:
        kernel.tools = svc.wrap_hooks(kernel.tools)
    if cfg.backend == "kubernetes":
        n = cfg.reaper(cfg.instance)
    else:
        n = reap_sandboxes(cfg.instance)
    logger.info("sessions de code : %d bac(s) à sable orphelin(s) supprimé(s) au démarrage (instance %s, %s)",
                n, cfg.instance, cfg.backend)
    svc.start_reaper()
    forge_triggers = None
    if cfg.forge_triggers:
        # Déclencheurs de forge (§7.28.9) : webhook signé → une session par PR ; `forge_push` dans le jeu des comptes.
        from nokker.code_sessions.forge_triggers import (
            FORGE_CHANNEL, FORGE_SENDER, ForgeTriggers, MemoryForgeStore, PgForgeStore, make_executor)
        from nokker.store.conn import known_tenants

        def forge_turn(thread_id: str, text: str) -> Any:
            return kernel.run_turn(TurnInput(thread_id=thread_id, user_text=text, channel=FORGE_CHANNEL,
                                             sender=FORGE_SENDER))

        forge_triggers = ForgeTriggers(sessions=svc, store=PgForgeStore(dsn) if dsn else MemoryForgeStore(),
                                       run_turn=forge_turn, tenants=(lambda: known_tenants(dsn)) if dsn else None,
                                       scrub=getattr(inv, "scrub_output", None), executor=make_executor())
        kernel.tools.tester_sources.append(forge_triggers.tools())
        kernel.tools.refresh()
    code_http.configure(svc, forge_triggers)
    return svc


def configure_onboarding(config: NokkerConfig, tenancy: Tenancy, approvals: Any) -> None:
    """Routes publiques d'onboarding, seulement si front1 est configuré (bêta OVH)."""
    from nokker.onboarding.acceptances import MemoryAcceptances, PgAcceptances
    from nokker.onboarding.consents import MemoryConsents, PgConsents
    from nokker.onboarding.emailcode import EmailCodes, MemoryCodes, PgCodes
    from nokker.onboarding.front import FrontInvitations
    from nokker.onboarding.service import Onboarding

    token = os.environ.get(config.front_token_env, "")
    if not (config.front_url and token and approvals is not None):
        onboarding_http.configure(None)
        return
    dsn = config.pg_dsn()
    cf_id, cf_secret = os.environ.get(config.front_access_id_env, ""), os.environ.get(config.front_access_secret_env, "")
    access = {"CF-Access-Client-Id": cf_id, "CF-Access-Client-Secret": cf_secret} if cf_id and cf_secret else None
    front = FrontInvitations(config.front_url, token, extra_headers=access)
    codes = EmailCodes(front=front, tenancy=tenancy, approvals=approvals, store=PgCodes(dsn) if dsn else MemoryCodes())
    onboarding_http.configure(Onboarding(front=front, tenancy=tenancy,
                                         approvals=approvals,
                                         acceptances=PgAcceptances(dsn) if dsn else MemoryAcceptances(),
                                         consents=PgConsents(dsn) if dsn else MemoryConsents()), codes)


def configure_account(config: NokkerConfig, kernel: Kernel, approvals: Any = None) -> None:
    """« Mon compte » hors onboarding : export des données (nokker/account/export.py). Postgres : exports gardés 24 h
    sous RLS ; sans base, en mémoire (tests, dev)."""
    from nokker.account.export import AccountExports, MemoryExports, PgExports
    from nokker.onboarding.acceptances import PgAcceptances
    from nokker.onboarding.consents import PgConsents

    dsn = config.pg_dsn()
    limits = kernel.limits
    account_http.configure(AccountExports(
        store=PgExports(dsn) if dsn else MemoryExports(), turns_after=kernel.store.turns_after,
        acceptances=PgAcceptances(dsn).for_account if dsn else None, consents=PgConsents(dsn).history if dsn else None,
        usage=limits.usage if limits is not None and hasattr(limits, "usage") else None), approvals)


def build_shares(config: NokkerConfig) -> ThreadShares:
    """Liens de conversation partagés (nokker/threads/shares.py) : Postgres (migration 044) si une base est posée,
    sinon en mémoire (dev : perdus au redémarrage). `share_base_url` préfixe les liens rendus."""
    from nokker.threads.shares import PgShares

    dsn = config.pg_dsn()
    return ThreadShares(PgShares(dsn) if dsn else MemoryShares(), base_url=config.share_base_url)


def configure_billing(config: NokkerConfig) -> None:
    """Abonnements Stripe, seulement si `billing_enabled` ET les deux secrets posés. Jamais de clé live par défaut."""
    from nokker.billing.plans import plans_from_config
    from nokker.billing.service import Billing
    from nokker.billing.store import MemoryBilling, PgBilling
    from nokker.billing.stripe import StripeClient

    key, webhook = os.environ.get(config.billing_secret_key_env, ""), os.environ.get(config.billing_webhook_secret_env, "")
    if not (config.billing_enabled and key and webhook):
        if config.billing_enabled:
            missing = [n for n, v in ((config.billing_secret_key_env, key), (config.billing_webhook_secret_env, webhook))
                       if not v]
            logger.warning("billing_enabled sans %s : facturation éteinte (plafonds de la bêta)", " ni ".join(missing))
        billing_http.configure(None)
        return
    if key.startswith(("sk_live_", "rk_live_")) and not config.billing_live:
        raise RuntimeError("billing: clé Stripe live refusée (billing_live est faux)")
    dsn = config.pg_dsn()
    billing_http.configure(Billing(store=PgBilling(dsn) if dsn else MemoryBilling(), stripe=StripeClient(key),
                                   plans=plans_from_config(config.billing_plans), prices=dict(config.billing_prices),
                                   webhook_secret=webhook, app_url=config.app_url,
                                   coupon=str(getattr(config, "billing_coupon", "") or "")))


def configure_feedback(config: NokkerConfig, kernel: Kernel) -> None:
    """Retours des testeurs : gardés dans leur locataire, signalés aux opérateurs par le bot de l'instance interne (comme les
    candidatures) si `admin_notify_chat_ids` et son jeton sont posés."""
    from nokker.admin.pull import TelegramText

    dsn = config.pg_dsn()
    notify_token = os.environ.get(config.admin_notify_token_env, "")
    notify = TelegramText(notify_token, config.admin_notify_ids()).send \
        if notify_token and config.admin_notify_ids() else None
    feedback_mod.configure(feedback_mod.Feedback(
        store=feedback_mod.PgFeedback(dsn) if dsn else feedback_mod.MemoryFeedback(),
        scrub=kernel.invariants.scrub_output, notify=notify))


def configure_telegram(config: NokkerConfig, tenancy: Tenancy, kernel: Kernel, hub: Any, approvals: Any = None) -> None:
    """Canal Telegram du noyau (bot de la bêta) : jeton, nom du bot et secret de webhook tous trois requis."""
    from nokker.telegram.adapter import BotApi, TelegramAdapter
    from nokker.telegram.pairing import MemoryPairing, PgPairing

    token, secret = config.telegram_bot_token(), os.environ.get(config.telegram_webhook_secret_env, "")
    if not (token and secret and config.telegram_bot_username):
        telegram_http.configure(None, None, "", "")
        return
    dsn = config.pg_dsn()
    pairing = PgPairing(dsn) if dsn else MemoryPairing()

    def run_turn(inp: TurnInput) -> Turn:  # même chemin que l'API : fil du hub, verrou du fil
        if hub is not None:
            hub.ensure_thread_for_turn(inp.thread_id, inp.sender, inp.user_text)
            with hub.thread_lock(inp.thread_id):
                return kernel.run_turn(inp)
        return kernel.run_turn(inp)

    bot = BotApi(token)
    from nokker.voice import speak_lexicon
    from nokker.voice.tts import from_config as voice_from_config

    sl = speak_lexicon.setup(config.speak_lexicon, dsn)  # None (drapeau éteint) : voix inchangée
    house_of = household_http.service

    def speak_scope(account: Any) -> Any:
        h = house_of()
        got = h.membership(account.account_id) if h is not None else None
        return speak_lexicon.SpeakScope(got[1].tenant_id, got[1].id) if got is not None else None

    adapter = TelegramAdapter(tenancy=tenancy, pairing=pairing, run_turn=run_turn, bot=bot,
                              feedback=feedback_mod.service(),
                              voice=voice_from_config(config.tts, kernel.invariants.scrub_output,
                                                      prepare=sl.prepare if sl is not None else None),
                              speak_scope=speak_scope if sl is not None else None)
    telegram_http.configure(adapter, pairing, secret, config.telegram_bot_username)
    if getattr(kernel, "goals", None) is not None:  # points d'étape des objectifs : Telegram relié au compte
        from nokker.goals.service import TelegramGoalNotifier

        kernel.goals.notify.append(TelegramGoalNotifier(tenancy=tenancy, bot=bot))
    plan = plan_http.service()
    if plan is not None and plan.delivery is not None:  # brief du matin par Telegram : sendMessage seulement
        plan.delivery.transports.bot = bot
    if approvals is not None and config.app_url:
        from nokker.telegram.notify import TesterApprovalNotifier

        approvals.tester_notifier = TesterApprovalNotifier(tenancy=tenancy, bot=bot, app_url=config.app_url)
    if cloud_http._cloud is not None and cloud_http._cloud.approvals_relay is not None:
        # Carte forte déposée par une machine liée (lot X10) : un message au Telegram appairé du compte, avec le lien
        # vers la page de décision (sendMessage seulement ; on ne décide rien dans Telegram).
        from nokker.cloud.approvals import RelayedApprovalNotifier

        base = cloud_http._cloud.links.base_url.rstrip("/")
        mini = ""
        if cloud_http._cloud.attestor is not None:  # bouton Mini App seulement si nokker.ai sait vérifier la preuve
            mini = str((config.cloud_link or {}).get("mini_app_url") or config.telegram_mini_app_url
                       or f"{base}/miniapp/approve.html")  # la Mini App déjà déclarée chez BotFather
        cloud_http._cloud.notify_approval = RelayedApprovalNotifier(
            tenancy=tenancy, bot=bot, app_url=base, mini_app_url=mini)


def configure_channels(config: NokkerConfig, tenancy: Tenancy, kernel: Kernel, hub: Any, approvals: Any = None) -> None:
    """Canaux Slack et WhatsApp (docs/CHANNELS.md) : après Telegram, dont ils complètent le notificateur de cartes."""
    from nokker.channels.setup import configure_channels as _configure

    try:
        _configure(config, tenancy, kernel, hub, approvals)
    except Exception:  # noqa: BLE001 — un canal mal configuré ne bloque jamais le noyau
        logger.exception("canaux Slack/WhatsApp : configuration en échec, canaux éteints")
        channels_http.configure(None, None)


def tool_loading(config: NokkerConfig) -> str:
    """« all » si l'amont du palier Ship a un cache de préfixe, « relevance » sinon (tools/select.py)."""
    ship = config.tiers.get("ship")
    return "all" if ship is not None and ship.cache == "prefix" else "relevance"


def start_admin(config: NokkerConfig) -> Any:
    """Console opérateur (lot A0) : écouteur séparé + tirage des candidatures. Une panne ici ne bloque jamais le noyau."""
    from nokker.admin.access import AccessVerifier
    from nokker.admin.pull import BetaPuller, TelegramText
    from nokker.admin.server import serve_admin
    from nokker.admin.store import MemoryAdminStore, PgAdminStore

    try:
        dsn = config.pg_dsn()
        store: Any = PgAdminStore(dsn) if dsn else MemoryAdminStore()
        store.seed_operators(config.admin_operators, "system:config")
        verifier = AccessVerifier(team_domain=config.admin_access_team,
                                  audience=os.environ.get(config.admin_access_aud_env, ""))
        if not verifier.team_domain:
            logger.warning("console opérateur : admin_access_team vide, toutes les requêtes seront refusées")
        if not verifier.audience:
            logger.warning("console opérateur : %s vide, toutes les requêtes seront refusées", config.admin_access_aud_env)
        token = os.environ.get(config.beta_pull_token_env, "")
        front = None
        pull = bool(config.admin_candidatures and config.beta_pull_url)
        if config.beta_pull_url and not config.admin_candidatures:
            logger.warning("console opérateur : admin_candidatures est faux, beta_pull_url ignoré (aucun tirage)")
        if not config.admin_candidatures:
            logger.info("console opérateur : module candidatures désactivé (console Suisse : %s)",
                        config.admin_candidatures_console)
        if pull and token:
            from nokker.onboarding.front import FrontInvitations

            front = FrontInvitations(config.beta_pull_url, token)  # invitations : mêmes routes internes que le tirage
        from nokker.admin.overview import MemoryOverview, PgOverview, enabled_flags

        httpd = serve_admin(store, verifier, config.admin_host, config.admin_port, front,
                            overview=PgOverview(dsn) if dsn else MemoryOverview(),
                            candidatures=config.admin_candidatures,
                            candidatures_console=config.admin_candidatures_console, limits=config.tenant_limits,
                            billing_plans=config.billing_plans, flags=enabled_flags(config))
        if pull and token:
            bot = os.environ.get(config.admin_notify_token_env, "")
            notify = TelegramText(bot, config.admin_notify_ids()).send \
                if bot and config.admin_notify_ids() else None
            if notify is None:
                logger.warning("candidatures : pas de notification Telegram (%s ou admin_notify_chat_ids vide)",
                               config.admin_notify_token_env)
            BetaPuller(store=store, base_url=config.beta_pull_url, token=token, send=notify,
                       console_url=config.admin_console_url, interval_s=config.beta_pull_interval_s).start()
        elif pull:
            logger.warning("tirage des candidatures : %s vide, tirage désactivé", config.beta_pull_token_env)
        return httpd
    except Exception:  # noqa: BLE001
        logger.exception("console opérateur : démarrage en échec (le noyau continue)")
        return None


def _mcp_header_source(srv: dict[str, Any], static: dict[str, str], secrets: Any | None) -> Any:
    """En-têtes d'un serveur MCP avec `token` et/ou `headers` (valeurs littérales ou `secret:NOM`), relus au coffre à
    chaque ouverture de session. None si le serveur n'en déclare pas (en-têtes fixes, comme avant X4)."""
    if not srv.get("token") and not srv.get("headers"):
        return None
    from nokker.secrets.refs import SecretResolver

    resolver = secrets.resolver() if secrets is not None else SecretResolver(None)

    def build() -> dict[str, str]:
        h = {**static, **{str(k): str(v) for k, v in resolver.resolve(dict(srv.get("headers") or {})).items()}}
        if srv.get("token"):
            h["Authorization"] = f"Bearer {resolver.resolve(srv['token'])}"
        return h

    return build


def _home_source(config: NokkerConfig, local_api: Any) -> Any:
    """Outils maison (docs/HOME-ASSISTANT.md) si `home_assistant.enabled` ; une config refusée (jeton en clair, URL
    hors règles) laisse la maison éteinte, avec le motif au journal (jamais de valeur)."""
    if not (config.home_assistant or {}).get("enabled"):
        return None
    from nokker.home.client import HomeError
    from nokker.home.source import home_source

    vault = getattr(local_api, "vault", None) if local_api is not None else None
    try:
        return home_source(config, lokkal=local_api is not None,
                           resolve=vault.resolver().resolve if vault is not None else None,
                           state_dir=getattr(local_api, "state_dir", None) if local_api is not None else None)
    except (HomeError, ValueError) as e:
        logger.error("maison : configuration refusée, outils éteints (%s)", e)
        return None


def configure_home_watch(kernel: Kernel, source: Any, hub: BubbleHub | None) -> None:
    """Veille du foyer allumée : porte et cartes fortes, bloc du brief, route média, fil de relève (docs/HOME-WATCH.md)."""
    from nokker.homewatch import http as home_watch_http
    from nokker.homewatch.source import install

    plan = plan_http.service()
    delivery = getattr(plan, "delivery", None) if plan is not None else None
    push = delivery.deliver if delivery is not None else None  # Telegram/e-mail : seulement si la personne l'a armé
    install(kernel, source, hub, push)
    household = household_http.service()
    if household is not None:  # profil mineur d'un foyer : toujours le rôle teen/child, quelle que soit la config
        def profile_of(sender: str | None) -> str | None:
            st = household.minor_state(sender)
            return st.profile if st is not None else None

        source.policy.profile_of = profile_of
    if plan is not None:
        plan.home_watch = source.brief_text
    home_watch_http.configure(source, hub)
    source.start()
    logger.info("veille du foyer : allumée (passerelle %s)", source.client.base_url)


def _home_watch_source(config: NokkerConfig, local_api: Any) -> Any:
    """Veille du foyer (docs/HOME-WATCH.md) si `home_watch.enabled`, Lokkal seulement (la passerelle `home-bridge` n'existe
    que là). Une config refusée (jeton absent, URL, rôle inconnu) laisse la veille éteinte, motif au journal."""
    if local_api is None or not (config.home_watch or {}).get("enabled"):
        return None
    from nokker.homewatch.client import BridgeError
    from nokker.homewatch.source import home_watch_source

    try:
        return home_watch_source(config, token=os.environ.get("NOKKER_HOME_BRIDGE_TOKEN", ""),
                                 state_dir=getattr(local_api, "state_dir", None))
    except (BridgeError, ValueError) as e:
        logger.error("veille du foyer : configuration refusée, outils éteints (%s)", e)
        return None


def mcp_apps_origin(config: NokkerConfig) -> str:
    """`mcp_apps_origin` validé (docs/MCP-NEXT.md §3) : lève ValueError — le démarrage échoue — si la valeur est
    l'origine de l'app (`app_url`, `webauthn_origin`, `front_url`, `telegram_mini_app_url`) ou partage son domaine."""
    from nokker.tools.mcp_next import check_apps_origin

    return check_apps_origin(getattr(config, "mcp_apps_origin", "") or "",
                             [u for u in (config.app_url, config.webauthn_origin, config.front_url,
                                          config.telegram_mini_app_url) if u])


def mcp_tool_sources(config: NokkerConfig, effects: EffectsTable, secrets: Any | None,
                     apps: Any | None = None) -> list[Any]:
    """Une source par serveur de `mcp_tool_servers`. Un serveur MCP ne déclare pas d'effet lui-même : la table (F3)
    l'annote, et sert aussi de 2e ligne de défense (SECRET -> poignée) indépendante de redact_result du noyau. Un
    serveur qui déclare des outils en lecture (`read_tools`, nokker/tools/mcp_config.py) est entouré de
    `ReadDeclaredSource`, qui ne lève que des écritures par défaut, jamais un effet connu."""
    from nokker.previews.ssrf import current_policy, resolve_and_check
    from nokker.tools.mcp_config import ReadDeclaredSource

    apps_origin = mcp_apps_origin(config) if config.mcp_apps else ""
    out: list[Any] = []
    for srv in config.mcp_tool_servers:
        token = os.environ.get(srv.get("token_env", ""), "") if srv.get("token_env") else ""
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        source_kw = {"header_source": hs} if (hs := _mcp_header_source(srv, headers, secrets)) is not None else {}
        if srv.get("origin") == "user" and current_policy().strict:
            # Serveur ajouté par l'utilisateur (session, import, pack) : son adresse vient d'un tiers possible — garde
            # SSRF à chaque ouverture de session, connexion épinglée. Les serveurs de config.yaml restent hors garde.
            source_kw["guard"] = lambda url: resolve_and_check(url, enforce_ports=True)
        if config.mcp_modern or config.mcp_apps:  # docs/MCP-NEXT.md ; éteints = construction d'avant
            source_kw.update(modern=config.mcp_modern, apps=config.mcp_apps, task_wait_s=config.mcp_task_wait_s)
            if config.mcp_apps and apps_origin:
                source_kw["apps_origin"] = apps_origin
        raw_source = McpToolSource(name=srv["name"], url=srv["url"], headers=headers,
                                   close_on_turn_end=srv.get("close_on_turn_end"), **source_kw)
        source: Any = SecretScrubbingToolSource(inner=raw_source, effects=effects)
        reads = [str(t) for t in srv.get("read_tools") or [] if isinstance(t, str)]
        if reads:
            source = ReadDeclaredSource(inner=source, read_tools=reads, effects=effects,
                                        confirmed_writes=[str(t) for t in srv.get("confirmed_writes") or []])
        if apps is not None:
            apps.add_server(srv["name"], source, raw_source, effects.annotate)
        out.append(source)
    return out


def build_kernel(config: NokkerConfig, invariants: Invariants | None = None,
                 ledger: LedgerBackend | None = None, memory: object | None = None,
                 approvals: BaseApprovals | None = None, hub: BubbleHub | None = None,
                 connectors: ConnectorProvider | None = None, extra_sources: list[Any] | None = None,
                 secrets: Any | None = None) -> Kernel:
    """Ressources par locataire (docs/TENANCY.md §5) : outils (`TenantRegistry`), mémoire longue (`TenantMemory`),
    préfixe (`product_prefix_path` de l'engine) et plafonds (`TenantLimits`, `LimitedPolicy`) suivent le locataire
    courant, lu à chaque appel. Interne : tout ce qui suit, inchangé. Testeur : cœurs + `recall_context` + web + bulles +
    SES connecteurs (`connectors`, défaut `NoConnectors`) ; jamais `mcp_tool_servers` ni la mémoire longue.

    `memory` : même pattern d'injection que `invariants`/`ledger` ci-dessus — par défaut, le MCP
    `sokkan-memory` RÉEL (prod), `None` pour tout appelant normal. Un appelant (ex. le banc P1-S1, qui
    doit comparer le noyau aux mêmes stubs que la boucle nue, pas à de vraies notes de prod) peut
    passer son propre objet, pourvu qu'il porte les DEUX rôles de `MemoryPort`/`ToolSource`
    (`recall`/`propose` + `specs`/`call`) — voir `CorthexisMemory` ci-dessous pour le contrat exact.
    Le remplacement vit côté appelant (config du banc), pas ici.

    `extra_sources` : sources d'outils de plus pour le locataire interne seulement (jamais pour un testeur) — les outils
    de code d'une session Nokker Lokkal (`nokker code`, `nokker/tools/code.py`).

    `secrets` : le coffre de Lokkal (`nokker.secrets.Vault`, lot X4) — ses valeurs entrent au caviardage des
    invariants, et les références `secret:NOM` des serveurs MCP (`token`, `headers`) sont résolues à chaque
    connexion. None : ni l'un ni l'autre (une référence échoue à l'usage)."""
    # Politique réseau des clients sortants (bloc `ssrf:`, mode strict ÉTEINT par défaut : nokker/previews/ssrf.py).
    from nokker.previews.ssrf import configure as _ssrf_configure

    _ssrf_configure(config.ssrf)
    dsn = config.pg_dsn()
    store = PgStore(dsn) if dsn else MemoryStore()
    if dsn:
        apply_migrations(dsn)

    if memory is None:
        # `memory_mode` (ou NOKKER_MEMORY_MODE) : `mcp` = sokkan-memory par `memory_mcp_url`, inchangé ; `direct` =
        # CortHeXis dans le processus sur le Postgres + l'embed du compose (Nokker Lokkal, X1b) ; `off`.
        memory = memory_from_config(config)
    # Mode mcp et `memory_mcp_url` vide (bêta OVH) : aucune mémoire longue, ni rappel ni outils memory_*, pour personne.
    memory_sources = [memory] if memory is not None else []

    effects = EffectsTable.load(config.effect_overrides)
    app_registry = None
    if config.mcp_apps:
        from nokker.tools.mcp_apps import AppRegistry

        app_registry = AppRegistry()
    mcp_sources = mcp_tool_sources(config, effects, secrets, app_registry)
    web_sources = []
    if config.web_search_enabled:
        web_sources.append(WebToolSource(engines=list(config.web_search_engines), budget_s=config.web_search_budget_s,
                                         blocked_terms=list(config.web_search_blocked_terms)))
    bubble_sources = []
    if hub is not None:
        previews_svc = None
        if config.previews_enabled:
            from nokker.previews import get_default_service

            previews_svc = get_default_service(config)
        bubble_sources = [BubbleToolSource(hub=hub, previews=previews_svc)]
        if getattr(config, "show_me", False):  # docs/SHOW-ME.md ; éteint : aucune source, aucune bulle `pointer`
            from nokker.showme.tool import ShowMeSource

            hub.show_me = ShowMeSource()
            bubble_sources.append(hub.show_me)
    ctx_manager = build_context_manager(config, store, hub) if config.context_enabled else None
    context_sources = [RecallContextSource(ctx_manager)] if ctx_manager is not None else []
    core_source = builtin_core_source()
    # Conseiller intra-tour (`advise`, drapeau `advise.enabled`, nokker/route/advise.py) : locataire interne seulement
    # (un testeur paierait du Deep hors de son quota `deep_per_day`). Éteint : aucune source, rien ne change.
    from nokker.route.advise import build_advise_source

    def _advisor_provider(tier_cfg: Any) -> Any:
        extra = {"reasoning_off_body": tier_cfg.reasoning_off_body} if tier_cfg.reasoning_off_body is not None else {}
        return SokkanProvider(base_url=tier_cfg.base_url, api_key=tier_cfg.api_key(), model=tier_cfg.model, **extra)

    advise_source = build_advise_source(config.advise, config.tiers, _advisor_provider)
    advise_sources = [advise_source] if advise_source is not None else []
    goals = build_goals(config, known=getattr(invariants, "known", None) if invariants is not None else None)
    goal_sources = []
    if goals is not None and config.outcomes_enabled:  # outil `rubric_propose` (docs/OUTCOMES-GOAL.md)
        from nokker.goals.tools import GoalToolSource

        goal_sources = [GoalToolSource(service=goals)]
    internal_tools = Registry(sources=[core_source, *advise_sources, *context_sources, *memory_sources, *mcp_sources,
                                       *web_sources, *bubble_sources, *goal_sources, *(extra_sources or [])])
    # Pool de navigateurs de la bêta (docs/BROWSER-POOL.md) : un Chrome isolé par testeur, seulement si l'adresse du
    # gestionnaire est posée (secret du déploiement) ; l'interne garde son navigateur partagé.
    browser_sources = []
    pool_url = os.environ.get(config.browser_pool_url_env or "", "")
    if pool_url:
        from nokker.tools.browser_pool import BrowserPoolSource

        token_env = config.browser_pool_token_env
        browser_sources.append(BrowserPoolSource(base_url=pool_url, api_token=lambda: os.environ.get(token_env, "")))
    # Testeur : JAMAIS mcp_sources (workspace, navigateur partagé de l'opérateur) ni memory_sources (sokkan-memory).
    tools = TenantRegistry(internal=internal_tools, tester_sources=[core_source, *context_sources, *web_sources,
                                                                    *bubble_sources, *goal_sources, *browser_sources],
                           connectors=connectors)

    inv = invariants or build_invariants(config)
    if secrets is not None and hasattr(inv, "known"):
        # Toute valeur du coffre (les deux portées) est caviardée dans chaque résultat d'outil et chaque réponse ; les
        # valeurs connues d'avant (env NOKKER_SECRET_*, fichier) sont gardées.
        inv.known = secrets.known.merge(inv.known)
    if hub is not None and getattr(hub, "show_me", None) is not None:
        hub.show_me.scrub = inv.scrub_output  # tout ce qui part dans la bulle `pointer` est caviardé
    gates = build_gates(config, inv)
    escalator = TenantEscalator(EscalatorV0(addressee=config.escalation_addressee))  # jamais chez un testeur
    limits = TenantLimits.from_config(config.tenant_limits, dsn)
    if ctx_manager is not None:
        ctx_manager.scrub = inv.scrub_output
        # NEVER-CLEAR §14 (drapeau never_clear_fixes_enabled) : dans un fil de groupe, seule la consigne de l'expéditeur
        # qui a le GO est promue en couche 3
        ctx_manager.go_authorized = getattr(inv, "go_authorized", None)
        context: ContextEngineV0 = ContextEngineV1(tools=tools, prefix_path=config.prefix_path,
                                                   profile_path=config.profile_path, manager=ctx_manager,
                                                   product_prefix_path=config.product_prefix())
    else:
        context = ContextEngineV0(tools=tools, prefix_path=config.prefix_path, profile_path=config.profile_path,
                                  product_prefix_path=config.product_prefix())

    providers: dict[Tier, Any] = {}
    nina_mode = str(((config.route or {}).get("nina") or {}).get("mode", "off"))
    nina_provider = None
    for name, tier_cfg in config.tiers.items():
        try:
            tier = Tier(name)
        except ValueError:
            if advise_source is None or name not in advise_source.extra:  # palier du conseiller seulement (`advise`)
                logger.warning("config: palier inconnu ignoré: %s", name)
            continue
        if tier is Tier.NINA and nina_mode not in ("shadow", "route"):
            logger.info("config : palier nina posé mais éteint (route.nina.mode: %s)", nina_mode)
            continue
        if tier is Tier.SMALL and tier_cfg.api_key_env and not tier_cfg.api_key():
            # Petit palier sans clé (Lokkal sans lien de compte, clé de l'utilisateur ailleurs) : pas de palier, la
            # politique se replie sur Ship (X8) au lieu d'un appel qui échoue à chaque politesse.
            logger.info("config : petit palier sans clé (%s), %s", tier_cfg.api_key_env,
                        "en attente du lien de compte" if config.local else "ignoré")
            continue
        extra = {"reasoning_off_body": tier_cfg.reasoning_off_body} if tier_cfg.reasoning_off_body is not None else {}
        probe = None
        if tier_cfg.vision_auto:  # vision `auto` : une sonde au premier tour avec image (§7.6)
            from nokker.providers.vision_probe import VisionProbe

            probe = VisionProbe(tier_cfg.base_url, tier_cfg.model, config.vision_probe_path())
        main_provider = SokkanProvider(base_url=tier_cfg.base_url, api_key=tier_cfg.api_key(),
                                       model=tier_cfg.model, vision=tier_cfg.vision, vision_probe=probe, **extra)
        if tier_cfg.fallback_model:
            from nokker.providers.hedged import HedgedProvider

            # Le modèle de repli n'a pas de capacité déclarée à lui : pas de vision (le palier n'enverra pas d'image).
            spare = SokkanProvider(base_url=tier_cfg.base_url, api_key=tier_cfg.api_key(),
                                   model=tier_cfg.fallback_model, **extra)
            built: Any = HedgedProvider(main_provider, spare, hedge_after_s=tier_cfg.hedge_after_s)
        else:
            built = main_provider
        # Fenêtre servie (palier nina : 32 k par défaut) : `core/loop.py::_fit_context` bascule sur Ship au-delà.
        from nokker.route.nina import DEFAULT_MAX_CONTEXT

        built.max_context_tokens = tier_cfg.max_context_tokens or (DEFAULT_MAX_CONTEXT if tier is Tier.NINA else 0)
        if tier is Tier.NINA and nina_mode == "shadow":
            nina_provider = built  # en ombre : jamais dans les paliers servis, la politique ne le voit pas
            continue
        providers[tier] = built

    policy = LimitedPolicy(inner=build_policy(config, providers), limits=limits)
    ledger = ledger if ledger is not None else build_ledger(config)
    # P1 (18.10.2026) : câblé par défaut (`approvals_enabled=True`, doctrine produit — jamais d'action à
    # effet exécutée sans preuve). `approvals=` explicite (banc, test) prime toujours sur la config.
    appr = approvals if approvals is not None else (build_approvals(config) if config.approvals_enabled else None)
    tenant_memory = TenantMemory(memory if memory is not None else NullMemory())
    images_root = config.images_root()
    kernel = Kernel(store=store, context=context, memory=tenant_memory, invariants=inv, gates=gates,
                    escalator=escalator, policy=policy, providers=providers, tools=tools, ledger=ledger,
                    approvals=appr, tool_loading=tool_loading(config), observer=hub, limits=limits,
                    images=ImageStore(Path(images_root)) if images_root else None,
                    text_attachments=TextAttachments(web_enabled=config.web_search_enabled)
                    if config.text_attachments_enabled else None,
                    reviewer=build_reviewer(config, known=getattr(inv, "known", None)),
                    advisor=build_advisor(config, known=getattr(inv, "known", None)),
                    auto_reviewer=build_auto_reviewer(config, known=getattr(inv, "known", None)) if appr is not None
                    else None,
                    traces=build_recorder(config, inv))
    if nina_provider is not None:
        from nokker.route.nina import build_shadow

        sink = getattr(kernel.traces, "sink", None)
        kernel.nina_shadow = build_shadow(config, nina_provider, inv, sink=sink)
    kernel.steer_enabled = bool(getattr(config, "steer_enabled", False))
    kernel.fork_bases = bool(getattr(config, "delegate_fork", False))  # fork de sous-agent (nokker/context/fork.py)
    if goals is not None:
        if goals.grader is not None and getattr(goals.grader.transport, "known", None) is None:
            goals.grader.transport.known = getattr(inv, "known", None)  # caviardage du correcteur : coffre compris
        goals.scrub = inv.scrub_output
        goals.kernel = kernel
        kernel.goals = goals
    kernel.mcp_apps = app_registry
    kernel.provenance = build_provenance(getattr(config, "provenance", None), inv)  # None si éteint (défaut)
    if isinstance(policy.inner, PolicyV1):
        policy.inner.available = lambda: tuple(kernel.providers)  # les paliers servis à cet instant (X8)
    if config.self_improve:  # variantes APPROUVÉES sur carte (docs/SELF-IMPROVE.md) ; éteint : rien n'est lu
        from nokker.improve.overlay import install_active

        install_active(config, kernel)
    if hub is not None:
        hub.kernel = kernel
        if hub.scrub is None:
            hub.scrub = inv.scrub_output
    return kernel


def _usage_dict(turn: Turn) -> dict[str, int]:
    prompt = sum(c.usage.prompt for c in turn.calls)
    completion = sum(c.usage.completion for c in turn.calls)
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _miniapp_versioned(name: str, body: bytes, miniapp_dir: Path) -> bytes:
    """Ajoute `?v=<empreinte>` aux références internes de la Mini App (approve.html → style.css, approve.js ;
    approve.js → approve-core.js), pour qu'un fichier modifié soit rechargé même si un cache l'a gardé."""
    def tag(fname: str) -> str:
        try:
            return hashlib.sha256((miniapp_dir / fname).read_bytes()).hexdigest()[:10]
        except OSError:
            return "0"
    text = body.decode("utf-8")
    if name == "approve.html":
        text = text.replace('href="style.css"', f'href="style.css?v={tag("style.css")}"')
        text = text.replace('src="approve.js"', f'src="approve.js?v={tag("approve.js")}{tag("approve-core.js")}"')
    elif name == "approve.js":
        text = text.replace('"./approve-core.js"', f'"./approve-core.js?v={tag("approve-core.js")}"')
    else:
        return body
    return text.encode("utf-8")


def approval_to_json(a: Approval) -> dict[str, Any]:
    """Forme CANONIQUE de `GET /v1/approvals/{id}` (docs/APPROVALS.md §5, fixée pour la Mini App
    Telegram, branche `p1/miniapp`) — `tool_name`/`arguments`/`strength`/`advice`/`reason`/`rule`
    directement au nom du champ `core.ports.Approval` ; `effect` dérivé de `rule` (`"effect:xxx[:...]"`
    -> `"xxx"`) pour l'affichage ; dates en ISO 8601 UTC (jamais un epoch nu : `approve.js` fait
    `Date.parse(expiresAtIso)`)."""
    effect = a.rule.split(":")[1] if a.rule.startswith("effect:") else None
    # X8 (docs/LOKKAL.md §7.17) : l'avis du juge est rangé `[risk:<niveau>] <texte>` ; rendu séparé ici.
    advice_risk, advice = parse_advice(a.advice)
    return {
        "id": a.id, "thread_id": a.thread_id, "turn_index": a.turn_index, "channel": a.channel,
        "tool_name": a.tool_name, "arguments": a.arguments, "args_sha256": a.args_sha256, "effect": effect,
        "rule": a.rule, "reason": a.reason, "advice": advice, "advice_risk": advice_risk, "strength": a.strength,
        "status": a.status,
        "created_at": _iso(a.created_at), "expires_at": _iso(a.expires_at), "decided_by": a.decided_by,
        "decided_via": a.decided_via, "decided_at": _iso(a.decided_at), "result_summary": a.result_summary,
        "error": a.error, "call_id": a.call_id,
        # Relecteur automatique (docs/APPROVALS.md §8) : absent quand la carte n'a pas été relue (drapeau éteint).
        **({"auto_review": {**a.auto_review, "at": _iso(a.auto_review.get("at")),
                            "revoked_at": _iso(a.auto_review.get("revoked_at"))}} if a.auto_review else {}),
    }


def device_to_json(d: Device) -> dict[str, Any]:
    """Forme CANONIQUE de `POST`/`GET /v1/devices` (docs/APPROVALS.md §5) — JAMAIS `public_key_spki`
    (pas un secret, mais inutile au client une fois l'enrôlement fait ; conforme au principe « jamais
    une valeur de plus que nécessaire dans une réponse »)."""
    return {"id": d.id, "code": d.code, "name": d.name, "user": d.user, "status": d.status,
            "created_at": _iso(d.created_at), "approved_at": _iso(d.approved_at), "approved_via": d.approved_via,
            "telegram": bool(d.telegram_user_id)}  # un booléen, jamais l'identifiant Telegram


def _display_name(user_id: str) -> str:
    """`alice` -> `Alice`, `alice_b` -> `Alice B` (docs/BUBBLES.md §9.2) — mise en forme SEULEMENT (pas
    une table de vrais prénoms en config : volontairement simple pour P1)."""
    words = [w for w in re.split(r"[_\-\s]+", (user_id or "").strip()) if w]
    return " ".join(w.capitalize() for w in words) if words else (user_id or "")


def _initials(name: str) -> str:
    words = [w for w in name.split() if w]
    if len(words) >= 2:
        return (words[0][0] + words[1][0]).upper()
    if words:
        return words[0][:2].upper()
    return "?"


def user_to_json(user_id: str) -> dict[str, str]:
    """Forme de `GET /v1/users` (docs/BUBBLES.md §9.2) : identifiant + nom affichable + initiales."""
    name = _display_name(user_id)
    return {"id": user_id, "name": name, "initials": _initials(name)}


def _approvals_error_status(exc: Exception) -> int:
    if isinstance(exc, (ApprovalNotFound, DeviceNotFound)):
        return 404
    if isinstance(exc, ApprovalExpired):
        return 410
    if isinstance(exc, ApprovalNotPending):
        return 409
    if isinstance(exc, ProofInvalid):
        return 403
    return 400  # ApprovalError/DeviceError génériques, ou décision inconnue


def _device_error_code(exc: Exception) -> str:
    if isinstance(exc, DeviceNotFound):
        return "device_unknown"
    if isinstance(exc, DeviceNotApproved):
        return "device_not_approved"
    return "device_error"


def _export_filename(thread_id: str) -> str:
    """Nom de fichier d'un export : id client du fil, réduit à [A-Za-z0-9_-] (aucun guillemet dans l'en-tête)."""
    return "nokker-thread-" + (re.sub(r"[^A-Za-z0-9_-]", "_", thread_id.rsplit(":", 1)[-1])[:64] or "fil")


def make_handler(kernel: Kernel, api_keys: dict[str, str], ledger: LedgerBackend | None = None, *,
                 approvals: BaseApprovals | None = None, events: Any | None = None,
                 telegram_bot_token: str | None = None, telegram_ids: dict[str, str] | None = None,
                 miniapp_dir: Path | None = None, hub: BubbleHub | None = None,
                 previews: PreviewsRoutes | None = None, agents: AgentsHub | None = None,
                 known_users: frozenset[str] = frozenset(),
                 session_thread_by_sender: dict[str, str] | None = None,
                 tenancy: Tenancy | None = None, open_api: bool = True,
                 internal_tenant: bool = True, local_api: Any | None = None,
                 shares: ThreadShares | None = None,
                 approval_suggestions: dict[str, Any] | None = None,
                 memory_search_api: bool = False,
                 improve_config: Any | None = None,
                 knock_hub: Any = None) -> type[BaseHTTPRequestHandler]:
    """`api_keys` = {jeton Bearer: identité} (`NokkerConfig.api_key_map()`). Vide = serveur ouvert
    (dev) ; alors AUCUNE requête n'a de `sender` de confiance (toujours `None`, jamais de GO).

    `approvals`/`events` : `None` désactive TOUTES les routes `/v1/approvals*`, `/v1/devices*`,
    `/v1/sessions`, `/v1/events`, `/miniapp/*` (404, même esprit que `/metrics` sans `ledger`) — un
    serveur qui ne veut pas du chantier Approbations fortes n'en expose aucune trace.
    `telegram_ids` = `NokkerConfig.telegram_ids` ({id Telegram numérique: sender}) — résout l'identité
    d'un `initData` validé en un `sender` que `GoAuthorizedSenders` reconnaît.
    `hub` (docs/BUBBLES.md) : `None` désactive `/v1/threads*`, `/v1/bubbles*` et `/v1/notifications`
    (404) ; branché, il sert aussi `/v1/events` même sans approbations (événements filtrés par membre).
    `agents` (docs/BUBBLES.md §9.4) : `None` désactive `/v1/agents*` (404) — indépendant de `hub`
    (Crew peut exister sans fils/bulles), mais l'activation d'un `agent_draft` (côté `hub`) échoue si
    `agents` n'a pas aussi été posé sur `hub.agents_hub` par l'appelant (`serve()` le fait).
    `local_api` (Nokker Lokkal, `nokker/local/api.py`) : `None` (bêta) = aucune route `/v1/local/*` ; posé, chaque requête
    doit porter un `Host` autorisé (sinon 403, contre le DNS rebinding) et `/v1/local/*` exige le jeton local.
    `approval_suggestions` (docs/APPROVALS.md §7, `NokkerConfig.approval_suggestions`) : `enabled` faux ou absent =
    `/v1/approvals/suggestions*` en 404.
    `known_users` (docs/BUBBLES.md §9.2) : identités servies par `GET /v1/users`, indépendamment de
    `hub`/`agents` (liste de partage).
    `shares` (liens de conversation, nokker/threads/shares.py) : absent, un stockage en mémoire (dev, tests) dès que
    `hub` est posé ; `serve()` passe celui de Postgres.
    `memory_search_api` (docs/MCP-SERVER.md) : `POST /v1/memory/search` ; faux (défaut) = 404.

    **Multi-locataire** (docs/TENANCY.md §2-§3) : chaque requête authentifiée résout son expéditeur en locataire
    (`nokker.tenancy.resolve.resolve_tenant` : `us_…` par `tenancy`, interne par la config, sinon 403) et s'exécute
    dans `tenant_scope` — y compris le fil de travail du streaming. Les fils d'un testeur sont préfixés
    (`scoped_thread_id`) ; un objet d'un autre locataire répond 404. `tenancy` absent : `MemoryTenancy()` vide (aucun
    compte de testeur). Le résolveur de comptes est posé ici sur les invariants (GO), les approbations (décideur)
    et le hub (membres) s'ils n'en ont pas déjà un."""
    telegram_ids = telegram_ids or {}
    tenancy = tenancy if tenancy is not None else MemoryTenancy()
    if shares is None and hub is not None:
        shares = ThreadShares(MemoryShares())
    account_tenant = account_tenant_fn(tenancy)
    for target, attr in ((getattr(kernel, "invariants", None), "account_tenant"), (approvals, "account_tenant"),
                         (hub, "user_tenant")):
        if target is not None and hasattr(target, attr) and getattr(target, attr) is None:
            setattr(target, attr, account_tenant)
    internal_fixed = frozenset(known_users) | frozenset(api_keys.values()) | frozenset(telegram_ids.values())

    class _Internal:
        """Expéditeurs internes : config (clés d'API, `telegram_ids`, `known_users`) et utilisateurs autorisés des
        approbations (appareils des opérateurs internes)."""

        def __contains__(self, sender: object) -> bool:
            if not internal_tenant:
                return False  # bêta OVH : aucun expéditeur interne, quels que soient les défauts de la config
            return isinstance(sender, str) and (
                sender in internal_fixed or (approvals is not None and approvals.is_authorized(sender)))

    internal_senders = _Internal()

    def agent_tenant(a: Any) -> str:
        return (account_tenant(a.created_by) or "") if is_account(a.created_by) else INTERNAL_TENANT

    def agent_json(a: Any) -> dict[str, Any]:
        """La carte d'un agent ; avec la relève du courrier branchée, `mail_watch` (dernière relève, erreur, messages en
        attente) pour un agent `mail:<filtre>` — jamais d'identifiant."""
        out = agent_to_json(a)
        fn = getattr(agents, "mail_watch", None)
        watch = fn() if callable(fn) else None
        st = watch.describe(a) if watch is not None else None
        if st is not None:
            out["mail_watch"] = st
        return out

    def device_scope(device_id: str) -> str:
        """Locataire d'un appareil AVANT toute identité (défi de connexion, création de session) : celui du compte
        lié par l'identité `device` (onboarding), sinon l'interne — où un appareil de testeur n'est pas visible
        sous RLS."""
        acc = tenancy.account_for_identity("device", device_id)
        return acc.tenant_id if acc is not None else INTERNAL_TENANT

    session_threads = session_thread_by_sender or {}
    last_session: dict[str, str] = {}  # fil fixe -> dernier identifiant de session vu (mémoire du processus)
    miniapp_dir = miniapp_dir or MINIAPP_DIR
    executor = ApprovalExecutor(kernel=kernel, approvals=approvals) if approvals is not None else None
    if executor is not None and tenancy is not None:  # suppression d'un compte par la personne (« Mon compte »)
        from nokker.account import delete as account_delete

        def cancel_subscriptions(tenant_id: str) -> None:  # abonné : Stripe résilié AVANT toute suspension
            svc = billing_http.service()
            if svc is not None:
                svc.cancel_for(tenant_id)

        executor.account_actions[account_delete.ACTION] = account_delete.executor_action(tenancy, cancel_subscriptions)
    if executor is not None and hub is not None:
        executor.on_direct = lambda a, content, is_error: hub.direct_result(a.thread_id, a.id, a.tool_name, content,
                                                                            is_error, owner=a.decided_by)
        executor.on_result = lambda a, content, is_error, ms: hub.approval_result(
            a.thread_id, a.turn_index, a.call_id, a.id, a.tool_name, content, is_error, ms=ms, owner=a.decided_by)
    if executor is not None and local_api is not None:
        # Carte d'un sous-agent (§7.22) : outil et tour de suite avec le noyau de l'ENFANT, jamais celui du parent ;
        # enfant disparu → carte refusée. Lu à chaque carte (serve pose `local_api.subagents`).
        def _child_kernel(thread_id: str) -> Any:
            subagents = getattr(local_api, "subagents", None)
            return subagents.kernel_for_card(thread_id) if subagents is not None else None

        executor.subagent_kernel = _child_kernel
    elif executor is not None:
        # Sessions de code multi-utilisateur (§7.28) : l'enfant de la session du fil ; sans sessions, aucun (refus).
        def _code_child_kernel(thread_id: str) -> Any:
            code = code_http.service()
            return code.subagent_kernel(thread_id) if code is not None else None

        executor.subagent_kernel = _code_child_kernel
    # File par fil (docs/LOKKAL.md §7.4.9) : les verrous du noyau, ceux que prend `run_turn` (et `hub.thread_lock`).
    turn_locks = kernel.turn_locks if isinstance(getattr(kernel, "turn_locks", None), TurnLocks) else TurnLocks()
    cancel_epochs: dict[str, int] = {}  # fil racine -> nombre d'annulations (une suite en file d'avant est abandonnée)

    def schedule_decision(decided: Approval, *, continue_rejected: bool = False) -> tuple[str, threading.Event]:
        """Exécution d'une carte décidée EN FOND, dans la file de son fil : l'outil puis le tour de suite attendent la
        fin du tour en cours (celui qui a posé la carte, le plus souvent), jamais en parallèle. Rend `("none" | "queued" |
        "running", fin)` : `none` = rien ne tourne (refus, sauf `continue_rejected`) ; `queued` = un tour tourne ou
        attend sur le fil ; `running` = le travail part tout de suite. `fin` est posé quand tout est fini."""
        done = threading.Event()
        runs = executor is not None and (decided.status == "approved"
                                         or (continue_rejected and decided.status == "rejected"))
        if not runs:
            done.set()
            return "none", done
        lock = turn_locks.get(decided.thread_id)
        root = root_thread(decided.thread_id)
        epoch = cancel_epochs.get(root, 0)
        ticket = lock.reserve()

        def job() -> None:
            try:
                lock.acquire(ticket=ticket)
                try:
                    if cancel_epochs.get(root, 0) != epoch and decided.status == "approved":
                        # Fil annulé pendant que la suite attendait : l'outil n'est pas lancé (comme une carte retirée).
                        with tenant_scope(tenant_of_thread(decided.thread_id)):
                            a = approvals.mark_failed(decided.id, "annulé par l'utilisateur")
                        if decided.channel == DIRECT_CHANNEL and executor.on_direct is not None:
                            executor.on_direct(a, "annulé par l'utilisateur", True)
                        elif executor.on_result is not None:
                            executor.on_result(a, "annulé par l'utilisateur", True, None)
                        return
                    executor.run(decided)
                finally:
                    lock.release()
            except Exception:  # noqa: BLE001 — l'échec est sur la carte (mark_failed) ; la réponse est déjà partie
                logger.exception("ApprovalExecutor.run a levé pour l'approbation %s", decided.id)
            finally:
                done.set()

        try:
            threading.Thread(target=contextvars.copy_context().run, args=(job,), daemon=True,
                             name=f"approval-{decided.id[:12]}").start()
        except Exception:
            lock.cancel(ticket)  # jamais une place réservée sans travail derrière : le fil resterait bloqué
            done.set()
            raise
        return ("running" if ticket.immediate else "queued"), done
    if knock_hub is not None:
        # Heurtoir (docs/KNOCK.md §5.3) : une carte approuvée par toc toc toc s'exécute par le même chemin que les
        # autres décisions, dans la file de son fil.
        knock_hub.execute = lambda d: schedule_decision(d)[1]
    if household_http.enabled() and approvals is not None:
        # Foyer (X23) : les cartes fortes des profils mineurs, décidées par leur représentant depuis ses appareils ;
        # la suite tourne dans la file du fil du mineur, comme une décision ordinaire.
        from nokker.household.cards import GuardianCards

        household_http.configure(household_http.service(), approvals=GuardianCards(
            household=household_http.service(), approvals=approvals, to_json=approval_to_json,
            schedule=lambda d: schedule_decision(d, continue_rejected=True)[1], wait_s=DECISION_WAIT_S))
    crew_engine = getattr(agents, "engine", None) if agents is not None else None
    if executor is not None and crew_engine is not None:
        # Moteur Crew (X14) : la carte forte d'armement est un geste du noyau ; la suite d'un fil de passage tourne avec
        # le noyau du passage (ses portes, son budget, ses secrets).
        from nokker.agents.engine import ARM_TOOL

        executor.handlers[ARM_TOOL] = crew_engine.on_arm_decision
        executor.kernel_for = crew_engine.kernel_for
    if executor is not None and house_http.enabled():
        # Salle Maison : la suite d'une carte posée dans la salle tourne avec le noyau de salle de l'agent (ses outils,
        # ses portes) et sa réponse repart dans la salle ; tout autre fil : le résolveur d'avant.
        before_house = executor.kernel_for

        def _house_or(thread_id: str) -> Any:
            k = house_http.service().kernel_for(thread_id)
            return k if k is not None else (before_house(thread_id) if before_house is not None else None)

        executor.kernel_for = _house_or
    if executor is not None and plan_http.service() is not None and plan_http.service().delivery is not None:
        # Brief du matin par e-mail (§7.18.5) : la carte forte qui arme la livraison est un geste du noyau.
        from nokker.plan.delivery import DELIVERY_TOOL

        executor.handlers[DELIVERY_TOOL] = plan_http.service().delivery.on_arm_decision
    if executor is not None and local_api is not None and getattr(local_api, "pages", None) is not None:
        # Publier une page (§7.26) : geste du noyau sur carte forte, la version signée devient publique.
        from nokker.pages.public import PUBLISH_TOOL, publish_handler

        def page_published(meta: Any) -> None:
            if hub is not None:
                hub.page_changed(meta)
            if getattr(local_api, "mirror", None) is not None:
                local_api.mirror.wake()  # la copie (si l'utilisateur l'a choisie) part avec sa publication

        executor.handlers[PUBLISH_TOOL] = publish_handler(local_api.pages, after=page_published)
    if executor is not None and improve_config is not None:
        # Auto-amélioration (docs/SELF-IMPROVE.md) : la carte d'une variante est un geste du noyau ; approuvée, une
        # version de plus (calque d'outils ou de routeur, ou skill appris) ; jamais un bloc pendant le gel du format.
        from nokker.improve.proposal import IMPROVE_TOOL, improve_handler

        executor.handlers[IMPROVE_TOOL] = improve_handler(
            improve_config, kernel, lambda: getattr(learning_http.service(), "skills", None))

    def apply_local_decision(decided: Approval) -> None:
        """Une carte décidée pour la machine (jeton local, ou téléphone par nokker.ai, lot X10) : approuvée, l'exécuteur
        lance l'outil et la suite du tour, dans la file du fil. UN SEUL chemin pour les deux origines de la décision ;
        ici on attend la fin (la relève de nokker.ai, déjà dans son propre fil d'exécution, rend ensuite l'issue)."""
        schedule_decision(decided)[1].wait()

    if local_api is not None:
        local_api.apply_decision = apply_local_decision  # pour les décisions venues par nokker.ai (serve)
        if hub is not None:  # pages retirées par la modération (§7.26 point 6) : les bulles suivent
            local_api.page_changed = hub.page_changed

            def page_moderated(meta: Any) -> None:
                """Premier retrait vu : l'auteur est prévenu une fois (bulle `notify`, motif et recours)."""
                with tenant_scope(tenant_of_thread(meta.thread_id)):
                    hub.page_moderated(meta, fallback_user=LOCAL_OWNER)

            local_api.page_moderated = page_moderated

        def describe_local_session(thread_id: str) -> dict[str, Any]:
            """Champs ajoutés à `GET /v1/local/sessions` (§7.4.1) : titre du fil au hub (aucun avant le premier tour),
            date du dernier tour rangé, tour en cours ou en file sur le fil, cartes en attente du fil et de ses
            sous-agents `<fil>/sa-…`."""
            out: dict[str, Any] = {"working": turn_locks.get(thread_id).busy()}
            controls = getattr(kernel, "controls", None)
            if controls is not None:
                out["paused"] = controls.is_paused(thread_id, tenant_of_thread(thread_id), None)
            with tenant_scope(tenant_of_thread(thread_id)):
                t = hub.threads.get(thread_id) if hub is not None else None
                if t is not None:
                    out["title"] = t.title or None
                last = getattr(kernel.store, "last_turn_at", None)
                at = last(thread_id) if callable(last) else None
                if at is not None:
                    out["last_turn_at"] = at.isoformat()
                if approvals is not None:
                    out["pending_approvals"] = sum(
                        1 for a in approvals.pending()
                        if a.thread_id == thread_id or a.thread_id.startswith(thread_id + "/sa-"))
            return out

        local_api.describe_session = describe_local_session

    def cancel_thread(thread_id: str) -> int:
        """Arrête le tour en cours du fil, retire ses cartes en attente (une carte approuvée après l'annulation
        n'exécute plus rien) et abandonne les suites de décisions encore en file. Rend le nombre de cartes retirées."""
        root = root_thread(thread_id)
        cancel_epochs[root] = cancel_epochs.get(root, 0) + 1
        kernel.cancel(thread_id)
        n = 0
        if approvals is not None:
            for a in approvals.pending():
                if a.thread_id == thread_id or a.thread_id.startswith(thread_id + "/sa-"):
                    approvals.mark_failed(a.id, "annulé par l'utilisateur")
                    n += 1
        return n

    def thread_control(thread_id: str, op: str, body: dict[str, Any], account: str) -> tuple[int, dict[str, Any]]:
        """`POST /v1/threads/{id}/pause|resume|steer` (docs/CLIENT-API.md §Pause et steer), l'appelant ayant déjà prouvé
        que le fil est à lui. `account` : le compte de la pause globale qui peut encore tenir le fil après une reprise."""
        controls = getattr(kernel, "controls", None)
        if controls is None:
            return 404, {"error": {"message": "pause non prise en charge"}}
        tenant = tenant_of_thread(thread_id)
        if op == "pause":
            kernel.pause(thread_id)
            return 202, {"thread_id": thread_id, "paused": True, "running": controls.running(thread_id)}
        if op == "resume":
            was = kernel.resume(thread_id)
            return 200, {"thread_id": thread_id, "was_paused": was,
                         "paused": controls.is_paused(thread_id, tenant, account)}
        if not getattr(kernel, "steer_enabled", False):
            return 404, {"error": {"message": "steer désactivé", "code": "steer_disabled"}}
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            return 400, {"error": {"message": "text : chaîne non vide attendue"}}
        if len(text) > STEER_MAX_CHARS:
            return 400, {"error": {"message": f"text : {STEER_MAX_CHARS} caractères au plus"}}
        steer = kernel.steer(thread_id, text)
        if steer is None:  # choix documenté : pas de bascule en message normal, le client l'envoie lui-même
            return 409, {"error": {"message": "aucun tour en cours sur ce fil", "code": "no_running_turn"}}
        return 202, {"thread_id": thread_id, "steer_id": steer.id, "status": "queued",
                     "paused": controls.is_paused(thread_id, tenant, account)}

    def account_control(op: str, tenant: str, account: str) -> tuple[int, dict[str, Any]]:
        """`POST /v1/pause|resume` : tous les fils du compte (`ANY_ACCOUNT` = tout le locataire, la machine Lokkal)."""
        if getattr(kernel, "controls", None) is None:
            return 404, {"error": {"message": "pause non prise en charge"}}
        if op == "pause":
            kernel.pause_account(tenant, account)
            return 202, {"scope": "account", "paused": True}
        return 200, {"scope": "account", "paused": False, "was_paused": kernel.resume_account(tenant, account)}

    class Handler(BaseHTTPRequestHandler):
        server_version = "nokker/0"

        def log_message(self, fmt: str, *args: Any) -> None:
            # Jetons d'URL masqués (invitation `?t=`, vu dans les journaux OVH le 21.10) : un journal n'est pas un coffre.
            # Derrière le tunnel Cloudflare, le pair est cloudflared : l'IP du visiteur est dans CF-Connecting-IP (posée
            # par Cloudflare ; sur OVH aucun autre chemin n'atteint le pod). Les deux sont journalisées.
            peer = self.client_address[0]
            visitor = (getattr(self, "headers", None) or {}).get("CF-Connecting-IP")
            who = f"{visitor} (via {peer})" if visitor else peer
            logger.info("%s - %s", who, _RE_LOG_DEVICE_TOKEN.sub(r"\1***", _RE_LOG_SECRET.sub(r"\1***", fmt % args)))

        def parse_request(self) -> bool:
            if not super().parse_request():
                return False
            if local_api is not None and not local_api.host_ok(self.headers.get("Host")):
                self._json(403, {"error": {"message": "host refusé"}})
                return False
            if local_api is not None and self._via_relay() \
                    and local_api.is_owner(self.headers.get("Authorization")):
                # Le jeton local ne sort jamais de la machine : présenté par la passerelle, il est refusé (§7.5).
                self._json(403, {"error": {"message": "jeton local refusé par la passerelle"}})
                return False
            if local_api is not None and self._via_relay() and not local_api.delegation_allows(
                    self.command, urlparse(self.path).path, self.headers.get("X-Session-Id"),
                    self.headers.get("Authorization")):
                # Jeton de délégation de nokker.ai (X21) : les routes de SA session seulement, rien d'autre de la machine.
                self._json(403, {"error": {"message": "délégation : routes de la session déléguée seulement"}})
                return False
            return True

        def _local_route(self, method: str, path: str) -> bool:
            """Routes `/v1/local/*` (Nokker Lokkal) ; True si la requête a reçu sa réponse."""
            if self._glance_gate(method, path):  # jeton glance (lunettes) : 403 hors de GET /v1/glance
                return True
            if ambient_http.is_glance_path(path):  # vue glance : appairage et appareils du titulaire
                if ambient_http.handle_glance_claim(self, method, path):
                    return True
                if path == "/v1/glance":
                    self._json(401, {"error": {"code": "glance_token_required", "message": "jeton glance requis"}})
                elif local_api is None:
                    self._account_route(method, path)
                elif not self._local_owner():
                    self._json(401, {"error": {"message": "jeton local requis"}})
                else:
                    ambient_http.handle_glance_owner(self, method, path, LOCAL_OWNER, INTERNAL_TENANT, None)
                return True
            if speak_lexicon_mod.is_path(path):  # lexique de prononciation du foyer (docs/VOICE.md §6)
                if local_api is None:
                    self._account_route(method, path)
                elif not self._local_owner():
                    self._json(401, {"error": {"message": "jeton local requis"}})
                else:
                    from nokker.ambient.displays import Membership

                    speak_lexicon_mod.handle_member(self, method, path, Membership(
                        account_id="local", household_id="local", household_tenant=INTERNAL_TENANT, profile="adult"),
                        ambient_http._body, ambient_http._json)
                return True
            if ambient_http.is_device_path(path):  # écrans e-ink : jeton d'appareil (cloud et Lokkal), drapeau `ambient`
                ambient_http.handle_device(self, method, path)
                return True
            if ambient_http.is_ambient_path(path):  # /v1/ambient/* : membre du foyer (Lokkal : propriétaire)
                if local_api is None:
                    self._account_route(method, path)
                elif not self._local_owner():
                    self._json(401, {"error": {"message": "jeton local requis"}})
                else:
                    from nokker.ambient.displays import Membership

                    ambient_http.handle_member(self, method, path, Membership(
                        account_id="local", household_id="local", household_tenant=INTERNAL_TENANT, profile="adult"))
                return True
            if local_api is not None and path.startswith("/v1/pages/public/"):
                self._public_page_route(method, path)
                return True
            if local_api is not None and path.startswith("/v1/pages/"):
                self._page_route(method, path)
                return True
            m = _RE_THREAD_CANCEL.match(path.replace("%2F", "/").replace("%2f", "/")) \
                if local_api is not None and method == "POST" else None
            if m:
                if not self._local_owner():
                    self._json(401, {"error": {"message": "jeton local requis"}})
                    return True
                controls = getattr(kernel, "controls", None)
                running = bool(controls is not None and controls.running(m.group(1)))
                body = {"cancelled": m.group(1), "approvals_cancelled": cancel_thread(m.group(1)), "running": running}
                if getattr(local_api, "subagents", None) is not None:
                    # ses sous-agents (fond et premier plan) : un noyau chacun, annulé à part
                    body["subagents_cancelled"] = local_api.subagents.cancel_children(m.group(1))
                self._json(202, body)
                return True
            m = _RE_THREAD_CONTROL.match(path.replace("%2F", "/").replace("%2f", "/")) \
                if local_api is not None and method == "POST" else None
            if m or (local_api is not None and method == "POST" and path in ("/v1/pause", "/v1/resume")):
                if not self._local_owner():
                    self._json(401, {"error": {"message": "jeton local requis"}})
                    return True
                if m:  # la machine a un seul propriétaire : tous ses fils sont à lui
                    self._json(*thread_control(m.group(1), m.group(2), self._read_json_body(), ANY_ACCOUNT))
                else:
                    self._json(*account_control(path.rsplit("/", 1)[1], INTERNAL_TENANT, ANY_ACCOUNT))
                return True
            if local_api is not None and learning_http.enabled() and learning_http.is_learning_path(path):
                # Apprentissage (docs/LEARNING.md §6) : le propriétaire de la machine ; par la passerelle, lecture seule.
                if not self._local_owner():
                    self._json(401, {"error": {"message": "jeton local requis"}})
                    return True
                from nokker.learning.wiring import LOCAL_ACCOUNT

                out = learning_http.dispatch(method, self.path, LOCAL_ACCOUNT, self._read_json_body,
                                             writes=not self._via_relay())
                self._json(*(out or (404, {"error": {"message": "not found"}})))
                return True
            if local_api is None or not path.startswith("/v1/local/"):
                return False
            m = _RE_LOCAL_EXPORT.match(path) if method == "GET" else None
            if m:
                self._local_export(unquote(m.group(1)))
                return True
            if path.startswith("/v1/local/preview/") or path.startswith("/v1/local/processes"):
                self._process_route(method, path)
                return True
            if path.startswith("/v1/local/approvals") and self._local_owner():
                self._local_approvals(method, path)
                return True
            if path == "/v1/local/tools/call" and method == "POST":
                self._local_tool_call()
                return True
            if path == "/v1/local/cost" and method == "GET":
                self._local_cost()
                return True
            if path == "/v1/local/memory" and method == "GET":
                # État de la mémoire longue (X1b, docs/LOKKAL.md §7.13) : contrôle « mémoire » de `nokker doctor`.
                if not self._local_owner():
                    self._json(401, {"error": {"message": "jeton local requis"}})
                    return True
                self._json(200, memory_status(kernel.memory))
                return True
            body = self._read_json_body() if method in ("POST", "PUT") else {}
            out = local_api.handle(method, path, self.headers.get("Authorization"), body,
                                   relayed=self._via_relay())
            self._json(*(out or (404, {"error": {"message": "not found"}})))
            return True

        def _public_page_route(self, method: str, path: str) -> None:
            """`GET /v1/pages/public/{public_id}` (docs/LOKKAL.md §7.26) : la version PUBLIÉE d'une page publique, sans
            preuve (c'est le principe) mais par son id public seulement, jamais par son `page_id` ; en-têtes stricts.
            Le relais l'appelle pour `<nom>--pub.nokker.app/<public_id>` et pose les siens par-dessus."""
            from nokker.pages.store import PUBLIC_PAGE_HEADERS

            m = _RE_PUBLIC_PAGE.match(path)
            meta = local_api.pages.by_public_id(m.group(1)) if m and method in ("GET", "HEAD") else None
            found = local_api.pages.read(meta.page_id, meta.public_version) if meta is not None else None
            if found is None:
                self._json(404, {"error": {"message": "not found"}})
                return
            body = found[1]
            self.send_response(200)
            for k, v in PUBLIC_PAGE_HEADERS.items():
                self.send_header(k, v)
            if not meta.public_index:
                self.send_header("X-Robots-Tag", "noindex, nofollow")
            self.send_header("X-Nokker-Index", "1" if meta.public_index else "0")  # lu par le relais
            # lu par le relais pour le registre de modération de nokker.ai (§7.26 point 6), jamais transmis au navigateur
            self.send_header("X-Nokker-Page-Id", meta.page_id)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if method == "GET":
                self.wfile.write(body)

        def _page_unpublished(self, meta: Any) -> str:
            """Après dépublication (ou épingle `local`) : la copie publique part tout de suite chez nokker.ai, la bulle
            suit. Rend l'état du retrait : `done` | `pending` (refait par le miroir) | `none` (machine non liée)."""
            mirror = getattr(local_api, "mirror", None)
            if mirror is not None:
                purge = mirror.purge_public(meta.page_id)
            elif local_api.link is not None:
                purge = local_api.link.purge_public(meta.page_id)
            else:
                purge = "none"
            if hub is not None:
                hub.page_changed(meta)
            return purge

        def _page_publication(self, op: str, meta: Any) -> None:
            """`POST /v1/pages/{id}/publish {index?}` → 202 carte forte ; `POST …/unpublish` → 200 tout de suite ;
            `PUT …/locality {locality: "local"|null}` (docs/LOKKAL.md §7.26). Propriétaire : jeton local, ou compte lié
            par la passerelle ; le jeton de délégation n'y arrive jamais (ses routes sont bornées à sa session)."""
            from nokker.pages.public import request_publish
            from nokker.pages.store import MODERATED_MESSAGE, PageError

            if not self._local_owner():
                self._json(401, {"error": {"message": "jeton local requis"}})
                return
            body = self._read_json_body()
            store = local_api.pages
            if op == "/unpublish":
                meta = store.unpublish(meta.page_id)
                self._json(200, {"page_id": meta.page_id, "public": False, "purge": self._page_unpublished(meta)})
                return
            if op == "/locality":
                loc = body.get("locality") if isinstance(body, dict) else "?"
                if loc not in ("local", None):
                    self._json(400, {"error": {"message": "corps {locality: \"local\" | null} requis"}})
                    return
                was_public = meta.public
                meta = store.set_locality(meta.page_id, loc == "local")
                purge = "none"
                if loc == "local":
                    mirror = getattr(local_api, "mirror", None)
                    if mirror is not None:
                        mirror.set_page_mirror(meta.page_id, False)  # la copie privée part aussi
                    if was_public:
                        purge = self._page_unpublished(meta)  # copie publique retirée, bulle prévenue
                    elif hub is not None:
                        hub.page_changed(meta)
                elif hub is not None:
                    hub.page_changed(meta)
                self._json(200, {"page_id": meta.page_id, "locality": meta.locality, "public": meta.public,
                                 "purge": purge})
                return
            # publish
            if meta.moderated_at is not None:
                self._json(403, {"error": {"code": "page_moderated", "message": MODERATED_MESSAGE}})
                return
            if meta.locality == "local":
                self._json(409, {"error": {"code": "local_page",
                                           "message": "page épinglée « local » : elle ne quitte pas la machine"}})
                return
            if approvals is None:
                self._json(503, {"error": {"message": "approbations désactivées : publier exige une carte forte"}})
                return
            link = local_api.link
            relay_host = local_api.setup.state.get("relay_host") if local_api.setup is not None else None
            if link is None or not relay_host:
                self._json(409, {"error": {"code": "not_linked",
                                           "message": "publier passe par la passerelle : lie d'abord la machine"}})
                return
            allowed = link.pages_public(ttl_s=0.0)  # relu : droit de publier ET pages retirées (§7.26 point 6)
            local_api.sync_moderation()
            if store.get(meta.page_id) is not None and store.get(meta.page_id).moderated_at is not None:
                self._json(403, {"error": {"code": "page_moderated", "message": MODERATED_MESSAGE}})
                return
            if not allowed:
                self._json(403, {"error": {"code": "pages_public_not_allowed",
                                           "message": "pages publiques réservées aux abonnés et comptes vérifiés"}})
                return
            index = body.get("index", False) if isinstance(body, dict) else False
            if not isinstance(index, bool):
                self._json(400, {"error": {"message": "index : booléen"}})
                return
            try:
                with tenant_scope(tenant_of_thread(meta.thread_id)):
                    card = request_publish(approvals, store, meta, index=index, by=LOCAL_OWNER)
            except PageError as e:
                self._json(409, {"error": {"code": "local_page", "message": str(e)}})
                return
            self._json(202, {"approval_id": card.id, "strength": card.strength, "rule": card.rule,
                             "version": card.arguments.get("version")})

        def _page_route(self, method: str, path: str) -> None:
            """`GET /v1/pages/{id}` (auth locale, OU lien signé, OU passerelle avec le compte prouvé ; sinon 404) et
            `POST /v1/pages/{id}/link` (propriétaire) — docs/BUBBLES.md §10.3 ; publication : `_page_publication`.
            Une page publique ne se sert JAMAIS ici par son `page_id` : seulement par son id public (§7.26)."""
            from nokker.pages.store import PAGE_HEADERS

            m = _RE_PAGE.match(path)
            store = local_api.pages
            meta = store.get(m.group(1)) if m else None
            if meta is None:
                self._json(404, {"error": {"message": "not found"}})
                return
            op = m.group(2)
            if op in ("/publish", "/unpublish", "/locality"):
                if (op == "/locality") != (method == "PUT") or (op != "/locality" and method != "POST"):
                    self._json(404, {"error": {"message": "not found"}})
                    return
                self._page_publication(op, meta)
                return
            if method == "DELETE" and not op:
                # Supprimer une page (propriétaire local) : toutes ses versions ; sa copie chez nokker.ai, s'il y en a
                # une, est purgée par le miroir tout de suite (§7.23).
                if not self._local_owner():
                    self._json(401, {"error": {"message": "jeton local requis"}})
                    return
                was_public = meta.public
                store.delete(meta.page_id)
                if was_public:  # la copie publique part tout de suite, avant même le passage du miroir
                    self._page_unpublished(meta)
                if getattr(local_api, "mirror", None) is not None:
                    local_api.mirror.wake()
                self._json(200, {"deleted": True, "page_id": meta.page_id})
                return
            if op:
                if method != "POST":
                    self._json(404, {"error": {"message": "not found"}})
                elif not self._local_owner():
                    self._json(401, {"error": {"message": "jeton local requis"}})
                else:
                    body = self._read_json_body()
                    download = bool(body.get("download"))
                    if self._via_relay():
                        # Interface servie par la passerelle : lien d'entrée sur <nom>--pages.nokker.app, signé par
                        # nokker.ai à la demande de cette machine (même route pour le client, en local comme à distance).
                        got = local_api.link.pages_token(meta.page_id, bool(body.get("embed")), download) \
                            if local_api.link is not None else None
                    else:
                        url, exp = store.link(meta.page_id, download=download)
                        got = {"url": url, "expires_at": exp}
                    relay_host = (local_api.setup.state.get("relay_host") if local_api.setup is not None else None)
                    from nokker.pages.public import public_url

                    pub = public_url(relay_host, meta)
                    if got and pub:  # « Copier le lien » : l'URL PUBLIQUE, une autre origine et un autre id (§7.26)
                        got = {**got, "public_url": pub, "public_version": meta.public_version}
                    if got and meta.moderated_at is not None:  # « retirée par la modération » (§7.26 point 6)
                        got = {**got, "moderated_at": meta.moderated_at}
                        if meta.moderation is not None:  # exposé des motifs : {at, reason, appeal}
                            got["moderation"] = dict(meta.moderation)
                    self._json(200, got) if got else self._json(502, {"error": {"message": "nokker.ai injoignable"}})
                return
            if method != "GET":
                self._json(404, {"error": {"message": "not found"}})
                return
            q = parse_qs(urlparse(self.path).query)
            exp, sig = (q.get("exp") or [""])[0], (q.get("sig") or [""])[0]
            tunnel = getattr(local_api, "tunnel", None)
            # Par la passerelle : le relais a vérifié le cookie de pages de CE compte et le dit dans X-Nokker-Account,
            # cru seulement si la requête arrive par notre tunnel (X-Nokker-Tunnel = son secret de processus).
            relayed = (tunnel is not None and self.headers.get("X-Nokker-Pages") == "1"
                       and bool(self.headers.get("X-Nokker-Account"))
                       and hmac.compare_digest(self.headers.get("X-Nokker-Tunnel") or "", getattr(tunnel, "secret", "")))
            download = (q.get("dl") or [""])[0] == "1"
            if not (relayed or (exp and sig and store.verify(meta.page_id, exp, sig, download)) or self._local_owner()):
                self._json(404, {"error": {"message": "not found"}})  # une page privée n'existe pas pour un inconnu
                return
            try:
                version = int((q.get("v") or ["0"])[0]) or None
            except ValueError:
                version = None
            found = store.read(meta.page_id, version)
            if found is None:
                self._json(404, {"error": {"message": "not found"}})
                return
            body = found[1]
            self.send_response(200)
            for k, v in PAGE_HEADERS.items():
                self.send_header(k, v)
            if download:  # « Télécharger » : la page est autonome (§10.1), elle marche hors ligne telle quelle
                from urllib.parse import quote as _q

                from nokker.pages.store import download_name

                name = download_name(found[0])
                ascii_name = name.encode("ascii", "replace").decode().replace("?", "_").replace('"', "")
                self.send_header("Content-Disposition",
                                 f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{_q(name)}")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _process_route(self, method: str, path: str) -> None:
            """Commandes en arrière-plan (lot X6, docs/LOKKAL.md §7.7) : `GET /v1/local/processes` (propriétaire),
            `POST /v1/local/processes/{id}/link` (propriétaire) → lien signé de l'aperçu du port, et
            `GET /v1/local/preview/{id}/{exp}.{sig}/…` : la requête est rejouée DANS le bac à sable du processus
            (aucun port publié sur l'hôte), servie avec la CSP `sandbox` des pages. En local seulement : par la
            passerelle, l'aperçu n'existe pas (404), comme une page privée pour un inconnu."""
            from nokker.pages.store import PAGE_HEADERS
            from nokker.tools.processes import ProcessError

            relayed = self._via_relay()
            if path == "/v1/local/processes" and method == "GET":
                if not self._local_owner():
                    self._json(401, {"error": {"message": "jeton local requis"}})
                else:
                    self._json(200, {"processes": local_api.code.processes()})
                return
            m = _RE_PROC_LINK.match(path)
            if m and method == "POST":
                found = local_api.code.find_process(m.group(1)) if not relayed else None
                if not self._local_owner():
                    self._json(401, {"error": {"message": "jeton local requis"}})
                elif found is None or not found[1].port or found[1].state != "running":
                    self._json(404, {"error": {"message": "pas d'aperçu pour ce processus"}})
                else:
                    url, exp = local_api.preview_signer.link(m.group(1))
                    self._json(200, {"url": url, "expires_at": exp, "port": found[1].port})
                return
            m = _RE_PREVIEW.match(path)
            if not m or method not in ("GET", "HEAD") or relayed \
                    or not local_api.preview_signer.verify(m.group(1), m.group(2)):
                self._json(404, {"error": {"message": "not found"}})
                return
            found = local_api.code.find_process(m.group(1))
            if found is None:
                self._json(404, {"error": {"message": "not found"}})
                return
            tid, proc, src = found
            query = urlparse(self.path).query
            upstream = (m.group(3) or "/") + (f"?{query}" if query else "")
            try:
                status, ctype, body, location = src.process_table(tid).fetch(proc.id, upstream, method)
            except ProcessError as e:
                self._json(502, {"error": {"message": str(e)}})
                return
            base = f"/v1/local/preview/{m.group(1)}/{m.group(2)}"
            self.send_response(status)
            for k, v in PAGE_HEADERS.items():
                if k != "Content-Type":
                    self.send_header(k, v)
            self.send_header("Content-Type", re.sub(r"[\r\n]", "", ctype)[:200])
            if location:  # redirection de l'appli : gardée sous le préfixe signé, jamais vers une autre origine
                loc = re.sub(r"^https?://(127\.0\.0\.1|localhost)(:\d+)?", "", location.strip())
                if loc.startswith("/") and not loc.startswith("//") and not re.search(r"[\r\n]", loc):
                    self.send_header("Location", base + loc)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if method == "GET":
                self.wfile.write(body)

        def _local_tool_call(self) -> None:
            """Appel d'outil du propriétaire, sans tour modèle (docs/LOKKAL.md §7.4.4) : `/diff`, `/rollback`, `/skills`,
            `/plan` du client terminal. Liste blanche, fil de session locale seulement, mêmes portes que la boucle."""
            if not self._local_owner():
                self._json(401, {"error": {"message": "jeton local requis"}})
                return
            body = self._read_json_body()
            thread_id, name = str(body.get("thread_id") or ""), str(body.get("name") or "")
            arguments = body.get("arguments") or {}
            if thread_id not in local_api.code.sessions():
                self._json(404, {"error": {"message": "pas de session locale sur ce fil"}})
                return
            direct = DIRECT_TOOLS | ({"memory"} if local_api.project_memory is not None else set()) \
                | ({"delegate_status", "delegate_cancel"} if getattr(local_api, "subagents", None) is not None else set()) \
                | ({"skill_history", "skill_revert"} if getattr(getattr(local_api, "learning", None), "skills", None)
                   is not None else set()) \
                | ({"lsp_diagnostics"} if getattr(local_api.code, "lsp", None) is not None else set())
            if name not in direct:
                self._json(403, {"error": {"message": f"outil non appelable directement : {name}"}})
                return
            if not isinstance(arguments, dict) or (name == "todo" and arguments):
                self._json(400, {"error": {"message": "arguments invalides (todo : lecture seule, sans argument)"}})
                return
            if name == "delegate_status" and arguments.get("wait_s"):  # le verrou du fil est tenu pendant l'appel
                self._json(400, {"error": {"message": "delegate_status hors tour : sans wait_s"}})
                return
            lock = hub.thread_lock(thread_id) if hub is not None else None
            if lock is not None and not lock.acquire(blocking=False):
                self._json(409, {"error": {"message": "un tour est en cours sur ce fil"}})
                return
            try:
                with tenant_scope(tenant_of_thread(thread_id)):
                    out = kernel.direct_call(thread_id, name, arguments, sender=LOCAL_OWNER)
            finally:
                if lock is not None:
                    lock.release()
            if isinstance(out, str):
                self._json(202, {"approval_id": out})
            else:
                self._json(200, direct_result_body(name, out))

        def _code_tool_call(self, thread_id: str, name: str, arguments: dict[str, Any], user: str) -> Any:
            """Appel d'outil hors tour d'une session de code (§7.28, routes de nokker/code_sessions/http.py) : mêmes
            portes que la boucle, sous le verrou du fil ; None si un tour est en cours."""
            lock = hub.thread_lock(thread_id) if hub is not None else None
            if lock is not None and not lock.acquire(blocking=False):
                return None
            try:
                return kernel.direct_call(thread_id, name, arguments, sender=user)
            finally:
                if lock is not None:
                    lock.release()

        def _local_cost(self) -> None:
            """Totaux de coût de la session et du jour (X2, docs/LOKKAL.md §7.8) : lecture seule du ledger, aucun outil,
            aucun tour modèle. `?thread_id=` (facultatif : sans lui, le jour seul) et `?day=AAAA-MM-JJ` (UTC, défaut
            aujourd'hui)."""
            from datetime import date as _date

            from nokker.ledger.turn_cost import ledger_totals

            if not self._local_owner():
                self._json(401, {"error": {"message": "jeton local requis"}})
                return
            if ledger is None:
                self._json(404, {"error": {"message": "ledger non configuré"}})
                return
            q = parse_qs(urlparse(self.path).query)
            thread_id = (q.get("thread_id") or [""])[0]
            raw_day = (q.get("day") or [""])[0]
            try:
                day = _date.fromisoformat(raw_day) if raw_day else datetime.now(timezone.utc).date()
            except ValueError:
                self._json(400, {"error": {"message": "day : AAAA-MM-JJ attendu"}})
                return
            prices = getattr(ledger, "prices", None)
            with tenant_scope(tenant_of_thread(thread_id) if thread_id else INTERNAL_TENANT):
                session = ({"thread_id": thread_id, **ledger_totals(ledger, prices, thread_id=thread_id)}
                           if thread_id else None)
                today = {"date": day.isoformat(), **ledger_totals(ledger, prices, day=day)}
            self._json(200, {"session": session, "day": today})

        def _local_owner(self) -> bool:
            """Le propriétaire de la machine : jeton local (sur la machine), ou jeton signé par nokker.ai arrivé par la
            passerelle (§7.5)."""
            auth = self.headers.get("Authorization")
            if self._via_relay():
                return local_api.relayed_owner(auth) is not None
            return local_api.is_owner(auth)

        def _local_approvals(self, method: str, path: str) -> None:
            """Cartes des sessions locales, décidées avec le jeton local (docs/LOKKAL.md §7.4.3). Le propriétaire local
            est le locataire interne : les approbations sont en Postgres sous RLS, le locataire se fixe ICI, à la
            frontière (sans lui : NoTenant → 502 sur GET et decide, install neuve rc.9.3, 04.11.2026)."""
            with tenant_scope(INTERNAL_TENANT):
                self._local_approvals_scoped(method, path)

        def _local_approvals_scoped(self, method: str, path: str) -> None:
            from nokker.local.approvals import append_user_rule, rule_kind, suggested_rules
            from nokker.qa.permissions import RELAY_CARD_RULE, RELAYED_STRONG_RULE, Rule

            if approvals is None:
                self._json(404, {"error": {"message": "approbations désactivées"}})
                return
            if path == "/v1/local/approvals" and method == "GET":
                # `forwarded` (X10) : carte forte déposée chez nokker.ai, le lien est parti au téléphone ; faux sinon
                # (machine non liée, relais éteint, dépôt pas encore fait) : elle se décide au terminal. Un dépôt
                # plus tard publie `approval.forwarded`.
                relay = local_api.remote_approvals if local_api is not None else None
                self._json(200, {"approvals": [
                    {**approval_to_json(a),
                     "suggested_rules": (rules := suggested_rules(a.tool_name, a.arguments, a.strength, a.rule)),
                     # libellé juste côté client (01.11) : `tool` | `file` | `dir` | `prefix` | `exact`, même ordre
                     "suggested_rule_kinds": [rule_kind(r) for r in rules],
                     "forwarded": a.strength == "strong" and relay is not None and relay.forwarded(a.id)}
                    for a in approvals.pending()]})
                return
            m = re.match(r"^/v1/local/approvals/([^/]+)/decide$", path)
            if not (m and method == "POST"):
                self._json(404, {"error": {"message": "not found"}})
                return
            body = self._read_json_body()
            decision, scope, rule = str(body.get("decision") or ""), str(body.get("scope") or "once"), body.get("rule")
            card = approvals.get(m.group(1))
            if card is None:
                self._json(404, {"error": {"message": "carte inconnue"}})
                return
            from nokker.tools.ssh import is_prod_card

            from nokker.pages.public import is_publish_card

            if is_publish_card(card.rule) and decision == "approve":
                # Publier une page (§7.26) : une sortie vers le public, signée à la clé d'appareil (app.nokker.ai,
                # téléphone, ou POST /v1/approvals/{id}/decision) ; jamais au jeton local seul.
                self._json(403, {"error": {"code": "device_key_required",
                                           "message": "publication de page : signe-la avec ta clé d'appareil"}})
                return
            if is_prod_card(card.rule) and decision == "approve":
                # Plancher prod (lot X11, docs/LOKKAL.md §7.14) : le jeton local est un fichier de la machine, que le
                # modèle peut lire en backend local ; approuver une commande à effet sur la prod se signe à la clé
                # d'appareil (refuser reste possible au jeton local).
                self._json(403, {"error": {"message": "carte prod : signe-la avec ta clé d'appareil "
                                                      "(POST /v1/approvals/{id}/decision, docs/APPROVALS.md)",
                                           "code": "device_key_required"}})
                return
            if card.rule == RELAYED_STRONG_RULE and decision == "approve":
                # Tour venu du web (X10) : la carte forte se signe à la clé d'appareil (téléphone), jamais au jeton
                # local, que le modèle peut lire en backend local. Refuser reste possible.
                self._json(403, {"error": {"code": "device_key_required",
                                           "message": "action demandée depuis le web : signe la carte sur ton "
                                                      "téléphone (app.nokker.ai)"}})
                return
            if card.strength == "strong" and self._via_relay():
                # À distance, une carte forte se signe avec la clé d'appareil (app.nokker.ai, téléphone), §7.3.
                self._json(403, {"error": {"message": "carte forte : décide-la sur app.nokker.ai ou ton téléphone"}})
                return
            if card.strength == "strong" and card.rule == RELAY_CARD_RULE and decision == "approve":
                # Envoi externe ou destructif d'un compte nokker.ai (X20b) : nokker.ai n'exécute que sur une carte
                # signée par un appareil du compte ; approuvée ici, elle serait brûlée sans rien envoyer. Refuser reste
                # possible en local.
                self._json(403, {"error": {"code": "strong_approval_required",
                                           "message": "envoi externe d'un compte nokker.ai : à signer sur ton "
                                                      "téléphone (app.nokker.ai) ; la carte attend"}})
                return
            allowed = suggested_rules(card.tool_name, card.arguments, card.strength, card.rule)
            if scope not in ("once", "session", "always") or (scope != "once" and rule not in allowed):
                self._json(400, {"error": {"message": "portée ou règle invalide (choisir parmi suggested_rules)"}})
                return
            try:
                decided = approvals.decide_local(card.id, decision, LOCAL_OWNER)
            except (ProofInvalid, ApprovalNotFound, ApprovalExpired, ApprovalNotPending) as e:
                self._json(409, {"error": {"message": str(e)}})
                return
            perms = getattr(kernel.gates, "permissions", None)
            if decision == "approve" and scope != "once" and perms is not None:
                if scope == "session":
                    perms.allow_for_thread(card.thread_id, Rule.parse(rule))
                else:
                    append_user_rule(local_api.settings_path, rule)
                    if Rule.parse(rule) not in perms.allow:
                        perms.allow.append(Rule.parse(rule))
            # Réponse tout de suite (§7.4.9) : l'outil et le tour de suite tournent en fond, dans la file du fil, et se
            # suivent par les événements (`tool.result` de l'appel en attente, `turn.*` de la suite).
            continuation, _ = schedule_decision(decided)
            final = approvals.get(decided.id) or decided
            self._json(202 if continuation != "none" else 200,
                       {**approval_to_json(final), "approval_id": decided.id, "continuation": continuation,
                        "scope": scope, "rule": rule if scope != "once" else None})

        def _begin_stream(self, status: int, content_type: str = "text/event-stream") -> None:
            """En-têtes d'une réponse en flux (SSE, inférence relayée). Le serveur parle HTTP/1.0 par défaut : un
            client Go (cloudflared devant app.nokker.ai/v1) ignore alors `Transfer-Encoding: chunked`, lit le cadrage
            comme du corps et attend la fermeture — que `Connection: keep-alive` n'envoie jamais (le bouton Envoyer
            du web restait grisé jusqu'à la coupure d'inactivité de Cloudflare). Ici : HTTP/1.1 chunked, connexion
            fermée après le dernier morceau."""
            self.protocol_version = "HTTP/1.1"
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("X-Accel-Buffering", "no")  # un nginx devant le noyau ne doit pas retenir le flux

        def _write_chunk(self, data: bytes) -> None:
            self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")

        def _sse(self, obj: dict[str, Any]) -> None:
            self._write_chunk(f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode())

        def _json(self, status: int, obj: dict[str, Any]) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authenticate(self) -> tuple[bool, str | None]:
            """(autorisé, sender). `sender` ne vient JAMAIS du corps/d'un en-tête arbitraire — SEULEMENT
            de la clé Bearer présentée, via `api_keys` (secret que seul le vrai appelant détient)."""
            if not api_keys and open_api:
                return True, None  # serveur ouvert (dev, `dev_open_api`) : aucune identité de confiance
            auth = self.headers.get("Authorization", "")
            token = auth[7:].strip() if auth.startswith("Bearer ") else ""
            sender = api_keys.get(token)
            if sender is None and token and local_api is not None and self._via_relay() \
                    and local_api.relayed_owner(auth) is not None:
                sender = LOCAL_OWNER  # le propriétaire, par la passerelle, avec un jeton signé par nokker.ai (§7.5)
            if sender is None and token and approvals is not None:
                # Client web/CLI/app : jeton de SESSION d'un appareil enrôlé ET confirmé (POST /v1/sessions), même
                # preuve que pour /v1/threads et /v1/bubbles.
                session = approvals.verify_session(token)
                if session is not None:
                    sender = session.user
            return (sender is not None), sender

        def _read_json_body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b""
            if not raw:
                return {}
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return {}

        def _via_relay(self) -> bool:
            """Requête venue du WEB par la passerelle : le relais le dit (`X-Nokker-Relay`), ou elle est arrivée par
            notre tunnel (`X-Nokker-Tunnel` = son secret de processus, posé par nokker/local/tunnel.py sur CHAQUE
            requête relayée). Le second ne dépend pas du relais : un en-tête perdu ne rend jamais un tour web « local »."""
            if self.headers.get("X-Nokker-Relay"):
                return True
            tunnel = getattr(local_api, "tunnel", None) if local_api is not None else None
            secret = getattr(tunnel, "secret", "") if tunnel is not None else ""
            return bool(secret) and hmac.compare_digest(self.headers.get("X-Nokker-Tunnel") or "", secret)

        def _resolve_identity(self, body: dict[str, Any] | None = None) -> str | None:
            """Identité vérifiée pour `/v1/approvals*`, `/v1/devices*`, `/v1/sessions` — trois preuves
            possibles : (1) clé Bearer statique (`api_keys`) ; (2) jeton de SESSION
            (`POST /v1/sessions`, `approvals.verify_session`) ; (3) `initData` Telegram validé — en-tête
            `X-Telegram-Init-Data` (la Mini App le pose sur CHAQUE appel, GET et POST) ou champ
            `init_data` du corps JSON, résolu en `sender` via `NokkerConfig.telegram_ids`. Même principe
            que `_authenticate` : jamais une identité qu'un champ du corps prétend être lui-même."""
            auth = self.headers.get("Authorization", "")
            token = auth[7:].strip() if auth.startswith("Bearer ") else ""
            if token:
                sender = api_keys.get(token)
                if sender:
                    return sender
                if approvals is not None:
                    session = approvals.verify_session(token)
                    if session is not None:
                        return session.user
                if local_api is not None and self._via_relay() and local_api.relayed_owner(auth) is not None:
                    # Le propriétaire par la passerelle (§7.5), comme dans `_authenticate` : sans ça, le web servi par
                    # `<nom>.nokker.app` ouvrait sa session puis prenait 401 sur /v1/threads (recette du 26.10).
                    return LOCAL_OWNER
            init_data = self.headers.get("X-Telegram-Init-Data") or (body or {}).get("init_data")
            if init_data and telegram_bot_token and approvals is not None:
                try:
                    info = approvals_crypto.verify_telegram_init_data(init_data, telegram_bot_token)
                except approvals_crypto.TelegramInitDataError:
                    return None
                tg_user_id = str((info.get("user") or {}).get("id", ""))
                if tg_user_id in telegram_ids:
                    return telegram_ids[tg_user_id]
                acc = tenancy.account_for_identity("telegram", tg_user_id) if tg_user_id else None
                return acc.account_id if acc is not None else None
            return None

        def _tenant_for(self, sender: str | None) -> str | None:
            """Locataire de l'expéditeur authentifié, ou 403 envoyé et `None`. Jamais de repli sur l'interne, sauf
            le serveur ouvert de dev (aucune clé configurée, `sender` None)."""
            if sender is None and not api_keys and open_api:
                return INTERNAL_TENANT
            try:
                return resolve_tenant(sender, tenancy, internal_senders)
            except UnknownSender:
                self._json(403, {"error": {"message": "forbidden"}})
                return None

        def _serve_miniapp_file(self, name: str) -> None:
            content_type = MINIAPP_FILES.get(name)
            path = miniapp_dir / name
            if content_type is None or ".." in name:
                self._json(404, {"error": {"message": "not found"}})
                return
            try:
                body = path.read_bytes()
            except OSError:
                self._json(404, {"error": {"message": "not found"}})
                return
            body = _miniapp_versioned(name, body, miniapp_dir)
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            # Le webview Telegram et Cloudflare gardaient style.css 4 h (vu le 18.10.2026 : correctif CSS invisible
            # sur iPhone) : jamais de cache, et des URL versionnées par empreinte en plus (_miniapp_versioned).
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if self._glance_gate("GET", path):  # jeton glance : sa seule lecture, 403 ailleurs
                return
            m = _RE_PUBLIC_SHARE.match(path)
            if m and shares is not None:  # lien partagé : public, sans connexion (nokker/threads/shares.py)
                self._public_share(m.group(1), (m.group(2) or ".json")[1:])
                return
            if self._local_route("GET", path):
                return
            if path == "/v1/approvals/suggestions" or path.startswith("/v1/approvals/suggestions/"):
                self._suggestions_route("GET", path)  # avant `/v1/approvals/{id}` (docs/APPROVALS.md §7)
                return
            if incidents_http.handle(self, "GET", path) or plan_http.handle(self, "GET", path):  # X18, drapeaux
                return
            if home_watch_http.handle(self, "GET", path):  # veille du foyer, drapeau `home_watch` (Lokkal)
                return
            if code_http.handle(self, "GET", path):  # sessions de code multi-utilisateur (§7.28), bloc `code`
                return
            if cloud_http.handle(self, "GET", path):  # nokker.ai : lien Lokkal, passerelle, inférence relayée
                return
            if path == "/healthz":
                # version du noyau et plus vieux client accepté : le client terminal (Lokkal) s'y compare
                # `memory` (X1b) : ok | down (mode direct, base ou embed injoignable : détail par GET /v1/local/memory)
                # | mcp | off. Le noyau reste « ok » : une mémoire en panne ne l'empêche pas de répondre.
                mem = memory_status(kernel.memory)
                mem_state = mem["mode"] if mem.get("ok") is None else ("ok" if mem["ok"] else "down")
                # `gateway` : on | off | no_key | invalid_key (lien Lokkal de nokker.ai, `cloud_http.gateway_state`).
                self._json(200, {"status": "ok", "version": NOKKER_VERSION, "min_client_version": MIN_CLIENT_VERSION,
                                 "memory": mem_state, "gateway": cloud_http.gateway_state()})
                return
            if path == "/metrics":
                if ledger is None:
                    self._json(404, {"error": {"message": "ledger non configuré"}})
                    return
                body = render_metrics(ledger).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            m = _RE_MINIAPP.match(path)
            if m:
                self._serve_miniapp_file(m.group(1))
                return
            # Vérification du webhook WhatsApp (Meta, hub.challenge) : publique, canal éteint = 404 (docs/CHANNELS.md)
            if channels_http.handle_public(self, "GET", path, urlparse(self.path).query):
                return

            # Onboarding (nokker/onboarding/http.py) : routes publiques, sans session (le testeur n'a pas encore de compte).
            if onboarding_http.handle(self, "GET", self.path):  # chemin COMPLET : ?t= et ?lang= y sont
                return
            if path.startswith("/v1/connectors") or path in ("/v1/account", "/v1/account/usage") \
                    or (team_http.enabled() and team_http.is_team_path(path)) \
                    or (packs_http.enabled() and packs_http.is_packs_path(path)) \
                    or (learning_http.enabled() and learning_http.is_learning_path(path)) \
                    or (long_http.enabled() and long_http.is_long_memory_path(path)) \
                    or (consolidation_http.enabled() and consolidation_http.is_consolidation_path(path)) \
                    or (sso_http.enabled() and sso_http.is_sso_path(path)) \
                    or (household_http.enabled() and household_http.is_household_path(path)) \
                    or (paper_http.enabled() and paper_http.is_paper_path(path)) \
                    or (famcal_http.enabled() and famcal_http.is_famcal_path(path)) \
                    or payments_http.is_payments_path(path) \
                    or (house_http.enabled() and house_http.is_house_path(path)) \
                    or (presence_http.enabled() and presence_http.is_presence_path(path)) \
                    or account_http.matches(path) \
                    or (path.startswith("/v1/billing/") and billing_http.service() is not None):
                self._account_route("GET", path)
                return

            # /v1/previews* (nokker/api/previews_routes.py, docs/BUBBLES.md §5) : point d'accroche
            # unique, hors du reste de ce dispatcheur — voir l'en-tête de previews_routes.py.
            if previews is not None and previews.handle_get(self, self.path):
                return

            if guide_http.enabled() and guide_http.is_guide_path(path):  # Guide (docs/GUIDE.md), drapeau `guide`
                self._guide_route("GET", path, {})
                return

            if hub is not None and (path.startswith(("/v1/threads", "/v1/bubbles")) or path == "/v1/notifications"
                                    or (path == "/v1/events" and approvals is None)):
                self._bubbles_route("GET", path, {})
                return

            # /v1/agents, /v1/users (docs/BUBBLES.md §9.2/§9.4) : INDÉPENDANTS de `hub`/`approvals` —
            # Crew et la liste de partage n'ont besoin que d'une identité résolue.
            if agents is not None and (path == "/v1/agents" or path.startswith("/v1/agents/")):
                self._agents_route("GET", path)
                return

            if path == "/v1/users":
                sender = self._resolve_identity()
                if sender is None:
                    self._json(401, {"error": {"message": "unauthorized"}})
                    return
                tid = self._tenant_for(sender)
                if tid is None:
                    return
                # Testeur : un compte = un locataire en bêta, il ne partage qu'avec lui-même.
                users = sorted(known_users) if tid == INTERNAL_TENANT else [sender]
                self._json(200, [user_to_json(u) for u in users])
                return

            if approvals is None:
                if path.startswith(("/v1/approvals", "/v1/devices", "/v1/events")):
                    self._json(404, {"error": {"message": "approbations fortes non configurées"}})
                    return
                self._json(404, {"error": {"message": "not found"}})
                return

            sender = self._resolve_identity()
            if sender is None:
                self._json(401, {"error": {"message": "unauthorized"}})
                return
            tid = self._tenant_for(sender)
            if tid is None:
                return
            with tenant_scope(tid):
                self._approvals_get(path, sender)

        def _suggestions_route(self, method: str, path: str) -> None:
            """Règles d'approbation suggérées (docs/APPROVALS.md §7). Lecture : toute identité résolue, sur SES cartes
            (locataire courant, décidées par elle). Acceptation : le propriétaire de la machine seulement (Lokkal), car
            la règle s'écrit dans SES réglages, comme la portée `always` d'une décision locale (§7.4.3)."""
            from nokker.approvals import suggestions as sg
            from nokker.local.approvals import append_user_rule
            from nokker.qa.permissions import Rule

            settings = sg.Settings.from_config(approval_suggestions)
            if approvals is None or not settings.enabled:
                self._json(404, {"error": {"message": "not found"}})
                return
            body = self._read_json_body() if method == "POST" else {}
            sender = self._resolve_identity(body)
            if sender is None:
                self._json(401, {"error": {"message": "unauthorized"}})
                return
            tid = self._tenant_for(sender)
            if tid is None:
                return
            perms = getattr(kernel.gates, "permissions", None)
            settings_path = getattr(local_api, "settings_path", None) if local_api is not None else None
            rule_store = "user_settings" if (sender == LOCAL_OWNER and perms is not None and settings_path) else None
            with tenant_scope(tid):
                now = approvals.clock()
                existing = [r.tool if r.pattern is None else f"{r.tool}({r.pattern})"
                            for r in (perms.allow if perms is not None else [])]
                history = [a for a in approvals.history(now - settings.window_s) if approvals.visible_to(sender, a)]
                marks = approvals.suggestion_marks(sender)
                items = sg.compute(history, tenant=tid, user=sender, settings=settings, now=now, hidden=marks.keys(),
                                   existing=existing)
                if path == "/v1/approvals/suggestions" and method == "GET":
                    self._json(200, {"suggestions": [i.to_json() for i in items], "rule_store": rule_store,
                                     "threshold": settings.threshold, "window_days": settings.window_days})
                    return
                m = re.match(r"^/v1/approvals/suggestions/([^/]+)/(accept|dismiss)$", path)
                if not (m and method == "POST"):
                    self._json(404, {"error": {"message": "not found"}})
                    return
                item = next((i for i in items if i.id == m.group(1)), None)
                if item is None:
                    self._json(404, {"error": {"message": "suggestion inconnue ou plus valable"}})
                    return
                if m.group(2) == "dismiss":
                    approvals.mark_suggestion(item.id, sender, "dismissed", item.rule, {})
                    self._json(200, {"id": item.id, "state": "dismissed", "rule": item.rule})
                    return
                if rule_store is None:
                    self._json(409, {"error": {"code": "no_rule_store",
                                               "message": "pas de règles durables ici : une règle « toujours » "
                                                          "s'écrit dans les réglages du propriétaire de la machine"}})
                    return
                provenance = {"source": "approval_suggestion", "suggestion_id": item.id,
                              "approved": item.approved, "approval_ids": item.approval_ids,
                              "accepted_by": sender,
                              "accepted_at": datetime.fromtimestamp(now, tz=timezone.utc).isoformat()}
                append_user_rule(settings_path, item.rule, provenance)
                parsed = Rule.parse(item.rule)
                if parsed not in perms.allow:
                    perms.allow.append(parsed)
                approvals.mark_suggestion(item.id, sender, "accepted", item.rule, provenance)
                self._json(200, {"id": item.id, "state": "accepted", "rule": item.rule, "provenance": provenance})

        def _auto_review_revoke(self, approval_id: str) -> None:
            """`POST /v1/approvals/{id}/auto-review/revoke` : le propriétaire révoque une décision du relecteur
            automatique (docs/APPROVALS.md §8). 404 si le relecteur est éteint ou la carte d'un autre locataire."""
            reviewer = getattr(kernel, "auto_reviewer", None)
            if approvals is None or reviewer is None:
                self._json(404, {"error": {"message": "not found"}})
                return
            body = self._read_json_body()
            sender = self._resolve_identity(body if isinstance(body, dict) else {})
            if sender is None:
                self._json(401, {"error": {"message": "unauthorized"}})
                return
            tid = self._tenant_for(sender)
            if tid is None:
                return
            with tenant_scope(tid):
                card = approvals.get(approval_id)
                if card is None or not approvals.visible_to(sender, card):
                    self._json(404, {"error": {"message": "approbation inconnue"}})
                    return
                try:
                    card = approvals.revoke_auto_review(approval_id, sender)
                except (ApprovalError, DeviceError) as e:
                    self._json(_approvals_error_status(e), {"error": {"message": str(e)}})
                    return
                self._json(200, approval_to_json(card))

        def _approvals_get(self, path: str, sender: str) -> None:
            assert approvals is not None
            if path == "/v1/approvals":
                # Seul `?status=pending` est câblé (docs/CLIENT-API.md n'en montre pas d'autre) ; le
                # port `Approvals` n'a pas de méthode « toutes, tous statuts » — `pending()` reste la
                # seule vue de liste pour l'instant, quelle que soit la valeur de `status`. Pas de
                # filtre par utilisateur : les opérateurs internes voient TOUTES les cartes en attente (l'un ou
                # l'autre peut décider — docs/CLIENT-API.md §Événements, « la première décision valide
                # gagne »), contrairement aux appareils (`GET /v1/devices`, propres à un utilisateur). Multi-locataire :
                # `pending()` ne rend que les cartes du locataire courant.
                # Carte d'une session de code : à son propriétaire seulement (`thread_owner`, §7.28).
                self._json(200, [approval_to_json(a) for a in approvals.pending() if approvals.visible_to(sender, a)])
                return

            m = _RE_APPROVAL_ONE.match(path)
            if m:
                approval = approvals.get(m.group(1))
                if approval is not None and not approvals.visible_to(sender, approval):
                    approval = None
                if approval is None:
                    self._json(404, {"error": {"message": "approbation inconnue"}})
                    return
                self._json(200, approval_to_json(approval))
                return

            if path == "/v1/devices":
                self._json(200, [device_to_json(d) for d in approvals.list_devices(user=sender)])
                return

            if path == "/v1/events":
                self._serve_events(sender)
                return

            self._json(404, {"error": {"message": "not found"}})

        # ─────────────────────── fils et bulles (docs/BUBBLES.md) ───────────────────────

        def _export_doc(self, t: Any, *, include_tools: bool, before: int | None = None,
                        limit: int = thread_export.DEFAULT_LIMIT, public: bool = False) -> dict[str, Any]:
            """Export d'un fil (nokker/threads/export.py), dans le locataire courant : historique du store, résultats
            d'outils secrets scellés, tout texte passé par le scrubber de sortie des invariants."""
            assert hub is not None
            inv = getattr(kernel, "invariants", None)
            scrub = getattr(inv, "scrub_output", None) or hub.scrub or (lambda text: text)
            return thread_export.build_export(
                hub.thread_json(t), kernel.store.history(t.id), scrub=scrub,
                effect_of=thread_export.default_effect_of(getattr(kernel, "tools", None)),
                include_tools=include_tools, before=before, limit=limit, public=public)

        def _send_rendered(self, doc: dict[str, Any], fmt: str, *, filename: str | None = None,
                           public: bool = False) -> None:
            body = thread_export.render(doc, fmt)
            self.send_response(200)
            self.send_header("Content-Type", thread_export.CONTENT_TYPES[fmt])
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             thread_export.HTML_CSP if fmt == "html" else "default-src 'none'; sandbox")
            if public:
                self.send_header("X-Robots-Tag", "noindex, nofollow")
            if filename:
                self.send_header("Content-Disposition", f'attachment; filename="{filename}.{fmt}"')
            self.end_headers()
            self.wfile.write(body)

        def _export_params(self) -> tuple[str, bool, int | None, int]:
            query = parse_qs(urlparse(self.path).query)
            fmt = (query.get("format") or ["json"])[0]
            if fmt not in thread_export.FORMATS:
                raise thread_export.ExportError("format : json | md | html")
            include_tools = (query.get("include_tools") or ["1"])[0] not in ("0", "false")
            try:
                before = int(query["before"][0]) if query.get("before") else None
                limit = int((query.get("limit") or [thread_export.DEFAULT_LIMIT])[0])
            except ValueError:
                raise thread_export.ExportError("before/limit entiers") from None
            return fmt, include_tools, before, limit

        def _public_share(self, token: str, fmt: str) -> None:
            """`GET /v1/public/shares/{jeton}[.html|.md|.json]` : SANS connexion, l'instantané d'un partage actif.
            Jeton inconnu, révoqué ou expiré : le même 404. Débit borné par IP et global (429)."""
            assert shares is not None
            ip = self.headers.get("CF-Connecting-IP") or self.headers.get("X-Real-IP") or self.client_address[0]
            if not shares.limiter.ok(str(ip or "?")):
                body = json.dumps({"error": {"message": "rate_limited"}}).encode()
                self.send_response(429)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Retry-After", "60")
                self.end_headers()
                self.wfile.write(body)
                return
            try:
                doc = shares.public(token)
            except Exception:  # base injoignable : rien ne sort, l'opérateur a la trace
                logger.exception("lecture d'un partage public impossible")
                self._json(503, {"error": {"message": "unavailable"}})
                return
            if doc is None:
                self._json(404, {"error": {"message": "not found"}})
                return
            self._send_rendered(doc, fmt, public=True)

        def _local_export(self, thread_id: str) -> None:
            """`GET /v1/local/sessions/{id}/export` (Lokkal) : même export, jeton local, fils de la machine seulement."""
            if not self._local_owner():
                self._json(401, {"error": {"message": "jeton local requis"}})
                return
            if tenant_of_thread(thread_id) != INTERNAL_TENANT:
                self._json(404, {"error": {"message": "pas de session locale sur ce fil"}})
                return
            try:
                fmt, include_tools, before, limit = self._export_params()
                with tenant_scope(INTERNAL_TENANT):
                    turns = kernel.store.history(thread_id)
                    t = hub.threads.get(thread_id) if hub is not None else None
                    if t is None and not turns and thread_id not in local_api.code.sessions():
                        self._json(404, {"error": {"message": "pas de session locale sur ce fil"}})
                        return
                    head = hub.thread_json(t) if t is not None and hub is not None else {"id": thread_id, "title": ""}
                    inv = getattr(kernel, "invariants", None)
                    doc = thread_export.build_export(
                        head, turns, scrub=getattr(inv, "scrub_output", None) or (lambda text: text),
                        effect_of=thread_export.default_effect_of(getattr(kernel, "tools", None)),
                        include_tools=include_tools, before=before, limit=limit)
            except thread_export.ExportError as e:
                self._json(400, {"error": {"message": str(e)}})
                return
            self._send_rendered(doc, fmt, filename=_export_filename(thread_id))

        def _bubbles_error(self, e: Exception) -> None:
            if isinstance(e, VersionConflict):
                self._json(409, {"error": {"message": "version périmée", "current_version": e.current}})
            elif isinstance(e, (ThreadNotFound, BubbleNotFound)):
                self._json(404, {"error": {"message": str(e) or "not found"}})
            elif isinstance(e, (ThreadForbidden, BubbleForbidden)):
                self._json(403, {"error": {"message": str(e) or "forbidden"}})
            else:
                self._json(400, {"error": {"message": str(e)}})

        def _bubbles_route(self, method: str, path: str, body: dict[str, Any]) -> None:
            assert hub is not None
            sender = self._resolve_identity(body)
            if sender is None:
                self._json(401, {"error": {"message": "unauthorized"}})
                return
            tid = self._tenant_for(sender)
            if tid is None:
                return
            try:
                with tenant_scope(tid):
                    self._bubbles_dispatch(method, unquote(path), body, sender)
            except (ThreadError, BubbleError, ShareError, thread_export.ExportError) as e:
                self._bubbles_error(e)
            except ShareNotFound:
                self._json(404, {"error": {"message": "partage inconnu"}})

        def _guide_route(self, method: str, path: str, body: dict[str, Any]) -> None:
            """`/v1/guide*` (docs/GUIDE.md) : toute identité vérifiée (compte, propriétaire Lokkal), dans son locataire."""
            sender = self._resolve_identity(body)
            if sender is None:
                self._json(401, {"error": {"message": "unauthorized"}})
                return
            tid = self._tenant_for(sender)
            if tid is None:
                return
            with tenant_scope(tid):
                guide_http.handle(self, method, path, body, sender, tid)

        def _goal_route(self, method: str, tid: str, action: str | None, body: dict[str, Any], sender: str) -> None:
            """`GET|POST /v1/threads/{id}/goal[/stop|resume|freeze]` (docs/OUTCOMES-GOAL.md) : membres du fil
            seulement, dans le locataire de l'appelant. GET : l'objectif courant (ou le dernier) et ses notes ; POST :
            pose un objectif (`mode` goal|task, `driver` kernel|client, `text`, `criteria`, `budget`, `checkpoint_every`)."""
            from nokker.goals.service import goal_json
            from nokker.qa.outcomes import RubricError

            assert hub is not None
            svc = kernel.goals
            hub.require_member(tid, sender)
            if method == "GET":
                g = svc.current(tid) or svc.store.latest(tid)
                self._json(200, {"goal": goal_json(g, svc.store.grades(g.id)) if g is not None else None})
                return
            try:
                if action == "stop":
                    g = svc.stop(tid)
                elif action == "resume":
                    g = svc.resume(tid)
                elif action == "freeze":
                    g = svc.freeze(tid)
                else:
                    budget = body.get("budget")
                    if budget is not None and not isinstance(budget, dict):
                        raise RubricError("budget : un objet")
                    g = svc.start(tid, sender, str(body.get("text") or ""), criteria=body.get("criteria"),
                                  mode=str(body.get("mode") or "goal"), driver=str(body.get("driver") or "kernel"),
                                  budget=budget, checkpoint_every=body.get("checkpoint_every"), channel="api",
                                  start_index=kernel.store.next_index(tid))
                    self._json(201, {"goal": goal_json(g)})
                    return
            except (RubricError, TypeError, ValueError) as e:
                self._json(400, {"error": {"message": str(e)}})
                return
            if g is None:
                self._json(409, {"error": {"message": "rien à faire : aucun objectif dans cet état sur ce fil"}})
                return
            self._json(200, {"goal": goal_json(g)})

        def _bubbles_dispatch(self, method: str, path: str, body: dict[str, Any], sender: str) -> None:
            assert hub is not None
            query = parse_qs(urlparse(self.path).query)

            def q(name: str, default: str | None = None) -> str | None:
                vals = query.get(name)
                return vals[0] if vals else default

            def sid(thread_id: Any) -> str:
                # Identifiant de fil du client → identifiant interne du locataire courant (idempotent).
                return scoped_thread_id(current_tenant(), str(thread_id or ""))

            if path == "/v1/events" and method == "GET":
                self._serve_events(sender)
                return
            if path == "/v1/notifications" and method == "GET":
                self._json(200, [bubble_to_json(b) for b in hub.list_notifications(sender)])
                return
            if path == "/v1/threads":
                if method == "GET":
                    self._json(200, [hub.thread_json(t) for t in hub.list_threads(sender)])
                    return
                if method == "POST":
                    t = hub.create_thread(str(body.get("title", "")), sender)
                    self._json(201, hub.thread_json(t))
                    return
            m = _RE_THREAD_ONE.match(path)
            if m:
                tid = sid(m.group(1))
                if method == "GET":
                    t = hub.require_member(tid, sender)
                    before = q("before")
                    limit = q("limit", "50")
                    try:
                        before_i = int(before) if before is not None else None
                        limit_i = int(limit or 50)
                    except ValueError:
                        raise ThreadError("before/limit entiers") from None
                    bubbles = [bubble_to_json(b, shared=sh) for b, sh in hub.list_bubbles(tid, sender)]
                    self._json(200, {"thread": hub.thread_json(t),
                                     "messages": hub.messages(tid, before=before_i, limit=limit_i),
                                     "bubbles": bubbles})
                    return
                if method == "DELETE":
                    hub.delete_thread(tid, sender)
                    if shares is not None:  # un fil supprimé n'a plus aucun lien public, tout de suite
                        try:
                            shares.revoke_thread(tid)
                        except Exception:  # le fil est supprimé ; l'opérateur a la trace
                            logger.exception("partages du fil %s non révoqués", tid)
                    self._json(200, {"deleted": tid})
                    return
                if method == "PATCH":
                    members = body.get("members")
                    if members is not None and not isinstance(members, list):
                        raise ThreadError("members doit être une liste")
                    title = body.get("title")
                    t = hub.patch_thread(tid, sender, title=str(title) if title is not None else None,
                                         members=[str(x) for x in members] if members is not None else None)
                    self._json(200, hub.thread_json(t))
                    return
            m = _RE_THREAD_CONTROL.match(path)
            if m and method == "POST":
                # Pause, reprise, steer : membres du fil seulement ; le fil est celui du locataire de l'appelant (`sid`).
                tid = sid(m.group(1))
                hub.require_member(tid, sender)
                self._json(*thread_control(tid, m.group(2), body, sender))
                return
            m = _RE_THREAD_IMAGE.match(path)
            if m and method == "GET":
                # Octets d'une image jointe (X7) : membres du fil seulement, et seulement si CE fil la référence.
                tid = sid(m.group(1))
                hub.require_member(tid, sender)
                images = getattr(kernel, "images", None)
                data = images.get(tid, m.group(2)) if images is not None else None
                media = sniff_media_type(data) if data is not None else None
                if data is None or media is None:
                    self._json(404, {"error": {"message": "image inconnue"}})
                    return
                self.send_response(200)
                self.send_header("Content-Type", media)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "private, max-age=86400, immutable")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy", "default-src 'none'; sandbox")
                self.end_headers()
                self.wfile.write(data)
                return
            m = _RE_THREAD_SUB.match(path)
            if m:
                tid, sub = sid(m.group(1)), m.group(2)
                if sub == "bubbles" and method == "GET":
                    include_closed = q("include_closed", "0") in ("1", "true")
                    items = hub.list_bubbles(tid, sender, include_closed=include_closed)
                    self._json(200, [bubble_to_json(b, shared=sh) for b, sh in items])
                    return
                if sub == "bubbles" and method == "POST":
                    b = hub.create_bubble(tid, sender, kind=str(body.get("kind", "")),
                                          title=str(body.get("title", "")), state=body.get("state") or {},
                                          status=str(body.get("status") or "idle"))
                    self._json(201, bubble_to_json(b))
                    return
                if sub == "messages" and method == "POST":
                    out = hub.send_message(sid(body.get("from_thread", "")), tid, str(body.get("text", "")),
                                           ask=bool(body.get("ask", False)), sender=sender)
                    self._json(202 if out.get("ask") else 200, out)
                    return
            m = _RE_THREAD_EXPORT.match(path)
            if m and method == "GET":
                # Export complet (membres du fil) : messages, réponses, appels d'outils ; secrets scellés.
                tid = sid(m.group(1))
                t = hub.require_member(tid, sender)
                fmt, include_tools, before_i, limit_i = self._export_params()
                doc = self._export_doc(t, include_tools=include_tools, before=before_i, limit=limit_i)
                self._send_rendered(doc, fmt, filename=_export_filename(m.group(1)))
                return
            m = _RE_THREAD_GOAL.match(path)
            if m and getattr(kernel, "goals", None) is not None and method in ("GET", "POST"):
                self._goal_route(method, sid(m.group(1)), m.group(2), body, sender)
                return
            m = _RE_THREAD_SHARES.match(path)
            if m and shares is not None and method in ("GET", "POST"):
                # Liens partagés : PROPRIÉTAIRE du fil seulement (un membre : 403, un inconnu : 404).
                tid = sid(m.group(1))
                t = hub.require_member(tid, sender)
                if sender is None or t.owner != sender:
                    raise ThreadForbidden("seul le propriétaire partage la session")
                if method == "GET":
                    now = shares.clock()
                    self._json(200, {"shares": [share_to_json(x, now) for x in shares.list(tid)]})
                    return
                include_tools = body.get("include_tools", False)
                if not isinstance(include_tools, bool):
                    raise ShareError("include_tools booléen")
                snap = self._export_doc(t, include_tools=include_tools, limit=thread_export.MAX_LIMIT, public=True)
                meta, token = shares.create(tid, sender, snap, expires_in=body.get("expires_in"),
                                            include_tools=include_tools)
                out = share_to_json(meta, shares.clock())
                self._json(201, {**out, "url": shares.url(token), "turn_count": snap["turn_count"],
                                 "truncated": snap["page"]["next_before"] is not None})
                return
            m = _RE_SHARE_ONE.match(path)
            if m and shares is not None and method == "DELETE":
                meta = shares.get(m.group(1))  # partage d'un autre locataire : ShareNotFound → 404
                t = hub.threads.get(meta.thread_id)
                if t is not None:
                    if not t.is_member(sender):
                        raise ShareNotFound(meta.share_id)
                    if t.owner != sender:
                        raise ThreadForbidden("seul le propriétaire révoque un partage")
                elif meta.owner != sender:
                    raise ShareNotFound(meta.share_id)
                self._json(200, share_to_json(shares.revoke(meta.share_id), shares.clock()))
                return
            m = _RE_BUBBLE_ONE.match(path)
            if m:
                bid = m.group(1)
                if method == "GET":
                    b, readonly = hub.get_bubble(bid, sender)
                    self._json(200, bubble_to_json(b, shared=readonly))
                    return
                if method == "PATCH":
                    if_version = body.get("if_version")
                    try:
                        if_version = int(if_version) if if_version is not None else None
                    except (TypeError, ValueError):
                        raise BubbleError("if_version entier") from None
                    title = body.get("title")
                    b = hub.patch_bubble(bid, sender, title=str(title) if title is not None else None,
                                         state=body.get("state"), status=body.get("status"), if_version=if_version)
                    self._json(200, bubble_to_json(b))
                    return
            m = _RE_BUBBLE_OP.match(path)
            if m and method == "POST":
                bid, op = m.groups()
                if op == "close":
                    self._json(200, bubble_to_json(hub.close_bubble(bid, sender)))
                    return
                if op == "transfer":
                    out = hub.transfer(bid, sender, sid(body.get("to_thread", "")), str(body.get("mode", "")))
                    out = {**out, "bubble": bubble_to_json(out["bubble"])}
                    self._json(200, out)
                    return
                if op == "share":
                    members, threads = body.get("members"), body.get("threads")
                    if (members is not None and not isinstance(members, list)) or \
                            (threads is not None and not isinstance(threads, list)):
                        raise BubbleError("members/threads doivent être des listes")
                    b = hub.share(bid, sender, members=[str(x) for x in members] if members is not None else None,
                                  threads=[sid(x) for x in threads] if threads is not None else None)
                    self._json(200, bubble_to_json(b))
                    return
            m = _RE_BUBBLE_ACTION.match(path)
            if m and method == "POST":
                out = hub.action(m.group(1), sender, m.group(2))
                self._json(202 if out.get("accepted") else 200, out)
                return
            m = _RE_APP_CALL.match(path)
            if m and method == "POST" and getattr(kernel, "mcp_apps", None) is not None:
                self._app_tool_call(m.group(1), body, sender)
                return
            self._json(404, {"error": {"message": "not found"}})

        def _app_tool_call(self, app_id: str, body: dict[str, Any], sender: str) -> None:
            """`tools/call` d'une app MCP affichée (docs/MCP-NEXT.md §4) : app connue de CE locataire, demandeur membre
            du fil de l'app, outil du même serveur avec « app » dans sa `visibility` ; puis les portes du noyau
            (`Kernel.app_tool_call`), sous le verrou du fil. Une carte : 202 `{approval_id}` ; sinon 200."""
            assert hub is not None
            reg = kernel.mcp_apps
            rec = reg.get(app_id)
            if rec is None or rec.tenant != current_tenant_or_none():
                self._json(404, {"error": {"message": "app inconnue"}})
                return
            hub.require_member(rec.thread_id, sender)  # ThreadForbidden → 403
            name, arguments = str(body.get("name") or ""), body.get("arguments")
            arguments = {} if arguments is None else arguments
            if not isinstance(arguments, dict):
                self._json(400, {"error": {"message": "arguments : objet attendu"}})
                return
            if not reg.allowed(rec, name):
                self._json(403, {"error": {"message": f"outil non appelable par cette app : {name}"}})
                return
            lock = hub.thread_lock(rec.thread_id)
            if not lock.acquire(blocking=False):
                self._json(409, {"error": {"message": "un tour est en cours sur ce fil"}})
                return
            try:
                out = kernel.app_tool_call(rec.thread_id, rec.server, name, arguments, sender=sender)
            finally:
                lock.release()
            if isinstance(out, str):
                self._json(202, {"approval_id": out})
                return
            text = hub._scrub(out.content or "")
            body_out: dict[str, Any] = {"content": text, "is_error": out.is_error,
                                        "result": {"content": [{"type": "text", "text": text}],
                                                   "isError": out.is_error}}
            if out.reason:
                body_out["reason"] = out.reason
            self._json(200, body_out)

        def do_PUT(self) -> None:
            path = urlparse(self.path).path
            if self._local_route("PUT", path):
                return
            if path.startswith("/v1/mirror/pages/") and cloud_http.handle(self, "PUT", path):  # pages copiées (§7.23)
                return
            if plan_http.handle(self, "PUT", path):  # livraison du brief (§7.18.5), drapeau `plan`
                return
            if code_http.handle(self, "PUT", path):  # identifiants de forge des sessions de code (§7.28)
                return
            if path.startswith("/v1/connectors") or path == "/v1/account/consent" \
                    or (team_http.enabled() and team_http.is_team_path(path)) \
                    or (consolidation_http.enabled() and consolidation_http.is_consolidation_path(path)) \
                    or (sso_http.enabled() and sso_http.is_sso_path(path)) \
                    or (household_http.enabled() and household_http.is_household_path(path)) \
                    or (paper_http.enabled() and paper_http.is_paper_path(path)) \
                    or (famcal_http.enabled() and famcal_http.is_famcal_path(path)) \
                    or payments_http.is_payments_path(path):
                self._account_route("PUT", path)
                return
            self._json(404, {"error": {"message": "not found"}})

        def _session_actor(self, account: Any) -> Any:
            """Le compte, avec la façon dont sa session a été ouverte (`approved_via` et `approved_at` de l'appareil de
            la session) : « SSO obligatoire » refuse les autres méthodes (nokker/sso/service.py::gate_reason). Sans
            session d'appareil (initData Telegram), la méthode est « telegram »."""
            from nokker.sso.model import SessionActor

            auth = self.headers.get("Authorization", "")
            token = auth[7:].strip() if auth.startswith("Bearer ") else ""
            session = approvals.verify_session(token) if (token and approvals is not None) else None
            if session is None or session.user != account.account_id:
                return SessionActor.of(account, "telegram", 0.0)
            device = approvals.get_device(session.device_id)
            if device is None:
                return SessionActor.of(account, "", 0.0)
            return SessionActor.of(account, device.approved_via, device.approved_at)

        def _glance_gate(self, method: str, path: str) -> bool:
            """Jeton glance (`nkg_…`, docs/AMBIENT.md §6) : il n'ouvre QUE `GET /v1/glance`. Partout ailleurs, et pour
            toute méthode, 403 — avant toute autre authentification, pour qu'aucune route ne l'interprète."""
            from nokker.ambient.glance import is_glance_token

            auth = self.headers.get("Authorization", "")
            token = auth[7:].strip() if auth.startswith("Bearer ") else ""
            if not is_glance_token(token):
                return False
            svc = ambient_http.service()
            glance = svc.glance if svc is not None else None
            if not (method == "GET" and path == "/v1/glance") or glance is None:
                self._json(403, {"error": {"code": "glance_scope", "message": "jeton glance : lecture de /v1/glance seulement"}})
                return True
            device = glance.resolve(token)
            if device is None:
                self._json(401, {"error": {"code": "bad_glance_token", "message": "jeton glance inconnu ou révoqué"}})
                return True
            wait = glance.read_allowed(device.id)
            if wait:  # au plus 2 lectures par minute et par jeton (docs/AMBIENT.md §6.2)
                body = json.dumps({"error": {"code": "glance_rate", "message": "trop de lectures : réessayez plus tard",
                                             "retry_after": wait}}).encode()
                self.send_response(429)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Retry-After", str(wait))
                self.end_headers()
                self.wfile.write(body)
                return True
            tid = self._tenant_for(device.account_id)
            if tid is None:
                return True
            with tenant_scope(tid):
                self._json(200, self._glance_view(device.account_id))
            return True

        def _glance_view(self, sender: str) -> dict[str, Any]:
            """Les quatre lectures de la vue glance, chacune isolée (une panne ou un drapeau éteint → null / [] et
            son nom dans `unavailable`) ; mêmes formes JSON que les routes d'origine."""
            out: dict[str, Any] = {"brief": None, "brief_last_run": None, "approvals": [], "incidents": [],
                                   "unavailable": [], "at": datetime.now(timezone.utc).isoformat()}
            plan = plan_http.service()
            brief = None
            try:
                if plan is None:
                    raise LookupError
                brief = plan.get_brief(sender)
                out["brief"] = {"agent": agent_to_json(brief) if brief is not None else None}
            except Exception:  # noqa: BLE001
                out["unavailable"].append("brief")
            try:
                if brief is not None:
                    if agents is None:
                        raise LookupError
                    runs, _ = agents.list_runs(brief.id, limit=1)
                    out["brief_last_run"] = run_to_json(runs[0]) if runs else None
            except Exception:  # noqa: BLE001
                out["unavailable"].append("brief_last_run")
            try:
                if approvals is None:
                    raise LookupError
                out["approvals"] = [approval_to_json(a) for a in approvals.pending() if approvals.visible_to(sender, a)]
            except Exception:  # noqa: BLE001
                out["unavailable"].append("approvals")
            try:
                op = incidents_http.service()
                if op is None:
                    raise LookupError
                from nokker.incidents.types import incident_to_json

                page, _ = op.list(state="live", limit=20)
                out["incidents"] = [incident_to_json(i, timeline=False) for i in page]
            except Exception:  # noqa: BLE001
                out["unavailable"].append("incidents")
            return out

        def _ambient_member(self, account: Any) -> Any:
            """Le membre du foyer qui agit (foyer allumé et adhésion active), ou None."""
            from nokker.ambient.displays import Membership

            house = household_http.service()
            got = house.membership(account.account_id) if house is not None else None
            if got is None:
                return None
            _, project, row = got
            return Membership(account_id=account.account_id, household_id=project.id,
                              household_tenant=project.tenant_id, profile=row.profile)

        def _account_route(self, method: str, path: str) -> None:
            """Routes d'un compte testeur (connecteurs, appairage Telegram, usage) : identité vérifiée, locataire fixé, compte
            `us_…` exigé — le locataire interne n'a ni connecteurs ni appairage."""
            sender = self._resolve_identity()
            if sender is None:
                self._json(401, {"error": {"message": "unauthorized"}})
                return
            tid = self._tenant_for(sender)
            if tid is None:
                return
            with tenant_scope(tid):
                account = tenancy.account(sender) if is_account(sender) else None
                if account is not None and sso_http.enabled() \
                        and (team_http.is_team_path(path) or sso_http.is_sso_path(path)):
                    # SSO obligatoire (drapeau `sso`) : l'équipe a besoin de savoir COMMENT la session a été ouverte
                    account = self._session_actor(account)
                house = household_http.service()
                minor = house.minor_state(account.account_id) if house is not None and account is not None else None
                if account is None:
                    self._json(403, {"error": {"message": "account_required"}})
                elif minor is not None and method != "GET" and packs_http.is_packs_path(path) \
                        and not (minor.active and minor.policy.minor_agents):
                    # profil mineur (X23) : installer ou retirer un pack d'agents suit la liste blanche du foyer
                    self._json(403, {"error": {"code": "minor_agent_refused",
                                               "message": "packs d'agents hors de la liste blanche du foyer"}})
                elif speak_lexicon_mod.is_path(path):  # lexique de prononciation du foyer (docs/VOICE.md §6)
                    speak_lexicon_mod.handle_member(self, method, path, self._ambient_member(account),
                                                    ambient_http._body, ambient_http._json)
                elif ambient_http.is_ambient_path(path):  # ambiant du foyer (docs/AMBIENT.md §5)
                    ambient_http.handle_member(self, method, path, self._ambient_member(account))
                elif ambient_http.is_glance_path(path):  # vue glance : offre, liste, révocation (docs/AMBIENT.md §6)
                    ambient_http.handle_glance_owner(self, method, path, account.account_id, tid,
                                                     self._ambient_member(account))
                elif path == "/v1/telegram/pairing" and method == "POST":
                    telegram_http.pairing(self, account.account_id)
                elif channels_http.is_account_path(path) and method == "POST":
                    channels_http.handle_account(self, path, account.account_id)
                elif path == "/v1/feedback" and method == "POST":
                    svc = feedback_mod.service()
                    fid = svc.submit(account, "web", str(self._read_json_body().get("text", ""))) if svc else None
                    if svc is None:
                        self._json(503, {"error": "feedback_disabled"})
                    elif fid is None:
                        self._json(400, {"error": "empty"})
                    else:
                        self._json(201, {"id": fid})
                elif path == "/v1/account/usage" and method == "GET":
                    if kernel.limits is None:
                        self._json(404, {"error": {"message": "not found"}})
                    else:
                        usage = kernel.limits.usage(account.account_id)
                        house = getattr(kernel, "household", None)
                        if house is not None:  # foyer (X23) : le quota commun et le plafond du membre
                            try:
                                block = house.member_usage(account.account_id)
                            except Exception:  # noqa: BLE001 — l'usage du compte reste servi
                                logger.warning("usage du foyer illisible", exc_info=True)
                                block = None
                            if block is not None:
                                usage = {**usage, "household": block}
                        self._json(200, usage)
                elif account_http.route(self, method, path, account, tid):
                    pass
                elif onboarding_http.account_route(self, method, path, account) \
                        or sso_http.handle(self, method, path, account) \
                        or team_http.handle(self, method, path, account) \
                        or packs_http.handle(self, method, path, account) \
                        or learning_http.handle(self, method, path, account) \
                        or long_http.handle(self, method, path, account) \
                        or consolidation_http.handle(self, method, path, account) \
                        or household_http.handle(self, method, path, account) \
                        or paper_http.handle(self, method, path, account) \
                        or famcal_http.handle(self, method, path, account) \
                        or payments_http.handle(self, method, path, account) \
                        or house_http.handle(self, method, path, account) \
                        or presence_http.handle(self, method, path, account):  # X17, packs_web, foyer, papiers, paiements, salle Maison
                    pass
                elif billing_http.handle(self, method, path, account):
                    pass
                elif not connectors_http.handle(self, method, path, account):
                    self._json(404, {"error": {"message": "not found"}})

        def do_PATCH(self) -> None:
            path = urlparse(self.path).path
            if self._local_route("PATCH", path):
                return
            if incidents_http.handle(self, "PATCH", path) or plan_http.handle(self, "PATCH", path):  # X18, drapeaux
                return
            if code_http.handle(self, "PATCH", path):  # mode d'une session de code (§7.28)
                return
            if (team_http.enabled() and team_http.is_team_path(path)) \
                    or (sso_http.enabled() and sso_http.is_sso_path(path)) \
                    or (household_http.enabled() and household_http.is_household_path(path)) \
                    or (paper_http.enabled() and paper_http.is_paper_path(path)) \
                    or (famcal_http.enabled() and famcal_http.is_famcal_path(path)):
                self._account_route("PATCH", path)
                return
            if agents is not None and path.startswith("/v1/agents/"):
                self._agents_route("PATCH", path)
                return
            if hub is None or not path.startswith(("/v1/threads", "/v1/bubbles")):
                self._json(404, {"error": {"message": "not found"}})
                return
            self._bubbles_route("PATCH", path, self._read_json_body())

        def _write_event(self, ev: Any, sender: str | None = None) -> None:
            payload = dict(ev.data)
            audience = payload.pop("_audience", None)
            ev_tenant = payload.pop("_tenant", None)
            sub_tenant = current_tenant_or_none()
            if ev_tenant != sub_tenant and not (ev_tenant is None and sub_tenant == INTERNAL_TENANT):
                return  # un événement ne sort jamais de son locataire (docs/TENANCY.md) ; sans locataire : l'interne
            if audience is not None and sender not in audience:
                return  # filtrage par membre (docs/BUBBLES.md §6)
            if "approval" in payload and isinstance(payload["approval"], Approval):
                payload["approval"] = approval_to_json(payload["approval"])
            self._write_chunk(f"id: {ev.id}\nevent: {ev.event_type}\ndata: "
                             f"{json.dumps(payload, ensure_ascii=False)}\n\n".encode())

        def _serve_events(self, sender: str | None = None) -> None:
            if events is None:
                self._json(404, {"error": {"message": "events non configurés"}})
                return
            # Reprise (demandée par le chantier clients nokker-apps) : `Last-Event-ID` (en-tête standard
            # SSE, reposé automatiquement par EventSource à la reconnexion) rejoue la fenêtre bornée
            # manquée avant de basculer en direct — sans ça, un client qui perd sa connexion quelques
            # secondes perd silencieusement les cartes arrivées pendant la coupure.
            last_event_id_raw = self.headers.get("Last-Event-ID")
            self._begin_stream(200)
            self.end_headers()
            q = events.subscribe()
            try:
                if last_event_id_raw:
                    try:
                        last_event_id = int(last_event_id_raw)
                    except ValueError:
                        last_event_id = 0
                    for ev in events.replay_since(last_event_id):
                        self._write_event(ev, sender)
                while True:
                    try:
                        ev = q.get(timeout=15)
                    except queue.Empty:
                        self._write_chunk(b": keep-alive\n\n")
                        continue
                    self._write_event(ev, sender)
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                events.unsubscribe(q)

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            if self._local_route("POST", path):
                return
            if path == "/v1/approvals/suggestions" or path.startswith("/v1/approvals/suggestions/"):
                self._suggestions_route("POST", path)  # avant `/v1/approvals/{id}` (docs/APPROVALS.md §7)
                return
            m = _RE_APPROVAL_AUTO_REVOKE.match(path)
            if m:
                self._auto_review_revoke(m.group(1))  # relecteur automatique (docs/APPROVALS.md §8)
                return
            if incidents_http.handle(self, "POST", path) or plan_http.handle(self, "POST", path):  # X18, drapeaux
                return
            if code_http.handle(self, "POST", path):  # sessions de code multi-utilisateur (§7.28), bloc `code`
                return
            if cloud_http.handle(self, "POST", path):  # nokker.ai : lien Lokkal, passerelle, inférence relayée
                return
            if path != "/v1/chat/completions":
                if billing_http.handle_webhook(self, "POST", path):  # public : authentifié par la signature Stripe
                    return
                if onboarding_http.handle(self, "POST", self.path):  # chemin COMPLET : ?t= et ?lang= y sont
                    return
                if sso_http.handle_public(self, "POST", path):  # SSO OIDC (drapeau `sso`) : pas encore de session
                    return
                if path == "/v1/telegram/webhook":
                    telegram_http.webhook(self)
                    return
                if channels_http.handle_public(self, "POST", path):  # Slack, WhatsApp : signature du fournisseur
                    return
                if path == "/v1/telegram/pairing" or path == "/v1/feedback" or path.startswith("/v1/connectors") \
                        or channels_http.is_account_path(path) \
                        or (team_http.enabled() and team_http.is_team_path(path)) \
                        or (packs_http.enabled() and packs_http.is_packs_path(path)) \
                        or (learning_http.enabled() and learning_http.is_learning_path(path)) \
                        or (long_http.enabled() and long_http.is_long_memory_path(path)) \
                    or (consolidation_http.enabled() and consolidation_http.is_consolidation_path(path)) \
                        or (sso_http.enabled() and sso_http.is_sso_path(path)) \
                        or (household_http.enabled() and household_http.is_household_path(path)) \
                    or (paper_http.enabled() and paper_http.is_paper_path(path)) \
                    or (famcal_http.enabled() and famcal_http.is_famcal_path(path)) \
                        or (house_http.enabled() and house_http.is_house_path(path)) \
                        or (presence_http.enabled() and presence_http.is_presence_path(path)) \
                        or account_http.matches(path) \
                        or (path.startswith("/v1/billing/") and billing_http.service() is not None):
                    self._account_route("POST", path)
                    return
                if path in ("/v1/pause", "/v1/resume"):
                    # Pause globale : les tours de CE compte dans SON locataire, jamais ceux d'un autre compte.
                    sender = self._resolve_identity()
                    if sender is None:
                        self._json(401, {"error": {"message": "unauthorized"}})
                        return
                    tenant = self._tenant_for(sender)
                    if tenant is None:
                        return
                    self._json(*account_control(path.rsplit("/", 1)[1], tenant, sender))
                    return
                if guide_http.enabled() and guide_http.is_guide_path(path):  # Guide (docs/GUIDE.md), drapeau `guide`
                    self._guide_route("POST", path, self._read_json_body())
                    return
                if hub is not None and path.startswith(("/v1/threads", "/v1/bubbles")):
                    self._bubbles_route("POST", path, self._read_json_body())
                    return
                if hub is not None and getattr(kernel, "mcp_apps", None) is not None and _RE_APP_CALL.match(path):
                    self._bubbles_route("POST", path, self._read_json_body())
                    return
                if agents is not None and (path == "/v1/agents" or path.startswith("/v1/agents/")):
                    self._agents_route("POST", path)
                    return
                if memory_search_api and path == "/v1/memory/search":
                    self._memory_search()
                    return
                self._handle_approvals_post(path)
                return
            authorized, sender = self._authenticate()
            if not authorized:
                self._json(401, {"error": {"message": "unauthorized"}})
                return
            tenant = self._tenant_for(sender)
            if tenant is None:
                return
            with tenant_scope(tenant):
                self._chat_completions(sender, tenant)

        def _memory_search(self) -> None:
            """`POST /v1/memory/search {query, k?}` (docs/MCP-SERVER.md) : la mémoire longue du locataire de
            l'identité vérifiée (`kernel.memory`, résolue dans `tenant_scope` comme le rappel d'un tour) ; un testeur
            sans mémoire n'a rien. Extraits passés par `scrub_output`."""
            sender = self._resolve_identity()
            if sender is None:
                self._json(401, {"error": {"message": "unauthorized"}})
                return
            tid = self._tenant_for(sender)
            if tid is None:
                return
            body = self._read_json_body()
            body = body if isinstance(body, dict) else {}
            query = str(body.get("query") or "").strip()
            try:
                k = max(1, min(int(body.get("k") or 5), 20))
            except (TypeError, ValueError):
                k = 5
            if not query or len(query) > 2000:
                self._json(400, {"error": {"message": "query : texte de 1 à 2000 caractères"}})
                return
            scrub = getattr(kernel.invariants, "scrub_output", lambda s: s)
            with tenant_scope(tid):
                try:
                    found = kernel.memory.recall(query, k)
                except Exception:  # mémoire injoignable : 503, jamais une trace
                    logger.warning("recherche mémoire en échec", exc_info=True)
                    self._json(503, {"error": {"message": "mémoire indisponible"}})
                    return
            self._json(200, {"results": [{"note": r.note, "text": scrub(r.text), "score": round(float(r.score), 4)}
                                         for r in found[:k]]})

        def _chat_completions(self, sender: str | None, tenant: str) -> None:
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length > MAX_CHAT_BODY:  # 4 images de 5 Mo en base64 + le texte (X7)
                self._json(413, {"error": {"message": f"requête au-delà de {MAX_CHAT_BODY // (1024 * 1024)} Mo"}})
                return
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                self._json(400, {"error": {"message": "invalid JSON"}})
                return

            # `X-Session-Id` (docs/CLIENT-API.md, docs/BUBBLES.md §1) ou `X-Hermes-Session-Id` (passerelle amont),
            # préfixé par le locataire d'un testeur (docs/TENANCY.md §3) : même X-Session-Id, fils distincts.
            client_session = (self.headers.get("X-Session-Id") or self.headers.get("X-Hermes-Session-Id")
                              or str(uuid.uuid4()))
            thread_id = scoped_thread_id(tenant, client_session)
            if tenant_of_thread(thread_id) != tenant:
                self._json(404, {"error": {"message": "fil inconnu"}})  # l'interne qui vise le fil d'un testeur
                return
            fixed = session_threads.get(sender or "") if tenant == INTERNAL_TENANT else None
            if fixed:
                if last_session.get(fixed, thread_id) != thread_id:
                    try:
                        kernel.context.new_episode(fixed, "marker")  # type: ignore[attr-defined]
                    except Exception:
                        logger.exception("clôture d'épisode sur changement de session en échec")
                last_session[fixed] = thread_id
                thread_id = fixed
            messages = body.get("messages") or []
            user_text = ""
            raw_images: list[Any] = []
            attached = ""
            try:
                for m in reversed(messages):
                    if m.get("role") == "user":
                        # X7 (docs/LOKKAL.md §7.6) : `content` chaîne ou parties OpenAI (text, image_url en data URL),
                        # et/ou `attachments` [{data|url|path, media_type?, name?}] ; type contrôlé par les octets.
                        user_text, parts = split_content(m.get("content"))
                        extra = m.get("attachments") or []
                        if not isinstance(extra, list):
                            raise ImageError("attachments doit être une liste")
                        # §7.6.1 : avec `text_attachments_enabled`, une pièce porte un `type` (image|text|dir|url ;
                        # sans type = image) ; éteint, `attachments` part tel quel au parseur d'images (inchangé).
                        texts: list[dict[str, Any]] = []
                        text_svc = getattr(kernel, "text_attachments", None)
                        if text_svc is not None:
                            extra, texts = split_kinds(extra)
                        cwd = local_api.code.sessions().get(thread_id) if local_api is not None else None
                        if parts or extra:
                            if getattr(kernel, "images", None) is None:
                                raise ImageError("images non prises en charge sur cette instance")
                            raw_images = parse_attachments([*extra, *parts], cwd=cwd)
                        if texts:
                            attached = render_block(text_svc.parse(
                                texts, cwd=cwd, known=getattr(getattr(kernel, "invariants", None), "known", None)))
                        break
            except ImageError as e:
                self._json(400, {"error": {"message": str(e)}})
                return
            stream = bool(body.get("stream", False))

            if hub is not None:
                try:
                    hub.ensure_thread_for_turn(thread_id, sender, user_text)
                except ThreadForbidden:
                    self._json(403, {"error": {"message": "pas membre de ce fil"}})
                    return
                except ThreadNotFound:
                    self._json(404, {"error": {"message": "fil inconnu"}})
                    return
            refs = [kernel.images.put(thread_id, img) for img in raw_images] if raw_images else []  # type: ignore[attr-defined]
            # Un tour arrivé par le relais (en-tête posé par la passerelle, qui exige déjà un jeton signé par nokker.ai
            # pour l'identité du propriétaire) porte son propre canal : les portes n'y accordent aucun GO textuel.
            channel = RELAY_CHANNEL if self._via_relay() else "api"
            inp = TurnInput(thread_id=thread_id, user_text=user_text, channel=channel, sender=sender, images=refs,
                            attached=attached)
            if getattr(kernel, "goals", None) is not None:
                # `metadata.cli_control` (CLI : btw, goal_check, goal_continue, loop) : étiquette du tour, lue par les
                # objectifs seulement (docs/OUTCOMES-GOAL.md). Drapeaux éteints : ignorée, comme par un noyau ancien.
                from nokker.goals.service import CONTROLS

                meta = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
                control = str(meta.get("cli_control") or "")
                if control in CONTROLS:
                    inp.control = control
            if stream:
                tokens = (self.headers.get("X-Nokker-Stream-Tokens") or "").strip() == "1"
                self._stream_turn(inp, tokens)
                return
            try:
                turn = self._run_turn(inp, None)
            except Exception as e:
                logger.exception("run_turn a levé une exception non rattrapée")
                self._json(500, {"error": {"message": str(e)}})
                return

            completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
            created = int(time.time())
            model_name = turn.calls[-1].model if turn.calls else "nokker"
            out = {
                "id": completion_id, "object": "chat.completion", "created": created, "model": model_name,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": turn.reply},
                             "finish_reason": "stop"}],
                "usage": _usage_dict(turn),
            }
            delegated = getattr(local_api, "delegated", None) if local_api is not None else None
            if delegated is not None and delegated.is_delegated(thread_id):
                from nokker.local.delegation import turn_extension

                out["nokker"] = turn_extension(turn)  # session déléguée (X21) : réponse finale ou carte en attente
            self._json(200, out)

        def _run_turn(self, inp: TurnInput, sink: Any) -> Turn:
            if hub is not None:
                with hub.thread_lock(inp.thread_id):
                    return kernel.run_turn(inp, sink) if sink is not None else kernel.run_turn(inp)
            return kernel.run_turn(inp, sink) if sink is not None else kernel.run_turn(inp)

        def _stream_turn(self, inp: TurnInput, tokens: bool) -> None:
            """`stream: true` (P1, 19.10.2026) : en-têtes SSE TOUT DE SUITE, tour dans un fil de travail,
            `: ping` toutes les `STREAM_PING_S` tant que rien d'autre ne part (Cloudflare coupe à 100 s sans
            octet). Sans `tokens` (passerelle amont, tout client OpenAI) : à la fin, EXACTEMENT les trois chunks
            d'avant (rôle, réponse + usage, stop) puis `[DONE]` — seuls les commentaires `: ping` s'ajoutent,
            ignorés par tout parseur SSE. Avec `tokens` (`X-Nokker-Stream-Tokens: 1`) : rôle d'emblée, deltas
            caviardés en direct, `nokker.reset` quand un appel finit en outils, puis un chunk final
            `nokker: {reset, final}` qui porte la réponse qui fait foi (docs/CLIENT-API.md)."""
            completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
            events_q: queue.Queue[tuple[str, Any]] = queue.Queue()

            class _Sink:
                def delta(self, text: str) -> None:
                    events_q.put(("delta", text))

                def reset(self) -> None:
                    events_q.put(("reset", None))

            outcome: dict[str, Any] = {}

            def _work() -> None:
                try:
                    outcome["turn"] = self._run_turn(inp, _Sink() if tokens else None)
                except Exception as e:  # noqa: BLE001 — rapporté au client dans le flux
                    logger.exception("run_turn a levé une exception non rattrapée")
                    outcome["error"] = e
                finally:
                    events_q.put(("done", None))

            self._begin_stream(200)
            self.end_headers()
            # Le tour emporte le contexte de la requête (locataire courant, docs/TENANCY.md §2).
            worker = threading.Thread(target=contextvars.copy_context().run, args=(_work,),
                                      name=f"turn-{inp.thread_id[:24]}", daemon=True)
            worker.start()

            def _chunk(delta: dict[str, Any], model: str = "nokker", **extra: Any) -> dict[str, Any]:
                obj: dict[str, Any] = {"id": completion_id, "object": "chat.completion.chunk",
                                       "created": int(time.time()), "model": model,
                                       "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
                obj.update(extra)
                return obj

            try:
                if tokens:
                    self._sse(_chunk({"role": "assistant"}))
                while True:
                    try:
                        kind, value = events_q.get(timeout=STREAM_PING_S)
                    except queue.Empty:
                        self._write_chunk(b": ping\n\n")
                        continue
                    if kind == "done":
                        break
                    if kind == "delta":
                        self._sse(_chunk({"content": value}))
                    elif kind == "reset":
                        self._sse(_chunk({}, nokker={"reset": True}))
                if "error" in outcome:
                    # En-têtes déjà partis : l'erreur part dans le flux, sans `[DONE]` (la passerelle amont
                    # rapporte alors une réponse interrompue au lieu d'un texte vide présenté comme complet).
                    self._sse({"error": {"message": str(outcome["error"])}})
                    self.wfile.write(b"0\r\n\r\n")
                    return
                turn: Turn = outcome["turn"]
                created = int(time.time())
                model_name = turn.calls[-1].model if turn.calls else "nokker"
                usage = _usage_dict(turn)
                if tokens:
                    self._sse({"id": completion_id, "object": "chat.completion.chunk", "created": created,
                               "model": model_name,
                               "choices": [{"index": 0, "delta": {"content": turn.reply}, "finish_reason": None}],
                               "usage": usage, "nokker": {"reset": True, "final": True}})
                else:
                    self._sse({"id": completion_id, "object": "chat.completion.chunk", "created": created,
                               "model": model_name,
                               "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]})
                    self._sse({"id": completion_id, "object": "chat.completion.chunk", "created": created,
                               "model": model_name,
                               "choices": [{"index": 0, "delta": {"content": turn.reply}, "finish_reason": None}],
                               "usage": usage})
                self._sse({"id": completion_id, "object": "chat.completion.chunk", "created": created,
                           "model": model_name, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
                self._write_chunk(b"data: [DONE]\n\n")
                self.wfile.write(b"0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError):
                # Client parti (onglet fermé, coupure réseau) : le tour continue dans son fil et s'enregistre
                # (le fil le montrera au prochain chargement) ; on ne fait qu'arrêter d'écrire.
                logger.info("client parti pendant le tour %s (le tour continue)", inp.thread_id)
                self.close_connection = True

        def _agents_route(self, method: str, path: str) -> None:
            """Crew (docs/BUBBLES.md §9.4, §9.5). Identité vérifiée du locataire sur toutes les routes, sauf le webhook
            signé (`POST /v1/agents/{id}/webhooks/{hook}`, pas d'identité : la signature HMAC de l'agent en tient lieu).
            Un agent d'un autre locataire n'existe pas (404). Erreurs `{"error": {"message"}}` : 400 champ invalide,
            404 inconnu, 409 état (passage déjà en cours, échéance passée, rejeu), 503 moteur ou approbations absents."""
            from nokker.agents.hub import EngineUnavailable

            assert agents is not None
            m = _RE_AGENT_WEBHOOK.match(path)
            if m and method == "POST":
                engine = getattr(agents, "engine", None)
                length = int(self.headers.get("Content-Length", 0) or 0)
                if engine is None:
                    self._json(404, {"error": {"message": "not found"}})
                elif length > MAX_WEBHOOK_BODY:
                    self._json(413, {"error": {"message": f"corps au-delà de {MAX_WEBHOOK_BODY // 1024} Kio"}})
                else:
                    raw = self.rfile.read(length) if length else b""
                    from nokker.agents.engine import SIG_HEADER

                    self._json(*engine.webhook(m.group(1), m.group(2), raw, self.headers.get(SIG_HEADER)))
                return
            body = self._read_json_body() if method in ("POST", "PATCH") else {}
            sender = self._resolve_identity(body)
            if sender is None:
                self._json(401, {"error": {"message": "unauthorized"}})
                return
            tid = self._tenant_for(sender)
            if tid is None:
                return
            try:
                with tenant_scope(tid):
                    self._agents_dispatch(method, path, body, sender, tid)
            except AgentNotFound as e:
                self._json(404, {"error": {"message": f"inconnu : {e}"}})
            except AgentMissing as e:  # déménagement refusé (X15) : ce qui manque à la destination, par noms
                self._json(409, {"error": {"message": str(e), "missing": e.missing}})
            except AgentConflict as e:
                self._json(409, {"error": {"message": str(e)}})
            except EngineUnavailable as e:
                self._json(503, {"error": {"message": str(e)}})
            except AgentError as e:
                code = getattr(e, "code", None)
                self._json(400, {"error": {"message": str(e), **({"code": code} if code else {})}})

        def _agent_project(self, body: dict[str, Any], sender: str) -> dict[str, Any] | None:
            """Champ `project` d'un agent (équipe, X17) : drapeau `team` éteint = champ ignoré ; allumé = il faut être au
            moins membre du projet (sinon 404, comme un projet inconnu). None = réponse d'erreur déjà envoyée."""
            if "project" not in body:
                return body
            team = team_http.service()
            if team is None or not body.get("project"):
                return {k: v for k, v in body.items() if k != "project"} if team is None else body
            from nokker.team.model import TeamError

            account = tenancy.account(sender) if is_account(sender) else None
            try:
                if account is None:
                    raise TeamError(404, "not_found", "projet inconnu")
                return {**body, "project": team.check_agent_project(account, str(body.get("project")))}
            except TeamError as e:
                self._json(e.status, {"error": {"code": e.code, "message": e.message}})
                return None

        def _agents_dispatch(self, method: str, path: str, body: dict[str, Any], sender: str, tid: str) -> None:
            assert agents is not None
            if path == "/v1/agents":
                if method == "GET":
                    self._json(200, [agent_json(a) for a in agents.list_agents() if agent_tenant(a) == tid])
                elif method == "POST":
                    body = self._agent_project(body, sender)
                    if body is None:
                        return
                    house = household_http.service()
                    st = house.minor_state(sender) if house is not None and is_account(sender) else None
                    if st is not None:  # profil mineur (X23) : la liste blanche du foyer, rien d'autre
                        from nokker.household.boundaries import minor_may_create_agent

                        if not st.active or not minor_may_create_agent(str(body.get("name") or ""),
                                                                       st.policy.minor_agents):
                            self._json(403, {"error": {"code": "minor_agent_refused",
                                                       "message": "agent hors de la liste blanche du foyer"}})
                            return
                    self._json(201, agent_json(agents.create_from_body(body, sender)))
                else:
                    self._json(404, {"error": {"message": "not found"}})
                return
            m = _RE_AGENT_RUN_CANCEL.match(path)
            m1 = _RE_AGENT.match(path)
            agent_id = (m or m1).group(1) if (m or m1) else None
            if agent_id is None:
                self._json(404, {"error": {"message": "not found"}})
                return
            if m1 and m1.group(2) == "move" and method == "POST":
                # Déménagement (X15, LOKKAL §6.7) : l'agent peut vivre sur une machine (connu par son deck), pas ici.
                self._json(200, agent_json(agents.move(agent_id, body.get("to"), sender, tid)))
                return
            a = agents.get_agent(agent_id)
            if agent_tenant(a) != tid:
                raise AgentNotFound(agent_id)  # un agent d'un autre locataire n'existe pas ici
            if m and method == "POST":
                self._json(200, run_to_json(agents.cancel_run(agent_id, m.group(2), sender)))
                return
            op = m1.group(2) if m1 else None
            if op is None and method == "GET":
                self._json(200, agent_json(a))
            elif op is None and method == "PATCH":
                body = self._agent_project(body, sender)
                if body is None:
                    return
                self._json(200, agent_json(agents.patch_agent(agent_id, body, sender)))
            elif op is None and method == "DELETE":
                agents.delete_agent(agent_id, sender)
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif op == "arm" and method == "POST":
                agent, approval_id = agents.arm(agent_id, sender)
                if approval_id:
                    self._json(202, {"approval_id": approval_id, "agent": agent_json(agent)})
                else:
                    self._json(200, agent_json(agent))
            elif op == "disarm" and method == "POST":
                self._json(200, agent_json(agents.disarm(agent_id, sender)))
            elif op == "run" and method == "POST":
                self._json(202, {"run_id": agents.run_now(agent_id, sender).id})
            elif op == "runs" and method == "GET":
                q = parse_qs(urlparse(self.path).query)
                try:
                    limit = int((q.get("limit") or ["20"])[0])
                except ValueError:
                    raise AgentError("limit : entier attendu") from None
                runs, nxt = agents.list_runs(agent_id, limit=limit, before=(q.get("before") or [None])[0])
                self._json(200, {"runs": [run_to_json(r) for r in runs], "next_before": nxt})
            elif op == "webhook-secret" and method == "POST":
                from nokker.agents.engine import SIG_HEADER

                key, hook = agents.webhook_secret(agent_id)
                self._json(200, {"secret": key, "url": f"/v1/agents/{agent_id}/webhooks/{hook}",
                                 "header": SIG_HEADER,
                                 "signature": "t=<epoch>,v1=<hex HMAC-SHA256(secret, \"<t>.\" + corps brut)>"})
            else:
                self._json(404, {"error": {"message": "not found"}})

        def _handle_approvals_post(self, path: str) -> None:
            """Tout `POST` qui n'est pas `/v1/chat/completions` — `/v1/devices`, `/v1/devices/{id}/confirm`
            (TOTP, extension hors Mini App — elle confirme par CLI/attente), `/v1/approvals/{id}/challenge`,
            `/v1/approvals/{id}/decision`, `/v1/sessions` (docs/CLIENT-API.md). `initData` Telegram est
            accepté en en-tête `X-Telegram-Init-Data` OU champ `init_data` du corps — la Mini App
            (`approve.js`) envoie les deux suivant l'appel ; les deux voies sont vérifiées."""
            if approvals is None:
                if path.startswith(("/v1/approvals", "/v1/devices", "/v1/sessions")):
                    self._json(404, {"error": {"message": "approbations fortes non configurées"}})
                    return
                self._json(404, {"error": {"message": "not found"}})
                return

            body = self._read_json_body()
            # Chaque branche fixe le locataire (`scope.enter_context(tenant_scope(…))`) avant de toucher au backend.
            with ExitStack() as scope:
                self._approvals_post(path, body, scope)

        def _approvals_post(self, path: str, body: dict[str, Any], scope: ExitStack) -> None:
            assert approvals is not None
            if path == "/v1/devices":
                sender = self._resolve_identity(body)
                anonymous = False
                tid: str | None = INTERNAL_TENANT  # enrôlement sans identité : utilisateurs internes seulement
                if sender is None:
                    # §3.1bis : un navigateur n'a aucune identité avant son premier appareil. Il DÉCLARE son
                    # utilisateur ; l'appareil reste `pending` (aucun droit) jusqu'à confirmation par l'hôte
                    # (`devices approve <code>`) ou par le TOTP de CET utilisateur. Débit borné.
                    claimed = str(body.get("user", "")).strip().lower()
                    # Sans locataire interne (bêta OVH), un appareil ne naît que d'une invitation : même 401 pour
                    # tout nom, pour ne pas révéler lesquels existent (trouvé en recette sur app.nokker.ai le 21.10).
                    if (not internal_tenant or not claimed or not approvals.is_authorized(claimed)
                            or not _anon_enroll_allowed()):
                        self._json(401, {"error": {"message": "unauthorized"}})
                        return
                    sender, anonymous = claimed, True
                else:
                    tid = self._tenant_for(sender)
                    if tid is None:
                        return
                scope.enter_context(tenant_scope(tid))
                public_key_spki = str(body.get("public_key_spki", "")).strip()
                name = str(body.get("name", "")).strip() or "appareil sans nom"
                if not public_key_spki:
                    self._json(400, {"error": {"message": "public_key_spki manquant"}})
                    return
                telegram_user_id = None
                init_data = body.get("init_data")
                if init_data and telegram_bot_token:
                    try:
                        info = approvals_crypto.verify_telegram_init_data(init_data, telegram_bot_token)
                        telegram_user_id = str((info.get("user") or {}).get("id", "")) or None
                    except approvals_crypto.TelegramInitDataError:
                        pass
                if anonymous:
                    telegram_user_id = None  # jamais de lien Telegram sur un enrôlement sans preuve
                device = approvals.enroll_device(user=sender, name=name, public_key_spki=public_key_spki,
                                                 telegram_user_id=telegram_user_id)
                if not anonymous and telegram_user_id and not self.headers.get("Authorization"):
                    # Identité tirée de l'initData seul : ce Telegram est celui relié au compte. La Mini App enrôle
                    # son appareil, confirmé tout de suite (sinon il restait `pending` sans moyen de le confirmer).
                    device = approvals.confirm_device_by_telegram(device.id, telegram_user_id)
                if anonymous:
                    logger.warning("appareil web en attente pour %s : code %s (devices approve <code>)",
                                   sender, device.code)
                self._json(201, device_to_json(device))
                return

            m = _RE_DEVICE_CONFIRM.match(path)
            if m:
                totp = str(body.get("totp", "")).strip()
                totp_user = str(body.get("user", "")).strip()
                if not totp or not totp_user:
                    self._json(400, {"error": {"message": "totp/user manquants"}})
                    return
                totp_tenant = approvals.user_tenant(totp_user.lower())
                if totp_tenant is None:
                    self._json(403, {"error": {"message": f"utilisateur non autorisé : {totp_user!r}"}})
                    return
                scope.enter_context(tenant_scope(totp_tenant))
                try:
                    device = approvals.confirm_device_totp(m.group(1), totp, totp_user)
                except (ApprovalError, DeviceError) as e:
                    self._json(_approvals_error_status(e), {"error": {"message": str(e)}})
                    return
                self._json(200, device_to_json(device))
                return

            m = _RE_DEVICE_SESSION_CHALLENGE.match(path)
            if m:
                # Pas d'identité requise : c'est ce défi, signé, qui VA établir une identité
                # (docs/CLIENT-API.md §Authentification — manquait côté contrat, signalé par le
                # chantier clients nokker-apps, dont le faux serveur l'utilise déjà).
                scope.enter_context(tenant_scope(device_scope(m.group(1))))
                try:
                    ch = approvals.login_challenge(m.group(1))
                except (ApprovalError, DeviceError) as e:
                    # Code stable : un écran distingue « appareil inconnu » (oublier la clé locale, se reconnecter) et
                    # « appareil en attente / révoqué » d'une panne du service.
                    err: dict[str, Any] = {"message": str(e), "code": _device_error_code(e)}
                    if getattr(e, "status", None):
                        err["device_status"] = e.status
                    self._json(_approvals_error_status(e), {"error": err})
                    return
                self._json(200, {"challenge": ch.canonical(), "nonce": ch.nonce, "exp": ch.exp})
                return

            m = _RE_APPROVAL_CHALLENGE.match(path)
            if m:
                sender = self._resolve_identity(body)
                if sender is None:
                    self._json(401, {"error": {"message": "unauthorized"}})
                    return
                tid = self._tenant_for(sender)
                if tid is None:
                    return
                scope.enter_context(tenant_scope(tid))  # carte d'un autre locataire : ApprovalNotFound → 404
                try:
                    ch = approvals.challenge(m.group(1))
                except (ApprovalError, DeviceError) as e:
                    self._json(_approvals_error_status(e), {"error": {"message": str(e)}})
                    return
                self._json(200, {"challenge": ch.canonical(), "nonce": ch.nonce, "exp": ch.exp})
                return

            m = _RE_APPROVAL_KNOCK_ARM.match(path)
            if m:
                # Heurtoir (docs/KNOCK.md §5.3) : « approuver au heurtoir » arme la carte pour une pièce ; le toc toc
                # toc suivant dans cette pièce, avec un membre autorisé présent, l'approuvera. Drapeau éteint : 404.
                if knock_hub is None:
                    self._json(404, {"error": {"message": "not found"}})
                    return
                sender = self._resolve_identity(body)
                if sender is None:
                    self._json(401, {"error": {"message": "unauthorized"}})
                    return
                tid = self._tenant_for(sender)
                if tid is None:
                    return
                scope.enter_context(tenant_scope(tid))
                card = approvals.get(m.group(1))
                if card is None or not approvals.decider_allowed(sender, tid, card):
                    self._json(404, {"error": {"message": "carte introuvable"}})
                    return
                why = knock_hub.arm(m.group(1), str(body.get("room", "")).strip())
                if why:
                    self._json(409, {"error": {"message": why, "code": "knock_refused"}})
                    return
                self._json(200, {"approval_id": m.group(1), "room": str(body.get("room", "")).strip(),
                                 "window_s": knock_hub.config.approve.window_s})
                return

            m = _RE_APPROVAL_DECISION.match(path)
            if m:
                sender = self._resolve_identity(body)
                if sender is None:
                    self._json(401, {"error": {"message": "unauthorized"}})
                    return
                tid = self._tenant_for(sender)
                if tid is None:
                    return
                # Carte d'un autre locataire : 404 ; décideur hors du locataire de la carte : 403 (`decide`).
                scope.enter_context(tenant_scope(tid))
                decision = str(body.get("decision", "")).strip()
                webauthn = body.get("webauthn") or {}
                proof = Proof(challenge=body.get("challenge"), signature=body.get("signature"),
                             device_id=body.get("device_id"), telegram_init_data=body.get("init_data"),
                             totp=body.get("totp"), totp_user=body.get("totp_user") or body.get("user"),
                             webauthn_client_data_json=webauthn.get("client_data_json") or webauthn.get("clientDataJSON"),
                             webauthn_authenticator_data=webauthn.get("authenticator_data")
                             or webauthn.get("authenticatorData"),
                             webauthn_signature=webauthn.get("signature"))
                try:
                    decided = approvals.decide(m.group(1), decision, proof)
                except (ApprovalError, DeviceError) as e:
                    err: dict[str, Any] = {"message": str(e)}
                    if isinstance(e, StrongDeviceRequired):
                        err["code"] = "strong_device_required"  # le client propose un appareil de confiance
                    self._json(_approvals_error_status(e), {"error": err})
                    return
                # Même file par fil que la décision locale (§7.4.9). La réponse reste celle d'après l'exécution
                # (executed/failed, lus par la Mini App) quand elle tient dans `DECISION_WAIT_S` ; sinon (un tour
                # tourne encore sur le fil) : 200 avec la carte telle qu'elle est (`approved`), l'exécution suit en
                # fond — la Mini App affiche alors « Approuvé, Nina va rendre compte sur le fil d'origine ». Sans
                # borne, Cloudflare coupait à 100 s (524) alors que la décision était enregistrée (revue du 2.11).
                if not schedule_decision(decided, continue_rejected=True)[1].wait(DECISION_WAIT_S):
                    logger.info("décision %s : exécution encore en file après %.0f s, réponse sans attendre",
                                decided.id, DECISION_WAIT_S)
                final = approvals.get(decided.id) or decided
                self._json(200, approval_to_json(final))
                return

            if path == "/v1/sessions":
                device_id = str(body.get("device_id", "")).strip()
                challenge_text = body.get("challenge")
                signature = body.get("signature")
                webauthn_raw = body.get("webauthn")
                webauthn = None
                if webauthn_raw:
                    webauthn = {
                        "client_data_json": webauthn_raw.get("client_data_json") or webauthn_raw.get("clientDataJSON"),
                        "authenticator_data": webauthn_raw.get("authenticator_data")
                        or webauthn_raw.get("authenticatorData"),
                        "signature": webauthn_raw.get("signature"),
                    }
                if not device_id or not challenge_text or not (signature or webauthn):
                    self._json(400, {"error": {"message": "device_id/challenge et signature ou webauthn manquants"}})
                    return
                scope.enter_context(tenant_scope(device_scope(device_id)))
                try:
                    session = approvals.create_session(device_id, challenge_text, signature, webauthn=webauthn)
                except (ApprovalError, DeviceError) as e:
                    self._json(_approvals_error_status(e), {"error": {"message": str(e)}})
                    return
                self._json(200, {"token": session.token, "user": session.user, "device_id": session.device_id,
                                 "expires_at": _iso(session.expires_at)})
                return

            self._json(404, {"error": {"message": "not found"}})

        def do_DELETE(self) -> None:
            path = urlparse(self.path).path
            if self._local_route("DELETE", path):
                return
            if path.startswith("/v1/mirror/pages/") and cloud_http.handle(self, "DELETE", path):  # pages copiées
                return
            if incidents_http.handle(self, "DELETE", path) or plan_http.handle(self, "DELETE", path):  # X18, drapeaux
                return
            if code_http.handle(self, "DELETE", path):  # sessions de code multi-utilisateur (§7.28), bloc `code`
                return
            if path.startswith("/v1/connectors") or (team_http.enabled() and team_http.is_team_path(path)) \
                    or (packs_http.enabled() and packs_http.is_packs_path(path)) \
                    or (long_http.enabled() and long_http.is_long_memory_path(path)) \
                    or (consolidation_http.enabled() and consolidation_http.is_consolidation_path(path)) \
                    or (sso_http.enabled() and sso_http.is_sso_path(path)) \
                    or (household_http.enabled() and household_http.is_household_path(path)) \
                    or (paper_http.enabled() and paper_http.is_paper_path(path)) \
                    or (famcal_http.enabled() and famcal_http.is_famcal_path(path)) \
                    or payments_http.is_payments_path(path):
                self._account_route("DELETE", path)
                return
            if hub is not None and (_RE_THREAD_ONE.match(path) or _RE_SHARE_ONE.match(path)):
                self._bubbles_route("DELETE", path, {})
                return
            if agents is not None and path.startswith("/v1/agents/"):
                self._agents_route("DELETE", path)
                return
            if approvals is None:
                self._json(404, {"error": {"message": "not found"}})
                return
            m = _RE_DEVICE_ONE.match(path)
            if not m:
                self._json(404, {"error": {"message": "not found"}})
                return
            sender = self._resolve_identity()
            if sender is None:
                self._json(401, {"error": {"message": "unauthorized"}})
                return
            tid = self._tenant_for(sender)
            if tid is None:
                return
            with tenant_scope(tid):
                device = approvals.get_device(m.group(1))  # appareil d'un autre locataire : None → 404
                if device is None:
                    self._json(404, {"error": {"message": "appareil inconnu"}})
                    return
                if device.user != sender:
                    self._json(403, {"error": {"message": "cet appareil n'appartient pas à l'identité résolue"}})
                    return
                approvals.delete_device(device.id)
            self._json(200, {"deleted": True})

    return Handler


def _start_notify_sweeper(hub: BubbleHub, period_s: float = 30.0) -> None:
    """Ferme les bulles `notify` expirées (10 min, docs/BUBBLES.md §4) même si personne ne lit l'API."""
    import threading

    def loop() -> None:
        while True:
            time.sleep(period_s)
            try:
                hub.sweep_expired()
            except Exception:  # noqa: BLE001
                logger.exception("balayage des notifications en échec")

    threading.Thread(target=loop, daemon=True, name="nokker-notify-sweeper").start()


def crew_engine_enabled(config: NokkerConfig) -> bool:
    """Le moteur tourne dans Lokkal (sauf `local.crew.enabled: false`) ; ailleurs (nokker.ai) seulement sur demande
    explicite (`NOKKER_CREW_ENGINE=1`) : des agents qui tournent chez nokker.ai, c'est une décision d'offre (§6.7)."""
    if config.local:
        return bool((config.local.get("crew") or {}).get("enabled", True))
    return os.environ.get("NOKKER_CREW_ENGINE", "").strip() == "1"


def build_mail_watch(config: Any, engine: Any, connectors: Any) -> Any:
    """Relève du courrier du déclencheur `mail:<filtre>` (docs/BUBBLES.md §9.5, `nokker/agents/mailwatch.py`) : seulement
    si l'instance a des connecteurs (`connectors` = `ConnectorProvider`, la boîte se construit comme pour un tour) et
    sauf `local.crew.mail: false`. Réglages `local.crew` : `mail_poll_s` (120, borné à [30, 3600]),
    `mail_max_runs_per_hour` (12). Curseurs en Postgres (`nokker_mail_watch`) si un DSN est configuré. None : le
    déclencheur reste refusé (`NO_MAIL_TRIGGER`)."""
    crew = ((config.local or {}).get("crew") or {}) if getattr(config, "local", None) else {}
    if connectors is None or not hasattr(connectors, "mail_for") or not crew.get("mail", True):
        return None
    from nokker.agents.mailwatch import DEFAULT_MAX_RUNS_PER_HOUR, DEFAULT_POLL_S, MailWatcher, MemoryWatchStore

    dsn = config.pg_dsn()
    if dsn:
        from nokker.agents.mailwatch import PgWatchStore

        store: Any = PgWatchStore(dsn)
    else:
        store = MemoryWatchStore()
    return MailWatcher(engine=engine, mailbox=connectors.mail_for, store=store,
                       poll_s=float(crew.get("mail_poll_s", DEFAULT_POLL_S)),
                       max_runs_per_hour=int(crew.get("mail_max_runs_per_hour", DEFAULT_MAX_RUNS_PER_HOUR)),
                       loop_s=float(crew.get("tick_s", 15)))


def start_crew_engine(config: NokkerConfig, agents_hub: AgentsHub, kernel: Kernel, *, hub: BubbleHub | None,
                      approvals: BaseApprovals | None, tenancy: Tenancy, local_api: Any | None, remote: Any = None,
                      local_runs: bool = True, start: bool = True, connectors: Any = None) -> Any:
    """Moteur Crew (lot X14, docs/BUBBLES.md §9.5) : passages réels des agents, ordonnanceur en fil de fond. Les
    secrets d'un passage viennent du coffre local (X4) ; sans coffre, un agent qui en déclare est `skipped`.
    `connectors` (le `ConnectorProvider` des tours) : la relève du courrier du déclencheur `mail:` (`build_mail_watch`)."""
    from nokker.agents.engine import AgentEngine
    from nokker.store.conn import known_tenants

    account_tenant = account_tenant_fn(tenancy)
    dsn = config.pg_dsn()
    vault = local_api.vault if local_api is not None else None
    setup = local_api.setup if local_api is not None else None
    if setup is not None:
        agents_hub.local_machine = lambda: setup.state.get("machine_id")
    crew = config.local.get("crew") or {} if config.local else {}
    engine = AgentEngine(
        hub=agents_hub, kernel=kernel, bubbles=hub, approvals=approvals,
        resolver=vault.resolver if vault is not None else None,
        tenants=(lambda: known_tenants(dsn)) if dsn else None,
        tenant_of=lambda a: (account_tenant(a.created_by) or "") if is_account(a.created_by) else INTERNAL_TENANT,
        tick_s=float(crew.get("tick_s", 15)), max_concurrent=int(crew.get("max_concurrent", 2)),
        misfire_s=float(crew.get("misfire_s", 6 * 3600)), remote=remote, local_runs=local_runs)
    engine.mail_watch = build_mail_watch(config, engine, connectors)
    if start:
        engine.start()
    logger.info("crew : moteur des agents lancé (tick %.0f s, %d passages en même temps au plus%s)", engine.tick_s,
                engine.max_concurrent, ", relève du courrier toutes les %.0f s" % engine.mail_watch.poll_s
                if engine.mail_watch is not None else "")
    return engine


def build_tenancy(config: NokkerConfig) -> Tenancy:
    """Comptes et locataires (docs/TENANCY.md) : Postgres si un DSN est configuré, mémoire sinon (dev, tests)."""
    dsn = config.pg_dsn()
    return PgTenancy(dsn) if dsn else MemoryTenancy()


def link_endpoint(state: dict[str, Any], get_secret: Any) -> tuple[str, str] | None:
    """(base_url, clé) du lien de compte, ou None sans lien. La clé du lien est un jeton de MACHINE valable sur
    l'inférence relayée par nokker.ai (`state["inference_base"]`, `<nokker.ai>/v1/inference`), PAS sur le Router
    (`router.sokkan.ch`) qui la refuse (401 « invalid or revoked API key ») ; un lien ancien sans `inference_base`
    retombe sur le Router (même règle que `LocalSetup.provider_for_default`)."""
    from nokker.local.setup import ROUTER_URL

    key = get_secret("router") or ""
    return (str(state.get("inference_base") or ROUTER_URL), key) if key else None


def web_url_of_lokkal() -> str:
    """Adresse du WEB de Lokkal sur la machine (page /setup), pas celle de l'API (8742, qui répond 401 sans jeton) :
    `NOKKER_WEB_URL`, sinon `http://127.0.0.1:${NOKKER_PORT}` (port web du compose, 7420 par défaut)."""
    url = os.environ.get("NOKKER_WEB_URL") or f"http://127.0.0.1:{os.environ.get('NOKKER_PORT') or 7420}"
    return url.rstrip("/")


def setup_guard(kernel: Kernel, setup: Any, web_url: str, run_turn: Any, tier_keys: Any = lambda: False,
                token: Any = lambda: None) -> Any:
    """Lokkal sans AUCUN modèle utilisable (ni compte relié, ni byok avec modèle, ni palier de config avec clé ; `done`
    seul ne bloque pas : un Lokkal mis à jour reste servi) : le tour s'arrête AVANT l'appel au modèle, avec une phrase
    pour la personne (pas l'erreur brute du palier) et le lien de la page de configuration du WEB. Le tour est un ÉCHEC
    (`failure` `no_model` : `turn.done.error`, code de sortie non nul du client terminal), annoncé aux observateurs
    (`turn.started` / `turn.done`) mais jamais enregistré dans le fil : le message n'entre pas dans l'historique que le
    modèle relira. Le jeton local n'entre dans le lien (`/setup#token=…`, fragment jamais envoyé au serveur) que pour un
    tour arrivé SUR la machine (`api`) : jamais par la passerelle ni par Telegram. Message destiné à la personne."""
    base = f"Aucun modèle n'est configuré. Termine la configuration : {web_url}/setup"

    def guarded(inp: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            ready = setup.has_model(bool(tier_keys()))
        except Exception:  # noqa: BLE001 — état illisible : on ne bloque pas sur une panne du garde
            ready = True
        if ready:
            return run_turn(inp, *args, **kwargs)
        from nokker.core.turn import Message

        tok = None
        if getattr(inp, "channel", None) == "api":
            try:
                tok = token()
            except Exception:  # noqa: BLE001 — sans jeton, le lien reste valable (la page le demande)
                tok = None
        turn = Turn(thread_id=inp.thread_id, index=kernel.store.next_index(inp.thread_id),
                    input=kernel.invariants.check_input(inp.user_text), channel=inp.channel, sender=inp.sender,
                    reply=base)
        turn.messages.append(Message(role="assistant", content=base))
        turn.failure = {"code": "no_model", "message": base}  # jamais le jeton dans l'erreur
        # Les observateurs (bus d'événements : turn.delta, turn.done, que la passerelle peut relayer) ne voient QUE la
        # phrase sans jeton ; le lien avec jeton ne part que dans la réponse rendue à l'appelant, sur la machine.
        kernel._observe("turn_started", turn)
        kernel._observe("turn_done", turn)
        if tok:
            message = f"{base}#token={tok}"
            turn.reply = message
            turn.messages[-1] = Message(role="assistant", content=message)
        return turn

    return guarded


def default_model_applier(config: NokkerConfig, kernel: Kernel, link_endpoint_fn: Any) -> Any:
    """Rend `use_default_model(base_url, clé, modèle)` : branche à chaud le modèle par défaut de Lokkal sur le noyau.
    Appelée au démarrage (Lokkal déjà lié) ET à l'approbation du lien : la clé reçue sert tout de suite. Les paliers et
    les juges de la config qui visent le Router (`router.sokkan.ch`) sans `ROUTER_KEY` passent, comme Ship, par
    l'inférence du lien (base ET clé du lien). `link_endpoint_fn()` rend `(base, clé)` du lien, ou None. La clé ne sort
    jamais : ni journal, ni erreur."""
    from nokker.local.setup import ROUTER_URL

    def same(a: str, b: str) -> bool:
        return a.rstrip("/") == b.rstrip("/")

    def use_default_model(base_url: str, api_key: str, model: str) -> None:
        # Le modèle choisi à l'onboarding sert les deux paliers, en direct (pas de redémarrage).
        mode = config.vision_mode_for_model(model)  # palier de la config qui sert ce modèle, sinon local.vision
        probe = None
        if mode == "auto":
            from nokker.providers.vision_probe import VisionProbe

            probe = VisionProbe(base_url, model, config.vision_probe_path())
        provider = SokkanProvider(base_url=base_url, api_key=api_key, model=model, vision=mode == "true",
                                  vision_probe=probe)
        kernel.providers[Tier.SHIP] = provider
        kernel.providers[Tier.DEEP] = provider
        link = link_endpoint_fn()

        def endpoint_of(cfg: Any) -> tuple[str, str] | None:
            """(base, clé) sous lesquels un palier ou le juge de la config sert : ceux de la config avec ROUTER_KEY ;
            sans elle, le lien si la config vise le Router ou le point d'accès du lien."""
            if cfg.api_key():
                return cfg.base_url, cfg.api_key()
            if link is not None and (same(cfg.base_url, ROUTER_URL) or same(cfg.base_url, link[0])):
                return link
            if same(cfg.base_url, base_url):  # même point d'accès que le modèle choisi (clé de l'utilisateur)
                return base_url, api_key
            return None

        # X8 : hors du point d'accès du petit palier (clé de l'utilisateur, modèle local), un seul modèle — le petit
        # palier part (la politique se replie sur Ship) et le juge non choisi en config suit le modèle choisi.
        # Quand le modèle choisi est celui du lien (ou du Router), le petit palier est (re)construit sur le même point
        # d'accès avec ROUTER_KEY si elle est posée, sinon avec le lien : il n'est plus « sans clé » une fois lié.
        small = config.tiers.get("small")
        ep = endpoint_of(small) if small is not None and (same(small.base_url, base_url)
                                                          or same(base_url, link[0] if link else "")) else None
        if small is None or ep is None:
            kernel.providers.pop(Tier.SMALL, None)
        else:
            small_extra = ({"reasoning_off_body": small.reasoning_off_body}
                           if small.reasoning_off_body is not None else {})
            kernel.providers[Tier.SMALL] = SokkanProvider(base_url=ep[0], api_key=ep[1], model=small.model,
                                                          vision=small.vision, **small_extra)
            logger.info("lokkal : petit palier branché (%s)", small.model)
        for role in (kernel.reviewer, kernel.advisor):
            t = getattr(role, "transport", None)
            if t is None:
                continue
            if not config.judge_explicit:
                t.base_url, t.api_key, t.model = base_url, api_key, model
                t._plain = False  # le nouvel amont accepte peut-être `reasoning_effort`
                role.model_name = model
                continue
            # Juge choisi en config : il garde son modèle ; sur le Router sans ROUTER_KEY, il passe par le lien.
            jep = endpoint_of(config.judge_config())
            if jep is not None:
                t.base_url, t.api_key = jep

    return use_default_model


def serve(config: NokkerConfig | None = None, kernel: Kernel | None = None,
          ledger: LedgerBackend | None = None) -> None:
    config = config or load_config()
    from nokker.code_sessions.config import CodeConfig

    CodeConfig.from_config(config.code, lokkal=config.local is not None)  # bloc `code` refusé : arrêt, tout de suite
    check_rls_role(config)
    apply_corthexis_schema(config)
    tenancy = build_tenancy(config)
    if not config.api_key_map() and config.dev_open_api:
        logger.warning("dev_open_api : serveur OUVERT, toute requête sans identité tombe dans %s", INTERNAL_TENANT)
    ledger = ledger if ledger is not None else build_ledger(config)
    # Nokker Lokkal (docs/LOKKAL.md §7) : jeton local, Host autorisés, sessions de code. Absent = bêta, rien de tout ça.
    local_api = LocalApi.from_config(config.local, config.port) if config.local else None
    if local_api is not None and local_api.vault is not None:
        local_api.vault.startup_report()  # fournisseur choisi ; adresse OpenBao sans choix, coffre scellé : un avis
    if local_api is not None:  # sessions de code : leur préfixe (le dossier, le bac à sable, les tests)
        import dataclasses

        code_prefix = config.local.get("prefix_path") or str(Path(__file__).parent.parent / "prompts"
                                                             / "prefix-lokkal-code.md")
        if local_api.project_memory is not None:  # mémoire de projet (X1) : la règle « noter ce qu'on apprend »
            from nokker.context.project_memory import code_prefix as _code_prefix

            code_prefix = _code_prefix(code_prefix, project_memory=True)
        config = dataclasses.replace(config, prefix_path=code_prefix, profile_path=None)
    config_mcp_names = [str(s.get("name") or "") for s in config.mcp_tool_servers]
    if local_api is not None:
        # Lot X16 : serveurs MCP ajoutés depuis une session (`mcp_server_add`, carte) et effets déclarés par les packs
        # installés, lus AU DÉMARRAGE seulement. Un effet de pack ne fait que relever la table (jamais la baisser) ;
        # un nom de serveur déjà pris par config.yaml l'emporte.
        from nokker.tools.packs import user_home, with_user_extensions

        config = with_user_extensions(config, user_home(local_api.state_dir))
    events = EventBus() if (config.approvals_enabled or config.bubbles_enabled or config.agents_enabled) else None
    hub = build_hub(config, events) if config.bubbles_enabled else None
    # Crew minimal (docs/BUBBLES.md §9.4) : indépendant de `hub`/`approvals` — posé sur `hub.agents_hub`
    # ci-dessous pour que l'activation d'un `agent_draft` (côté `hub`) y accède.
    agents_hub = build_agents(config, events) if config.agents_enabled else None
    # UNE SEULE instance du backend `Approvals`, partagée par le `Kernel` (qui pose les cartes sur
    # `Decision.HITL`) ET par les routes HTTP (qui les servent/décident) — sinon les deux verraient des
    # approbations différentes. `kernel=` explicite (appelant qui construit son propre noyau) : on lit
    # alors `kernel.approvals` au lieu d'en reconstruire un second.
    provider = None  # connecteurs des tours ; aussi la boîte relevée par le déclencheur `mail:` (start_crew_engine)
    home_watch = None  # veille du foyer (docs/HOME-WATCH.md), drapeau `home_watch.enabled`, Lokkal seulement
    mcp_config_source = None  # Lokkal : outils mcp_server_* (lot X16), dont le plancher des déclarations en lecture
    home = None  # maison (docs/HOME-ASSISTANT.md), drapeau `home_assistant.enabled`
    if kernel is not None:
        approvals = kernel.approvals
        hub = getattr(kernel, "observer", None) if isinstance(getattr(kernel, "observer", None), BubbleHub) else None
    else:
        approvals = build_approvals(config, on_event=events.publish if events is not None else None) \
            if config.approvals_enabled else None
        connector_store = build_connector_store(config)
        resolve = local_api.vault.resolver().resolve if local_api is not None and local_api.vault is not None else None
        # Localité des connecteurs (docs/LOKKAL.md §6.7) : posés dans Lokkal = `local`, chez nokker.ai = `cloud`.
        locality = "local" if local_api is not None else "cloud"
        provider = ConnectorsForTenant(connector_store, tenancy.account_for_tenant, resolve=resolve,
                                       locality=locality) if connector_store else None
        delegate = None
        extra = None
        if local_api is not None:
            # Sous-agents (L3) : l'enfant est une copie du noyau parent (mêmes portes, approbations,
            # fournisseurs, max_tokens, max_calls) autour du registre filtré par `delegate`.
            import dataclasses as _dc

            from nokker.tools.delegate import DelegateToolSource

            parent: dict[str, Kernel] = {}
            # Hooks (X3) : le registre filtré de l'enfant en est entouré aussi ; le coureur ne lance chez un sous-agent
            # que les hooks d'un bloc `"subagents": true` (héritage explicite, nokker/tools/hooks.py).
            def child_tools(registry: Any) -> Any:
                # Même enveloppe que le registre du parent : hooks (X3), puis journal d'audit (X11) par-dessus.
                tools = local_api.hooks.wrap(registry) if local_api.hooks is not None else registry
                audited = getattr(parent["k"], "tools", None)
                return audited.wrap(tools) if isinstance(audited, AuditedTools) else tools

            def note_parent(thread_id: str, line: str) -> None:
                # Fin d'enfants de fond : une ligne dans le contexte du PROCHAIN tour du fil parent (comme « hors tour »).
                parent["k"].add_owner_note(thread_id, line)

            delegate = DelegateToolSource(
                lambda registry, thread_id, observer: _dc.replace(
                    parent["k"], observer=observer, tools=child_tools(registry)),
                local_api.code, emit=events.publish if events is not None else None, parent_observer=hub,
                background=True, note=note_parent,
                # état des enfants dans l'état local : au redémarrage, `interrupted / restart` et worktree conservé
                state_path=(local_api.state_dir / "subagents.json") if local_api.state_dir is not None else None,
                fork=bool(getattr(config, "delegate_fork", False)))
            local_api.subagents = delegate  # annulation d'un fil → ses sous-agents ; `delegate_status` hors tour
            local_api.emit = events.publish if events is not None else None  # `checkpoint.created` (§7.4.7)
            if getattr(local_api.code, "lsp", None) is not None and hub is not None:
                local_api.code.lsp.scrub = hub._scrub  # `lsp.diagnostics` caviardés comme les `tool.result` (lot 13)
            extra = [*local_api.tool_sources(), delegate]
            if config.execute_code_enabled:
                # execute_code (docs/LOKKAL.md §7.31) : le script tourne dans le bac à sable de la session du fil.
                from nokker.tools.execute_code import ExecuteCodeSource

                _code = local_api.code
                extra.append(ExecuteCodeSource.from_config(lambda ctx: _code.for_thread(ctx.thread_id),
                                                           config.execute_code))
            if local_api.connectors is not None:
                # Boîte mail posée dans Lokkal (docs/LOKKAL.md §7.25) : avant les connecteurs relayés, elle gagne.
                extra.append(local_api.connectors.source())
            # Packs d'agents et serveurs MCP ajoutés (lot X16) : installer pose des compétences scannées et PROPOSE
            # des agents (bulles agent_draft) ; ajouter un serveur écrit un fichier lu au prochain démarrage.
            from nokker.bubbles.tools import pack_removed, propose_agent_draft
            from nokker.tools.mcp_config import FILE_NAME as _MCP_FILE, McpConfigToolSource
            from nokker.tools.packs import PackToolSource, user_home

            _hub = hub

            def _propose(args: dict[str, Any], ctx: Any, extra_state: dict[str, Any]) -> Any:
                return propose_agent_draft(_hub, ctx, args, extra_state)

            _vault = local_api.vault
            ext_dir = user_home(local_api.state_dir)  # le même dossier que celui lu au démarrage
            def _removed(pack: str, bubble_ids: list[str], ctx: Any) -> dict[str, Any]:
                return pack_removed(_hub, ctx, pack, bubble_ids)

            pack_source = PackToolSource(home=ext_dir, propose=_propose if _hub is not None else None,
                                         on_remove=_removed if _hub is not None else None)
            extra.append(pack_source)
            # Route locale du retrait (POST /v1/local/packs/{nom}/remove) : la même source, la même carte.
            local_api.packs = pack_source
            local_api.packs_thread = (lambda tid: _hub.threads.get(tid) is not None) if _hub is not None else None
            mcp_config_source = McpConfigToolSource(
                path=ext_dir / _MCP_FILE,
                known_secrets=(lambda: [i.name for i in _vault.list()]) if _vault is not None else None,
                configured=lambda: list(config_mcp_names))
            extra.append(mcp_config_source)
            if local_api.relayed_enabled and local_api.setup is not None:
                # Connecteurs relayés (lot X20, drapeau `local.relayed_connectors`) : les connecteurs posés chez
                # nokker.ai, appelés par le lien de compte ; l'identifiant reste là-bas. En dernier : un outil local
                # du même nom passe devant.
                from nokker.local.relayed import RelayedConnectors

                _setup = local_api.setup
                local_api.relayed = RelayedConnectors(
                    base_url=local_api.link.base_url if local_api.link else "https://app.nokker.ai",
                    token=lambda: _setup.vault.get("machine_token"), approvals=approvals,
                    journal_path=(local_api.state_dir or Path("~/.config/nokker").expanduser()) / "relay-audit.jsonl")
                extra.append(local_api.relayed)
        home = _home_source(config, local_api)
        # Salle Maison allumée (docs/HOUSE-ROOM.md §2.2) : les outils `maison*` n'existent que dans la salle (agent
        # Maison, fournisseur enregistré par `register_house_providers`), plus dans le registre ordinaire. Éteinte
        # (défaut) : registre d'avant.
        if home is not None and not house_room_on(config):  # maison : interne seulement, jamais chez un testeur
            extra = [*(extra or []), home]
        home_watch = _home_watch_source(config, local_api)
        if home_watch is not None:  # veille du foyer (docs/HOME-WATCH.md) : Lokkal seulement, interne seulement
            extra = [*(extra or []), home_watch]
        kernel = build_kernel(config, ledger=ledger, approvals=approvals, hub=hub, connectors=provider,
                              extra_sources=extra, secrets=local_api.vault if local_api is not None else None)
        if delegate is not None:
            parent["k"] = kernel
            delegate.bind(getattr(kernel.tools, "internal", kernel.tools))
        if local_api is not None and (config.local or {}).get("folder_mode", False):
            # Mode dossier (docs/FOLDER-MODE.md, drapeau `local.folder_mode`) : chaque passage tourne sur une copie du
            # noyau qui ne voit que les outils du dossier (`folder_kernel`), lue au moment du passage.
            from nokker.folder.run import FolderRuns, folder_kernel

            local_api.folder = FolderRuns(state_dir=local_api.state_dir or Path("~/.config/nokker").expanduser(),
                                          make_kernel=lambda src: folder_kernel(kernel, src),
                                          emit=events.publish if events is not None else None)
        if local_api is not None and local_api.connectors is not None:
            local_api.connectors.on_change = lambda: kernel.tools.refresh()  # boîte posée ou retirée : l'index suit
        if local_api is not None and local_api.relayed is not None:
            local_api.relayed.on_change = lambda: kernel.tools.refresh()  # liste changée : l'index du tour suit
            local_api.relayed.start()  # relue toutes les 60 s ; machine non liée : aucun outil, aucun appel
        connectors_http.configure(Connectors(connector_store, resolve=resolve, locality=locality)
                                  if connector_store else None)
        configure_onboarding(config, tenancy, approvals)
        configure_account(config, kernel, approvals)
        configure_billing(config)
        if billing_http.service() is not None and kernel.limits is not None:
            # Plafonds du palier payé (docs/TENANCY.md) : un abonné actif a ceux de son plan, les autres ceux de la bêta.
            kernel.limits.caps_for = billing_http.service().caps_for
        configure_cloud_link(config, tenancy, approvals, ledger, store=kernel.store,
                             connector_store=connector_store if local_api is None else None,
                             invariants=kernel.invariants, limits=kernel.limits)
        configure_team(config, tenancy, agents=agents_hub, scrub=kernel.invariants.scrub_output)
        configure_sso(config, tenancy, approvals)
        configure_plan_operate(config, kernel, agents=agents_hub, hub=hub, approvals=approvals, tenancy=tenancy,
                               events=events, connector_store=connector_store if local_api is None else None)
        configure_team_budget(config, kernel, agents=agents_hub)
        # Sessions de code AVANT le foyer : les portes des mineurs enveloppent celles des sessions (qui autorisent selon
        # le mode sans repasser par les portes internes), et le foyer branche le refus d'une session à un mineur.
        configure_code_sessions(config, kernel, hub=hub, events=events)
        configure_household(config, kernel, tenancy, approvals)
        if local_api is None:  # papiers du foyer : comptes de nokker.ai seulement
            configure_household_paper(config, kernel, tenancy)
        if local_api is None:  # calendrier familial : comptes de nokker.ai (après les papiers : PDF importés)
            configure_family_calendar(config, kernel, tenancy)
        configure_payments(config, kernel, tenancy, approvals, events)  # après le foyer (portes des mineurs dedans)
        if cloud_http._cloud is not None:  # skill « sessions Lokkal » (§6.4) pour les comptes liés : outil DESTRUCTIVE
            from nokker.cloud.lokkal_session import LokkalSessionTools

            kernel.tools.tester_sources.append(LokkalSessionTools(links=cloud_http._cloud.links,
                                                                  signer=cloud_http._cloud.signer))
            if cloud_http._cloud.handoffs is not None:
                # Délégation par besoin (X21) : le modèle PROPOSE (outil READ), l'utilisateur accepte (bulle ou route) ;
                # le résultat revient en bulle et dans le contexte du fil web.
                from nokker.cloud.handoff import HandoffTools, bubble_updater

                service = cloud_http._cloud.handoffs
                service.scrub = kernel.invariants.scrub_output
                if hub is not None:
                    hub.handoffs = service
                    service.on_update = bubble_updater(hub)
                kernel.tools.tester_sources.append(HandoffTools(service=service,
                                                                tenant_of=cloud_http._cloud.tenant_of))
            # Ces deux outils n'existent que pour un compte qui a une machine liée et non révoquée (`available_for`) :
            # un lien ou une révocation reconstruit le jeu d'outils de son locataire.
            if hasattr(kernel.tools, "forget"):
                cloud_http._cloud.links.on_change = kernel.tools.forget
            kernel.tools.refresh()
        configure_feedback(config, kernel)
        configure_telegram(config, tenancy, kernel, hub, approvals)
        configure_channels(config, tenancy, kernel, hub, approvals)  # Slack, WhatsApp : éteints par défaut
        configure_packs(config, kernel, hub, local=local_api is not None)
        if local_api is None:  # Lokkal : après ses portes (permissions), plus bas
            configure_learning(config, kernel, events, tenancy=tenancy)
            configure_long_memory(config, kernel, tenancy)  # après le foyer et la quarantaine
            configure_consolidation(config, kernel, tenancy, hub)  # après la mémoire longue
        register_house_providers(home=home, home_watch=home_watch)  # agents de la salle Maison (si allumée)
        room = configure_house_room(config, kernel, events)  # après le foyer : portes des mineurs déjà posées
        configure_presence(config, kernel, events=events, hub=hub, approvals=approvals, agents=agents_hub, room=room)
        configure_guide(config, kernel, events=events, tenancy=tenancy)
    configure_ambient(config, kernel)  # écrans e-ink et voix de la pièce : éteints par défaut
    if hub is not None:
        hub.agents_hub = agents_hub
        if local_api is not None:
            hub.pages = local_api.pages  # une page déplacée vers un autre fil le suit (docs/BUBBLES.md §10.5)
            if getattr(local_api, "cast", None) is not None:
                # Écrans du foyer (docs/SCREEN-CAST.md) : caviardage du noyau (coffre compris) sur tout ce qui va à
                # l'écran, bulle `cast` par affichage, décisions par la bulle.
                local_api.cast.scrub = kernel.invariants.scrub_output
                hub.cast = local_api.cast
                hub.producers.cast_state = local_api.cast.bubble_state
                local_api.cast.on_change = hub.cast_changed
    if local_api is not None:
        from nokker.qa.permissions import LocalPermissions, Mode, PermissionGates

        from nokker.tools.code import reap_sandboxes

        if local_api.code.backend == "docker" and (n := reap_sandboxes(local_api.code.instance or "")):
            logger.info("lokkal : %d bac(s) à sable orphelin(s) supprimé(s) au démarrage", n)
        if local_api.code.backend == "local" and local_api.code.state_dir \
                and (n := reap_sandboxes("", processes_dir=os.path.join(local_api.code.state_dir, "processes"))):
            logger.info("lokkal : %d processus en arrière-plan orphelin(s) arrêté(s) au démarrage", n)
        kernel.max_calls = int(config.local.get("max_calls", 60))  # une tâche de code fait des dizaines d'appels
        kernel.max_tokens = int(config.local.get("max_tokens", 16384))
        kernel.budget_notice_at = config.local.get("budget_notice_at", 0.8)  # « termine maintenant » à 80 % du budget
        kernel.stall_limit = config.local.get("stall_limit", 6)  # 6 appels répétés à l'identique : conclure
        from nokker.qa.escalate import LocalEscalator

        kernel.escalator = LocalEscalator()  # en local, personne d'autre que le propriétaire

        def link_endpoint_fn() -> tuple[str, str] | None:
            """(base, clé) du lien lus au coffre local à chaque appel ; None sans lien ou coffre illisible."""
            if local_api.setup is None:
                return None
            try:
                return link_endpoint(local_api.setup.state, local_api.setup.vault.get)
            except Exception:  # noqa: BLE001 — coffre sans clé : pas de lien, jamais d'erreur qui porte la clé
                return None

        use_default_model = default_model_applier(config, kernel, link_endpoint_fn)

        local_api.on_default_model = use_default_model
        try:
            chosen = local_api.setup.provider_for_default() if local_api.setup is not None else None
        except Exception:  # noqa: BLE001 — coffre sans clé : les paliers de la config restent
            logger.warning("lokkal : modèle par défaut illisible au démarrage, paliers de la config gardés", exc_info=True)
            chosen = None
        if chosen is not None:
            use_default_model(*chosen)
        for flag, role in (("judge_review", kernel.reviewer), ("judge_advice", kernel.advisor)):
            t = getattr(role, "transport", None)
            if t is not None and not t.api_key:  # ni ROUTER_KEY ni lien de compte : le juge échouera à chaque appel
                logger.warning("%s sans clé du juge (ROUTER_KEY ni lien de compte) : carte sans avis et diff non relu",
                               flag)
        if config.local.get("reasoning", True):  # banc L0 (L01, 23.10) : sans raisonnement, le 397B pense à voix haute
            getattr(kernel.policy, "inner", kernel.policy).ship_reasoning = True
        from nokker.tools.code import CODE_TOOLS

        kernel.gates = PermissionGates(inner=kernel.gates, permissions=LocalPermissions.load(
            # accept_edits par défaut : chaque écriture du dossier est photographiée (points de retour), donc
            # réversible ; les commandes et le destructif restent demandés.
            mode=config.local.get("mode"), fallback=Mode.ACCEPT_EDITS,
            user_settings=Path(config.local.get("settings_path") or "~/.config/nokker/settings.json").expanduser()),
            code_session=lambda tid: local_api.code.for_thread(tid) is not None,
            # GO textuel : valable dans le bac à sable d'une session de code seulement (docker ou kubernetes, jamais le
            # backend « local », qui tourne sur l'hôte) ; ailleurs le destructif reste une carte forte.
            go_sandbox=lambda tid: local_api.code.backend != "local" and local_api.code.for_thread(tid) is not None,
            session_tools=frozenset(CODE_TOOLS))
        perms = kernel.gates.permissions
        local_api.permissions = perms  # `GET|PUT /v1/local/sessions/{fil}/mode` (§7.4.10)
        if local_api.project_skills is not None:
            # Skills du dépôt (drapeau `local.project_skills`) : jamais en mode plan (mode du fil, dépôt compris),
            # `skill.untrusted` à l'ouverture d'un dépôt dont l'arbre n'a pas encore la confiance de l'utilisateur.
            local_api.project_skills.mode_of = perms.mode_for
            local_api.project_skills.emit = events.publish if events is not None else None
        if local_api.hooks_enabled:
            # Hooks des sessions (lot X3, drapeau `local.hooks`) : autour du registre du noyau, donc de tout appel
            # (tour, appel direct du propriétaire, carte approuvée) ; commandes dans le bac à sable du fil.
            from nokker.tools.hooks import HookRunner

            local_api.hooks = HookRunner(sandbox=local_api.code.run_hook, user_settings=local_api.settings_path,
                                         scrub=kernel.invariants.scrub_output, redact=kernel.invariants.redact_result,
                                         mode_of=perms.mode_for, emit=events.publish if events is not None else None)
            kernel.tools = local_api.hooks.wrap(kernel.tools)

        team_rules = None
        if config.local.get("team") and local_api.setup is not None:
            # Équipe (X17, drapeau `local.team`) : les règles des projets qui déclarent le dépôt restreignent le fil.
            from nokker.local.team import TeamRules

            setup = local_api.setup
            team_rules = TeamRules(base_url=local_api.link.base_url if local_api.link else "https://app.nokker.ai",
                                   token=lambda: setup.vault.get("machine_token") or "", state=setup.state,
                                   save=setup._save)

        def on_session_open(thread_id: str, cwd: str) -> dict[str, Any] | None:
            # Le dépôt restreint son fil (comme `nokker code`), AVANT les hooks : en mode plan, aucun ne tourne.
            perms.restrict_for_thread(thread_id, Path(cwd) / ".nokker" / "settings.json")
            if team_rules is not None:  # puis les projets d'équipe du dépôt (restreindre seulement, X17)
                from nokker.local.team import restrict_session

                restrict_session(perms, team_rules, thread_id, cwd, local_api._root_of(cwd) or cwd)
            return {"hooks": local_api.hooks.open_session(thread_id, cwd)} if local_api.hooks is not None else None

        local_api.on_session_open = on_session_open
        if local_api.project_memory is not None:  # X1 : noyau dans la fenêtre, rappel du projet, événements
            from nokker.context.project_memory import install as install_project_memory

            local_api.project_memory.emit = events.publish if events is not None else None
            install_project_memory(kernel, local_api.project_memory)
        if mcp_config_source is not None and hasattr(kernel.gates, "floors"):
            # Outils déclarés en lecture au nom d'écriture évidente : carte FORTE, qu'aucun mode ni règle ne lève.
            kernel.gates.floors.append(mcp_config_source.floor)
        if local_api.ssh is not None:
            # Ops (lot X11, drapeau `local.ops`) : plancher par rôle devant les permissions, puis le journal d'audit
            # autour du registre ENTIER (hooks compris), donc de tout appel exécuté (tour, appel direct, carte
            # approuvée ; les sous-agents reçoivent la même enveloppe, `child_tools`).
            from nokker.tools.ssh import REMOTE_TOOLS

            perms_gates = kernel.gates
            perms_gates.floors.append(local_api.ssh.floor)
            kernel.tools = AuditedTools(inner=kernel.tools, log=local_api.audit, scrub=kernel.invariants.scrub_output,
                                        describe=[local_api.ssh.describe], remote=frozenset(REMOTE_TOOLS))
            logger.info("lokkal : ops allumé (inventaire %s, journal %s)", local_api.ssh.hosts_path,
                        local_api.audit.path)
        if home is not None:  # maison : sa porte enveloppe les permissions (mode plan et règles deny gardent le refus)
            from nokker.home.source import install as install_home

            install_home(kernel, home, hub)
        if home_watch is not None:  # veille du foyer : sa porte enveloppe les permissions (refus intérieurs gardés)
            configure_home_watch(kernel, home_watch, hub)
        # Apprentissage (docs/LEARNING.md) : la carte obligatoire enveloppe les permissions et les planchers.
        configure_learning(config, kernel, events, local_api=local_api)
    cloud_crew = cloud_http._cloud.crew if cloud_http._cloud is not None and local_api is None else None
    if agents_hub is not None and cloud_crew is not None:
        # nokker.ai (X15) : `placement` auto / web / machines du compte, déménagement, deck ; le moteur tourne pour
        # envoyer les passages aux machines, et n'exécute ici (agents `web`) qu'avec NOKKER_CREW_ENGINE=1.
        from nokker.cloud.crew import CloudPlacement

        agents_hub.placement_policy = CloudPlacement(links=cloud_crew.links, web_allowed=cloud_crew.web_allowed)
        agents_hub.mover = cloud_crew
        cloud_http._cloud.agents = agents_hub
    crew_engine = None
    # Boîte relevée par le déclencheur `mail:` : celle de Lokkal pour le propriétaire local, celle du compte sinon.
    from nokker.local.connectors import Mailboxes

    local_mail = local_api.connectors if local_api is not None else None
    mailboxes = Mailboxes(local=local_mail, provider=provider) \
        if local_mail is not None or provider is not None else None
    if agents_hub is not None and (crew_engine_enabled(config) or cloud_crew is not None):
        crew_engine = start_crew_engine(config, agents_hub, kernel, hub=hub, approvals=approvals, tenancy=tenancy,
                                        local_api=local_api, remote=cloud_crew,
                                        local_runs=crew_engine_enabled(config),
                                        # Lokkal : lancé une fois le retour vers nokker.ai posé
                                        start=local_api is None, connectors=mailboxes)
        if cloud_crew is not None:
            cloud_crew.engine = crew_engine
        # `agent_draft` décrit les déclencheurs selon la présence du moteur (posé ici, après le noyau) : l'index se relit.
        refresh = getattr(kernel.tools, "refresh", None)
        if callable(refresh):
            refresh()
    if config.admin_enabled:
        start_admin(config)
    # Sans clé d'API configurée, le serveur n'est PLUS ouvert par défaut (bêta OVH : `api_keys: []`, seuls les jetons de
    # session des testeurs passent) ; le mode ouvert de dev se demande explicitement (`dev_open_api: true`).
    api_keys = config.api_key_map()
    if local_api is not None:  # le jeton local vaut identité « propriétaire local » sur toutes les routes
        api_keys = {**api_keys, local_api.token: LOCAL_OWNER}
    knock_hub = None
    if config.knock.get("enabled"):
        # Heurtoir (docs/KNOCK.md) : Lokkal seulement ; les déclencheurs viennent de home-bridge, jamais du LAN direct.
        from nokker.knock.bridge import configure_knock

        knock_hub = configure_knock(config, kernel, approvals, local=local_api is not None)
    handler = make_handler(kernel, api_keys, ledger, approvals=approvals, events=events, open_api=config.dev_open_api,
                           telegram_bot_token=config.telegram_bot_token() or None, telegram_ids=config.telegram_ids,
                           hub=hub, previews=register_previews_routes(config), agents=agents_hub,
                           known_users=config.known_users(),
                           session_thread_by_sender=config.session_thread_by_sender, tenancy=tenancy,
                           internal_tenant=config.internal_tenant, local_api=local_api,
                           shares=build_shares(config) if hub is not None else None,
                           approval_suggestions=config.approval_suggestions,
                           memory_search_api=config.memory_search_api,
                           improve_config=config if config.self_improve else None, knock_hub=knock_hub)
    if knock_hub is not None and getattr(knock_hub, "pump", None) is not None:
        knock_hub.pump.start()
    if hub is not None:
        _start_notify_sweeper(hub)
    if local_api is not None and config.local.get("mirror") and local_api.setup is not None \
            and local_api.setup.vault.get("machine_token"):
        # Miroir web ⇄ Lokkal (L5, abonnés) : nokker.ai refuse (403) si le compte n'y a pas droit.
        from nokker.local.connectors import keep_local_fn
        from nokker.local.mirror import MirrorWorker

        def run_event_of(thread_id: str) -> str | None:
            if agents_hub is None:
                return None
            run = agents_hub.store.run_by_thread(thread_id)  # dans le locataire interne (le fil du miroir)
            return run.event if run is not None else None

        setup = local_api.setup
        base_url = local_api.link.base_url if local_api.link else "https://app.nokker.ai"
        team_notes = None
        if team_rules is not None and local_api.project_memory is not None:
            # Notes de projet d'équipe poussées ET tirées (X17, §7.15) : `local.team` ET `local.mirror`.
            from nokker.local.team_notes import TeamNotesSync

            team_notes = TeamNotesSync(base_url=base_url, token=lambda: setup.vault.get("machine_token") or "",
                                       state=setup.state, save=setup._save, memory=local_api.project_memory,
                                       team=team_rules)
        local_api.mirror = MirrorWorker(
            store=kernel.store, base_url=base_url,
            token=lambda: setup.vault.get("machine_token") or "", state=setup.state,
            save=setup._save,  # le propriétaire local est dans le locataire interne
            notes=local_api.project_memory,  # notes `both` seulement (X21) ; None : pas de notes en miroir
            pages=local_api.pages,  # pages dont l'utilisateur a demandé la copie (§7.23) ; None : `local.pages` éteint
            team_notes=team_notes,
            # boîte mail locale et passages `mail:` : ces tours, et les notes qui en dérivent, restent sur la machine
            # (docs/LOKKAL.md §7.25)
            keep_local=keep_local_fn(local_api.connectors, run_event_of))
        if team_notes is not None:
            team_notes.stays = local_api.mirror.note_stays  # même règle pour les notes d'équipe
        local_api.mirror.start()
        logger.info("lokkal : miroir web ⇄ machine lancé")
    if local_api is not None:
        # Charge de la machine (X5) : envoyée à nokker.ai toutes les 60 s et à l'ouverture du tunnel, seulement si la
        # machine est liée (jeton de machine au coffre, relu à chaque envoi : un lien fait après le démarrage compte).
        from nokker.local.load import ActiveTurns, LoadReporter, measure, nvidia_gpus

        active = ActiveTurns()
        kernel.run_turn = active.wrap(kernel.run_turn)  # fils dont un tour est en cours (API, Telegram)
        setup = local_api.setup
        if setup is not None:
            kernel.run_turn = setup_guard(kernel, setup, web_url_of_lokkal(), kernel.run_turn,
                                         lambda: any(t.api_key() for t in config.tiers.values()),
                                         token=lambda: getattr(local_api, "token", None))

        def machine_token() -> str | None:
            try:
                return setup.vault.get("machine_token") if setup is not None else None
            except Exception:  # noqa: BLE001 — coffre sans clé : machine considérée non liée
                return None

        if local_api.inventory is not None:
            # Inventaire des ressources `local` (X21), par NOMS : connecteurs posés dans Lokkal, serveurs MCP déclarés.
            def local_connectors() -> list[str]:
                kinds = list(local_api.connectors.kinds()) if local_api.connectors is not None else []
                if connectors_http._connectors is not None:
                    kinds += [c["kind"] for c in connectors_http._connectors.cards(LOCAL_OWNER)
                              if c.get("state") not in (None, "absent")]
                return sorted(set(kinds))

            local_api.inventory.connectors = local_connectors
            local_api.inventory.mcp = lambda: [str(m.get("name") or "") for m in config.mcp_tool_servers]
        if agents_hub is not None and crew_engine is not None:
            # Agents du deck multi-machines (X15) : passages demandés par nokker.ai, déménagements, deck publié avec la
            # charge (noms et états ; titres des fils seulement s'ils sont en miroir).
            from nokker.local.crew import LocalCrew

            def title_of(thread_id: str) -> str | None:
                t = hub.threads.get(thread_id) if hub is not None else None
                return getattr(t, "title", None) if t is not None else None

            local_api.crew = LocalCrew(
                hub=agents_hub, base_url=local_api.link.base_url if local_api.link else "https://app.nokker.ai",
                token=machine_token, owner=LOCAL_OWNER, active=active.threads, sessions=local_api.code.sessions,
                approvals=approvals, title_of=title_of, mirrored=lambda: local_api.mirror is not None)
            crew_engine.on_guest = local_api.crew.report
        if crew_engine is not None:
            crew_engine.start()  # reprise après redémarrage : un passage invité interrompu est rendu à nokker.ai
        local_api.load = LoadReporter(
            base_url=local_api.link.base_url if local_api.link else "https://app.nokker.ai", token=machine_token,
            measure=lambda: measure(version=NOKKER_VERSION, sessions=len(local_api.code.sessions()),
                                    active_turns=active.count(), gpus=nvidia_gpus),
            resources=local_api.inventory.collect if local_api.inventory is not None else None,
            deck=local_api.crew.snapshot if local_api.crew is not None else None)
        local_api.load.start()
        if local_api.delegated is not None:
            # Tours des sessions déléguées par nokker.ai (X21) : leur réponse, et elle seule, repart vers le fil web
            # (y compris la suite d'un tour après une carte décidée sur la machine ou au téléphone).
            kernel.run_turn = local_api.delegated.wrap(kernel.run_turn)
    if local_api is not None and approvals is not None and local_api.setup is not None \
            and config.local.get("remote_approvals", True):
        # Approbations fortes depuis le téléphone (lot X10, docs/LOKKAL.md §7.3) : une carte forte part à nokker.ai
        # quand la machine est liée (jeton relu à chaque passage), la preuve revient et se vérifie ICI, puis la carte
        # est appliquée par le même chemin que la décision locale.
        from nokker.approvals.notify import ChainNotifier
        from nokker.local.remote_approvals import RemoteApprovals

        setup = local_api.setup
        local_api.remote_approvals = RemoteApprovals(
            base_url=local_api.link.base_url if local_api.link else "https://app.nokker.ai", token=machine_token,
            account=lambda: setup.state.get("account"), approvals=approvals, apply=local_api.apply_decision,
            scrub=kernel.invariants.scrub_output, decided_by=LOCAL_OWNER, state=setup.state, save=setup._save,
            # Décision produit (26.10) : une carte approuvée dans la Mini App Telegram est acceptée sur attestation de
            # nokker.ai ; `local.remote_approvals_attested: false` la refuse (seule une passkey, vérifiée ici, décide).
            attested=bool(config.local.get("remote_approvals_attested", True)))
        approvals.notifier = ChainNotifier((approvals.notifier, local_api.remote_approvals))
        if local_api.relayed is not None:  # X20b : la signature vérifiée d'une carte forte part avec l'appel relayé
            local_api.relayed.signed = local_api.remote_approvals.signed_proof
        local_api.remote_approvals.start()
    if local_api is not None and config.local.get("relay_url"):
        # Gardé pour lancer le tunnel à l'approbation du lien (LocalApi.link_approved), sans redémarrage.
        local_api.tunnel_args = (config.local["relay_url"], f"http://127.0.0.1:{config.port}",
                                 config.local.get("web_base"))
        if local_api.start_tunnel(config.local["relay_url"], f"http://127.0.0.1:{config.port}",
                                  config.local.get("web_base")):
            logger.info("lokkal : tunnel de la passerelle lancé vers %s", config.local["relay_url"])
    if getattr(kernel, "goals", None) is not None and kernel.goals.goals_enabled:
        # Objectifs menés par le noyau, actifs avant l'arrêt : un tour enchaîné chacun (docs/OUTCOMES-GOAL.md).
        n = kernel.goals.recover()
        if n:
            logger.info("objectifs : %d objectif(s) repris après redémarrage", n)
    httpd = ThreadingHTTPServer((config.host, config.port), handler)
    logger.info("nokker api: écoute sur %s:%d", config.host, config.port)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()


__all__ = [
    "build_agents", "build_approvals", "build_hub", "build_invariants", "build_kernel", "build_ledger",
    "build_notifier", "build_tenancy", "make_handler", "serve",
]
