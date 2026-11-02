"""Rendu d'une page d'écran (docs/SCREEN-CAST.md §4) : arguments de `show_on_screen` → HTML autonome.

Tout texte passe par `scrub` (caviardage du noyau) avant d'être échappé ; une poignée `[secret:…]` s'affiche
« [masqué] » ; un élément `private: true` est remplacé par « contenu privé masqué ». Le SVG est relu élément par
élément (liste blanche, aucun script, aucune ressource externe). Les images sont mises en ligne (`data:`) : la page ne
charge rien d'autre que son propre flux d'événements, sauf la carte (tuiles OpenStreetMap, dite dans la doc)."""
from __future__ import annotations

import base64
import html
import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from xml.etree import ElementTree

from nokker.cast import texts as T

MAX_ROWS, MAX_COLS, MAX_SLIDES, MAX_MARKERS = 200, 20, 40, 50
MAX_CELL, MAX_TEXT, MAX_WEB_TEXT = 300, 2000, 12000
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_PAGE_BYTES = 12 * 1024 * 1024
KINDS = ("diagram", "table", "image", "map", "slides", "web")
OSM = "https://www.openstreetmap.org"
_HANDLE = re.compile(r"\[secret:[^\]\s]{1,80}\]")
PRIVATE_HTML = '<span class="priv">contenu privé masqué</span>'

_SVG_NS = "http://www.w3.org/2000/svg"
_SVG_OK = frozenset({"svg", "g", "path", "rect", "circle", "ellipse", "line", "polyline", "polygon", "text", "tspan",
                     "title", "desc", "defs", "linearGradient", "radialGradient", "stop", "marker", "clipPath",
                     "mask", "pattern", "symbol"})
_ATTR_BAD = re.compile(r"(?i)javascript:|data:|url\(\s*['\"]?(?!#)|@import|expression\(")


class RenderError(ValueError):
    """Refus du rendu : le message est un motif de texts.py (vu par le modèle après « REFUSÉ : »)."""


@dataclass
class Rendered:
    kind: str
    title: str
    body: str
    masked: int = 0
    interval_s: int = 0
    frames: tuple[str, ...] = ()  # origines d'iframe permises (CSP)
    media: tuple[bytes, str] | None = None  # image brute (écrans « média seulement »)
    summary: dict[str, Any] = field(default_factory=dict)


class _Ctx:
    def __init__(self, scrub: Callable[[str], str]) -> None:
        self.scrub, self.masked = scrub, 0

    def text(self, value: Any, limit: int = MAX_TEXT) -> str:
        raw = str(value if value is not None else "")[:limit * 2]
        clean = self.scrub(raw)
        clean, n = _HANDLE.subn("[masqué]", clean)
        self.masked += n
        return html.escape(clean[:limit])

    def private(self, item: Any) -> bool:
        if isinstance(item, dict) and item.get("private") is True:
            self.masked += 1
            return True
        return False


def _need(args: dict[str, Any], name: str) -> Any:
    v = args.get(name)
    if v in (None, "", []):
        raise RenderError(T.WHY_MISSING.format(name=name))
    return v


