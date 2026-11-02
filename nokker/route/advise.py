"""Outil `advise` : l'exécutant consulte un palier plus fort au milieu de son tour (veille 01.11, §7.1).

Le routage reste PAR TOUR (`route/policy.py`) : le tour garde son palier (Ship en général) et sa masse de jetons. Quand
il bloque, le modèle appelle `advise(question, context)` ; le noyau envoie UNE requête au palier conseiller (`deep` par
défaut, sinon le palier nommé dans `advise.tier`, par exemple un palier `nina` déclaré sous `tiers:`), sans outils, sans
raisonnement, avec un plafond de jetons de sortie, et rend sa consigne comme résultat d'outil.

Ce que voit le conseiller : un message système fixe (`ADVISOR_SYSTEM`), la demande de l'utilisateur du tour (bornée), la
question et l'extrait donnés par l'exécutant (bornés). Jamais la conversation entière : c'est l'exécutant qui choisit
l'extrait. Tout part par le filet de secrets du noyau (`Invariants.redact_context`) et la réponse par `scrub_output`.

Garde-fous :
  - plafond d'appels par tour (`max_per_turn`, défaut 2) : au-delà, refus sans appel ;
  - pas de récursion : le conseiller n'a aucun outil, et un `advise` lancé pendant une consultation est refusé ;
  - un tour déjà servi par le palier conseiller ne le consulte pas (il paierait deux fois la même réponse) ;
  - palier conseiller absent (ni le principal, ni `fallback_tier`) : refus clair, le tour continue.

Coût : l'appel est un `LLMCall` du tour (`turn.calls`, donc `turn.cost` X2, budgets de projet et de foyer, arrêt Crew),
écrit au ledger avec le motif « conseiller : … » (la ligne de route de l'appel), et tracé à part par l'enregistreur de
traces (`TraceRecorder.side`, ligne `meta.side = "advisor"`).

Drapeau `advise.enabled` (défaut false) : éteint, la source n'est pas construite, l'outil n'existe pas.
"""
from __future__ import annotations

import contextvars
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from nokker.core.ports import ToolContext, ToolSpec
from nokker.core.turn import Effect, Message, Tier, ToolResult
from nokker.tenancy.context import current_tenant_or_none

logger = logging.getLogger("nokker.route.advise")

SOURCE = "builtin:advise"
TOOL = "advise"
REASON_PREFIX = "conseiller"
TRUNCATED = " …[coupé]"

# ── Textes vus par un modèle (à transmettre tels quels à l'entraînement avant fusion) ──
ADVISOR_SYSTEM = (
    "Tu es le conseiller d'un agent de travail qui exécute une tâche avec un modèle plus petit. Il te consulte au "
    "milieu de son tour. Tu n'as pas d'outils et tu ne vois que sa question et l'extrait qu'il te donne. Réponds par "
    "une consigne courte et actionnable : la marche à suivre, le piège à éviter ou la décision à prendre, en quelques "
    "lignes. Si l'extrait ne suffit pas pour trancher, dis ce qu'il doit vérifier d'abord. N'invente ni fichier, ni "
    "résultat, ni valeur.")
ADVISOR_USER = "[Demande de l'utilisateur]\n{demand}\n[/Demande]\n\n[Question de l'agent]\n{question}\n[/Question]"
ADVISOR_EXTRACT = "\n\n[Extrait du contexte]\n{context}\n[/Extrait]"
DESCRIPTION = (
    "Demande conseil à un modèle plus fort quand tu bloques (plan d'attaque, bug qui résiste, choix risqué). Il ne "
    "voit que ta question et l'extrait que tu lui donnes, n'a pas d'outils et répond par une consigne courte. Au plus "
    "{max_per_turn} fois par tour ; chaque appel coûte.")
