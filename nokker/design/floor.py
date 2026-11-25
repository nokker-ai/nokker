"""Plancher de design vérifiable sans navigateur ni modèle (lot V8, docs/DESIGN-GUARD.md).

Règles tirées des planchers publiés par Hallmark (MIT) et Impeccable (Apache-2.0), réécrites ici en Python : aucun
code amont, aucun binaire, aucun accès réseau. Elles lisent une page HTML autonome (CSS dans `<style>` et `style=`).
Deux niveaux, comme en amont :
  - `floor` (plancher) : à corriger ;
  - `reflex` : un réflexe de modèle, à corriger ou à justifier.
Ce que seul un rendu ou un œil voit (hiérarchie, chiffres inventés, texte masqué) n'est PAS vérifié ici : un résultat
propre est un plancher, pas une preuve de qualité. Sert à `design_check` (pages) et au contrôle des écrans du mur (V7).
"""
from __future__ import annotations

import colorsys
import re
from dataclasses import dataclass
from html.parser import HTMLParser

MAX_BYTES = 2 * 1024 * 1024
MAX_FINDINGS = 40


@dataclass(frozen=True)
class Finding:
    rule: str
    level: str      # floor | reflex
    line: int
    message: str


# ─────────────────────────────── lecture ───────────────────────────────
class _Page(HTMLParser):
    INTERACTIVE = {"a", "button", "input", "select", "textarea", "summary"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.styles: list[tuple[int, str]] = []      # (ligne, css)
        self.inline: list[tuple[int, str]] = []
        self.tags: list[tuple[str, dict[str, str], int]] = []
        self.label_for: set[str] = set()
        self.texts: list[tuple[int, str, str]] = []  # (ligne, balise ouverte la plus proche, texte)
        self._stack: list[str] = []
        self._in_style = False
        self._in_label = 0
        self.html_lang: str | None = None
        self.has_viewport = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: (v or "") for k, v in attrs}
        line = self.getpos()[0]
        self.tags.append((tag, a, line))
        if tag == "html":
            self.html_lang = a.get("lang", "")
        if tag == "meta" and a.get("name", "").lower() == "viewport":
            self.has_viewport = True
        if tag == "style":
            self._in_style = True
        if tag == "label":
            self._in_label += 1
            if a.get("for"):
                self.label_for.add(a["for"])
        if tag in ("input", "select", "textarea") and self._in_label:
            a["_in_label"] = "1"
        if "style" in a:
            self.inline.append((line, a["style"]))
        if tag not in ("br", "img", "input", "meta", "link", "hr", "source", "wbr", "col", "area", "base"):
            self._stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag == "style":
            self._in_style = False
        if tag == "label" and self._in_label:
            self._in_label -= 1
        if tag in self._stack:
            while self._stack and self._stack.pop() != tag:
                pass

    def handle_data(self, data: str) -> None:
        line = self.getpos()[0]
        if self._in_style:
            self.styles.append((line, data))
        elif data.strip():
            self.texts.append((line, self._stack[-1] if self._stack else "", data))


_RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")
_COLOR = re.compile(r"#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?|oklch|oklab|lab|lch)\([^)]*\)")
_EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿\U0001F000-\U0001F2FF]")
_GENERIC_FONTS = {"serif", "sans-serif", "monospace", "cursive", "fantasy", "system-ui", "ui-sans-serif", "ui-serif",
                  "ui-monospace", "ui-rounded", "emoji", "math", "fangsong", "inherit", "initial", "unset"}


def _blocks(css: str, first_line: int) -> list[tuple[str, str, int]]:
    """(sélecteur, déclarations, ligne de la règle dans la page) à plat ; une règle dans un @media garde son sélecteur."""
    out = []
    css = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), css, flags=re.S)  # commentaires : lignes gardées
    for m in _RULE.finditer(css):
        sel = m.group(1)
        line = first_line + css[:m.start()].count("\n") + sel[:len(sel) - len(sel.lstrip())].count("\n")
        out.append((sel.strip(), m.group(2), line))
    return out


def _decls(body: str) -> dict[str, str]:
    d = {}
    for part in body.split(";"):
        if ":" in part:
            k, _, v = part.partition(":")
            d[k.strip().lower()] = v.strip()
    return d


# ─────────────────────────────── couleurs ───────────────────────────────
def _rgb(value: str, tokens: dict[str, str], depth: int = 0) -> tuple[float, float, float] | None:
    v = value.strip().lower()
    m = re.match(r"var\(\s*(--[\w-]+)\s*(?:,\s*(.+))?\)$", v)
    if m and depth < 5:
        return _rgb(tokens.get(m.group(1), m.group(2) or ""), tokens, depth + 1)
    m = re.match(r"#([0-9a-f]{3,8})$", v)
    if m:
        h = m.group(1)
        if len(h) in (3, 4):
            h = "".join(c * 2 for c in h[:3])
        if len(h) in (6, 8):
            return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]
        return None
    m = re.match(r"rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)", v)
    if m:
        return tuple(min(float(x), 255) / 255 for x in m.groups())  # type: ignore[return-value]
    named = {"white": (1.0, 1.0, 1.0), "black": (0.0, 0.0, 0.0)}
    return named.get(v)


