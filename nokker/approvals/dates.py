"""Contrôle déterministe des dates et heures d'une carte (relecteur automatique, docs/APPROVALS.md §8).

Avant tout appel au modèle relecteur : chaque date et chaque heure présentes dans les ARGUMENTS de la carte doivent se
retrouver dans la DEMANDE de l'utilisateur, une fois les deux normalisées (`AAAA-MM-JJ`, `HH:MM`). Les dates relatives
de la demande (« aujourd'hui », « demain », « après-demain », « jeudi », « le 24 », « 24 octobre ») sont résolues dans le
fuseau du compte, à l'instant de la carte. Formes reconnues en FR / DE / IT / EN : mois et jours de la semaine des quatre
langues (« 19. November », « venerdì », « Nov 19th »), relatifs (« morgen », « übermorgen », « domani », « dopodomani »,
« tonight »), heures « 14h », « 14:30 », « 2pm », « 14 Uhr », « alle 15 », « um 9 », « midi »/« noon ». Un jour de semaine qui est AUJOURD'HUI (« vendredi » un vendredi) est
ambigu (le jour même ou dans 7 jours) : il ne résout rien, la carte va à l'humain. Une date ou une heure de la carte
absente de la demande → escalade, sans appel au modèle.

Rien ici n'est vu par le modèle.
"""
from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from typing import Any

# Mois et jours de la semaine, FR / DE / IT / EN, forme repliée (`_fold` : sans accents, minuscules). Les abréviations
# ne sont reconnues que collées à un numéro de jour (« 19 nov. », « Nov 19 ») ; celles qui sont aussi des mots courants
# (« mar », « set », « ago », « gen », « mag », « sat », « sun »…) ne figurent pas.
MONTHS = {"janvier": 1, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6, "juillet": 7, "aout": 8,
          "septembre": 9, "octobre": 10, "novembre": 11, "decembre": 12,
          "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7, "august": 8,
          "september": 9, "october": 10, "november": 11, "december": 12,
          "januar": 1, "janner": 1, "februar": 2, "marz": 3, "maerz": 3, "juni": 6, "juli": 7,
          "oktober": 10, "dezember": 12,
          "gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4, "maggio": 5, "giugno": 6, "luglio": 7, "agosto": 8,
          "settembre": 9, "ottobre": 10, "dicembre": 12}
MONTH_ABBR = {"jan": 1, "janv": 1, "feb": 2, "febr": 2, "fev": 2, "fevr": 2, "apr": 4, "avr": 4, "jun": 6, "jul": 7,
              "juil": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "okt": 10, "nov": 11, "dec": 12, "dez": 12,
              "dic": 12, "ott": 10, "giu": 6, "lug": 7}
EN_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
             "november", "december", "jan", "feb", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec")
_ALL_MONTHS = {**MONTHS, **MONTH_ABBR}
WEEKDAYS = {"lundi": 0, "mardi": 1, "mercredi": 2, "jeudi": 3, "vendredi": 4, "samedi": 5, "dimanche": 6,
            "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
            "montag": 0, "dienstag": 1, "mittwoch": 2, "donnerstag": 3, "freitag": 4, "samstag": 5, "sonnabend": 5,
            "sonntag": 6,
            "lunedi": 0, "martedi": 1, "mercoledi": 2, "giovedi": 3, "venerdi": 4, "sabato": 5, "domenica": 6}
# « vendredi prochain », « next friday », « nächsten Freitag », « venerdì prossimo » : la semaine suivante si c'est
# aujourd'hui (sinon le prochain jour de ce nom, comme sans le mot).
_NEXT_BEFORE = r"(?:next|nachsten|naechsten|kommenden|prossimo|prossima)\s+"
_NEXT_AFTER = r"\s+(?:prochain|prossimo|prossima)\b"

_MONTH_RE = "|".join(sorted(_ALL_MONTHS, key=len, reverse=True))
_ORD = r"(?:er|re|e|st|nd|rd|th)?\.?"  # 1er, 1st, 19th, « 19. » (allemand)

