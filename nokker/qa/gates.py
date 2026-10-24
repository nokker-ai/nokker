"""`Gates` v0 (`core.ports.Gates`) — PLAN-P1 §2.5/§5.1 (F2). Combine :

  - couche 1, `Invariants` (port — F3 livre la vraie implémentation ; v0 l'utilise tel quel, aucune
    décision de sûreté n'est dupliquée ici) ;
  - couche 2, un juge NUL (`NullJudge`) qui renvoie toujours ALLOW en attendant le vrai service Nina QA
    (B-2). Il satisfait le Protocol `Judge` pour que la couche 2 soit déjà câblée, pas simulée en dur.

Règle de bascule propre à `GatesV0` (au-delà de ce que l'invariant d'entrée sait déjà faire) : un drapeau
de domaine (fiscal/juridique/montant) sur `turn.input.domain_flags`, si le tour n'est pas déjà escaladé,
force ESCALATE — "escalade humaine" de l'invariant 5 du préfixe, appliquée par le noyau et pas seulement
par la bonne volonté du modèle.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Callable

from nokker.core.ports import Judge, JudgeAnswer, ToolSpec, call_effect
from nokker.core.turn import Decision, Effect, Gate, Tier, ToolCall, Turn, Verdict
from nokker.qa.calibrate import DEFAULT_THRESHOLDS, Thresholds
from nokker.qa.questions import QUESTIONS, Primitive, action_prompt, switch_prompt

logger = logging.getLogger("nokker.qa.gates")

DEFAULT_DOMAIN_FLAGS = frozenset({"fiscal", "juridique", "montant"})


@dataclass
class NullJudge:
    """Couche 2 nulle (B-2 pas encore livré) : toujours ALLOW, jamais de probabilité (pas de logprobs)."""

    model_name: str = "null-judge-v0"

    def ask(self, question_id: str, prompt: str, options: list[str]) -> JudgeAnswer:
        choice = options[0] if options else "ALLOW"
        return JudgeAnswer(choice=choice, probabilities=None, model=self.model_name, latency_s=0.0)


@dataclass
class GatesV0:
    invariants: object  # core.ports.Invariants — typé `object` pour ne pas importer F3 ici (découplage)
    judge: Judge = field(default_factory=NullJudge)
    domain_flags: frozenset[str] = DEFAULT_DOMAIN_FLAGS

    def action(self, call: ToolCall, spec: ToolSpec | None, turn: Turn) -> list[Verdict]:
        return [self.invariants.check_action(call, spec, turn)]

    def switch(self, reply: str, turn: Turn, tier: Tier) -> list[Verdict]:
        flags = turn.input.domain_flags & self.domain_flags
        if flags and not turn.escalated:
            return [Verdict(gate=Gate.SWITCH, decision=Decision.ESCALATE, layer=1,
                             rule=f"domaine:{'+'.join(sorted(flags))}",
                             reason="drapeau de domaine sans escalade explicite du modèle")]
        # Couche 2 : juge nul en attendant B-2 — toujours ALLOW, mais le port Judge est bien exercé.
        answer = self.judge.ask("switch:v0", reply, ["ALLOW"])
        return [Verdict(gate=Gate.SWITCH, decision=Decision.ALLOW, layer=2, rule="judge-nul",
                         reason="aucun juge réel avant B-2", probability=None, judge_model=answer.model)]


__all__ = ["DEFAULT_DOMAIN_FLAGS", "GatesV0", "GatesV1", "NullJudge"]


# ───────────────────────────────────────────────────────────────────────────────────────────────
# `Gates` v1 — chantier B-2/B-3 (PLAN-P1 §2.5/§5.3) : le juge Nina QA réel remplace `NullJudge`.
# ───────────────────────────────────────────────────────────────────────────────────────────────
#
# Doctrine Nick, 17.10.2026 (note mémoire `nokker`, fin de note) : « le juge Nina QA conseille et
# escalade (fiscal/juridique : il ne tranche pas à la place de l'humain), il ne bloque pas ». Donc :
#   - DENY reste réservé à la couche 1 (`qa/invariants.py` : SEND_BULK toujours, etc.) — `GatesV1`
#     n'émet JAMAIS DENY depuis le juge ;
#   - porte ACTION, en cas de doute ou de faute probable → **HITL** (PLAN §2.3 étape 5 : « le juge
#     (couche 2) : cette action est-elle celle demandée ? En cas de doute : HITL ») ;
#   - porte BASCULE, doute → **REDO_DEEP** (relance en Deep, même préfixe — `core/loop.py` gère déjà
#     la relance sans faire entrer la réponse candidate dans l'historique) ; faute probable au-dessus
#     du seuil haut → **ESCALATE** (le noyau remplace la réponse, PLAN §2.3 étape 7).
#
# Nécessité (ne pas appeler le juge pour rien, latence/coût) :
#   - ACTION : seulement si l'outil a un effet déclaré au-delà de READ (une lecture n'a rien à
#     juger : la couche 1 a déjà statué `effect:read` → ALLOW) ;
#   - BASCULE : seulement si (a) un outil à effet a été utilisé CE TOUR (verdict couche 1 `effect:*`
#     journalisé, autre que `effect:read`), (b) un drapeau de domaine est posé, ou (c) la réponse a
#     l'air d'affirmer un fait/chiffre (heuristique grossière `_looks_assertive` — un faux négatif ici
#     ne casse rien de sûr : ce n'est qu'un déclencheur de coût, pas un filet de sûreté, qui reste la
#     couche 1 + le déclenchement (a)/(b) dès qu'un outil ou un drapeau de domaine est en jeu).
#
# ⚠️ Champ manquant au contrat (`core.turn.Verdict`, F2) : le juge produit un AVIS texte (risques,
# sauvegarde, retour arrière — consigne Nick 17.10) quand il déclenche HITL. `Verdict` n'a pas de
# champ `advice`. PAS AJOUTÉ ICI (mandat B-2 : ne pas toucher `core/*.py`) — l'avis est mis dans
# `Verdict.reason` en attendant qu'un chantier avec mandat sur `core/turn.py` ajoute ce champ.
# Signalé aussi dans `nokker-judge-bench/README.md` § B-2 et dans la note mémoire `nokker-p0`.

_ASSERTIVE_HINT = re.compile(
    r"\d|chiffre|montant|CHF|EUR|résultat|confirmé|effectué|réalisé|a été (envoyé|créé|supprimé|modifié)",
    re.IGNORECASE)


def _looks_assertive(reply: str) -> bool:
    return bool(_ASSERTIVE_HINT.search(reply or ""))


def _tool_effect_used_this_turn(turn: Turn) -> bool:
    return any(v.gate is Gate.ACTION and v.layer == 1 and v.rule not in ("effect:read",)
              and not v.rule.startswith("effect:read") for v in turn.verdicts)


@dataclass
class GatesV1:
    """Couche 1 (`Invariants`, F3) + couche 2 (le vrai `Judge`, B-2) — porte action APRÈS les
    invariants, porte bascule avant l'envoi. Seuils par question : `calibrate.Thresholds`
    (`DEFAULT_THRESHOLDS` si aucune calibration mesurée n'est fournie — zone grise large et
    prudente, 0,3/0,7)."""

    invariants: object  # core.ports.Invariants
    judge: Judge
    thresholds: dict[str, Thresholds] = field(default_factory=lambda: dict(DEFAULT_THRESHOLDS))
    domain_flags: frozenset[str] = DEFAULT_DOMAIN_FLAGS
    context_builder: Callable[[Turn], str] | None = None  # None -> _default_context

    # ── porte action (PLAN §2.3 étape 5) ──
    def action(self, call: ToolCall, spec: ToolSpec | None, turn: Turn) -> list[Verdict]:
        v1 = self.invariants.check_action(call, spec, turn)
        if v1.decision is not Decision.ALLOW:
            return [v1]  # couche 1 a déjà statué (HITL/DENY/...) : pas besoin du juge
        effect = call_effect(spec, call.arguments)
        if effect is Effect.READ:
            return [v1]  # pas d'effet -> pas nécessaire (coût/latence)
        v2 = self._ask_action(call, spec, turn, effect)
        return [v1, v2]

    def _ask_action(self, call: ToolCall, spec: ToolSpec | None, turn: Turn, effect: Effect) -> Verdict:
        q = QUESTIONS["A.effet"]
        context = self._context(turn)
        prompt = action_prompt(turn.input.text, context, call.name, call.arguments, effect.value, q)
        t = self.thresholds.get(q.question_id, DEFAULT_THRESHOLDS[q.question_id])
        answer = self.judge.ask(q.question_id, prompt, list(q.options))
        p_alert = _p_alert(q, answer)
        if p_alert is None:
            # Pas de logprobs exploitables : repli sur le choix brut (pas de zone grise mesurable).
            decision = Decision.HITL if q.alerts(answer.choice) else Decision.ALLOW
            reason = f"juge {answer.model} (sans logprobs) : {answer.choice}"
        elif p_alert >= t.high:
            decision = Decision.HITL
            reason = f"juge {answer.model} : effet non autorisé probable (p={p_alert:.2f} ≥ {t.high})"
        elif p_alert > t.low:
            decision = Decision.HITL  # pas de REDO_DEEP côté action (PLAN §2.3 étape 5 : doute -> HITL)
            reason = f"juge {answer.model} : zone grise (p={p_alert:.2f}), vérification humaine demandée"
        else:
            decision = Decision.ALLOW
            reason = f"juge {answer.model} : effet jugé autorisé (p={p_alert:.2f} ≤ {t.low})"
        if decision is Decision.HITL:
            reason = self._with_advice(reason, turn, context, call_desc=f"{call.name}({call.arguments})")
        return Verdict(gate=Gate.ACTION, decision=decision, layer=2, rule=f"judge:{q.question_id}",
                       reason=reason, probability=p_alert, judge_model=answer.model,
                       latency_s=answer.latency_s, subject=call.id)

    # ── porte bascule (PLAN §2.3 étape 7) ──
    def switch(self, reply: str, turn: Turn, tier: Tier) -> list[Verdict]:
        flags = turn.input.domain_flags & self.domain_flags
        if flags and not turn.escalated:
            return [Verdict(gate=Gate.SWITCH, decision=Decision.ESCALATE, layer=1,
                             rule=f"domaine:{'+'.join(sorted(flags))}",
                             reason="drapeau de domaine sans escalade explicite du modèle")]
        necessary = bool(flags) or _tool_effect_used_this_turn(turn) or _looks_assertive(reply)
        if not necessary:
            return [Verdict(gate=Gate.SWITCH, decision=Decision.ALLOW, layer=2, rule="judge:non-necessaire",
                             reason="aucun outil à effet, drapeau ou affirmation détectée ce tour")]
        return [self._ask_switch(q_id, reply, turn, tier) for q_id in ("R.humain", "R.invention", "R.secret")]

    def _ask_switch(self, question_id: str, reply: str, turn: Turn, tier: Tier) -> Verdict:
        q = QUESTIONS[question_id]
        context = self._context(turn)
        prompt = switch_prompt(turn.input.text, context, reply, q)
        t = self.thresholds.get(question_id, DEFAULT_THRESHOLDS[question_id])
        answer = self.judge.ask(question_id, prompt, list(q.options))
        p_alert = _p_alert(q, answer)
        if p_alert is None:
            decision = Decision.ESCALATE if q.alerts(answer.choice) else Decision.ALLOW
            reason = f"juge {answer.model} (sans logprobs) : {answer.choice}"
        elif p_alert >= t.high:
            decision = Decision.ESCALATE
            reason = f"juge {answer.model} : {question_id} probable (p={p_alert:.2f} ≥ {t.high})"
        elif p_alert > t.low and tier is not Tier.DEEP:
            decision = Decision.REDO_DEEP
            reason = f"juge {answer.model} : doute {question_id} (p={p_alert:.2f}), relance en Deep"
        else:
            decision = Decision.ALLOW
            reason = f"juge {answer.model} : {question_id} improbable (p={p_alert})"
        return Verdict(gate=Gate.SWITCH, decision=decision, layer=2, rule=f"judge:{question_id}",
                       reason=reason, probability=p_alert, judge_model=answer.model, latency_s=answer.latency_s,
                       subject="reply")

    # ── avis (2e appel, seulement quand HITL — consigne Nick 17.10) ──
    def _with_advice(self, reason: str, turn: Turn, context: str, call_desc: str) -> str:
        advise = getattr(self.judge, "advise", None)
        if advise is None:
            return reason  # backend sans capacité d'avis (ex. NullJudge, modèle scripté de test)
        try:
            avis = advise(f"Demande humaine : {turn.input.text}\nContexte : {context}\n"
                          f"Action suspendue en attente d'approbation : {call_desc}\n"
                          "Donne un avis COURT (2-3 phrases) : risque principal, sauvegarde ou "
                          "précaution utile, comment revenir en arrière si besoin.")
        except Exception as e:  # noqa: BLE001 — un avis qui échoue ne doit jamais bloquer le tour
            logger.warning("gates: avis HITL indisponible: %s", e)
            return reason
        return f"{reason}\nAvis : {avis}" if avis else reason

    def _context(self, turn: Turn) -> str:
        if self.context_builder is not None:
            return self.context_builder(turn)
        return _default_context(turn)


def _default_context(turn: Turn) -> str:
    parts = [f"Canal : {turn.channel}", f"Expéditeur : {turn.sender or '(inconnu)'}"]
    tool_msgs = [m for m in turn.messages if m.role == "tool"]
    if tool_msgs:
        parts.append("Outils déjà appelés ce tour :")
        parts.extend(f"- {m.name} -> {m.content[:300]}" for m in tool_msgs[-5:])
    else:
        parts.append("Outils appelés pendant le tour : aucun")
    return "\n".join(parts)


def _p_alert(q, answer: JudgeAnswer) -> float | None:
    """Même règle que `calibrate.ScoredRow.p_alert` (doit rester identique : les seuils sont ajustés
    sur cette définition). `judge.py::OpenAICompatJudge` remplit déjà les DEUX options pour une
    question Noul (complément appliqué avant de renvoyer `JudgeAnswer`), donc le repli par
    complément ci-dessous ne joue en pratique que pour un `Judge` tiers qui ne le ferait pas —
    défense en profondeur, pas le chemin normal."""
    if answer.probabilities is None:
        return None
    if q.primitive is Primitive.NOUL:
        alert_opt = q.alert[0] if q.alert else None
        if alert_opt is None:
            return None
        direct = answer.probabilities.get(alert_opt)
        if direct is not None:
            return direct
        conf = answer.probabilities.get(answer.choice)
        return (1.0 - conf) if conf is not None else None
    conf = answer.probabilities.get(answer.choice)
    if conf is None:
        return None
    return conf if q.alerts(answer.choice) else 1.0 - conf
