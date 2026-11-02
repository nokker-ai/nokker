"""Porte du calendrier familial (drapeau `family_calendar`) : posée AUTOUR des portes du noyau, comme `PaperGates`.

`family_calendar_commit` → UNE carte standard `family:calendar`, avec un motif CALCULÉ PAR LE NOYAU depuis le brouillon
rangé (jamais recopié des arguments du modèle) : en tête ce qui N'EST PAS écrit et pourquoi, puis la liste des lignes
qui seront écrites, la cible et le fuseau. La carte porte la révision du brouillon (dans les arguments, donc dans leur
empreinte) : modifier le brouillon après la carte la rend caduque (l'outil refuse d'écrire).

Quand rien n'est écrivable (brouillon introuvable, révision dépassée, aucune ligne valide), la porte laisse passer
l'appel : l'outil, qui n'écrit que sous une carte approuvée, rend alors l'explication sans rien faire. Une relecture
qui plante REFUSE l'action. Le relecteur automatique ne décide jamais cette carte (`auto_review.EXCLUDED_RULES`).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from nokker.core.turn import Decision, Gate, ToolCall, Turn, Verdict
from nokker.household.famcal.review import MAX_SHOWN
from nokker.household.famcal.service import FamcalError, FamilyCalendar
from nokker.household.famcal.tools import CARD_TOOL, SOURCE, not_written
from nokker.qa.untrusted import inline

RULE = "family:calendar"
MAX_REASON_LINES = 40


@dataclass
class FamilyCalendarGates:
    inner: Any
    service: FamilyCalendar

    def action(self, call: ToolCall, spec: Any, turn: Turn) -> list[Verdict]:
        if call.name != CARD_TOOL or str(getattr(spec, "source", "")) != SOURCE:
            return self.inner.action(call, spec, turn)
        a = call.arguments or {}
        try:
            d, ok, _bad = self.service.card(a.get("draft_id"), a.get("revision"))
            if not ok:
                return [Verdict(gate=Gate.ACTION, decision=Decision.ALLOW, layer=1, rule=f"{RULE}:nothing",
                                reason="aucune ligne valide : rien à écrire", subject=call.id)]
            lines = self.service.lines(d)
            rows = [x.text() + (f" ({'; '.join(inline(n, MAX_SHOWN) for n in x.notes)})" if x.notes else "") for x in ok]
            if len(rows) > MAX_REASON_LINES:
                rows = rows[:MAX_REASON_LINES] + [f"… et {len(rows) - MAX_REASON_LINES} autre(s)"]
            n_bad = len(lines) - len(ok)
            head = (f"calendrier familial (depuis {inline(d.origin, MAX_SHOWN)}, révision {d.revision}) : {len(ok)} "
                    f"événement(s) à écrire dans {self.service.target()}, fuseau {d.timezone}"
                    + (f", {n_bad} NON écrit(s)" if n_bad else "") + " :\n")
            return [Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule=RULE,
                            reason=not_written(lines) + head + "\n".join(rows), subject=call.id,
                            strength="standard")]
        except FamcalError as e:
            return [Verdict(gate=Gate.ACTION, decision=Decision.ALLOW, layer=1, rule=f"{RULE}:invalid",
                            reason=f"rien à approuver : {e}", subject=call.id)]
        except Exception:  # noqa: BLE001 — une relecture qui plante ne laisse JAMAIS passer une écriture
            return [Verdict(gate=Gate.ACTION, decision=Decision.DENY, layer=1, rule=f"{RULE}:error",
                            reason="relecture impossible : écriture refusée", subject=call.id)]

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


__all__ = ["RULE", "FamilyCalendarGates"]
