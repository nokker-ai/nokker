"""Types du noyau Nokker — aucun import de la bibliothèque amont, aucun import d'un autre paquet nokker.

Tout ce qui traverse une frontière de pilier (contexte, juge, multi-modèle) est décrit ici.
Les objets sont des dataclasses simples, sérialisables en JSON (store Postgres, ledger, API).
"""
from __future__ import annotations

import enum
import secrets
from dataclasses import dataclass, field
from typing import Any


class Tier(str, enum.Enum):
    """Paliers de modèle (PLAN-P1 §2.6). Jamais de palier « frontier » : décision produit du 16.10."""

    LOCAL = "local"  # Magnitude : classe de tâche, juge, résumés
    SMALL = "small"  # petit palier (X8, docs/LOKKAL.md §7.17) : le trivial (politesses), par le Router ou un modèle local
    SHIP = "ship"  # route par défaut (-eu ; -swiss si le fil exige la zone CH)
    DEEP = "deep"  # 397B OVH en débordement transitoire (D11, 17.10), 122B dès qu'il est servi
    NINA = "nina"  # modèle maison, À PART (docs/LOKKAL.md §7.17 point 13) : éteint par défaut, ombre puis types de tour


class Effect(str, enum.Enum):
    """Effet déclaré d'un outil (tools/effects.py). Un outil sans effet déclaré est traité comme WRITE."""

    READ = "read"
    WRITE = "write"
    SEND_EXTERNAL = "send_external"
    SEND_BULK = "send_bulk"
    DESTRUCTIVE = "destructive"
    SPEND = "spend"
    SECRET = "secret"  # le résultat contient un secret : il entre dans le contexte en poignée [secret:NOM]


class Gate(str, enum.Enum):
    ACTION = "action"  # avant chaque appel d'outil
    SWITCH = "switch"  # « bascule » : avant d'envoyer la réponse
    DELIVERABLE = "deliverable"  # fin de tâche longue (Crew, P2)


class Decision(str, enum.Enum):
    """Issue d'une porte. L'ordre compte : la plus sévère l'emporte quand on combine."""

    ALLOW = "allow"
    REDO_DEEP = "redo_deep"  # relancer le même appel en Deep, même préfixe (porte bascule seulement)
    HITL = "hitl"  # carte d'approbation humaine ; l'action attend
    ESCALATE = "escalate"  # le noyau escalade lui-même et remplace la réponse
    DENY = "deny"  # refus net (ex. send_bulk)

    @property
    def severity(self) -> int:
        return _SEVERITY[self]


_SEVERITY = {Decision.ALLOW: 0, Decision.REDO_DEEP: 1, Decision.HITL: 2, Decision.ESCALATE: 3, Decision.DENY: 4}


THINKING_KEY = "thinking_blocks"  # clé du message filaire qui porte les blocs de réflexion rejoués (Message.thinking_blocks)