PARAM_QUESTION = "ce que tu veux savoir, précis et autonome"
PARAM_CONTEXT = "extrait utile (code, erreur, contraintes), {context_chars} caractères au plus"
RESULT = "[Conseil]\n{advice}\n[/Conseil]"
REFUSED_CAP = "Plafond du conseiller atteint pour ce tour ({n} appels) : décide avec ce que tu as."
REFUSED_NESTED = "Le conseiller ne peut pas se consulter lui-même."
REFUSED_SAME_TIER = "Ce tour tourne déjà sur le palier du conseiller : décide toi-même."
REFUSED_ABSENT = "Conseiller indisponible (aucun palier conseiller servi) : continue sans."
REFUSED_EMPTY = "question requise"
FAILED = "Conseiller indisponible ({error}) : continue sans."
EMPTY_ADVICE = "(le conseiller n'a rien répondu)"

_ADVISING: contextvars.ContextVar[bool] = contextvars.ContextVar("nokker_advising", default=False)


def _bounded(text: Any, limit: int) -> str:
    t = str(text or "").strip()
    return t if len(t) <= limit else t[:limit] + TRUNCATED


@dataclass
class AdviseConfig:
    enabled: bool = False
    tier: str = "deep"  # clé de `tiers:` ; « deep » = le palier Deep servi par le noyau
    fallback_tier: str = ""  # palier de repli quand `tier` n'est pas servi (ex. « nina »), vide = aucun
    max_per_turn: int = 2
    max_tokens: int = 600  # consigne courte (la veille parle de 400-700 jetons)
    question_chars: int = 1500
    context_chars: int = 6000
    demand_chars: int = 2000

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> AdviseConfig:
        raw = dict(raw or {})
        known = {k: raw[k] for k in cls.__dataclass_fields__ if k in raw}
        cfg = cls(**known)
        cfg.max_per_turn = max(1, int(cfg.max_per_turn))
        cfg.max_tokens = max(16, int(cfg.max_tokens))
        return cfg

    def tier_names(self) -> tuple[str, ...]:
        return tuple(n for n in (self.tier, self.fallback_tier) if n)


def tier_enum(name: str) -> Tier | None:
    try:
        return Tier(name)
    except ValueError:
        return None


