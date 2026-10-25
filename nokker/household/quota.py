"""Quota commun du foyer (lot X23, docs/LOKKAL.md §7.29) : des TOURS, comptés dans le ledger.

- **Ce qui compte** : tout tour d'un membre actif du foyer (web, Telegram, passage d'un agent du membre). Le tour
  porte l'étiquette du foyer (`nokker/ledger/attribution.py::SpendTag.household_id`), posée par le noyau comme celle
  d'un projet d'équipe ; le ledger la range avec chaque appel. Un tour = une paire (fil, index) qui a au moins un appel
  modèle ; les fils de sous-agents (`<fil>/sa-…`) appartiennent au tour qui les a lancés. Aucun second compteur.
- **Fenêtres** : celles des plafonds de compte (nokker/tenancy/limits.py) — le jour de 12:00 UTC à 12:00 UTC, la
  semaine du lundi 06:00 UTC ; un tour compte dans la fenêtre de son premier appel.
- **Plafonds**, vérifiés AVANT le tour (`Kernel.run_turn`), dans cet ordre : profil mineur suspendu, plafond du
  membre (tours par jour : défaut de la configuration, ou celui posé par le payeur), quota du foyer par jour, par
  semaine. Refus : réponse claire FR/EN avec l'heure de remise à zéro, aucun appel modèle, tour gardé avec son verdict
  `household:<plafond>` dont le motif porte `resets_at=<ISO>`. Mode `soft` : on compte, le tour qui dépasse reçoit un
  avertissement une fois, rien n'est coupé.
- **Palier du payeur** : le quota est celui du palier actif du payeur (`Household.tier`). Foyer `suspended` (le payeur
  n'a plus de palier de foyer) : le tour n'est plus attribué au foyer, le membre revient à SES plafonds de compte
  (`check` rend None, donc `TenantLimits.check_turn(shared=False)`) ; un profil mineur suspendu reste refusé.
- **Prudence** : un ledger illisible refuse le tour (`household:unavailable`), comme le budget d'un projet.
- **Limite connue** : deux tours lancés au même instant au bord du plafond passent tous les deux (le ledger compte
  un tour à son premier appel) ; le dépassement est borné par le nombre de membres.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from nokker.core.turn import Decision, Gate, Verdict
from nokker.household.model import Settings
from nokker.ledger.attribution import SpendTag
from nokker.tenancy.limits import day_window, week_window

logger = logging.getLogger("nokker.household.quota")
ZURICH = ZoneInfo("Europe/Zurich")

REPLY = {
    "member_turns_per_day": (
        "Limite du jour atteinte pour ton profil : {n} messages par jour, fixée par le payeur du foyer. Le compteur "
        "repart à {at} (heure suisse) ; ta conversation est gardée.\n\n"
        "Daily limit reached for your profile: {n} messages per day, set by the household payer. The counter resets "
        "at {at} (Swiss time); your conversation is kept."),
    "turns_per_day": (
        "Limite du jour atteinte pour le foyer : {n} messages par jour pour tous ses membres. Le compteur repart à {at} "
        "(heure suisse) ; ta conversation est gardée.\n\n"
        "Daily limit reached for the household: {n} messages per day for all its members. The counter resets at {at} "
        "(Swiss time); your conversation is kept."),
    "turns_per_week": (
        "Limite de la semaine atteinte pour le foyer : {n} messages par semaine pour tous ses membres. Le compteur "
        "repart lundi à {at} (heure suisse) ; ta conversation est gardée.\n\n"
        "Weekly limit reached for the household: {n} messages per week for all its members. The counter resets on "
        "Monday at {at} (Swiss time); your conversation is kept."),
    "suspended": (
        "Ce profil est suspendu par le représentant légal du foyer : aucun message n'est traité tant qu'il ne l'a pas "
        "réactivé.\n\n"
        "This profile is suspended by the household's legal guardian: no message is processed until they reactivate "
        "it."),
    "unavailable": (
        "Le quota du foyer est illisible pour l'instant : par prudence, rien n'a été envoyé au modèle. Réessaie dans un "
        "instant.\n\n"
        "The household quota cannot be read right now: to be safe, nothing was sent to the model. Try again in a "
        "moment."),
}
NOTICE = {
    "turns_per_day": ("— Le foyer vient de dépasser ses {n} messages du jour. Rien n'est coupé pour l'instant ; le "
                      "compteur repart à {at} (heure suisse).\n"
                      "— The household just passed its {n} daily messages. Nothing is cut for now; the counter resets "
                      "at {at} (Swiss time)."),
    "turns_per_week": ("— Le foyer vient de dépasser ses {n} messages de la semaine. Rien n'est coupé pour l'instant ; "
                       "le compteur repart lundi à {at} (heure suisse).\n"
                       "— The household just passed its {n} weekly messages. Nothing is cut for now; the counter "
                       "resets on Monday at {at} (Swiss time)."),
    "member_turns_per_day": ("— Tu viens de dépasser les {n} messages du jour de ton profil. Rien n'est coupé pour "
                             "l'instant ; le compteur repart à {at} (heure suisse).\n"
                             "— You just passed the {n} daily messages of your profile. Nothing is cut for now; the "
                             "counter resets at {at} (Swiss time)."),
}


def _hour(at: datetime) -> str:
    return at.astimezone(ZURICH).strftime("%H:%M")


@dataclass
class HouseholdTicket:
    """Ce que le noyau garde d'un tour d'un membre : l'étiquette du ledger, le refus éventuel, l'avertissement du
    mode soft (calculé avant le tour : il suit la réponse)."""

    tag: SpendTag
    verdict: Verdict | None = None
    reply: str = ""
    soft_notice: str | None = None


@dataclass
class HouseholdQuota:
    household: Any  # nokker.household.service.Household
    ledger: Any  # ledger avec `fetch_household_calls` (InMemoryLedger, PostgresLedger)
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    @property
    def settings(self) -> Settings:
        return self.household.settings

    # ── lecture du ledger ──
    def _turns(self, p: Any, actor_account: str, now: datetime) -> dict[str, Any]:
        (d0, d1), (w0, w1) = day_window(now), week_window(now)
        since = min(d0, w0)
        records = self.ledger.fetch_household_calls(p.id, p.tenant_id, since=since, actor=actor_account)
        first: dict[tuple[str, int], tuple[datetime, str]] = {}
        for r in records:
            if "/sa-" in (r.thread_id or ""):
                continue  # sous-agent : son tour parent est déjà compté
            key = (r.thread_id, int(r.turn_index))
            at = r.created_at if r.created_at.tzinfo else r.created_at.replace(tzinfo=UTC)
            if key not in first or at < first[key][0]:
                first[key] = (at, r.account or "")
        by: dict[str, dict[str, int]] = {}
        day = week = 0
        for at, acc in first.values():
            line = by.setdefault(acc, {"today": 0, "week": 0})
            if at >= d0:
                day += 1
                line["today"] += 1
            if at >= w0:
                week += 1
                line["week"] += 1
        return {"day": day, "week": week, "by_account": by, "day_end": d1, "week_end": w1, "week_start": w0}

    # ── tour ──
    def check(self, thread_id: str, sender: str | None) -> HouseholdTicket | None:
        """None : le compte n'est pas membre d'un foyer (rien n'est attribué). Sinon le ticket."""
        try:
            found = self.household.membership(sender)
        except Exception:
            logger.exception("foyer : adhésion de %s illisible", sender)
            return None
        if found is None:
            return None
        acc, p, row = found
        tag = SpendTag(None, None, acc.account_id, household_id=p.id, household_tenant=p.tenant_id)
        t = HouseholdTicket(tag=tag)
        if row.minor:
            st = self.household.minor_state(acc.account_id)
            if st is None or not st.active:
                return self._refuse(t, "suspended", REPLY["suspended"], None)
        tier = self.household.tier(p.tenant_id)
        if not tier.active:
            return None  # foyer suspendu : plus de quota commun, les plafonds du compte s'appliquent
        now = self.clock()
        try:
            n = self._turns(p, acc.account_id, now)
        except Exception:
            logger.exception("quota du foyer %s illisible", p.id)
            return self._refuse(t, "unavailable", REPLY["unavailable"], None)
        s = tier.settings
        mine = n["by_account"].get(acc.account_id, {"today": 0})["today"]
        member_cap, _ = row.cap(s)
        checks = (("member_turns_per_day", mine, member_cap, n["day_end"]),
                  ("turns_per_day", n["day"], s.turns_per_day, n["day_end"]),
                  ("turns_per_week", n["week"], s.turns_per_week, n["week_end"]))
        for rule, used, cap, resets in checks:
            if cap is None or used < cap:
                continue
            if s.mode == "soft":
                if used == cap and t.soft_notice is None:  # ce tour sera le n° cap + 1 : il dépasse, une fois
                    t.soft_notice = NOTICE[rule].format(n=cap, at=_hour(resets))
                continue
            return self._refuse(t, rule, REPLY[rule].format(n=cap, at=_hour(resets)), resets)
        return t

    def tag(self, sender: str | None) -> SpendTag | None:
        """Étiquette seule, sans relire le quota : la suite d'un tour déjà admis (inférence relayée, §7.2) se range au
        foyer comme les autres appels du tour. None hors foyer ou foyer suspendu (comme `check`)."""
        try:
            found = self.household.membership(sender)
        except Exception:
            logger.exception("foyer : adhésion de %s illisible", sender)
            return None
        if found is None:
            return None
        acc, p, _row = found
        if not self.household.tier(p.tenant_id).active:
            return None
        return SpendTag(None, None, acc.account_id, household_id=p.id, household_tenant=p.tenant_id)

    @staticmethod
    def _refuse(t: HouseholdTicket, rule: str, reply: str, resets: datetime | None) -> HouseholdTicket:
        when = f" ; resets_at={resets.astimezone(ZURICH).isoformat()}" if resets is not None else ""
        t.verdict = Verdict(gate=Gate.SWITCH, decision=Decision.DENY, layer=0, rule=f"household:{rule}",
                            reason=f"foyer {t.tag.household_id} ({rule}) : tour refusé avant l'appel modèle{when}",
                            subject="reply")
        t.reply = reply
        return t

    def notice(self, ticket: HouseholdTicket | None) -> str | None:
        return None if ticket is None or ticket.verdict is not None else ticket.soft_notice

    # ── vue GET /v1/household ──
    def usage(self, p: Any, actor_account: str) -> dict[str, Any]:
        now = self.clock()
        n = self._turns(p, actor_account, now)
        return {"today": {"used": n["day"], "resets_at": n["day_end"].astimezone(ZURICH).isoformat()},
                "week": {"used": n["week"], "starts": n["week_start"].astimezone(ZURICH).isoformat(),
                         "resets_at": n["week_end"].astimezone(ZURICH).isoformat()},
                "by_account": n["by_account"]}

    def member_usage(self, account_id: str) -> dict[str, Any] | None:
        """Bloc `household` de `GET /v1/account/usage` pour un membre : le quota commun et SON plafond."""
        found = self.household.membership(account_id)
        if found is None:
            return None
        acc, p, row = found
        u = self.usage(p, acc.account_id)
        tier = self.household.tier(p.tenant_id)
        s = tier.settings
        cap, source = row.cap(s) if tier.active else (None, "none")
        mine = u["by_account"].get(acc.account_id, {"today": 0, "week": 0})
        return {"id": p.id, "mode": s.mode, "state": tier.state,
                "today": {"used": u["today"]["used"], "cap": s.turns_per_day if tier.active else None,
                          "resets_at": u["today"]["resets_at"]},
                "week": {"used": u["week"]["used"], "cap": s.turns_per_week if tier.active else None,
                         "resets_at": u["week"]["resets_at"]},
                "member": {"today": mine["today"], "cap": cap, "source": source}}


def counted_turns(records: Iterable[Any]) -> int:
    """Nombre de tours dans des lignes du ledger (paires fil/index, sous-agents exclus) : utilitaire des bancs."""
    return len({(r.thread_id, int(r.turn_index)) for r in records if "/sa-" not in (r.thread_id or "")})


__all__ = ["HouseholdQuota", "HouseholdTicket", "counted_turns"]
