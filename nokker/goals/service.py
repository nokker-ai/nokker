"""Objectifs persistants et rubriques notées (chantier p2/outcomes-goal, docs/OUTCOMES-GOAL.md).

Deux drapeaux, éteints par défaut (`outcomes_enabled`, `goals_enabled`) ; éteints, ce service n'est pas construit et le
noyau ne change rien (`Kernel.goals is None`).

- **Rubrique d'une tâche** (`mode: task`, drapeau `outcomes_enabled`) : tant qu'elle est active sur le fil, chaque
  réponse candidate est notée par le correcteur (`nokker/qa/outcomes.py`) ; sous le seuil, l'agent refait une passe
  DANS le tour (`OUTCOME_NOTE`), `max_passes` fois au plus et dans la limite des appels du tour. Satisfaite : `done`.
- **Objectif** (`mode: goal`, drapeau `goals_enabled`) : une rubrique, un budget (jetons, temps, coût, tours) et des
  points d'étape. Persisté sous le fil (migration 050) : il survit aux tours et aux redémarrages. Après chaque tour, le
  correcteur note ; l'objectif s'arrête quand la rubrique est satisfaite (`done`), le budget épuisé (`budget`), sur un
  blocage (`blocked` : carte en attente, erreur, escalade, aucun progrès sur trois notes) ou sur `/goal stop`
  (`stopped`). Un point d'étape part sur le canal de l'utilisateur (Telegram relié au compte, `notify`).
  - meneur `kernel` (posé par `/goal <texte>` dans un fil, ou l'API) : le noyau enchaîne lui-même les tours
    (`GOAL_CONTINUE_BLOCK`, canal `goal`, portes sans GO écrit comme un passage Crew, budget vérifié avant chaque appel
    modèle) ; repris au démarrage ;
  - meneur `client` (la CLI : `/goal`, `metadata.cli_control`) : la CLI envoie ses tours `goal_check` / `goal_continue` ;
    le noyau répond lui-même au tour `goal_check` avec la note du correcteur et le marqueur attendu par la CLI
    (`GOAL: DONE` / `GOAL: CONTINUE`, sans marqueur quand l'objectif est arrêté : la CLI s'arrête), et le retour du
    correcteur entre au tour `goal_continue` suivant (`GOAL_FEEDBACK_NOTE`).
"""
from __future__ import annotations

import dataclasses
import logging
import re
import secrets as _secrets
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from nokker.core.turn import Decision, Gate, Turn, TurnInput, Verdict
from nokker.goals.store import DRIVERS, MODES, Goal
from nokker.qa.outcomes import (
    OUTCOME_FINAL_NOTE,
    OUTCOME_NOTE,
    Grade,
    OutcomeGrader,
    Rubric,
    RubricError,
    evidence_of,
    grade_from_json,
    make_rubric,
)
from nokker.tenancy.context import INTERNAL_TENANT, current_tenant_or_none, tenant_scope

logger = logging.getLogger("nokker.goals")

GOAL_CHANNEL = "goal"
CONTROLS = ("btw", "goal_check", "goal_continue", "loop")  # `metadata.cli_control` (nokker-apps apps/cli/src/prompts.ts)
GOAL_DONE_MARKER = "GOAL: DONE"  # marqueurs de la CLI (identiques, anglais)
GOAL_CONTINUE_MARKER = "GOAL: CONTINUE"
# Réponse d'un tour écrite par le noyau, pas par le modèle (verdict au tour, lu par `nokker traces export`).
SYNTHETIC_RULE = "turn:synthetic"
SYNTHETIC_GRADER = "grader"  # réponse « [Correcteur] … » au tour `goal_check`
SYNTHETIC_GOAL_CMD = "goal_cmd"  # réponse d'une commande `/goal`
STALL_GRADES = 3  # trois notes de suite sans progrès : bloqué
MAX_CHECKPOINTS = 20
EVIDENCE_TURNS = 6
STATUS_FR = {"draft": "rubrique à figer", "active": "en cours", "done": "atteint", "budget": "budget épuisé",
             "blocked": "bloqué", "stopped": "arrêté"}

# ─────────────────────────────── textes vus par le modèle ───────────────────────────────

# Message d'un tour enchaîné par le noyau (meneur `kernel`), même forme que `goalContinuePrompt` de la CLI.
GOAL_CONTINUE_BLOCK = """[Objectif en cours]
Objectif :
{goal}

Rubrique (un correcteur séparé la note après chaque tour) :
{rubric}

{last}
Budget restant : {left}.
Choisis la prochaine étape concrète et fais-la. À la fin de ce tour, dis en une ou deux phrases ce que tu as fait et ce qui reste.
[/Objectif en cours]"""
LAST_GRADE = "Dernière note : {total}/{max} (seuil {need}). Critères en défaut :\n{lines}\n"
NO_GRADE_YET = "Pas encore de note.\n"

