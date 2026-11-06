"""Calculs, conversions d'unités et de dates — en local, sans réseau ni source externe.

`calc` évalue une expression arithmétique par un interpréteur d'arbre syntaxique restreint (nombres, + - * / // % **,
parenthèses, quelques fonctions mathématiques) : jamais `eval`, aucun nom hors de la liste, puissances et tailles bornées.
"""
from __future__ import annotations

import ast
import math
import operator
from collections.abc import Callable
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from nokker.tools.essentials.common import EssentialsError, fold, num, plural

# ─────────────────────────────── unités ───────────────────────────────

# dimension -> {unité canonique: facteur vers l'unité de base}
_UNITS: dict[str, dict[str, float]] = {
    "longueur": {"m": 1, "km": 1e3, "cm": 1e-2, "mm": 1e-3, "um": 1e-6, "mi": 1609.344, "yd": 0.9144,
                 "ft": 0.3048, "in": 0.0254, "nmi": 1852},
    "masse": {"kg": 1, "g": 1e-3, "mg": 1e-6, "t": 1e3, "lb": 0.45359237, "oz": 0.028349523125, "st": 6.35029318},
    "volume": {"l": 1, "ml": 1e-3, "cl": 1e-2, "dl": 0.1, "m3": 1e3, "cm3": 1e-3, "gal": 3.785411784,
               "ukgal": 4.54609, "floz": 0.0295735295625, "qt": 0.946352946, "pt": 0.473176473},
    "surface": {"m2": 1, "km2": 1e6, "cm2": 1e-4, "ha": 1e4, "a": 100, "ft2": 0.09290304, "acre": 4046.8564224},
    "vitesse": {"m/s": 1, "km/h": 1 / 3.6, "mph": 0.44704, "kn": 1852 / 3600},
    "durée": {"s": 1, "min": 60, "h": 3600, "d": 86400, "sem": 604800},
    "données": {"b": 1, "kb": 1e3, "mb": 1e6, "gb": 1e9, "tb": 1e12, "kib": 1024, "mib": 1024 ** 2,
                "gib": 1024 ** 3, "tib": 1024 ** 4},
    "énergie": {"j": 1, "kj": 1e3, "cal": 4.184, "kcal": 4184, "wh": 3600, "kwh": 3.6e6},
    "pression": {"pa": 1, "hpa": 100, "kpa": 1e3, "bar": 1e5, "mbar": 100, "atm": 101325, "psi": 6894.757293168,
                 "mmhg": 133.322387415},
    "puissance": {"w": 1, "kw": 1e3, "ch": 735.49875, "hp": 745.69987158227},
}
_ALIASES = {
    "metre": "m", "metres": "m", "meter": "m", "meters": "m", "kilometre": "km", "kilometres": "km",
    "kilometer": "km", "centimetre": "cm", "centimetres": "cm", "millimetre": "mm", "millimetres": "mm",
    "micrometre": "um", "µm": "um", "mile": "mi", "miles": "mi", "mille": "mi", "milles": "mi", "yard": "yd",
    "yards": "yd", "pied": "ft", "pieds": "ft", "foot": "ft", "feet": "ft", "pouce": "in", "pouces": "in",
    "inch": "in", "inches": "in", "mille marin": "nmi", "milles marins": "nmi", "nautical mile": "nmi",
    "kilogramme": "kg", "kilogrammes": "kg", "kilo": "kg", "kilos": "kg", "gramme": "g", "grammes": "g",
    "milligramme": "mg", "tonne": "t", "tonnes": "t", "livre": "lb", "livres": "lb", "lbs": "lb", "pound": "lb",
    "pounds": "lb", "once": "oz", "onces": "oz", "ounce": "oz", "stone": "st",
    "litre": "l", "litres": "l", "liter": "l", "millilitre": "ml", "centilitre": "cl", "decilitre": "dl",
    "m³": "m3", "cm³": "cm3", "gallon": "gal", "gallons": "gal", "us gal": "gal", "gallon imperial": "ukgal",
    "fl oz": "floz", "quart": "qt", "pint": "pt", "pinte": "pt",
    "m²": "m2", "km²": "km2", "cm²": "cm2", "hectare": "ha", "hectares": "ha", "are": "a", "ares": "a",
    "sq ft": "ft2", "ft²": "ft2", "acres": "acre",
    "kmh": "km/h", "km/heure": "km/h", "kph": "km/h", "noeud": "kn", "noeuds": "kn", "knot": "kn", "knots": "kn",
    "sec": "s", "seconde": "s", "secondes": "s", "minute": "min", "minutes": "min", "heure": "h", "heures": "h",
    "hr": "h", "jour": "d", "jours": "d", "day": "d", "days": "d", "semaine": "sem", "semaines": "sem",
    "week": "sem", "weeks": "sem",
    "octet": "b", "octets": "b", "byte": "b", "bytes": "b", "ko": "kb", "mo": "mb", "go": "gb", "to": "tb",
    "kio": "kib", "mio": "mib", "gio": "gib", "tio": "tib",
    "joule": "j", "joules": "j", "calorie": "cal", "calories": "cal", "kilocalorie": "kcal", "kilocalories": "kcal",
    "watt": "w", "watts": "w", "kilowatt": "kw", "cheval": "ch", "chevaux": "ch", "cv": "ch", "horsepower": "hp",
    "pascal": "pa", "millibar": "mbar", "atmosphere": "atm",
}
_TEMPS = {"c": "c", "°c": "c", "celsius": "c", "degre celsius": "c", "degres celsius": "c", "f": "f", "°f": "f",
          "fahrenheit": "f", "k": "k", "kelvin": "k"}


