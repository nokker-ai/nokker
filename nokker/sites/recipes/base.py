"""Recettes des sites connectés (docs/CONNECTED-SITES.md §7) : gestes NOMMÉS, écrits et relus par nous.

Une recette ne voit la page que par l'arbre d'accessibilité (rôles et noms), jamais par des classes CSS. Elle ne
navigue que par `Nav.goto` (hôtes de la recette, gouverneur, détection des signaux) et ne tape un secret que par
`Nav.fill_secret`. Le modèle choisit un geste et ses arguments ; il ne pilote jamais la page.

`SocialRecipe` couvre la forme commune (connexion en un ou deux temps, notifications, messagerie, profils, recherche)
à partir d'un jeu de libellés et d'URL ; LinkedIn et le site simulé en sont deux instances.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlsplit

from nokker.sites.driver import Node, find, find_all

# Ce que la page demande, vu par `detect` (§4.2).
LOGGED_IN = "logged_in"
ASK_USERNAME = "username"
ASK_PASSWORD = "password"
ASK_CODE = "code"
ASK_APP = "app_approval"
LINK_SENT = "link_sent"
CAPTCHA = "captcha"
BAD_PASSWORD = "bad_password"
UNUSUAL = "unusual"
UNKNOWN = "unknown"


_DEGREE = re.compile(r"(?:\d+\s*(?:er|e|ème|st|nd|rd|th)\+?|[·•]\s*\d+\S*)", re.IGNORECASE)


class RecipeBroken(RuntimeError):
    """La page ne ressemble plus à ce que la recette connaît : le geste s'arrête, rien n'est deviné."""

    def __init__(self, gesture: str, step: str) -> None:
        super().__init__(f"{gesture} : étape {step}")
        self.gesture = gesture
        self.step = step


@dataclass
class Item:
    ref_kind: str  # msg | in | post | notif | comment
    url: str | None
    fields: dict[str, str]  # champs de TIERS (clôturés un à un)
    meta: dict[str, Any] = field(default_factory=dict)  # produits par la recette (hors clôture : date, degré, non lu)


@dataclass
class SocialRecipe:
    site: str
    name: str
    hosts: tuple[str, ...]
    labels: dict[str, Any]
    list_kinds: tuple[str, ...] = ("notifications", "messages")
    search_kinds: tuple[str, ...] = ("people", "posts")
    send_actions: tuple[str, ...] = ()  # écriture : lot L2, rien en L0/L1
    limits_counter: dict[str, str] = field(default_factory=lambda: {
        "search": "searches", "profile": "profiles"})
    max_text: int = 4000
    home_path: str = "/feed/"  # page d'accueil d'un compte connecté : vérification de la session après une pause
    version: str = "1"

    @property
    def base(self) -> str:
        return f"https://{self.hosts[0]}"

    @property
    def home_url(self) -> str:
        return self.base + self.home_path

    @property
    def login_url(self) -> str:
        return self.labels["login_url"]

    def gestures_line(self, read_only: bool = False) -> dict[str, tuple[str, ...]]:
        out = {"list": self.list_kinds, "search": self.search_kinds}
        if self.send_actions and not read_only:
            out["send"] = self.send_actions
        return out

    # ── connexion ──
    def detect(self, nodes: list[Node], url: str) -> str:
        L, path = self.labels, urlsplit(url).path
        names = " | ".join(n.name for n in nodes)
        if find(nodes, "link", re.compile("^" + re.escape(L["me"]))):
            return LOGGED_IN
        if L["unusual"] in names:
            return UNUSUAL
        if find(nodes, "alert", L["bad_password"]):
            return BAD_PASSWORD
        if L["captcha"] in names or find(nodes, "iframe", re.compile("captcha", re.I)):
            return CAPTCHA
        if find(nodes, "textbox", L["code"]):
            return ASK_CODE
        if L["app"] in names:
            return ASK_APP
        if L["link_sent"] in names:
            return LINK_SENT
        user_box = find(nodes, "textbox", L["username"])
        pass_box = find(nodes, "textbox", re.compile("^" + re.escape(L["password"]) + "$"))
        if user_box is not None and not user_box.value:
            return ASK_USERNAME
        if pass_box is not None:
            return ASK_PASSWORD
        if path.startswith("/checkpoint") or path.startswith("/login"):
            return UNKNOWN
        return UNKNOWN

    def type_username(self, nav: "Any", username: str) -> None:
        nodes = nav.nodes()
        box = find(nodes, "textbox", self.labels["username"])
        if box is None:
            raise RecipeBroken("login", "identifiant")
        nav.type(box.ref, username)
        if self.labels.get("continue"):
            btn = find(nav.nodes(), "button", self.labels["continue"])
            if btn is None:
                raise RecipeBroken("login", "continuer")
            nav.click(btn.ref)

    def fill_password(self, nav: "Any", fill: Callable[[str], None]) -> None:
        nodes = nav.nodes()
        box = find(nodes, "textbox", re.compile("^" + re.escape(self.labels["password"]) + "$"))
        if box is None:
            raise RecipeBroken("login", "mot de passe")
        fill(box.ref)
        btn = find(nav.nodes(), "button", self.labels["signin"])
        if btn is None:
            raise RecipeBroken("login", "valider")
        nav.click(btn.ref)

    def fill_code(self, nav: "Any", fill: Callable[[str], None]) -> None:
        box = find(nav.nodes(), "textbox", self.labels["code"])
        if box is None:
            raise RecipeBroken("login", "code")
        fill(box.ref)
        btn = find(nav.nodes(), "button", self.labels["validate"])
        if btn is None:
            raise RecipeBroken("login", "valider le code")
        nav.click(btn.ref)

    def account_name(self, nodes: list[Node]) -> str:
        me = find(nodes, "link", re.compile("^" + re.escape(self.labels["me"])))
        if me is None:
            return ""
        return me.name.split("—", 1)[1].strip() if "—" in me.name else ""

    # ── lecture ──
    def list(self, nav: "Any", kind: str, max_items: int) -> list[Item]:
        if kind == "notifications":
            nav.goto(self.base + "/notifications/")
            arts = find_all(nav.nodes(), "article")
            return [Item("notif", None, {"text": a.name}, {"date": a.attrs.get("date", "")}) for a in arts][:max_items]
        if kind == "messages":
            nav.goto(self.base + "/messaging/")
            rows = find_all(nav.nodes(), "listitem")
            seen, out = set(), []
            for r in rows:  # dédoublonnage par fil (jamais par texte : défaut de li_inbox_list.py)
                href = r.attrs.get("href", "")
                if not href or href in seen:
                    continue
                seen.add(href)
                out.append(Item("msg", self.base + href, {"with": r.name, "last": r.value},
                                {"date": r.attrs.get("date", ""), "unread": r.attrs.get("unread") == "1"}))
            return out[:max_items]
        raise ValueError(kind)

    def read(self, nav: "Any", ref_kind: str, url: str) -> tuple[str, list[Item]]:
        nav.goto(url)
        nodes = nav.nodes()
        if ref_kind == "msg":
            return "thread", [Item("msg", None, {"from": a.name, "text": a.value}, {"date": a.attrs.get("date", "")})
                              for a in find_all(nodes, "article")]
        if ref_kind == "in":
            head = next((n for n in nodes if n.role == "heading"), None)
            if head is None:
                raise RecipeBroken("read profile", "titre")
            headline = next((n.name for n in nodes if n.attrs.get("role_hint") == "headline"), "")
            degree = next((n.name for n in nodes if n.attrs.get("role_hint") == "degree"), "")
            if not headline and not degree:
                # vrai navigateur (§13.3) : pas d'indice de rôle ; titre = premier paragraphe après l'en-tête, degré =
                # un paragraphe « 1er », « 2e », « 3e+ »
                after = [n.value or n.name for n in nodes[nodes.index(head) + 1:]
                         if n.role in ("paragraph", "text") and (n.value or n.name)]
                degree = next((t for t in after if _DEGREE.fullmatch(t.strip())), "")
                headline = next((t for t in after if not _DEGREE.fullmatch(t.strip())), "")
            return "profile", [Item("in", None, {"name": head.name, "headline": headline}, {"degree": degree})]
        if ref_kind == "post":
            arts = find_all(nodes, "article")
            if not arts:
                raise RecipeBroken("read post", "post")
            post = arts[0]
            items = [Item("post", None, {"author": post.name, "text": post.value}, {})]
            # rôle `comment` (ARIA) : Chrome l'expose en `article` ; à défaut, les articles après le post
            comments = find_all(nodes, "comment") or arts[1:]
            items += [Item("comment", None, {"author": c.name, "text": c.value}, {}) for c in comments][:50]
            return "post", items
        return "page", [Item("page", None, {"text": nav.text()[:20000]}, {})]

    def search(self, nav: "Any", kind: str, query: str, max_items: int) -> list[Item]:
        path = {"people": "people", "posts": "content", "companies": "companies"}[kind]
        nav.goto(f"{self.base}/search/results/{path}/?keywords={quote(query)}")
        nodes = nav.nodes()
        if kind == "people":
            return [Item("in", self.base + n.attrs["href"], {"name": n.name, "headline": n.value},
                         {"degree": n.attrs.get("degree", "")})
                    for n in find_all(nodes, "link") if n.attrs.get("href", "").startswith("/in/")
                    and n.attrs.get("href") != "/in/me/"][:max_items]
        if kind == "posts":
            return [Item("post", self.base + n.attrs["href"], {"author": n.name, "text": n.value}, {})
                    for n in find_all(nodes, "article") if n.attrs.get("href")][:max_items]
        return []


__all__ = ["ASK_APP", "ASK_CODE", "ASK_PASSWORD", "ASK_USERNAME", "BAD_PASSWORD", "CAPTCHA", "Item", "LINK_SENT",
           "LOGGED_IN", "RecipeBroken", "SocialRecipe", "UNKNOWN", "UNUSUAL"]
