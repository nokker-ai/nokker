"""Config du noyau (YAML + env) — PLAN-P1 §2.8 : « aucun secret dans le fichier : secrets par variables
d'env. » Le YAML porte la forme (URLs, modèles, chemins) ; toute valeur qui ressemble à un secret est lue
depuis l'environnement, jamais depuis le fichier.
"""
from __future__ import annotations

import logging
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = os.environ.get("NOKKER_CONFIG", "/etc/nokker/config.yaml")
# Persona produit d'un testeur de la bêta (docs/TENANCY.md §5), packagée avec le noyau.
PRODUCT_PREFIX_PATH = Path(__file__).parent / "prompts" / "prefix-nokker.md"


_JUDGE_FALLBACK_WARNED: set[str] = set()  # modèles déjà signalés (un WARNING par modèle et par processus)

DELEGATE_TIER_DESC_MAX = 120


@dataclass
class TierConfig:
    base_url: str
    model: str
    api_key_env: str = "ROUTER_KEY"
    # Corps ajouté pour couper le raisonnement sur ce palier. None = défaut du provider ({"reasoning_effort": "none"},
    # seule variante acceptée par le Router SOKKAN). vLLM/llama.cpp avec Qwen : {"chat_template_kwargs": {"enable_thinking": false}}.
    reasoning_off_body: dict[str, Any] | None = None
    # Corps ajouté quand le noyau demande la réflexion (pendant de `reasoning_off_body`). None = défaut du fournisseur
    # (openai : rien, l'amont raisonne ; anthropic : `{"thinking": {"type": "adaptive"}}`). Permet de régler la réflexion
    # par palier sans code (`{"thinking": {"type": "enabled", "budget_tokens": 8000}}`, `{"reasoning_effort": "low"}`…).
    reasoning_on_body: dict[str, Any] | None = None
    # Format anthropic : niveau d'effort de l'API (`output_config.effort`), envoyé à chaque appel du palier, réflexion
    # ou non. Vide (défaut) = rien envoyé. Un `reasoning_on_body` / `reasoning_off_body` qui pose `output_config` prime.
    effort: str = ""
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
    # `vision: auto` dans la config : `vision` reste faux et une sonde unique tranche au premier tour avec image
    # (nokker/providers/vision_probe.py).
    vision_auto: bool = False
    # Fenêtre de contexte servie (jetons, prompt + réponse) ; 0 = inconnue, pas de contrôle. Palier `nina` (70B, KV FP8,
    # 32 k) : un appel qui ne tient pas n'y part pas, il bascule sur Ship (`core/loop.py::_fit_context`).
    max_context_tokens: int = 0
    # Paramètres d'échantillonnage envoyés tels quels à chaque appel du palier (`temperature`, `top_p`…). Vide = défaut
    # de l'amont. Seules ces clés passent : rien qui puisse écraser model/messages/tools/stream.
    sampling: dict[str, Any] = field(default_factory=dict)
    # Format de l'API de l'amont : « openai » (défaut, `/chat/completions`, `providers/sokkan.py`) ou « anthropic »
    # (`/v1/messages`, `providers/anthropic.py`, docs/NOKKAN-ANTHROPIC.md). Absent = openai : rien ne change.
    format: str = "openai"
    # Format anthropic seulement : en-tête `anthropic-version` (vide = défaut du provider) et schéma d'authentification
    # (« x-api-key », ou « bearer » pour une passerelle qui attend `Authorization: Bearer`).
    anthropic_version: str = ""
    auth: str = "x-api-key"
    # Envoyer l'identifiant de l'appel en en-tête `X-Nokker-Call-Id` à l'amont de ce palier (défaut : non). À n'activer
    # que pour un amont à nous (Router, vLLM) : un amont tiers n'a pas à recevoir nos identifiants. Le ledger porte
    # l'identifiant dans tous les cas.
    send_call_id: bool = False
    # `gate: true` : ce palier est derrière la passerelle de contrôle de l'hôte et reçoit, SEUL, les en-têtes d'amont de la
    # session (`upstream_headers`, nokker/providers/gate.py). Défaut faux : aucun en-tête ne part.
    gate: bool = False
    # `tiers.<nom>.router` : ce palier parle-t-il au Router de Nokker ? None (absent) = déduit de l'hôte de `base_url`
    # (`router.sokkan.ch`) ; vrai/faux force (Router de préproduction sur un autre hôte). Un amont Router réécrit `model`
    # dans le corps : sans en-tête `X-Model-Served`, le modèle servi reste inconnu (core/model_served.py).
    router: bool | None = None
    # Délais du fournisseur du palier (`tiers.<nom>.timeout_s` / `total_budget_s`), en secondes : lecture/écriture d'une
    # requête (qui borne aussi le temps total d'un appel, réessais compris) et budget total d'attente d'une RÉPONSE de
    # l'amont. Défauts = les valeurs d'avant (180 et 80) : sans le champ, rien ne change (providers/sokkan.py).
    timeout_s: float = 180.0
    total_budget_s: float = 80.0
    # Format anthropic avec `cache: prefix` seulement : durée des points de cache (« 5m » | « 1h ») ; vide (champ absent) =
    # comportement d'avant (deux points, durée par défaut de l'API). Un champ posé ajoute un 3e point, sur le dernier message.
    cache_ttl: str = ""
    # Format anthropic : envoie l'en-tête bêta de liaison des blocs de réflexion et journalise (sans texte) les réécritures
    # de fenêtre que l'amont signale (`input_transformations`). Défaut faux : aucun en-tête bêta (providers/anthropic.py).
    binding_controls: bool = False
    # `tiers.<nom>.temperature` (nombre de 0 à 2) et `tiers.<nom>.max_tokens` (entier de 1 à 131072) : posés dans le corps de chaque
    # appel du palier (openai-compat, sokkan, anthropic ; anthropic récent ignore la température, voir `NO_SAMPLING_SINCE`).
    # None (absent) = comportement d'avant. `temperature` rejoint `sampling` ; un appelant qui impose `max_tokens` (noyau) prime.
    temperature: float | None = None
    max_tokens: int | None = None
    # `tiers.<nom>.action_claims_guard` : garde « c'est fait » sans action faite (`core/action_claims.py`) POUR CE PALIER. None (absent) = le
    # défaut : allumée sur `nina`, sinon le réglage d'hôte `local.action_claims_guard` ; un booléen posé prime (false éteint même `nina`).
    action_claims_guard: bool | None = None

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
    # Préfixe par MODÈLE servant le tour (Lokkal : `local.prefix_by_model: {<id de modèle>: <chemin>}`, lu au démarrage ; repli sur `prefix_path`). None : rien ne change.
    prefix_by_model: dict[str, str] | None = None
    # Variantes de préfixe CHOISIES PAR LA SESSION (`local.prefix_variants: {nom: chemin}` ; `prefix_variant` de POST /v1/local/sessions) : liste
    # blanche des préfixes alternatifs, lus au démarrage. None : aucune variante, rien ne change.
    prefix_variants: dict[str, str] | None = None
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
    # Pack « Essentiels », vague 1 (docs/ESSENTIALS.md, nokker/tools/essentials/) : météo et pollen suisses (MétéoSuisse
    # OGD), transports publics (transport.opendata.ch), change (BCE), Wikipédia, bulletin d'avalanches (SLF), jours
    # fériés suisses, conversions, calculs et dates en local. Sans compte, pour tous les locataires. Éteint (défaut) :
    # aucune source construite, aucun outil, contexte du modèle inchangé à l'octet.
    essentials: bool = False
    # Pack « Essentiels », vague 2 (docs/ESSENTIALS.md §Vague 2) : Grand Genève et France voisine, mobilité partagée,
    # bornes de recharge, rivières et lacs, qualité de l'air, vacances scolaires. Demande `essentials`. Vide ou
    # `enabled: false` (défaut) : aucun outil de plus, transit_* sans repli France, contexte inchangé à l'octet.
    # Clés : `enabled`, `contact_email` (adresse exigée par sharedmobility.ch ; vide = mobility_nearby absent),
    # `charging_permitted` (accord de l'OFEN pour l'usage commercial ; false = charging_nearby absent),
    # `navitia_token` (`secret:NOM`, variable d'environnement du pod ; vide ou illisible = pas de repli SNCF).
    essentials_v2: dict[str, Any] = field(default_factory=dict)
    # Pack Emploi (docs/JOBS-PACK.md, nokker/jobs/) : outils `jobs_*` adossés au service emploi (API partenaire, jeton
    # machine). Vide ou `enabled: false` (défaut) : aucune source, aucune route, contexte du modèle inchangé à l'octet.
    # Clés : `enabled`, `base_url`, `private_key` (`secret:NOM` : clé P-256 du client de service, JWT ES256 signé à chaque
    # appel), `issuer`, `audience`, `tokens_per_turn` (jetons du service = 1 tour du quota Nokker), `residence_country`
    # (défaut CH). Activation ensuite par membre (Packs → Emploi).
    jobs_pack: dict[str, Any] = field(default_factory=dict)
    # Aperçus de liens pour les bulles (nokker/previews/, P1 18.10.2026, docs/BUBBLES.md §5) :
    # OpenGraph/Twitter/oEmbed/Trafilatura, images re-servies par le noyau (cache disque borné).
    previews_enabled: bool = True
    # Steer (POST /v1/threads/{id}/steer) et mention de reprise après une pause, vus par le modèle (docs/CLIENT-API.md
    # §Pause et steer, docs/CONTEXT-FORMAT.md) : éteints par défaut tant que les traces d'entraînement ne les portent
    # pas. Éteint, la route de steer répond 404 ; la pause et la reprise marchent, sans rien ajouter au contexte.
    steer_enabled: bool = False
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
    # Pièces jointes texte, dossier et URL d'un message (`nokker/attachments.py`, docs/LOKKAL.md §7.6.1) : bloc
    # `[Pièces jointes]` dans l'en-tête du tour. Éteint (défaut) : `attachments` reste au format images X7 seul, le
    # modèle voit exactement ce qu'il voyait (contrat avec l'entraînement, docs/CONTEXT-FORMAT.md).
    text_attachments_enabled: bool = False
    # Provenance des valeurs (docs/PROVENANCE.md, `nokker/qa/provenance.py`) : `enabled` (défaut false : rien n'est
    # construit, le modèle voit exactement ce qu'il voyait), `strict` (origine inconnue = non fiable), `workspace_trusted`
    # (défaut true), `decisions` ({envoi|fetch|exec: carte|refus}).
    provenance: dict[str, Any] = field(default_factory=dict)
    # Réponses vocales Telegram (docs/VOICE.md, `nokker/voice/tts.py`) : `enabled` (défaut false : aucune voix, aucun
    # fournisseur construit), `provider` (command | openai_compat | elevenlabs), `reply_mode` (voice_in | always | off),
    # `max_chars`, et le bloc du fournisseur. Le fournisseur réel est un choix de l'opérateur. « Écouter » du web (§7,
    # `nokker/voice/web.py`, fournisseur qui streame seulement) : `web_max_chars` (4000), `web_output_format`.
    tts: dict[str, Any] = field(default_factory=dict)
    # Lexique de prononciation du foyer et normalisation suisse avant la synthèse (docs/VOICE.md §6,
    # `nokker/voice/speak_lexicon.py`) : `enabled` (défaut false : le texte part au TTS tel quel, octet pour octet),
    # `eighty` (huitante | quatre-vingts), `acronyms` ({sigle: lecture}), `entries` ({mot: prononciation}, Lokkal).
    speak_lexicon: dict[str, Any] = field(default_factory=dict)
    # « Ton Nokker » (V6, docs/AVATAR.md, `nokker/persona.py`) : nom, voix et avatar de l'assistant choisis à
    # l'onboarding. `enabled` (défaut false : aucune route /v1/persona*, voix et mur inchangés), `default_name`,
    # `voices` ([{id, label, langs, elevenlabs: {fr: …}}] ; absent : la seule voix configurée de `tts`, id `nina`).
    persona: dict[str, Any] = field(default_factory=dict)
    # Nina en direct (V6 live, docs/AVATAR.md §5, `nokker/nina_live.py`) : vidéo temps réel Runway + voix ElevenLabs,
    # texte produit par le noyau (Custom LLM). `enabled` (défaut false : aucune route, aucun appel sortant), `account`
    # (le titulaire), `agent_id`, `avatar_id`, `runway_key_file`|`runway_key_env`, `elevenlabs_key_env`,
    # `llm_secret_env`, `max_session_s` (600).
    nina_live: dict[str, Any] = field(default_factory=dict)
    # Heurtoir (docs/KNOCK.md, `nokker/knock/`) : déclencheurs physiques (toc toc toc, clap, touche, NFC) reçus de la
    # passerelle home-bridge. Vide ou `enabled: false` (défaut) : aucun fil lancé, aucune lecture de la passerelle,
    # aucune carte décidée autrement. Clés : `enabled`, `rooms`, `approve` ({enabled, rules, window_s ≤ 60, presence_max_age_s}).
    knock: dict[str, Any] = field(default_factory=dict)
    # Ambiant du foyer (docs/AMBIENT.md, `nokker/ambient/`) : `room_audio` (voix dite dans la pièce, Lokkal + passerelle
    # home-bridge) et `eink` (écrans e-ink du foyer, cloud et Lokkal), chacun avec `enabled` (défaut false : aucune
    # route, aucun fil, rien ne change).
    ambient: dict[str, Any] = field(default_factory=dict)
    # Règles d'approbation suggérées (docs/APPROVALS.md §7) : `enabled` (défaut false : routes 404), `threshold`
    # (cartes approuvées sans refus, défaut 5), `window_days` (défaut 30).
    approval_suggestions: dict[str, Any] = field(default_factory=dict)
    # Relecteur automatique des cartes `standard` (docs/APPROVALS.md §8) : `enabled` (défaut false : aucune carte relue),
    # `tier` (`judge` par défaut, ou un palier de `tiers`), `policy` (texte de la politique ; défaut
    # `auto_review.DEFAULT_POLICY`), `max_tokens`, `trajectory_calls`, `max_approvals_per_turn`.
    auto_review: dict[str, Any] = field(default_factory=dict)
    host: str = "0.0.0.0"
    port: int = 8642
    tiers: dict[str, TierConfig] = field(default_factory=lambda: {
        "ship": TierConfig(base_url="https://router.sokkan.ch/v1", model="qwen/qwen3.8-27b"),
        "deep": TierConfig(base_url="https://router.sokkan.ch/v1", model="qwen/qwen3.5-397b-a17b"),
    })
    mcp_tool_servers: list[dict[str, Any]] = field(default_factory=list)  # [{"name","url","token_env", "description_max_chars" (défaut 4000)}]
    # Garde SSRF des clients sortants (nokker/previews/ssrf.py, chantier p2/ssrf-audit) : `strict` (défaut false :
    # comportement historique, octet pour octet), `allow` (["hôte:port", …] autorisés vers le réseau privé, jamais
    # link-local), `ports` (défaut [80, 443, 8080, 8443] pour la récupération web, les MCP ajoutés, les compétences).
    ssrf: dict[str, Any] = field(default_factory=dict)
    # Juge Nina QA (B-2). Mesuré le 17.10.2026 sur le banc B-1 (187 questions, Router) : Ship qwen3.8-27b 80,5 %,
    # Deep 397B 81,8 % — AUCUN backend n'atteint le seuil P1 de 90 %. Le juge est donc DÉSACTIVÉ par défaut
    # (`judge_enabled=False` → portes = invariants F3 seuls) jusqu'à un backend mesuré ≥ 90 % (cible : Nina-70B).
    judge_enabled: bool = False
    # `judge_thresholds_path` =
    # fichier produit par `python -m nokker.qa.calibrate` (None -> seuils par défaut prudents,
    # `qa.calibrate.DEFAULT_THRESHOLDS`, 0,3/0,7).
    judge: TierConfig = field(default_factory=lambda: TierConfig(base_url="https://router.sokkan.ch/v1",
                                                                  model="qwen/qwen3.8-27b"))
    # Vrai : refuse de démarrer si le juge, le relecteur de cartes (`auto_review`, tier judge) ou un correcteur
    # (`outcomes_grader: judge`) est allumé sans bloc `judge:` explicite (sinon repli silencieux sur le palier Ship).
    judge_require_explicit: bool = False
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
    # ─────────────── Rubrique, correcteur séparé et objectifs persistants (docs/OUTCOMES-GOAL.md) ───────────────
    # Éteints par défaut : rien ne change (`Kernel.goals is None`, ni outil, ni bloc, ni route).
    # `outcomes_enabled` : rubrique d'une tâche (`POST /v1/threads/{id}/goal {mode: task}`, outil `rubric_propose`),
    # notée par le correcteur dans sa propre fenêtre ; sous `outcomes_threshold` (part du maximum), l'agent refait une
    # passe, `outcomes_max_passes` fois au plus par tour. `outcomes_grader` : palier du correcteur, `judge` (le juge
    # X8, `judge:` ou Ship) ou un nom de palier de `tiers` (ship, deep, small…).
    outcomes_enabled: bool = False
    outcomes_grader: str = "judge"
    outcomes_threshold: float = 1.0
    outcomes_max_passes: int = 2
    outcomes_max_tokens: int = 600
    # `goals_enabled` : objectifs sur plusieurs tours (`/goal <texte>` dans un fil, l'API, ou la CLI par
    # `metadata.cli_control`), persistés sous le fil (migration 050), repris au redémarrage ; budget par défaut
    # `goals_default_budget` (tokens, seconds, turns, cost + currency) ; point d'étape toutes les
    # `goals_checkpoint_every` notes ou `goals_checkpoint_s` secondes, sur le Telegram relié au compte.
    goals_enabled: bool = False
    goals_default_budget: dict[str, Any] = field(default_factory=lambda: {"tokens": 400_000, "seconds": 7200,
                                                                          "turns": 20})
    goals_checkpoint_every: int = 3
    goals_checkpoint_s: float = 1800.0
    # Routage par tour : `policy` v0 (défaut : Ship, Deep sur signaux) | v1 (trivial → `small`, conversation → Ship,
    # code → Deep) ; `classifier` rules (défaut, aucun appel) | model (petit palier, 1 jeton, tour ambigu seulement) ;
    # `code_to_deep` (v1, défaut true) ; `classifier_max_tokens` (1 ; plus pour un petit modèle qui raisonne) ;
    # `deep_tool_failures` (3) : échecs d'outil d'un tour avant Deep ; `deep_reasoning` (true) : réflexion demandée sur le palier deep (false : elle
    # suit celle de Ship, sans la forcer ; une session le fait aussi par `deep_reasoning: false`) ; `nina` : palier maison à part
    # (`{mode: off|shadow|route, shadow_fraction, kinds, max_tokens, max_in_flight}`, route/nina.py, défaut off).
    route: dict[str, Any] = field(default_factory=dict)
    # Conseiller intra-tour (veille 01.11 §7.1, `nokker/route/advise.py`) : outil `advise` par lequel l'exécutant
    # consulte un palier plus fort au milieu du tour. Clés : `enabled` (défaut false : outil absent), `tier` (clé de
    # `tiers`, défaut deep), `fallback_tier` (ex. nina, déclaré sous `tiers`), `max_per_turn` (2), `max_tokens` (600),
    # `question_chars`, `context_chars`, `demand_chars`. Locataire interne seulement (Lokkal, canari).
    advise: dict[str, Any] = field(default_factory=dict)
    # Fork de sous-agent (veille 01.11 §3.1, `nokker/context/fork.py`) : `delegate(fork=true)` part du contexte exact du
    # dernier appel du parent (même préfixe, cache réutilisé). Défaut false : schéma de `delegate` et noyau inchangés.
    delegate_fork: bool = False
    # Palier du sous-agent : `delegate(tier=ship|deep)`. Défaut false : schéma et description de `delegate` inchangés ;
    # un enfant ne dépasse jamais les droits du parent (deep refusé à un compte sans deep).
    delegate_tier: bool = False
    # Paliers du sous-agent choisis par l'hôte : liste de noms de `tiers` (ex. `[outils, defi, redaction, petit]`), exposée au
    # modèle comme enum du paramètre `tier` de `delegate` (demande `delegate_tier: true`). Un nom absent de `tiers` est refusé
    # au démarrage (`check_delegate_tiers`). Vide (défaut) : `ship` et `deep`, comme avant.
    # Chaque entrée : une chaîne (le nom) ou `{name, description}` ; la description (120 car. au plus, une ligne) est citée entre
    # parenthèses après le nom dans le paramètre `tier` : un hôte devrait en fournir une par nom (un nom nu ne dit pas à quoi
    # il sert, le modèle ne peut pas choisir).
    delegate_tiers: list[Any] = field(default_factory=list)
    # Locataires (ids, ex. `tn_nina`) à qui `delegate` est offert en conversation (hors sessions de code, Lokkal et
    # CLI qui l'ont déjà). Vide par défaut : rien n'est ouvert.
    delegate_tenants: list[str] = field(default_factory=list)

    # ─────────────────────── P1 : approbations fortes (docs/APPROVALS.md) ───────────────────────
    # Doctrine produit (17.10) : jamais d'action à effet HITL exécutée sans preuve — donc câblé par défaut
    # (`True`), pas une option qu'il faut se souvenir d'activer. `False` reproduit le comportement
    # d'avant ce chantier (carte seulement dans `turn.pending_approvals`, aucune preuve exigée).
    approvals_enabled: bool = True
    approvals_dsn_env: str | None = None  # None -> réutilise pg_dsn_env (même Postgres que le Store)
    # Réglages des cartes : `executing_stale_s` (défaut 900, plancher 60) = ancienneté d'une carte `executing` au-delà de
    # laquelle elle passe `interrupted` (docs/APPROVALS.md), au démarrage et à chaque balayage.
    approvals: dict[str, Any] = field(default_factory=dict)
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
    # Qui crée et migre la base CortHeXis (docs/MEMORY-TENANCY.md §5.1). `managed` (défaut : Lokkal, développement) : le
    # pod, au premier usage, et `ensure` au démarrage. `external` (bêta) : le job `nokker migrate` avec
    # CORTHEXIS_OWNER_DATABASE_URL (rôle propriétaire) ; le pod ne crée ni ne modifie rien et vérifie locataire, RLS
    # forcée et propriété au démarrage. `context_corthexis` accepte aussi `owner_database_url_env` (défaut
    # CORTHEXIS_OWNER_DATABASE_URL) et `app_role` (défaut nokker_app : rôle qui reçoit le DML).
    corthexis_schema: str = "managed"
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
    # Traces d'entraînement / d'évaluation (docs/TRACES.md, nokker/traces/) : `traces_record: true` garde CHAQUE appel
    # modèle (messages exacts envoyés au fournisseur, schémas d'outils, suite du tour) dans `trace_calls` (migration
    # 045). Défaut false : rien n'est enregistré. `traces_retention_days` borne la table (purge par le noyau, au plus
    # une fois par heure et par locataire, et par `nokker traces purge`). `traces_target` : beta | canary | lokkal
    # (vide : lokkal si le bloc `local` est posé, sinon unknown).
    traces_record: bool = False
    traces_retention_days: int = 30
    traces_target: str = ""
    # Nokker Lokkal (docs/LOKKAL.md §7, nokker/local/api.py) : None (bêta) = pas d'API locale ni d'outils de code.
    # {token_path, allowed_hosts, code: {roots, host_roots, backend, image, network_name, network, host_volumes,
    #  require_sandbox : bac docker pour un hôte, docs/HOST-EMBED.md §1},
    #  secrets: {provider: file|openbao (défaut file ; NOKKER_SECRETS_PROVIDER prime), file: {path, key_env, key_file},
    #            openbao: {address, mount, prefix, auth, role_id_file, secret_id_file, token_file, ca_file}}}
    # (coffre local, lot X4 : nokker/secrets/, docs/en/secrets.md)
    local: dict[str, Any] | None = None
    # Maison (docs/HOME-ASSISTANT.md, nokker/home/) : `enabled` (défaut false : aucun outil, aucune connexion), `url`,
    # `token: secret:NOM` (jamais une valeur), `entities` (liste blanche), `members`/`roles`, `tier` (small), `climate`.
    home_assistant: dict[str, Any] = field(default_factory=dict)
    # Kiosque HDMI d'un Lokkal (docs/KIOSK.md, nokker/kiosk.py) : `{enabled, place: {lat, lon, stops}, ha_entities,
    # tz}`. Éteint par défaut (aucune route). Lecture seule, jeton local du titulaire, sur la machine seulement.
    kiosk: dict[str, Any] = field(default_factory=dict)
    # Veille du foyer (docs/HOME-WATCH.md, nokker/homewatch/) : `enabled` (défaut false : aucun outil, aucune connexion,
    # aucun fil), `bridge_url` (http://home-bridge:8800), `media_base_url` (adresse de la passerelle vue de l'écran
    # local), `members`/`roles`/`default_role`, `tenant`, `tz`, `poll_s`, `slow_poll_s`, `retention_h`. Lokkal seul ;
    # le jeton de la passerelle vient de NOKKER_HOME_BRIDGE_TOKEN, jamais de ce fichier.
    home_watch: dict[str, Any] = field(default_factory=dict)
    # Sessions de code multi-utilisateur (docs/LOKKAL.md §7.28, nokker/code_sessions/) : None ou `enabled: false`
    # (défaut) = aucune route /v1/code/*, aucun outil de code pour les comptes. {enabled, backend: docker|kubernetes,
    # workspace_root, layout: tenant/user/session, max_sessions_per_user, idle_ttl_s, instance, modes, default_mode,
    # forge: {allowed_hosts}, secrets: {provider, file, openbao}, hooks, state_dir, docker: {image, network,
    # network_name, host_root}, kubernetes: {…, transmis au module du bac à sable}}. Refusé dans Lokkal (local.code).
    code: dict[str, Any] | None = None
    # `execute_code` (docs/LOKKAL.md §7.31, nokker/tools/execute_code.py) : un script Python du modèle, dans le bac à
    # sable du fil, qui appelle plusieurs outils en un appel (mêmes portes que la boucle). Éteint = outil absent de la
    # surface (ni schéma ni index, appel → « outil inconnu »). Bloc `execute_code` : {tools (liste blanche, lectures par
    # défaut), timeout_s, max_timeout_s, max_calls, stdout_chars, return_chars, error_chars, python}.
    execute_code_enabled: bool = False
    execute_code: dict[str, Any] = field(default_factory=dict)
    # `POST /v1/memory/search` (docs/MCP-SERVER.md) : recherche dans la mémoire longue du compte, sous la même
    # identité et le même locataire que les autres routes. Éteint = 404. Sert le serveur MCP `nokker mcp serve`.
    memory_search_api: bool = False
    # API hôte (docs/NOKKAN-HOST-API.md) : ce qu'un produit hôte qui embarque le noyau lui demande en plus. Trois
    # drapeaux indépendants, éteints par défaut (éteint = schémas, routes, événements et réponses d'avant, à l'octet).
    # `host_clarify_options` : `clarify` prend une liste d'options (+ « Autre (à préciser) » ajoutée par le noyau),
    # événements `question.asked` / `question.answered`, routes `/v1/local/questions*`, boutons sur Telegram.
    host_clarify_options: bool = False
    # `host_session_budget` : plafond de dépense par session, posé par l'hôte (`budget` à l'ouverture d'une session
    # locale, `GET|PUT /v1/local/sessions/{fil}/budget`), décompté sur le ledger (jetons et/ou coût), refus AVANT tout
    # appel modèle avec un code stable (`session_budget_exhausted`, nokker/ledger/session_budget.py).
    host_session_budget: bool = False
    # `host_reasoning_stream` : la réflexion renvoyée par l'amont (`reasoning` / `reasoning_content`) est exposée au
    # client dans un champ SÉPARÉ (`nokker.reasoning` du flux d'un tour, événement `turn.reasoning`), caviardée, pour
    # un affichage replié. Jamais dans le texte de la réponse, le contexte du modèle, le store ni la mémoire.
    host_reasoning_stream: bool = False
    # Client MCP à la révision 2026-07-28 (docs/MCP-NEXT.md §1-§2) pour les serveurs de `mcp_tool_servers` : sonde
    # `server/discover` puis repli `initialize` (serveurs 2025-11-25 et avant), MRTR, Tasks, listes en cache selon
    # `ttlMs`. Éteint = poignée de main `initialize` seule, comme avant. `mcp_task_wait_s` : attente max d'une tâche.
    mcp_modern: bool = False
    mcp_task_wait_s: float = 20.0
    # MCP Apps (docs/MCP-NEXT.md §3-§4) : l'interface `ui://` d'un outil part au client (`tool.app`), jamais au modèle ;
    # ses `tools/call` repassent par les portes (POST /v1/apps/{app_id}/tools/call). Éteint = ni annonce de l'extension,
    # ni événement, ni route (404).
    mcp_apps: bool = False
    # Domaine DÉDIÉ des apps MCP (`https://hôte[:port]`, distinct de l'app : refus au démarrage sinon). Vide (défaut) :
    # `tool.app` porte `sandbox: "allow-scripts"` seul (iframe srcdoc, origine opaque) ; posé : `allow-scripts
    # allow-same-origin` et `sandbox_origin`, pour un proxy servi sur ce domaine.
    mcp_apps_origin: str = ""
    # Serveurs MCP en transport stdio (docs/NOKKAN-MCP-STDIO.md, nokker/tools/mcp_stdio.py) : un processus par fil,
    # lancé DANS le bac à sable de la session de code, identité par variables d'environnement de la session. Éteint par
    # défaut (`enabled` absent ou autre que true : rien n'est lu, rien n'est importé). Interdit hors Lokkal (instance
    # multi-locataire, sessions de code des comptes) même allumé. {enabled, servers: [{name, command, args, env: {NOM:
    # secret:NOM | session | thread}, read_tools}], timeout_s, start_timeout_s, idle_ttl_s, max_processes}
    mcp_stdio: dict[str, Any] = field(default_factory=dict)
    # nokker.ai, côté cloud du lien Lokkal (docs/LOKKAL.md §7.2 révisé, §7.5, nokker/cloud/) : None (défaut) = aucune
    # route /v1/link/*, /v1/gateway/*, /v1/inference/*. {signing_key_env, base_url, gateway_domain}
    cloud_link: dict[str, Any] | None = None
    # Équipe (lot X17, docs/LOKKAL.md §7.15, nokker/team/) : projets partagés entre comptes nokker.ai, membres et rôles,
    # catalogue de skills, règles qui restreignent, noms de secrets, notes de projet en miroir. Éteint par défaut :
    # aucune route /v1/projects*, aucun champ `project` sur les agents, comportement inchangé.
    team: bool = False
    # Réglages de l'équipe : {budget_rates: {CHF: <EUR pour 1 CHF>, USD: <EUR pour 1 USD>}} — taux de conversion des
    # dépenses vers la devise d'un budget de projet (nokker/team/budget.py) ; USD ↔ CHF vient du catalogue de prix.
    # Absent : une dépense dans une devise sans taux rend le budget invérifiable (tour refusé par prudence).
    team_settings: dict[str, Any] | None = None
    # Foyer (lot X23, docs/LOKKAL.md §7.29, nokker/household/) : un projet d'équipe de type `household` (demande `team`),
    # quota commun du payeur compté dans le ledger, mémoire du foyer commune. Éteint par défaut : aucune route
    # /v1/household*, aucun tour attribué, comportement inchangé. `household_minors` (éteint) : profils teen/child,
    # attestation du représentant légal, garde-fous (carte forte du représentant, liste blanche, préfixe adapté).
    # household_settings : {plan: famille, turns_per_day: 110, turns_per_week: 550, member_turns_per_day: 40,
    # max_members: 6 (au plus), mode: hard|soft} — le palier `plan` de `billing_plans`, s'il existe, donne le quota.
    household: bool = False
    household_minors: bool = False
    # household_minors_mode : `all` (défaut) tout payeur ; `allowlist` les seuls payeurs autorisés un par un par
    # l'opérateur (`python -m nokker household minors allow`, en base, §7.29.4). Autre 403 `minors_not_enabled`.
    household_minors_mode: str = "all"
    household_settings: dict[str, Any] | None = None
    # Papiers du foyer (docs/HOUSEHOLD-PAPER.md, nokker/household/paper/) : import de courrier et de PDF, QR-facture
    # suisse décodée, échéances et programmes vers l'agenda (carte de relecture), fichier pain.001 PRÉPARÉ derrière une
    # carte forte (jamais envoyé ni payé), relevés fiscaux eCH-0196 rangés par année. Éteint par défaut : aucune route
    # /v1/paper*, aucun outil `paper_*`, aucune porte de plus, contexte inchangé à l'octet. Ne demande PAS `household`
    # (un compte seul a ses papiers) ; le partage entre membres, lui, demande un foyer.
    household_paper: bool = False
    # Calendrier familial (docs/FAMILY-CALENDAR.md, nokker/household/famcal/) : photo ou PDF d'un planning (école,
    # club, convocation, déchetterie) → brouillon relu par le noyau (dates ambiguës, doublons) → UNE carte → agenda CalDAV
    # du compte (sinon fichier .ics). Éteint par défaut : aucune route /v1/family-calendar*, aucun outil
    # `family_calendar_*`, aucune porte de plus, contexte inchangé à l'octet. Ne demande ni `household` ni
    # `household_paper` (un PDF importé dans les Papiers se lit si ce drapeau-là est allumé aussi).
    family_calendar: bool = False
    # Projets du foyer, l'agent chef de projet familial (docs/FAMILY-PROJECTS.md, nokker/household/projects/) : plan
    # (jalons, tâches, assignés, échéances, budget indicatif) → brouillon relu par le noyau → UNE carte `family:project`
    # → projet du foyer ; suivi (coche locale, relance douce dans le foyer, point hebdo, replanification par un nouveau
    # brouillon). Profils mineurs : leurs tâches seulement. Demande `household`. Éteint par défaut : aucune route
    # /v1/household/projects*, /tasks*, /project-drafts*, /project-settings, aucun outil `family_project_*`, aucune
    # porte de plus, contexte inchangé à l'octet.
    family_projects: bool = False
    # Paiements « l'agent prépare, l'humain paie » (docs/PAYMENTS.md, nokker/payments/) : arrêt au paiement dans le
    # navigateur (aucun champ de carte rempli, bouton de paiement refusé), outil `pay_request` → carte FORTE « à payer »,
    # mandat signé par la passkey (WebAuthn, `webauthn_origin` requis), budgets par membre du foyer. Éteint par défaut :
    # aucun outil, aucune porte, aucune route /v1/payments*, défis d'approbation inchangés.
    # payments_settings : {default_budget: {currency: CHF, per_purchase: 200, per_day: 300, per_month: 1000} | null,
    # mandate_ttl_s: 86400, timezone: Europe/Zurich, handoff: true}
    payments: bool = False
    payments_settings: dict[str, Any] | None = None
    # Agent de paiement (docs/PAY-AGENT.md, service SÉPARÉ `python -m payagent`) : avec `payments` allumé, un mandat
    # signé peut confier le paiement à l'agent (Stripe Shared Payment Tokens) quand le vendeur a un profil Stripe connu
    # et que l'approbateur a enregistré un moyen de paiement ; sinon la personne paie elle-même, comme sans ce drapeau.
    # Éteint par défaut : aucune route /v1/payments/method*, description de `pay_request` et mandats inchangés.
    # pay_agent_settings : {url: http://pay-agent:8790, publishable_key: pk_…, sellers: {shop.example.ch: profile_…}}
    # Secrets hors YAML : NOKKER_PAY_AGENT_TOKEN, NOKKER_PAY_AGENT_CALLBACK_SECRET.
    pay_agent: bool = False
    pay_agent_settings: dict[str, Any] | None = None
    # Données de carte masquées pour le modèle (docs/PAYMENTS.md §2 bis) : le masquage est TOUJOURS actif ; ce drapeau
    # n'allume que ses deux textes vus par le modèle (repère « [Masque] n numéro(s)… » en fin de résultat, texte de la
    # capture refusée). Éteint : pas de repère, ancien texte de capture.
    card_mask_notice: bool = False
    # Bulle `approval` posée par le noyau dans le fil à chaque carte qui revient aux humains (docs/BUBBLES.md,
    # `BubbleHub.approval_pending`), suivie à la décision et à l'expiration ; protégée du modèle et des clients. Éteint :
    # seule la bulle d'une décision du relecteur automatique existe, comme avant.
    approval_bubbles: bool = False
    # Outils des sessions de code (§7.28 : read_file, search_files, write_file, patch, terminal, todo, checkpoints,
    # skills de session, delegate en fond et delegate_status/delegate_cancel, execute_code) OFFERTS seulement dans un
    # fil à session de code vivante (`tenancy/resources.py`) : ailleurs, leur schéma est absent (pas seulement refusé).
    # Éteint (défaut) : le jeu du locataire dans tous ses fils, comme avant.
    code_tools_session_only: bool = False
    # La VOIX (boucle principale, profondeur 0) lit seulement parmi les outils des sessions de code : write_file, patch,
    # terminal, todo et execute_code sont absents de son jeu, même dans un fil de session ; elle fait construire par
    # `delegate` (worktree), dont les enfants (`/sa-`) gardent tout le jeu. Éteint (défaut) : rien ne change.
    voice_read_only: bool = False
    # Routeur de delegate (harnais du 70B) : la voix confie et attend, sans palier ; le noyau lance UN enfant « routeur »
    # (son modèle : `delegate_router_settings` {model (défaut qwen/qwen3.5-397b-a17b), base_url, api_key_env : ceux du
    # palier ship par défaut}) qui choisit ship, deep ou les deux et rend UN compte rendu. Éteint (défaut) : inchangé.
    delegate_router: bool = False
    delegate_router_settings: dict[str, Any] | None = None
    # Notes de fonctions (`nokker/guide/notes.py`) : une note standard par fonction du catalogue du Guide (source
    # unique), 0 à 3 dans un bloc `[Fonctions]` de l'en-tête du tour, filtrées par drapeaux et profil mineur, et l'outil
    # `feature_get`. Éteint (défaut) : aucun bloc, aucun outil, rien ne change.
    feature_notes: bool = False
    feature_notes_settings: dict[str, Any] | None = None  # min_cosine, min_hits, max_notes, budget_chars
    # Choix de l'outil au paiement (banc du palier 02.11 : pay_request posé 2 fois sur 3 au mieux, le modèle demande une
    # confirmation en texte) : avec `payments`, description de `pay_request` qui dit d'appeler l'outil au lieu de
    # répondre ou de demander (la carte EST la confirmation). Éteint par défaut : description inchangée à l'octet.
    # Sans effet quand l'agent de paiement (`pay_agent`) fournit sa propre description.
    pay_tool_choice: bool = False
    # Consigne de langue du préfixe produit (nokker/context/reply_language.py) : la ligne de langue de la section Style
    # est remplacée par une consigne en tête de section (répondre dans la langue de la personne même si le préfixe, les
    # blocs, les outils ou les documents sont dans une autre). Éteint par défaut : préfixe identique à l'octet près.
    reply_language: bool = False
    # Ligne de langue en fin d'en-tête du tour, juste avant le message (nokker/context/reply_language.py::TAIL_LINE) ;
    # indépendante de `reply_language`. Éteint par défaut : en-tête du tour identique à l'octet près.
    reply_language_tail: bool = False
    # Route v1 (`route.policy: v1`, classeur par règles) : une demande de résumé ou de relais d'un document reste sur le
    # palier de la voix (conversation) même avec des mots techniques (nokker/route/classify.py::_RELAY_ASK). Aucun texte
    # vu par le modèle. Éteint par défaut : classement inchangé.
    route_relay_voice: bool = False
    # Salle Maison (chantier p2/house-room, docs/HOUSE-ROOM.md, nokker/house/) : une salle persistante par foyer, où les
    # membres parlent aux agents de la maison (intendant par défaut, @mention pour un agent) avec les droits de CHAQUE
    # membre ; outil `ask_house` dans les sessions des membres. Demande `household`. Éteint par défaut : aucune route
    # /v1/house*, aucun outil, aucun texte vu par le modèle ne change.
    # house_room_settings : {agents: {<slug>: {tier, profiles, enabled}}, rights: {adult, teen, child},
    # transcript_messages, transcript_chars, max_consults, max_text, page} (nokker/house/roster.py::Settings).
    house_room: bool = False
    # Mode « montre-moi » (docs/SHOW-ME.md, nokker/showme/) : outil `point_at` (lecture seule, aucune carte) qui pose un
    # repère (flèche + légende) sur un élément du navigateur du fil, de l'interface ou d'une image jointe, en bulle
    # `pointer` ; « Fait » dans la bulle → étape suivante. Demande les bulles (hub). Éteint par défaut : aucun outil,
    # aucune bulle `pointer`, aucun texte vu par le modèle ne change.
    show_me: bool = False
    # Vue navigateur en direct (lot V3 « Agent qui montre », docs/BROWSER-LIVE.md, nokker/browserlive/) : événements
    # browser.* sur /v1/events, captures EN MÉMOIRE (URL signée 60 s), passation à l'humain, bulle `browser`. Demande le
    # pool de navigateurs et le bus d'événements. Éteint : aucune route /v1/browser/*, rien ne change.
    browser_live: bool = False
    # Navigateur générique de Lokkal (nokker/tools/browser_sites.py) : outils browser_* du locataire interne servis par
    # un enfant dédié de sites-chrome (NOKKER_SITES_BROWSER_URL), id `st_` + 10 [a-z0-9]. Éteint : aucun outil browser_*.
    sites_browser_generic: bool = False
    sites_browser_generic_id: str = "st_navigateur"
    house_room_settings: dict[str, Any] | None = None
    # Vue « Qui fait quoi » (docs/WHO-DOES-WHAT.md, nokker/presence/) : présence des agents (fils en cours, sessions de
    # code, objectifs, sous-agents, agents Crew, salle Maison) dérivée de l'état existant, route GET /v1/presence en
    # lecture seule et événements `presence.changed`. Aucun texte vu par le modèle. Éteint par défaut : aucune route,
    # aucun observateur sur le bus, rien ne change.
    who_does_what: bool = False
    # Guide (docs/GUIDE.md, nokker/guide/) : sur-agent qui présente les fonctions allumées pour le compte, comment les
    # activer, et propose la suivante d'après les compteurs d'usage (codes, jamais de contenu). Routes /v1/guide*,
    # appel modèle sur le palier `guide_settings.tier` (défaut `small`, sinon Ship). N'active rien. Éteint : aucune
    # route, aucun observateur, rien de vu par le modèle.
    guide: bool = False
    guide_settings: dict[str, Any] | None = None  # tier, max_tokens (400), max_asks_per_day (40)
    # SSO OIDC des équipes (docs/LOKKAL.md §7.24, nokker/sso/) : connexion par le fournisseur d'une organisation,
    # groupes → rôles des projets, « SSO obligatoire ». Demande `team`. Éteint par défaut : aucune route /v1/sso*,
    # /v1/auth/oidc/*, /v1/projects/{id}/sso, aucun contrôle de plus sur l'équipe.
    # sso_settings : {redirect_uri (https, la page du client web qui renvoie code et state), secrets: {provider: file|
    # openbao, file: {...}, openbao: {...}} (secrets des clients, portée system), state_dir, dns_over_https (URL DoH
    # JSON pour prouver un domaine)}
    sso: bool = False
    sso_settings: dict[str, Any] | None = None
    # Packs d'agents chez nokker.ai (docs/LOKKAL.md §7.30, nokker/webpacks/) : un compte installe dans SON périmètre les
    # compétences et les modèles d'agents d'un pack livré avec le noyau qui tournent au web (jamais les serveurs MCP ni
    # ce qui demande une machine). Éteint par défaut : aucune route /v1/packs*, aucun outil de pack pour les testeurs.
    packs_web: bool = False
    # Apprentissage sous contrôle humain (docs/LEARNING.md, nokker/learning/) — comptes `us_…` de nokker.ai ; Lokkal lit
    # les mêmes clés sous `local`. Éteints par défaut : aucun outil, aucune ligne, aucune route de plus.
    # `learning` : skill_create/update/history/revert (carte obligatoire, versions, journal, /v1/learning/*) ;
    # `learning_nudges` : une ligne fixe au tour qui suit un tour réussi riche en outils ; `memory_pending` : les
    # écritures de mémoire durable attendent l'approbation (/v1/memory/pending*). `learning_settings` :
    # {learning_nudge_min_tools: 8, learning_nudge_cooldown: 20}.
    learning: bool = False
    learning_nudges: bool = False
    memory_pending: bool = False
    learning_settings: dict[str, Any] | None = None
    # Mémoire longue multi-locataire (docs/MEMORY-TENANCY.md, nokker/memory/tenant_long.py, migration 049) : mémoire de
    # profil par compte et mémoire commune du foyer, sous RLS, plongements CortHeXis (pgvector de la base du noyau,
    # service d'embed `CORTHEXIS_EMBED_URLS`), rappelées dans le `[Rappel mémoire]` des testeurs. Éteint par défaut :
    # aucune route /v1/memory/long*, rien de rappelé de plus. `tenant_long_memory_settings` : {user_k: 3,
    # household_k: 2, always_values_max: 8, always_values_mode: marked|names|off, values_guard_mode: on|off, min_score: 0.35, relative_floor: 0.6, lexical_min_score: 0.3, vector_weight: 0.7,
    # max_candidates: 500, tools: false}.
    tenant_long_memory: bool = False
    tenant_long_memory_settings: dict[str, Any] | None = None
    # Capture AUTOMATIQUE des valeurs durables (nokker/memory/values.py) : à la clôture d'un épisode et au chargement d'un
    # historique importé, une extraction (palier `tier`, défaut ship) range décisions, préférences et valeurs (clé, valeur,
    # date, tour source) dans la mémoire longue du compte ; une valeur remplacée est archivée, jamais effacée. Demande
    # `tenant_long_memory` et le never clear (`context_enabled`). `memory_capture_settings` : {tier: ship, max_chars:
    # 24000, max_tokens: 900, seed: 7 (température 0 + graine fixe), bootstrap_max: 0 (tous les épisodes importés, par lots), background: true}.
    memory_capture: bool = True
    memory_capture_settings: dict[str, Any] | None = None
    # Auto-amélioration hors ligne sous contrôle humain (docs/SELF-IMPROVE.md, nokker/improve/). Éteint par défaut :
    # `nokker improve` refuse de tourner, aucune variante n'est lue au démarrage, aucun geste de carte n'est branché ;
    # ce que voit le modèle est identique octet pour octet. Allumé, seule une variante APPROUVÉE sur carte (versionnée,
    # réversible) change un texte ou un seuil. `self_improve_settings` : {state_dir, budget_eur (2.0), max_candidates
    # (12), price_in_eur_per_mtok (0.5), price_out_eur_per_mtok (1.5), block_freeze (true), format_ack: [sha256…]}.
    self_improve: bool = False
    self_improve_settings: dict[str, Any] | None = None
    # Consolidation nocturne de la mémoire (docs/CONSOLIDATION.md, nokker/memory/consolidation/, migration 053) : chaque
    # nuit, à l'heure locale du compte, fusion des doublons, corrections, archivage du périmé, résumé des épisodes
    # anciens, leçons tirées des échecs ; appliquée directement, journalisée avec son inverse, défaisable, rapportée le
    # matin. Demande `tenant_long_memory`. Éteint par défaut : aucune route /v1/memory/consolidation*, aucun passage,
    # aucun texte de plus. `memory_consolidation_settings` : {max_ops: 40, tier: flash, cron: "30 3 * * *",
    # default_tz: Europe/Zurich, …} (engine.py::Settings).
    memory_consolidation: bool = False
    memory_consolidation_settings: dict[str, Any] | None = None
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
    # Base publique de l'API pour les liens de conversation partagés (`POST /v1/threads/{id}/shares` → `url`) : vide =
    # chemin relatif `/v1/public/shares/<jeton>.html`, que le client préfixe par l'hôte de l'API.
    share_base_url: str = ""
    # Canaux Slack et WhatsApp (docs/CHANNELS.md, nokker/channels/) : None ou `enabled: false` (défaut) = aucune route
    # /v1/slack/*, /v1/whatsapp/*, aucun appel au fournisseur. Secrets en RÉFÉRENCES `secret:NOM` seulement (une valeur
    # en clair refuse le canal). slack : {enabled, team_id, app_id, bot_user_id, bot_token, signing_secret,
    # channel_mentions, rate_per_minute, rate_burst, lang, voice: {enabled, model, max_bytes}, secrets, state_dir}.
    # whatsapp : {enabled, phone_number_id, display_number, graph_version, access_token, app_secret, verify_token,
    # card_template: {name, lang}, max_age_s, rate_per_minute, rate_burst, lang, voice, secrets, state_dir}.
    slack: dict[str, Any] | None = None
    whatsapp: dict[str, Any] | None = None
    admin_enabled: bool = False
    admin_host: str = "127.0.0.1"
    admin_port: int = 8744
    admin_access_team: str = ""  # organisation Cloudflare Access (<équipe>.cloudflareaccess.com) ; requise si admin_enabled
    admin_access_aud_env: str = "NOKKER_ADMIN_ACCESS_AUD"
    admin_operators: dict[str, str] = field(default_factory=dict)
    admin_console_url: str = "https://admin.nokker.ai"
    # Module candidatures / invitations de la console. Faux (bêta OVH) : aucun écran, aucun tirage, aucun appel vers
    # intake/front1 (même si `beta_pull_url` est posé) ; la console renvoie vers la console suisse. Les candidatures ne
    # quittent pas la Suisse (scope §16).
    admin_candidatures: bool = True
    admin_candidatures_console: str = "admin.nokker.ai"  # console qui porte les candidatures (affichée si désactivé)
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

    # ─────────────────────── Abonnements Stripe (nokker/billing) ───────────────────────
    # Éteint par défaut : aucune route /v1/billing/*, aucun plafond de palier. Une clé `sk_live_…` est REFUSÉE tant que
    # `billing_live` n'est pas posé (décision produit, pas de la configuration par défaut).
    billing_enabled: bool = False
    billing_live: bool = False
    billing_secret_key_env: str = "NOKKER_STRIPE_SECRET_KEY"
    billing_webhook_secret_env: str = "NOKKER_STRIPE_WEBHOOK_SECRET"
    billing_prices: dict[str, str] = field(default_factory=dict)  # {palier: price_id Stripe}
    billing_plans: dict[str, dict[str, int]] = field(default_factory=dict)  # plafonds qui remplacent DEFAULT_PLANS
    # Coupon bêta PAR COMPTE (C16, 15.11 ; nokker/billing/beta_coupon.py) : id du coupon Stripe « Solo offert 6 mois »
    # (100 %, `duration: repeating` 6), posé SEULEMENT sur le Checkout Solo d'un compte rendu éligible par l'opérateur à
    # l'invitation, une seule fois. Vide = aucune remise. Pas un secret. Remplace `billing_coupon` (global, retiré).
    billing_beta_coupon: str = ""
    # Configuration du portail client Stripe (bpc_…) propre à Nokker : sans elle, le portail prend celle par défaut du
    # compte, partagée avec un autre produit du même compte Stripe (autre titre, autres CGU). Pas un secret.
    billing_portal_configuration: str = ""
    # Vouchers d'essai d'un mois (nokker/billing/voucher.py, migration 067) : éteint par défaut (tout code est refusé).
    # Allumés, ils exigent le sel des empreintes dans l'environnement (`vouchers_salt_env`, 16 caractères au moins ; le
    # changer rouvre l'essai à tout le monde). `voucher_grants_invite` : un code valide sert aussi d'invitation (décision
    # attendue, éteint).
    vouchers_enabled: bool = False
    vouchers_salt_env: str = "NOKKER_VOUCHER_SALT"
    voucher_grants_invite: bool = False
    # ─────────────────────── BYOK cloud (nokker/byok, docs/CLOUD-BYOK.md) ───────────────────────
    # Un compte de nokker.ai branche sa propre inférence compatible OpenAI au lieu du Router, si sa formule l'inclut.
    # Éteint par défaut (None ou `enabled: false`) : aucune route /v1/account/models*, aucun tour ne change de modèle.
    # {enabled, plans: [byok, solo, famille, large] (formules qui l'incluent), open_to_all (dev/banc : tout compte y a droit ; défaut
    # faux), byok_only_plans: [byok] (formules SANS inférence Nokker : jamais le Router), secrets: {provider:
    # file|openbao, file: {...}, openbao: {...}} (clés, portée system), state_dir, timeout_s}
    cloud_byok: dict[str, Any] | None = None
    # Connecteurs OAuth (docs/CONNECTORS-OAUTH.md) : {microsoft: {client_id, client_secret: secret:NOM, …}, google: {…,
    # picker: {developer_key, app_id}}} sur les défauts de nokker/connectors/oauth.py. `secret:NOM` = variable
    # d'environnement NOM du pod (Secret Kubernetes). Éteint sans client_id, sans secret lisible, ou sans
    # NOKKER_OAUTH_STATE_KEY (signature du `state`).
    oauth_providers: dict[str, Any] | None = None
    # Taux USD → CHF du coût affiché (`cost_chf` de `turn.cost` et de `GET /v1/local/cost`, docs/LOKKAL.md §7.8) :
    # 1 USD = `billing_usd_chf` CHF. Défaut 0,80 (l'inverse du 1 CHF = 1,25 USD du catalogue Router). Lu dans la config
    # seulement, jamais sur le réseau ; indépendant de `billing_enabled`.
    billing_usd_chf: float = 0.80

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

    def vision_mode_for_model(self, model: str) -> str:
        """« true » | « auto » | « false » pour un modèle choisi à l'onboarding : la vision déclarée d'un palier de la
        config qui sert le même modèle, sinon `local.vision` (le réglage des paliers servis par ce modèle), sinon non."""
        if self.vision_for_model(model):
            return "true"

        def bare(m: str) -> str:
            return (m or "").rsplit("/", 1)[-1].strip().lower()

        if model and any(t.vision_auto and bare(t.model) == bare(model) for t in self.tiers.values()):
            return "auto"
        return vision_mode((self.local or {}).get("vision", False))

    def vision_probe_path(self) -> Path | None:
        """Résultats des sondes de vision : dans l'état local de la machine ; None hors Lokkal (gardés en mémoire)."""
        if self.local is None:
            return None
        return Path(self.local.get("state_dir") or "~/.config/nokker").expanduser() / "vision-probes.json"

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

    def judge_is_explicit(self) -> bool:
        return bool(self.judge_explicit or self.judge != TierConfig(base_url="https://router.sokkan.ch/v1",
                                                                     model="qwen/qwen3.8-27b"))

    def judge_consumers(self) -> list[str]:
        """Fonctions allumées qui s'appuient sur `judge_config()` (donc sur Ship si `judge:` n'est pas posé)."""
        out = [n for n in ("judge_enabled", "judge_review", "judge_advice") if getattr(self, n, False)]
        ar = self.auto_review if isinstance(self.auto_review, dict) else {}
        if ar.get("enabled") and str(ar.get("tier") or "judge") == "judge":
            out.append("auto_review")
        if (self.outcomes_enabled or self.goals_enabled) and str(self.outcomes_grader or "judge") == "judge":
            out.append("outcomes_grader")
        return out

    def delegate_tier_entries(self) -> list[tuple[str, str]]:
        """`delegate_tiers` ramené à `[(nom, description)]` (description vide si absente) ; suppose `check_delegate_tiers` passé."""
        return [(e, "") if isinstance(e, str) else (e["name"], e.get("description") or "") for e in self.delegate_tiers]

    def check_delegate_tiers(self) -> None:
        """Au démarrage : `delegate_tiers` ne porte que des noms de `tiers` existants (chaînes ou `{name, description}`), sans
        doublon, descriptions d'au plus 120 caractères sur une ligne, avec `delegate_tier: true`."""
        entries = self.delegate_tiers
        if not entries:
            return
        if not isinstance(entries, list):
            raise ValueError("delegate_tiers : liste de noms de paliers (ou d'objets {name, description}), sans doublon")  # noqa: TRY004 — erreur de config
        names: list[str] = []
        for e in entries:
            name, desc = (e, None) if isinstance(e, str) else (e.get("name"), e.get("description")) \
                if isinstance(e, dict) and set(e) <= {"name", "description"} else (None, None)
            if not isinstance(name, str) or not name:
                raise ValueError("delegate_tiers : chaque entrée est un nom ou un objet {name, description}")
            if desc is not None and (not isinstance(desc, str) or not desc.strip() or len(desc) > DELEGATE_TIER_DESC_MAX
                                     or any(c in desc for c in "\r\n\0")):
                raise ValueError(f"delegate_tiers.{name}.description : texte non vide d'une ligne, "
                                 f"{DELEGATE_TIER_DESC_MAX} caractères au plus")
            names.append(name)
        if len(set(names)) != len(names):
            raise ValueError("delegate_tiers : liste de noms de paliers (ou d'objets {name, description}), sans doublon")
        if not self.delegate_tier:
            raise ValueError("delegate_tiers : demande `delegate_tier: true` (le paramètre tier de delegate)")
        for name in names:
            if name not in self.tiers:
                raise ValueError(f"delegate_tiers : palier {name!r} absent de `tiers` "
                                 f"(noms connus : {', '.join(sorted(self.tiers))})")

    def check_judge_explicit(self) -> None:
        """Au démarrage : un juge qui retombe sur Ship est dit (WARNING, une fois par modèle) ou, avec
        `judge_require_explicit`, refusé."""
        users = self.judge_consumers()
        if not users or self.judge_is_explicit():
            return
        model = self.judge_config().model
        if self.judge_require_explicit:
            raise ValueError(f"judge_require_explicit : {', '.join(users)} allumé sans bloc `judge:` explicite "
                             f"(il retomberait sur le palier Ship, {model}) ; poser `judge:` dans la config")
        if model not in _JUDGE_FALLBACK_WARNED:
            _JUDGE_FALLBACK_WARNED.add(model)
            logging.getLogger("nokker.config").warning(
                "`judge:` n'est pas posé : %s retombe sur le palier Ship, modèle retenu %s ; poser `judge:` "
                "(ou judge_require_explicit: true pour refuser ce repli)", ", ".join(users), model)

    def go_senders(self) -> frozenset[str]:
        from nokker.qa.invariants import GoAuthorizedSenders

        extra = {*self.extra_go_senders, *self.telegram_ids}
        return frozenset(GoAuthorizedSenders().senders | {s.lower() for s in extra})