def _unit(raw: str) -> tuple[str, str] | None:
    plain = raw.strip().lower()
    folded = fold(raw).replace("°", "").strip()
    for cand in (plain.replace(" ", ""), _ALIASES.get(plain), _ALIASES.get(folded), folded.replace(" ", "")):
        if not cand:
            continue
        for dim, table in _UNITS.items():
            if cand in table:
                return dim, cand
    return None


def _temp(raw: str) -> str | None:
    return _TEMPS.get(raw.strip().lower()) or _TEMPS.get(fold(raw).replace("°", "").strip())


def _approx(out: float, fmt: str, unit: str) -> str:
    """Valeur arrondie AFFICHÉE comme telle, en plus de la valeur exacte (jamais à sa place)."""
    short = format(out, fmt)
    return "" if short == num(out) else f" (≈ {short} {unit})"


def convert(value: float, from_unit: str, to_unit: str) -> str:
    ta, tb = _temp(from_unit), _temp(to_unit)
    if ta and tb:
        kelvin = {"c": value + 273.15, "f": (value - 32) * 5 / 9 + 273.15, "k": value}[ta]
        out = {"c": kelvin - 273.15, "f": (kelvin - 273.15) * 9 / 5 + 32, "k": kelvin}[tb]
        names = {"c": "°C", "f": "°F", "k": "K"}
        return f"{num(value)} {names[ta]} = {num(out)} {names[tb]}{_approx(out, '.4g', names[tb])}"
    a, b = _unit(from_unit), _unit(to_unit)
    if a is None or b is None:
        unknown = from_unit if a is None and not ta else to_unit
        raise EssentialsError(f"unité inconnue : « {unknown} ».", "argument_invalide")
    if a[0] != b[0]:
        raise EssentialsError(f"conversion impossible : {from_unit} ({a[0]}) et {to_unit} ({b[0]}).",
                              "argument_invalide")
    out = value * _UNITS[a[0]][a[1]] / _UNITS[b[0]][b[1]]
    return f"{num(value)} {from_unit.strip()} = {num(out)} {to_unit.strip()}{_approx(out, '.6g', to_unit.strip())}"


# ─────────────────────────────── calcul ───────────────────────────────

_BIN: dict[type, Callable[[float, float], float]] = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow,
}
_UN = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCS: dict[str, Callable[..., float]] = {
    "sqrt": math.sqrt, "racine": math.sqrt, "abs": abs, "round": round, "arrondi": round, "min": min, "max": max,
    "log": math.log, "ln": math.log, "log10": math.log10, "exp": math.exp, "sin": math.sin, "cos": math.cos,
    "tan": math.tan, "floor": math.floor, "ceil": math.ceil,
}
_CONSTS = {"pi": math.pi, "e": math.e}
MAX_EXPR = 300
_MAX_ABS = 1e300


