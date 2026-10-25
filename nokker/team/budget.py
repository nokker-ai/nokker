"""Budget d'un projet d'équipe (docs/LOKKAL.md §7.15) : plafond du jour et du mois, décompté sur le LEDGER.

- **Ce qui compte** : les appels modèle faits pendant un tour rattaché au projet — un fil de session rattaché à une
  carte du plan du projet, ou un passage d'un agent du projet (`resolve(fil) → pj_…`), sous-agents compris (fil
  `<fil>/sa-…`). Le tour porte une étiquette (`nokker/ledger/attribution.py`) que le ledger range avec chaque ligne ;
  le budget relit ces lignes, aucun second compteur.
- **Qui dépense** : l'expéditeur du tour (le propriétaire de l'agent pour un passage), seulement s'il est encore
  `member` au moins. Un viewer, un ancien membre, un non-membre : le tour n'est pas attribué au projet.
- **Fenêtres** : jour et mois civils, heure suisse. Coût calculé comme le coût par tour (X2, `turn_cost.summarize` :
  catalogue, devise de chaque entrée) puis converti dans la devise du plafond : USD ↔ CHF au taux du catalogue, EUR
  par les taux de la configuration (`team_settings.budget_rates`, EUR pour une unité) ; jamais un taux inventé. Une
  dépense qu'on ne sait pas convertir rend le budget invérifiable : refus par prudence (comme le budget d'un agent).
- **Refus** : vérifié AVANT le tour (`Kernel.run_turn`), comme les plafonds du locataire : réponse claire, aucun appel
  modèle, tour gardé avec son verdict `project_budget:<fenêtre>`. Un tour commencé sous le plafond finit ; le suivant
  est refusé. À 80 % franchis pendant un tour : un avertissement suit la réponse, une fois.
- **Vue** : total et plafond par fenêtre ; ligne par membre pour owner et maintainer, sa propre ligne pour les autres.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from nokker.core.turn import Decision, Gate, LLMCall, Verdict
from nokker.ledger.attribution import SpendTag
from nokker.ledger.turn_cost import root_thread, row_from_call, row_from_record, summarize
from nokker.team.model import RANK, Member, Project, TeamError

logger = logging.getLogger("nokker.team.budget")

ZURICH = ZoneInfo("Europe/Zurich")
WARN_RATIO = 0.8
WINDOWS = ("daily", "monthly")

REPLY = {
    "daily": ("Budget du projet « {name} » atteint pour aujourd'hui : {spent} dépensés sur un plafond de {cap}. Rien n'a "
              "été envoyé au modèle ; ta conversation est gardée. Le compteur repart à minuit (heure suisse) ; un owner "
              "ou un maintainer du projet peut aussi relever le plafond.\n\n"
              "Project budget of “{name}” reached for today: {spent} spent out of a {cap} cap. Nothing was sent to "
              "the model; your conversation is kept. The counter resets at midnight (Swiss time); a project owner or "
              "maintainer can also raise the cap."),
    "monthly": ("Budget du projet « {name} » atteint pour ce mois : {spent} dépensés sur un plafond de {cap}. Rien n'a "
                "été envoyé au modèle ; ta conversation est gardée. Le compteur repart le 1er du mois (heure suisse) ; "
                "un owner ou un maintainer du projet peut aussi relever le plafond.\n\n"
                "Project budget of “{name}” reached for this month: {spent} spent out of a {cap} cap. Nothing was sent "
                "to the model; your conversation is kept. The counter resets on the 1st (Swiss time); a project owner "
                "or maintainer can also raise the cap."),
    "unverifiable": ("Budget du projet « {name} » invérifiable : des dépenses en {what} ne se convertissent pas en "
                     "{currency} sur ce serveur. Par prudence, rien n'a été envoyé au modèle ; ta conversation est "
                     "gardée.\n\n"
                     "The budget of project “{name}” cannot be checked: spending in {what} cannot be converted to "
                     "{currency} on this server. To be safe, nothing was sent to the model; your conversation is kept."),
    "unavailable": ("Budget du projet « {name} » illisible pour l'instant : par prudence, rien n'a été envoyé au "
                    "modèle. Réessaie dans un instant.\n\n"
                    "The budget of project “{name}” cannot be read right now: to be safe, nothing was sent to the "
                    "model. Try again in a moment."),
}
NOTICE = ("— Le projet « {name} » a passé 80 % de son budget {label} ({spent} sur {cap}).\n"
          "— Project “{name}” has passed 80% of its {label_en} budget ({spent} of {cap}).")
LABELS = {"daily": ("du jour", "daily"), "monthly": ("du mois", "monthly")}


def day_start(now: datetime) -> datetime:
    local = now.astimezone(ZURICH)
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def month_start(now: datetime) -> datetime:
    return day_start(now).replace(day=1)


def _next_day(now: datetime) -> datetime:
    return day_start(now) + timedelta(days=1)  # heure murale de Zurich : changement d'heure compris


def _next_month(now: datetime) -> datetime:
    return (month_start(now) + timedelta(days=32)).replace(day=1)


def _money(amount: float, currency: str) -> str:
    return f"{amount:.2f} {currency}"


@dataclass
class BudgetTicket:
    """Ce que le noyau garde d'un tour attribué : l'étiquette du ledger, le refus éventuel, l'état d'avant le tour
    (pour l'avertissement de 80 %)."""

    tag: SpendTag
    project_name: str
    currency: str
    caps: dict[str, float]
    spent: dict[str, float]  # fenêtre → dépensé AVANT le tour, dans la devise du plafond
    verdict: Verdict | None = None
    reply: str = ""


@dataclass
class ProjectBudget:
    team: Any  # nokker.team.service.Team
    ledger: Any  # ledger avec `fetch_project_calls` (InMemoryLedger, PostgresLedger)
    prices: Any = None  # PriceCatalog
    resolve: Callable[[str], str | None] | None = None  # fil de session → pj_… (carte du plan, agent du projet)
    rates: dict[str, float] | None = None  # EUR pour UNE unité de la devise (configuration) ; None = aucun taux
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    # ── conversion ──
    def _eur(self, currency: str) -> float | None:
        if currency == "EUR":
            return 1.0
        rates = self.rates or {}
        if currency in rates:
            return float(rates[currency])
        chf_usd = getattr(self.prices, "chf_usd_rate", None)
        if chf_usd and currency == "CHF" and "USD" in rates:
            return float(rates["USD"]) * float(chf_usd)
        if chf_usd and currency == "USD" and "CHF" in rates:
            return float(rates["CHF"]) / float(chf_usd)
        return None

    def _factor(self, src: str, dst: str) -> float | None:
        if src == dst:
            return 1.0
        chf_usd = getattr(self.prices, "chf_usd_rate", None)
        if chf_usd and {src, dst} == {"CHF", "USD"}:
            return float(chf_usd) if src == "CHF" else 1.0 / float(chf_usd)
        a, b = self._eur(src), self._eur(dst)
        return None if a is None or b is None or b == 0 else a / b

    def _convert(self, costs: dict[str, float], to: str) -> tuple[float, list[str]]:
        total, missing = 0.0, []
        for cur, amount in costs.items():
            f = self._factor(cur, to)
            if f is None:
                missing.append(cur)
            else:
                total += amount * f
        return total, sorted(missing)

    # ── lecture du ledger ──
    def _records(self, p: Project, actor_account: str, now: datetime) -> list[Any]:
        return self.ledger.fetch_project_calls(p.id, p.tenant_id, since=month_start(now).astimezone(UTC),
                                               actor=actor_account)

    def spend_of(self, records: Iterable[Any], *, accounts: Collection[str] | None = None, since: datetime,
                 until: datetime | None = None, currency: str = "EUR") -> dict[str, Any]:
        """Compteur commun (budget de projet, et tout autre plafond partagé : le foyer, X23) : dépense des lignes du
        ledger `records` faites par `accounts` (None = tous) dans [since, until), dans `currency`, par les règles du
        budget (coût X2, conversions du catalogue et de la configuration). → {currency, total, by_account,
        unconverted: [devises sans taux], unpriced_models}. Ne lit rien : l'appelant fournit les lignes qu'il a le
        droit de lire (`fetch_project_calls`, ou celles d'un locataire)."""
        mine = [r for r in records if r.created_at >= since and (until is None or r.created_at < until)
                and (accounts is None or r.account in accounts)]
        allsum = summarize((row_from_record(r) for r in mine), self.prices)
        total, missing = self._convert(allsum["cost"], currency)
        by: dict[str, float] = {}
        for acc in sorted({r.account for r in mine if r.account}):
            cost = summarize((row_from_record(r) for r in mine if r.account == acc), self.prices)["cost"]
            by[acc] = round(self._convert(cost, currency)[0], 6)
        return {"currency": currency, "total": round(total, 6), "by_account": by, "unconverted": missing,
                "unpriced_models": allsum["unpriced_models"]}

    def _sums(self, records: Iterable[Any], currency: str, now: datetime) -> dict[str, Any]:
        records = list(records)
        month = self.spend_of(records, since=month_start(now), currency=currency)
        day = self.spend_of(records, since=day_start(now), currency=currency)
        return {"daily": day["total"], "monthly": month["total"],
                "missing": sorted(set(month["unconverted"]) | set(day["unconverted"])),
                "unpriced": month["unpriced_models"], "by_day": day["by_account"], "by_month": month["by_account"]}

    # ── attribution d'un tour ──
    def _member_project(self, sender: str | None, pid: str) -> tuple[Any, Project, Member] | None:
        tenancy = getattr(self.team, "tenancy", None)
        actor = tenancy.account(sender) if tenancy is not None and sender else None
        if actor is None or getattr(actor, "status", "active") != "active":
            return None
        try:
            p, me = self.team._gate(actor, pid, "viewer")
        except TeamError:
            return None
        if m_rank(me.role) < m_rank("member"):
            return None
        return actor, p, me

    def check(self, thread_id: str, sender: str | None) -> BudgetTicket | None:
        """None : le tour n'est pas attribué à un projet. Sinon le ticket : son étiquette et, s'il faut refuser, le
        verdict et la réponse. Une erreur de lecture du budget refuse (par prudence) ; une erreur de résolution du fil
        laisse le tour hors projet (journalisée)."""
        try:
            pid = self.resolve(root_thread(thread_id)) if self.resolve is not None else None
            found = self._member_project(sender, pid) if pid else None
        except Exception:  # noqa: BLE001 — résolution du fil en panne : le tour n'est pas attribué
            logger.exception("budget de projet : fil %s non résolu", thread_id)
            return None
        if found is None:
            return None
        actor, p, _ = found
        return self._ticket(actor, p)

    def check_project(self, pid: str, sender: str | None) -> BudgetTicket | None:
        """Même contrôle pour un passage d'agent du projet AVANT d'ouvrir son fil (moteur Crew)."""
        found = self._member_project(sender, pid)
        return None if found is None else self._ticket(found[0], found[1])

    def _ticket(self, actor: Any, p: Project) -> BudgetTicket:
        tag = SpendTag(project_id=p.id, project_tenant=p.tenant_id, account=actor.account_id)
        budget = p.budget or {}
        currency = budget.get("currency") or "EUR"
        caps = {w: float(budget[w]) for w in WINDOWS if budget.get(w) is not None}
        t = BudgetTicket(tag=tag, project_name=p.name, currency=currency, caps=caps, spent={})
        if not caps:
            return t
        now = self.clock()
        try:
            sums = self._sums(self._records(p, actor.account_id, now), currency, now)
        except Exception:  # noqa: BLE001 — budget illisible : refus par prudence, jamais un tour sans plafond
            logger.exception("budget de projet %s illisible", p.id)
            return self._refuse(t, "unavailable", REPLY["unavailable"].format(name=p.name))
        t.spent = {w: sums[w] for w in WINDOWS}
        if sums["missing"]:
            what = ", ".join(sums["missing"])
            return self._refuse(t, "unverifiable", REPLY["unverifiable"].format(name=p.name, what=what,
                                                                                currency=currency))
        for w in WINDOWS:
            if w in caps and sums[w] >= caps[w]:
                return self._refuse(t, w, REPLY[w].format(name=p.name, spent=_money(sums[w], currency),
                                                          cap=_money(caps[w], currency)))
        return t

    @staticmethod
    def _refuse(t: BudgetTicket, window: str, reply: str) -> BudgetTicket:
        t.verdict = Verdict(gate=Gate.SWITCH, decision=Decision.DENY, layer=0, rule=f"project_budget:{window}",
                            reason=f"budget du projet {t.tag.project_id} ({window}) : tour refusé avant l'appel modèle",
                            subject="reply")
        t.reply = reply
        return t

    def notice(self, ticket: BudgetTicket | None, calls: Iterable[LLMCall]) -> str | None:
        """Avertissement à joindre à la réponse du tour qui fait passer une fenêtre au-delà de 80 % du plafond."""
        if ticket is None or ticket.verdict is not None or not ticket.caps:
            return None
        cost = summarize([row_from_call(c) for c in calls], self.prices)["cost"]
        add, missing = self._convert(cost, ticket.currency)
        if missing:
            return None
        for w in WINDOWS:
            cap = ticket.caps.get(w)
            if not cap:
                continue
            before = ticket.spent.get(w, 0.0)
            after = before + add
            if before < WARN_RATIO * cap <= after:
                fr, en = LABELS[w]
                return NOTICE.format(name=ticket.project_name, label=fr, label_en=en,
                                     spent=_money(after, ticket.currency), cap=_money(cap, ticket.currency))
        return None

    # ── vue GET /v1/projects/{id}/budget ──
    def view(self, actor: Any, pid: str) -> dict[str, Any]:
        p, me = self.team._gate(actor, pid)
        budget = p.budget or {}
        currency = budget.get("currency") or "EUR"
        now = self.clock()
        records = self._records(p, actor.account_id, now)
        total = self._sums(records, currency, now)

        def window(w: str, resets: datetime) -> dict[str, Any]:
            cap = budget.get(w)
            spent = round(total[w], 6)
            ratio = (spent / cap) if cap else None
            state = ("none" if cap is None else "over" if spent >= cap else "warning" if ratio >= WARN_RATIO
                     else "ok")
            return {"spent": spent, "cap": cap, "ratio": None if ratio is None else round(ratio, 4), "state": state,
                    "resets_at": resets.isoformat()}

        members = {x.account_id: x for x in self.team.store.members(actor, pid)}
        accounts = set(members) | {r.account for r in records if r.account}
        see_all = m_rank(me.role) >= m_rank("maintainer")
        lines = []
        for acc in sorted(accounts, key=lambda a: (a not in members, members[a].email if a in members else a)):
            if not see_all and acc != actor.account_id:
                continue
            mem = members.get(acc)
            line = {"account_id": acc, "email": mem.email if mem else None, "role": mem.role if mem else None,
                    "you": acc == actor.account_id, "today": total["by_day"].get(acc, 0.0),
                    "month": total["by_month"].get(acc, 0.0)}
            if mem is None:
                line["former"] = True
            lines.append(line)
        return {"project_id": p.id, "budget": p.budget, "currency": currency, "counted": True,
                "today": window("daily", _next_day(now)), "month": window("monthly", _next_month(now)),
                "members": lines, "unconverted": total["missing"], "unpriced_models": total["unpriced"],
                "tz": "Europe/Zurich"}


def m_rank(role: str | None) -> int:
    return RANK.get(role or "", -1)


def thread_project_resolver(*, agents: Any = None, plan: Any = None) -> Callable[[str], str | None]:
    """Fil de session → projet d'équipe : le passage d'un agent du projet (champ `project` de l'agent), sinon un fil
    rattaché à une carte du plan d'un projet (ou un passage d'un agent listé sur cette carte). Locataire courant."""

    def resolve(thread_id: str) -> str | None:
        store = getattr(agents, "store", None)
        run = store.run_by_thread(thread_id) if store is not None else None
        if run is not None:
            a = store.get(run.agent_id)
            if a is not None and a.project:
                return a.project
        if plan is not None:
            card = plan.card_for_thread(plan._index(), thread_id)
            if card is not None and card.project:
                return card.project
        return None

    return resolve


__all__ = ["BudgetTicket", "ProjectBudget", "day_start", "month_start", "thread_project_resolver"]
