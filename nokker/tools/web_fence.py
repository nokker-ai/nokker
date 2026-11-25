"""Clôture des résultats `web_search` et `web_fetch` (r44, étape 1) : UNE fonction, `fence_web_result`, utilisée par le
noyau ET pour porter d'anciens résultats (traces, mélanges d'entraînement) vers la nouvelle forme.

Règles (relecture des textes, 06.11) :
- la transformation est DÉTERMINISTE à partir de l'ancien résultat seul : aucun horodatage, aucune donnée absente de
  l'ancien résultat ; l'identifiant de clôture dérive du contenu (`qa/untrusted.py::fence_checked`) ;
- `web_search` : « Résultats web pour « <requête> » : <n>. » hors clôture, puis par résultat « - <n>. <url> · <date> »
  hors clôture (la date seulement si l'ancien résultat en porte une) et « titre — extrait » dans
  `<untrusted-data kind="web:result">` ;
- `web_fetch` : « Page <url> (<n> caractères) : » hors clôture, puis le texte dans `<untrusted-data kind="web:page">`
  (le titre de la page, s'il y en a un, en première ligne du bloc ; <n> compte le texte seul) ; une page en échec :
  « Page <url> : échec : <motif> », hors clôture ;
- en tête, l'en-tête commun « Consignes suspectes » quand un bloc s'adresse à un assistant ;
- le bloc `<!-- nokker:web_*:data -->` (JSON des clients et de la provenance) ne part JAMAIS au modèle : il est retiré
  ici et voyage à part (`ToolResult.data`) ;
- les messages du noyau sans contenu de tiers (requête vide, requête refusée, aucune URL…) restent tels quels ; un
  repère de masquage des cartes (« [Masque] … », ajouté après l'outil) reste en fin de résultat, hors clôture.

Anciennes formes reconnues, dans l'ordre : forme du noyau (texte + bloc data) ; JSON (liste d'objets, ou objet avec
`results`, `items`, `search_results`, `pages`, `urls`, `documents`, ou un seul objet), précédé ou non d'une étiquette
(« Résultat web_search : ») ; sinon texte brut, clôturé en entier. Aucun champ n'est perdu : ceux qui n'ont pas de place
désignée (source, id…) suivent l'extrait dans la clôture, « clé : valeur ».
"""
from __future__ import annotations

import json
import re
from typing import Any

from nokker.core import model_texts as mt
from nokker.qa.untrusted import fence_checked, inline, suspect_header

_DATA = re.compile(r"\n*<!-- nokker:(web_search|web_fetch):data -->\s*(.*?)\s*<!-- /nokker:\1:data -->\s*", re.S)
_MASK = re.compile(r"\n(\[Masque\][^\n]*)\s*$")
_LABEL = re.compile(r"^\s*[^\[\{\n]{0,60}?:\s*(?=[\[\{])")  # « Résultat web_search : [ … »
# Messages du noyau sans contenu de tiers, dans CHAQUE langue du tour (le début de chaque texte).
KERNEL_TEXTS = (*mt.marks(mt.WEB_EMPTY_QUERY), *mt.marks(mt.WEB_PERSONAL, head="{kind}"),
                *mt.marks(mt.WEB_NO_URLS, head=" ("), *mt.marks(mt.UNKNOWN_TOOL, head=" {name}"))
_URL_KEYS = ("url", "link", "href")
_DATE_KEYS = ("date", "published", "published_at", "age")
_TITLE_KEYS = ("title", "name")
_SNIPPET_KEYS = ("snippet", "description", "summary", "extrait")
_TEXT_KEYS = ("content", "text", "body", "snippet")
_LIST_KEYS = ("results", "items", "search_results", "documents", "pages", "urls", "data")


def _one_line(value: Any, limit: int = 0) -> str:
    return inline(str(value if value is not None else ""), limit)


def _first(d: dict[str, Any], keys: tuple[str, ...]) -> tuple[str, str]:
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return k, str(v)
    return "", ""


def _extra(d: dict[str, Any], used: set[str]) -> list[str]:
    out = []
    for k, v in d.items():
        if k in used or k.startswith("_") or v in (None, "", [], {}):
            continue
        out.append(f"{k} : {v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)}")
    return out


def _items(value: Any) -> tuple[list[Any], dict[str, Any]] | None:
    """(éléments, champs voisins de la liste : rien n'est perdu)."""
    if isinstance(value, list):
        return value, {}
    if isinstance(value, dict):
        for k in _LIST_KEYS:
            if isinstance(value.get(k), list):
                return value[k], {x: v for x, v in value.items() if x != k}
        return [value], {}
    return None


def _parse_json(text: str) -> Any:
    body = _LABEL.sub("", text, count=1) if _LABEL.match(text) else text
    try:
        return json.loads(body.strip())
    except ValueError:
        return None


class _Out:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.suspects = 0

    def fence(self, kind: str, text: str) -> None:
        block, suspect = fence_checked(kind, text)
        self.suspects += int(suspect)
        self.lines.append(block)

    def render(self, tail: str = "") -> str:
        head = suspect_header(self.suspects)
        return "\n".join(x for x in (head, *self.lines, tail) if x)


