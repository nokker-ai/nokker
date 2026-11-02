"""Image du cadre e-ink du foyer (docs/AMBIENT.md §3) : le tableau COMMUN du foyer (brief du jour, agenda familial,
courses, météo) mis en page pour la taille et la profondeur de l'écran, rendu en PNG 1 bit ou 4 gris.

Ce module ne lit RIEN : il reçoit un `Board` (le tableau du foyer, nokker/ambient/displays.py) et le dessine. Le
tableau ne contient que du contenu « foyer » par construction ; un élément marqué `private` est en plus écarté ici
(défense en profondeur), et le texte passe par le caviardage du noyau avant d'être dessiné.
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from nokker.ambient.font import WIDTH as GLYPH_W
from nokker.ambient.raster import Canvas, line_height, wrap

# Tailles courantes : (largeur, hauteur, profondeur par défaut). « custom » = largeur/hauteur données (bornes ci-dessous).
PRESETS: dict[str, tuple[int, int, int]] = {
    "trmnl": (800, 480, 1),
    "trmnl_4g": (800, 480, 2),
    "waveshare_7in5": (800, 480, 1),
    "waveshare_4in2": (400, 300, 1),
    "inkplate_10": (1200, 825, 2),
    "kindle_600x800": (600, 800, 2),
    "kindle_758x1024": (758, 1024, 2),
    "kindle_1072x1448": (1072, 1448, 2),
}
MIN_SIDE, MAX_SIDE = 200, 2000

MAX_BRIEF = 600
MAX_AGENDA = 8
MAX_COURSES = 20
MAX_ITEM = 80

FR_DAYS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
FR_MONTHS = ("janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre",
             "novembre", "décembre")


class FrameError(ValueError):
    """Réglage d'écran ou tableau refusé : le message revient tel quel à l'appelant (API)."""


def screen_size(model: str, width: int | None = None, height: int | None = None,
                depth: int | None = None) -> tuple[int, int, int]:
    """(largeur, hauteur, profondeur) d'un modèle connu, ou d'un écran `custom` borné."""
    if model in PRESETS:
        w, h, d = PRESETS[model]
    elif model == "custom":
        try:
            w, h, d = int(width or 0), int(height or 0), 1
        except (TypeError, ValueError):
            raise FrameError("largeur et hauteur : des entiers") from None
        if not (MIN_SIDE <= w <= MAX_SIDE and MIN_SIDE <= h <= MAX_SIDE):
            raise FrameError(f"écran custom : côtés entre {MIN_SIDE} et {MAX_SIDE} pixels")
    else:
        raise FrameError("modèle d'écran inconnu (" + ", ".join([*PRESETS, "custom"]) + ")")
    if depth is not None:
        if int(depth) not in (1, 2):
            raise FrameError("profondeur : 1 (noir et blanc) ou 2 (4 gris)")
        d = int(depth)
    return w, h, d


def _clip(s: Any, n: int) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n - 1] + "…"


@dataclass
class Board:
    """Tableau commun du foyer. Tout ce qui est ici est, par construction, visible par tous les membres et sur tout
    écran du foyer. `version` change à chaque écriture (sert de nom de fichier TRMNL et de clé de cache)."""

    brief: str = ""
    agenda: list[dict[str, str]] = field(default_factory=list)  # [{quand, quoi}]
    courses: list[str] = field(default_factory=list)
    meteo: dict[str, Any] = field(default_factory=dict)  # {resume, min, max}
    version: int = 0
    updated_at: float = 0.0

    @classmethod
    def clean(cls, raw: Any, *, version: int = 0, updated_at: float = 0.0) -> Board:
        """Tableau validé depuis l'API ; écarte tout élément marqué `private` (jamais affiché)."""
        if not isinstance(raw, dict):
            raise FrameError("tableau : un objet {brief, agenda, courses, meteo}")
        agenda_in = raw.get("agenda") or []
        courses_in = raw.get("courses") or []
        if not isinstance(agenda_in, list) or not isinstance(courses_in, list):
            raise FrameError("agenda et courses : des listes")
        agenda: list[dict[str, str]] = []
        for it in agenda_in:
            if isinstance(it, dict) and not it.get("private"):
                quoi = _clip(it.get("quoi") or it.get("what"), MAX_ITEM)
                if quoi:
                    agenda.append({"quand": _clip(it.get("quand") or it.get("when"), 24), "quoi": quoi})
        courses = []
        for it in courses_in:
            if isinstance(it, dict):
                if it.get("private"):
                    continue
                it = it.get("nom") or it.get("name")
            item = _clip(it, MAX_ITEM)
            if item:
                courses.append(item)
        meteo_in = raw.get("meteo") or {}
        meteo: dict[str, Any] = {}
        if isinstance(meteo_in, dict) and not meteo_in.get("private"):
            if meteo_in.get("resume"):
                meteo["resume"] = _clip(meteo_in["resume"], 60)
            for k in ("min", "max"):
                try:
                    if meteo_in.get(k) is not None:
                        meteo[k] = round(float(meteo_in[k]))
                except (TypeError, ValueError):
                    pass
        brief = raw.get("brief") or ""
        if isinstance(brief, dict):
            brief = "" if brief.get("private") else brief.get("texte") or brief.get("text") or ""
        return cls(brief=_clip(brief, MAX_BRIEF), agenda=agenda[:MAX_AGENDA], courses=courses[:MAX_COURSES],
                   meteo=meteo, version=version, updated_at=updated_at)

    def to_json(self) -> dict[str, Any]:
        return {"brief": self.brief, "agenda": list(self.agenda), "courses": list(self.courses),
                "meteo": dict(self.meteo), "version": self.version, "updated_at": self.updated_at}


