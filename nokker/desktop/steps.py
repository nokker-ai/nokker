"""Pas validés à l'écran (B2 « Nokker te montre », docs/DESKTOP.md §12) — mode `session` seulement.

Une action du modèle (`desktop_act`, `desktop_open`) n'est pas exécutée tout de suite : elle ouvre un PAS, montré à la
personne dans une bulle `pointer` (cible `{"kind": "desktop", …}`), qui répond :
  - `go` (« Vas-y ») : accord pour CE pas seulement (id du pas + `args_sha256`), le nœud agit ;
  - `self` (« Je le fais moi-même ») : le nœud n'agit pas, il observe la cible seule (`desktop.observe`) ;
  - `stop` (« Arrête ») : rien n'est fait.
Au-delà de `wait_s`, l'outil rend `desktop_pending` et le suivi de P7b (`PhoneFollow`, repris tel quel) lance le tour de
suite `DESKTOP_CONTINUE` à la clôture du pas. Ce module est la « source » de ce suivi : même forme que le service du
téléphone (`store.task`, `_expire`, `clock`, `sleep`). Un seul pas en attente par conversation. Tout vit en mémoire :
après un redémarrage, le pas expire sans tour de suite."""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from nokker.desktop import texts as T
from nokker.qa.untrusted import inline

logger = logging.getLogger("nokker.desktop.steps")

STEP_TTL_S = 600.0            # un pas sans réponse expire au bout de 10 min
OBSERVE_S = 300               # « moi-même » : 5 min d'observation au plus
ACTIONS = [{"id": "go", "label": "Vas-y", "primary": True}, {"id": "self", "label": "Je le fais moi-même"},
           {"id": "stop", "label": "Arrête"}]
TITLE = "Étape {n}"
TERMINAL = ("done", "failed", "expired", "cancelled")


