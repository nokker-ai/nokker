"""Carte de proposition et geste du noyau (docs/SELF-IMPROVE.md §6).

La boucle ne touche à rien : elle écrit une `Proposal` (diff, scores avant/après, coût de la nuit) et, avec
`--propose`, pose une carte STANDARD par le mécanisme d'approbation existant (`Approvals.request`, comme l'armement d'un
agent Crew). Les arguments de la carte SONT la variante : `args_sha256` garantit que c'est elle qui sera appliquée.

À la décision (`ApprovalExecutor.handlers[IMPROVE_TOOL]`, branché par `api/server.py` drapeau allumé seulement) :

- refusée : rien n'est écrit ;
- approuvée, `tools` / `router` : nouvelle version du calque (`store.py`), posée à chaud sur le registre / la politique ;
  revenir : `nokker improve revert --target <cible> --version N` (0 = plus rien) ;
- approuvée, `skills` : une nouvelle version de chaque skill appris par le service d'apprentissage (`skill_update`,
  acteur `improve`, carte liée) ; revenir : `skill_revert` comme pour tout skill appris (docs/LEARNING.md §2.6) ;
- `blocks` : JAMAIS appliqué tant que dure le gel du format. `apply_blocks` refuse si `block_freeze` est vrai (défaut) ou
  si l'empreinte de la variante n'est pas dans `format_ack` (accord de la relecture du format sur CE texte) ; et même gel levé, ce
  prototype n'écrit aucun bloc (l'intégration se fera avec la relecture du format). La carte d'un bloc est une proposition à relayer.
"""
from __future__ import annotations

import difflib
import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from types import SimpleNamespace
from typing import Any

from nokker.improve.loop import RunResult
from nokker.improve.overlay import apply_router, router_flat, sha256_of, validate_router_value
from nokker.improve.reflect import render_value
from nokker.improve.store import ImproveStore

IMPROVE_TOOL = "improve_apply"  # geste du noyau sur carte, pas un outil du registre (le modèle ne le voit jamais)
IMPROVE_CHANNEL = "improve"
IMPROVE_RULE = "improve:proposal"
# Texte vu par le modèle (descriptions d'outils, skills, blocs) : la carte le dit, pour relayer à la relecture du format avant d'approuver.
MODEL_VISIBLE = {"tools": True, "skills": True, "router": False, "blocks": True}


class FrozenError(RuntimeError):
    """Variante de bloc pendant le gel du format (ou sans son accord) : rien n'est appliqué."""


@dataclass
class Proposal:
    id: str
    target: str
    overlay: dict[str, Any]
    base_sha256: str
    diff: str
    scores_before: dict[str, Any]
    scores_after: dict[str, Any]
    spent_eur: float
    candidate: str
    front: list[dict[str, Any]] = field(default_factory=list)
    frozen: bool = False
    model_visible: bool = False
    account: str | None = None

    def arguments(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("front")
        d["overlay_sha256"] = sha256_of(self.overlay)
        return d


def unified_diff(target: str, before: dict[str, Any], after: dict[str, Any]) -> str:
    parts = []
    for key in sorted(after):
        a = render_value(target, before.get(key, "")).splitlines(keepends=True)
        b = render_value(target, after[key]).splitlines(keepends=True)
        parts.append("".join(difflib.unified_diff(a, b, fromfile=f"{target}/{key}@base", tofile=f"{target}/{key}@variante")))
    return "\n".join(p for p in parts if p)


def build_proposal(run: RunResult, *, base_sha256: str, account: str | None = None) -> Proposal | None:
    best, base = run.candidate(run.best), run.candidate("c00")
    if best is None or base is None or best.scores is None or base.scores is None:
        return None
    changed = {k: v for k, v in best.overlay.items() if v != run.base_values.get(k)}
    if not changed:
        return None
    front = [{"id": c.id, **c.scores.summary()} for c in run.candidates if c.id in run.front and c.scores]
    return Proposal(id=sha256_of({"t": run.target, "o": changed, "b": base_sha256})[:16], target=run.target,
                    overlay=changed, base_sha256=base_sha256, diff=unified_diff(run.target, run.base_values, changed),
                    scores_before=base.scores.summary(), scores_after=best.scores.summary(),
                    spent_eur=round(run.spent_eur, 6), candidate=best.id, front=front,
                    frozen=run.target == "blocks", model_visible=MODEL_VISIBLE[run.target], account=account)


def card_reason(p: Proposal) -> str:
    b, a = p.scores_before, p.scores_after
    head = (f"variante « {p.target} » ({', '.join(sorted(p.overlay))}) : qualité {b['quality']:.3f} → "
            f"{a['quality']:.3f}, coût {b['cost_per_1k_eur']:.6f} → {a['cost_per_1k_eur']:.6f} EUR/1000, sûreté "
            f"inchangée ou meilleure ; nuit {p.spent_eur:.4f} EUR")
    if p.frozen:
        head += " — GEL DU FORMAT : proposition à relayer, non applicable"
    elif p.model_visible:
        head += " — texte vu par le modèle : à relayer à la relecture du format avant d'approuver"
    return head


def request_card(approvals: Any, p: Proposal, *, by: str | None = None) -> str:
    """Pose la carte (standard) par `Approvals.request` ; rend son identifiant."""
    from nokker.core.turn import Decision, Gate, InputCheck, ToolCall, Turn, Verdict

    turn = Turn(thread_id=f"improve-{p.id}", index=0, input=InputCheck(text=""), channel=IMPROVE_CHANNEL, sender=by)
    call = ToolCall(id=f"improve-{p.id}", name=IMPROVE_TOOL, arguments=p.arguments())
    verdict = Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule=IMPROVE_RULE, reason=card_reason(p),
                      subject=call.id, strength="standard")
    return approvals.request(turn, call, verdict, None).id


