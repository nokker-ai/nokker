"""Interfaces (ports) du noyau. Chaque pilier est derrière UN port ; la boucle ne connaît que ces ports.

Règle : une implémentation ne dépend que de `nokker.core.turn` et de sa propre bibliothèque.
Les briques Hermes (gateway, STT, client MCP) se branchent derrière ces ports, jamais dans la boucle.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable

from nokker.core.turn import (
    Decision, Effect, InputCheck, LLMCall, Message, Tier, ToolCall, ToolResult, Turn, TurnInput, Usage, Verdict,
)

# ─────────────────────────────── outils ───────────────────────────────


@dataclass
class ToolSpec:
    name: str
    description: str  # une ligne : c'est elle qui va dans l'index du bloc du tour
    parameters: dict[str, Any]  # JSON schema
    effect: Effect = Effect.WRITE  # défaut prudent (PLAN §2.3 étape 5)
    core: bool = False  # True = schéma dans le préfixe stable (≤ 8 outils)
    source: str = "builtin"  # builtin | mcp:<serveur>
    # Effet qui dépend des arguments (ex. `terminal` : `ls` ne détruit rien, `rm -rf` si). None = `effect` pour
    # tout appel ; sinon `effect` est le repli quand le classement échoue (donc le plus prudent des deux).
    effect_of: Callable[[dict[str, Any]], Effect] | None = field(default=None, compare=False, repr=False)

    def effect_for(self, arguments: dict[str, Any]) -> Effect:
        """Effet de CET appel : `effect_of(arguments)` si l'outil classe ses appels, sinon `effect`."""
        if self.effect_of is None:
            return self.effect
        try:
            return self.effect_of(arguments)
        except Exception:  # noqa: BLE001 — classement cassé = effet déclaré
            return self.effect

    def to_openai(self) -> dict[str, Any]:
        return {"type": "function", "function": {"name": self.name, "description": self.description,
                                                 "parameters": self.parameters}}


def call_effect(spec: Any, arguments: dict[str, Any]) -> Effect:
    """Effet d'un appel pour les portes et la boucle : sans spec, WRITE (défaut prudent) ; une spec qui ne sait pas
    classer ses appels (MCP, objets de test) donne son `effect`."""
    if spec is None:
        return Effect.WRITE
    effect_for = getattr(spec, "effect_for", None)
    return effect_for(arguments) if effect_for is not None else spec.effect


@runtime_checkable
class ToolSource(Protocol):
    """Une source d'outils (builtin, serveur MCP…). Le registre agrège les sources."""

    def specs(self) -> list[ToolSpec]: ...

    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult: ...


@dataclass
class ToolContext:
    thread_id: str
    turn_index: int
    channel: str
    sender: str | None = None


class ToolRegistry(Protocol):
    """Agrège les ToolSource. Les outils cœur sont dans le préfixe ; le reste est trouvé par `tool_find`."""

    def core(self) -> list[ToolSpec]: ...

    def spec(self, name: str) -> ToolSpec | None: ...

    def find(self, query: str, limit: int = 5) -> list[ToolSpec]: ...

    def index(self) -> list[tuple[str, str]]:
        """(nom, description d'une ligne) de tous les outils non cœur — l'index court du bloc du tour."""
        ...

    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult: ...


# ─────────────────────── pilier (a) : contexte dynamique ───────────────────────


@dataclass
class Recall:
    note: str  # nom de la note
    text: str  # extrait injecté
    score: float
    rerank: float | None = None


class MemoryPort(Protocol):
    """CortHeXis. S1 : MCP sokkan-memory en lecture ; S2-S3 : paquet corthexis 2.1 en direct."""

    def recall(self, query: str, k: int = 5) -> list[Recall]: ...

    def propose(self, thread_id: str, facts: list[str]) -> None:
        """Écriture en QUARANTAINE uniquement (D6) — jamais dans la mémoire de prod."""
        ...


@dataclass
class Assembled:
    """Sortie du Context Engine pour UN appel modèle."""

    messages: list[dict[str, Any]]  # prêts pour /chat/completions
    tools: list[dict[str, Any]]  # schémas envoyés à cet appel (cœur + chargés)
    prefix_sha256: str  # hash du préfixe stable (couche 1 + schémas cœur) — identique entre tours et paliers (A4)
    layer_tokens: dict[str, int] = field(default_factory=dict)