def args_sha(args: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(args, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


@dataclass
class Step:
    step_id: str
    thread_id: str
    turn_index: int
    sender: str
    channel: str
    op: str
    args: dict[str, Any]
    args_sha: str
    target: str            # libellé borné de la cible (rôle « nom »), pour les textes et la bulle
    node: Any
    created_at: float
    deadline: float
    no: int = 1
    status: str = "waiting"   # waiting | running | done | failed | expired | cancelled
    answer: str | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    bubble_id: str | None = None
    event: threading.Event = field(default_factory=threading.Event, repr=False)

    @property
    def action(self) -> str:
        return T.ACTION_WORDS.get(self.op, self.op)


@dataclass
class DesktopSteps:
    svc: Any  # DesktopService
    # Bulle `pointer` (posés par le câblage, BubbleHub) : ouvrir → id ; mettre à jour (état, fermer ?)
    open_bubble: Callable[[Step, dict[str, Any]], str | None] | None = None
    update_bubble: Callable[[str, dict[str, Any], bool], None] | None = None
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    ttl_s: float = STEP_TTL_S
    observe_s: int = OBSERVE_S
    _steps: dict[str, Step] = field(default_factory=dict, init=False)
    _count: dict[str, int] = field(default_factory=dict, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    # ── forme attendue par PhoneFollow (P7b) ──
    @property
    def store(self) -> DesktopSteps:
        return self

    def task(self, step_id: str) -> Step | None:
        return self._steps.get(step_id)

    def _expire(self, step: Step, now: float) -> Step:
        with self._lock:
            if step.status == "waiting" and now > step.deadline:
                step.status = "expired"
                step.event.set()
                self._close_bubble(step, "expired")
        return step

    # ── ouverture ──
    def open(self, *, thread_id: str, turn_index: int, sender: str, channel: str, node: Any, op: str,
             args: dict[str, Any], target: str, preview: str | None) -> Step | None:
        """Ouvre le pas et sa bulle ; None si un pas attend déjà la personne dans cette conversation."""
        now = self.clock()
        with self._lock:
            for s in self._steps.values():
                if s.thread_id == thread_id and s.status in ("waiting", "running") and now <= s.deadline:
                    return None
            n = self._count.get(thread_id, 0) + 1
            self._count[thread_id] = n
            step = Step(step_id="ds_" + secrets.token_hex(8), thread_id=thread_id, turn_index=turn_index,
                        sender=sender, channel=channel, op=op, args=dict(args), args_sha=args_sha(args),
                        target=target, node=node, created_at=now, deadline=now + self.ttl_s, no=n)
            self._steps[step.step_id] = step
            if len(self._steps) > 2000:
                for k in [k for k, v in self._steps.items() if v.status in TERMINAL][:1000]:
                    del self._steps[k]
        state = {"target": {"kind": "desktop", "app": inline(args.get("app"), 60),
                            "window": inline(args.get("window"), 200), "role": inline(args.get("expect_role"), 40),
                            "name": inline(args.get("expect_name"), 120), "action": op.split(".", 1)[1],
                            "preview": preview},
                 "caption": f"{step.action} : {target}", "step": n, "steps": None, "waiting": False, "answer": None,
                 "step_id": step.step_id, "title": TITLE.format(n=n)}
        if self.open_bubble is not None:
            try:
                step.bubble_id = self.open_bubble(step, state)
            except Exception:  # noqa: BLE001 — sans bulle, la personne ne peut pas répondre : le pas expirera
                logger.exception("bureau : bulle du pas %s non ouverte", step.step_id)
        return step

    # ── réponse de la personne (membre du fil : contrôlé par la route des bulles) ──
    def answer(self, step_id: str, user: str | None, action: str) -> dict[str, Any]:
        if action not in ("go", "self", "stop"):
            raise LookupError(f"action inconnue : {action}")
        with self._lock:
            step = self._steps.get(step_id)
            if step is None:
                raise LookupError("pas inconnu")
            if step.status != "waiting" or self.clock() > step.deadline:
                return {"accepted": False, "status": step.status}
            if args_sha(step.args) != step.args_sha:  # défense : l'accord ne vaut que pour CE pas, tel que montré
                step.status, step.error = "failed", {"code": "stale_target"}
                step.event.set()
                return {"accepted": False, "status": step.status}
            step.answer = action
            if action == "stop":
                step.status, step.error = "failed", {"code": "stopped_by_person"}
                step.event.set()
                self._close_bubble(step, "stop")
                return {"accepted": True, "status": "stopped"}
            step.status = "running"
        if step.bubble_id and self.update_bubble is not None:
            self.update_bubble(step.bubble_id, {"waiting": True, "answer": action}, False)
        threading.Thread(target=self._run, args=(step,), name="desktop-step", daemon=True).start()
        return {"accepted": True, "status": "running"}

    def _run(self, step: Step) -> None:
        try:
            if step.answer == "go":
                task = self.svc.submit(step.node, step.op, step.args, wait_s=max(self.svc.wait_s, 60.0))
            else:  # self : observer la cible seule
                obs = {k: step.args[k] for k in ("job", "window", "app", "snap_id", "ref", "expect_role",
                                                 "expect_name") if k in step.args}
                task = self.svc.submit(step.node, "desktop.observe", {**obs, "timeout_s": self.observe_s},
                                       wait_s=self.observe_s + 20.0)
            with self._lock:
                if task.status == "done" and task.result is not None:
                    step.status, step.result = "done", {**task.result, "by": step.answer}
                elif task.status == "failed":
                    step.status, step.error = "failed", dict(task.error or {"code": "failed"})
                else:
                    step.status, step.error = "failed", {"code": "timeout"}
        except Exception:  # noqa: BLE001
            logger.exception("bureau : pas %s en échec", step.step_id)
            with self._lock:
                step.status, step.error = "failed", {"code": "failed"}
        step.event.set()
        self._close_bubble(step, step.status)

    def _close_bubble(self, step: Step, outcome: str) -> None:
        if step.bubble_id and self.update_bubble is not None:
            try:
                self.update_bubble(step.bubble_id, {"waiting": False, "answer": step.answer, "outcome": outcome}, True)
            except Exception:  # noqa: BLE001
                logger.exception("bureau : bulle du pas %s non fermée", step.step_id)


# ─────────────────────────────── textes du résultat (même tour, ou tour de suite) ───────────────────────────────
def outcome_text(step: Step, status: str, fmt: Callable[[str, dict[str, Any]], str]) -> str:
    """Texte vu par le modèle pour un pas clos. `fmt(op, résultat)` = la mise en forme des résultats de B1."""
    action, target = step.action, step.target
    if status == "done" and step.result is not None:
        if step.result.get("by") == "self":
            return (T.STEP_SELF_SEEN if step.result.get("changed") else T.STEP_SELF_UNSEEN).format(action=action,
                                                                                                   target=target)
        return T.STEP_DONE + fmt(step.op, step.result)
    code = (step.error or {}).get("code") if status == "failed" else None
    if code == "stopped_by_person":
        return T.STEP_STOPPED.format(action=action, target=target)
    if status == "failed":
        why = T.WHY.get(str(code), T.WHY["failed"])
        if "{" in why:
            why = why.format_map(_Blank({"code": inline(code, 40)}))
        return T.STEP_FAILED.format(action=action, target=target, why=why)
    return T.STEP_EXPIRED.format(action=action, target=target)


class _Blank(dict):
    def __missing__(self, key: str) -> str:
        return "?"


def continuation_for(fmt: Callable[[str, dict[str, Any]], str]) -> Callable[[Any, Any, str], tuple[str, str]]:
    """`continue_with` du suivi P7b : (texte du tour de suite, bloc joint). En-tête du noyau hors clôture ; un résultat
    lu à l'écran (déjà clôturé par `fmt`) va dans le bloc joint."""
    def cont(f: Any, step: Any, status: str) -> tuple[str, str]:
        if step is None:
            return T.CONTINUE_PREFIX + T.STEP_EXPIRED.format(action="?", target="?"), ""
        text = outcome_text(step, status, fmt)
        if "<untrusted-data" in text:
            head, _, rest = text.partition("\n")
            return T.CONTINUE_PREFIX + head, rest
        return T.CONTINUE_PREFIX + text, ""

    return cont


def decided_by_person(follow: Any, step: Any, status: str) -> bool:
    """Un pas du bureau ne relance un tour que si une PERSONNE l'a décidé (go / self / stop) : un pas clos de lui-même (échec, expiration
    sans réponse) ne lance rien. Vu sur lokkal-test1 (12.11) : chaque pas clos relançait un tour qui reposait un pas."""
    return getattr(step, "answer", None) in ("go", "self", "stop")


_FOLLOW: Any = None


def configure_follow(follow: Any) -> None:
    global _FOLLOW
    _FOLLOW = follow


def follow() -> Any:
    return _FOLLOW


__all__ = ["ACTIONS", "DesktopSteps", "Step", "args_sha", "configure_follow", "continuation_for", "follow",
           "outcome_text"]