def vision_mode(raw: Any) -> str:
    """`vision` d'un palier : `true` | `false` | `auto` (booléen YAML ou chaîne). Une valeur inconnue = texte seul."""
    if raw is True or (isinstance(raw, str) and raw.strip().lower() in ("true", "yes", "on")):
        return "true"
    if isinstance(raw, str) and raw.strip().lower() == "auto":
        return "auto"
    if raw not in (False, None) and not (isinstance(raw, str) and raw.strip().lower() in ("false", "no", "off", "")):
        logging.getLogger("nokker.config").warning("vision : valeur inconnue %r, texte seul (true|false|auto)", raw)
    return "false"


SAMPLING_KEYS = ("temperature", "top_p", "top_k", "min_p", "presence_penalty", "frequency_penalty",
                 "repetition_penalty", "seed")


def sampling_of(raw: Any) -> dict[str, Any]:
    """`tiers.<nom>.sampling` → dict validé : clés connues seulement (une clé inconnue est une erreur de config, pas un
    champ qui partirait en silence à l'amont), valeurs numériques."""
    if not raw:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"sampling : dict attendu, reçu {type(raw).__name__}")
    unknown = set(raw) - set(SAMPLING_KEYS)
    if unknown:
        raise ValueError(f"sampling : clés inconnues {sorted(unknown)} (connues : {', '.join(SAMPLING_KEYS)})")
    return {k: (int(v) if k in ("top_k", "seed") else float(v)) for k, v in raw.items()}


