"""Config du noyau (YAML + env) — PLAN-P1 §2.8 : « aucun secret dans le fichier : secrets par variables
d'env. » Le YAML porte la forme (URLs, modèles, chemins) ; toute valeur qui ressemble à un secret est lue
depuis l'environnement, jamais depuis le fichier.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = os.environ.get("NOKKER_CONFIG", "/etc/nokker/config.yaml")
# Persona produit d'un testeur de la bêta (docs/TENANCY.md §5), packagée avec le noyau.
PRODUCT_PREFIX_PATH = Path(__file__).parent / "prompts" / "prefix-nokker.md"


@dataclass
class TierConfig:
    base_url: str
    model: str
    api_key_env: str = "ROUTER_KEY"
    # Corps ajouté pour couper le raisonnement sur ce palier. None = défaut du provider ({"reasoning_effort": "none"},
    # seule variante acceptée par le Router SOKKAN). vLLM/llama.cpp avec Qwen : {"chat_template_kwargs": {"enable_thinking": false}}.
    reasoning_off_body: dict[str, Any] | None = None
    # Repli sur lenteur (providers/hedged.py) : si `fallback_model` (même base_url/clé) n'a rien rendu au bout de
    # `hedge_after_s`, ce second modèle répond à la place. Vide = pas de repli.
    fallback_model: str = ""
    hedge_after_s: float = 8.0
    # Cache de préfixe de l'amont : « none » (OVH au token, chaque token payé à chaque appel → schémas d'outils chargés
    # selon le message) ou « prefix » (nos GPU, vLLM/llama.cpp : tout le catalogue, stable donc payé une fois). Celui du
    # palier Ship décide de `Kernel.tool_loading` (tools/select.py).
    cache: str = "none"
    # Le modèle lit-il les images jointes (X7, docs/LOKKAL.md §7.6) ? Sans vision, l'image n'est pas envoyée : le modèle
    # reçoit une note texte et l'utilisateur l'événement `attachment.skipped`. Mesuré avant de passer à `true`.
    vision: bool = False

    def api_key(self) -> str:
        return os.environ.get(self.api_key_env, "")


@dataclass
class ApiKeyIdentity:
    """Une clé Bearer acceptée par `/v1/chat/completions`, et l'identité qui lui est liée.

    Sécurité (consigne produit F2, 17.10.2026) : `Turn.sender` ne doit JAMAIS venir d'un champ que le
    CLIENT contrôle (`user` du corps, un en-tête arbitraire) — ça reviendrait à laisser l'appelant
    s'auto-attribuer un GO. La seule identité qu'`api/server.py` fait confiance est celle liée, ICI EN
    CONFIG SERVEUR, à la clé Bearer effectivement présentée (secret que seul le vrai appelant détient)."""

    token_env: str
    sender: str

    def token(self) -> str:
        return os.environ.get(self.token_env, "")


@dataclass
class NokkerConfig:
    pg_dsn_env: str = "NOKKER_PG_DSN"
    memory_mcp_url: str = ""  # vide = pas de mémoire longue (l'instance interne pose son URL dans sa config)
    memory_mcp_token_env: str = "SOKKAN_MEMORY_MCP_TOKEN"
    # Mémoire longue (`nokker/memory/corthexis.py::from_config`) : `mcp` (défaut : `memory_mcp_url`, vide = aucune),
    # `direct` (bibliothèque CortHeXis dans le processus, sur le Postgres et l'embed du compose — Nokker Lokkal) ou
    # `off`. Vide : `NOKKER_MEMORY_MODE` de l'environnement, sinon `mcp`.
    memory_mode: str = ""
    # Mode `direct` : `projects` (scope du rappel ; défaut `[u-local]`, les notes de portée user de la mémoire de projet),
    # `database_url_env` (CORTHEXIS_DATABASE_URL), `embed_urls`, `profile` (leger|standard|gpu), `rerank_url`, `deep`,
    # `min_score` (0,15), `relative_floor` (0,6), `min_rerank` (0,35) — seuils de l'index local au profil leger.
    memory_direct: dict[str, Any] = field(default_factory=dict)
    prefix_path: str = str(PRODUCT_PREFIX_PATH)  # l'instance interne pose son propre préfixe dans sa config
    escalation_addressee: str = "l'équipe"  # « Je transmets à … » de l'instance interne (jamais chez un testeur)
    profile_path: str | None = None
    # Multi-locataire (docs/TENANCY.md §5). `memory_mcp_url` vide = aucune mémoire longue, pour personne (bêta OVH :
    # aucune connexion à sokkan-memory, pas d'outils memory_*). `prefix_path`/`profile_path` ci-dessus = locataire interne
    # seulement ; un testeur reçoit `product_prefix_path` (None = le préfixe packagé `nokker/prompts/prefix-nokker.md`),
    # sans profil. `tenant_limits` : quotas par compte des testeurs, l'interne n'en a pas (nokker/tenancy/limits.py ; clés
    # `mode` soft|hard, `turns_per_day`, `turns_per_week`, `hard_turns_per_day`, `deep_per_day` ; absentes = offre Solo).
    product_prefix_path: str | None = None
    tenant_limits: dict[str, Any] = field(default_factory=dict)
    # Clés Bearer acceptées par /v1/chat/completions + identité liée (voir ApiKeyIdentity). Vide =
    # serveur ouvert (dev seulement) : alors sender=None TOUJOURS (aucune identité de confiance).
    api_keys: list[ApiKeyIdentity] = field(default_factory=list)
    # Identifiants Telegram numériques connus de l'instance interne — PAS utilisés pour résoudre Turn.sender
    # aujourd'hui : vérifié dans gateway/run_turn.py (hermes-nokker), le mode proxy de la passerelle amont
    # NE TRANSMET AUCUNE identité (ni en-tête, ni champ `user` ; corps strictement {model, messages,
    # stream}). Posés ici pour que F7 (pont gateway) puisse les relier à un futur en-tête VÉRIFIÉ côté
    # gateway (elle connaît déjà TELEGRAM_ALLOWED_USERS avant d'appeler le proxy) ; `qa/invariants.py`
    # (`GoAuthorizedSenders`) les reconnaîtra alors puisqu'ils sont fusionnés dans `extra_go_senders`.
    telegram_ids: dict[str, str] = field(default_factory=dict)
    # Expéditeurs autorisés à donner un GO (GoAuthorizedSenders), plus les telegram_ids (voir ci-dessus : inertes tant
    # que rien ne pose Turn.sender à cette valeur). Vide par défaut : personne ; chaque instance pose les siens.
    extra_go_senders: list[str] = field(default_factory=list)
    known_secrets_env_prefix: str | None = "NOKKER_SECRET_"  # KnownSecrets.load(env_prefix=...)
    known_secrets_file: str | None = None
    allowlist_domains: list[str] = field(default_factory=list)
    allowlist_addresses: list[str] = field(default_factory=list)
    effect_overrides: dict[str, str] = field(default_factory=dict)  # EffectsTable.load(...)
    # Recherche web sans clé (tools/web.py, P1 18.10.2026) : 4 moteurs gratuits/publics (Exa,
    # Parallel, Firecrawl, Keenable — décision produit, pas de moteur payant, pas d'index maison).
    web_search_enabled: bool = True
    web_search_engines: list[str] = field(default_factory=lambda: ["exa", "parallel", "firecrawl", "keenable"])
    web_search_budget_s: float = 2.5
    web_search_blocked_terms: list[str] = field(default_factory=list)  # garde PII, en plus des motifs fixes
    # Aperçus de liens pour les bulles (nokker/previews/, P1 18.10.2026, docs/BUBBLES.md §5) :
    # OpenGraph/Twitter/oEmbed/Trafilatura, images re-servies par le noyau (cache disque borné).
    previews_enabled: bool = True
    previews_cache_dir: str = "/var/lib/nokker/previews"
    previews_ttl_s: float = 86400.0  # 24 h, aperçu JSON (pas l'image : voir ImageDiskCache, pas de TTL)
    previews_image_cache_max_bytes: int = 200 * 1024 * 1024  # 200 Mo, éviction la plus ancienne d'abord
    # Clé HMAC des URL d'image signées (docs/BUBBLES.md §9.3, `<img>` ne pose pas d'`Authorization`).
    # Vide (défaut) -> `PreviewService` en génère une au démarrage et la garde en mémoire (limite :
    # invalidée à chaque redémarrage, le client relit `GET /v1/previews` qui re-signe) ; un secret
    # d'environnement ici la rend stable entre redémarrages, SANS jamais passer par le YAML du dépôt.
    previews_sign_secret_env: str = "NOKKER_PREVIEWS_SIGN_SECRET"
    ledger_dsn_env: str | None = None  # None -> réutilise pg_dsn_env (même Postgres que le Store)
    # Magasin des images jointes (X7, nokker/images/store.py). Vide : `<local.state_dir>/images` sur Lokkal, aucune image
    # acceptée ailleurs (400 sur la route de chat).
    images_dir: str = ""
    host: str = "0.0.0.0"
    port: int = 8642
    tiers: dict[str, TierConfig] = field(default_factory=lambda: {
        "ship": TierConfig(base_url="https://router.sokkan.ch/v1", model="qwen/qwen3.8-27b"),
        "deep": TierConfig(base_url="https://router.sokkan.ch/v1", model="qwen/qwen3.5-397b-a17b"),
    })
    mcp_tool_servers: list[dict[str, Any]] = field(default_factory=list)  # [{"name","url","token_env"}]
    # Juge Nina QA (B-2). Mesuré le 17.10.2026 sur le banc B-1 (187 questions, Router) : Ship qwen3.8-27b 80,5 %,
    # Deep 397B 81,8 % — AUCUN backend n'atteint le seuil P1 de 90 %. Le juge est donc DÉSACTIVÉ par défaut
    # (`judge_enabled=False` → portes = invariants F3 seuls) jusqu'à un backend mesuré ≥ 90 % (cible : Nina-70B).
    judge_enabled: bool = False
    # `judge_thresholds_path` =
    # fichier produit par `python -m nokker.qa.calibrate` (None -> seuils par défaut prudents,
    # `qa.calibrate.DEFAULT_THRESHOLDS`, 0,3/0,7).
    judge: TierConfig = field(default_factory=lambda: TierConfig(base_url="https://router.sokkan.ch/v1",
                                                                  model="qwen/qwen3.8-27b"))
    judge_explicit: bool = False  # posé par `load_config` quand le YAML porte `judge:` (sinon `judge_config()` = Ship)
    judge_thresholds_path: str | None = None
    # ─────────────────────── X8 : juge allumé, multi-modèle (docs/LOKKAL.md §7.17) ───────────────────────
    # Rôles du juge indépendants des portes ci-dessus (`judge_enabled` = couche 2 des portes action/bascule, < 90 % au
    # banc B-1, éteinte) : relecture du diff avant « terminé » et avis sur les cartes fortes. Éteints par défaut.
    judge_review: bool = False
    judge_review_rounds: int = 1  # renvois de relecture à l'auteur par tour, au plus
    judge_advice: bool = False
    judge_max_tokens: int = 400  # relecture et avis ; plus pour un juge qui raisonne d'abord (gpt-oss)
    judge_verdict_max_tokens: int = 1  # questions des portes : 1 jeton (logprobs) ; plus pour un juge qui raisonne
    # Routage par tour : `policy` v0 (défaut : Ship, Deep sur signaux) | v1 (trivial → `small`, conversation → Ship,
    # code → Deep) ; `classifier` rules (défaut, aucun appel) | model (petit palier, 1 jeton, tour ambigu seulement) ;
    # `code_to_deep` (v1, défaut true) ; `classifier_max_tokens` (1 ; plus pour un petit modèle qui raisonne).
    route: dict[str, Any] = field(default_factory=dict)

    # ─────────────────────── P1 : approbations fortes (docs/APPROVALS.md) ───────────────────────
    # Doctrine produit (17.10) : jamais d'action à effet HITL exécutée sans preuve — donc câblé par défaut
    # (`True`), pas une option qu'il faut se souvenir d'activer. `False` reproduit le comportement
    # d'avant ce chantier (carte seulement dans `turn.pending_approvals`, aucune preuve exigée).
    approvals_enabled: bool = True
    approvals_dsn_env: str | None = None  # None -> réutilise pg_dsn_env (même Postgres que le Store)
    # Notificateur Telegram (sendMessage + bouton `web_app`) : désactivé par défaut (aucun appel réseau
    # tant que l'opérateur ne l'active pas explicitement ET ne fournit pas de jeton de bot).
    approvals_telegram_enabled: bool = False
    telegram_bot_token_env: str = "TELEGRAM_BOT_TOKEN"
    telegram_mini_app_url: str = ""
    # Origine attendue d'une preuve WebAuthn (web/passkey, ADR nokker-apps/docs/adr/0001-*.md) —
    # `clientDataJSON.origin` DOIT valoir exactement ceci. `None` = WebAuthn refusé (défaut sûr : pas
    # de vérification sans savoir quelle origine est légitime).
    webauthn_origin: str | None = None
    # ─────────────────────── P1 : fils et bulles (docs/BUBBLES.md) ───────────────────────
    # Routes /v1/threads*, /v1/bubbles*, /v1/notifications, événements turn.*/bubble.* et outils
    # bubble_update/session_send/agent_draft.
    bubbles_enabled: bool = True
    # Never clear (docs/NEVER-CLEAR.md) : fenêtre calculée (épisodes, délestage, réservoir, `recall_context`).
    # DÉSACTIVÉ par défaut tant que le canari n'a pas validé le chantier (comportement V0 inchangé) ;
    # `context_budgets` surcharge `nokker.context.types.ContextBudgets` (clés = noms des champs).
    context_enabled: bool = False
    context_budgets: dict[str, Any] = field(default_factory=dict)
    # Version CortHeXis de la mémoire de session (docs/NEVER-CLEAR.md §11) : le moteur CortHeXis (pgvector, embeddings,
    # reranker, projets) indexe les épisodes clos ; sans lui, recherche lexicale seulement. Clés : `enabled`,
    # `root_dir` (notes par fil), `database_url_env` (défaut CORTHEXIS_DATABASE_URL), `embed_urls`, `profile`
    # (leger|standard|gpu), `rerank_url`, `min_score`, `min_rerank`, `chunk_target`.
    context_corthexis: dict[str, Any] = field(default_factory=dict)
    # {sender: thread_id}. Une passerelle qui fait tourner l'identifiant de session à chaque `/new` (passerelle amont du
    # canari, vu le 19.10.2026 : « Session reset ») casserait le « never clear » : chaque session serait un fil neuf.
    # Pour ces identités, toutes les sessions tombent dans UN fil fixe ; un changement d'identifiant de session y
    # clôt l'épisode (raison `marker`) sans rien perdre.
    session_thread_by_sender: dict[str, str] = field(default_factory=dict)
    # Crew minimal (docs/BUBBLES.md §9.4) : GET/POST /v1/agents + activation de agent_draft. Pas
    # d'exécution réelle d'agent (P2, hors de ce chantier) — un agent créé reste `armed`.
    agents_enabled: bool = True
    # ─────────────────────── Console opérateur, lot A0 (doc interne nokker/espace-admin-2026-10.md) ───────────────────────
    # Second écouteur du processus, derrière Cloudflare Access (admin.nokker.ai → tunnel → admin_host:admin_port). Le jeton
    # Access est vérifié par la console (organisation `admin_access_team`, audience lue dans `admin_access_aud_env`) ; le
    # rôle vient de `admin_operators` ({e-mail: owner|ops|lecteur}), recopié dans la table `operators` au démarrage.
    # Dev seulement : sans clé d'API, accepter toute requête comme interne (sans identité). JAMAIS en bêta/prod : une
    # requête anonyme tomberait dans le locataire interne (trouvé à l'intégration multi-locataire, 21.10.2026).
    dev_open_api: bool = False
    # Locataire interne (opérateurs internes, canari). `false` sur la bêta OVH : aucun expéditeur n'y est interne (clés d'API,
    # `telegram_ids`, appareils de l'opérateur), et aucun appareil ne s'enrôle sans invitation. docs/TENANCY.md §2.
    internal_tenant: bool = True
    # Nokker Lokkal (docs/LOKKAL.md §7, nokker/local/api.py) : None (bêta) = pas d'API locale ni d'outils de code.
    # {token_path, allowed_hosts, code: {roots, host_roots, backend, image, network_name},
    #  secrets: {provider: file|openbao (défaut file ; NOKKER_SECRETS_PROVIDER prime), file: {path, key_env, key_file},
    #            openbao: {address, mount, prefix, auth, role_id_file, secret_id_file, token_file, ca_file}}}
    # (coffre local, lot X4 : nokker/secrets/, docs/en/secrets.md)
    local: dict[str, Any] | None = None
    # nokker.ai, côté cloud du lien Lokkal (docs/LOKKAL.md §7.2 révisé, §7.5, nokker/cloud/) : None (défaut) = aucune
    # route /v1/link/*, /v1/gateway/*, /v1/inference/*. {signing_key_env, base_url, gateway_domain}
    cloud_link: dict[str, Any] | None = None
    # Équipe (lot X17, docs/LOKKAL.md §7.15, nokker/team/) : projets partagés entre comptes nokker.ai, membres et rôles,
    # catalogue de skills, règles qui restreignent, noms de secrets, notes de projet en miroir. Éteint par défaut :
    # aucune route /v1/projects*, aucun champ `project` sur les agents, comportement inchangé.
    team: bool = False
    # Plan et incidents (lot X18, docs/LOKKAL.md §7.18). `plan` : cartes hiérarchiques, consignes héritées injectées,
    # avancement calculé, propositions de découpe, brief du matin (routes /v1/plan*, outils plan_*). `operate` : alertes
    # signées → incidents → agents `alert:<nom>` (routes /v1/alerts*, /v1/incidents*). Éteints par défaut : 404 comme
    # avant, aucun outil, aucun bloc de contexte. Le brief et les passages d'agents demandent le moteur Crew (X14).
    plan: bool = False
    operate: bool = False
    # Pool de navigateurs de la bêta (docs/BROWSER-POOL.md, nokker/tools/browser_pool.py) : noms des variables
    # d'environnement de l'adresse du gestionnaire et de son jeton. Variable absente ou vide = pas de navigateur.
    browser_pool_url_env: str | None = "NOKKER_BROWSER_POOL_URL"
    browser_pool_token_env: str = "NOKKER_BROWSER_POOL_TOKEN"
    # Instance qui accueille des testeurs (bêta OVH) : refuser de démarrer si le rôle Postgres échappe à la RLS
    # (superutilisateur, BYPASSRLS). Le canari (interne seulement, rôle superutilisateur) le laisse à False.
    require_rls: bool = False
    # ─────────────────────── Bêta multi-locataire : canaux et onboarding (docs/TENANCY.md) ───────────────────────
    # front1 (Suisse) : candidatures et invitations, lues par le noyau via intake.nokker.ai (tunnel + Access). Vide =
    # pas d'onboarding sur cette instance (le canari).
    front_url: str = ""
    front_token_env: str = "NOKKER_FRONT1_TOKEN"
    # Jeton de service Cloudflare Access devant intake.nokker.ai (en plus du Bearer de l'application).
    front_access_id_env: str = "NOKKER_FRONT1_ACCESS_CLIENT_ID"
    front_access_secret_env: str = "NOKKER_FRONT1_ACCESS_CLIENT_SECRET"
    # Bot Telegram de la BÊTA (jamais celui du canari ni celui de l'instance interne) : webhook /v1/telegram/webhook authentifié par
    # `secret_token`, liens d'appairage t.me/<bot>?start=<code>. Vide = canal désactivé.
    telegram_bot_username: str = ""
    telegram_webhook_secret_env: str = "NOKKER_TELEGRAM_WEBHOOK_SECRET"
    # Base de l'app web (liens des notifications : /approvals/<id>). Vide = pas de notification Telegram des testeurs.
    app_url: str = ""
    admin_enabled: bool = False
    admin_host: str = "127.0.0.1"
    admin_port: int = 8744
    admin_access_team: str = ""  # organisation Cloudflare Access (<équipe>.cloudflareaccess.com) ; requise si admin_enabled
    admin_access_aud_env: str = "NOKKER_ADMIN_ACCESS_AUD"
    admin_operators: dict[str, str] = field(default_factory=dict)
    admin_console_url: str = "https://admin.nokker.ai"
    # Tirage des candidatures depuis nokker-beta-api (front1, routes /internal/* liées à une adresse privée). Vide = pas de
    # tirage. Notification Telegram de chaque nouvelle candidature aux `admin_notify_chat_ids` (bot : ci-dessous).
    beta_pull_url: str = ""
    beta_pull_token_env: str = "NOKKER_BETA_PULL_TOKEN"
    beta_pull_interval_s: float = 60.0
    admin_notify_chat_ids: list[str] = field(default_factory=list)
    # Même liste lue dans l'environnement (identifiants séparés par des virgules), ajoutée à la précédente : un
    # identifiant de personne n'a pas à vivre dans la config versionnée.
    admin_notify_chat_ids_env: str | None = None
    # Jeton du bot qui envoie ces notifications (décision produit du 21.10 : le bot de prod de l'instance interne, pas celui du canari qui garde les
    # approbations). sendMessage SEULEMENT : ce bot est lu en continu par OpenClaw, un getUpdates/setWebhook d'ici lui
    # volerait ses messages. Vide = pas de notification Telegram.
    admin_notify_token_env: str = "NOKKER_ADMIN_NOTIFY_TOKEN"

    def images_root(self) -> str:
        """Dossier du magasin d'images : `images_dir`, sinon `<local.state_dir>/images` (Lokkal), sinon vide (aucune)."""
        if self.images_dir:
            return self.images_dir
        if self.local is not None:
            return str(Path(self.local.get("state_dir") or "~/.config/nokker").expanduser() / "images")
        return ""

    def vision_for_model(self, model: str) -> bool:
        """Vision d'un modèle choisi à l'onboarding (Router, clé de l'utilisateur, modèle local) : celle d'un palier de
        la config qui sert le même modèle (nom comparé sans préfixe ni casse), sinon non."""
        def bare(m: str) -> str:
            return (m or "").rsplit("/", 1)[-1].strip().lower()

        return any(t.vision and bare(t.model) == bare(model) for t in self.tiers.values()) if model else False

    def known_users(self) -> frozenset[str]:
        """Utilisateurs connus (partage de bulle, membres de fil) : identités des clés d'API + Telegram."""
        return frozenset({k.sender for k in self.api_keys} | set(self.telegram_ids.values()))

    def pg_dsn(self) -> str:
        return os.environ.get(self.pg_dsn_env, "")

    def ledger_dsn(self) -> str:
        if self.ledger_dsn_env:
            return os.environ.get(self.ledger_dsn_env, "")
        return self.pg_dsn()

    def approvals_dsn(self) -> str:
        if self.approvals_dsn_env:
            return os.environ.get(self.approvals_dsn_env, "")
        return self.pg_dsn()

    def telegram_bot_token(self) -> str:
        return os.environ.get(self.telegram_bot_token_env, "")

    def product_prefix(self) -> str:
        return self.product_prefix_path or str(PRODUCT_PREFIX_PATH)

    def memory_token(self) -> str:
        return os.environ.get(self.memory_mcp_token_env, "")

    def previews_sign_secret(self) -> str:
        return os.environ.get(self.previews_sign_secret_env, "")

    def api_key_map(self) -> dict[str, str]:
        """{jeton résolu: identité} — jetons vides ignorés (jamais de correspondance « vide » à vide)."""
        return {k.token(): k.sender for k in self.api_keys if k.token()}

    def admin_notify_ids(self) -> tuple[str, ...]:
        from_env = os.environ.get(self.admin_notify_chat_ids_env, "") if self.admin_notify_chat_ids_env else ""
        extra = [x.strip() for x in from_env.split(",") if x.strip()]
        return tuple(dict.fromkeys([*map(str, self.admin_notify_chat_ids), *extra]))

    def judge_config(self) -> TierConfig:
        """Modèle du juge : `judge:` du YAML s'il est posé ; sinon le palier Ship (un seul modèle : clé de l'utilisateur,
        modèle local — X8), sinon le premier palier, sinon le défaut du code."""
        default = TierConfig(base_url="https://router.sokkan.ch/v1", model="qwen/qwen3.8-27b")
        if self.judge_explicit or self.judge != default:
            return self.judge
        return self.tiers.get("ship") or next(iter(self.tiers.values()), None) or self.judge

    def go_senders(self) -> frozenset[str]:
        from nokker.qa.invariants import GoAuthorizedSenders

        extra = {*self.extra_go_senders, *self.telegram_ids}
        return frozenset(GoAuthorizedSenders().senders | {s.lower() for s in extra})


