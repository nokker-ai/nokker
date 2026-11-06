"""Jours fériés suisses, calculés EN LOCAL (aucun réseau).

Référence : annexe « Jours fériés en Suisse » du SECO (extrait LTr/OLT 1, état au 12.12.2024) — liste, par canton, des
jours fériés cantonaux assimilés au dimanche (art. 20a al. 1 LTr) ; le 1er août est le jour férié fédéral. Six cantons
ont des exceptions par district ou par commune (AG, AI, AR, FR, NE, SO) : le texte rendu le signale.

date.nager.at, prévu au départ, est écarté : ses conditions réservent l'API aux projets privés ou non lucratifs
(« For commercial purposes we require active sponsorship ») — docs/ESSENTIALS.md.
"""
from __future__ import annotations

from datetime import date, timedelta

CANTONS = ("AG", "AI", "AR", "BE", "BL", "BS", "FR", "GE", "GL", "GR", "JU", "LU", "NE", "NW", "OW", "SG", "SH", "SO",
           "SZ", "TG", "TI", "UR", "VD", "VS", "ZG", "ZH")
ALL = frozenset(CANTONS)
WITH_EXCEPTIONS = frozenset({"AG", "AI", "AR", "FR", "NE", "SO"})

CANTON_NAMES = {
    "argovie": "AG", "aargau": "AG", "appenzell rhodes interieures": "AI", "appenzell innerrhoden": "AI",
    "appenzell rhodes exterieures": "AR", "appenzell ausserrhoden": "AR", "berne": "BE", "bern": "BE",
    "bale campagne": "BL", "basel landschaft": "BL", "bale ville": "BS", "basel stadt": "BS", "fribourg": "FR",
    "freiburg": "FR", "geneve": "GE", "genf": "GE", "ginevra": "GE", "glaris": "GL", "glarus": "GL", "grisons": "GR",
    "graubunden": "GR", "grigioni": "GR", "jura": "JU", "lucerne": "LU", "luzern": "LU", "neuchatel": "NE",
    "nidwald": "NW", "nidwalden": "NW", "obwald": "OW", "obwalden": "OW", "saint gall": "SG", "st gallen": "SG",
    "schaffhouse": "SH", "schaffhausen": "SH", "soleure": "SO", "solothurn": "SO", "schwyz": "SZ", "thurgovie": "TG",
    "thurgau": "TG", "tessin": "TI", "ticino": "TI", "uri": "UR", "vaud": "VD", "waadt": "VD", "valais": "VS",
    "wallis": "VS", "vallese": "VS", "zoug": "ZG", "zug": "ZG", "zurich": "ZH", "zuerich": "ZH",
}