class ContextEngine(Protocol):
    """Assemble les 5 couches (PLAN §2.4) : [préfixe][profil][épisodes clos][épisode courant][bloc du tour].

    Invariants à tester en unitaire :
      - le préfixe ne dépend NI du palier NI du modèle NI de l'heure (hash identique) ;
      - les couches 1-4 sont append-only à l'intérieur d'un épisode (le cache amont reste valide) ;
      - le rappel mémoire et l'index d'outils vont dans le bloc du tour, jamais dans le préfixe.
    """

    def assemble(self, turn: Turn, history: list[Turn], recall: list[Recall], loaded_tools: list[ToolSpec],
                 tier: Tier) -> Assembled: ...

    def admit_result(self, result: ToolResult) -> ToolResult:
        """Résultat long → réservoir (remplacé par résumé + id) ; sinon renvoyé tel quel."""
        ...


# ─────────────────────── pilier (b) : juge Nina QA ───────────────────────


class Invariants(Protocol):
    """Couche 1 : déterministe, ne dépend d'aucun modèle. Toujours AVANT le juge."""

    def check_input(self, text: str) -> InputCheck: ...

    def check_action(self, call: ToolCall, spec: ToolSpec | None, turn: Turn) -> Verdict: ...

    def redact_result(self, result: ToolResult, spec: ToolSpec | None) -> ToolResult:
        """Appliqué par la boucle à CHAQUE résultat d'outil avant qu'il entre dans le contexte ou le store :
        effet SECRET → valeur remplacée par une poignée [secret:NOM] ; tout résultat → valeurs connues du coffre caviardées."""
        ...

    def scrub_output(self, text: str) -> str:
        """Scrubber de sortie : valeurs connues du coffre + motifs (PLAN §2.3 étape 8)."""
        ...


@dataclass
class JudgeAnswer:
    choice: str  # l'option retenue (un token)
    probabilities: dict[str, float] | None  # None sans logprobs (D13)
    model: str
    latency_s: float


class Judge(Protocol):
    """Couche 2 : questions atomiques, max_tokens=1. `options` = libellés d'un seul token chacun."""

    def ask(self, question_id: str, prompt: str, options: list[str]) -> JudgeAnswer: ...


class Gates(Protocol):
    """Combine invariants + juge en un Verdict par porte. La plus sévère décision gagne."""

    def action(self, call: ToolCall, spec: ToolSpec | None, turn: Turn) -> list[Verdict]: ...

    def switch(self, reply: str, turn: Turn, tier: Tier) -> list[Verdict]: ...


class Escalator(Protocol):
    """Escalade exécutée par le noyau (carte sur le canal d'origine). Renvoie le texte qui remplace la réponse."""

    def escalate(self, turn: Turn, reason: str) -> str: ...


# ─────────────────────── pilier (c) : multi-modèle ───────────────────────


@dataclass
class RouteDecision:
    tier: Tier
    reasoning: bool
    reason: str  # journalisé à chaque appel (C2)


@dataclass
class RouteState:
    """Ce que la politique voit pour décider du palier de l'appel suivant."""

    turn: Turn
    call_index: int  # 0 = premier appel du tour
    tool_failures: int = 0  # échecs d'outil depuis le dernier message humain
    repeated_error: bool = False  # même signature d'erreur 2×
    switch_verdicts: list[Verdict] = field(default_factory=list)  # verdicts de la dernière porte bascule
    current_tier: Tier = Tier.SHIP


class Policy(Protocol):
    def choose(self, state: RouteState) -> RouteDecision: ...


@dataclass
class Completion:
    message: Message
    usage: Usage
    model: str
    latency_s: float
    finish_reason: str | None = None
    raw: dict[str, Any] | None = None
    # P1-S1 (diag latence, 17.10.2026) : nombre de tentatives HTTP faites par le provider pour CET appel
    # (1 = du premier coup). `core/loop.py` journalise un fait (ledger) quand > 1 — visibilité sur un amont
    # lent/saturé sans jamais bloquer le tour (voir providers/sokkan.py::SokkanProvider._post_with_retry).
    attempts: int = 1


class Provider(Protocol):
    """Client OpenAI-compatible d'un palier. `session_key` = affinité de cache côté Router (F5).

    Streaming (P1, 19.10.2026) : un provider qui sait streamer accepte EN PLUS un mot-clé optionnel
    `on_delta: Callable[[str], None] | None = None` (morceaux de `content` bruts, dans l'ordre) et renvoie
    exactement la même `Completion` qu'en non-streamé. Ce mot-clé ne fait PAS partie du Protocol : le
    noyau ne le passe qu'aux providers dont la signature de `complete` le déclare (`core/loop.py::
    accepts_on_delta`) — un provider qui l'ignore (faux providers des tests, amont sans SSE) reste valide."""

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], *, reasoning: bool,
                 session_key: str, max_tokens: int = 2048) -> Completion: ...