def load_config(path: str | None = None) -> NokkerConfig:
    path = path or DEFAULT_CONFIG_PATH
    p = Path(path)
    if not p.exists():
        return NokkerConfig()
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    tiers = {
        name: TierConfig(base_url=t["base_url"], model=t["model"], api_key_env=t.get("api_key_env", "ROUTER_KEY"),
                         reasoning_off_body=t.get("reasoning_off_body"),
                         fallback_model=t.get("fallback_model", ""), hedge_after_s=float(t.get("hedge_after_s", 8.0)),
                         cache=str(t.get("cache", "none")), vision=bool(t.get("vision", False)))
        for name, t in (raw.get("tiers") or {}).items()
    } or None
    # `api_keys: []` explicite = AUCUNE clé (bêta OVH), pas une clé par défaut ; absente = défaut.
    api_keys = [ApiKeyIdentity(token_env=k["token_env"], sender=k["sender"]) for k in raw.get("api_keys") or []] \
        if "api_keys" in raw else None
    judge_raw = raw.get("judge")
    judge = TierConfig(base_url=judge_raw["base_url"], model=judge_raw["model"],
                       api_key_env=judge_raw.get("api_key_env", "ROUTER_KEY"),
                       reasoning_off_body=judge_raw.get("reasoning_off_body")) if judge_raw else None
    kwargs: dict[str, Any] = {k: v for k, v in raw.items() if k not in ("tiers", "api_keys", "judge")}
    if tiers:
        kwargs["tiers"] = tiers
    if api_keys is not None:
        kwargs["api_keys"] = api_keys
    if judge:
        kwargs["judge"] = judge
        kwargs["judge_explicit"] = True
    return NokkerConfig(**kwargs)


__all__ = ["ApiKeyIdentity", "DEFAULT_CONFIG_PATH", "NokkerConfig", "PRODUCT_PREFIX_PATH", "TierConfig", "load_config"]