def strip_thinking(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Messages sans la clé des blocs de réflexion rejoués : tout fournisseur qui ne sait pas les rejouer ne les voit
    jamais. Même liste (mêmes objets) quand il n'y en a pas ; les messages d'origine ne sont pas modifiés."""
    if not any(isinstance(m, dict) and THINKING_KEY in m for m in messages):
        return messages
    return [{k: v for k, v in m.items() if k != THINKING_KEY} if isinstance(m, dict) and THINKING_KEY in m else m
            for m in messages]


@dataclass
class Message:
    """Message au format OpenAI chat. `content` est déjà caviardé (invariants d'entrée)."""

    role: str  # system | user | assistant | tool
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None
    # Blocs `thinking` / `redacted_thinking` rendus par un fournisseur au format Anthropic AVEC leur signature, à
    # renvoyer tels quels devant les `tool_use` du même message au tour d'outils suivant (l'API les exige, réflexion
    # active). Jamais affichés à la personne, jamais envoyés à un autre fournisseur (`strip_thinking`), absents de
    # `to_openai()` sans `thinking=True`. Vide (défaut) = rien ne change, y compris dans le stockage JSON.
    thinking_blocks: list[dict[str, Any]] = field(default_factory=list)

    def to_openai(self, thinking: bool = False) -> dict[str, Any]:
        d: dict[str, Any] = {"role": self.role, "content": self.content}
        if thinking and self.thinking_blocks:
            d[THINKING_KEY] = [dict(b) for b in self.thinking_blocks]
        if self.tool_calls:
            d["tool_calls"] = [tc.to_openai() for tc in self.tool_calls]
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        if self.name and self.role == "tool":
            d["name"] = self.name
        return d


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str = ""  # tel que renvoyé par le modèle, pour le journal si le JSON est invalide

    def to_openai(self) -> dict[str, Any]:
        import json

        return {"id": self.id, "type": "function",
                "function": {"name": self.name, "arguments": self.raw_arguments or json.dumps(self.arguments)}}


@dataclass
class ToolResult:
    call_id: str
    name: str
    content: str  # ce qui ENTRE dans le contexte (poignées de secrets, résumé si réservoir)
    is_error: bool = False
    reservoir_id: str | None = None  # résultat long rangé hors fenêtre (pilier a)
    secret_handles: list[str] = field(default_factory=list)
    # Motif stable d'un refus, pour les clients seulement (`Verdict.code` : `plan_mode`…) : `tool.result` et réponse de
    # `tools/call` le portent en `reason`. Jamais dans `content` (le texte vu par le modèle ne change pas).
    reason: str | None = None
    # Refus d'une porte (mode plan, `deny_tools`, règle deny, valeur masquée…), pour les clients seulement : `tool.result`
    # porte alors `status: "refused"` et `reason`, réponse de `tools/call` aussi. Sans effet sur ce que lit le modèle :
    # le refus de la boucle garde `is_error=False` (un refus n'est pas un échec d'outil : il ne compte ni dans
    # `RouteState.tool_failures`, qui monte le palier, ni dans `repeated_error`).
    refused: bool = False
    # Motif donné par la personne avec le refus d'une carte (`approvals/reasons.py`), pour les clients seulement : `tool.result`
    # le porte en `decision_reason` (le champ `reason` vaut déjà « declined »). Le modèle le lit dans `content`, encadré.
    decision_reason: str | None = None
    # MCP Apps (drapeau `mcp_apps`, docs/MCP-NEXT.md) : l'interface `ui://` renvoyée par l'outil, pour le CLIENT seulement
    # (événement `tool.app`). Jamais dans `content`, jamais persistée avec le tour, jamais vue par le modèle.
    app: dict[str, Any] | None = field(default=None, repr=False, compare=False)
    # Données structurées d'un outil pour le NOYAU et les CLIENTS (bulle de recherche, provenance) : jamais dans
    # `content`, jamais vues par le modèle, jamais persistées avec le tour (r44 : bloc data de web_search/web_fetch).
    data: dict[str, Any] | None = field(default=None, repr=False, compare=False)
    # Images rendues par un outil (r44 : read_file d'une image), octets contrôlés (`nokker.images.validate.RawImage`) :
    # jamais dans `content` (qui n'en porte qu'une ligne « image <mime>, <n> octets, <l>×<h> ») ; le noyau les range au
    # magasin d'images et les joint au tour, comme les pièces jointes : partie image sur un palier avec vision.
    images: list[Any] = field(default_factory=list, repr=False, compare=False)
    # Code de sortie d'une commande (outil `terminal`), propagé tel quel par l'outil : jamais deviné dans `content`.
    # None = l'outil n'a pas de code de sortie. Un code ≠ 0 n'est pas `is_error` ; le bloc `[Approbation]` l'annonce.
    exit_code: int | None = None
    # Note du garde-fou contre les boucles d'outils (`core/tool_loop_guard.py`) : ajoutée au message `tool` envoyé au modèle, jamais à
    # `content` (le mémo du tour, les événements et la détection de répétition lisent le résultat sans elle).
    loop_note: str = ""


CALL_ID_PREFIX = "nc_"


def new_call_id() -> str:
    """Identifiant d'UN appel modèle (`nc_` + 16 hexadécimaux), posé par le noyau : inscrit au ledger, et envoyé en
    en-tête `X-Nokker-Call-Id` seulement aux paliers qui le demandent (`tiers.<nom>.send_call_id`). Aléatoire : il ne
    dit rien d'une personne, d'un fil ni d'un tour."""
    return CALL_ID_PREFIX + secrets.token_hex(8)


@dataclass
class Usage:
    prompt: int = 0
    completion: int = 0
    cached: int = 0
    reasoning: int = 0
    cached_reported: bool = False  # False = l'amont n'a rien déclaré : ne pas en déduire 0 % de cache (A5)
    reasoning_reported: bool = False  # même règle pour le raisonnement (X2) : absent ≠ zéro
    # Jetons du prompt ÉCRITS en cache par cet appel (amont au format Anthropic, `cache_creation_input_tokens`) :
    # déjà comptés dans `prompt`, jamais dans `cached` (qui reste « lus en cache »). 0 partout ailleurs.
    cache_write: int = 0
    # Durée du cache des jetons écrits : « 5m » (défaut de l'amont) ou « 1h ». Choisit le prix d'écriture du ledger
    # (`cache_write` ou `cache_write_1h`). Aucun provider ne la pose encore : « 5m » partout.
    cache_ttl: str = "5m"


@dataclass
class LLMCall:
    """Un appel modèle, tel qu'il part au ledger (T5)."""

    tier: Tier
    model: str
    reasoning: bool
    prefix_sha256: str
    layer_tokens: dict[str, int]  # tokens par couche du Context Engine (préfixe, profil, épisodes, courant, bloc)
    usage: Usage
    latency_s: float
    route_reason: str  # motif de la décision de palier, toujours renseigné (C2)
    chf: float | None = None
    # Tokens du prompt identiques EN TÊTE à l'appel précédent du même fil : ce qu'un cache de préfixe POURRAIT
    # servir, mesuré côté noyau, indépendamment de l'amont (OVH ne cache pas : `cached` y vaut toujours 0).
    # None = premier appel connu du fil depuis le démarrage.
    reusable_tokens: int | None = None
    # Identifiant de l'appel (`new_call_id`) : celui du provider quand l'appel est parti par lui (le même que l'en-tête
    # `X-Nokker-Call-Id`), sinon généré ici. Un appel = un identifiant ; un repli sur un autre palier est un autre appel.
    call_id: str = field(default_factory=new_call_id)
    # Modèle RENVOYÉ par l'amont (`Completion.model_served`) : le champ `model` de la réponse, tel quel, ou None (colonne
    # `model_served`). `model` ci-dessus reste ce que les providers rendent déjà (réponse, sinon modèle du palier) : il
    # sert au prix, il ne dit pas si l'amont a répondu.
    model_served: str | None = None
    # Id de l'amont (`Completion.upstream`, en-tête `X-Sokkan-Upstream`) : traces et événement, pas de colonne du ledger.
    upstream: str | None = None


@dataclass
class Verdict:
    """Décision d'une porte : couche 1 (invariant, déterministe) ou couche 2 (juge)."""

    gate: Gate
    decision: Decision
    layer: int  # 1 = invariant, 2 = juge
    rule: str  # id de l'invariant ou de la question posée
    reason: str = ""
    probability: float | None = None  # None si le backend n'a pas de logprobs (D13)
    judge_model: str | None = None
    latency_s: float = 0.0
    subject: str | None = None  # id de l'appel d'outil ou « reply »
    # Niveau de preuve requis si ce verdict débouche sur une approbation forte (docs/APPROVALS.md §1) :
    # "strong" (DESTRUCTIVE), "standard" (SEND_EXTERNAL hors allowlist, SPEND), None pour tout le reste
    # (y compris SEND_BULK, qui reste DENY — jamais soumis à approbation). Posé par
    # `qa/invariants.py::DeterministicInvariants.check_action`, lu par `Kernel` (core/loop.py) pour
    # savoir quel niveau de preuve exiger via le port `Approvals` (core/ports.py).
    strength: str | None = None
    # Code stable pour les clients (`plan_mode`, `deny_rule`, `hardline`, `no_code_session` ; None ailleurs) : porté par
    # le `tool.result` d'un refus (docs/LOKKAL.md §7.4.2). Jamais dans le texte vu par le modèle (qui garde rule/reason).
    # Un HITL des permissions locales (qa/permissions.py) en porte aussi un (`accept_edits_write`, `ask_rule`…) : il
    # devient le `reason_code` de la carte (`Approval.reason_code`).
    code: str | None = None


# `TurnInput.control` du tour de suite d'une carte décidée (approvals/executor.py) : posé par le noyau SEULEMENT (un
# client ne pose que `goals.service.CONTROLS`). Le bloc `[Approbation]` est écrit par le noyau et redit une décision
# déjà prise sur la carte : il ne lève aucun drapeau de domaine (un montant du résultat de `pay_request`, par exemple).
APPROVAL_CONTINUE = "approval_continue"
# Tour du GESTE « ouvrir par mon téléphone » (P11, nokker/cloud/egress.py) : écrit par le noyau au nom de la personne,
# jamais accepté d'un client (les `control` d'un client passent par une liste fermée).
PHONE_FETCH = "phone_fetch"
# Tour de SUITE d'une tâche du téléphone close après les 20 s du tour (P7b, nokker/cloud/phone_tools.py) : écrit par le
# noyau, jamais accepté d'un client ; le résultat du téléphone est joint, clôturé comme donnée non fiable.
PHONE_CONTINUE = "phone_continue"
# Tour de SUITE d'un pas du bureau validé (ou non) par la personne après les 20 s du tour (docs/DESKTOP.md §12.2,
# nokker/desktop/steps.py) : écrit par le noyau, jamais accepté d'un client ; le résultat du nœud est joint, clôturé.
DESKTOP_CONTINUE = "desktop_continue"


@dataclass
class InputCheck:
    """Résultat des invariants d'entrée, AVANT tout enregistrement (PLAN §2.3 étape 1)."""

    text: str  # caviardé
    redacted: list[str] = field(default_factory=list)  # noms des secrets détectés (jamais les valeurs)
    domain_flags: set[str] = field(default_factory=set)  # fiscal, juridique, montant, …
    # « approval » : le message est le bloc `[Approbation]` ÉCRIT PAR LE NOYAU pour la suite d'une carte décidée
    # (`APPROVAL_CONTINUE`), pas un texte de la personne. Vide = message de la personne. Exposé aux clients
    # (`GET /v1/threads/{id}`, champ `origin`) pour ne pas l'afficher comme une bulle de la personne.
    origin: str = ""


@dataclass
class ImageRef:
    """Image jointe à un message (lot X7, docs/LOKKAL.md §7.6) : la RÉFÉRENCE seule, les octets vivent dans le magasin
    d'images (`nokker/images/store.py`, par sha256, dédupliqués par locataire). `media_type` est celui lu dans les
    octets, jamais celui déclaré par le client."""

    sha256: str
    media_type: str  # image/png | image/jpeg | image/webp | image/gif
    size: int  # octets
    name: str = ""  # nom de fichier d'origine (affichage seulement), caviardé
    # Qui a joint l'image (r44) : "" = la personne ; « tool:<outil> » = rendue par un outil (read_file d'une image).
    # Pour les CLIENTS (export, miroir, partage, historique) seulement : jamais présentée comme envoyée par la personne.
    # Le modèle n'en voit rien (le Context Engine ne lit que sha256, type, taille, nom).
    origin: str = ""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"sha256": self.sha256, "media_type": self.media_type, "size": self.size, "name": self.name}
        if self.origin:
            d["origin"] = self.origin
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ImageRef:
        return cls(sha256=str(d["sha256"]), media_type=str(d.get("media_type", "")), size=int(d.get("size", 0)),
                   name=str(d.get("name", "")), origin=str(d.get("origin") or ""))

    @property
    def from_tool(self) -> bool:
        return self.origin.startswith("tool:")


# Canal d'un tour arrivé par le relais `<nom>.nokker.app` (jeton de passerelle signé par nokker.ai, en-tête
# `X-Nokker-Relay`). Même identité que le tour local (`local-owner`), mais les portes savent qu'il vient du WEB :
# aucun GO textuel ne lève une carte forte (X10, docs/LOKKAL.md §7.3).
RELAY_CHANNEL = "relay"


@dataclass
class TurnInput:
    thread_id: str
    user_text: str
    channel: str = "api"  # telegram | slack | webchat | api | relay
    sender: str | None = None
    attachments: list[dict[str, Any]] = field(default_factory=list)
    # Images déjà validées et rangées au magasin (X7) : le noyau ne reçoit jamais d'octets dans le tour.
    images: list[ImageRef] = field(default_factory=list)
    # Bloc `[Pièces jointes]` (texte, dossier, url ; `nokker/attachments.py`) déjà caviardé et borné par la frontière ;
    # vide = aucune (drapeau `text_attachments_enabled` éteint, ou message sans pièce texte).
    attached: str = ""
    # Pièces texte faites sur l'appareil (P9 : OCR, transcription, dictée ; `attachments.parse_device`), déjà
    # caviardées : rangées avec le message ({id, kind, source, name, chars, text, lang?, pages?, duration_s?}).
    texts: list[dict[str, Any]] = field(default_factory=list)
    # Étiquette d'un tour envoyé par un client lui-même (`metadata.cli_control` de /v1/chat/completions : btw,
    # goal_check, goal_continue, loop ; nokker-apps apps/cli/src/prompts.ts), ou `goal_continue` pour un tour enchaîné
    # par le noyau (docs/OUTCOMES-GOAL.md). Vide = tour tapé. Lue seulement par les objectifs (`Kernel.goals`).
    control: str = ""
    # Langue des textes fixes vus de la personne pour ce tour (fr/en/de/it), posée à chaque point d'entrée : langue de la
    # requête (HTTP), `language_code` (Telegram), langue résolue à la soumission (objectifs, agents, canaux). None : le
    # noyau la résout (requête en cours, sinon compte, sinon dernière langue reconnue du fil, sinon fr).
    lang: str | None = None
    # Vrai quand `lang` est la langue de la personne ELLE-MÊME (son message : requête, `language_code`) et non un repli :
    # seule celle-là est mémorisée sur le fil (`Turn.lang_seen`).
    lang_recognized: bool = False
    # Vrai quand `lang` repose sur un VRAI SIGNAL (requête, compte, message détecté, langue du fil, `NOKKER_LANG`) et non sur
    # le défaut du code (`fr` sans aucun signal) : la ligne de langue de l'en-tête n'est rendue que dans ce cas.
    lang_signal: bool = False
    # Surface d'où le client écrit (`X-Nokker-Surface` : cli, acp, vscode, web, pwa), déjà validée sur la liste fermée
    # (`self_context.normalize_surface`) ; None = inconnue. Ne sert qu'à la ligne « Canal » du bloc `[Environnement]`.
    surface: str | None = None


@dataclass
class Turn:
    """Un tour complet : une entrée humaine → une réponse. Append-only en base."""

    thread_id: str
    index: int
    input: InputCheck
    channel: str = "api"
    sender: str | None = None  # identité de l'humain du tour : un GO ne vaut que venant d'un expéditeur autorisé
    reply: str = ""
    messages: list[Message] = field(default_factory=list)  # messages produits pendant le tour (assistant/tool)
    calls: list[LLMCall] = field(default_factory=list)
    verdicts: list[Verdict] = field(default_factory=list)
    escalated: bool = False
    pending_approvals: list[ToolCall] = field(default_factory=list)
    error: str | None = None
    # Blocs de contexte reçus d'ailleurs (bulle transférée, message d'une autre session — docs/BUBBLES.md
    # §3-§4), vidés de la boîte du fil au début du tour ; gardés ici pour que l'historique les rejoue.
    injected: list[str] = field(default_factory=list)
    # En-tête du message du tour tel qu'envoyé au modèle (date, épisodes pertinents, rappel, contexte reçu), figé au
    # premier assemblage du tour et REJOUÉ TEL QUEL par l'historique : le prompt du tour suivant reprend alors le
    # précédent octet pour octet (cache de préfixe entre tours, option A, décision produit du 20.10.2026). Vide = tour d'avant
    # ce changement, rejoué comme avant (injected + texte).
    head: str = ""
    # Réflexion de la dernière réponse sans appel d'outil, en attente d'être rangée sur le message final (`_seal_thinking`).
    # Jamais persistée ni vue par le modèle.
    final_thinking: list[dict[str, Any]] = field(default_factory=list, repr=False, compare=False)
    # Images jointes au message humain (X7) : références seulement, persistées avec le tour (append-only) ; les octets
    # restent au magasin d'images et partent avec l'effacement du fil ou du compte.
    images: list[ImageRef] = field(default_factory=list)
    # Bloc `[Pièces jointes]` du message (TurnInput.attached) : entre dans l'en-tête figé (`head`), rejoué avec lui.
    # Non persisté à part (`head` le porte) : un en-tête délesté par le never clear emporte les pièces jointes.
    attached: str = ""
    # Pièces texte de l'appareil (TurnInput.texts), persistées avec le tour : métadonnées dans l'historique du fil
    # (`attachments`), texte relu par GET /v1/threads/{id}/attachments/{att_id}.
    texts: list[dict[str, Any]] = field(default_factory=list)
    # Pour les clients seulement (`turn.done`, docs/CLIENT-API.md §Fin de tour), jamais persistés ni vus par le modèle :
    # échec du tour `{code, message}` (palier indisponible, erreur d'amont, exception…) et cartes posées pendant le tour.
    # `retry: False` (et `limit`) en plus pour un refus de plafond de session : l'hôte ne doit pas réessayer.
    failure: dict[str, Any] | None = None
    approval_ids: list[str] = field(default_factory=list)
    # Langue des textes fixes de CE tour (posée par le noyau, jamais persistée) ; les lecteurs d'un échec s'en servent.
    lang: str = ""
    # Vrai si `lang` vient d'un signal réel (voir `TurnInput.lang_signal`) ; non persisté, lu par la ligne de langue de l'en-tête.
    lang_signal: bool = False
    # Dernière langue reconnue sur le fil : celle du message humain de ce tour (fr/en/de/it), vide pour un tour qui n'en
    # dit rien (suite de carte, tâche de fond). PERSISTÉE avec le tour (aucune colonne : le tour est un JSON).
    lang_seen: str = ""
    # Repli de palier du tour (Deep indisponible -> Ship) : `{"from": "deep", "to": "ship"}`, pour `turn.done.fallback` seulement.
    # `notice` : ligne d'état pour la PERSONNE (`turn.done.notice`), posée seulement si le tour devait être profond ; jamais
    # dans `reply`, `messages`, l'historique ni le cache, jamais vue du modèle. Ni l'un ni l'autre n'est persisté.
    fallback: dict[str, str] | None = None
    notice: str = ""
    # Surface du client (`TurnInput.surface`), lue par le bloc `[Environnement]` ; non persistée.
    surface: str | None = None
    # Question à choix posée par `clarify` (drapeau `host_clarify_options`, nokker/tools/clarify.py) : pour les clients
    # seulement (`question.asked`, `nokker.question`), jamais persistée ni vue par le modèle (qui garde le texte de la
    # réponse). None = pas de question, ou drapeau éteint.
    question: dict[str, Any] | None = None
    # Réflexion de l'amont, `{call_index, text}` caviardé par appel modèle qui en a renvoyé (`host_reasoning_stream`) :
    # pour les clients seulement (`nokker.reasoning`, `turn.reasoning`). Jamais dans `messages` (donc jamais renvoyée
    # au modèle), jamais persistée, jamais proposée à la mémoire. Vide = aucune, ou drapeau éteint.
    reasoning: list[dict[str, Any]] = field(default_factory=list)