@dataclass
class AdviseToolSource:
    """`ToolSource` de l'outil `advise`. `extra` : fournisseurs des paliers conseillers qui ne sont pas des paliers de
    routage (`tiers.nina` …), construits par `build_kernel` ; les paliers de routage (`deep`, `ship`…) sont lus dans
    `kernel.providers` À L'APPEL (la liste des paliers servis peut changer sans redémarrage)."""

    config: AdviseConfig
    extra: dict[str, Any] = field(default_factory=dict)
    max_turns: int = 4096
    core = True
    _counts: OrderedDict = field(default_factory=OrderedDict, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def specs(self) -> list[ToolSpec]:
        c = self.config
        return [ToolSpec(
            name=TOOL, core=True, source=SOURCE, effect=Effect.READ,
            description=DESCRIPTION.format(max_per_turn=c.max_per_turn),
            parameters={"type": "object", "required": ["question"], "properties": {
                "question": {"type": "string", "description": PARAM_QUESTION},
                "context": {"type": "string",
                            "description": PARAM_CONTEXT.format(context_chars=c.context_chars)}}})]

    # ── palier conseiller ──
    def provider_for(self, kernel: Any) -> tuple[str, Any] | None:
        providers = getattr(kernel, "providers", None) or {}
        for name in self.config.tier_names():
            t = tier_enum(name)
            # Palier de routage servi par le noyau d'abord ; sinon le fournisseur construit à part (palier hors `Tier`,
            # ou `nina` quand le routage ne le sert pas : mode off ou shadow).
            p = providers.get(t) if t is not None else None
            if p is None:
                p = self.extra.get(name)
            if p is not None:
                return name, p
        return None

    def _take(self, key: tuple) -> int:
        """Compte un appel du tour ; rend le nombre d'appels AVANT celui-ci."""
        with self._lock:
            n = self._counts.get(key, 0)
            self._counts[key] = n + 1
            self._counts.move_to_end(key)
            while len(self._counts) > self.max_turns:
                self._counts.popitem(last=False)
            return n

    def call(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        def out(text: str, error: bool = False) -> ToolResult:
            return ToolResult(call_id="", name=name, content=text, is_error=error)

        if name != TOOL:
            return out(f"outil inconnu : {name}", True)
        if _ADVISING.get():
            return out(REFUSED_NESTED, True)
        c = self.config
        question = _bounded(arguments.get("question"), c.question_chars)
        if not question:
            return out(REFUSED_EMPTY, True)
        kernel, turn = ctx.kernel, ctx.turn
        if kernel is None or turn is None:
            return out(REFUSED_ABSENT, True)
        chosen = self.provider_for(kernel)
        if chosen is None:
            return out(REFUSED_ABSENT, True)
        tier_name, provider = chosen
        t = tier_enum(tier_name)
        executing = next((call.tier for call in reversed(turn.calls)
                          if not str(call.route_reason).startswith(REASON_PREFIX)), None)
        if t is not None and executing is t:
            return out(REFUSED_SAME_TIER, True)
        key = (current_tenant_or_none(), ctx.thread_id, ctx.turn_index)
        if self._take(key) >= c.max_per_turn:
            return out(REFUSED_CAP.format(n=c.max_per_turn), True)

        user = ADVISOR_USER.format(demand=_bounded(getattr(turn.input, "text", ""), c.demand_chars) or "(aucune)",
                                   question=question)
        extract = _bounded(arguments.get("context"), c.context_chars)
        if extract:
            user += ADVISOR_EXTRACT.format(context=extract)
        messages = [{"role": "system", "content": ADVISOR_SYSTEM}, {"role": "user", "content": user}]
        redact = getattr(getattr(kernel, "invariants", None), "redact_context", None)
        if callable(redact):
            messages = redact(messages)
        token = _ADVISING.set(True)
        started = time.monotonic()
        try:
            comp = provider.complete(messages, [], reasoning=False, session_key=f"{ctx.thread_id}#advise",
                                     max_tokens=c.max_tokens)
        except Exception as e:  # noqa: BLE001 — un conseiller en panne ne casse jamais le tour
            logger.warning("route : conseiller %s en échec (%s)", tier_name, e)
            return out(FAILED.format(error=type(e).__name__), True)
        finally:
            _ADVISING.reset(token)
        if not getattr(comp, "latency_s", None):
            comp.latency_s = time.monotonic() - started
        from_tier = executing.value if executing is not None else "?"
        reason = f"{REASON_PREFIX} : consulté par l'exécutant ({from_tier} → {tier_name})"
        record = getattr(kernel, "record_side_call", None)
        if callable(record):
            record(turn, t or Tier.DEEP, comp, reason, messages, side="advisor",
                   meta={"advisor_tier": tier_name, "advisor_model": str(comp.model or "")})
        logger.info("route : %s, modèle %s, %d+%d jetons", reason, comp.model, comp.usage.prompt,
                    comp.usage.completion)
        advice = (comp.message.content or "").strip() if isinstance(comp.message, Message) else ""
        scrub = getattr(getattr(kernel, "invariants", None), "scrub_output", None)
        if callable(scrub):
            advice = scrub(advice)
        return out(RESULT.format(advice=advice or EMPTY_ADVICE))


def build_advise_source(raw: dict[str, Any] | None, tiers: dict[str, Any], make_provider: Any) -> AdviseToolSource | None:
    """La source si `advise.enabled`, sinon None. `make_provider(tier_config)` construit le fournisseur d'un palier
    conseiller qui n'est pas un palier de routage (clé de `tiers` hors `Tier`)."""
    cfg = AdviseConfig.from_dict(raw)
    if not cfg.enabled:
        return None
    extra = {}
    for name in cfg.tier_names():
        if name in tiers and tier_enum(name) in (None, Tier.NINA):  # nina : servi par le routage seulement en mode route
            extra[name] = make_provider(tiers[name])
    return AdviseToolSource(config=cfg, extra=extra)


__all__ = ["ADVISOR_EXTRACT", "ADVISOR_SYSTEM", "ADVISOR_USER", "AdviseConfig", "AdviseToolSource", "DESCRIPTION",
           "REASON_PREFIX", "RESULT", "TOOL", "build_advise_source"]
