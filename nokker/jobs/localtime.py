"""Jour et heure LOCAUX du pack Emploi (heure suisse). Le jour d'une candidature décide du mois du relevé ORP : il est
pris dans le fuseau de la personne, jamais en UTC ni dans celui de la machine. Aucun fuseau par locataire n'existe
encore ; Europe/Zurich partout (à brancher ici le jour où il existera)."""
from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Zurich")


def local(ts: float) -> datetime:
    """Instant (epoch) → heure suisse, avec son décalage (+01:00 / +02:00)."""
    return datetime.fromtimestamp(float(ts), TZ)


def day(ts: float) -> str:
    """Jour local AAAA-MM-JJ d'un instant (epoch)."""
    return local(ts).strftime("%Y-%m-%d")


def today(now: datetime) -> str:
    """Jour local AAAA-MM-JJ d'un `datetime` (sans fuseau : lu comme UTC)."""
    return (now if now.tzinfo else now.replace(tzinfo=UTC)).astimezone(TZ).date().isoformat()


def day_of(text: object) -> str:
    """Jour local d'une date-heure ISO rendue par le service emploi ; une date seule (ou une heure sans décalage) est
    gardée telle quelle."""
    s = str(text or "")
    if len(s) <= 10:
        return s
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return s[:10]
    return d.astimezone(TZ).date().isoformat() if d.tzinfo else s[:10]


__all__ = ["TZ", "day", "day_of", "local", "today"]
