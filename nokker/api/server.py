"""`/v1/chat/completions` OpenAI-compatible en SSE + `/healthz` + `/metrics` — PLAN-P1 §2.8.

Contrat consommé par la gateway Hermes en mode proxy (`gateway/run_turn.py` dans hermes-nokker) :
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
lignes `data: ` — la gateway Hermes ne voit aucune différence. Les jetons en direct sont réservés aux clients qui
posent `X-Nokker-Stream-Tokens: 1` (le web) : la gateway concatène TOUT `delta.content` reçu, elle ne doit donc
jamais recevoir de texte partiel ni de reset. Contrat : docs/CLIENT-API.md §Streaming.

**Sécurité de `Turn.sender`** (consigne Nick/F2, 17.10.2026) : il ne vient JAMAIS d'un champ que le
client contrôle (`user` du corps, un en-tête arbitraire) — seulement de la clé Bearer effectivement
présentée, résolue via `NokkerConfig.api_key_map()` (secret que seul le vrai appelant détient). La
gateway Hermes en mode proxy ne transmet AUCUNE identité vérifiable aujourd'hui (corps strictement
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
from nokker.agents.types import agent_to_json
from nokker.approvals import crypto as approvals_crypto
from nokker.approvals.base import BaseApprovals
from nokker.approvals.errors import (
    ApprovalError, ApprovalExpired, ApprovalNotFound, ApprovalNotPending, DeviceError, DeviceNotFound, ProofInvalid,
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
from nokker.config import NokkerConfig, load_config
from nokker.context.engine import ContextEngineV0
from nokker.context.engine_v1 import ContextEngineV1
from nokker.context.manager import ContextManager
from nokker.context.tools import RecallContextSource
from nokker.context.types import ContextBudgets
from nokker.core.loop import Kernel
from nokker.core.ports import Approval, Invariants, Proof
from nokker.core.turn import LLMCall, Tier, Turn, TurnInput, Usage
from nokker.ledger.costs import InMemoryLedger, LedgerBackend, PostgresLedger
from nokker.ledger.metrics import render as render_metrics
from nokker.memory.corthexis import CorthexisMemory
from nokker.providers.sokkan import SokkanProvider
from nokker.qa.calibrate import DEFAULT_THRESHOLDS, load_thresholds_file
from nokker.qa.escalate import EscalatorV0
from nokker.qa.gates import GatesV0, GatesV1
from nokker.qa.invariants import Allowlist, DeterministicInvariants, GoAuthorizedSenders
from nokker.qa.judge import HttpJudgeTransport, OpenAICompatJudge
from nokker.qa.secrets import KnownSecrets
from nokker.route.policy import PolicyV0
from nokker.store.pg import MemoryStore, PgStore, apply_migrations
from nokker import feedback as feedback_mod
from nokker.connectors import http as connectors_http
from nokker.cloud import http as cloud_http
from nokker.onboarding import http as onboarding_http
from nokker.telegram import http as telegram_http
from nokker.connectors.provider import ConnectorProvider as ConnectorsForTenant
from nokker.connectors.service import Connectors
from nokker.tenancy.connectors import ConnectorProvider
from nokker.tenancy.context import (
    INTERNAL_TENANT, current_tenant, current_tenant_or_none, scoped_thread_id, tenant_of_thread, tenant_scope,
)
from nokker.tenancy.resolve import UnknownSender, account_tenant_fn, is_account, resolve_tenant
from nokker.tenancy.limits import LimitedPolicy, TenantLimits
from nokker.tenancy.resources import NullMemory, TenantEscalator, TenantMemory, TenantRegistry
from nokker.tenancy.store import MemoryTenancy, PgTenancy, Tenancy
from nokker.threads.memory import InMemoryThreads
from nokker.threads.types import ThreadError, ThreadForbidden, ThreadNotFound
from nokker.tools.effects import EffectsTable, SecretScrubbingToolSource
from nokker.tools.mcp import McpToolSource
from nokker.tools.registry import Registry, builtin_core_source
from nokker.local.api import LOCAL_OWNER, LocalApi
from nokker.tools.web import WebToolSource

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
_RE_APPROVAL_DECISION = re.compile(r"^/v1/approvals/([^/]+)/decision$")
_RE_DEVICE_ONE = re.compile(r"^/v1/devices/([^/]+)$")
_RE_DEVICE_CONFIRM = re.compile(r"^/v1/devices/([^/]+)/confirm$")
_RE_DEVICE_SESSION_CHALLENGE = re.compile(r"^/v1/devices/([^/]+)/session-challenge$")
_RE_MINIAPP = re.compile(r"^/miniapp/([^/]+)$")
_RE_LOG_SECRET = re.compile(r"([?&](?:t|token|code|init_data|key)=)[^&\s\"]+")
_RE_THREAD_ONE = re.compile(r"^/v1/threads/([^/]+)$")
_RE_THREAD_CANCEL = re.compile(r"^/v1/threads/([^/]+)/cancel$")
# Outils qu'un client peut appeler hors tour (docs/LOKKAL.md §7.4.4) : points de retour, skills, lecture du plan. Jamais
# terminal, write_file, patch : ceux-là restent au modèle, sous ses portes.
DIRECT_TOOLS = frozenset({"checkpoint_list", "checkpoint_diff", "checkpoint_create", "checkpoint_restore", "skill_list",
                          "skill_view", "todo"})
NOKKER_VERSION = "0.1.0"
MIN_CLIENT_VERSION = "0.1.0"
_RE_THREAD_SUB = re.compile(r"^/v1/threads/([^/]+)/(messages|bubbles)$")
_RE_BUBBLE_ONE = re.compile(r"^/v1/bubbles/([^/]+)$")
_RE_BUBBLE_OP = re.compile(r"^/v1/bubbles/([^/]+)/(close|transfer|share)$")
_RE_BUBBLE_ACTION = re.compile(r"^/v1/bubbles/([^/]+)/actions/([^/]+)$")


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


def build_judge(config: NokkerConfig) -> OpenAICompatJudge:
    """B-2 : backend par défaut mesuré sur le banc B-1 (`nokker-judge-bench/README.md` § B-2
    mesures) — Ship sans raisonnement (`reasoning_effort: "none"`, SEULE variante qui coupe
    vraiment le raisonnement sur le Router SOKKAN, cf `providers/sokkan.py`), `top_logprobs=1`
    (plafond mesuré du Router)."""
    transport = HttpJudgeTransport(base_url=config.judge.base_url, model=config.judge.model,
                                   api_key=config.judge.api_key(), extra_body={"reasoning_effort": "none"},
                                   top_logprobs=1)
    return OpenAICompatJudge(transport=transport, model_name=config.judge.model)


def build_gates(config: NokkerConfig, invariants: Invariants) -> GatesV0 | GatesV1:
    if not config.judge_enabled:
        return GatesV0(invariants=invariants)  # juge < 90 % au banc B-1 : invariants seuls (cf config.py)
    thresholds = load_thresholds_file(config.judge_thresholds_path) if config.judge_thresholds_path \
        else dict(DEFAULT_THRESHOLDS)
    return GatesV1(invariants=invariants, judge=build_judge(config), thresholds=thresholds)


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

        mgr.semantic = CorthexisSemanticIndex(
            cx.get("root_dir") or "/var/lib/nokker/session-memory",
            database_url=os.environ.get(cx.get("database_url_env", "CORTHEXIS_DATABASE_URL"), ""),
            embed_urls=cx.get("embed_urls", ""), profile=cx.get("profile", "leger"), rerank_url=cx.get("rerank_url", ""),
            min_score=float(cx.get("min_score", DEFAULT_MIN_SCORE)),
            min_rerank=float(cx.get("min_rerank", DEFAULT_MIN_RERANK)), chunk_target=int(cx.get("chunk_target", 300)))
    if hub is not None:
        mgr.on_episode_closed = hub.create_episode_bubble
        hub.context_manager = mgr
    return mgr


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
        return None
    dsn = config.pg_dsn()
    if not dsn:
        return MemoryConnectorStore(box)
    from psycopg.rows import dict_row

    from nokker.store.conn import connect

    return PgConnectorStore(dsn, box, connect=lambda: connect(dsn, row_factory=dict_row))


def configure_cloud_link(config: NokkerConfig, tenancy: Tenancy, approvals: Any, ledger: Any,
                         store: Any = None) -> None:
    """nokker.ai, côté cloud du lien Lokkal (nokker/cloud/) : seulement si `cloud_link` est configuré ET la clé de
    signature présente ; sinon aucune route (la bêta d'avant ne change pas)."""
    cl = config.cloud_link or {}
    if not cl:
        cloud_http.configure(None)
        return
    import os

    from nokker.cloud.http import CloudLink
    from nokker.cloud.link import LinkService, Signer

    pem = os.environ.get(cl.get("signing_key_env") or "NOKKER_GATEWAY_SIGNING_KEY", "")
    if not pem:
        logger.warning("cloud_link configuré sans clé de signature : routes du lien Lokkal désactivées")
        cloud_http.configure(None)
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

    def record_usage(tenant: str, machine: str, usage: dict, seconds: float) -> None:
        if ledger is None:
            return
        call = LLMCall(tier=Tier.DEEP, model=deep.model, reasoning=False, prefix_sha256="", layer_tokens={},
                       usage=Usage(prompt=int(usage.get("prompt_tokens") or 0),
                                   completion=int(usage.get("completion_tokens") or 0)),
                       latency_s=seconds, route_reason=f"lokkal:{machine}")
        ledger.record(scoped_thread_id(tenant, f"lokkal-{machine}"), 0, call)

    dsn = config.pg_dsn()
    if dsn:
        from nokker.cloud.pg import PgLinkStore

        store: Any = PgLinkStore(dsn)
    else:
        from nokker.cloud.link import MemoryLinkStore

        store = MemoryLinkStore()
    cloud_http.configure(CloudLink(
        links=LinkService(base_url=cl.get("base_url") or "https://app.nokker.ai",
                          gateway_domain=cl.get("gateway_domain") or "nokker.app", store=store),
        signer=Signer(private_pem=pem.encode(), issuer=(cl.get("base_url") or "https://app.nokker.ai").rstrip("/")),
        identity=identity, tenant_of=tenant_of,
        router_base=deep.base_url, router_key=deep.api_key, record_usage=record_usage, store=store,
        # Miroir (L5, abonnés) : drapeau `mirror` + liste de comptes (« * » = tous) tant que l'abonnement n'est pas lu.
        mirror_allowed=(lambda acc: "*" in cl.get("mirror_accounts", []) or acc in cl.get("mirror_accounts", []))
        if cl.get("mirror") else None))


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
    adapter = TelegramAdapter(tenancy=tenancy, pairing=pairing, run_turn=run_turn, bot=bot,
                              feedback=feedback_mod.service())
    telegram_http.configure(adapter, pairing, secret, config.telegram_bot_username)
    if approvals is not None and config.app_url:
        from nokker.telegram.notify import TesterApprovalNotifier

        approvals.tester_notifier = TesterApprovalNotifier(tenancy=tenancy, bot=bot, app_url=config.app_url)


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
        if config.beta_pull_url and token:
            from nokker.onboarding.front import FrontInvitations

            front = FrontInvitations(config.beta_pull_url, token)  # invitations : mêmes routes internes que le tirage
        httpd = serve_admin(store, verifier, config.admin_host, config.admin_port, front)
        if config.beta_pull_url and token:
            bot = os.environ.get(config.admin_notify_token_env, "")
            notify = TelegramText(bot, config.admin_notify_ids()).send \
                if bot and config.admin_notify_ids() else None
            if notify is None:
                logger.warning("candidatures : pas de notification Telegram (%s ou admin_notify_chat_ids vide)",
                               config.admin_notify_token_env)
            BetaPuller(store=store, base_url=config.beta_pull_url, token=token, send=notify,
                       console_url=config.admin_console_url, interval_s=config.beta_pull_interval_s).start()
        elif config.beta_pull_url:
            logger.warning("tirage des candidatures : %s vide, tirage désactivé", config.beta_pull_token_env)
        return httpd
    except Exception:  # noqa: BLE001
        logger.exception("console opérateur : démarrage en échec (le noyau continue)")
        return None


def build_kernel(config: NokkerConfig, invariants: Invariants | None = None,
                 ledger: LedgerBackend | None = None, memory: object | None = None,
                 approvals: BaseApprovals | None = None, hub: BubbleHub | None = None,
                 connectors: ConnectorProvider | None = None, extra_sources: list[Any] | None = None) -> Kernel:
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
    de code d'une session Nokker Lokkal (`nokker code`, `nokker/tools/code.py`)."""
    dsn = config.pg_dsn()
    store = PgStore(dsn) if dsn else MemoryStore()
    if dsn:
        apply_migrations(dsn)

    if memory is None and config.memory_mcp_url:
        memory = CorthexisMemory(url=config.memory_mcp_url, token=config.memory_token())
        memory.warm_up()
    # `memory_mcp_url` vide (bêta OVH) : aucune connexion à sokkan-memory, ni rappel ni outils memory_*, pour personne.
    memory_sources = [memory] if memory is not None else []

    effects = EffectsTable.load(config.effect_overrides)
    mcp_sources = []
    for srv in config.mcp_tool_servers:
        token = os.environ.get(srv.get("token_env", ""), "") if srv.get("token_env") else ""
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        raw_source = McpToolSource(name=srv["name"], url=srv["url"], headers=headers,
                                   close_on_turn_end=srv.get("close_on_turn_end"))
        # Un serveur MCP ne déclare pas d'effet lui-même : la table (F3) l'annote, et sert aussi de
        # 2e ligne de défense (SECRET -> poignée) indépendante de redact_result du noyau.
        mcp_sources.append(SecretScrubbingToolSource(inner=raw_source, effects=effects))
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
    ctx_manager = build_context_manager(config, store, hub) if config.context_enabled else None
    context_sources = [RecallContextSource(ctx_manager)] if ctx_manager is not None else []
    core_source = builtin_core_source()
    internal_tools = Registry(sources=[core_source, *context_sources, *memory_sources, *mcp_sources, *web_sources,
                                       *bubble_sources, *(extra_sources or [])])
    # Pool de navigateurs de la bêta (docs/BROWSER-POOL.md) : un Chrome isolé par testeur, seulement si l'adresse du
    # gestionnaire est posée (secret du déploiement) ; l'interne garde brosim.
    browser_sources = []
    pool_url = os.environ.get(config.browser_pool_url_env or "", "")
    if pool_url:
        from nokker.tools.browser_pool import BrowserPoolSource

        token_env = config.browser_pool_token_env
        browser_sources.append(BrowserPoolSource(base_url=pool_url, api_token=lambda: os.environ.get(token_env, "")))
    # Testeur : JAMAIS mcp_sources (workspace, brosim de Nick) ni memory_sources (sokkan-memory).
    tools = TenantRegistry(internal=internal_tools, tester_sources=[core_source, *context_sources, *web_sources,
                                                                    *bubble_sources, *browser_sources],
                           connectors=connectors)

    inv = invariants or build_invariants(config)
    gates = build_gates(config, inv)
    escalator = TenantEscalator(EscalatorV0(addressee=config.escalation_addressee))  # jamais chez un testeur
    limits = TenantLimits.from_config(config.tenant_limits, dsn)
    policy = LimitedPolicy(inner=PolicyV0(), limits=limits)
    if ctx_manager is not None:
        ctx_manager.scrub = inv.scrub_output
        context: ContextEngineV0 = ContextEngineV1(tools=tools, prefix_path=config.prefix_path,
                                                   profile_path=config.profile_path, manager=ctx_manager,
                                                   product_prefix_path=config.product_prefix())
    else:
        context = ContextEngineV0(tools=tools, prefix_path=config.prefix_path, profile_path=config.profile_path,
                                  product_prefix_path=config.product_prefix())

    providers: dict[Tier, Any] = {}
    for name, tier_cfg in config.tiers.items():
        try:
            tier = Tier(name)
        except ValueError:
            logger.warning("config: palier inconnu ignoré: %s", name)
            continue
        extra = {"reasoning_off_body": tier_cfg.reasoning_off_body} if tier_cfg.reasoning_off_body is not None else {}
        main_provider = SokkanProvider(base_url=tier_cfg.base_url, api_key=tier_cfg.api_key(),
                                       model=tier_cfg.model, **extra)
        if tier_cfg.fallback_model:
            from nokker.providers.hedged import HedgedProvider

            spare = SokkanProvider(base_url=tier_cfg.base_url, api_key=tier_cfg.api_key(),
                                   model=tier_cfg.fallback_model, **extra)
            providers[tier] = HedgedProvider(main_provider, spare, hedge_after_s=tier_cfg.hedge_after_s)
        else:
            providers[tier] = main_provider

    ledger = ledger if ledger is not None else build_ledger(config)
    # P1 (18.10.2026) : câblé par défaut (`approvals_enabled=True`, doctrine Nick — jamais d'action à
    # effet exécutée sans preuve). `approvals=` explicite (banc, test) prime toujours sur la config.
    appr = approvals if approvals is not None else (build_approvals(config) if config.approvals_enabled else None)
    tenant_memory = TenantMemory(memory if memory is not None else NullMemory())
    kernel = Kernel(store=store, context=context, memory=tenant_memory, invariants=inv, gates=gates,
                    escalator=escalator, policy=policy, providers=providers, tools=tools, ledger=ledger,
                    approvals=appr, tool_loading=tool_loading(config), observer=hub, limits=limits)
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
    return {
        "id": a.id, "thread_id": a.thread_id, "turn_index": a.turn_index, "channel": a.channel,
        "tool_name": a.tool_name, "arguments": a.arguments, "args_sha256": a.args_sha256, "effect": effect,
        "rule": a.rule, "reason": a.reason, "advice": a.advice, "strength": a.strength, "status": a.status,
        "created_at": _iso(a.created_at), "expires_at": _iso(a.expires_at), "decided_by": a.decided_by,
        "decided_via": a.decided_via, "decided_at": _iso(a.decided_at), "result_summary": a.result_summary,
        "error": a.error,
    }


def device_to_json(d: Device) -> dict[str, Any]:
    """Forme CANONIQUE de `POST`/`GET /v1/devices` (docs/APPROVALS.md §5) — JAMAIS `public_key_spki`
    (pas un secret, mais inutile au client une fois l'enrôlement fait ; conforme au principe « jamais
    une valeur de plus que nécessaire dans une réponse »)."""
    return {"id": d.id, "code": d.code, "name": d.name, "user": d.user, "status": d.status,
            "created_at": _iso(d.created_at), "approved_at": _iso(d.approved_at), "approved_via": d.approved_via}


def _display_name(user_id: str) -> str:
    """`nick` -> `Nick`, `nick_s` -> `Nick S` (docs/BUBBLES.md §9.2) — mise en forme SEULEMENT (pas
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


def make_handler(kernel: Kernel, api_keys: dict[str, str], ledger: LedgerBackend | None = None, *,
                 approvals: BaseApprovals | None = None, events: Any | None = None,
                 telegram_bot_token: str | None = None, telegram_ids: dict[str, str] | None = None,
                 miniapp_dir: Path | None = None, hub: BubbleHub | None = None,
                 previews: PreviewsRoutes | None = None, agents: AgentsHub | None = None,
                 known_users: frozenset[str] = frozenset(),
                 session_thread_by_sender: dict[str, str] | None = None,
                 tenancy: Tenancy | None = None, open_api: bool = True,
                 internal_tenant: bool = True, local_api: Any | None = None) -> type[BaseHTTPRequestHandler]:
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
    `known_users` (docs/BUBBLES.md §9.2) : identités servies par `GET /v1/users`, indépendamment de
    `hub`/`agents` (liste de partage).

    **Multi-locataire** (docs/TENANCY.md §2-§3) : chaque requête authentifiée résout son expéditeur en locataire
    (`nokker.tenancy.resolve.resolve_tenant` : `us_…` par `tenancy`, interne par la config, sinon 403) et s'exécute
    dans `tenant_scope` — y compris le fil de travail du streaming. Les fils d'un testeur sont préfixés
    (`scoped_thread_id`) ; un objet d'un autre locataire répond 404. `tenancy` absent : `MemoryTenancy()` vide (aucun
    compte de testeur). Le résolveur de comptes est posé ici sur les invariants (GO), les approbations (décideur)
    et le hub (membres) s'ils n'en ont pas déjà un."""
    telegram_ids = telegram_ids or {}
    tenancy = tenancy if tenancy is not None else MemoryTenancy()
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
    if executor is not None and hub is not None:
        executor.on_direct = lambda a, content, is_error: hub.direct_result(a.thread_id, a.id, a.tool_name, content,
                                                                            is_error, owner=a.decided_by)

    def cancel_thread(thread_id: str) -> int:
        """Arrête le tour en cours du fil et retire ses cartes en attente (une carte approuvée après l'annulation
        n'exécute plus rien). Rend le nombre de cartes retirées."""
        kernel.cancel(thread_id)
        n = 0
        if approvals is not None:
            for a in approvals.pending():
                if a.thread_id == thread_id or a.thread_id.startswith(thread_id + "/sa-"):
                    approvals.mark_failed(a.id, "annulé par l'utilisateur")
                    n += 1
        return n

    class Handler(BaseHTTPRequestHandler):
        server_version = "nokker/0"

        def log_message(self, fmt: str, *args: Any) -> None:
            # Jetons d'URL masqués (invitation `?t=`, vu dans les journaux OVH le 21.10) : un journal n'est pas un coffre.
            # Derrière le tunnel Cloudflare, le pair est cloudflared : l'IP du visiteur est dans CF-Connecting-IP (posée
            # par Cloudflare ; sur OVH aucun autre chemin n'atteint le pod). Les deux sont journalisées.
            peer = self.client_address[0]
            visitor = (getattr(self, "headers", None) or {}).get("CF-Connecting-IP")
            who = f"{visitor} (via {peer})" if visitor else peer
            logger.info("%s - %s", who, _RE_LOG_SECRET.sub(r"\1***", fmt % args))

        def parse_request(self) -> bool:
            if not super().parse_request():
                return False
            if local_api is not None and not local_api.host_ok(self.headers.get("Host")):
                self._json(403, {"error": {"message": "host refusé"}})
                return False
            if local_api is not None and self.headers.get("X-Nokker-Relay") \
                    and local_api.is_owner(self.headers.get("Authorization")):
                # Le jeton local ne sort jamais de la machine : présenté par la passerelle, il est refusé (§7.5).
                self._json(403, {"error": {"message": "jeton local refusé par la passerelle"}})
                return False
            return True

        def _local_route(self, method: str, path: str) -> bool:
            """Routes `/v1/local/*` (Nokker Lokkal) ; True si la requête a reçu sa réponse."""
            m = _RE_THREAD_CANCEL.match(path) if local_api is not None and method == "POST" else None
            if m:
                if not self._local_owner():
                    self._json(401, {"error": {"message": "jeton local requis"}})
                    return True
                self._json(202, {"cancelled": m.group(1), "approvals_cancelled": cancel_thread(m.group(1))})
                return True
            if local_api is None or not path.startswith("/v1/local/"):
                return False
            if path.startswith("/v1/local/approvals") and self._local_owner():
                self._local_approvals(method, path)
                return True
            if path == "/v1/local/tools/call" and method == "POST":
                self._local_tool_call()
                return True
            body = self._read_json_body() if method in ("POST", "PUT") else {}
            out = local_api.handle(method, path, self.headers.get("Authorization"), body,
                                   relayed=bool(self.headers.get("X-Nokker-Relay")))
            self._json(*(out or (404, {"error": {"message": "not found"}})))
            return True

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
            if name not in DIRECT_TOOLS:
                self._json(403, {"error": {"message": f"outil non appelable directement : {name}"}})
                return
            if not isinstance(arguments, dict) or (name == "todo" and arguments):
                self._json(400, {"error": {"message": "arguments invalides (todo : lecture seule, sans argument)"}})
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
                self._json(200, {"content": out.content, "is_error": out.is_error})

        def _local_owner(self) -> bool:
            """Le propriétaire de la machine : jeton local (sur la machine), ou jeton signé par nokker.ai arrivé par la
            passerelle (§7.5)."""
            auth = self.headers.get("Authorization")
            if self.headers.get("X-Nokker-Relay"):
                return local_api.relayed_owner(auth) is not None
            return local_api.is_owner(auth)

        def _local_approvals(self, method: str, path: str) -> None:
            """Cartes des sessions locales, décidées avec le jeton local (docs/LOKKAL.md §7.4.3)."""
            from nokker.local.approvals import append_user_rule, suggested_rules
            from nokker.qa.permissions import Rule

            if approvals is None:
                self._json(404, {"error": {"message": "approbations désactivées"}})
                return
            if path == "/v1/local/approvals" and method == "GET":
                self._json(200, {"approvals": [
                    {**approval_to_json(a), "suggested_rules": suggested_rules(a.tool_name, a.arguments, a.strength)}
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
            if card.strength == "strong" and self.headers.get("X-Nokker-Relay"):
                # À distance, une carte forte se signe avec la clé d'appareil (app.nokker.ai, téléphone), §7.3.
                self._json(403, {"error": {"message": "carte forte : décide-la sur app.nokker.ai ou ton téléphone"}})
                return
            allowed = suggested_rules(card.tool_name, card.arguments, card.strength)
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
            if decided.status == "approved" and executor is not None:
                try:
                    executor.run(decided)
                except Exception:  # noqa: BLE001 — l'échec est sur la carte (mark_failed), la réponse part
                    logger.exception("ApprovalExecutor.run a levé pour l'approbation %s", decided.id)
            final = approvals.get(decided.id) or decided
            self._json(200, {**approval_to_json(final), "scope": scope, "rule": rule if scope != "once" else None})

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
            if sender is None and token and local_api is not None and self.headers.get("X-Nokker-Relay") \
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
            if self._local_route("GET", path):
                return
            if cloud_http.handle(self, "GET", path):  # nokker.ai : lien Lokkal, passerelle, inférence relayée
                return
            if path == "/healthz":
                # version du noyau et plus vieux client accepté : le client terminal (Lokkal) s'y compare
                self._json(200, {"status": "ok", "version": NOKKER_VERSION, "min_client_version": MIN_CLIENT_VERSION})
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

            # Onboarding (nokker/onboarding/http.py) : routes publiques, sans session (le testeur n'a pas encore de compte).
            if onboarding_http.handle(self, "GET", self.path):  # chemin COMPLET : ?t= et ?lang= y sont
                return
            if path.startswith("/v1/connectors") or path in ("/v1/account", "/v1/account/usage"):
                self._account_route("GET", path)
                return

            # /v1/previews* (nokker/api/previews_routes.py, docs/BUBBLES.md §5) : point d'accroche
            # unique, hors du reste de ce dispatcheur — voir l'en-tête de previews_routes.py.
            if previews is not None and previews.handle_get(self, self.path):
                return

            if hub is not None and (path.startswith(("/v1/threads", "/v1/bubbles")) or path == "/v1/notifications"
                                    or (path == "/v1/events" and approvals is None)):
                self._bubbles_route("GET", path, {})
                return

            # /v1/agents, /v1/users (docs/BUBBLES.md §9.2/§9.4) : INDÉPENDANTS de `hub`/`approvals` —
            # Crew et la liste de partage n'ont besoin que d'une identité résolue.
            if agents is not None and path == "/v1/agents":
                sender = self._resolve_identity()
                if sender is None:
                    self._json(401, {"error": {"message": "unauthorized"}})
                    return
                tid = self._tenant_for(sender)
                if tid is None:
                    return
                with tenant_scope(tid):
                    self._json(200, [agent_to_json(a) for a in agents.list_agents() if agent_tenant(a) == tid])
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
                self._json(200, [approval_to_json(a) for a in approvals.pending()])
                return

            m = _RE_APPROVAL_ONE.match(path)
            if m:
                approval = approvals.get(m.group(1))
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
            except (ThreadError, BubbleError) as e:
                self._bubbles_error(e)

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
            self._json(404, {"error": {"message": "not found"}})

        def do_PUT(self) -> None:
            path = urlparse(self.path).path
            if self._local_route("PUT", path):
                return
            if path.startswith("/v1/connectors") or path == "/v1/account/consent":
                self._account_route("PUT", path)
                return
            self._json(404, {"error": {"message": "not found"}})

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
                if account is None:
                    self._json(403, {"error": {"message": "account_required"}})
                elif path == "/v1/telegram/pairing" and method == "POST":
                    telegram_http.pairing(self, account.account_id)
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
                        self._json(200, kernel.limits.usage(account.account_id))
                elif onboarding_http.account_route(self, method, path, account):
                    pass
                elif not connectors_http.handle(self, method, path, account):
                    self._json(404, {"error": {"message": "not found"}})

        def do_PATCH(self) -> None:
            path = urlparse(self.path).path
            if self._local_route("PATCH", path):
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
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Transfer-Encoding", "chunked")
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
            if cloud_http.handle(self, "POST", path):  # nokker.ai : lien Lokkal, passerelle, inférence relayée
                return
            if path != "/v1/chat/completions":
                if onboarding_http.handle(self, "POST", self.path):  # chemin COMPLET : ?t= et ?lang= y sont
                    return
                if path == "/v1/telegram/webhook":
                    telegram_http.webhook(self)
                    return
                if path == "/v1/telegram/pairing" or path == "/v1/feedback" or path.startswith("/v1/connectors"):
                    self._account_route("POST", path)
                    return
                if hub is not None and path.startswith(("/v1/threads", "/v1/bubbles")):
                    self._bubbles_route("POST", path, self._read_json_body())
                    return
                if agents is not None and path == "/v1/agents":
                    self._handle_agents_post()
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

        def _chat_completions(self, sender: str | None, tenant: str) -> None:
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                self._json(400, {"error": {"message": "invalid JSON"}})
                return

            # `X-Session-Id` (docs/CLIENT-API.md, docs/BUBBLES.md §1) ou `X-Hermes-Session-Id` (gateway Hermes),
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
            for m in reversed(messages):
                if m.get("role") == "user":
                    user_text = m.get("content") or ""
                    break
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
            inp = TurnInput(thread_id=thread_id, user_text=user_text, channel="api", sender=sender)
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
            self._json(200, {
                "id": completion_id, "object": "chat.completion", "created": created, "model": model_name,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": turn.reply},
                             "finish_reason": "stop"}],
                "usage": _usage_dict(turn),
            })

        def _run_turn(self, inp: TurnInput, sink: Any) -> Turn:
            if hub is not None:
                with hub.thread_lock(inp.thread_id):
                    return kernel.run_turn(inp, sink) if sink is not None else kernel.run_turn(inp)
            return kernel.run_turn(inp, sink) if sink is not None else kernel.run_turn(inp)

        def _stream_turn(self, inp: TurnInput, tokens: bool) -> None:
            """`stream: true` (P1, 19.10.2026) : en-têtes SSE TOUT DE SUITE, tour dans un fil de travail,
            `: ping` toutes les `STREAM_PING_S` tant que rien d'autre ne part (Cloudflare coupe à 100 s sans
            octet). Sans `tokens` (gateway Hermes, tout client OpenAI) : à la fin, EXACTEMENT les trois chunks
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

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("X-Accel-Buffering", "no")  # un nginx devant le noyau ne doit pas retenir le flux
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
                    # En-têtes déjà partis : l'erreur part dans le flux, sans `[DONE]` (la gateway Hermes
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

        def _handle_agents_post(self) -> None:
            """`POST /v1/agents` (docs/BUBBLES.md §9.4) — Crew minimal : crée l'agent directement
            (status `armed`, pas d'exécution réelle), publie `agent.created`. Indépendant de `hub`."""
            assert agents is not None
            body = self._read_json_body()
            sender = self._resolve_identity(body)
            if sender is None:
                self._json(401, {"error": {"message": "unauthorized"}})
                return
            tid = self._tenant_for(sender)
            if tid is None:
                return
            with tenant_scope(tid):
                self._create_agent(body, sender)

        def _create_agent(self, body: dict[str, Any], sender: str) -> None:
            assert agents is not None
            name = str(body.get("name", "")).strip()
            role = str(body.get("role", "")).strip()
            if not name or not role:
                self._json(400, {"error": {"message": "name/role requis"}})
                return
            tools = body.get("tools")
            if tools is not None and not isinstance(tools, list):
                self._json(400, {"error": {"message": "tools doit être une liste"}})
                return
            agent = agents.create_agent(name=name, role=role, model=str(body.get("model", "")).strip(),
                                        tools=[str(t) for t in (tools or [])], created_by=sender)
            self._json(201, agent_to_json(agent))

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
                    # tout nom, pour ne pas révéler lesquels existent (trouvé par Nick sur app.nokker.ai le 21.10).
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
                    self._json(_approvals_error_status(e), {"error": {"message": str(e)}})
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
                    self._json(_approvals_error_status(e), {"error": {"message": str(e)}})
                    return
                if executor is not None:
                    try:
                        executor.run(decided)
                    except Exception:  # noqa: BLE001 — la décision est déjà actée ; l'exécution est best-effort ici
                        logger.exception("ApprovalExecutor.run a levé pour l'approbation %s", decided.id)
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
            if path.startswith("/v1/connectors"):
                self._account_route("DELETE", path)
                return
            if hub is not None and _RE_THREAD_ONE.match(path):
                self._bubbles_route("DELETE", path, {})
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


def build_tenancy(config: NokkerConfig) -> Tenancy:
    """Comptes et locataires (docs/TENANCY.md) : Postgres si un DSN est configuré, mémoire sinon (dev, tests)."""
    dsn = config.pg_dsn()
    return PgTenancy(dsn) if dsn else MemoryTenancy()


def serve(config: NokkerConfig | None = None, kernel: Kernel | None = None,
          ledger: LedgerBackend | None = None) -> None:
    config = config or load_config()
    check_rls_role(config)
    tenancy = build_tenancy(config)
    if not config.api_key_map() and config.dev_open_api:
        logger.warning("dev_open_api : serveur OUVERT, toute requête sans identité tombe dans %s", INTERNAL_TENANT)
    ledger = ledger if ledger is not None else build_ledger(config)
    # Nokker Lokkal (docs/LOKKAL.md §7) : jeton local, Host autorisés, sessions de code. Absent = bêta, rien de tout ça.
    local_api = LocalApi.from_config(config.local, config.port) if config.local else None
    if local_api is not None:  # sessions de code : leur préfixe (le dossier, le bac à sable, les tests)
        import dataclasses

        code_prefix = config.local.get("prefix_path") or str(Path(__file__).parent.parent / "prompts"
                                                             / "prefix-lokkal-code.md")
        config = dataclasses.replace(config, prefix_path=code_prefix, profile_path=None)
    events = EventBus() if (config.approvals_enabled or config.bubbles_enabled or config.agents_enabled) else None
    hub = build_hub(config, events) if config.bubbles_enabled else None
    # Crew minimal (docs/BUBBLES.md §9.4) : indépendant de `hub`/`approvals` — posé sur `hub.agents_hub`
    # ci-dessous pour que l'activation d'un `agent_draft` (côté `hub`) y accède.
    agents_hub = build_agents(config, events) if config.agents_enabled else None
    # UNE SEULE instance du backend `Approvals`, partagée par le `Kernel` (qui pose les cartes sur
    # `Decision.HITL`) ET par les routes HTTP (qui les servent/décident) — sinon les deux verraient des
    # approbations différentes. `kernel=` explicite (appelant qui construit son propre noyau) : on lit
    # alors `kernel.approvals` au lieu d'en reconstruire un second.
    if kernel is not None:
        approvals = kernel.approvals
        hub = getattr(kernel, "observer", None) if isinstance(getattr(kernel, "observer", None), BubbleHub) else None
    else:
        approvals = build_approvals(config, on_event=events.publish if events is not None else None) \
            if config.approvals_enabled else None
        connector_store = build_connector_store(config)
        provider = ConnectorsForTenant(connector_store, tenancy.account_for_tenant) if connector_store else None
        delegate = None
        extra = None
        if local_api is not None:
            # Sous-agents (L3, couloir features) : l'enfant est une copie du noyau parent (mêmes portes, approbations,
            # fournisseurs, max_tokens, max_calls) autour du registre filtré par `delegate`.
            import dataclasses as _dc

            from nokker.tools.delegate import DelegateToolSource

            parent: dict[str, Kernel] = {}
            delegate = DelegateToolSource(
                lambda registry, thread_id, observer: _dc.replace(parent["k"], tools=registry, observer=observer),
                local_api.code, emit=events.publish if events is not None else None, parent_observer=hub)
            extra = [*local_api.tool_sources(), delegate]
        kernel = build_kernel(config, ledger=ledger, approvals=approvals, hub=hub, connectors=provider,
                              extra_sources=extra)
        if delegate is not None:
            parent["k"] = kernel
            delegate.bind(getattr(kernel.tools, "internal", kernel.tools))
        connectors_http.configure(Connectors(connector_store) if connector_store else None)
        configure_onboarding(config, tenancy, approvals)
        configure_cloud_link(config, tenancy, approvals, ledger, store=kernel.store)
        if cloud_http._cloud is not None:  # skill « sessions Lokkal » (§6.4) pour les comptes : outil DESTRUCTIVE
            from nokker.cloud.lokkal_session import LokkalSessionTools

            kernel.tools.tester_sources.append(LokkalSessionTools(links=cloud_http._cloud.links,
                                                                  signer=cloud_http._cloud.signer))
            kernel.tools.refresh()
        configure_feedback(config, kernel)
        configure_telegram(config, tenancy, kernel, hub, approvals)
    if hub is not None:
        hub.agents_hub = agents_hub
    if local_api is not None:
        from nokker.qa.permissions import LocalPermissions, Mode, PermissionGates

        from nokker.tools.code import reap_sandboxes

        if local_api.code.backend == "docker" and (n := reap_sandboxes(local_api.code.instance or "")):
            logger.info("lokkal : %d bac(s) à sable orphelin(s) supprimé(s) au démarrage", n)
        kernel.max_calls = int(config.local.get("max_calls", 60))  # une tâche de code fait des dizaines d'appels
        kernel.max_tokens = int(config.local.get("max_tokens", 16384))
        from nokker.qa.escalate import LocalEscalator

        kernel.escalator = LocalEscalator()  # en local, personne d'autre que le propriétaire

        def use_default_model(base_url: str, api_key: str, model: str) -> None:
            # Le modèle choisi à l'onboarding sert les deux paliers, en direct (pas de redémarrage).
            provider = SokkanProvider(base_url=base_url, api_key=api_key, model=model)
            kernel.providers[Tier.SHIP] = provider
            kernel.providers[Tier.DEEP] = provider

        local_api.on_default_model = use_default_model
        try:
            chosen = local_api.setup.provider_for_default() if local_api.setup is not None else None
        except Exception:  # noqa: BLE001 — coffre sans clé : les paliers de la config restent
            logger.warning("lokkal : modèle par défaut illisible au démarrage, paliers de la config gardés", exc_info=True)
            chosen = None
        if chosen is not None:
            use_default_model(*chosen)
        if config.local.get("reasoning", True):  # banc L0 (L01, 23.10) : sans raisonnement, le 397B pense à voix haute
            getattr(kernel.policy, "inner", kernel.policy).ship_reasoning = True
        kernel.gates = PermissionGates(inner=kernel.gates, permissions=LocalPermissions.load(
            # accept_edits par défaut : chaque écriture du dossier est photographiée (points de retour), donc
            # réversible ; les commandes et le destructif restent demandés.
            mode=config.local.get("mode"), fallback=Mode.ACCEPT_EDITS,
            user_settings=Path(config.local.get("settings_path") or "~/.config/nokker/settings.json").expanduser()))
        perms = kernel.gates.permissions
        local_api.on_session_open = lambda thread_id, cwd: perms.restrict_for_thread(
            thread_id, Path(cwd) / ".nokker" / "settings.json")  # le dépôt restreint son fil, comme `nokker code`
    if config.admin_enabled:
        start_admin(config)
    # Sans clé d'API configurée, le serveur n'est PLUS ouvert par défaut (bêta OVH : `api_keys: []`, seuls les jetons de
    # session des testeurs passent) ; le mode ouvert de dev se demande explicitement (`dev_open_api: true`).
    api_keys = config.api_key_map()
    if local_api is not None:  # le jeton local vaut identité « propriétaire local » sur toutes les routes
        api_keys = {**api_keys, local_api.token: LOCAL_OWNER}
    handler = make_handler(kernel, api_keys, ledger, approvals=approvals, events=events, open_api=config.dev_open_api,
                           telegram_bot_token=config.telegram_bot_token() or None, telegram_ids=config.telegram_ids,
                           hub=hub, previews=register_previews_routes(config), agents=agents_hub,
                           known_users=config.known_users(),
                           session_thread_by_sender=config.session_thread_by_sender, tenancy=tenancy,
                           internal_tenant=config.internal_tenant, local_api=local_api)
    if hub is not None:
        _start_notify_sweeper(hub)
    if local_api is not None and config.local.get("mirror") and local_api.setup is not None \
            and local_api.setup.vault.get("machine_token"):
        # Miroir web ⇄ Lokkal (L5, abonnés) : nokker.ai refuse (403) si le compte n'y a pas droit.
        from nokker.local.mirror import MirrorWorker

        setup = local_api.setup
        local_api.mirror = MirrorWorker(
            store=kernel.store, base_url=local_api.link.base_url if local_api.link else "https://app.nokker.ai",
            token=lambda: setup.vault.get("machine_token") or "", state=setup.state,
            save=setup._save)  # le propriétaire local est dans le locataire interne
        local_api.mirror.start()
        logger.info("lokkal : miroir web ⇄ machine lancé")
    if local_api is not None and config.local.get("relay_url"):
        if local_api.start_tunnel(config.local["relay_url"], f"http://127.0.0.1:{config.port}",
                                  config.local.get("web_base")):
            logger.info("lokkal : tunnel de la passerelle lancé vers %s", config.local["relay_url"])
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