def _sniff(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def image_data(data: bytes, path: str) -> tuple[bytes, str]:
    if len(data) > MAX_IMAGE_BYTES:
        raise RenderError(T.WHY_IMAGE_SIZE.format(path=path, mb=MAX_IMAGE_BYTES // (1024 * 1024)))
    mime = _sniff(data)
    if mime is None:
        raise RenderError(T.WHY_IMAGE_FORMAT.format(path=path))
    return data, mime


def _data_uri(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def sanitize_svg(source: str, c: _Ctx) -> str:
    """SVG relu par liste blanche ; sérialisé à la main (préfixes sans espace de noms, rien d'inconnu)."""
    if re.search(r"(?i)<!(DOCTYPE|ENTITY)", source):
        raise RenderError(T.WHY_SVG.format(reason=T.WHY_SVG_DOCTYPE))
    try:
        root = ElementTree.fromstring(source.strip())
    except ElementTree.ParseError:
        raise RenderError(T.WHY_SVG.format(reason=T.WHY_SVG_UNREADABLE)) from None

    def local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1] if tag.startswith("{") else tag

    if local(root.tag) != "svg":
        raise RenderError(T.WHY_SVG.format(reason=T.WHY_SVG_ROOT))

    def out(el: ElementTree.Element) -> str:
        tag = local(el.tag)
        if tag not in _SVG_OK or (el.tag.startswith("{") and not el.tag.startswith("{" + _SVG_NS)):
            return ""  # élément interdit (script, foreignObject, image, use, style…) : retiré avec son contenu
        attrs = []
        for k, v in el.attrib.items():
            name = local(k)
            if name.lower().startswith("on") or name in ("href", "src") or _ATTR_BAD.search(v):
                continue
            if not re.fullmatch(r"[A-Za-z_:][A-Za-z0-9_.:-]*", name):
                continue
            attrs.append(f' {name}="{c.text(v, 2000)}"')
        if tag == "svg":
            attrs.append(f' xmlns="{_SVG_NS}"')
        inner = c.text(el.text, MAX_TEXT) if el.text and el.text.strip() else ""
        for child in el:
            inner += out(child)
            if child.tail and child.tail.strip():
                inner += c.text(child.tail, MAX_TEXT)
        return f"<{tag}{''.join(attrs)}>{inner}</{tag}>"

    return out(root)


def _table(args: dict[str, Any], c: _Ctx) -> str:
    rows = args.get("rows")
    if not isinstance(rows, list) or not rows:
        raise RenderError(T.WHY_MISSING.format(name="rows"))
    if len(rows) > MAX_ROWS:
        raise RenderError(T.WHY_TOO_MANY.format(what="lignes", n=MAX_ROWS))
    cols = args.get("columns") if isinstance(args.get("columns"), list) else []
    if len(cols) > MAX_COLS:
        raise RenderError(T.WHY_TOO_MANY.format(what="colonnes", n=MAX_COLS))
    head = "".join(f"<th>{c.text(h, MAX_CELL)}</th>" for h in cols)
    width = max([len(cols)] + [len(r.get("cells") or []) if isinstance(r, dict) else
                               len(r) if isinstance(r, list) else 1 for r in rows])
    width = min(width, MAX_COLS)
    body = []
    for r in rows:
        if c.private(r):
            body.append(f'<tr><td colspan="{width}">{PRIVATE_HTML}</td></tr>')
            continue
        cells = (r.get("cells") or []) if isinstance(r, dict) else r if isinstance(r, list) else [r]
        body.append("<tr>" + "".join(f"<td>{PRIVATE_HTML if c.private(x) else c.text(x, MAX_CELL)}</td>"
                                     for x in cells[:MAX_COLS]) + "</tr>")
    return (f'<table>{"<thead><tr>" + head + "</tr></thead>" if head else ""}<tbody>{"".join(body)}</tbody>'
            "</table>")


def _coord(v: Any, lo: float, hi: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise RenderError(T.WHY_COORDS) from None
    if not (lo <= f <= hi) or math.isnan(f):
        raise RenderError(T.WHY_COORDS)
    return f


def _map(args: dict[str, Any], c: _Ctx) -> tuple[str, tuple[str, ...]]:
    lat = _coord(_need(args, "lat"), -90, 90)
    lon = _coord(_need(args, "lon"), -180, 180)
    try:
        zoom = max(1, min(19, int(args.get("zoom") or 13)))
    except (TypeError, ValueError):
        zoom = 13
    span = 360 / (2 ** zoom)
    bbox = f"{lon - span:.5f},{lat - span / 2:.5f},{lon + span:.5f},{lat + span / 2:.5f}"
    markers = args.get("markers") if isinstance(args.get("markers"), list) else []
    if len(markers) > MAX_MARKERS:
        raise RenderError(T.WHY_TOO_MANY.format(what="points", n=MAX_MARKERS))
    shown, items = None, []
    for m in markers:
        if c.private(m):
            items.append(f"<li>{PRIVATE_HTML}</li>")
            continue
        if not isinstance(m, dict):
            continue
        mlat, mlon = _coord(m.get("lat"), -90, 90), _coord(m.get("lon"), -180, 180)
        shown = shown or (mlat, mlon)
        items.append(f"<li>{c.text(m.get('label') or f'{mlat:.4f}, {mlon:.4f}', 120)}</li>")
    marker = f"&amp;marker={shown[0]:.5f}%2C{shown[1]:.5f}" if shown else ""
    frame = (f'<iframe class="map" src="{OSM}/export/embed.html?bbox={bbox}&amp;layer=mapnik{marker}" '
             'referrerpolicy="no-referrer" title="carte"></iframe>')
    legend = f'<ul class="legend">{"".join(items)}</ul>' if items else ""
    return frame + legend + '<p class="src">© contributeurs OpenStreetMap</p>', (OSM,)


def render(args: dict[str, Any], *, scrub: Callable[[str], str],
           read_image: Callable[[str], bytes] | None = None,
           fetch_web: Callable[[str], tuple[str, str]] | None = None) -> Rendered:
    """`read_image(path)` rend les octets d'un fichier du dossier de travail (ou lève RenderError) ; `fetch_web(url)`
    rend (titre, texte extrait)."""
    kind = str(args.get("kind") or "")
    if kind not in KINDS:
        raise RenderError(T.WHY_KIND.format(kind=kind[:40]))
    c = _Ctx(scrub)
    title = c.text(args.get("title") or "", 120)
    frames: tuple[str, ...] = ()
    media = None
    interval = 0

    def load(path: Any) -> tuple[bytes, str]:
        if read_image is None:
            raise RenderError(T.WHY_NO_ROOT)
        p = str(path or "")
        return image_data(read_image(p), p)

    if kind == "diagram":
        body = f'<figure class="svg">{sanitize_svg(str(_need(args, "svg")), c)}</figure>'
    elif kind == "table":
        body = _table(args, c)
    elif kind == "image":
        data, mime = load(_need(args, "path"))
        media = (data, mime)
        body = f'<img class="full" alt="{title}" src="{_data_uri(data, mime)}">'
    elif kind == "map":
        body, frames = _map(args, c)
    elif kind == "slides":
        slides = _need(args, "slides")
        if not isinstance(slides, list):
            raise RenderError(T.WHY_MISSING.format(name="slides"))
        if len(slides) > MAX_SLIDES:
            raise RenderError(T.WHY_TOO_MANY.format(what="diapositives", n=MAX_SLIDES))
        try:
            interval = max(3, min(120, int(args.get("interval_s") or 10)))
        except (TypeError, ValueError):
            interval = 10
        parts = []
        for s in slides:
            if c.private(s):
                parts.append(f'<section class="slide"><p>{PRIVATE_HTML}</p></section>')
                continue
            s = s if isinstance(s, dict) else {"text": s}
            img = ""
            if s.get("image"):
                data, mime = load(s["image"])
                img = f'<img alt="" src="{_data_uri(data, mime)}">'
            parts.append(f'<section class="slide"><h2>{c.text(s.get("title") or "", 120)}</h2>{img}'
                         f'<p>{c.text(s.get("text") or "")}</p></section>')
        body = "".join(parts)
    else:  # web
        url = str(_need(args, "url"))
        if fetch_web is None:
            raise RenderError(T.WHY_WEB.format(url=url[:200]))
        wtitle, text = fetch_web(url)
        if not text.strip():
            raise RenderError(T.WHY_WEB_EMPTY.format(url=url[:200]))
        from urllib.parse import urlparse

        title = title or c.text(wtitle, 120)
        paras = "".join(f"<p>{c.text(p, MAX_WEB_TEXT)}</p>" for p in text[:MAX_WEB_TEXT].split("\n") if p.strip())
        body = f'<article class="reader">{paras}<p class="src">{c.text(urlparse(url).hostname or "", 120)}</p></article>'
    if title:
        body = f"<h1>{title}</h1>{body}"
    if len(body) > MAX_PAGE_BYTES:
        raise RenderError(T.WHY_PAGE_SIZE.format(mb=MAX_PAGE_BYTES // (1024 * 1024)))
    return Rendered(kind=kind, title=html.unescape(title), body=body, masked=c.masked, interval_s=interval,
                    frames=frames, media=media, summary={"kind": kind, "title": html.unescape(title)})


_CSS = """
:root{color-scheme:dark}*{box-sizing:border-box}
body{margin:0;background:#0f1115;color:#eef0f4;font:clamp(18px,2.2vw,34px)/1.45 system-ui,sans-serif}
main{padding:4vh 5vw;min-height:100vh}h1{font-size:1.6em;margin:0 0 .6em}h2{font-size:1.25em;margin:0 0 .4em}
table{border-collapse:collapse;width:100%}th,td{border-bottom:1px solid #2c313a;padding:.35em .6em;text-align:left}
th{color:#9fb3ff}figure.svg{margin:0}figure.svg svg{width:100%;max-height:82vh}
img.full{display:block;max-width:100%;max-height:86vh;margin:auto}.slide img{max-width:100%;max-height:60vh}
iframe.map{width:100%;height:72vh;border:0}.legend{columns:2}.src{color:#8a93a3;font-size:.7em}
.priv{color:#8a93a3;font-style:italic}.reader{max-width:60em}.end{color:#8a93a3}
"""

_JS = """
(function(){
function slides(){var s=[].slice.call(document.querySelectorAll('.slide'));if(window.__nk)clearInterval(window.__nk);
if(s.length<2)return;var i=0,ms=(+document.getElementById('root').dataset.interval||10)*1000;
s.forEach(function(e,k){e.hidden=k!==0});window.__nk=setInterval(function(){s[i].hidden=true;i=(i+1)%s.length;
s[i].hidden=false},ms);}
document.addEventListener('DOMContentLoaded',function(){slides();if(!document.body.dataset.live)return;
var es=new EventSource(location.pathname.replace(/\\/$/,'')+'/events');
es.addEventListener('update',function(e){var d=JSON.parse(e.data);document.title=d.title;
var r=document.getElementById('root');r.innerHTML=d.body;r.dataset.interval=d.interval||'';slides();});
es.addEventListener('end',function(){es.close();document.getElementById('root').innerHTML=
'<p class="end">Affichage terminé.</p>';});});
})();
"""


def page(r: Rendered, *, live: bool, nonce: str = "") -> str:
    """Page complète. `live` : écran (flux d'événements) ; sinon bulle (iframe srcdoc côté web, sans flux)."""
    n = f' nonce="{nonce}"' if nonce else ""
    return (f'<!doctype html><html lang="fr"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<meta name="referrer" content="no-referrer"><title>{html.escape(r.title)}</title>'
            f"<style{n}>{_CSS}</style><script{n}>{_JS}</script></head>"
            f"<body{' data-live=1' if live else ''}>"
            f'<main id="root" data-interval="{r.interval_s or ""}">'
            f"{r.body}</main></body></html>")


def update_event(r: Rendered) -> str:
    return json.dumps({"title": r.title, "body": r.body, "interval": r.interval_s or ""}, ensure_ascii=False)


def csp(nonce: str) -> str:
    """Politique de la page d'écran : rien d'autre que ses images en ligne, son script (nonce), son flux et la carte.
    Fixe pour la vie du lien (une mise à jour peut passer d'un tableau à une carte)."""
    return (f"default-src 'none'; img-src data:; style-src 'unsafe-inline'; script-src 'nonce-{nonce}'; "
            f"connect-src 'self'; frame-src {OSM}; base-uri 'none'; form-action 'none'")


__all__ = ["KINDS", "MAX_IMAGE_BYTES", "RenderError", "Rendered", "csp", "image_data", "page", "render",
           "sanitize_svg", "update_event"]