# Retour du correcteur entré au tour suivant d'un objectif mené par la CLI (après `GOAL: CONTINUE`).
GOAL_FEEDBACK_NOTE = """[Retour du correcteur]
Note : {total}/{max} (seuil {need}). Critères en défaut :
{lines}
[/Retour du correcteur]"""

# Réponse du noyau au tour `goal_check` de la CLI (rangée dans l'historique comme réponse du tour).
CHECK_REPLY = "[Correcteur] Note : {total}/{max} (seuil {need}).\n{lines}\n{marker}"
CHECK_ALL_MET = "Tous les critères sont satisfaits."
CHECK_NO_GRADE = "[Correcteur] Pas encore de note pour cet objectif.\n" + GOAL_CONTINUE_MARKER
CHECK_ENDED = "[Correcteur] Objectif arrêté : {reason}."

# Réponses des commandes `/goal` tapées dans un fil (réponse du tour, rangée dans l'historique).
CMD_STARTED = ("Objectif posé : {text}\nRubrique :\n{rubric}\nBudget : {budget}.\n{how} « /goal stop » l'arrête.")
CMD_HOW_KERNEL = "Je m'y mets ; un point d'étape part sur ton canal {every}."
CMD_HOW_DRAFT = "Rubrique à proposer puis à figer (« /goal valide ») avant que je m'y mette."
CMD_STATUS = "Objectif : {text}\nÉtat : {state}\n{grade}Dépensé : {spent}."
CMD_NONE = "Aucun objectif en cours sur ce fil."
CMD_STOPPED = "Objectif arrêté à ta demande."
CMD_RESUMED = "Objectif repris."
CMD_NOT_RESUMABLE = "Rien à reprendre : l'objectif n'est pas bloqué."
CMD_FROZEN = "Rubrique figée :\n{rubric}"
CMD_NOTHING_TO_FREEZE = "Aucune rubrique proposée à figer."


def _n(n: int, singular: str, plural: str) -> str:
    """Accord du nombre (français : 0 et 1 au singulier)."""
    return f"{n} {singular if abs(int(n)) < 2 else plural}"


def _every(n: int) -> str:
    return "à chaque note" if int(n) <= 1 else f"toutes les {int(n)} notes"


def _fmt_rubric(items: list[str] | None) -> str:
    return "\n".join(f"C{i}. {t}" for i, t in enumerate(items or [], 1)) or "(aucune)"


def _lines(g: Grade, rubric: Rubric) -> str:
    return g.failing_lines(rubric)


