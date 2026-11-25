"""« Éléments clés » d'un épisode : ce qu'un résumé extractif par phrases perd (lot compact-cache, point 4) — noms de
fichiers et chemins, identifiants, valeurs avec unité — et les RÉVISIONS de valeur (point 5) : « on passe de 1e-3 à 2e-4 »
fait de 2e-4 la valeur courante, présentée comme REMPLAÇANT 1e-3.

Pur Python, déterministe, sans modèle. Entrée : les textes d'un épisode dans l'ordre chronologique (messages de l'utilisateur,
réponses du modèle, arguments d'appels d'outils). Sortie : une ligne « Éléments clés : a ; b (remplace c) ; … » bornée en
caractères, dédupliquée, dans l'ordre chronologique ; les éléments de plus fort poids (fichier, valeur révisée) passent d'abord
quand la place manque. Les libellés de la ligne sont dans `core/model_texts.py` (4 langues)."""
from __future__ import annotations

import re
from dataclasses import dataclass

from nokker.approvals.rule_labels import current_lang
from nokker.core import model_texts as mt

_UNITS = (r"(?:%|°C|°|µs|μs|ms|s|min|h|jours?|days?|Hz|kHz|MHz|GHz|rad/s|rad|m/s|km/h|kg|g|mg|t|mm|cm|km|m|N|kN|Pa|kPa|MPa|GPa|bar|"
          r"V|mV|kV|A|mA|W|kW|MW|Wh|kWh|Nm|N\.m|J|kJ|K|dB|CHF|EUR|USD|€|\$|Ko|Mo|Go|KB|MB|GB|ko|mo|go|tours?|rpm)")
_NUM = r"[-+]?\d+(?:[.,]\d+)?(?:[eE][-+]?\d+)?"
_VALUE = re.compile(rf"(?<![\w.]){_NUM}(?:\s?{_UNITS}(?![\w]))?")
_SCI = re.compile(r"(?<![\w.])[-+]?\d+(?:[.,]\d+)?[eE][-+]?\d+")
_FILE = re.compile(r"(?<![\w/.-])(?:[\w.\-]+/)*[\w\-]+\.[A-Za-z][A-Za-z0-9]{0,4}(?![\w@])")
_PATH = re.compile(r"(?<![\w/])(?:~|\.{1,2})?/(?:[\w.\-]+/)+[\w.\-]+")
_ID = re.compile(r"\b(?:[A-Z]{1,6}[-_]?\d[\w\-/.]{2,}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|"
                 r"(?:ep|rs|dy|bu|th|ap)_[0-9a-f]{6,})\b")
# Pas des noms de fichiers : domaines, nombres décimaux, abréviations, versions.
_NOT_FILE_EXT = frozenset({"ch", "com", "org", "net", "fr", "de", "it", "io", "app", "dev", "ai", "eu", "co", "uk", "us"})
_STOP_BEFORE = frozenset("le la les un une des de du d l à au aux en et ou que qui pour sur avec sans dans par se est a "
                         "on je tu il nous vous ça ce c'est finalement alors donc puis ok oui non "
                         "the a an of to in on at is are was and or as by it this that we you".split())

# Révisions : (ancienne, nouvelle) — groupes nommés `old` / `new`. Valeur = jeton sans espace + unité éventuelle.
_V = r"[^\s,;:()«»\"“”]+(?:\s?" + _UNITS + r"(?![\w]))?"
_PAIRS = [re.compile(p, re.IGNORECASE) for p in (
    rf"\bpass(?:e|er|es|ez|ons|ent)\b[^.;!?]{{0,40}}?\bde\s+(?P<old>{_V})\s+à\s+(?P<new>{_V})",
    rf"\bchang(?:e|es|ed|ing)\b[^.;!?]{{0,40}}?\bfrom\s+(?P<old>{_V})\s+to\s+(?P<new>{_V})",
    rf"\bvon\s+(?P<old>{_V})\s+auf\s+(?P<new>{_V})",
    rf"\b(?:passa|passare|passiamo|cambia|cambiare|cambiamo)\b[^.;!?]{{0,30}}?\bda\s+(?P<old>{_V})\s+a\s+(?P<new>{_V})",
    rf"(?P<new>{_V})\s+(?:au lieu (?:de|du|d')|instead of|statt|anstatt|invece di|al posto di)\s*(?P<old>{_V})",
    rf"(?P<old>\S*\d\S*(?:\s?{_UNITS}(?![\w]))?)\s*(?:→|->|=>)\s*(?P<new>\S*\d\S*(?:\s?{_UNITS}(?![\w]))?)",
)]
_PAIR_STRIP = ".,;:)(«»\"“”'"


def _revisions(text: str) -> list[tuple[str, str, int]]:
    out: list[tuple[str, str, int]] = []
    for rx in _PAIRS:
        for m in rx.finditer(text or ""):
            old, new = (m.group("old").strip(_PAIR_STRIP), m.group("new").strip(_PAIR_STRIP))
            if old and new and old != new and any(c.isdigit() for c in old) and any(c.isdigit() for c in new):
                out.append((old, new, m.start()))
    return out


def revision_pairs(text: str) -> list[tuple[str, str]]:
    """Les (ancienne, nouvelle) valeurs d'un texte : « on passe de 1e-3 à 2e-4 » → [("1e-3", "2e-4")]. Une paire sans chiffre
    d'un côté n'est pas une valeur : écartée."""
    return [(o, n) for o, n, _ in _revisions(text)]


