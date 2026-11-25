"""Rendu d'un écran du catalogue en HTML statique (lot V7, docs/SCREENS.md §3).

Le noyau seul écrit le HTML : chaque texte est échappé, aucune adresse externe, aucun script. La page porte sa propre
CSP (`default-src 'none'` ; le seul style permis est le bloc `<style>` de la page, par son empreinte) et se lit à 2 m :
grands caractères, contraste AA, jetons de la marque à `:root`. Elle passe le plancher de design (`nokker.design.floor`)
sans écart, ce que vérifient les tests. Le mur l'affiche dans une iframe `sandbox` sans `allow-scripts`.
"""
from __future__ import annotations

import base64
import hashlib
import html
from datetime import datetime, timedelta
from typing import Any

MAX_HTML = 64 * 1024
_DAYS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
_MONTHS = ("janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre",
           "décembre")

# Jetons de la marque (kit v1) : encre et papier, corail et braise, gris chaud. Polices de la marque (Cinzel pour les
# titres, IBM Plex Sans pour le corps ; SIL OFL, nokker/screens/fonts) servies par le noyau en même origine.
FONT_DIR = "/v1/screens/fonts"
FONTS = ("cinzel-latin-600-normal", "cinzel-latin-ext-600-normal", "ibm-plex-sans-latin-400-normal",
         "ibm-plex-sans-latin-ext-400-normal", "ibm-plex-sans-latin-600-normal", "ibm-plex-sans-latin-ext-600-normal")
_LATIN = "U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+2000-206F,U+20AC,U+2122,U+2212"
_LATIN_EXT = "U+0100-02BA,U+02BD-02C5,U+02C7-02CC,U+02CE-02D7,U+02DD-02FF,U+1E00-1E9F,U+20A0-20C0,U+A720-A7FF"


def _face(family: str, file: str, weight: int, rng: str) -> str:
    return (f'@font-face{{font-family:"{family}";font-style:normal;font-weight:{weight};font-display:swap;'
            f'src:url({FONT_DIR}/{file}.woff2) format("woff2");unicode-range:{rng}}}')


_FACES = "".join([_face("Cinzel", "cinzel-latin-600-normal", 600, _LATIN),
                  _face("Cinzel", "cinzel-latin-ext-600-normal", 600, _LATIN_EXT),
                  _face("IBM Plex Sans", "ibm-plex-sans-latin-400-normal", 400, _LATIN),
                  _face("IBM Plex Sans", "ibm-plex-sans-latin-ext-400-normal", 400, _LATIN_EXT),
                  _face("IBM Plex Sans", "ibm-plex-sans-latin-600-normal", 600, _LATIN),
                  _face("IBM Plex Sans", "ibm-plex-sans-latin-ext-600-normal", 600, _LATIN_EXT)])
