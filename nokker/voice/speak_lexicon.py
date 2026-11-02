"""Lexique de prononciation du foyer et normalisation suisse avant la synthèse vocale (docs/VOICE.md §6).

Une couche PURE et déterministe, posée juste AVANT l'appel au fournisseur de voix (Telegram et voix de la pièce) :
le modèle ne la voit jamais, ni en entrée ni en sortie (le texte écrit ne change pas, seul le texte envoyé au TTS).

  1. le lexique du foyer (mot ou groupe de mots → prononciation) est appliqué d'abord, protégé par des jetons ;
  2. la normalisation fr-CH (langue `fr` seulement) : IBAN et références épelés par groupes, téléphones suisses,
     dates et heures, montants CHF/EUR, nombres (apostrophe des milliers, septante / huitante|quatre-vingt / nonante),
     ordinaux, abréviations (env., p. ex., St-, Av., ch. …), sigles épelés (table) ;
  3. les jetons du lexique sont remplacés par leur prononciation.

Drapeau `speak_lexicon.enabled` ÉTEINT par défaut : `service()` est None, le texte part au TTS tel quel (octet pour
octet). Texte sans rien à normaliser : renvoyé inchangé (aucun nettoyage d'espaces ici).

Stockage du lexique : `PgLexiconStore` (table `household_lexicon`, RLS du locataire du foyer, migration 060) ou
`MemoryLexiconStore` (Lokkal : entrées posées dans la config `speak_lexicon.entries`, lecture seule côté routes).
"""
from __future__ import annotations

import calendar
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger("nokker.voice.speak_lexicon")

MAX_ENTRIES = 500
MAX_WORD = 80
MAX_SAY = 120

# ───────────────────────────── nombres en toutes lettres (fr-CH) ─────────────────────────────

_UNITS = ["zéro", "un", "deux", "trois", "quatre", "cinq", "six", "sept", "huit", "neuf", "dix", "onze", "douze",
          "treize", "quatorze", "quinze", "seize", "dix-sept", "dix-huit", "dix-neuf"]
_TENS = {20: "vingt", 30: "trente", 40: "quarante", 50: "cinquante", 60: "soixante"}
_MAX_N = 999_999_999_999


def _below_100(n: int, eighty: str) -> str:
    if n < 20:
        return _UNITS[n]
    t, u = divmod(n, 10)
    if t in (2, 3, 4, 5, 6):
        base = _TENS[t * 10]
        if u == 0:
            return base
        return f"{base} et un" if u == 1 else f"{base}-{_UNITS[u]}"
    if t == 7:  # septante (Suisse romande)
        return "septante" if u == 0 else ("septante et un" if u == 1 else f"septante-{_UNITS[u]}")
    if t == 8:
        if eighty == "huitante":
            return "huitante" if u == 0 else ("huitante et un" if u == 1 else f"huitante-{_UNITS[u]}")
        return "quatre-vingts" if u == 0 else f"quatre-vingt-{_UNITS[u]}"
    return "nonante" if u == 0 else ("nonante et un" if u == 1 else f"nonante-{_UNITS[u]}")


def _below_1000(n: int, eighty: str, final: bool) -> str:
    h, r = divmod(n, 100)
    if h == 0:
        w = _below_100(r, eighty)
    elif r == 0:
        w = "cent" if h == 1 else f"{_UNITS[h]} cent" + ("s" if final else "")
    else:
        w = ("cent " if h == 1 else f"{_UNITS[h]} cent ") + _below_100(r, eighty)
    if not final and w.endswith("quatre-vingts"):
        w = w[:-1]  # quatre-vingt mille
    return w


def number_to_words(n: int, eighty: str = "quatre-vingts") -> str:
    """Entier → lettres. `eighty` : « huitante » (VD, VS, FR…) ou « quatre-vingts » (GE, NE, JU…). Au-delà de
    999 999 999 999 : les chiffres, inchangés."""
    if n < 0:
        w = number_to_words(-n, eighty)
        return w if w.lstrip("-").isdigit() else "moins " + w
    if n > _MAX_N:
        return str(n)
    if n == 0:
        return "zéro"
    parts: list[str] = []
    for scale, name in ((1_000_000_000, "milliard"), (1_000_000, "million")):
        q, n = divmod(n, scale)
        if q:
            parts.append(f"{_below_1000(q, eighty, True)} {name}" + ("s" if q > 1 else ""))
    q, n = divmod(n, 1000)
    if q:
        parts.append("mille" if q == 1 else f"{_below_1000(q, eighty, False)} mille")
    if n:
        parts.append(_below_1000(n, eighty, True))
    return " ".join(parts)