@dataclass
class Item:
    kind: str  # file | id | value
    text: str  # « mon_modele_v3.mbd », « HV-63/35-450 », « pas de temps 2e-4 s »
    key: str = ""  # grandeur (valeurs) : « pas de temps » ; vide = pas de remplacement possible
    replaces: str = ""  # ancienne valeur (« 1e-3 s »), quand une révision l'a remplacée
    order: int = 0
    weight: int = 1


def _label_before(text: str, start: int) -> str:
    """Les 1 à 3 mots pleins juste avant une valeur : la grandeur qu'elle qualifie (« pas de temps », « masse »)."""
    head = re.sub(r"[:=]\s*$", "", text[:start].rstrip()).split()
    words: list[str] = []
    for n, w in enumerate(reversed(head[-6:])):
        if n and w[-1:] in ",;.!?":  # une ponctuation ferme la proposition : la grandeur est dans la même
            break
        wl = w.strip(".,;:()«»\"'").lower()
        if not wl or not any(c.isalpha() for c in wl):
            break
        if wl in _STOP_BEFORE and words and len(words) >= 1 and wl not in ("de", "du", "d", "of"):
            break
        words.append(wl)
        if len(words) >= 3:
            break
    out = list(reversed(words))
    while out and out[0] in _STOP_BEFORE:
        out.pop(0)
    return " ".join(out)


def extract(texts: list[str]) -> list[Item]:
    """Éléments clés des textes, dans l'ordre chronologique, dédupliqués ; une valeur plus récente de la MÊME grandeur
    remplace la précédente (`replaces`)."""
    items: list[Item] = []
    seen: dict[tuple[str, str], Item] = {}
    by_key: dict[str, Item] = {}
    order = 0

    def add(it: Item) -> None:
        nonlocal order
        k = (it.kind, it.text.lower())
        if k in seen:  # déjà vu : on ne le répète pas (le plus ancien garde sa place)
            return
        it.order = order = order + 1
        seen[k] = it
        if it.kind == "value" and it.key:
            prev = by_key.get(it.key)
            if prev is not None and prev.text != it.text:  # même grandeur, autre valeur : la dernière REMPLACE
                it.replaces = prev.text.removeprefix(prev.key).strip() or prev.text
                it.weight = 4
                items.remove(prev)
            by_key[it.key] = it
        items.append(it)

    for text in texts:
        text = text or ""
        for m in _FILE.finditer(text):
            tok = m.group(0)
            ext = tok.rsplit(".", 1)[-1].lower()
            if ext in _NOT_FILE_EXT or tok.replace(".", "").isdigit() or len(tok) < 4 or not any(c.isalpha() for c in ext):
                continue
            add(Item("file", tok, weight=3))
        for m in _PATH.finditer(text):
            add(Item("file", m.group(0), weight=3))
        for m in _ID.finditer(text):
            add(Item("id", m.group(0), weight=2))
        covered: set[str] = set()
        for old, new, start in _revisions(text):  # « le pas de temps passe de 1e-3 à 2e-4 » : la grandeur précède le verbe
            label = _label_before(text, start)
            prior = next((i for i in reversed(items) if i.kind == "value" and i.text.endswith(old) and i.key), None)
            if prior is not None:  # la valeur d'avant a déjà été vue avec sa grandeur : c'est la même
                label = prior.key
            elif not label:  # « on passe de 1e-3 à 2e-4 pour le pas de temps » : la grandeur suit la paire
                tail = re.search(re.escape(new) + r"\s+(?:pour|de|du|des|of|for|für|per)\s+(?:le |la |les |l'|the |den |die |das |il |lo )?"
                                 r"([\w\-]+(?:\s(?:de|du|d')?\s?[\w\-]+){0,2})", text)
                label = tail.group(1).strip().lower() if tail else ""
            add(Item("value", f"{label} {old}".strip(), key=label, weight=3))
            add(Item("value", f"{label} {new}".strip(), key=label, weight=4))
            covered.update((old, new))
        for m in _VALUE.finditer(text):
            tok = m.group(0).strip()
            if tok in covered or not (_SCI.fullmatch(tok) or re.search(_UNITS + r"$", tok)):
                continue
            label = _label_before(text, m.start())
            add(Item("value", f"{label} {tok}".strip(), key=label, weight=2 if label else 1))
    return items


def key_line(texts: list[str], max_chars: int = 300, lang: str | None = None) -> str:
    """« Éléments clés : mon_modele_v3.mbd ; pas de temps 2e-4 s (remplace 1e-3 s) ; … », ≤ `max_chars` (étiquette comprise),
    chronologique ; vide s'il n'y a rien. Quand la place manque, les poids forts (fichiers, valeurs révisées) d'abord, puis les
    plus récents."""
    label = mt.pick(mt.KEY_ITEMS_LABEL, lang or current_lang())
    repl = mt.pick(mt.KEY_ITEM_REPLACES, lang or current_lang())
    items = extract(texts)
    if not items:
        return ""

    def render(it: Item) -> str:
        return it.text + (" (" + repl.format(old=it.replaces) + ")" if it.replaces else "")

    room = max_chars - len(label) - 1
    chosen: list[Item] = []
    used = 0
    for it in sorted(items, key=lambda i: (-i.weight, -i.order)):
        n = len(render(it)) + 3
        if used + n > room:
            continue
        chosen.append(it)
        used += n
    if not chosen:
        return ""
    chosen.sort(key=lambda i: i.order)
    return label + " " + " ; ".join(render(i) for i in chosen) + "."


__all__ = ["Item", "extract", "key_line", "revision_pairs"]