class TurnStream(Protocol):
    """Puits du texte streamé d'un tour (P1, 19.10.2026 — `api/server.py`, en-tête `X-Nokker-Stream-Tokens`).

    Le noyau n'y écrit que du texte DÉJÀ caviardé (`Invariants.scrub_output` sur le cumul, avec retenue —
    `core/stream.py::ScrubbedDeltas`). `reset()` = le texte streamé depuis le dernier reset n'est pas la
    réponse (l'appel a fini en tool_calls, a été relancé, ou a échoué) : le client le vide. La réponse qui
    fait foi reste `Turn.reply`, envoyée à la fin du tour."""

    def delta(self, text: str) -> None: ...

    def reset(self) -> None: ...


# ─────────────────────── pilier (d) : approbations fortes ───────────────────────
#
# P1 (18.10.2026), docs/APPROVALS.md fait foi. Une action que les invariants mettent en `Decision.HITL`
# (couche 1, `qa/invariants.py`) n'est jamais exécutée sans une approbation PROUVÉE CRYPTOGRAPHIQUEMENT
# par un humain autorisé — `Verdict.strength` (core/turn.py) dit quel niveau de preuve exiger :
# "strong" (clé d'appareil libérée par biométrie) ou "standard" (idem, ou TOTP).
#
# Le `Kernel` (core/loop.py) n'utilise que `request()` (branchement sur `Decision.HITL`, optionnel : sans
# port `approvals`, le comportement actuel — carte seulement dans `turn.pending_approvals` — ne change
# pas). Le reste du cycle de vie (défi, décision, enrôlement d'appareil, session) est consommé par
# `nokker/api/server.py` et `nokker/approvals/executor.py`, pas par la boucle elle-même.


@dataclass
class Approval:
    """Une carte d'approbation — DOCS/APPROVALS.md §2 pour le cycle de vie complet."""

    id: str
    thread_id: str
    turn_index: int
    channel: str
    tool_name: str
    arguments: dict[str, Any]
    args_sha256: str  # empreinte des arguments AU MOMENT DE LA DEMANDE (JSON canonique, clés triées)
    strength: str  # "strong" | "standard" (Verdict.strength — jamais None ici : sinon pas de demande)
    rule: str  # Verdict.rule qui a produit le HITL
    reason: str = ""  # Verdict.reason
    advice: str | None = None  # avis de Nina (risques/sauvegarde/retour arrière) — n'accompagne, ne bloque rien
    status: str = "pending"  # pending | approved | rejected | expired | executed | failed
    created_at: float = 0.0  # epoch seconds (time.time())
    expires_at: float = 0.0
    decided_by: str | None = None  # identité VÉRIFIÉE (device.user ou utilisateur du TOTP)
    decided_via: str | None = None  # "signature" | "signature+telegram" | "totp" | "cli"
    decided_at: float | None = None
    result_summary: str | None = None  # contenu (déjà `redact_result`) du `ToolResult` d'exécution
    error: str | None = None


@dataclass
class Challenge:
    """Défi à signer — JSON canonique `{"approval_id","args_sha256","nonce","exp","aud"}` (TTL 120 s,
    docs/APPROVALS.md §3). `canonical()` est EXACTEMENT ce que le client doit signer (préfixé du nom de
    la décision, `challenge + "." + decision`) et renvoyer tel quel dans `Proof.challenge`."""

    approval_id: str
    args_sha256: str
    nonce: str
    exp: float
    aud: str = "nokker-approval"

    def canonical(self) -> str:
        import json

        payload = {"approval_id": self.approval_id, "args_sha256": self.args_sha256, "nonce": self.nonce,
                   "exp": self.exp, "aud": self.aud}
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass
class Proof:
    """Preuve soumise à `Approvals.decide()`. Un seul des TROIS chemins de preuve ci-dessous est rempli :

      - appareil, signature brute (strong ou standard) : `device_id` + `challenge` (texte EXACT reçu de
        `challenge()`) + `signature` (ECDSA P-256/SHA-256, base64url, IEEE P1363 — signe
        `challenge + "." + decision`) ; `telegram_init_data` en plus si la preuve vient de la Mini App
        (validé en SUS de la signature) ;
      - appareil, WebAuthn/passkey (web, strong ou standard — ADR `nokker-apps/docs/adr/0001-*.md`) :
        `device_id` + `challenge` + `webauthn_client_data_json`/`webauthn_authenticator_data`/
        `webauthn_signature` (DER, PAS P1363) — le `challenge` WebAuthn embarqué dans
        `clientDataJSON.challenge` est `sha256(challenge + "." + decision)`, vérifié par
        `nokker.approvals.crypto.verify_webauthn_assertion` ; `strength == "strong"` exige le flag UV ;
      - TOTP (standard SEULEMENT — jamais pour `strength == "strong"`) : `totp_user` (qui soumet le
        code — pas prouvé par ce champ seul, c'est le code valide pour CE secret qui prouve) + `totp` +
        `challenge` (même défi, même garanties anti-rejeu/anti-falsification des arguments)."""

    challenge: str | None = None
    signature: str | None = None
    device_id: str | None = None
    telegram_init_data: str | None = None
    totp: str | None = None
    totp_user: str | None = None
    webauthn_client_data_json: str | None = None
    webauthn_authenticator_data: str | None = None
    webauthn_signature: str | None = None