def _digits_words(s: str) -> str:
    return " ".join(_UNITS[int(c)] for c in s if c.isdigit())


def _ordinal(n: int, eighty: str) -> str:
    if n == 1:
        return "premier"
    w = number_to_words(n, eighty)
    if w.endswith(("quatre-vingts", "cents", "millions", "milliards")):
        w = w[:-1]
    for end, rep in (("cinq", "cinqu"), ("neuf", "neuv")):
        if w.endswith(end):
            return w[: -len(end)] + rep + "ième"
    w = w.removesuffix("e")
    return w + "ième"


# ───────────────────────────── options ─────────────────────────────

DEFAULT_ACRONYMS: dict[str, str] = {
    "CFF": "C F F", "SBB": "S B B", "FFS": "F F S", "AVS": "A V S", "LPP": "L P P", "TVA": "T V A",
    "NPA": "N P A", "OFAS": "O F A S", "UBS": "U B S", "TPG": "T P G", "SMS": "S M S", "PDF": "P D F",
    "USB": "U S B",
}


@dataclass(frozen=True)
class SpeakOptions:
    eighty: str = "quatre-vingts"  # ou « huitante »
    acronyms: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_ACRONYMS))

    @staticmethod
    def from_raw(raw: dict[str, Any] | None) -> SpeakOptions:
        raw = raw or {}
        e = str(raw.get("eighty") or "quatre-vingts").strip().lower()
        eighty = "huitante" if e.startswith("huit") else "quatre-vingts"
        acr = dict(DEFAULT_ACRONYMS)
        for k, v in (raw.get("acronyms") or {}).items():
            k = str(k).strip()
            if not k:
                continue
            if v in (None, False, "", "keep"):  # un sigle qu'on lit comme un mot
                acr.pop(k, None)
            else:
                acr[k] = str(v)
        return SpeakOptions(eighty=eighty, acronyms=acr)


# ───────────────────────────── normalisation fr-CH ─────────────────────────────

_MONTHS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre",
           "novembre", "décembre"]
_AMT = r"(?P<{p}int>\d{{1,3}}(?:['’]\d{{3}})+|\d+)(?:[.,](?P<{p}dec>\d{{1,2}}|[-–—]))?"
_CUR = {"CHF": ("franc", "centime"), "FR": ("franc", "centime"), "SFR": ("franc", "centime"),
        "EUR": ("euro", "centime"), "€": ("euro", "centime")}


def _int(s: str) -> int:
    return int(re.sub(r"['’]", "", s))


def _amount(sign: str, ipart: str, dec: str | None, cur: str, eighty: str) -> str:
    unit, cent = _CUR[cur.upper().rstrip(".")]
    n = _int(ipart)
    cents = int(dec.ljust(2, "0")) if dec and dec[0].isdigit() else 0
    out = ""
    if n > _MAX_N:
        return ""
    if n or not cents:
        de = " de" if n and n % 1_000_000 == 0 else ""
        out = f"{number_to_words(n, eighty)}{de} {unit}" + ("s" if n > 1 else "")
    if cents:
        w = number_to_words(cents, eighty)
        if n == 0:
            w += f" {cent}" + ("s" if cents > 1 else "")
        out = f"{out} {w}".strip()
    return ("moins " if sign else "") + out


_RE_CUR_PRE = re.compile(r"(?<![\w])(?P<sign>-)?(?P<cur>CHF|SFr\.?|Fr\.|EUR|€)\s?" + _AMT.format(p="a") + r"(?![\w])",
                         re.IGNORECASE)
_RE_CUR_POST = re.compile(r"(?<![\w.,'’])(?P<sign>-)?" + _AMT.format(p="b") + r"\s?(?P<cur>CHF|EUR|€|Fr\.)(?![\w])",
                          re.IGNORECASE)

