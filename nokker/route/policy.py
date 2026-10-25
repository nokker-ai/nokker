"""`Policy` (`core.ports.Policy`) — PLAN-P1 §2.6/§5.1 (F2) et X8 (docs/LOKKAL.md §7.17).

`PolicyV0` — règle fixe, sans classe de tâche :

  - Ship par défaut, sans raisonnement ;
  - Deep si la dernière porte bascule a rendu REDO_DEEP, ou si `tool_failures >= deep_tool_failures`,
    ou si `repeated_error` ;
  - le motif (`RouteDecision.reason`) est TOUJOURS renseigné (journalisé au ledger, C2).

`PolicyV1` — routage par tour sur trois paliers (X8) : la classe du tour (`route/classify.py`) est fixée au premier
appel et tient pour tout le tour (le cache de préfixe d'un palier ne sert qu'à lui) :

  - trivial → petit palier (`small`), au premier appel seulement : s'il appelle un outil, la suite passe en Ship ;
  - conversation → Ship ;
  - code → Deep (« Deep écrit le code », LOKKAL §5.1) ; un tour de conversation qui se met à écrire un fichier
    (`write_file`, `patch`) passe en Deep pour la suite ;
  - les signaux de V0 (REDO_DEEP, échecs d'outil, même erreur ×2) envoient en Deep, quelle que soit la classe ;
  - un tour qui porte une image (X7, `turn.images`) ne part jamais sur un palier sans vision quand un palier servi l'a
    (`vision`) : le palier choisi se replie sur le plus proche qui lit les images (petit → Ship → Deep). Les tours
    texte gardent leur palier (et leur coût) ; aucun palier avec vision : routage inchangé, image signalée non lue.

Un palier absent de la config (clé de l'utilisateur, modèle local : un seul modèle) se replie sans erreur sur le
palier présent le plus proche (`resolve`) : petit → Ship → Deep, Deep → Ship → petit.
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from nokker.core.ports import RouteDecision, RouteState
from nokker.core.turn import Decision, Tier
from nokker.route.classify import Classified, ModelClassifier, TaskClass, classify

# Outils dont l'appel fait d'un tour de conversation un tour de code (écrire dans le dossier de travail).
CODE_WRITE_TOOLS = frozenset({"write_file", "patch"})

_FALLBACK = {
    Tier.SMALL: (Tier.SMALL, Tier.SHIP, Tier.DEEP),
    Tier.SHIP: (Tier.SHIP, Tier.DEEP, Tier.SMALL),
    Tier.DEEP: (Tier.DEEP, Tier.SHIP, Tier.SMALL),
    Tier.LOCAL: (Tier.LOCAL, Tier.SMALL, Tier.SHIP, Tier.DEEP),
}


def resolve(tier: Tier, available: Iterable[Tier]) -> Tier:
    """Le palier demandé s'il est configuré, sinon le plus proche ; Ship si rien n'est connu (le noyau escalade)."""
    have = set(available)
    for t in _FALLBACK.get(tier, (tier,)):
        if t in have:
            return t
    return Tier.SHIP


@dataclass
class PolicyV0:
    ship_reasoning: bool = False
    deep_reasoning: bool = True
    deep_tool_failures: int = 3

    def choose(self, state: RouteState) -> RouteDecision:
        if any(v.decision is Decision.REDO_DEEP for v in state.switch_verdicts):
            return RouteDecision(tier=Tier.DEEP, reasoning=self.deep_reasoning,
                                  reason="porte bascule : verdict REDO_DEEP")
        if state.tool_failures >= self.deep_tool_failures:
            return RouteDecision(tier=Tier.DEEP, reasoning=self.deep_reasoning,
                                  reason=f"{state.tool_failures} échecs d'outil >= {self.deep_tool_failures}")
        if state.repeated_error:
            return RouteDecision(tier=Tier.DEEP, reasoning=self.deep_reasoning,
                                  reason="même signature d'erreur deux fois")
        return RouteDecision(tier=Tier.SHIP, reasoning=self.ship_reasoning, reason="défaut : Ship")


@dataclass
class PolicyV1(PolicyV0):
    """Trois paliers par classe de tâche (X8). `available` rend les paliers servis À CET INSTANT (le choix d'un modèle
    à l'onboarding peut en retirer un sans redémarrage) ; None = tous."""

    available: Callable[[], Iterable[Tier]] | None = None
    # Paliers servis dont le modèle lit les images (`vision: true`), lus à chaque tour ; None = inconnu (rien ne change).
    vision: Callable[[], Iterable[Tier]] | None = None
    classifier: ModelClassifier | None = None  # étage 2 (modèle) ; None = règles seules
    code_to_deep: bool = True  # False : le code reste en Ship (Deep sur signaux seulement, comme V0)
    small_reasoning: bool = False
    max_threads: int = 2048  # classes gardées (dernier tour par fil), éviction la plus ancienne d'abord
    _turns: OrderedDict = field(default_factory=OrderedDict, init=False, repr=False)  # (fil, tour) -> Classified
    _last: OrderedDict = field(default_factory=OrderedDict, init=False, repr=False)  # fil -> TaskClass
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def _have(self) -> set[Tier]:
        return set(self.available()) if self.available is not None else set(Tier)

    def task_of(self, state: RouteState) -> Classified:
        """Classe du tour, calculée une fois (premier appel) et gardée pour les appels suivants du même tour."""
        turn = state.turn
        key = (turn.thread_id, turn.index)
        with self._lock:
            got = self._turns.get(key)
            previous = self._last.get(turn.thread_id)
        if got is None:
            small_ok = Tier.SMALL in self._have()
            got = classify(turn.input.text, previous, self.classifier if small_ok else None)
            with self._lock:
                self._turns[key] = got
                self._last[turn.thread_id] = got.task
                self._last.move_to_end(turn.thread_id)
                for d in (self._turns, self._last):
                    while len(d) > self.max_threads:
                        d.popitem(last=False)
        return got

    def _decision(self, tier: Tier, reason: str, state: RouteState | None = None) -> RouteDecision:
        have = self._have()
        actual = resolve(tier, have)
        if actual is not tier:
            reason = f"{reason} ; palier {tier.value} absent → {actual.value}"
        if state is not None and state.turn.images and self.vision is not None:
            seeing = set(self.vision()) & have
            if seeing and actual not in seeing:
                sees = resolve(actual, seeing)
                reason = f"{reason} ; image jointe, {actual.value} sans vision → {sees.value}"
                actual = sees
        reasoning = {Tier.DEEP: self.deep_reasoning, Tier.SMALL: self.small_reasoning}.get(actual, self.ship_reasoning)
        return RouteDecision(tier=actual, reasoning=reasoning, reason=reason)

    def choose(self, state: RouteState) -> RouteDecision:
        signal = PolicyV0.choose(self, state)
        if signal.tier is Tier.DEEP:
            return self._decision(Tier.DEEP, signal.reason, state)
        c = self.task_of(state)
        wrote = any(tc.name in CODE_WRITE_TOOLS for m in state.turn.messages for tc in m.tool_calls)
        if c.task is TaskClass.CODE or wrote:
            if not self.code_to_deep:
                return self._decision(Tier.SHIP, f"code, Deep coupé par la config : {c.reason}", state)
            why = c.reason if c.task is TaskClass.CODE else "écriture de fichier dans un tour de conversation"
            return self._decision(Tier.DEEP, f"classe code : {why}", state)
        if c.task is TaskClass.TRIVIAL and state.call_index == 0:
            return self._decision(Tier.SMALL, f"classe trivial : {c.reason}", state)
        if c.task is TaskClass.TRIVIAL:
            return self._decision(Tier.SHIP, "trivial qui a appelé un outil : suite en Ship", state)
        return self._decision(Tier.SHIP, f"classe conversation : {c.reason}", state)


__all__ = ["CODE_WRITE_TOOLS", "PolicyV0", "PolicyV1", "resolve"]
