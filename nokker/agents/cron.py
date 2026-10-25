"""Cron à 5 champs en heure murale d'un fuseau (docs/BUBBLES.md §9.5, déclencheur `schedule`) — sans dépendance.

Champs : minute (0-59), heure (0-23), jour du mois (1-31), mois (1-12 ou jan…dec), jour de semaine (0-7 ou sun…sat ;
0 et 7 = dimanche). Formes : `*`, `n`, `a-b`, `*/pas`, `a-b/pas`, `n/pas`, listes séparées par des virgules. Jour du
mois ET jour de semaine restreints tous les deux : l'un OU l'autre suffit (règle de cron). Pas de `@daily`, `L`, `W`,
`#`.

Changement d'heure (comme le cron d'un système) :
  - printemps, heure qui n'existe pas (02:30 à Zurich le dernier dimanche de mars) : l'échéance est sautée ;
  - automne, heure qui existe deux fois : une seule échéance, la première (avant le recul).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_MONTHS = {m: i + 1 for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())}
_DAYS = {d: i for i, d in enumerate("sun mon tue wed thu fri sat".split())}
_FIELDS = (("minute", 0, 59, {}), ("heure", 0, 23, {}), ("jour du mois", 1, 31, {}), ("mois", 1, 12, _MONTHS),
           ("jour de semaine", 0, 7, _DAYS))
MAX_DAYS = 366 * 5  # 29 février un lundi : jusqu'à 28 ans en théorie ; au-delà de 5 ans, « sans échéance »


class CronError(ValueError):
    """Expression ou fuseau invalide (400)."""


def _value(text: str, lo: int, hi: int, names: dict[str, int], label: str) -> int:
    t = text.strip().lower()
    if t in names:
        return names[t]
    if not re.fullmatch(r"\d{1,2}", t):
        raise CronError(f"cron : valeur illisible « {text} » ({label})")
    v = int(t)
    if not lo <= v <= hi:
        raise CronError(f"cron : {v} hors de [{lo}, {hi}] ({label})")
    return v


def _field(text: str, lo: int, hi: int, names: dict[str, int], label: str) -> tuple[frozenset[int], bool]:
    """(valeurs, restreint ?) d'un champ ; `restreint` = autre chose que `*` (règle jour du mois / de semaine)."""
    out: set[int] = set()
    restricted = text.strip() != "*"
    for part in text.split(","):
        part = part.strip()
        if not part:
            raise CronError(f"cron : élément vide ({label})")
        rng, _, step_s = part.partition("/")
        step = 1
        if step_s:
            if not re.fullmatch(r"\d{1,2}", step_s) or int(step_s) == 0:
                raise CronError(f"cron : pas invalide « {step_s} » ({label})")
            step = int(step_s)
        if rng == "*":
            a, b = lo, hi
        elif "-" in rng:
            x, _, y = rng.partition("-")
            a, b = _value(x, lo, hi, names, label), _value(y, lo, hi, names, label)
            if a > b:
                raise CronError(f"cron : intervalle décroissant « {rng} » ({label})")
        else:
            a = _value(rng, lo, hi, names, label)
            b = hi if step_s else a
        out.update(range(a, b + 1, step))
    return frozenset(out), restricted


@dataclass(frozen=True)
class Cron:
    expr: str
    minutes: frozenset[int]
    hours: frozenset[int]
    doms: frozenset[int]
    months: frozenset[int]
    dows: frozenset[int]  # 0 = dimanche (7 ramené à 0)
    dom_restricted: bool
    dow_restricted: bool

    @classmethod
    def parse(cls, expr: str) -> Cron:
        parts = (expr or "").split()
        if len(parts) != 5:
            raise CronError(f"cron : 5 champs attendus (minute heure jour mois jour-de-semaine), reçu « {expr} »")
        parsed = [_field(p, lo, hi, names, label)
                  for p, (label, lo, hi, names) in zip(parts, _FIELDS, strict=True)]
        (mi, _), (h, _), (dom, dom_r), (mo, _), (dow, dow_r) = parsed
        return cls(expr=" ".join(parts), minutes=mi, hours=h, doms=dom, months=mo,
                   dows=frozenset(0 if d == 7 else d for d in dow), dom_restricted=dom_r, dow_restricted=dow_r)

    def day_matches(self, d: date) -> bool:
        if d.month not in self.months:
            return False
        dom_ok = d.day in self.doms
        dow_ok = (d.isoweekday() % 7) in self.dows
        if self.dom_restricted and self.dow_restricted:
            return dom_ok or dow_ok
        return dom_ok and dow_ok

    def next_after(self, after: datetime, tz: ZoneInfo) -> datetime | None:
        """Première échéance STRICTEMENT après `after` (aware), en UTC ; None si aucune dans les 5 ans."""
        after_utc = after.astimezone(timezone.utc)
        local = after_utc.astimezone(tz).replace(second=0, microsecond=0, tzinfo=None)
        start_day = local.date()
        hours, minutes = sorted(self.hours), sorted(self.minutes)
        for offset in range(MAX_DAYS):
            day = start_day + timedelta(days=offset)
            if not self.day_matches(day):
                continue
            for h in hours:
                if offset == 0 and h < local.hour:
                    continue
                for m in minutes:
                    naive = datetime.combine(day, time(h, m))
                    if naive < local:
                        continue
                    at = _localize(naive, tz)
                    if at is not None and at > after_utc:
                        return at
        return None


def _localize(naive: datetime, tz: ZoneInfo) -> datetime | None:
    """Heure murale → UTC ; None si elle n'existe pas (trou du printemps) ; heure doublée : la première (fold=0)."""
    first = naive.replace(tzinfo=tz, fold=0)
    utc = first.astimezone(timezone.utc)
    if utc.astimezone(tz).replace(tzinfo=None) != naive:
        return None
    return utc


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise CronError(f"fuseau inconnu : {name!r} (ex. Europe/Zurich, UTC)") from None


def next_run(expr: str, tz_name: str, after: datetime) -> datetime | None:
    return Cron.parse(expr).next_after(after, zone(tz_name))


__all__ = ["Cron", "CronError", "next_run", "zone"]