_RE_IBAN = re.compile(r"(?<![\w])[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){3,7}(?: ?[A-Z0-9]{1,3})?(?![\w])")
_RE_QRREF = re.compile(r"(?<![\w])\d{2}(?: \d{5}){5}(?![\w])")
_RE_PHONE_INTL = re.compile(r"(?<![\w+])(?:\+41|0041)[\s.]?\(?0?\)?[\s.]?(\d{2})[\s.]?(\d{3})[\s.]?(\d{2})[\s.]?(\d{2})(?!\d)")
_RE_PHONE_LOCAL = re.compile(r"(?<![\w+.])(0[1-9]\d)[\s.]?(\d{3})[\s.]?(\d{2})[\s.]?(\d{2})(?![\w])")

_RE_DATE3 = re.compile(r"(?<![\w.,'’])(\d{1,2})\.(\d{1,2})\.(\d{4}|\d{2})(?![\w]|[.,]\d)")
_RE_DATE2_DOT = re.compile(r"(?<![\w.,'’])(\d{1,2})\.(\d{1,2})\.(?!\d)")
_RE_DATE2_CTX = re.compile(r"(?<![\w.,'’])((?:le|du|au|dès le|depuis le|jusqu'au|jusqu’au)\s)(\d{1,2})\.(\d{1,2})(?![\w]|[.,]\d)",
                           re.IGNORECASE)
_RE_TIME = re.compile(r"(?<![\w.,:'’])([01]?\d|2[0-3])(?:\s?h\s?([0-5]\d)?|:([0-5]\d))(?![\w:])")
_RE_ORD = re.compile(r"(?<![\w.,'’])(\d+)(er|re|e|ème|eme)(?![\w])")
_RE_NUM = re.compile(r"(?<![\w.,'’])(-?)(\d{1,3}(?:['’]\d{3})+|\d+)(?:[.,](\d+))?(\s?%)?(?![\w]|[.,'’]\d)")

_ABBR: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?<![\w.])env\.(?=\s)"), "environ"),
    (re.compile(r"(?<![\w.])[Pp]\.\s?ex\.(?=\s|,|$)"), "par exemple"),
    (re.compile(r"(?<![\w.])c\.-à-d\."), "c'est-à-dire"),
    (re.compile(r"(?<![\w.])etc\.(?=\s|,|$)"), "et cetera"),
    (re.compile(r"(?<![\w])Ste-(?=[A-ZÉÈ])"), "Sainte-"),
    (re.compile(r"(?<![\w])St-(?=[A-ZÉÈ])"), "Saint-"),
    (re.compile(r"(?<![\w])Ste\.?\s(?=[A-ZÉÈ])"), "Sainte "),
    (re.compile(r"(?<![\w])St\.?\s(?=[A-ZÉÈ])"), "Saint "),
    (re.compile(r"(?<![\w.])Av\.(?=\s)"), "Avenue"),
    (re.compile(r"(?<![\w.])ch\.(?=\s(?:de |du |des |d'|d’|[A-ZÉÈ]))"), "chemin"),
    (re.compile(r"(?<![\w.])Ch\.(?=\s(?:de |du |des |d'|d’|[A-ZÉÈ]))"), "Chemin"),
    (re.compile(r"(?<![\w.])Bd\.?(?=\s)"), "Boulevard"),
    (re.compile(r"(?<![\w.])Pl\.(?=\s)"), "Place"),
    (re.compile(r"(?<![\w.])[Rr]te\.?(?=\s)"), "Route"),
    (re.compile(r"(?<![\w.])Mme(?=\s)"), "Madame"),
    (re.compile(r"(?<![\w.])Mlle(?=\s)"), "Mademoiselle"),
    (re.compile(r"(?<![\w.])Dr\.?(?=\s[A-ZÉÈ])"), "Docteur"),
    (re.compile(r"(?<![\w.])M\.(?=\s[A-ZÉÈ])"), "Monsieur"),
    (re.compile(r"(?<![\w.])[Tt]él\.(?=\s|$)"), "téléphone"),
    (re.compile(r"(?<![\w.])n°\s?"), "numéro "),
    (re.compile(r"(?<![\w.])N°\s?"), "Numéro "),
]


def _spell_groups(s: str) -> str:
    """Lettres et chiffres un à un, par groupes séparés par une virgule (pause de la voix)."""
    def one(g: str) -> str:
        return " ".join(_UNITS[int(c)] if c.isdigit() else c.upper() for c in g)

    s = s.replace(" ", "")
    return ", ".join(one(s[i:i + 4]) for i in range(0, len(s), 4))


