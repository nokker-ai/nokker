"""Recettes de portails de candidature par ATS (docs/JOBS-PACK.md §6.3) : gestes NOMMÉS, écrits et relus par nous.

Même règle que les recettes des sites connectés (CONNECTED-SITES §7) : la page n'est vue que par son arbre
d'accessibilité (rôles, noms, valeurs), jamais par des classes CSS ; la recette ne navigue que par `nav.goto` (hôtes de
la recette, gouverneur, signaux) et ne tape un secret que par `nav.fill_secret`. Deux gestes :
- `form_scan` (lecture) : offre vivante, connexion ou invité, parcours du formulaire SANS soumettre, champs connus
  remplis, relevé de tous les champs, consentements et pièces (`walk(..., submit=False)`) ;
- `apply` (écriture, après la carte) : le même parcours avec exactement le plan, relecture des valeurs et des fichiers,
  « Envoyer », page de confirmation reconnue (`walk(..., submit=True)`).

Catalogue de ce lot : Workday et SuccessFactors (libellés de l'interface en français). Les libellés réels seront
confirmés par l'essai réel, sur accord explicite du propriétaire, dépôt par dépôt (§12).
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from nokker.jobs.apply.fakeats import SUCCESSFACTORS, WORKDAY
from nokker.sites.driver import Node, find


class PortalBroken(RuntimeError):
    """La page ne ressemble plus à ce que la recette connaît : le geste s'arrête, rien n'est deviné ni envoyé."""

    def __init__(self, step: str) -> None:
        super().__init__(step)
        self.step = step


class Stale(RuntimeError):
    """Le formulaire a changé depuis la carte (nouveau champ obligatoire, option disparue) : rien n'est envoyé."""

    def __init__(self, change: str) -> None:
        super().__init__(change)
        self.change = change


class Captcha(RuntimeError):
    """Vérification de sécurité du portail : passation de l'écran à la personne, jamais résolue par Nokker."""


def norm(label: str) -> str:
    """Libellé comparable : sans « * » final, sans « : », casse et espaces repliés, accents gardés."""
    t = unicodedata.normalize("NFC", str(label or "")).strip()
    t = re.sub(r"\s*\*\s*$", "", t)
    t = re.sub(r"\s*:\s*$", "", t)
    return " ".join(t.split()).casefold()


@dataclass
class Seen:
    """Un champ relevé sur une page (valeur actuelle comprise)."""

    page: int
    ref: str
    label: str  # libellé de la page, sans « * »
    kind: str  # text | choice | check | file
    required: bool
    options: tuple[str, ...] = ()
    value: str = ""
    disabled: bool = False


# Clés canoniques de la fiche de candidature (§6.4) ← libellés connus (toutes recettes ; un libellé inconnu devient une
# question de screening).
SHEET_LABELS: dict[str, tuple[str, ...]] = {
    "first_name": ("prénom", "first name", "vorname"),
    "last_name": ("nom", "nom de famille", "last name", "nachname"),
    "email": ("adresse e-mail", "e-mail", "email", "adresse électronique"),
    "phone": ("téléphone", "numéro de téléphone", "phone", "telefon"),
    "street": ("adresse", "rue et numéro", "rue"),
    "postcode": ("npa", "code postal", "plz"),
    "city": ("ville", "localité", "ort"),
    "country": ("pays", "pays de résidence", "country"),
    "nationality": ("nationalité", "nationality"),
    "permit": ("permis de travail", "type de permis", "permis de séjour"),
    "work_auth_ch": ("êtes-vous autorisé(e) à travailler en suisse ?", "êtes-vous autorisé à travailler en suisse ?"),
    "availability": ("disponibilité", "disponibilité (date d'entrée possible)", "date d'entrée possible"),
    "salary": ("prétentions salariales annuelles (chf)", "prétentions salariales (chf)", "salaire annuel souhaité"),
    "gender": ("genre", "sexe", "gender"),
    "disability": ("handicap", "situation de handicap"),
    "origin": ("origine", "origine ethnique"),
}
LABEL_KEY = {lab: key for key, labs in SHEET_LABELS.items() for lab in labs}
# Données sensibles : jamais remplies par défaut ; « je préfère ne pas répondre » quand c'est offert (§6.4).
SENSITIVE = frozenset({"gender", "disability", "origin"})
DECLINE = re.compile(r"(préfère ne pas|prefer not|keine angabe|ne souhaite pas)", re.IGNORECASE)
# Prétention et permis : une valeur qui diffère de la fiche rend la carte FORTE (§7.1).
STRONG_KEYS = frozenset({"salary", "permit"})

