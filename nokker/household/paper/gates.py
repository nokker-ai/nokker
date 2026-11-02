"""Portes des papiers du foyer (drapeau `household_paper`) : posées AUTOUR des portes du noyau, comme `MinorGates`.

Porte d'action — chaque outil `paper_*` qui agit pose une carte, avec un motif CALCULÉ PAR LE NOYAU (relecture des
dates, facture décodée, montants, IBAN masqués), jamais recopié des arguments du modèle :
- `paper_calendar_add`, `paper_bill_due` → carte standard `paper:calendar` (relecture : ce qui sera écrit, ce qui reste
  à préciser) ;
- `paper_share` → carte standard `paper:share` (qui reçoit la copie) ;
- `paper_payment_prepare` → carte FORTE `paper:payment` : le fichier est construit et validé au schéma AVANT la carte,
  la carte en porte l'empreinte ; approuvée, il devient téléchargeable, jamais transmis.
Quand rien n'est faisable (toutes les dates ambiguës, compte débité non réglé…), la porte laisse passer l'appel : l'outil
rend alors la relecture SANS rien faire (il n'agit que sous une carte approuvée, `ctx.approval_id`).

Porte bascule — invariant 5 (domaines fiscal / montant) : un tour qui a fait passer une action de papiers par une carte
garde sa réponse (règle `domaine:…:papiers`, tracée) au lieu d'être remplacé par l'escalade ; une demande de CONSEIL
fiscal ou juridique dans le message escalade comme avant. Rien ne change pour un tour sans outil de papiers.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from nokker.core.turn import Decision, Gate, ToolCall, Turn, Verdict
from nokker.household.paper import dates as dates_mod
from nokker.household.paper import qrbill
from nokker.household.paper.service import Paper, PaperError
from nokker.household.paper.tools import CARD_TOOLS, PAPER_TOOLS

RULE_CALENDAR = "paper:calendar"
RULE_SHARE = "paper:share"
RULE_PAYMENT = "paper:payment"
MAX_REASON_LINES = 40


def _hitl(call: ToolCall, rule: str, reason: str, strength: str) -> list[Verdict]:
    return [Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule=rule, reason=reason, subject=call.id,
                    strength=strength)]


def _allow(call: ToolCall, rule: str, reason: str) -> list[Verdict]:
    return [Verdict(gate=Gate.ACTION, decision=Decision.ALLOW, layer=1, rule=rule, reason=reason, subject=call.id)]


def _lines(items: list[str]) -> str:
    if len(items) > MAX_REASON_LINES:
        items = items[:MAX_REASON_LINES] + [f"… et {len(items) - MAX_REASON_LINES} autre(s)"]
    return "\n".join(items)


@dataclass
class PaperGates:
    inner: Any
    service: Paper

    def action(self, call: ToolCall, spec: Any, turn: Turn) -> list[Verdict]:
        if call.name not in CARD_TOOLS or str(getattr(spec, "source", "")) != "builtin:paper":
            return self.inner.action(call, spec, turn)
        try:
            return self._card(call)
        except PaperError as e:
            return _allow(call, f"{RULE_CALENDAR if 'calendar' in call.name else 'paper'}:invalid",
                          f"rien à approuver : {e}")
        except Exception:  # noqa: BLE001 — une relecture qui plante ne laisse JAMAIS passer une action
            return [Verdict(gate=Gate.ACTION, decision=Decision.DENY, layer=1, rule="paper:error",
                            reason="relecture impossible : action refusée", subject=call.id)]

    def _card(self, call: ToolCall) -> list[Verdict]:
        a = call.arguments or {}
        svc = self.service
        if call.name in ("paper_calendar_add", "paper_bill_due"):
            reviewed, doc = svc.calendar_review(a) if call.name == "paper_calendar_add" else svc.bill_due_review(a)
            ok = [r for r in reviewed if r.ok]
            bad = [r for r in reviewed if not r.ok]
            if not ok:
                return _allow(call, f"{RULE_CALENDAR}:nothing", "aucun événement sans ambiguïté : rien à écrire")
            src = f" (depuis « {doc.filename} »)" if doc is not None else ""
            head = (f"relecture{src} : {len(ok)} événement(s) à écrire dans {svc.calendar_target()}"
                    + (f", {len(bad)} NON écrit(s)" if bad else ""))
            return _hitl(call, RULE_CALENDAR, dates_mod.not_written(reviewed) + head + " :\n"
                         + _lines([r.line() for r in ok]), "standard")
        if call.name == "paper_share":
            doc = svc.document(a.get("document_id"))
            _, email, _ = svc.share_target(a.get("member"))
            return _hitl(call, RULE_SHARE, f"partager « {doc.title} » ({doc.kind}) avec {email} : une copie va dans "
                                           "son espace Papiers, elle y reste même si tu supprimes la tienne",
                         "standard")
        plan, export = svc.payment_card(a)
        if export is None:
            return _allow(call, f"{RULE_PAYMENT}:nothing", "paiement non préparable : " + "; ".join(plan.problems))
        assert plan.debtor is not None and plan.execution is not None
        rows = []
        for n, p in enumerate(plan.payments, start=1):
            b = p.bill
            ref = f"réf. {b.reference_type} …{b.reference[-6:]}" if b.reference else "sans référence"
            rows.append(f"{n}. {b.creditor.name} — {qrbill.masked_iban(b.iban)} "
                        f"({'QR-IBAN' if b.qr_iban else 'IBAN'}) — {b.currency} {p.amount:.2f} — {ref}")
        totals = ", ".join(f"{c} {v:.2f}" for c, v in sorted(plan.totals().items()))
        reason = ("fichier de paiement pain.001 PRÉPARÉ, jamais envoyé ni payé par Nokker : tu le télécharges et le "
                  f"charges toi-même dans ton e-banking. Compte débité {qrbill.masked_iban(plan.debtor.iban)}, "
                  f"exécution le {plan.execution.isoformat()}, {len(plan.payments)} paiement(s), total {totals} :\n"
                  + _lines(rows)
                  + ("\nATTENTION : " + "\nATTENTION : ".join(plan.warnings) if plan.warnings else "")
                  + f"\nEmpreinte du fichier : sha256 {export.sha256[:16]}…")
        return _hitl(call, RULE_PAYMENT, reason, "strong")

    def switch(self, reply: str, turn: Turn, tier: Any) -> list[Verdict]:
        verdicts = self.inner.switch(reply, turn, tier)
        if not any(v.decision is Decision.ESCALATE and v.rule.startswith("domaine:") for v in verdicts):
            return verdicts
        called = {tc.name for m in turn.messages for tc in (getattr(m, "tool_calls", None) or [])}
        if not called & set(PAPER_TOOLS):
            return verdicts
        from nokker.qa.gates import _ADVICE_QUESTION

        if _ADVICE_QUESTION.search(turn.input.text or ""):
            return verdicts
        return [Verdict(gate=v.gate, decision=Decision.ALLOW, layer=v.layer, rule=f"{v.rule}:papiers",
                        reason="papiers du foyer : toute action passe par une carte ; domaine tracé, réponse gardée",
                        subject=v.subject)
                if v.decision is Decision.ESCALATE and v.rule.startswith("domaine:") else v for v in verdicts]

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


__all__ = ["RULE_CALENDAR", "RULE_PAYMENT", "RULE_SHARE", "PaperGates"]