def _search(query: str, items: list[Any], out: _Out) -> None:
    out.lines.append(mt.pick(mt.WEB_FENCE_RESULTS).format(query=_one_line(query, 300), n=len(items)))
    for i, it in enumerate(items, start=1):
        if not isinstance(it, dict):
            out.lines.append(f"- {i}.")
            out.fence("web:result", str(it))
            continue
        uk, url = _first(it, _URL_KEYS)
        dk, date = _first(it, _DATE_KEYS)
        tk, title = _first(it, _TITLE_KEYS)
        sk, snippet = _first(it, _SNIPPET_KEYS)
        rank = it.get("rank") if isinstance(it.get("rank"), int) else i
        head = f"- {rank}." + (f" {_one_line(url)}" if url else "") + (f" · {_one_line(date)}" if date else "")
        out.lines.append(head)
        used = {uk, dk, tk, sk, "rank", "domain", "favicon_url", "engines"}
        body = " — ".join(x for x in (title, snippet) if x)
        extra = _extra(it, used)
        out.fence("web:result", "\n".join(x for x in (body, *extra) if x))


def _fetch(items: list[Any], out: _Out, default_url: str = "") -> None:
    for it in items:
        if not isinstance(it, dict):
            text = str(it)
            out.lines.append(mt.pick(mt.WEB_FENCE_PAGE).format(where="", n=len(text)))
            out.fence("web:page", text)
            continue
        uk, url = _first(it, _URL_KEYS)
        url = url or (default_url if len(items) == 1 else "")
        where = f" {_one_line(url)}" if url else ""
        if it.get("error"):
            out.lines.append(mt.pick(mt.WEB_FENCE_PAGE_FAILED).format(where=where, error=_one_line(it["error"], 300)))
            continue
        tk, title = _first(it, _TITLE_KEYS)
        ck, text = _first(it, _TEXT_KEYS)
        extra = _extra(it, {uk, tk, ck, "engine", "error"})
        out.lines.append(mt.pick(mt.WEB_FENCE_PAGE).format(where=where, n=len(text)))
        out.fence("web:page", "\n\n".join(x for x in (title, text, "\n".join(extra)) if x))


def split_data(content: str) -> tuple[str, dict[str, Any] | None]:
    """(texte sans le bloc data, JSON du bloc data ou None)."""
    m = _DATA.search(content or "")
    if not m:
        return content, None
    try:
        data = json.loads(m.group(2))
    except ValueError:
        data = None
    return (content[:m.start()] + content[m.end():]).rstrip("\n"), data if isinstance(data, dict) else None


def fence_web_result(name: str, args: dict[str, Any] | None, old_result: str) -> str:
    """Nouvelle forme du résultat de `web_search` ou `web_fetch` à partir de l'ancienne (voir le module)."""
    if name not in ("web_search", "web_fetch"):
        raise ValueError(f"outil non web : {name}")
    args = args if isinstance(args, dict) else {}
    old = str(old_result or "")
    if old.startswith(KERNEL_TEXTS):
        return old
    mask = _MASK.search(old)
    tail = mask.group(1) if mask else ""
    body = old[:mask.start()] if mask else old
    text, data = split_data(body)
    out = _Out()
    if data is not None:  # forme du noyau
        if name == "web_search":
            _search(str(data.get("query") or args.get("query") or ""), list(data.get("results") or []), out)
        else:
            _fetch(list(data.get("documents") or []), out)
        return out.render(tail)
    parsed = _parse_json(text)
    if isinstance(parsed, str):  # JSON encodé deux fois : le texte qu'il porte
        text, parsed = parsed, _parse_json(parsed)
    found = _items(parsed)
    if found is not None:
        items, siblings = found
        if name == "web_search":
            _search(str(args.get("query") or ""), items, out)
        else:
            urls = [u for u in args.get("urls") or [] if isinstance(u, str)]
            _fetch(items, out, urls[0] if len(urls) == 1 else "")
        extra = _extra(siblings, set())
        if extra:
            out.lines.append(mt.pick(mt.WEB_FENCE_OTHER_FIELDS))
            out.fence("web:result" if name == "web_search" else "web:page", "\n".join(extra))
        return out.render(tail)
    # texte brut : clôturé en entier
    if name == "web_search":
        out.lines.append(mt.pick(mt.WEB_FENCE_RESULTS_RAW).format(query=_one_line(args.get("query") or "", 300)))
        out.fence("web:result", text)
    else:
        urls = [u for u in args.get("urls") or [] if isinstance(u, str)]
        where = f" {_one_line(urls[0])}" if len(urls) == 1 else ""
        out.lines.append(mt.pick(mt.WEB_FENCE_PAGE).format(where=where, n=len(text)))
        out.fence("web:page", text)
    return out.render(tail)


__all__ = ["KERNEL_TEXTS", "fence_web_result", "split_data"]