def date_line(now: dt.datetime) -> str:
    return f"{FR_DAYS[now.weekday()].capitalize()} {now.day} {FR_MONTHS[now.month - 1]}"


@dataclass
class FrameRenderer:
    """Dessine un `Board` ; `scrub` = caviardage du noyau (un secret ne s'affiche jamais, même tapé par erreur)."""

    scrub: Callable[[str], str] = lambda s: s

    def render(self, board: Board, width: int, height: int, depth: int, *, now: dt.datetime,
               household_name: str = "") -> bytes:
        c = Canvas(width, height, depth)
        body = max(1, height // 400) if width >= 500 else 1
        title = body * 2
        margin = max(8, width // 40)
        gray = c.gray(0.34)
        y = margin
        sc = self.scrub
        head = _clip(sc(household_name), 40) if household_name else ""
        c.text(margin, y, date_line(now), scale=title)
        if head:
            hw = len(head) * GLYPH_W * body
            c.text(max(margin, width - margin - hw), y + line_height(title) - line_height(body), head, scale=body,
                   level=gray)
        y += line_height(title) + body * 4
        meteo = board.meteo
        if meteo:
            parts = [sc(meteo.get("resume", ""))]
            if "min" in meteo or "max" in meteo:
                parts.append(f"{meteo.get('min', '?')}° / {meteo.get('max', '?')}°")
            c.text(margin, y, " · ".join(p for p in parts if p), scale=body)
            y += line_height(body) + body * 2
        c.rect(margin, y, width - 2 * margin, max(1, body), 0)
        y += body * 6
        cols = 2 if width >= 700 and width > height else 1
        col_w = (width - 2 * margin - (margin if cols == 2 else 0)) // cols
        chars = max(10, col_w // (GLYPH_W * body))
        lh = line_height(body) + body * 2
        bottom = height - margin

        def section(x: int, y0: int, label: str, lines: list[tuple[str, int]]) -> int:
            if y0 + lh > bottom or not lines:
                return y0
            c.text(x, y0, label.upper(), scale=body, level=gray)
            y1 = y0 + lh
            for text, level in lines:
                for ln in wrap(text, chars) or [""]:
                    if y1 + lh > bottom:
                        return y1
                    c.text(x, y1, ln, scale=body, level=level)
                    y1 += lh
            return y1 + lh // 2

        left: list[tuple[str, list[tuple[str, int]]]] = []
        right: list[tuple[str, list[tuple[str, int]]]] = []
        if board.brief:
            left.append(("Aujourd'hui", [(sc(board.brief), 0)]))
        if board.agenda:
            (right if cols == 2 else left).append(
                ("Agenda", [(f"{sc(a['quand'])}  {sc(a['quoi'])}".strip(), 0) for a in board.agenda]))
        if board.courses:
            (right if cols == 2 else left).append(("Courses", [("- " + sc(x), 0) for x in board.courses]))
        if not (left or right):
            left.append(("Foyer", [("Rien au tableau aujourd'hui.", gray)]))
        yl = yr = y
        for label, lines in left:
            yl = section(margin, yl, label, lines)
        for label, lines in right:
            yr = section(margin + col_w + margin, yr, label, lines)
        return c.png()


__all__ = ["PRESETS", "Board", "FrameError", "FrameRenderer", "date_line", "screen_size"]