_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{1,2}):(\d{2}))?")
_DMY = re.compile(r"\b(\d{1,2})[./](\d{1,2})[./](\d{4}|\d{2})\b")
_DM = re.compile(r"\b(\d{1,2})[./](\d{1,2})\b(?![./]\d)")
_TIME = re.compile(r"(?<![\d.:/-])(\d{1,2})\s?(?:h|:)\s?(\d{2})?(?![\d])", re.IGNORECASE)
# « 2pm », « 2:30 pm », « 11.15am », « 9 a.m. »
_TIME_AMPM = re.compile(r"(?<![\d.:/-])(\d{1,2})(?:[:.h](\d{2}))?\s?([ap])\.?m\.?(?![a-z])")
# « 14 Uhr », « 14.30 Uhr »
_TIME_UHR = re.compile(r"(?<![\d.:/-])(\d{1,2})(?:[:.](\d{2}))?\s?uhr\b")
# « alle 14 », « alle ore 14:30 », « ore 9 », « um 14 », « um 14.30 » (pas « alle 14 Tage », « um 14 % »)
_TIME_WORD = re.compile(r"\b(?:alle\s+ore|dalle\s+ore|alle|dalle|ore|um)\s+(\d{1,2})(?:[:.](\d{2}))?(?![\d.:])"
                        r"(?!\s*(?:%|prozent|percent|per\s*cento|tage|tagen|wochen|monate|jahre|minuten|stunden|"
                        r"giorni|settimane|mesi|anni|minuti|chf|eur|euro|fr\b|franken|franchi))")
_NOON = re.compile(r"\b(?:midi|noon|mittag|mittags|mezzogiorno)\b")
_MIDNIGHT = re.compile(r"\b(?:minuit|midnight|mitternacht|mezzanotte)\b")
# « 19 novembre », « 1er mars 2027 », « 19. November », « 19th of November », « 19 nov. »
_DAY_MONTH = re.compile(r"\b(\d{1,2})" + _ORD + r"\s*(?:of\s+|de\s+|di\s+)?(" + _MONTH_RE + r")\b\.?(?:\s+(\d{4}))?")
# « November 19 », « Nov 19th, 2026 » (ordre anglais)
_MONTH_DAY = re.compile(r"\b(" + "|".join(sorted(EN_MONTHS, key=len, reverse=True)) + r")\b\.?\s+(\d{1,2})"
                        r"(?:st|nd|rd|th)?\b(?!\s*(?:h\b|:|uhr|[ap]\.?m))(?:,?\s+(\d{4}))?")
_DAY_ALONE = re.compile(r"\b(?:le|du|au|ce|il|del|dal|al|entro\s+il)\s+(\d{1,2})(?:er)?\b(?!\s*(?:h|:|[./]|%|\d))")
# « am 19. », « bis zum 19. » (allemand : le point fait l'ordinal) ; « the 19th », « on the 19th »
_DAY_ALONE_DE = re.compile(r"\b(?:am|bis|zum|vom|den|ab)\s+(\d{1,2})\.(?![\d.])(?!\s*(?:" + _MONTH_RE + r")\b)")
_DAY_ALONE_EN = re.compile(r"\bthe\s+(\d{1,2})(?:st|nd|rd|th)\b(?!\s*(?:of\s+)?(?:" + _MONTH_RE + r")\b)")