def _eval(node: ast.AST, depth: int = 0) -> float:
    if depth > 50:
        raise EssentialsError("expression trop imbriquée.", "argument_invalide")
    if isinstance(node, ast.Expression):
        return _eval(node.body, depth + 1)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.Name) and node.id in _CONSTS:
        return _CONSTS[node.id]
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UN:
        return _UN[type(node.op)](_eval(node.operand, depth + 1))
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
        left, right = _eval(node.left, depth + 1), _eval(node.right, depth + 1)
        if isinstance(node.op, ast.Pow) and (abs(right) > 1000 or (abs(left) > 1e6 and abs(right) > 50)):
            raise EssentialsError("puissance trop grande.", "argument_invalide")
        result = _BIN[type(node.op)](left, right)
        if isinstance(result, complex) or abs(result) > _MAX_ABS:
            raise EssentialsError("résultat hors limites.", "argument_invalide")
        return result
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS
            and not node.keywords and len(node.args) <= 10):
        return _FUNCS[node.func.id](*[_eval(a, depth + 1) for a in node.args])
    raise EssentialsError("expression non prise en charge (nombres, + - * / // % ^, parenthèses, sqrt, log, sin…).",
                          "argument_invalide")


def calc(expression: str) -> str:
    expr = expression.strip()
    if not expr:
        raise EssentialsError("expression vide.", "argument_invalide")
    if len(expr) > MAX_EXPR:
        raise EssentialsError(f"expression trop longue (> {MAX_EXPR} caractères).", "argument_invalide")
    src = expr.replace("^", "**").replace("×", "*").replace("÷", "/").replace("−", "-").replace("'", "")
    try:
        tree = ast.parse(src, mode="eval")
        value = _eval(tree)
    except EssentialsError:
        raise
    except ZeroDivisionError:
        raise EssentialsError("division par zéro.", "argument_invalide") from None
    except (SyntaxError, ValueError, TypeError, OverflowError) as exc:
        raise EssentialsError(f"expression invalide ({type(exc).__name__}).", "argument_invalide") from None
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
        shown = str(int(value))
    else:
        shown = f"{value:.12g}"
    return f"{expr} = {shown}"


# ─────────────────────────────── dates ───────────────────────────────

_WEEKDAYS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")


def today_ch() -> date:
    return datetime.now(ZoneInfo("Europe/Zurich")).date()


def parse_date(raw: str, today: date) -> date:
    v = raw.strip().lower()
    if v in ("", "today", "aujourd'hui", "aujourdhui", "heute", "oggi"):
        return today
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    raise EssentialsError(f"date illisible : « {raw} » (format AAAA-MM-JJ ou JJ.MM.AAAA).", "argument_invalide")


def _business_days(a: date, b: date) -> int:
    lo, hi = (a, b) if a <= b else (b, a)
    days = (hi - lo).days
    weeks, rest = divmod(days, 7)
    count = weeks * 5
    for i in range(rest):
        if (lo + timedelta(days=weeks * 7 + i)).weekday() < 5:
            count += 1
    return count


def date_calc(start: str, end: str, days: int | None, today: date | None = None) -> str:
    today = today or today_ch()
    a = parse_date(start, today)
    lines = [f"{a.isoformat()} : {_WEEKDAYS[a.weekday()]}, semaine ISO {a.isocalendar()[1]}"]
    if end.strip():
        b = parse_date(end, today)
        delta = (b - a).days
        lines.append(f"jusqu'au {b.isoformat()} ({_WEEKDAYS[b.weekday()]}) : {plural(delta, 'jour', 'jours')}, soit "
                     f"{plural(abs(delta) // 7, 'semaine', 'semaines')} et {plural(abs(delta) % 7, 'jour', 'jours')} ; "
                     f"{plural(_business_days(a, b), 'jour', 'jours')} du lundi au vendredi (borne de fin exclue, jours "
                     "fériés non déduits)")
    if days is not None:
        if abs(days) > 365_000:
            raise EssentialsError("décalage trop grand.", "argument_invalide")
        c = a + timedelta(days=days)
        lines.append(f"{'+' if days >= 0 else ''}{plural(days, 'jour', 'jours')} : {c.isoformat()} "
                     f"({_WEEKDAYS[c.weekday()]})")
    return "\n".join(lines)


__all__ = ["calc", "convert", "date_calc", "parse_date", "today_ch"]