def easter(year: int) -> date:
    """Dimanche de Pâques (grégorien, algorithme anonyme de Meeus/Jones/Butcher)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    month = (h + l_ - 7 * m + 114) // 31
    day = ((h + l_ - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def holidays(year: int) -> list[tuple[date, str, frozenset[str]]]:
    """(date, nom, cantons) de l'année, triés par date."""
    e = easter(year)
    first_thu_april = _nth_weekday(year, 4, 3, 1)
    # Näfelser Fahrt : premier jeudi d'avril, repoussé d'une semaine s'il tombe dans la semaine sainte.
    naefels = first_thu_april + timedelta(weeks=1) if e - timedelta(days=7) <= first_thu_april <= e + timedelta(days=1) \
        else first_thu_april
    first_sun_sept = _nth_weekday(year, 9, 6, 1)
    third_sun_sept = _nth_weekday(year, 9, 6, 3)
    rows = [
        (date(year, 1, 1), "Nouvel An", ALL),
        (date(year, 1, 2), "Saint-Berchtold", frozenset({"AG", "BE", "FR", "NE", "TG", "VD"})),
        (date(year, 1, 6), "Épiphanie", frozenset({"TI"})),
        (date(year, 3, 1), "Instauration de la République", frozenset({"NE"})),
        (date(year, 3, 19), "Saint-Joseph", frozenset({"SZ", "VS"})),
        (naefels, "Näfelser Fahrt", frozenset({"GL"})),
        (e - timedelta(days=2), "Vendredi saint", ALL - {"TI", "VS"}),
        (e + timedelta(days=1), "Lundi de Pâques", frozenset({"AG", "AI", "AR", "BE", "BL", "BS", "FR", "GE", "GL",
                                                              "GR", "JU", "SG", "SH", "TG", "TI", "VD", "ZH"})),
        (date(year, 5, 1), "Fête du travail", frozenset({"BL", "BS", "JU", "NE", "SH", "SO", "ZH"})),
        (e + timedelta(days=39), "Ascension", ALL),
        (e + timedelta(days=50), "Lundi de Pentecôte", frozenset({"AG", "AI", "AR", "BE", "BL", "BS", "FR", "GE", "GR",
                                                                 "JU", "SG", "SH", "TG", "VD", "ZH"})),
        (e + timedelta(days=60), "Fête-Dieu", frozenset({"AG", "AI", "FR", "JU", "LU", "NE", "NW", "OW", "SO", "SZ",
                                                        "UR", "VS", "ZG"})),
        (date(year, 8, 1), "Fête nationale (jour férié fédéral)", ALL),
        (date(year, 8, 15), "Assomption", frozenset({"AG", "FR", "LU", "NW", "OW", "SO", "SZ", "TI", "UR", "VS",
                                                     "ZG"})),
        (first_sun_sept + timedelta(days=4), "Jeûne genevois", frozenset({"GE"})),
        (third_sun_sept + timedelta(days=1), "Lundi du Jeûne fédéral", frozenset({"VD"})),
        (date(year, 11, 1), "Toussaint", frozenset({"AG", "FR", "GL", "LU", "NW", "OW", "SG", "SO", "SZ", "TI", "UR",
                                                   "VS", "ZG"})),
        (date(year, 12, 8), "Immaculée Conception", frozenset({"AG", "FR", "NW", "OW", "UR", "VS", "ZG"})),
        (date(year, 12, 25), "Noël", ALL),
        (date(year, 12, 26), "Saint-Étienne", frozenset({"AG", "AI", "AR", "BE", "BL", "BS", "FR", "GL", "GR", "LU",
                                                        "NE", "SG", "SH", "TG", "TI", "ZH"})),
        (date(year, 12, 31), "Restauration de la République", frozenset({"GE"})),
    ]
    return sorted(rows, key=lambda r: r[0])


def canton_code(value: str) -> str | None:
    from nokker.tools.essentials.common import fold

    v = value.strip()
    if v.upper() in ALL:
        return v.upper()
    return CANTON_NAMES.get(fold(v))


_WEEKDAYS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")


def render(year: int, canton: str | None) -> str:
    rows = holidays(year)
    if canton:
        rows = [r for r in rows if canton in r[2]]
        head = f"Jours fériés {year} assimilés au dimanche, canton {canton} :"
    else:
        head = f"Jours fériés {year} en Suisse (cantons concernés) :"
    lines = [head]
    for d, name, cantons in rows:
        where = "" if canton else (" — toute la Suisse" if cantons == ALL else f" — {', '.join(sorted(cantons))}")
        lines.append(f"- {d.isoformat()} ({_WEEKDAYS[d.weekday()]}) : {name}{where}")
    if canton and canton in WITH_EXCEPTIONS:
        lines.append(f"Attention : le canton {canton} a des exceptions par district ou par commune ; vérifier auprès "
                     "de la commune.")
    elif not canton:
        lines.append("AG, AI, AR, FR, NE et SO ont des exceptions par district ou par commune.")
    lines.append("Référence : SECO, annexe « Jours fériés en Suisse » (état au 12.12.2024), calcul local.")
    return "\n".join(lines)


__all__ = ["CANTONS", "canton_code", "easter", "holidays", "render"]