def _fold(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


def _date(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _year(y: str) -> int:
    n = int(y)
    return n + 2000 if n < 100 else n


def _hm(h: str, m: str, out: set[str]) -> None:
    if 0 <= int(h) <= 23 and (not m or 0 <= int(m) <= 59):
        out.add(f"{int(h):02d}:{int(m or 0):02d}")


def _times(text: str) -> set[str]:
    """Heures écrites dans `text` (`HH:MM`) : « 14h », « 14:30 », « 2pm », « 14 Uhr », « alle 14 », « um 9 », « midi ».
    Une forme reconnue est retirée du texte avant la suivante : « 2:30pm » ne donne pas aussi 02:30."""
    t = _fold(text)
    out: set[str] = set()
    for mt in list(_TIME_AMPM.finditer(t)):
        h, m, ap = mt.groups()
        # « 3 am Montag » (allemand « am ») n'est pas « 3 a.m. » : « am » détaché suivi d'un mot ne compte pas
        if ap == "a" and " " in mt.group(0) and "." not in mt.group(0)[-4:] and re.match(r"\s+[a-z]", t[mt.end():]):
            continue
        if 1 <= int(h) <= 12:
            _hm(str(int(h) % 12 + (12 if ap == "p" else 0)), m, out)
        t = t[:mt.start()] + " " * (mt.end() - mt.start()) + t[mt.end():]
    for h, m in _TIME_UHR.findall(t):
        _hm(h, m, out)
    t = _TIME_UHR.sub(" ", t)
    for h, m in _TIME_WORD.findall(t):
        _hm(h, m, out)
    t = _TIME_WORD.sub(" ", t)
    if _NOON.search(t):
        out.add("12:00")
    if _MIDNIGHT.search(t):
        out.add("00:00")
    for h, m in _TIME.findall(t):
        _hm(h, m, out)
    return out


# Fin d'un créneau : son heure découle du début et d'une durée, la demande ne la nomme presque jamais. Son JOUR reste
# contrôlé ; son heure, non.
END_KEYS = frozenset({"end", "fin", "end_time", "ends_at", "end_at", "until", "to"})
_TZ_SUFFIX = re.compile(r"(\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)(?:Z|[+-]\d{2}:?\d{2})\b")


def card_values(arguments: Any) -> tuple[set[str], set[str]]:
    """(dates, heures) écrites dans les arguments de la carte (formes absolues seulement)."""
    texts: list[tuple[str, bool]] = []

    def walk(v: Any, end: bool = False) -> None:
        if isinstance(v, dict):
            for k, x in v.items():
                walk(x, end or str(k).lower() in END_KEYS)
        elif isinstance(v, list):
            for x in v:
                walk(x, end)
        elif isinstance(v, str):
            texts.append((v, end))

    walk(arguments)
    dates, times = set(), set()
    for raw, end in texts:
        t = _TZ_SUFFIX.sub(r"\1", raw)  # décalage de fuseau (« +02:00 », « Z ») : pas une heure
        found: set[str] = set()
        rest = t
        for y, mo, d, hh, mm in _ISO.findall(t):
            dt = _date(int(y), int(mo), int(d))
            if dt:
                dates.add(dt.isoformat())
            if hh:
                found.add(f"{int(hh):02d}:{mm}")
        rest = _ISO.sub(" ", rest)
        for d, mo, y in _DMY.findall(rest):
            dt = _date(_year(y), int(mo), int(d))
            if dt:
                dates.add(dt.isoformat())
        rest = _DMY.sub(" ", rest)
        if len(rest) <= 80:  # une heure seule (« 15:00 », « 3pm ») dans un champ court ; jamais dans un texte libre
            if ":" in rest or "h" in rest.lower() or _TIME_AMPM.search(_fold(rest)):
                found |= _times(rest)
        if not end:
            times |= found
    return dates, times


def request_values(text: str, now: datetime) -> tuple[set[str], set[str]]:
    """(dates, heures) que la demande nomme, relatives résolues à `now` (déjà dans le fuseau du compte)."""
    t = _fold(text or "")
    today = now.date()
    dates: set[str] = set()
    for y, mo, d, hh, mm in _ISO.findall(t):
        dt = _date(int(y), int(mo), int(d))
        if dt:
            dates.add(dt.isoformat())
    for d, mo, y in _DMY.findall(t):
        dt = _date(_year(y), int(mo), int(d))
        if dt:
            dates.add(dt.isoformat())
    for d, mo in _DM.findall(_DMY.sub(" ", t)):
        dt = _next_dm(today, int(d), int(mo))
        if dt:
            dates.add(dt.isoformat())
    for d, name, y in _DAY_MONTH.findall(t):
        m = _ALL_MONTHS[name]
        dt = _date(int(y), m, int(d)) if y else _next_dm(today, int(d), m)
        if dt:
            dates.add(dt.isoformat())
    for name, d, y in _MONTH_DAY.findall(t):
        m = _ALL_MONTHS[name]
        dt = _date(int(y), m, int(d)) if y else _next_dm(today, int(d), m)
        if dt:
            dates.add(dt.isoformat())
    for rx in (_DAY_ALONE, _DAY_ALONE_DE, _DAY_ALONE_EN):
        for d in rx.findall(t):
            dt = _next_day(today, int(d))
            if dt:
                dates.add(dt.isoformat())
    # Relatifs. Les formes à deux jours sont retirées avant « demain » (« après-demain », « day after tomorrow »).
    if re.search(r"\baujourd'?hui\b|\btoday\b|\btonight\b|\bthis\s+(?:morning|afternoon|evening)\b|\bce soir\b"
                 r"|\bce matin\b|\bcet apres-midi\b|\bheute\b|\boggi\b|\bstasera\b|\bstamattina\b|\bstamani\b", t):
        dates.add(today.isoformat())
    two = re.compile(r"\bapres[- ]demain\b|\b(?:the\s+)?day\s+after\s+tomorrow\b|\bubermorgen\b|\bdopodomani\b")
    if two.search(t):
        dates.add((today + timedelta(days=2)).isoformat())
    rest = two.sub(" ", t)
    # « morgen » allemand = demain, sauf « guten Morgen », « am Morgen », « heute/jeden/diesen Morgen » (le matin)
    if re.search(r"\bdemain\b|\btomorrow\b|\bdomani\b|(?<!guten )(?<!am )(?<!heute )(?<!jeden )(?<!diesen )"
                 r"(?<!morgen )\bmorgen\b", rest):
        dates.add((today + timedelta(days=1)).isoformat())
    for name, wd in WEEKDAYS.items():
        if re.search(rf"\b{name}\b", t):
            delta = (wd - today.weekday()) % 7
            if delta == 0 and re.search(rf"\b{name}{_NEXT_AFTER}|\b{_NEXT_BEFORE}{name}\b", t):
                delta = 7
            if delta:  # le jour même sans « prochain » : ambigu (aujourd'hui ou dans 7 jours), rien n'est résolu
                dates.add((today + timedelta(days=delta)).isoformat())
    return dates, _times(_DMY.sub(" ", _ISO.sub(" ", t)))


def _next_dm(today: date, d: int, m: int) -> date | None:
    for y in (today.year, today.year + 1):
        dt = _date(y, m, d)
        if dt and dt >= today:
            return dt
    return None


def _next_day(today: date, d: int) -> date | None:
    """« le 24 » : ce mois-ci s'il n'est pas passé, sinon le mois suivant."""
    y, m = today.year, today.month
    for _ in range(2):
        dt = _date(y, m, d)
        if dt and dt >= today:
            return dt
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return None


def unmatched(request: str, arguments: Any, now: datetime) -> list[str]:
    """Dates (`AAAA-MM-JJ`) et heures (`HH:MM`) de la carte que la demande ne nomme pas ; vide = rien à redire."""
    cd, ct = card_values(arguments)
    if not cd and not ct:
        return []
    rd, rt = request_values(request, now)
    return sorted(cd - rd) + sorted(ct - rt)


_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# ─────────── adresse désignée par la demande et lue dans un résultat d'outil de lecture (provenance) ───────────
#
# Une adresse absente de la demande est admise SEULEMENT si (1) la demande DÉSIGNE sa place (« réponds à
# l'expéditeur », « au dernier message », « à toute l'équipe »…) ET (2) elle figure mot pour mot, à cette place, dans
# le résultat d'un outil de LECTURE de la trajectoire :
#   - expéditeur / dernier message → en-tête From / Reply-To / Sender d'un `mail_read` (jamais le corps du message) ;
#   - « à tous » → les mêmes en-têtes plus To / Cc ;
#   - équipe → un en-tête d'un `mail_read`, ou une ligne d'un fichier / de la mémoire / du carnet d'adresses, qui nomme
#     l'équipe et ne porte aucune consigne (« envoie aussi à », « mets en copie », « forward to »…).
# Une page web, un résultat de recherche, un agenda, un sous-agent ne blanchissent jamais une adresse (sources écrites
# par des tiers, `nokker/qa/provenance.py`). Rien d'autre ne change : sans désignation, ou sans cette source, escalade.

_DESIGNATE_SENDER = re.compile(
    r"\brepon(?:d|s|se|dre)\w*|\bexpediteu\w*|\bdernier\s+(?:message|mail|courriel|e-?mail)"
    r"|\breply\w*|\brespond\w*|\bsender\b|\b(?:last|latest)\s+(?:message|mail|e-?mail)"
    r"|\bantwort\w*|\babsender\w*|\bletzte[nmrs]?\s+(?:nachricht|mail|e-?mail)"
    r"|\brispond\w*|\brisposta\b|\bmittente\b|\bultim[oa]\s+(?:messaggio|mail|e-?mail)")
_DESIGNATE_ALL = re.compile(r"\ba\s+tous\b|\breply[- ]?all\b|\b(?:reply|respond)\s+to\s+(?:all|everyone)\b"
                            r"|\ballen\s+antworten\b|\ba\s+tutti\b")
_TEAM = re.compile(r"\bequipe\w*|\bteams?\b|\bsquadra\b|\bcollegues?\b|\bcolleagues?\b|\bkolleg\w*"
                   r"|\bcolleghi\b|\bteam\w*")
# Une ligne qui DONNE une adresse comme consigne (injection) ne la blanchit jamais.
_DIRECTIVE = re.compile(
    r"\benvo[iy]\w*|\btransf[e]r\w*|\btransmet\w*|\bajout\w*|\ben\s+copie\b|\bcopie\b|\bcc\b|\bbcc\b"
    r"|\becri[sv]\w*|\bforward\w*|\bsend\w*|\badd\b|\binclude\w*|\bcopy\b|\bwrite\s+to\b"
    r"|\bschick\w*|\bsende\w*|\bweiterleit\w*|\bfug\w*|\bschreib\w*|\binoltr\w*|\binvi[ao]\w*|\bmand[ai]\w*"
    r"|\baggiung\w*|\bscriv\w*|\baussi\b|\begalement\b|\balso\b|\bauch\b|\banche\b|\binstead\b|\bplutot\b"
    r"|\bstattdessen\b|\binvece\b|\bignore\w*|\bignor\w*")

MAIL_READ_TOOLS = frozenset({"mail_read"})
# Lectures internes (fichier, mémoire, carnet d'adresses) : admises pour une adresse d'équipe seulement.
INTERNAL_READ_TOOLS = frozenset({"read_file", "search_files", "memory_get", "memory_search", "recall_context",
                                 "contacts_search", "contacts_get", "contacts_list", "contacts_read"})
_SENDER_KEYS = frozenset({"from", "reply_to", "sender"})
_ALL_KEYS = _SENDER_KEYS | {"to", "cc"}
# En-têtes d'un résultat texte (bloc de tête « Clé : valeur » jusqu'à la première ligne vide), forme repliée.
_HEADER_NAMES = {"from": "from", "de": "from", "von": "from", "da": "from", "expediteur": "from", "absender": "from",
                 "mittente": "from", "sender": "sender", "reply-to": "reply_to", "reply_to": "reply_to",
                 "reply to": "reply_to", "repondre a": "reply_to", "antwort an": "reply_to", "rispondi a": "reply_to",
                 "to": "to", "a": "to", "an": "to", "destinataire": "to", "destinataires": "to", "empfanger": "to",
                 "destinatario": "to", "destinatari": "to", "cc": "cc", "kopie": "cc", "copia": "cc"}
_HEADER_LINE = re.compile(r"^\s*([A-Za-z][A-Za-z_\- ]{0,19}?)\s*:\s*(.*)$")


def _header_addresses(value: Any) -> set[str]:
    """Adresses d'un en-tête (« Nom <a@b> », liste) : celle entre chevrons quand il y en a une — une adresse glissée dans
    le nom affiché (« "x@evil" <a@b> ») ne compte pas."""
    from email.utils import getaddresses

    vals = value if isinstance(value, list) else [value]
    out: set[str] = set()
    for _name, addr in getaddresses([str(v) for v in vals if v is not None]):
        if _EMAIL.fullmatch(addr or ""):
            out.add(addr.lower())
    return out


def _mail_headers(result: Any) -> list[tuple[str, str, set[str]]]:
    """(clé normalisée, valeur brute, adresses) des en-têtes d'un résultat de `mail_read` : JSON du connecteur, sinon
    bloc de tête « Clé : valeur » d'un texte. Le corps n'en fait jamais partie."""
    out: list[tuple[str, str, set[str]]] = []
    data: Any = result
    if isinstance(result, str):
        try:
            data = json.loads(result)
        except (ValueError, TypeError):
            data = None
    if isinstance(data, dict):
        for k, v in data.items():
            key = str(k).lower().replace("-", "_")
            if key in _ALL_KEYS and isinstance(v, (str, list)):
                out.append((key, json.dumps(v, ensure_ascii=False) if isinstance(v, list) else v,
                            _header_addresses(v)))
        return out
    if not isinstance(result, str):
        return out
    for line in result.splitlines():
        if not line.strip():
            break
        m = _HEADER_LINE.match(_fold(line))
        if not m:
            break
        key = _HEADER_NAMES.get(m.group(1).strip())
        if key:
            out.append((key, m.group(2), _header_addresses(m.group(2))))
    return out


def designated_addresses(request: str, trajectory: Iterable[dict[str, Any]] | None) -> set[str]:
    """Adresses que la demande désigne sans les écrire ET qu'un outil de lecture de la trajectoire a rendues à la place
    désignée (voir plus haut). Vide si la demande ne désigne rien."""
    t = _fold(request or "")
    sender = bool(_DESIGNATE_SENDER.search(t))
    everyone = sender and bool(_DESIGNATE_ALL.search(t))
    team = bool(_TEAM.search(t))
    if not (sender or team) or not trajectory:
        return set()
    out: set[str] = set()
    for it in trajectory:
        name = str((it or {}).get("name") or "")
        res = (it or {}).get("result")
        if res is None:
            continue
        if name in MAIL_READ_TOOLS:
            for key, raw, addrs in _mail_headers(res):
                if sender and key in (_ALL_KEYS if everyone else _SENDER_KEYS):
                    out |= addrs
                elif team and _TEAM.search(_fold(raw)):
                    out |= addrs
        elif team and name in INTERNAL_READ_TOOLS:
            text = res if isinstance(res, str) else json.dumps(res, ensure_ascii=False)
            for line in text.splitlines():
                f = _fold(line)
                if _TEAM.search(f) and not _DIRECTIVE.search(f):
                    out |= {a.lower() for a in _EMAIL.findall(line)}
    return out


def unmatched_addresses(request: str, arguments: Any, own: Iterable[str] = (),
                        trajectory: Iterable[dict[str, Any]] | None = None) -> list[str]:
    """Adresses de courriel des arguments (un destinataire, un agenda, un invité) que ni la demande ni les adresses du
    compte (`own`) ne nomment : la carte agirait pour quelqu'un que l'utilisateur n'a pas désigné. Avec `trajectory`
    (appels du tour avant la carte), une adresse que la demande désigne sans l'écrire et qu'un outil de lecture a rendue
    à cette place est admise (`designated_addresses`)."""
    known = {a.lower() for a in _EMAIL.findall(request or "")} | {a.lower() for a in own}
    found = {a.lower() for a in _EMAIL.findall(json.dumps(arguments, ensure_ascii=False))}
    strangers = found - known
    if strangers and trajectory:
        strangers -= designated_addresses(request, list(trajectory))
    return sorted(strangers)


__all__ = ["card_values", "designated_addresses", "request_values", "unmatched", "unmatched_addresses"]
