"""« Foyer démo » du kiosque d'un Lokkal (docs/KIOSK.md §1.2) : une vue Foyer FICTIVE, lue en configuration, pour montrer
le mur familial sur une machine qui n'a pas de vrai foyer (Lokkal n'a qu'un compte ; le vrai foyer, ses projets et
l'agent chef de projet vivent chez nokker.ai).

    kiosk:
      household_demo:                       # absent (défaut) : aucun champ `household`, vue du kiosque inchangée
        members: [{name: Léa, color: "#E4572E"}, {name: Hugo}, {name: Charlie}]
        agenda: [{day: 0, time: "17:30", duration_min: 90, member: Hugo, title: Foot}, {day: 2, all_day: true, title: Sortie}]
        tasks: [{member: Léa, title: Devoirs, done: false, due_day: 0}]
        meals: [{day: 0, lunch: Pâtes, dinner: Soupe}]
        shopping: [Pain, {text: Lait, done: true}]
        projects: [{title: Anniversaire de Charlie, progress: 0.4, next_step: Réserver la salle, next_due_day: 3}]

Règles tenues ici :
- 100 % configuration : aucune donnée de compte, d'agenda, de mail ni de Home Assistant ; les jours sont relatifs à
  « aujourd'hui » (`day` 0 à 6, fuseau du kiosque), donc la démo ne vieillit pas ;
- lecture seule, jamais d'action (le kiosque n'en a pas) ; la vue porte `demo: true` : le client la marque « démo » ;
- forme = `GlanceHousehold` du mur F1 (packages/sdk/src/household-wall.ts) SANS météo, départs ni Home Assistant, que le
  kiosque fournit lui-même ;
- une configuration mal formée est refusée en entier (motif au journal) : le kiosque reste sans foyer démo, jamais un
  plantage ni une demi-vue ; bornes strictes, champs inconnus refusés, caractères de contrôle et de bidi retirés."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from nokker.household.projects.wall import COLORS, initials

MAX_MEMBERS, MAX_AGENDA, MAX_TASKS, MAX_MEALS, MAX_SHOPPING, MAX_PROJECTS = 6, 12, 12, 7, 12, 4
MAX_TEXT = 80
_CTRL = re.compile(r"[\u0000-\u001f\u007f-\u009f​-‏‪-‮⁦-⁩]")
_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")
_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

_KEYS = {
    "members": {"name", "color"},
    "agenda": {"day", "time", "duration_min", "all_day", "member", "title"},
    "tasks": {"member", "title", "done", "due_day"},
    "meals": {"day", "lunch", "dinner"},
    "shopping": {"text", "done"},
    "projects": {"title", "progress", "next_step", "next_due_day"},
}


class DemoError(ValueError):
    pass


def _text(v: Any, what: str, required: bool = True) -> str:
    s = _CTRL.sub("", str(v if v is not None else "")).strip()
    if required and not s:
        raise DemoError(f"{what} : texte requis")
    if len(s) > MAX_TEXT:
        raise DemoError(f"{what} : {MAX_TEXT} caractères au plus")
    return s


def _day(v: Any, what: str) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 6:
        raise DemoError(f"{what} : entier de 0 (aujourd'hui) à 6")
    return v


def _rows(raw: Any, key: str, maximum: int) -> list[Any]:
    rows = raw.get(key) or []
    if not isinstance(rows, list) or len(rows) > maximum:
        raise DemoError(f"{key} : liste de {maximum} éléments au plus")
    return rows


def _item(row: Any, key: str) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise DemoError(f"{key} : objet attendu")
    unknown = set(row) - _KEYS[key]
    if unknown:
        raise DemoError(f"{key} : champs inconnus {sorted(unknown)}")
    return row


@dataclass(frozen=True)
class Demo:
    """Foyer démo validé ; `view(now)` rend la vue du jour, sans rien lire d'ailleurs."""

    members: tuple[dict[str, Any], ...]
    agenda: tuple[dict[str, Any], ...]
    tasks: tuple[dict[str, Any], ...]
    meals: tuple[dict[str, Any], ...]
    shopping: tuple[dict[str, Any], ...]
    projects: tuple[dict[str, Any], ...]

    @classmethod
    def parse(cls, raw: Any) -> Demo:
        if not isinstance(raw, dict):
            raise DemoError("household_demo : objet attendu")
        unknown = set(raw) - set(_KEYS)
        if unknown:
            raise DemoError(f"household_demo : champs inconnus {sorted(unknown)}")
        members: list[dict[str, Any]] = []
        for i, row in enumerate(_rows(raw, "members", MAX_MEMBERS)):
            r = _item(row if isinstance(row, dict) else {"name": row}, "members")
            name = _text(r.get("name"), "members.name")
            color = str(r.get("color") or COLORS[i % len(COLORS)])
            if not _COLOR.match(color):
                raise DemoError("members.color : #rrggbb")
            if any(m["name"] == name for m in members):
                raise DemoError(f"members : prénom en double {name!r}")
            members.append({"id": f"demo-m{i + 1}", "name": name, "color": color, "initials": initials(name)})
        by_name = {m["name"]: m["id"] for m in members}

        def member(r: dict[str, Any], what: str) -> str | None:
            n = r.get("member")
            if n is None:
                return None
            n = _text(n, what)
            if n not in by_name:
                raise DemoError(f"{what} : membre inconnu {n!r}")
            return by_name[n]

        agenda = []
        for i, row in enumerate(_rows(raw, "agenda", MAX_AGENDA)):
            r = _item(row, "agenda")
            a: dict[str, Any] = {"id": f"demo-e{i + 1}", "member_id": member(r, "agenda.member"),
                                 "day": _day(r.get("day", 0), "agenda.day"), "all_day": bool(r.get("all_day", False)),
                                 "title": _text(r.get("title"), "agenda.title")}
            if not a["all_day"]:
                m = _HHMM.match(str(r.get("time") or ""))
                if not m:
                    raise DemoError("agenda.time : HH:MM")
                a["time"] = time(int(m.group(1)), int(m.group(2)))
                dur = r.get("duration_min", 60)
                if isinstance(dur, bool) or not isinstance(dur, int) or not 5 <= dur <= 720:
                    raise DemoError("agenda.duration_min : 5 à 720")
                a["duration_min"] = dur
            agenda.append(a)
        tasks = []
        for i, row in enumerate(_rows(raw, "tasks", MAX_TASKS)):
            r = _item(row, "tasks")
            t: dict[str, Any] = {"id": f"demo-t{i + 1}", "member_id": member(r, "tasks.member"),
                                 "title": _text(r.get("title"), "tasks.title"), "done": bool(r.get("done", False))}
            if r.get("due_day") is not None:
                t["due_day"] = _day(r["due_day"], "tasks.due_day")
            tasks.append(t)
        meals = []
        for row in _rows(raw, "meals", MAX_MEALS):
            r = _item(row, "meals")
            m_: dict[str, Any] = {"day": _day(r.get("day"), "meals.day")}
            for k in ("lunch", "dinner"):
                if r.get(k):
                    m_[k] = _text(r[k], f"meals.{k}")
            meals.append(m_)
        shopping = []
        for i, row in enumerate(_rows(raw, "shopping", MAX_SHOPPING)):
            r = _item(row if isinstance(row, dict) else {"text": row}, "shopping")
            shopping.append({"id": f"demo-s{i + 1}", "text": _text(r.get("text"), "shopping.text"),
                             "done": bool(r.get("done", False))})
        projects = []
        for i, row in enumerate(_rows(raw, "projects", MAX_PROJECTS)):
            r = _item(row, "projects")
            prog = r.get("progress", 0)
            if isinstance(prog, bool) or not isinstance(prog, (int, float)) or not 0 <= prog <= 1:
                raise DemoError("projects.progress : nombre de 0 à 1")
            p: dict[str, Any] = {"id": f"demo-p{i + 1}", "title": _text(r.get("title"), "projects.title"),
                                 "progress": float(prog)}
            if r.get("next_step"):
                p["next_step"] = _text(r["next_step"], "projects.next_step")
            if r.get("next_due_day") is not None:
                p["next_due_day"] = _day(r["next_due_day"], "projects.next_due_day")
            projects.append(p)
        if not (members or agenda or tasks or meals or shopping or projects):
            raise DemoError("household_demo : vide")
        return cls(tuple(members), tuple(agenda), tuple(tasks), tuple(meals), tuple(shopping), tuple(projects))

    def view(self, now: datetime) -> dict[str, Any]:
        """La vue du jour (forme `GlanceHousehold`, sans météo/départs/HA). `now` : instant local du kiosque."""
        today = now.date()
        tz = now.tzinfo

        def d(offset: int) -> date:
            return today + timedelta(days=offset)

        agenda = []
        for a in self.agenda:
            ev: dict[str, Any] = {"id": a["id"], "member_id": a["member_id"], "all_day": a["all_day"],
                                  "title": a["title"]}
            if a["all_day"]:
                ev["start"] = d(a["day"]).isoformat()
            else:
                start = datetime.combine(d(a["day"]), a["time"], tzinfo=tz)
                ev["start"] = start.isoformat(timespec="seconds")
                ev["end"] = (start + timedelta(minutes=a["duration_min"])).isoformat(timespec="seconds")
            agenda.append(ev)
        agenda.sort(key=lambda e: e["start"])
        tasks = []
        for t in self.tasks:
            row = {"id": t["id"], "member_id": t["member_id"], "title": t["title"], "done": t["done"]}
            if "due_day" in t:
                row["due"] = d(t["due_day"]).isoformat()
            tasks.append(row)
        meals = [{"date": d(m["day"]).isoformat(), **{k: m[k] for k in ("lunch", "dinner") if k in m}}
                 for m in sorted(self.meals, key=lambda m: m["day"])]
        projects = []
        for p in self.projects:
            row = {"id": p["id"], "title": p["title"], "progress": p["progress"]}
            if "next_step" in p:
                row["next_step"] = p["next_step"]
            if "next_due_day" in p:
                row["next_due"] = d(p["next_due_day"]).isoformat()
            projects.append(row)
        return {"demo": True, "guest": False, "members": [dict(m) for m in self.members], "agenda": agenda,
                "tasks": tasks, "meals": meals, "shopping": [dict(s) for s in self.shopping], "projects": projects,
                "at": now.isoformat(timespec="seconds")}


__all__ = ["Demo", "DemoError"]