class Approvals(Protocol):
    """docs/APPROVALS.md §4. Implémentations : `nokker.approvals.pg.PgApprovals` (prod),
    `nokker.approvals.memory.InMemoryApprovals` (tests)."""

    def request(self, turn: Turn, call: ToolCall, verdict: Verdict, advice: str | None) -> Approval: ...

    def get(self, approval_id: str) -> Approval | None: ...

    def pending(self, user: str | None = None) -> list[Approval]: ...

    def challenge(self, approval_id: str) -> Challenge: ...

    def decide(self, approval_id: str, decision: str, proof: Proof) -> Approval:
        """Lève (`nokker.approvals.errors.ProofInvalid`/`ApprovalExpired`/`ApprovalNotPending`) si la
        preuve, le nonce, l'expiration ou l'expéditeur ne tiennent pas — jamais de retour « approuvé »
        silencieusement dégradé."""
        ...


# ─────────────────────────────── socle ───────────────────────────────


class TurnObserver(Protocol):
    """Observateur optionnel du tour (P1 bulles, docs/BUBBLES.md §3/§6) — `nokker/bubbles/hub.py::BubbleHub`.
    Le noyau l'appelle sans jamais dépendre de ce qu'il fait : toute exception levée ici est avalée (un
    observateur en panne ne casse jamais un tour)."""

    def context_for(self, thread_id: str) -> list[str]:
        """Blocs de contexte en attente pour ce fil (vidés : un bloc n'est injecté qu'une fois)."""
        ...

    def turn_started(self, turn: Turn) -> None: ...

    def tool_call(self, turn: Turn, call: ToolCall) -> None: ...

    def tool_result(self, turn: Turn, call: ToolCall, result: ToolResult, ms: int) -> None: ...

    def tool_executed(self, turn: Turn, call: ToolCall, spec: ToolSpec | None, result: ToolResult) -> str | None:
        """Appelé seulement quand l'outil a VRAIMENT été exécuté (porte action ALLOW, pas un rejeu du mémo
        READ). Renvoie un suffixe à ajouter au résultat admis (ex. « [bulle bu_… affichée] »), ou None."""
        ...

    def turn_done(self, turn: Turn) -> None: ...


class Store(Protocol):
    """Postgres dès le jour 1 (D3). Les tours sont append-only."""

    def history(self, thread_id: str, limit: int | None = None) -> list[Turn]: ...

    def next_index(self, thread_id: str) -> int: ...

    def append(self, turn: Turn) -> None: ...


class Ledger(Protocol):
    def record(self, thread_id: str, turn_index: int, call: LLMCall) -> None: ...

    def record_verdict(self, thread_id: str, turn_index: int, verdict: Verdict) -> None: ...


class StreamInterrupted(RuntimeError):
    """Le flux amont a cassé APRÈS qu'un morceau a été remis à `on_delta` : le provider ne réessaie pas lui-même,
    c'est la boucle qui vide le texte déjà vu (reset) et rejoue l'appel sans streaming."""


__all__ = [
    "StreamInterrupted",    "Approval", "Approvals", "Assembled", "Challenge", "Completion", "ContextEngine", "Decision", "Escalator",
    "Gates", "Invariants", "Judge", "JudgeAnswer", "Ledger", "MemoryPort", "Policy", "Proof", "Provider", "Recall",
    "RouteDecision", "RouteState", "Store", "ToolContext", "ToolRegistry", "ToolSource", "ToolSpec", "TurnInput",
    "TurnObserver", "TurnStream",
]