def _valid_date(d: int, m: int, y: int | None = None) -> bool:
    if not 1 <= m <= 12 or d < 1:
        return False
    return d <= calendar.monthrange(y if y else 2024, m)[1]  # sans année : 29.02 admis (bissextile)


def _day(d: int) -> str:
    return "premier" if d == 1 else number_to_words(d)


def _year(y: int, eighty: str) -> str:
    return number_to_words(y, eighty)


def normalize_fr_ch(text: str, opts: SpeakOptions | None = None) -> str:
    """Normalisation fr-CH du texte à dire. Un texte sans rien à normaliser est rendu tel quel."""
    opts = opts or SpeakOptions()
    e = opts.eighty
    out = text

    out = _RE_IBAN.sub(lambda m: _spell_groups(m.group(0)), out)
    out = _RE_QRREF.sub(lambda m: _spell_groups(m.group(0)), out)

    def intl(m: re.Match[str]) -> str:
        return "plus quarante et un, " + ", ".join(_digits_words(g) for g in m.groups())

    out = _RE_PHONE_INTL.sub(intl, out)
    out = _RE_PHONE_LOCAL.sub(lambda m: ", ".join(_digits_words(g) for g in m.groups()), out)

    def date3(m: re.Match[str]) -> str:
        d, mo, ys = int(m[1]), int(m[2]), m[3]
        y = int(ys) if len(ys) == 4 else 2000 + int(ys)
        if not _valid_date(d, mo, y):
            return m[0]
        return f"{_day(d)} {_MONTHS[mo - 1]} {_year(y, e)}"

    out = _RE_DATE3.sub(date3, out)
    out = _RE_DATE2_DOT.sub(lambda m: f"{_day(int(m[1]))} {_MONTHS[int(m[2]) - 1]}"
                            if _valid_date(int(m[1]), int(m[2])) and (len(m[1]) == 2 or len(m[2]) == 2) else m[0], out)
    out = _RE_DATE2_CTX.sub(lambda m: f"{m[1]}{_day(int(m[2]))} {_MONTHS[int(m[3]) - 1]}"
                            if _valid_date(int(m[2]), int(m[3])) else m[0], out)

    def time_(m: re.Match[str]) -> str:
        h = int(m[1])
        mins = m[2] or m[3]
        w = ("une heure" if h == 1 else f"{number_to_words(h, e)} heure" if h == 0 else f"{number_to_words(h, e)} heures")
        if h == 0 and not (mins and int(mins)):
            w = "minuit"
        if mins and int(mins):
            w += " " + number_to_words(int(mins), e)
        return w

    out = _RE_TIME.sub(time_, out)

    def cur_pre(m: re.Match[str]) -> str:
        return _amount(m["sign"] or "", m["aint"], m["adec"], m["cur"], e)

    def cur_post(m: re.Match[str]) -> str:
        return _amount(m["sign"] or "", m["bint"], m["bdec"], m["cur"], e)

    out = _RE_CUR_PRE.sub(cur_pre, out)
    out = _RE_CUR_POST.sub(cur_post, out)

    def ordinal(m: re.Match[str]) -> str:
        n, suf = int(m[1]), m[2]
        if n == 1 and suf in ("er", "re"):
            return "première" if suf == "re" else "premier"
        if suf in ("er", "re"):
            return m[0]
        return _ordinal(n, e) if n else m[0]

    out = _RE_ORD.sub(ordinal, out)

    def num(m: re.Match[str]) -> str:
        neg, ipart, dec, pct = m[1], m[2], m[3], m[4]
        n = _int(ipart)
        if n > _MAX_N:
            return m[0]
        w = number_to_words(n, e)
        if dec is not None:
            lead = len(dec) - len(dec.lstrip("0"))
            w += " virgule " + (_digits_words("0" * lead) + " " if lead and lead < len(dec) else "")
            rest = dec.lstrip("0")
            w += (number_to_words(int(rest), e) if rest else _digits_words(dec)) if len(dec) <= 3 else _digits_words(dec)
            w = w.replace("  ", " ")
        if neg:
            w = "moins " + w
        return w + (" pour cent" if pct else "")

    out = _RE_NUM.sub(num, out)

    for pat, rep in _ABBR:
        out = pat.sub(rep, out)
    if opts.acronyms:
        names = sorted(opts.acronyms, key=len, reverse=True)
        pat = re.compile(r"(?<![\w])(" + "|".join(re.escape(n) for n in names) + r")(?![\w])")
        out = pat.sub(lambda m: opts.acronyms[m[1]], out)
    return out