# Consentements (§7.1) : au-delà de la candidature = rétention > 12 mois, outils d'IA du recruteur, transfert hors
# Suisse/UE.
_RETENTION = re.compile(r"conserv\w*[^.]*?(\d+)\s*mois|(\d+)\s*months", re.IGNORECASE)
_AI = re.compile(r"intelligence artificielle|outils? d'ia\b|\bia\b|\bai\b|artificial intelligence", re.IGNORECASE)
_TRANSFER = re.compile(r"hors de (la )?suisse|hors (de l')?ue\b|à l'étranger|outside switzerland", re.IGNORECASE)
_PRIVACY = re.compile(r"confidentialit|données personnelles|privacy|datenschutz", re.IGNORECASE)
_MARKETING = re.compile(r"newsletter|offres commerciales|marketing|alertes", re.IGNORECASE)


def consent_kind(label: str) -> str:
    """privacy | retention | ai | transfer | marketing | other ; `retention` seulement au-delà de 12 mois."""
    m = _RETENTION.search(label)
    if m and int(m.group(1) or m.group(2)) > 12:
        return "retention"
    if _AI.search(label):
        return "ai"
    if _TRANSFER.search(label):
        return "transfer"
    if _MARKETING.search(label):
        return "marketing"
    if _PRIVACY.search(label) or m:
        return "privacy"
    return "other"


BEYOND = frozenset({"retention", "ai", "transfer"})