def _lum(c: tuple[float, float, float]) -> float:
    def ch(x: float) -> float:
        return x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4
    r, g, b = (ch(x) for x in c)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _hue(c: tuple[float, float, float]) -> tuple[float, float]:
    h, _l, s = colorsys.rgb_to_hls(*c)
    return h * 360, s


def _purple_to_blue(grad: str, tokens: dict[str, str]) -> bool:
    hues = []
    for raw in _COLOR.findall(grad) + re.findall(r"var\(--[\w-]+\)", grad):
        c = _rgb(raw, tokens)
        if c is not None:
            h, s = _hue(c)
            if s > 0.25:
                hues.append(h)
    purple = any(260 <= h <= 320 for h in hues)
    blue = any(190 <= h < 260 for h in hues)
    return purple and blue


# ─────────────────────────────── contrôle ───────────────────────────────
def check(html: str) -> list[Finding]:
    if len(html.encode("utf-8", "ignore")) > MAX_BYTES:
        return [Finding("size", "floor", 1, "page de plus de 2 Mo : non vérifiée")]
    p = _Page()
    try:
        p.feed(html)
        p.close()
    except Exception:  # noqa: BLE001 — HTML cassé : on vérifie ce qui a été lu
        pass
    found: list[Finding] = []

    def add(rule: str, level: str, line: int, msg: str) -> None:
        if len(found) < MAX_FINDINGS:
            found.append(Finding(rule, level, line, msg))

    css_all = "\n".join(c for _l, c in p.styles)
    first_style_line = p.styles[0][0] if p.styles else 1
    blocks = [b for line, css in p.styles for b in _blocks(css, line)] + [("[style]", s, line) for line, s in p.inline]
    tokens: dict[str, str] = {}
    for sel, body, _line in blocks:
        if re.search(r":root|\[data-theme|^html\b|^body\b", sel):
            for k, v in _decls(body).items():
                if k.startswith("--"):
                    tokens[k] = v

    # 1. couleurs et polices passent par des jetons déclarés (:root ou bloc de thème)
    literal: list[int] = []
    for sel, body, line in blocks:
        if re.search(r":root|\[data-theme", sel):
            continue
        for k, v in _decls(body).items():
            if not k.startswith("--") and _COLOR.search(v):
                literal.append(line)
    if literal:
        add("tokens", "floor", literal[0], f"{len(literal)} couleur(s) écrite(s) en dur hors des jetons de :root "
            "(la première ici)")

    motion = bool(re.search(r"@keyframes|\banimation\s*:|\btransition\s*:", css_all)) or any(
        re.search(r"\banimation\s*:|\btransition\s*:", s) for _l, s in p.inline)
    for sel, body, line in blocks:
        d = _decls(body)
        tr = d.get("transition", "") + " " + d.get("transition-property", "")
        # 2. jamais `transition: all`, jamais d'animation de width/height/top/left
        if re.search(r"(^|[\s,])all\b", tr):
            add("transition-all", "floor", line, f"`transition: all` sur « {sel[:60]} »")
        if re.search(r"(^|[\s,])(width|height|top|left)\b", tr):
            add("animate-layout", "floor", line,
                f"transition sur width/height/top/left (« {sel[:60]} ») : transform et opacity seulement")
        # 4. overflow-x: clip sur html et body, jamais hidden ni 100vw
        if re.match(r"^(html|body)(\s*,\s*(html|body))?$", sel) and d.get("overflow-x", d.get("overflow", "")) == "hidden":
            add("overflow-hidden", "floor", line, f"`overflow: hidden` sur {sel} : `overflow-x: clip`")
        if any("100vw" in v for v in d.values()):
            add("100vw", "floor", line, f"`100vw` dans « {sel[:60]} » : width: 100%")
        # 9. pas d'étiquettes en capitales espacées
        if d.get("text-transform") == "uppercase" and d.get("letter-spacing", "0") not in ("0", "normal", ""):
            add("tracked-caps", "reflex", line, f"capitales espacées sur « {sel[:60]} »")
        # 11. pas de dégradé violet → bleu
        for k in ("background", "background-image"):
            if "gradient" in d.get(k, "") and _purple_to_blue(d[k], tokens):
                add("purple-blue-gradient", "floor", line, f"dégradé violet-bleu sur « {sel[:60]} »")
        # contraste quand la règle donne les deux couleurs
        fg = _rgb(d["color"], tokens) if "color" in d else None
        bgv = d.get("background-color") or (d.get("background") if d.get("background", "").split()[:1] and
                                            "gradient" not in d.get("background", "") else None)
        bg = _rgb(bgv.split()[0], tokens) if bgv else None
        if fg and bg:
            ratio = contrast(fg, bg)
            px = re.match(r"([\d.]+)px$", d.get("font-size", ""))
            bold = d.get("font-weight", "") in ("bold", "bolder") or d.get("font-weight", "0").isdigit() and int(
                d.get("font-weight", "0")) >= 700
            large = bool(px) and (float(px.group(1)) >= 24 or (bold and float(px.group(1)) >= 18.66))
            if ratio < (3.0 if large else 4.5):
                add("contrast", "floor", line, f"contraste {ratio:.1f}:1 sur « {sel[:60]} » (4,5:1 au moins, 3:1 pour "
                    "un grand texte)")
        fs = d.get("font-size", "")
        m = re.match(r"([\d.]+)px$", fs)
        if m and sel in ("body", "html", "p") and float(m.group(1)) < 16:
            add("small-body", "reflex", line, f"texte courant en {fs} sur {sel} (16px au moins)")
        elif m and float(m.group(1)) < 12:
            add("tiny-text", "floor", line, f"texte en {fs} sur « {sel[:60]} » (12px au moins)")

    # 6. focus visible dès qu'il y a des éléments interactifs
    interactive = [t for t in p.tags if t[0] in _Page.INTERACTIVE or "tabindex" in t[1]]
    if interactive and ":focus-visible" not in css_all:
        add("focus-visible", "floor", interactive[0][2], "éléments interactifs sans règle :focus-visible")
    # 7. toute animation a son repli prefers-reduced-motion
    if motion and "prefers-reduced-motion" not in css_all:
        add("reduced-motion", "floor", first_style_line, "animation ou transition sans repli prefers-reduced-motion")
    # polices : deux familles, trois au plus
    fams = set()
    for _sel, body, _line in blocks:
        ff = _decls(body).get("font-family")
        if ff and not ff.startswith("var("):
            first = ff.split(",")[0].strip().strip("'\"").lower()
            if first not in _GENERIC_FONTS:
                fams.add(first)
    if len(fams) > 3:
        add("font-families", "reflex", first_style_line, f"{len(fams)} familles de polices (deux, trois au plus)")

    # document
    if p.html_lang is not None and not p.html_lang.strip():
        add("lang", "floor", 1, "<html> sans attribut lang")
    if p.html_lang is None and p.tags:
        add("lang", "floor", 1, "pas d'élément <html lang=…>")
    if not p.has_viewport and p.tags:
        add("viewport", "floor", 1, "pas de <meta name=viewport>")
    for tag, a, line in p.tags:
        if tag == "img" and "alt" not in a:
            add("img-alt", "floor", line, "<img> sans alt (alt=\"\" pour une image décorative)")
        if tag in ("input", "select", "textarea") and a.get("type", "") not in ("hidden", "submit", "button", "reset"):
            if not (a.get("aria-label") or a.get("aria-labelledby") or a.get("_in_label")
                    or (a.get("id") and a["id"] in p.label_for)):
                add("label", "floor", line, f"<{tag}> sans étiquette (label ou aria-label)")
        for k in ("src", "href"):
            if re.match(r"(?i)^\s*(https?:)?//", a.get(k, "")) and tag != "a":
                add("external", "floor", line, f"ressource externe ({tag} {k}) : une page Nokker est autonome")
    # 11. emoji en guise d'icône ; 10. méta-chaînes à points médians
    for line, tag, text in p.texts:
        t = text.strip()
        if tag in ("button", "a", "li", "h1", "h2", "h3", "h4", "span", "i") and _EMOJI.match(t):
            add("emoji-icon", "floor", line, f"emoji en guise d'icône dans <{tag}>")
        if t.count(" · ") >= 2:
            add("middot-meta", "reflex", line, "métadonnées séparées par des points médians (·)")
    return found


def report(findings: list[Finding]) -> str:
    if not findings:
        return ("design_check : rien au plancher. Un plancher propre n'est pas une preuve de qualité : relis le code, et "
                "demande à la personne de regarder la page.")
    floor = sum(1 for f in findings if f.level == "floor")
    lines = [f"design_check : {floor} écart(s) au plancher (à corriger), {len(findings) - floor} réflexe(s) (à "
             "corriger ou à justifier)"]
    lines += [f"- [{'plancher' if f.level == 'floor' else 'réflexe'}] {f.rule}, ligne {f.line} : {f.message}"
              for f in findings]
    return "\n".join(lines)


__all__ = ["Finding", "check", "contrast", "report"]