MAX_TOKENS_CEILING = 131072


def temperature_of(raw: Any) -> float | None:
    """`tiers.<nom>.temperature` : absent = None ; un nombre de 0 à 2 (un booléen ou une chaîne est une erreur de config)."""
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not 0 <= raw <= 2:
        raise ValueError(f"temperature : nombre de 0 à 2 attendu (reçu {raw!r})")
    return float(raw)


def max_tokens_of(raw: Any) -> int | None:
    """`tiers.<nom>.max_tokens` : absent = None ; un entier de 1 à 131072."""
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or not 1 <= raw <= MAX_TOKENS_CEILING:
        raise ValueError(f"max_tokens : entier de 1 à {MAX_TOKENS_CEILING} attendu (reçu {raw!r})")
    return raw


def sampling_with_temperature(sampling: dict[str, Any], temperature: float | None) -> dict[str, Any]:
    """`sampling` du palier avec sa `temperature` ; la poser aux deux endroits avec deux valeurs est une erreur de config."""
    if temperature is None:
        return sampling
    if "temperature" in sampling and sampling["temperature"] != temperature:
        raise ValueError("temperature : posée à la fois dans `temperature` et dans `sampling` avec deux valeurs")
    return {**sampling, "temperature": temperature}


TIER_FORMATS = ("openai", "anthropic")
TIER_AUTH = ("x-api-key", "bearer")