# ───────────────────────────── lexique du foyer ─────────────────────────────

_SENT_OPEN, _SENT_CLOSE, _SENT_BASE = "", "", 0xE100


def entry_key(word: str) -> str:
    """Clé d'une entrée : minuscules, espaces repliés. « Léa » et « léa » sont la même entrée."""
    return re.sub(r"\s+", " ", word.strip()).casefold()


def apply_lexicon(text: str, entries: dict[str, str]) -> tuple[str, list[str]]:
    """Remplace chaque mot ou groupe de mots du lexique par un jeton (les règles suivantes ne le voient pas).
    `entries` : clé → prononciation. Renvoie (texte avec jetons, prononciations par numéro de jeton)."""
    if not entries or not text:
        return text, []
    keys = sorted(entries, key=len, reverse=True)
    pat = re.compile(r"(?<![\w])(" + "|".join(r"\s+".join(re.escape(w) for w in k.split(" ")) for k in keys)
                     + r")(?![\w])", re.IGNORECASE)
    spoken: list[str] = []

    def sub(m: re.Match[str]) -> str:
        say = entries.get(entry_key(m[1]))
        if say is None:
            return m[0]
        spoken.append(say)
        idx = "".join(chr(_SENT_BASE + int(c)) for c in str(len(spoken) - 1))
        return _SENT_OPEN + idx + _SENT_CLOSE

    return pat.sub(sub, text), spoken


_RE_FORGED = re.compile("[\ue000\ue001\ue100-\ue109]")
_RE_SENT = re.compile(_SENT_OPEN + "([-]+)" + _SENT_CLOSE)


def restore(text: str, spoken: list[str]) -> str:
    def sub(m: re.Match[str]) -> str:
        i = int("".join(str(ord(c) - _SENT_BASE) for c in m[1]))
        return spoken[i] if i < len(spoken) else ""

    return _RE_SENT.sub(sub, text)


def speak_prepare(text: str, lang: str, entries: dict[str, str] | None = None,
                  opts: SpeakOptions | None = None) -> str:
    """Le texte envoyé au TTS : lexique du foyer (toutes langues), normalisation fr-CH (`fr` seulement)."""
    if not text:
        return text
    if _RE_FORGED.search(text):  # le texte ne peut pas fabriquer un jeton
        text = _RE_FORGED.sub("", text)
    protected, spoken = apply_lexicon(text, entries or {})
    if lang == "fr":
        protected = normalize_fr_ch(protected, opts)
    return restore(protected, spoken) if spoken else protected


# ───────────────────────────── stockage ─────────────────────────────

@dataclass(frozen=True)
class SpeakScope:
    """Le foyer dont on lit le lexique : son locataire (le payeur) et son identifiant."""

    tenant_id: str
    household_id: str


class LexiconError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class LexiconStore(Protocol):
    def entries(self, scope: SpeakScope) -> dict[str, str]: ...  # clé → prononciation

    def listing(self, scope: SpeakScope) -> list[dict[str, Any]]: ...

    def put(self, scope: SpeakScope, word: str, say: str, by: str) -> None: ...

    def delete(self, scope: SpeakScope, word: str) -> bool: ...


def check_entry(word: Any, say: Any) -> tuple[str, str]:
    if not isinstance(word, str) or not isinstance(say, str):
        raise LexiconError(400, "bad_entry", "word et say attendus (texte)")
    word, say = word.strip(), say.strip()
    if not word or not say:
        raise LexiconError(400, "bad_entry", "word et say ne peuvent pas être vides")
    if len(word) > MAX_WORD or len(say) > MAX_SAY:
        raise LexiconError(400, "too_long", f"word ≤ {MAX_WORD} et say ≤ {MAX_SAY} caractères")
    if any(ord(c) < 32 or 0xE000 <= ord(c) <= 0xF8FF for c in word + say):
        raise LexiconError(400, "bad_entry", "caractères de contrôle refusés")
    return word, say


