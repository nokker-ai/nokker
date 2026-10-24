"""Types du noyau Nokker — aucun import de Hermes, aucun import d'un autre paquet nokker.

Tout ce qui traverse une frontière de pilier (contexte, juge, multi-modèle) est décrit ici.
Les objets sont des dataclasses simples, sérialisables en JSON (store Postgres, ledger, API).
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


class Tier(str, enum.Enum):
    """Paliers de modèle (PLAN-P1 §2.6). Jamais de palier « frontier » : décision Nick du 16.10."""

    LOCAL = "local"  # Magnitude : classe de tâche, juge, résumés
    SHIP = "ship"  # route par défaut (-eu ; -swiss si le fil exige la zone CH)
    DEEP = "deep"  # 397B OVH en débordement transitoire (D11, 17.10), 122B dès qu'il est servi


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


@dataclass
class Message:
    """Message au format OpenAI chat. `content` est déjà caviardé (invariants d'entrée)."""

    role: str  # system | user | assistant | tool
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None

    def to_openai(self) -> dict[str, Any]:
        d: dict[str, Any] = {"role": self.role, "content": self.content}
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


@dataclass
class Usage:
    prompt: int = 0
    completion: int = 0
    cached: int = 0
    reasoning: int = 0
    cached_reported: bool = False  # False = l'amont n'a rien déclaré : ne pas en déduire 0 % de cache (A5)


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


@dataclass
class InputCheck:
    """Résultat des invariants d'entrée, AVANT tout enregistrement (PLAN §2.3 étape 1)."""

    text: str  # caviardé
    redacted: list[str] = field(default_factory=list)  # noms des secrets détectés (jamais les valeurs)
    domain_flags: set[str] = field(default_factory=set)  # fiscal, juridique, montant, …


@dataclass
class TurnInput:
    thread_id: str
    user_text: str
    channel: str = "api"  # telegram | slack | webchat | api
    sender: str | None = None
    attachments: list[dict[str, Any]] = field(default_factory=list)


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
    # précédent octet pour octet (cache de préfixe entre tours, option A de Nick, 20.10.2026). Vide = tour d'avant
    # ce changement, rejoué comme avant (injected + texte).
    head: str = ""