def format_of(raw: Any) -> str:
    """`tiers.<nom>.format` validé : absent ou vide = `openai` ; une valeur inconnue est une erreur de config."""
    value = str(raw or "openai").strip().lower()
    if value not in TIER_FORMATS:
        raise ValueError(f"format : valeurs connues {', '.join(TIER_FORMATS)} (reçu {raw!r})")
    return value


TIER_CACHE_TTL = ("5m", "1h")
TIER_EFFORTS = ("low", "medium", "high", "xhigh", "max")


def effort_of(raw: Any) -> str:
    """`tiers.<nom>.effort` validé : absent ou vide = rien ; sinon un niveau connu de l'API."""
    value = str(raw or "").strip().lower()
    if value and value not in TIER_EFFORTS:
        raise ValueError(f"effort : valeurs connues {', '.join(TIER_EFFORTS)} (reçu {raw!r})")
    return value


def body_of(raw: Any, name: str) -> dict[str, Any] | None:
    """`tiers.<nom>.reasoning_on_body` validé : absent = None ; sinon un dict (JSON-sérialisable par construction YAML)."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"{name} : dict attendu, reçu {type(raw).__name__}")  # noqa: TRY004 — erreur de config
    return dict(raw)


def cache_ttl_of(raw: Any) -> str:
    """`tiers.<nom>.cache_ttl` validé (format anthropic) : absent ou vide = rien (comportement d'avant) ; sinon `5m` ou `1h`."""
    value = str(raw or "").strip().lower()
    if value and value not in TIER_CACHE_TTL:
        raise ValueError(f"cache_ttl : valeurs connues {', '.join(TIER_CACHE_TTL)} (reçu {raw!r})")
    return value


def auth_of(raw: Any) -> str:
    """`tiers.<nom>.auth` validé (format anthropic) : absent = `x-api-key`."""
    value = str(raw or "x-api-key").strip().lower()
    if value not in TIER_AUTH:
        raise ValueError(f"auth : valeurs connues {', '.join(TIER_AUTH)} (reçu {raw!r})")
    return value


CLIENT_TAG_DEFAULT = "nokker"
_CLIENT_TAG_RE = re.compile(r"[a-z0-9-]{1,32}")


def valid_client_tag(value: Any) -> bool:
    """Étiquette `[a-z0-9-]{1,32}` (chaîne entière) : celle de `route.client_tag` et de l'en-tête `X-Nokker-Client`."""
    return isinstance(value, str) and _CLIENT_TAG_RE.fullmatch(value) is not None


def client_tag_of(route: Any) -> str:
    """`route.client_tag` : étiquette de DÉPLOIEMENT (`nokker`, `lokkal`, `beta`, `canary`…) envoyée au Router en en-tête
    `X-Nokker-Client`. Absente : `nokker`. Chaîne entière en `[a-z0-9-]{1,32}`, sinon erreur (au démarrage) : jamais un
    identifiant de personne, de locataire ou de fil."""
    raw = route.get("client_tag") if isinstance(route, dict) else None
    if raw is None:
        return CLIENT_TAG_DEFAULT
    if not valid_client_tag(raw):
        raise ValueError(f"route.client_tag : étiquette de déploiement [a-z0-9-]{{1,32}} attendue (reçu {raw!r})")
    return raw


def flag_of(raw: Any, name: str) -> bool:
    """Booléen de config : absent = faux ; autre chose qu'un vrai booléen (« false » entre guillemets) est une erreur."""
    if raw is None or raw is False:
        return False
    if raw is True:
        return True
    raise ValueError(f"{name} : booléen attendu (true ou false), reçu {raw!r}")


def gate_of(raw: Any) -> bool:
    """`tiers.<nom>.gate` : absent = faux ; seul un booléen est accepté (une chaîne « false » serait vraie)."""
    if raw is None:
        return False
    if not isinstance(raw, bool):
        raise ValueError(f"gate : true ou false attendu (reçu {raw!r})")  # noqa: TRY004 — erreur de config, pas de type
    return raw


TIMEOUT_MIN_S, TIMEOUT_MAX_S = 1.0, 3600.0


def seconds_of(raw: Any, default: float, name: str) -> float:
    """`tiers.<nom>.timeout_s` / `total_budget_s` validé : absent = défaut ; sinon un nombre fini entre 1 s et 1 h
    (zéro, négatif, texte ou valeur absurde = erreur de config, pas un palier qui bloque ou qui échoue à chaque appel)."""
    if raw is None or raw == "":
        return default
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
        raise ValueError(f"{name} : nombre de secondes attendu (reçu {raw!r})")
    if not TIMEOUT_MIN_S <= raw <= TIMEOUT_MAX_S:
        raise ValueError(f"{name} : entre {TIMEOUT_MIN_S:g} et {TIMEOUT_MAX_S:g} secondes (reçu {raw!r})")
    return float(raw)


def load_config(path: str | None = None) -> NokkerConfig:
    path = path or DEFAULT_CONFIG_PATH
    p = Path(path)
    if not p.exists():
        return NokkerConfig()
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    tiers = {
        name: TierConfig(base_url=t["base_url"], model=t["model"], api_key_env=t.get("api_key_env", "ROUTER_KEY"),
                         reasoning_off_body=t.get("reasoning_off_body"),
                         reasoning_on_body=body_of(t.get("reasoning_on_body"), "reasoning_on_body"),
                         effort=effort_of(t.get("effort")),
                         fallback_model=t.get("fallback_model", ""), hedge_after_s=float(t.get("hedge_after_s", 8.0)),
                         cache=str(t.get("cache", "none")), vision=vision_mode(t.get("vision")) == "true",
                         vision_auto=vision_mode(t.get("vision")) == "auto",
                         max_context_tokens=int(t.get("max_context_tokens", 0) or 0),
                         sampling=sampling_with_temperature(sampling_of(t.get("sampling")), temperature_of(t.get("temperature"))),
                         temperature=temperature_of(t.get("temperature")), max_tokens=max_tokens_of(t.get("max_tokens")),
                         format=format_of(t.get("format")),
                         anthropic_version=str(t.get("anthropic_version") or ""), auth=auth_of(t.get("auth")),
                         cache_ttl=cache_ttl_of(t.get("cache_ttl")),
                         binding_controls=flag_of(t.get("binding_controls"), "binding_controls"),
                         timeout_s=seconds_of(t.get("timeout_s"), 180.0, "timeout_s"),
                         total_budget_s=seconds_of(t.get("total_budget_s"), 80.0, "total_budget_s"),
                         gate=gate_of(t.get("gate")),
                         router=None if t.get("router") is None else flag_of(t.get("router"), "router"),
                         send_call_id=flag_of(t.get("send_call_id"), "send_call_id"),
                         action_claims_guard=None if t.get("action_claims_guard") is None
                         else flag_of(t.get("action_claims_guard"), "action_claims_guard"))
        for name, t in (raw.get("tiers") or {}).items()
    } or None
    # `api_keys: []` explicite = AUCUNE clé (bêta OVH), pas une clé par défaut ; absente = défaut.
    api_keys = [ApiKeyIdentity(token_env=k["token_env"], sender=k["sender"]) for k in raw.get("api_keys") or []] \
        if "api_keys" in raw else None
    judge_raw = raw.get("judge")
    judge = TierConfig(base_url=judge_raw["base_url"], model=judge_raw["model"],
                       api_key_env=judge_raw.get("api_key_env", "ROUTER_KEY"),
                       reasoning_off_body=judge_raw.get("reasoning_off_body"),
                       fallback_model=judge_raw.get("fallback_model", ""),
                       hedge_after_s=float(judge_raw.get("hedge_after_s", 8.0))) if judge_raw else None
    kwargs: dict[str, Any] = {k: v for k, v in raw.items() if k not in ("tiers", "api_keys", "judge")}
    if tiers:
        kwargs["tiers"] = tiers
    if api_keys is not None:
        kwargs["api_keys"] = api_keys
    if judge:
        kwargs["judge"] = judge
        kwargs["judge_explicit"] = True
    cfg = NokkerConfig(**kwargs)
    cfg.check_delegate_tiers()
    from nokker.providers.sokkan import set_client_tag

    set_client_tag(client_tag_of(cfg.route))  # invalide : ValueError au chargement (démarrage)
    return cfg


__all__ = ["ApiKeyIdentity", "DEFAULT_CONFIG_PATH", "NokkerConfig", "PRODUCT_PREFIX_PATH", "TierConfig", "load_config"]
