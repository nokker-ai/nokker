"""Porte des projets du foyer (drapeau `family_projects`) : posée AUTOUR des portes du noyau, comme celle du
calendrier familial.

`family_project_commit` → UNE carte standard `family:project`, avec un motif CALCULÉ PAR LE NOYAU depuis le brouillon
rangé (jamais recopié des arguments du modèle) : en tête ce qui N'EST PAS écrit et pourquoi, puis le projet (échéance,
visibilité, membres, budget indicatif) et les lignes qui seront écrites. La carte porte la révision du brouillon (dans
les arguments, donc dans leur empreinte) : modifier le brouillon après la carte la rend caduque (l'outil refuse
d'écrire).

Quand rien n'est écrivable (brouillon introuvable, révision dépassée, en-tête à corriger, aucune ligne valide), la
porte laisse passer l'appel : l'outil, qui n'écrit que sous une carte approuvée, rend alors l'explication sans rien
faire. Une relecture qui plante REFUSE l'action. Le relecteur automatique ne décide jamais cette carte
(`auto_review.EXCLUDED_RULES`).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from nokker.core.turn import Decision, Gate, ToolCall, Turn, Verdict
from nokker.household.projects.review import MAX_SHOWN
from nokker.household.projects.service import FamilyProjects, ProjectError
from nokker.household.projects.tools import CARD_TOOL, SOURCE, not_written
from nokker.qa.untrusted import inline

RULE = "family:project"
MAX_REASON_LINES = 40


@dataclass
class FamilyProjectGates:
    inner: Any
    service: FamilyProjects

    def action(self, call: ToolCall, spec: Any, turn: Turn) -> list[Verdict]:
        if call.name != CARD_TOOL or str(getattr(spec, "source", "")) != SOURCE:
            return self.inner.action(call, spec, turn)
        a = call.arguments or {}
        try:
            d, header, lines = self.service.card(a.get("draft_id"), a.get("revision"))
            why = self.service.committable(d, header, lines)
            if why is not None:
                return [Verdict(gate=Gate.ACTION, decision=Decision.ALLOW, layer=1, rule=f"{RULE}:nothing",
                                reason=f"rien à écrire : {why}", subject=call.id)]
            h = self.service.adult_home()
            ok = [x for x in lines if x.writable]
            rows = [x.text() + (f" ({'; '.join(inline(n, MAX_SHOWN) for n in x.notes)})" if x.notes else "")
                    for x in ok]
            if len(rows) > MAX_REASON_LINES:
                rows = rows[:MAX_REASON_LINES] + [f"… et {len(rows) - MAX_REASON_LINES} autre(s)"]
            names = ", ".join(inline(h.name(m), MAX_SHOWN) for m in header.members if h.name(m))
            budget = (f", budget indicatif {header.budget['amount']} {header.budget['currency']} (aucun paiement)"
                      if header.budget else "")
            verb = "nouveau projet du foyer" if d.kind == "create" else "replanification du projet"
            head = (f"{verb} « {inline(header.title, MAX_SHOWN)} » (révision {d.revision}) : échéance "
                    f"{header.deadline}, {'réservé aux parents' if header.visibility == 'parents' else 'tout le foyer'}"
                    f", membres : {names}{budget}. {len(ok)} ligne(s) à écrire dans le foyer, rien n'est envoyé "
                    "hors du foyer :\n")
            return [Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule=RULE,
                            reason=not_written(lines) + head + "\n".join(rows), subject=call.id,
                            strength="standard")]
        except ProjectError as e:
            return [Verdict(gate=Gate.ACTION, decision=Decision.ALLOW, layer=1, rule=f"{RULE}:invalid",
                            reason=f"rien à approuver : {e}", subject=call.id)]
        except Exception:  # noqa: BLE001 — une relecture qui plante ne laisse JAMAIS passer une écriture
            return [Verdict(gate=Gate.ACTION, decision=Decision.DENY, layer=1, rule=f"{RULE}:error",
                            reason="relecture impossible : écriture refusée", subject=call.id)]

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


__all__ = ["RULE", "FamilyProjectGates"]