def apply_blocks(settings: dict[str, Any], overlay_sha256: str) -> None:
    if settings.get("block_freeze", True):
        raise FrozenError("gel du format en cours (self_improve_settings.block_freeze) : variante de bloc non appliquée")
    if overlay_sha256 not in set(settings.get("format_ack") or []):
        raise FrozenError("pas d'accord de la relecture du format sur cette variante (self_improve_settings.format_ack) : "
                          "non appliquée")
    raise FrozenError("gel levé et accord présent, mais ce prototype n'applique aucun bloc : intégration à faire "
                      "avec la relecture du format")


def apply_proposal(args: dict[str, Any], *, config: Any, kernel: Any = None, learned: Any = None,
                   approval_id: str | None = None, thread_id: str = "") -> str:
    """Applique une variante APPROUVÉE ; rend le résumé posé sur la carte. Lève en cas de refus (carte en échec)."""
    target, overlay = str(args.get("target")), dict(args.get("overlay") or {})
    if sha256_of(overlay) != args.get("overlay_sha256"):
        raise ValueError("empreinte de la variante incohérente : rien n'est appliqué")
    settings = dict(getattr(config, "self_improve_settings", None) or {})
    if target == "blocks":
        apply_blocks(settings, str(args.get("overlay_sha256")))
    store = ImproveStore.from_config(config)
    if target in ("tools", "router"):
        active = store.active(target)
        if sha256_of(active) != args.get("base_sha256"):
            raise ValueError(f"le calque {target} a changé depuis la proposition : relancer la boucle")
        merged = {**active, **overlay} if target == "tools" else validate_router_value({**active, **router_flat(overlay)})
        v = store.add(target, merged, approval_id=approval_id, proposal_id=str(args.get("id")),
                      scores_before=args.get("scores_before"), scores_after=args.get("scores_after"))
        if kernel is not None:
            if target == "tools":
                reg = getattr(kernel.tools, "internal", kernel.tools)
                reg.overlay = dict(merged)
                kernel.tools.refresh()
            else:
                apply_router(kernel.policy, merged)
        return f"{target} : version {v.version} appliquée ({', '.join(sorted(overlay))})"
    if target == "skills":
        if learned is None:
            raise RuntimeError("apprentissage éteint : aucun skill appris à mettre à jour")
        account = str(args.get("account") or "")
        ctx = SimpleNamespace(thread_id=thread_id, turn_index=None, channel=IMPROVE_CHANNEL, approval_id=approval_id)
        done = []
        for name, value in sorted(overlay.items()):
            v = learned.create({"name": name, "description": value["description"], "body": value["body"],
                                "reason": f"variante de la boucle d'amélioration {args.get('id')}"},
                               account, ctx, update=True, actor="improve")
            done.append(f"{name} v{v.version}")
        return "skills : " + ", ".join(done)
    raise ValueError(f"cible inconnue : {target}")


def improve_handler(config: Any, kernel: Any, learned_fn: Callable[[], Any] = lambda: None) -> Callable[[Any], str]:
    """`ApprovalExecutor.handlers[IMPROVE_TOOL]` : approuvée → `apply_proposal` ; refusée → rien."""
    def handle(approval: Any) -> str:
        if approval.status != "approved":
            return "refusée : rien n'est appliqué"
        return apply_proposal(approval.arguments or {}, config=config, kernel=kernel, learned=learned_fn(),
                              approval_id=approval.id, thread_id=approval.thread_id)
    return handle


def write_run(run: RunResult, proposal: Proposal | None, out_dir: Any) -> dict[str, str]:
    """Écrit le compte rendu de la nuit : candidats, front, proposition et diff."""
    from pathlib import Path

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    files = {"candidates": out / "candidates.jsonl", "run": out / "run.json"}
    with open(files["candidates"], "w", encoding="utf-8") as f:
        f.writelines(json.dumps(c.to_json(), ensure_ascii=False, sort_keys=True) + "\n" for c in run.candidates)
    summary = {"target": run.target, "front": run.front, "best": run.best, "spent_eur": round(run.spent_eur, 6),
               "stopped": run.stopped, "trace_counts": run.trace_counts, "examples": run.examples,
               "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    files["run"].write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    if proposal is not None:
        files["proposal"] = out / "proposal.json"
        files["diff"] = out / "proposal.diff"
        files["proposal"].write_text(json.dumps({**proposal.arguments(), "front": proposal.front},
                                                ensure_ascii=False, indent=1), encoding="utf-8")
        files["diff"].write_text(proposal.diff, encoding="utf-8")
    return {k: str(v) for k, v in files.items()}


__all__ = [
    "IMPROVE_CHANNEL",
    "IMPROVE_RULE",
    "IMPROVE_TOOL",
    "FrozenError",
    "Proposal",
    "apply_blocks",
    "apply_proposal",
    "build_proposal",
    "card_reason",
    "improve_handler",
    "request_card",
    "unified_diff",
    "write_run",
]