@dataclass
class PortalRecipe:
    """Recette d'un ATS. `labels` : libellés d'accessibilité de l'interface ; `hosts` : hôtes de la recette."""

    ats: str
    name: str
    hosts: tuple[str, ...]
    labels: dict[str, Any]
    account: str = "optional"  # optional (invité possible) | required
    password_rule: tuple[int, int] = (10, 32)  # longueur min, max d'un mot de passe généré
    version: str = "1"

    def with_hosts(self, *hosts: str, **labels: Any) -> "PortalRecipe":
        """Instance pour un portail simulé (tests, e2e) : mêmes gestes, autres hôtes."""
        return replace(self, hosts=tuple(hosts), labels={**self.labels, **labels})

    # ── lecture de la page ──
    def closed(self, nodes: list[Node]) -> bool:
        return find(nodes, "heading", self.labels["closed"]) is not None

    def captcha(self, nodes: list[Node]) -> bool:
        return find(nodes, "heading", self.labels["captcha"]) is not None or \
            find(nodes, "iframe", re.compile("captcha", re.I)) is not None

    def confirmed(self, nodes: list[Node]) -> tuple[bool, str]:
        if find(nodes, "heading", self.labels["done"]) is None:
            return False, ""
        ref = next((n.name.split(":", 1)[1].strip() for n in nodes
                    if n.role in ("text", "paragraph") and re.match(r"^(Référence|Reference|Réf\.)\s*:", n.name)), "")
        return True, ref

    def offer_head(self, nodes: list[Node]) -> dict[str, str]:
        head = next((n.name for n in nodes if n.role == "heading"), "")
        company = next((n.name for n in nodes if n.attrs.get("role_hint") == "company"), "")
        desc = next((n.value for n in nodes if n.role == "article"), "")
        return {"title": head, "company": company, "description": desc}

    def fields(self, nodes: list[Node], page: int) -> list[Seen]:
        out = []
        for n in nodes:
            required = n.name.rstrip().endswith("*") or n.attrs.get("required") == "true"
            label = re.sub(r"\s*\*\s*$", "", n.name).strip()
            if n.role == "textbox":
                kind, opts = "text", ()
            elif n.role == "combobox":
                kind, opts = "choice", tuple(o for o in n.attrs.get("options", "").split("|") if o)
            elif n.role == "checkbox":
                kind, opts = "check", ()
            elif n.role == "button" and (n.attrs.get("file") == "1" or norm(label) in self.file_labels()):
                kind, opts = "file", ()
            else:
                continue
            value = n.value if kind != "check" else ("true" if n.attrs.get("checked") == "true" else "false")
            out.append(Seen(page, n.ref, label, kind, required, opts, value, n.attrs.get("disabled") == "true"))
        return out

    def file_labels(self) -> dict[str, str]:
        return {norm(self.labels["cv"]): "cv", norm(self.labels["letter"]): "letter"}

    # ── connexion ──
    def to_form(self, nav: Any, url: str, *, mode: str, login: Callable[[Any], None] | None = None) -> str:
        """Depuis la page de l'offre jusqu'à la première page du formulaire. `mode` : guest | login. Rend `form`,
        `closed`, `needs_account`. Lève `Captcha`, `PortalBroken`."""
        raise NotImplementedError

    def create_account(self, nav: Any, email: str, fill_password: Callable[[str], None],
                       code: Callable[[], str | None]) -> None:
        raise NotImplementedError

    # ── formulaire ──
    def walk(self, nav: Any, fill: Callable[[int, list[Seen]], list[Seen]], *, submit: bool,
             verify: Callable[[list[Node]], str | None] | None = None) -> dict[str, Any]:
        """Parcourt le formulaire. `fill(page, champs)` remplit la page et rend les champs obligatoires restés sans
        valeur (questions) ; s'il en reste, le parcours s'arrête là (`{"stopped": page}`). `submit=False` : s'arrête
        avant l'envoi (`{"ready": True}`). `submit=True` : `verify(nœuds)` relit la page d'envoi (None = conforme,
        sinon le défaut), puis « Envoyer » ; rend `{"sent": True, "confirmed": bool, "reference": str}`."""
        raise NotImplementedError

    def _click(self, nav: Any, name: str, step: str) -> None:
        btn = find(nav.nodes(), "button", re.compile("^" + re.escape(name) + r"\s*\*?$"))
        if btn is None:
            raise PortalBroken(step)
        nav.click(btn.ref)

    def _alert(self, nav: Any) -> str:
        a = find(nav.nodes(), "alert", re.compile("."))
        return a.name if a is not None else ""