class MemoryLexiconStore:
    """Lexique en mémoire, cloisonné par (locataire, foyer). `fixed` : entrées de la config (Lokkal), servies à TOUS les
    scopes et en lecture seule (modifier = éditer la config)."""

    def __init__(self, fixed: dict[str, str] | None = None) -> None:
        self._fixed = {entry_key(k): str(v) for k, v in (fixed or {}).items() if str(k).strip() and str(v).strip()}
        self._data: dict[tuple[str, str], dict[str, tuple[str, str, str]]] = {}
        self._lock = threading.Lock()

    @property
    def read_only(self) -> bool:
        return bool(self._fixed)

    def entries(self, scope: SpeakScope) -> dict[str, str]:
        with self._lock:
            mine = {k: v[1] for k, v in self._data.get((scope.tenant_id, scope.household_id), {}).items()}
        return {**self._fixed, **mine}

    def listing(self, scope: SpeakScope) -> list[dict[str, Any]]:
        with self._lock:
            mine = dict(self._data.get((scope.tenant_id, scope.household_id), {}))
        rows = [{"word": k, "say": v, "source": "config"} for k, v in self._fixed.items() if k not in mine]
        rows += [{"word": w, "say": s, "source": "foyer"} for _, (w, s, _by) in mine.items()]
        return sorted(rows, key=lambda r: r["word"].casefold())

    def put(self, scope: SpeakScope, word: str, say: str, by: str) -> None:
        if self.read_only:
            raise LexiconError(409, "read_only", "lexique fixé par la configuration (speak_lexicon.entries)")
        with self._lock:
            d = self._data.setdefault((scope.tenant_id, scope.household_id), {})
            if entry_key(word) not in d and len(d) >= MAX_ENTRIES:
                raise LexiconError(409, "full", f"{MAX_ENTRIES} entrées au plus")
            d[entry_key(word)] = (word, say, by)

    def delete(self, scope: SpeakScope, word: str) -> bool:
        if self.read_only:
            raise LexiconError(409, "read_only", "lexique fixé par la configuration (speak_lexicon.entries)")
        with self._lock:
            return self._data.get((scope.tenant_id, scope.household_id), {}).pop(entry_key(word), None) is not None


class PgLexiconStore:
    """Table `household_lexicon` (migration 060) : RLS activée et forcée sur le locataire du foyer."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def _for(self, tenant_id: str):
        from nokker.store.conn import connect_tenant

        return connect_tenant(self.dsn, tenant_id)

    def entries(self, scope: SpeakScope) -> dict[str, str]:
        with self._for(scope.tenant_id) as conn:
            rows = conn.execute("SELECT word_key, say FROM household_lexicon WHERE tenant_id=%s AND household_id=%s",
                                (scope.tenant_id, scope.household_id)).fetchall()
        return {k: s for k, s in rows}

    def listing(self, scope: SpeakScope) -> list[dict[str, Any]]:
        with self._for(scope.tenant_id) as conn:
            rows = conn.execute("SELECT word, say FROM household_lexicon WHERE tenant_id=%s AND household_id=%s "
                                "ORDER BY word_key", (scope.tenant_id, scope.household_id)).fetchall()
        return [{"word": w, "say": s, "source": "foyer"} for w, s in rows]

    def put(self, scope: SpeakScope, word: str, say: str, by: str) -> None:
        key = entry_key(word)
        with self._for(scope.tenant_id) as conn:
            n = conn.execute("SELECT count(*) FROM household_lexicon WHERE tenant_id=%s AND household_id=%s "
                             "AND word_key<>%s", (scope.tenant_id, scope.household_id, key)).fetchone()[0]
            if n >= MAX_ENTRIES:
                raise LexiconError(409, "full", f"{MAX_ENTRIES} entrées au plus")
            conn.execute(
                "INSERT INTO household_lexicon (tenant_id, household_id, word_key, word, say, updated_by, updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (tenant_id, household_id, word_key) DO UPDATE SET "
                "word=EXCLUDED.word, say=EXCLUDED.say, updated_by=EXCLUDED.updated_by, updated_at=EXCLUDED.updated_at",
                (scope.tenant_id, scope.household_id, key, word, say, by, time.time()))

    def delete(self, scope: SpeakScope, word: str) -> bool:
        with self._for(scope.tenant_id) as conn:
            cur = conn.execute("DELETE FROM household_lexicon WHERE tenant_id=%s AND household_id=%s AND word_key=%s",
                               (scope.tenant_id, scope.household_id, entry_key(word)))
            return cur.rowcount == 1


# ───────────────────────────── service ─────────────────────────────

@dataclass
class SpeakLexicon:
    store: LexiconStore
    options: SpeakOptions = field(default_factory=SpeakOptions)
    default_scope: SpeakScope | None = None  # voix de la pièce (Lokkal : un seul foyer)

    def prepare(self, text: str, lang: str, scope: SpeakScope | None = None) -> str:
        """Ne lève jamais : en cas d'erreur (base indisponible…), le texte part tel quel."""
        scope = scope or self.default_scope
        try:
            entries = self.store.entries(scope) if scope is not None else {}
        except Exception:
            logger.warning("lexique du foyer illisible, texte sans lexique", exc_info=True)
            entries = {}
        try:
            return speak_prepare(text, lang, entries, self.options)
        except Exception:
            logger.exception("normalisation vocale en échec, texte tel quel")
            return text