@dataclass
class GoalService:
    store: Any  # MemoryGoals | PgGoals
    grader: OutcomeGrader | None = None
    outcomes_enabled: bool = False  # rubrique de tâche + passes dans le tour + outil rubric_propose
    goals_enabled: bool = False  # objectifs sur plusieurs tours
    default_budget: dict[str, Any] = field(default_factory=lambda: {"tokens": 400_000, "seconds": 7200, "turns": 20})
    checkpoint_every: int = 3
    checkpoint_s: float = 1800.0
    notify: list[Callable[[Goal, str], None]] = field(default_factory=list)  # (objectif, texte) → canal de l'utilisateur
    scrub: Callable[[str], str] | None = None  # caviardage final des tours
    prices: Any = None  # catalogue de prix (coût du budget) ; None : celui du ledger du noyau
    clock: Callable[[], float] = field(default=time.time)
    submit: Callable[..., Any] | None = None  # exécution de fond (défaut : 2 fils)
    tenants: Callable[[], list[str]] | None = None  # locataires connus (reprise) ; None : ceux du magasin mémoire
    kernel: Any = None  # posé par le serveur après la construction du noyau
    run_turn: Callable[[Any, TurnInput], Turn] | None = None  # (noyau, entrée) ; défaut : noyau.run_turn

    def __post_init__(self) -> None:
        if self.submit is None:
            self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="nokker-goal")
            self.submit = self._pool.submit

    # ─────────── état ───────────

    def _now(self) -> float:
        return float(self.clock())

    def _tenant(self) -> str:
        return current_tenant_or_none() or INTERNAL_TENANT

    def current(self, thread_id: str) -> Goal | None:
        try:
            return self.store.current(thread_id)
        except Exception:  # noqa: BLE001 — un magasin en panne ne casse jamais un tour
            logger.warning("objectifs : lecture en échec", exc_info=True)
            return None

    def _save(self, g: Goal) -> None:
        g.updated_at = self._now()
        self.store.save(g)

    def start(self, thread_id: str, owner: str, text: str, *, criteria: Any = None, mode: str = "goal",
              driver: str = "kernel", budget: dict[str, Any] | None = None, checkpoint_every: int | None = None,
              channel: str = "api", start_index: int = 0, proposed: Any = None) -> Goal:
        """Pose l'objectif (ou la rubrique de tâche) du fil ; un objectif vivant d'avant est arrêté (« remplacé »)."""
        if mode not in MODES:
            raise RubricError(f"mode inconnu : {mode}")
        if driver not in DRIVERS:
            raise RubricError(f"meneur inconnu : {driver}")
        if mode == "goal" and not self.goals_enabled:
            raise RubricError("mode objectif éteint sur cette instance")
        if mode == "task" and not self.outcomes_enabled:
            raise RubricError("rubriques éteintes sur cette instance")
        text = " ".join(str(text or "").split())[:2000]
        rubric = make_rubric(criteria).to_json() if criteria else None
        if rubric is None and text and mode == "goal" and proposed is None:
            rubric = [text[:400]]  # sans critère : l'objectif lui-même, à satisfaire
        prop = make_rubric(proposed, "agent").to_json() if proposed else None
        if not text and not rubric and not prop:
            raise RubricError("objectif vide")
        b = dict(self.default_budget)
        for k, v in (budget or {}).items():
            if k in ("tokens", "seconds", "turns") and v is not None:
                b[k] = max(0, int(v))
            elif k == "cost" and v is not None:
                b[k] = max(0.0, float(v))
            elif k == "currency" and v:
                b[k] = str(v)[:8].upper()
        prev = self.current(thread_id)
        if prev is not None:
            self._end(prev, "stopped", "remplacé par un nouvel objectif", report=False)
        now = self._now()
        g = Goal(id="gl_" + _secrets.token_hex(8), thread_id=thread_id, owner=owner or "", text=text or "", mode=mode,
                 driver=driver, status="active" if rubric else "draft", rubric=rubric, rubric_source="user",
                 proposed=prop, budget=b, checkpoint_every=max(1, int(checkpoint_every or self.checkpoint_every)),
                 checkpoint_s=self.checkpoint_s, channel=channel, start_index=start_index, created_at=now,
                 updated_at=now, started_at=now, last_checkpoint_at=now)
        self._save(g)
        if g.status == "active" and g.mode == "goal" and g.driver == "kernel":
            self._schedule(g)
        return g

    def stop(self, thread_id: str, reason: str = "arrêté à ta demande") -> Goal | None:
        g = self.current(thread_id)
        if g is None:
            return None
        self._end(g, "stopped", reason)
        return g

    def resume(self, thread_id: str) -> Goal | None:
        g = self.current(thread_id)
        if g is None or g.status != "blocked":
            return None
        g.status, g.stop_reason, g.recent_totals = "active", "", []
        self._save(g)
        if g.mode == "goal" and g.driver == "kernel":
            self._schedule(g)
        return g

    def propose(self, thread_id: str, owner: str, criteria: Any, objective: str = "") -> Goal:
        """Rubrique PROPOSÉE par l'agent (outil `rubric_propose`) : rangée à côté, jamais notée tant que l'utilisateur
        ne l'a pas figée (`freeze`)."""
        prop = make_rubric(criteria, "agent").to_json()
        g = self.current(thread_id)
        if g is None:
            return self.start(thread_id, owner, objective or "", mode="task" if self.outcomes_enabled else "goal",
                              proposed=prop)
        g.proposed = prop
        self._save(g)
        return g

    def freeze(self, thread_id: str) -> Goal | None:
        g = self.current(thread_id)
        if g is None or not g.proposed:
            return None
        g.rubric, g.rubric_source, g.proposed = g.proposed, "agent", None
        was_draft = g.status == "draft"
        if was_draft:
            g.status = "active"
        self._save(g)
        if was_draft and g.mode == "goal" and g.driver == "kernel":
            self._schedule(g)
        return g

    def _end(self, g: Goal, status: str, reason: str, *, report: bool = True) -> None:
        g.status, g.stop_reason, g.ended_at = status, reason, self._now()
        if report:
            self._checkpoint(g, final=True)
        self._save(g)

    # ─────────── budget ───────────

    def _prices(self) -> Any:
        if self.prices is not None:
            return self.prices
        return getattr(getattr(self.kernel, "ledger", None), "prices", None)

    def _turn_usage(self, turn: Turn) -> tuple[int, dict[str, float], bool]:
        from nokker.ledger.turn_cost import turn_cost

        tokens = sum(int(c.usage.prompt or 0) + int(c.usage.completion or 0) for c in turn.calls)
        try:
            cost = turn_cost(turn.calls, self._prices())
        except Exception:  # noqa: BLE001
            return tokens, {}, bool(turn.calls)
        return tokens, dict(cost.get("cost") or {}), bool(cost.get("unpriced_models"))

    def over_budget(self, g: Goal, running: Turn | None = None) -> str | None:
        b = g.budget or {}
        tokens = int(g.spent.get("tokens") or 0)
        cost = dict(g.spent.get("cost") or {})
        unpriced = bool(g.spent.get("unpriced"))
        if running is not None:
            t, c, u = self._turn_usage(running)
            tokens += t
            for k, v in c.items():
                cost[k] = cost.get(k, 0.0) + v
            unpriced = unpriced or u
        if b.get("tokens") and tokens >= int(b["tokens"]):
            return f"budget de jetons atteint ({tokens} sur {b['tokens']})"
        if b.get("seconds") and self._now() - g.started_at >= float(b["seconds"]):
            return f"budget de temps atteint ({int(self._now() - g.started_at)} s sur {b['seconds']} s)"
        if b.get("turns") and int(g.spent.get("auto_turns") or 0) >= int(b["turns"]) and running is None:
            return f"nombre de tours atteint ({g.spent.get('auto_turns')} sur {b['turns']})"
        if b.get("cost"):
            cur = b.get("currency") or "EUR"
            if cost.get(cur, 0.0) >= float(b["cost"]):
                return f"budget de coût atteint ({cost.get(cur, 0.0):.4f} {cur} sur {b['cost']} {cur})"
            if tokens and (unpriced or set(cost) - {cur}):
                return f"coût non vérifiable en {cur} : objectif arrêté par prudence"
        return None

    def _left(self, g: Goal) -> str:
        b, parts = g.budget or {}, []
        if b.get("tokens"):
            parts.append(_n(max(0, int(b['tokens']) - int(g.spent.get('tokens') or 0)), "jeton", "jetons"))
        if b.get("seconds"):
            parts.append(f"{max(0, int(float(b['seconds']) - (self._now() - g.started_at)) // 60)} min")
        if b.get("turns"):
            parts.append(_n(max(0, int(b['turns']) - int(g.spent.get('auto_turns') or 0)), "tour", "tours"))
        if b.get("cost"):
            cur = b.get("currency") or "EUR"
            parts.append(f"{max(0.0, float(b['cost']) - float((g.spent.get('cost') or {}).get(cur, 0.0))):.2f} {cur}")
        return ", ".join(parts) or "sans limite"

    def _spent_text(self, g: Goal) -> str:
        cost = ", ".join(f"{v:.4f} {k}" for k, v in sorted((g.spent.get("cost") or {}).items()))
        mins = int((((g.ended_at or self._now()) - g.started_at)) // 60)
        return f"{_n(int(g.spent.get('tokens') or 0), 'jeton', 'jetons')}, {mins} min, " \
            f"{_n(int(g.spent.get('turns') or 0), 'tour', 'tours')}" + \
            (f", {cost}" if cost else "")

    def _budget_text(self, b: dict[str, Any]) -> str:
        parts = []
        if b.get("tokens"):
            parts.append(_n(int(b['tokens']), "jeton", "jetons"))
        if b.get("seconds"):
            parts.append(f"{int(b['seconds']) // 60} min")
        if b.get("turns"):
            parts.append(_n(int(b['turns']), "tour", "tours"))
        if b.get("cost"):
            parts.append(f"{b['cost']} {b.get('currency') or 'EUR'}")
        return ", ".join(parts) or "sans limite"

    # ─────────── crochets du noyau ───────────

    def before_turn(self, kernel: Any, turn: Turn, inp: TurnInput) -> str | None:
        """Avant tout appel modèle. Rend la réponse du tour quand le noyau répond lui-même (commande `/goal`, tour
        `goal_check` d'un objectif noté), None sinon (tour ordinaire). Une réponse du noyau est marquée au tour
        (verdict `turn:synthetic`, raison `grader` | `goal_cmd`) : l'export des traces la distingue d'une réponse du
        modèle sans heuristique (docs/TRACES.md)."""
        direct = self._before_turn(kernel, turn, inp)
        if direct is not None:
            kind = SYNTHETIC_GRADER if (getattr(inp, "control", "") or "") == "goal_check" else SYNTHETIC_GOAL_CMD
            kernel._log_verdicts(turn, [Verdict(gate=Gate.SWITCH, decision=Decision.ALLOW, layer=0,
                                                rule=SYNTHETIC_RULE, reason=kind)])
        return direct

    def _before_turn(self, kernel: Any, turn: Turn, inp: TurnInput) -> str | None:
        control = getattr(inp, "control", "") or ""
        if control:
            # Étiquette du tour (`metadata.cli_control`), stockée avec ses verdicts.
            kernel._log_verdicts(turn, [Verdict(gate=Gate.SWITCH, decision=Decision.ALLOW, layer=0,
                                                rule="turn:control", reason=control)])
        text = (turn.input.text or "").strip()
        if not control and re.match(r"^/goal(\s|$)", text):
            return self._command(kernel, turn, text[5:].strip())
        if control == "goal_check":
            g = self.current(turn.thread_id)
            if g is not None and g.mode == "goal" and g.rubric:
                return self._check_reply(kernel, g)
            if g is None:
                latest = self._latest(turn.thread_id)
                if latest is not None and latest.mode == "goal" and latest.status in ("budget", "stopped", "done"):
                    if latest.status == "done":
                        return self._check_reply(kernel, latest)
                    return CHECK_ENDED.format(reason=latest.stop_reason or STATUS_FR[latest.status])
        return None

    def _latest(self, thread_id: str) -> Goal | None:
        try:
            return self.store.latest(thread_id)
        except Exception:  # noqa: BLE001
            return None

    def _check_reply(self, kernel: Any, g: Goal) -> str:
        if g.status in ("budget", "stopped", "blocked"):
            return CHECK_ENDED.format(reason=g.stop_reason or STATUS_FR[g.status])
        grade = grade_from_json(g.last_grade)
        if grade is None:
            return CHECK_NO_GRADE
        rubric = make_rubric(g.rubric, g.rubric_source)
        done = grade.passed
        if not done and g.driver == "client":
            kernel.add_owner_note(g.thread_id, GOAL_FEEDBACK_NOTE.format(
                total=grade.total, max=grade.max, need=grade.need, lines=_lines(grade, rubric)))
        return CHECK_REPLY.format(total=grade.total, max=grade.max, need=grade.need,
                                  lines=CHECK_ALL_MET if not grade.failing() else _lines(grade, rubric),
                                  marker=GOAL_DONE_MARKER if done else GOAL_CONTINUE_MARKER)

    def _command(self, kernel: Any, turn: Turn, arg: str) -> str | None:
        word = arg.split("\n", 1)[0].strip().lower()
        if not self.goals_enabled and word not in ("valide", "valider", "fige", "figer"):
            return None  # objectifs éteints (rubriques seules) : `/goal …` reste un message ordinaire
        if arg == "":
            g = self.current(turn.thread_id) or self._latest(turn.thread_id)
            if g is None:
                return CMD_NONE
            grade = grade_from_json(g.last_grade)
            gtext = f"Note : {grade.total}/{grade.max} (seuil {grade.need}).\n" if grade is not None else ""
            state = STATUS_FR.get(g.status, g.status) + (f" ({g.stop_reason})" if g.stop_reason else "")
            return CMD_STATUS.format(text=g.text, state=state, grade=gtext, spent=self._spent_text(g))
        if word in ("stop", "arrête", "arrete", "clear"):
            return CMD_STOPPED if self.stop(turn.thread_id) is not None else CMD_NONE
        if word in ("reprendre", "resume"):
            return CMD_RESUMED if self.resume(turn.thread_id) is not None else CMD_NOT_RESUMABLE
        if word in ("valide", "valider", "fige", "figer"):
            g = self.freeze(turn.thread_id)
            return CMD_FROZEN.format(rubric=_fmt_rubric(g.rubric)) if g is not None else CMD_NOTHING_TO_FREEZE
        lines = [ln.strip() for ln in arg.splitlines() if ln.strip()]
        try:
            g = self.start(turn.thread_id, turn.sender or "", lines[0], criteria=lines[1:] or None, mode="goal",
                           driver="kernel", channel=turn.channel, start_index=turn.index + 1)
        except RubricError as e:
            return f"Objectif refusé : {e}."
        how = CMD_HOW_KERNEL.format(every=_every(g.checkpoint_every)) if g.status == "active" else CMD_HOW_DRAFT
        return CMD_STARTED.format(text=g.text, rubric=_fmt_rubric(g.rubric), budget=self._budget_text(g.budget),
                                  how=how)

    def rubric_for_turn(self, turn: Turn, inp: TurnInput) -> tuple[Goal, Rubric] | None:
        """Rubrique de tâche active sur le fil (passes DANS le tour), ou None."""
        if not self.outcomes_enabled or self.grader is None:
            return None
        if (getattr(inp, "control", "") or "") in ("btw", "goal_check"):
            return None
        g = self.current(turn.thread_id)
        if g is None or g.mode != "task" or g.status != "active" or not g.rubric:
            return None
        return g, make_rubric(g.rubric, g.rubric_source)

    def grade(self, kernel: Any, turn: Turn, g: Goal, rubric: Rubric, deliverable: str, evidence: str,
              pass_no: int) -> Grade | None:
        """Une note du correcteur, tracée : jetons au tour et au ledger, verdict `judge:outcome`, ligne de notation."""
        assert self.grader is not None
        try:
            res = self.grader.grade(rubric, deliverable, evidence)
        except Exception:  # noqa: BLE001 — un correcteur qui tombe ne note pas
            logger.warning("correcteur en échec", exc_info=True)
            res = None
        if res is None:
            kernel._log_verdicts(turn, [Verdict(gate=Gate.SWITCH, decision=Decision.ALLOW, layer=2,
                                                rule="judge:outcome:skipped",
                                                reason="correcteur muet ou réponse illisible : pas de note",
                                                subject="reply")])
            return None
        call = kernel._record_judge_call(turn, self.grader, res.model, res.usage, res.latency_s,
                                         "juge : note de la rubrique")
        self._trace_grade(kernel, turn, rubric, deliverable, evidence, res, call)
        kernel._log_verdicts(turn, [Verdict(
            gate=Gate.SWITCH, decision=Decision.ALLOW if res.passed else Decision.REDO_DEEP, layer=2,
            rule="judge:outcome", reason=("rubrique : " + res.summary() + "\n" + "\n".join(
                f"{s.id} {s.score} : {s.justification}" for s in res.scores))[:1500],
            judge_model=res.model, latency_s=res.latency_s, subject="reply")])
        g.gradings += 1
        g.last_grade = res.to_json()
        g.recent_totals = (g.recent_totals + [res.total])[-STALL_GRADES:]
        try:
            self.store.add_grade(g.id, g.thread_id, turn.index, pass_no, res.to_json(), self._now())
        except Exception:  # noqa: BLE001 — la note reste au verdict du tour
            logger.warning("objectifs : note non écrite", exc_info=True)
        return res

    def _trace_grade(self, kernel: Any, turn: Turn, rubric: Rubric, deliverable: str, evidence: str, res: Grade,
                     call: Any) -> None:
        """Ligne de trace à part (`meta.side = "grader"`) : le message exact du correcteur, sa réponse, son palier
        configuré (`grader_tier`) et le modèle qui a réellement répondu (`grader_model`). Sans enregistreur : rien."""
        if getattr(kernel, "traces", None) is None or not hasattr(kernel.traces, "side"):
            return
        from nokker.core.ports import Completion
        from nokker.core.turn import LLMCall, Message, Tier, Usage

        assert self.grader is not None
        tier = getattr(self.grader, "tier", None) or Tier.SHIP
        if call is None:  # correcteur sans usage déclaré : la ligne garde palier et modèle, jetons à zéro
            call = LLMCall(tier=tier, model=res.model, reasoning=False, prefix_sha256="", layer_tokens={},
                           usage=Usage(), latency_s=res.latency_s, route_reason="juge : note de la rubrique")
        prompt = [{"role": "user", "content": self.grader.prompt(rubric, deliverable, evidence)}]
        comp = Completion(message=Message("assistant", res.raw), usage=call.usage, model=res.model,
                          latency_s=res.latency_s)
        kernel._trace("side", turn, "grader", prompt, comp, call,
                      {"grader_tier": str(getattr(self.grader, "tier_name", "") or "judge"),
                       "grader_model": str(res.model or "")})

    def in_turn(self, kernel: Any, turn: Turn, goal: Goal, rubric: Rubric, candidate: str, passes: int,
                can_retry: bool) -> tuple[str | None, str]:
        """Rubrique de tâche, réponse candidate du tour : (note à renvoyer à l'agent, candidate) ; note None = la
        réponse part (satisfaite, correcteur muet, ou plus de passe : alors avec `OUTCOME_FINAL_NOTE`)."""
        res = self.grade(kernel, turn, goal, rubric, candidate, evidence_of([turn]), passes)
        if res is None:
            self._save(goal)
            return None, candidate
        if res.passed:
            goal.status, goal.stop_reason, goal.ended_at = "done", "rubrique satisfaite", self._now()
            self._save(goal)
            return None, candidate
        max_passes = int(getattr(self.grader, "max_passes", 2))
        if passes < max_passes and can_retry:
            self._save(goal)
            return OUTCOME_NOTE.format(total=res.total, max=res.max, need=res.need, lines=_lines(res, rubric),
                                       next=passes + 1, passes=max_passes), candidate
        why = "plus de passe" if passes >= max_passes else "plus d'appel ni de budget dans ce tour"
        self._save(goal)
        return None, candidate + OUTCOME_FINAL_NOTE.format(total=res.total, max=res.max, need=res.need,
                                                           passes=passes + 1, why=why, lines=_lines(res, rubric))

    def after_turn(self, kernel: Any, turn: Turn, inp: TurnInput) -> None:
        """Fin de tour (après l'enregistrement) : dépense, note de l'objectif, arrêt ou tour suivant, point d'étape."""
        g = self.current(turn.thread_id)
        if g is None or g.status != "active":
            return
        control = getattr(inp, "control", "") or ""
        tokens, cost, unpriced = self._turn_usage(turn)
        g.spent["tokens"] = int(g.spent.get("tokens") or 0) + tokens
        c = dict(g.spent.get("cost") or {})
        for k, v in cost.items():
            c[k] = round(c.get(k, 0.0) + v, 6)
        g.spent["cost"] = c
        g.spent["unpriced"] = bool(g.spent.get("unpriced")) or unpriced
        g.spent["turns"] = int(g.spent.get("turns") or 0) + 1
        if g.mode != "goal" or control in ("btw", "goal_check") or not g.rubric:
            self._save(g)
            return
        if turn.error == "cancelled":
            self._end(g, "stopped", "tour annulé")
            return
        blocked = None
        if turn.approval_ids or turn.pending_approvals:
            blocked = "en attente d'une carte d'approbation"
        elif turn.error == "stopped":
            blocked = None  # arrêt du budget pendant le tour : tranché plus bas
        elif turn.error:
            blocked = f"erreur du tour ({str(turn.error)[:120]})"
        elif turn.escalated:
            blocked = "escaladé à un humain"
        if blocked is None and turn.reply.strip():
            rubric = make_rubric(g.rubric, g.rubric_source)
            evidence = evidence_of(self._goal_turns(kernel, g, turn))
            res = self.grade(kernel, turn, g, rubric, turn.reply, evidence, g.gradings)
            if res is not None and res.passed:
                self._end(g, "done", "rubrique satisfaite")
                return
            if res is not None and len(g.recent_totals) >= STALL_GRADES and len(set(g.recent_totals)) == 1:
                blocked = f"aucun progrès sur {STALL_GRADES} notes"
        if blocked:
            self._end(g, "blocked", blocked)
            return
        reason = self.over_budget(g)
        if reason:
            self._end(g, "budget", reason)
            return
        if g.gradings and (g.gradings % max(1, g.checkpoint_every) == 0
                           or self._now() - g.last_checkpoint_at >= g.checkpoint_s):
            self._checkpoint(g)
        self._save(g)
        if g.driver == "kernel":
            self._schedule(g)

    def _goal_turns(self, kernel: Any, g: Goal, turn: Turn) -> list[Turn]:
        try:
            hist = [t for t in kernel.store.history(g.thread_id) if t.index >= g.start_index and t.index != turn.index]
        except Exception:  # noqa: BLE001
            hist = []
        return [*hist[-(EVIDENCE_TURNS - 1):], turn]

    # ─────────── points d'étape ───────────

    def checkpoint_text(self, g: Goal, final: bool = False) -> str:
        grade = grade_from_json(g.last_grade)
        n = len(g.checkpoints) + 1
        head = {"done": "Objectif atteint", "budget": "Objectif arrêté : budget épuisé",
                "blocked": "Objectif bloqué", "stopped": "Objectif arrêté"}.get(g.status) if final else None
        title = head or f"Objectif — point d'étape {n}"
        lines = [f"{title} : {g.text}"]
        if final and g.stop_reason:
            lines.append(f"Motif : {g.stop_reason}.")
        if grade is not None and g.rubric:
            rubric = make_rubric(g.rubric, g.rubric_source)
            lines.append(f"Note : {grade.total}/{grade.max} (seuil {grade.need}).")
            if grade.failing():
                lines.append("Critères en défaut :\n" + _lines(grade, rubric))
        lines.append(f"Dépensé : {self._spent_text(g)}.")
        if g.status == "blocked":
            lines.append("« /goal reprendre » relance l'objectif.")
        return "\n".join(lines)

    def _checkpoint(self, g: Goal, final: bool = False) -> None:
        text = self.checkpoint_text(g, final)
        if self.scrub is not None:
            text = self.scrub(text)
        from nokker.plan.delivery import strip_capability_links

        text = strip_capability_links(text)
        g.last_checkpoint_at = self._now()
        sent = []
        for n in self.notify:
            try:
                n(g, text)
                sent.append(type(n).__name__)
            except Exception:  # noqa: BLE001 — un canal en panne n'arrête pas l'objectif
                logger.warning("objectifs : point d'étape non envoyé", exc_info=True)
        g.checkpoints = (g.checkpoints + [{"at": g.last_checkpoint_at, "status": g.status, "text": text,
                                           "sent": sent}])[-MAX_CHECKPOINTS:]

    # ─────────── tours enchaînés par le noyau ───────────

    def _schedule(self, g: Goal) -> None:
        if self.kernel is None or not self.goals_enabled:
            return
        try:
            self.submit(self._continue, self._tenant(), g.id)
        except Exception:  # noqa: BLE001
            logger.warning("objectifs : tour suivant non planifié", exc_info=True)

    def _continue(self, tenant: str, goal_id: str) -> Turn | None:
        with tenant_scope(tenant):
            try:
                return self._continue_in_scope(goal_id)
            except Exception:  # noqa: BLE001 — un tour enchaîné qui lève bloque l'objectif, sans tuer le fil de fond
                logger.exception("objectifs : tour enchaîné en échec")
                g = self.store.get(goal_id)
                if g is not None and g.status == "active":
                    self._end(g, "blocked", "tour enchaîné en échec")
                return None

    def continue_text(self, g: Goal) -> str:
        rubric = make_rubric(g.rubric, g.rubric_source)
        grade = grade_from_json(g.last_grade)
        last = (LAST_GRADE.format(total=grade.total, max=grade.max, need=grade.need, lines=_lines(grade, rubric))
                if grade is not None else NO_GRADE_YET)
        return GOAL_CONTINUE_BLOCK.format(goal=g.text, rubric=rubric.render(), last=last, left=self._left(g))

    def _continue_in_scope(self, goal_id: str) -> Turn | None:
        from nokker.agents.engine import AgentRunGates

        g = self.store.get(goal_id)
        if g is None or g.status != "active" or g.driver != "kernel" or g.mode != "goal" or not g.rubric:
            return None
        reason = self.over_budget(g)
        if reason:
            self._end(g, "budget", reason)
            return None
        g.spent["auto_turns"] = int(g.spent.get("auto_turns") or 0) + 1
        self._save(g)
        kernel = self.kernel

        def stop(turn: Turn) -> str | None:
            cur = self.store.get(goal_id)
            if cur is None or cur.status != "active":
                return "objectif arrêté"
            return self.over_budget(cur, running=turn)

        # Portes du locataire sans GO écrit (le texte de l'objectif est enregistré, ce n'est pas un humain qui parle
        # maintenant), comme un passage Crew ; budget vérifié avant chaque appel modèle.
        rk = dataclasses.replace(kernel, gates=AgentRunGates(inner=kernel.gates, rules=[]), stop_check=stop)
        inp = TurnInput(thread_id=g.thread_id, user_text=self.continue_text(g), channel=GOAL_CHANNEL,
                        sender=g.owner or None, control="goal_continue")
        run = self.run_turn or (lambda k, i: k.run_turn(i))
        return run(rk, inp)

    def recover(self) -> int:
        """Au démarrage : chaque objectif actif mené par le noyau reprend (un tour enchaîné par objectif)."""
        n = 0
        tenants = self.tenants() if self.tenants is not None else (getattr(self.store, "tenants", lambda: [])()
                                                                  or [INTERNAL_TENANT])
        for tid in tenants:
            try:
                with tenant_scope(tid):
                    for g in self.store.live_kernel():
                        self.submit(self._continue, tid, g.id)
                        n += 1
            except Exception:  # noqa: BLE001 — un locataire en échec n'arrête pas les autres
                logger.exception("objectifs : reprise impossible pour %s", tid)
        return n


def goal_json(g: Goal, grades: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    out = {k: v for k, v in g.to_doc().items() if k not in ("recent_totals",)}
    out["rubric"] = [{"id": f"C{i}", "text": t} for i, t in enumerate(g.rubric or [], 1)] or None
    out["proposed"] = [{"id": f"C{i}", "text": t} for i, t in enumerate(g.proposed or [], 1)] or None
    if grades is not None:
        out["grades"] = grades
    return out


@dataclass
class TelegramGoalNotifier:
    """Point d'étape → Telegram relié au compte du locataire du fil (bot de la bêta, `sendMessage` seulement), comme les
    cartes des testeurs (`nokker/telegram/notify.py`). Sans compte ou sans chat relié : rien."""

    tenancy: Any
    bot: Any

    def __call__(self, g: Goal, text: str) -> None:
        from nokker.tenancy.context import tenant_of_thread

        account = self.tenancy.account_for_tenant(tenant_of_thread(g.thread_id))
        account_id = account.account_id if account is not None else (g.owner or None)
        if not account_id:
            return
        for chat in self.tenancy.identities_of(account_id, "telegram"):
            self.bot.send(chat, text)


__all__ = ["CHECK_ENDED", "CHECK_NO_GRADE", "CHECK_REPLY", "CONTROLS", "GOAL_CHANNEL", "GOAL_CONTINUE_BLOCK",
           "GOAL_CONTINUE_MARKER", "GOAL_DONE_MARKER", "GOAL_FEEDBACK_NOTE", "SYNTHETIC_GOAL_CMD", "SYNTHETIC_GRADER",
           "SYNTHETIC_RULE", "GoalService", "TelegramGoalNotifier", "goal_json"]