CSS = _FACES + """
:root{--bg:#0A0D14;--card:rgba(20,24,34,.78);--text:#EEF1F6;--muted:#AEB6C4;--accent:#FF7A6D;--ember:#E2474F;
--line:rgba(255,122,109,.22);--done:#8E97A8;--glow1:rgba(255,111,97,.20);--glow2:rgba(226,71,79,.14);
--grain:rgba(255,255,255,.025);--brand:"Cinzel","Times New Roman",serif;
--font:"IBM Plex Sans","Segoe UI",system-ui,sans-serif}
[data-theme=light]{--bg:#F4F2EE;--card:rgba(255,255,255,.82);--text:#1C1F24;--muted:#4A5260;--accent:#B52A33;
--ember:#C8313A;--line:rgba(200,49,58,.20);--done:#5B6472;--glow1:rgba(226,71,79,.13);--glow2:rgba(255,166,120,.16);
--grain:rgba(0,0,0,.022)}
*{box-sizing:border-box}
html,body{overflow-x:clip;margin:0}
body{min-height:100vh;color:var(--text);font-family:var(--font);font-size:clamp(20px,2.45vmax,46px);line-height:1.35;
background:radial-gradient(60vw 70vh at 88% 0%,var(--glow1),transparent 70%),
radial-gradient(55vw 60vh at 0% 100%,var(--glow2),transparent 72%),
repeating-linear-gradient(0deg,var(--grain) 0 1px,transparent 1px 3px),var(--bg)}
main{min-height:100vh;max-width:2400px;margin:0 auto;padding:5vh 4vw;display:grid;gap:3vw;align-content:start}
main.hero{grid-template-columns:minmax(0,.9fr) minmax(0,1.7fr)}
h1{font-family:var(--brand);font-weight:600;font-size:1.45em;line-height:1.1;margin:0 0 .5em;overflow-wrap:break-word;min-width:0}
h2{font-family:var(--brand);font-weight:600;font-size:1.1em;margin:0 0 .4em;overflow-wrap:break-word;min-width:0}
h3{font-weight:600;font-size:.82em;color:var(--accent);margin:0 0 .45em}
p{margin:0 0 .5em;max-width:40em}
.side{display:flex;flex-direction:column;gap:1.2em;min-width:0}
.clock .t{font-family:var(--brand);font-weight:600;font-size:min(4em,11vw);line-height:1;white-space:nowrap}
.clock .d{color:var(--muted);margin-top:.25em}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,12em),1fr));gap:1.1em;align-content:start;align-items:start}
.card{background:var(--card);border:1px solid var(--line);border-radius:.75em;padding:.85em 1em;min-width:0}
.ev{border-left:.22em solid var(--ember)}
.ev .w{color:var(--muted);margin:0}
.ev .what{font-weight:600;margin:0 0 .15em}
ul{margin:0;padding:0;list-style:none}
li{padding:.28em 0;border-bottom:1px solid var(--line);display:flex;gap:.55em;align-items:baseline}
li:last-child{border-bottom:0}
li .dot{flex:none;width:.42em;height:.42em;border-radius:50%;background:var(--accent);transform:translateY(-.12em)}
li .box{flex:none;width:.85em;height:.85em;border:2px solid var(--accent);border-radius:.22em;transform:translateY(.08em)}
li.done{color:var(--done);text-decoration:line-through}
li.done .box{background:var(--accent)}
.stat .v{font-family:var(--brand);font-weight:600;font-size:2.1em;color:var(--accent);line-height:1.05}
.stat .l{color:var(--muted)}
.note{border-left:.22em solid var(--accent)}
.card p.note{padding-left:.6em;margin:.45em 0 0}
.note.warn{border-left-color:var(--ember);font-weight:600}
hr{border:0;border-top:1px solid var(--line);margin:.6em 0}
@media (max-width:820px),(orientation:portrait){main.hero{grid-template-columns:minmax(0,1fr)}}"""
_CSS_SHA = base64.b64encode(hashlib.sha256(CSS.encode()).digest()).decode()
CSP = f"default-src 'none'; style-src 'sha256-{_CSS_SHA}'; font-src 'self'; base-uri 'none'; form-action 'none'"
# En-tête du cadre servi par le noyau : la même politique, plus `sandbox` sans aucun jeton (ni script, ni formulaire,
# ni même origine). Pas de frame-ancestors : le mur natif charge le web depuis une autre origine que le noyau.
FRAME_HEADERS = {"Content-Type": "text/html; charset=utf-8", "Content-Security-Policy": f"sandbox; {CSP}",
                 "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


def _e(s: Any) -> str:
    return html.escape(str(s), quote=True)


def _clock(now: datetime) -> str:
    return (f'<div class="clock"><div class="t">{now:%H:%M}</div>'
            f'<div class="d">{_DAYS[now.weekday()]} {now.day} {_MONTHS[now.month - 1]}</div></div>')


def _event(ev: dict[str, Any] | None, now: datetime, preview: bool) -> str:
    if preview:
        return ('<div class="card ev"><h3>Prochain rendez-vous</h3><p class="w">Sur le mur : ton prochain '
                'rendez-vous.</p></div>')
    if not ev:
        return '<div class="card ev"><h3>Prochain rendez-vous</h3><p class="w">Rien de prévu.</p></div>'
    when = ""
    try:
        start = datetime.fromisoformat(str(ev.get("start") or "").replace("Z", "+00:00"))
        if start.tzinfo is not None and now.tzinfo is not None:
            start = start.astimezone(now.tzinfo)
        day = ("aujourd'hui" if start.date() == now.date() else "demain" if start.date() == now.date() + timedelta(days=1)
               else f"{_DAYS[start.weekday()]} {start.day}")
        when = day if ev.get("all_day") else f"{day}, {start:%H:%M}"
    except (ValueError, TypeError):
        when = ""
    title = ev.get("title")
    head = _e(title) if isinstance(title, str) and title else "Rendez-vous"
    return (f'<div class="card ev"><h3>Prochain rendez-vous</h3><p class="what">{head}</p>'
            f'<p class="w">{_e(when)}</p></div>')


def _inner(b: dict[str, Any]) -> str:
    """Un composant dans une carte (jamais une carte dans une carte)."""
    t = b["type"]
    if t == "heading":
        return f"<h2>{_e(b['text'])}</h2>"
    if t == "text":
        return f"<p>{_e(b['text'])}</p>"
    if t == "list":
        head = f"<h3>{_e(b['title'])}</h3>" if b.get("title") else ""
        return head + "<ul>" + "".join(f'<li><span class="dot"></span><span>{_e(x)}</span></li>' for x in b["items"]) + "</ul>"
    if t == "checklist":
        head = f"<h3>{_e(b['title'])}</h3>" if b.get("title") else ""
        return head + "<ul>" + "".join(
            f'<li class="{"done" if x["done"] else "todo"}"><span class="box"></span><span>{_e(x["text"])}</span></li>'
            for x in b["items"]) + "</ul>"
    if t == "stat":
        return f'<div class="stat"><div class="v">{_e(b["value"])}</div><div class="l">{_e(b["label"])}</div></div>'
    if t == "divider":
        return "<hr>"
    return ""


def _card(blocks: list[dict[str, Any]], title: str | None = None) -> str:
    if not blocks:
        return ""
    if len(blocks) == 1 and blocks[0]["type"] == "note" and not title:
        b = blocks[0]
        return f'<div class="card note{" warn" if b.get("tone") == "warn" else ""}"><p>{_e(b["text"])}</p></div>'
    head = f"<h3>{_e(title)}</h3>" if title else ""
    body = "".join(f'<p class="{"note warn" if b.get("tone") == "warn" else "note"}">{_e(b["text"])}</p>'
                   if b["type"] == "note" else _inner(b) for b in blocks)
    return f'<div class="card">{head}{body}</div>'


def _layout(spec: dict[str, Any], now: datetime, ev: dict[str, Any] | None, preview: bool) -> tuple[str, str]:
    """(colonne de côté, cartes). L'heure et le prochain rendez-vous vont dans la colonne de côté, où qu'ils soient
    dans la description ; chaque group devient une carte, et les composants libres qui se suivent, une carte
    (un note seul, sa propre carte)."""
    side: list[str] = []
    cards: list[str] = []
    loose: list[dict[str, Any]] = []

    def flush() -> None:
        nonlocal loose
        run: list[dict[str, Any]] = []
        for b in loose:
            if b["type"] == "note":
                cards.append(_card(run))
                run = []
                cards.append(_card([b]))
            else:
                run.append(b)
        cards.append(_card(run))
        loose = []

    for b in spec["blocks"]:
        if b["type"] in ("clock", "next_event"):
            side.append(_clock(now) if b["type"] == "clock" else _event(ev, now, preview))
        elif b["type"] == "group":
            flush()
            rest = []
            for x in b["blocks"]:
                if x["type"] in ("clock", "next_event"):
                    side.append(_clock(now) if x["type"] == "clock" else _event(ev, now, preview))
                else:
                    rest.append(x)
            cards.append(_card(rest, b.get("title")))
        else:
            loose.append(b)
    flush()
    return "".join(side), "".join(c for c in cards if c)


def render(spec: dict[str, Any], *, now: datetime, next_event: dict[str, Any] | None = None, preview: bool = False,
           lang: str = "fr") -> str:
    """HTML complet d'un écran VALIDÉ (`catalog.validate`). `preview` : aperçu de la bulle, sans données du foyer."""
    side, cards = _layout(spec, now, next_event, preview)
    title = f"<h1>{_e(spec['title'])}</h1>"
    if side:
        main = f'<main class="hero"><div class="side">{title}{side}</div><div class="cards">{cards}</div></main>'
    else:
        main = f'<main>{title}<div class="cards">{cards}</div></main>'
    return (f'<!doctype html><html lang="{_e(lang)}" data-theme="{_e(spec.get("theme", "dark"))}"><head>'
            f'<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<meta http-equiv="Content-Security-Policy" content="{_e(CSP)}">'
            f'<title>{_e(spec["title"])}</title><style>{CSS}</style></head><body>{main}</body></html>')


__all__ = ["CSP", "CSS", "FONTS", "FONT_DIR", "FRAME_HEADERS", "MAX_HTML", "render"]