_state: dict[str, SpeakLexicon | None] = {"s": None}


def configure(s: SpeakLexicon | None) -> None:
    _state["s"] = s


def service() -> SpeakLexicon | None:
    return _state["s"]


def build(raw: dict[str, Any] | None, dsn: str | None = None) -> SpeakLexicon | None:
    """Bloc `speak_lexicon` de la config. Éteint (défaut) : None. Clés : `enabled`, `eighty` (huitante |
    quatre-vingts), `acronyms` ({sigle: lecture, ou "" pour le lire comme un mot}), `entries` ({mot: prononciation},
    Lokkal : lexique fixé par la config, sans base)."""
    raw = raw or {}
    if not raw.get("enabled"):
        return None
    entries = raw.get("entries") or {}
    if dsn and not entries:
        store: LexiconStore = PgLexiconStore(dsn)
        default = None
    else:
        store = MemoryLexiconStore(entries)
        default = SpeakScope("", "local")
    return SpeakLexicon(store=store, options=SpeakOptions.from_raw(raw), default_scope=default)


def setup(raw: dict[str, Any] | None, dsn: str | None = None) -> SpeakLexicon | None:
    s = build(raw, dsn)
    configure(s)
    return s


# ───────────────────────────── routes (foyer) ─────────────────────────────

PATH = "/v1/speak/lexicon"


def is_path(path: str) -> bool:
    return _state["s"] is not None and path == PATH


def handle_member(h: Any, method: str, path: str, who: Any, body_of: Any, send_json: Any) -> bool:
    """`GET` (tout membre sauf enfant) / `PUT {word, say}` / `DELETE {word}` (adulte seulement) sur le lexique du foyer
    de `who` (Membership : household_id, household_tenant, account_id, profile). Aucune lecture hors de ce foyer."""
    s = _state["s"]
    if s is None or path != PATH:
        return False
    if who is None:
        send_json(h, 403, {"error": {"code": "household_required", "message": "réservé aux membres d'un foyer"}})
        return True
    scope = SpeakScope(who.household_tenant, who.household_id)
    try:
        if method == "GET":
            if who.profile == "child":
                raise LexiconError(403, "adult_required", "réservé aux adultes et aux ados")
            send_json(h, 200, {"entries": s.store.listing(scope)})
        elif method in ("PUT", "DELETE"):
            if who.profile != "adult":
                raise LexiconError(403, "adult_required", "seul un adulte du foyer modifie le lexique")
            body = body_of(h)
            if not isinstance(body, dict):
                raise LexiconError(400, "bad_json", "corps JSON attendu")
            if method == "PUT":
                word, say = check_entry(body.get("word"), body.get("say"))
                s.store.put(scope, word, say, who.account_id)
                send_json(h, 200, {"word": word, "say": say})
            else:
                word = body.get("word")
                if not isinstance(word, str) or not word.strip():
                    raise LexiconError(400, "bad_entry", "word attendu")
                if not s.store.delete(scope, word):
                    raise LexiconError(404, "not_found", "entrée inconnue")
                send_json(h, 200, {"deleted": word})
        else:
            raise LexiconError(405, "method_not_allowed", "GET, PUT ou DELETE")
    except LexiconError as e:
        send_json(h, e.status, {"error": {"code": e.code, "message": e.message}})
    return True


__all__ = ["DEFAULT_ACRONYMS", "LexiconError", "MemoryLexiconStore", "PgLexiconStore", "SpeakLexicon", "SpeakOptions",
           "SpeakScope", "apply_lexicon", "build", "configure", "entry_key", "handle_member", "is_path",
           "normalize_fr_ch", "number_to_words", "service", "setup", "speak_prepare"]