class WorkdayRecipe(PortalRecipe):
    def to_form(self, nav: Any, url: str, *, mode: str, login: Callable[[Any], None] | None = None) -> str:
        nav.goto(url)
        nodes = nav.nodes()
        if self.closed(nodes):
            return "closed"
        self._click(nav, self.labels["apply"], "postuler")
        nodes = nav.nodes()
        if find(nodes, "button", self.labels["guest"]) is None:  # session de compte encore valide : déjà au formulaire
            return "form"
        if mode == "login" and login is not None:
            self._click(nav, self.labels["signin"], "connexion")
            login(nav)
            if self.captcha(nav.nodes()):
                raise Captcha()
            if self._alert(nav):
                return "bad_login"
            return "form"
        self._click(nav, self.labels["guest"], "invité")
        return "form"

    def create_account(self, nav: Any, email: str, fill_password: Callable[[str], None],
                       code: Callable[[], str | None]) -> None:
        self._click(nav, self.labels["create"], "créer un compte")
        nodes = nav.nodes()
        box = find(nodes, "textbox", re.compile("^" + re.escape(self.labels["email"])))
        pw = find(nodes, "textbox", re.compile("^" + re.escape(self.labels["password"]) + "$"))
        if box is None or pw is None:
            raise PortalBroken("création du compte")
        nav.type(box.ref, email)
        fill_password(find(nav.nodes(), "textbox", re.compile("^" + re.escape(self.labels["password"]) + "$")).ref)
        self._click(nav, self.labels["create"], "créer le compte")
        if self._alert(nav):
            raise PortalBroken("création du compte")

    def login(self, nav: Any, email: str, fill_password: Callable[[str], None]) -> None:
        nodes = nav.nodes()
        box = find(nodes, "textbox", re.compile("^" + re.escape(self.labels["email"])))
        if box is None:
            raise PortalBroken("connexion")
        nav.type(box.ref, email)
        pw = find(nav.nodes(), "textbox", re.compile("^" + re.escape(self.labels["password"]) + "$"))
        if pw is None:
            raise PortalBroken("connexion")
        fill_password(pw.ref)
        self._click(nav, self.labels["signin"], "connexion")

    def walk(self, nav: Any, fill: Callable[[int, list[Seen]], list[Seen]], *, submit: bool,
             verify: Callable[[list[Node]], str | None] | None = None) -> dict[str, Any]:
        steps = self.labels["steps"]
        for page in range(len(steps) - 1):
            nodes = nav.nodes()
            if self.captcha(nodes):
                raise Captcha()
            if find(nodes, "heading", steps[page]) is None:
                raise PortalBroken(f"page « {steps[page]} »")
            missing = fill(page, self.fields(nodes, page))
            if missing:
                return {"stopped": page, "missing": missing}
            self._click(nav, self.labels["next"], f"page « {steps[page]} »")
            alert = self._alert(nav)
            if alert:
                raise Stale(alert)
        nodes = nav.nodes()
        if find(nodes, "heading", steps[-1]) is None:
            raise PortalBroken("vérification")
        if not submit:
            return {"ready": True}
        if verify is not None:
            bad = verify(nodes)
            if bad:
                raise PortalBroken(f"relecture : {bad}")
        self._click(nav, self.labels["submit"], "envoi")
        nodes = nav.nodes()
        if self.captcha(nodes):
            raise Captcha()
        ok, ref = self.confirmed(nodes)
        return {"sent": True, "confirmed": ok, "reference": ref}


class SuccessFactorsRecipe(PortalRecipe):
    def to_form(self, nav: Any, url: str, *, mode: str, login: Callable[[Any], None] | None = None) -> str:
        nav.goto(url)
        nodes = nav.nodes()
        if self.closed(nodes):
            return "closed"
        self._click(nav, self.labels["apply"], "postuler")
        nodes = nav.nodes()
        if self.captcha(nodes):
            raise Captcha()
        if find(nodes, "button", self.labels["create"]) is None:  # déjà connecté : formulaire
            return "form"
        if mode != "login" or login is None:
            return "needs_account"
        login(nav)
        nodes = nav.nodes()
        if self.captcha(nodes):
            raise Captcha()
        if self._alert(nav):
            return "bad_login"
        return "form"

    def login(self, nav: Any, email: str, fill_password: Callable[[str], None]) -> None:
        nodes = nav.nodes()
        box = find(nodes, "textbox", re.compile("^" + re.escape(self.labels["email"])))
        pw = find(nodes, "textbox", re.compile("^" + re.escape(self.labels["password"]) + "$"))
        if box is None or pw is None:
            raise PortalBroken("connexion")
        nav.type(box.ref, email)
        fill_password(find(nav.nodes(), "textbox", re.compile("^" + re.escape(self.labels["password"]) + "$")).ref)
        self._click(nav, self.labels["signin"], "connexion")

    def create_account(self, nav: Any, email: str, fill_password: Callable[[str], None],
                       code: Callable[[], str | None]) -> None:
        self._click(nav, self.labels["create"], "créer un compte")
        L = self.labels
        # déclaration de confidentialité : à OUVRIR avant de pouvoir la cocher (§6.3) ; ouverte d'abord, la page de
        # création est rechargée au retour (ce qui serait tapé avant serait perdu)
        link = find(nav.nodes(), "link", L["privacy_link"])
        if link is None:
            raise PortalBroken("déclaration de confidentialité")
        nav.click(link.ref)
        back = find(nav.nodes(), "link", "Retour")
        if back is not None:
            nav.click(back.ref)
        nodes = nav.nodes()
        box = find(nodes, "textbox", re.compile("^" + re.escape(L["email"])))
        if box is None:
            raise PortalBroken("création du compte")
        nav.type(box.ref, email)
        for lab in (L["password"], L["password2"]):
            f = find(nav.nodes(), "textbox", re.compile("^" + re.escape(lab) + "$"))
            if f is None:
                raise PortalBroken("création du compte (mot de passe)")
            fill_password(f.ref)
        box = find(nav.nodes(), "checkbox", L["privacy_check"])
        if box is None or box.attrs.get("disabled") == "true":
            raise PortalBroken("déclaration de confidentialité")
        nav.check(box.ref, True)
        self._click(nav, L["create_submit"], "créer le compte")
        alert = self._alert(nav)
        if alert:
            raise PortalBroken("création du compte (refus du portail)")
        value = code()
        if not value:
            raise PortalBroken("code de vérification non reçu")
        cbox = find(nav.nodes(), "textbox", L["code"])
        if cbox is None:
            raise PortalBroken("code de vérification")
        nav.fill_secret(cbox.ref, value)
        self._click(nav, L["validate"], "code de vérification")
        if self._alert(nav):
            raise PortalBroken("code de vérification refusé")

    def walk(self, nav: Any, fill: Callable[[int, list[Seen]], list[Seen]], *, submit: bool,
             verify: Callable[[list[Node]], str | None] | None = None) -> dict[str, Any]:
        nodes = nav.nodes()
        if self.captcha(nodes):
            raise Captcha()
        if find(nodes, "button", self.labels["submit"]) is None:
            raise PortalBroken("formulaire")
        # CV et lettre re-téléversés juste avant chaque envoi : `fill` les pose sur CETTE page, maintenant
        missing = fill(0, self.fields(nodes, 0))
        if missing:
            return {"stopped": 0, "missing": missing}
        if not submit:
            return {"ready": True}
        nodes = nav.nodes()
        if verify is not None:
            bad = verify(nodes)
            if bad:
                raise PortalBroken(f"relecture : {bad}")
        self._click(nav, self.labels["submit"], "envoi")
        nodes = nav.nodes()
        if self.captcha(nodes):
            raise Captcha()
        alert = self._alert(nav)
        if alert:
            raise Stale(alert)
        ok, ref = self.confirmed(nodes)
        return {"sent": True, "confirmed": ok, "reference": ref}


# Libellés de l'interface française, relevés sur les portails simulés à l'image des campagnes réelles (§1) ; à
# confirmer par l'essai réel (§12, accord du propriétaire dépôt par dépôt). Hôtes réels des deux ATS.
WORKDAY_RECIPE = WorkdayRecipe(ats="workday", name="Workday", hosts=("myworkdayjobs.com", "myworkdaysite.com"),
                               labels=dict(WORKDAY), account="optional", password_rule=(12, 32))
SUCCESSFACTORS_RECIPE = SuccessFactorsRecipe(ats="successfactors", name="SuccessFactors",
                                             hosts=("successfactors.com", "successfactors.eu", "sapsf.com",
                                                    "sapsf.eu"),
                                             labels=dict(SUCCESSFACTORS), account="required", password_rule=(8, 18))
PORTALS: dict[str, PortalRecipe] = {r.ats: r for r in (WORKDAY_RECIPE, SUCCESSFACTORS_RECIPE)}

__all__ = ["BEYOND", "Captcha", "DECLINE", "LABEL_KEY", "PORTALS", "PortalBroken", "PortalRecipe", "SENSITIVE",
           "SHEET_LABELS", "STRONG_KEYS", "SUCCESSFACTORS_RECIPE", "Seen", "Stale", "SuccessFactorsRecipe",
           "WORKDAY_RECIPE", "WorkdayRecipe", "consent_kind", "norm"]
