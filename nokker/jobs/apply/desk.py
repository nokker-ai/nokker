"""`jobsd` : dépôts de candidatures par Lokkal, par-dessus `sitesd` (docs/JOBS-PACK.md §3.2, §6, §7.4).

Trois temps (§6.5), jamais un formulaire gardé ouvert pendant l'attente d'une carte :
1. `scan` (geste `form_scan`) : connexion au portail (compte de la personne) ou invité, offre vivante, parcours du
   formulaire SANS soumettre, champs connus remplis depuis la fiche et les réponses, relevé de tous les champs, des
   consentements et des pièces ; questions obligatoires sans réponse → rendues groupées, jamais devinées ; onglet fermé.
2. carte (posée par l'outil) avec le plan complet ; le plan est rangé chiffré, son empreinte liée à la carte.
3. `submit` (geste `apply`), seulement à l'approbation : rejeu depuis le début avec exactement le plan, tout écart
   (champ obligatoire nouveau, option disparue) → `Stale` ; relecture des valeurs et des fichiers avant « Envoyer » ;
   page de confirmation reconnue et CAPTURÉE (preuve), sinon `unconfirmed`, jamais renvoyé.

Comptes de portails = sites connectés de la catégorie « Portails emploi » (§6.2) : un connecteur par (ATS, hôte de
l'employeur), dans un magasin à part (plafond 50 par profil), mot de passe au coffre. Un compte créé pendant un dépôt
reçoit un mot de passe GÉNÉRÉ dans le coffre par Lokkal (décision du propriétaire du 05.11), tapé par `fill_secret`, jamais
montré au modèle. CAPTCHA : passation de l'écran à la personne (lien à usage unique), jamais résolu.
"""
from __future__ import annotations

import hashlib
import logging
import re
import secrets
import string
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from nokker.jobs.apply.journal import DepositJournal, Plans, SheetStore, Vaulted
from nokker.jobs.apply.model import FilePart, Plan, PlanField, Question, new_plan_id
from nokker.jobs.apply.portals import (BEYOND, DECLINE, LABEL_KEY, PORTALS, SENSITIVE, STRONG_KEYS, Captcha,
                                       PortalBroken, PortalRecipe, Seen, Stale, consent_kind, norm)
from nokker.sites.credentials import REASON_EXPIRED, REASON_FIRST, Credentials
from nokker.sites.driver import MaskedDriver, Node
from nokker.sites.governor import Governor, GovernorError, Limits
from nokker.sites.model import Connector, State
from nokker.sites.store import SitesStore

logger = logging.getLogger("nokker.jobs.apply")

PORTAL_MAX = 50  # comptes de portails par profil (§6.2)
PORTAL_LIMITS = {"pages": Limits(60, 300), "applies": Limits(3, 10, 30), "logins": Limits(3, 6)}


class DeskError(RuntimeError):
    """Arrêt d'un geste ; `code` est un code du §5.12 (ou de CONNECTED-SITES §8.6), `extra` remplit le texte."""

    def __init__(self, code: str, **extra: Any) -> None:
        super().__init__(code)
        self.code = code
        self.extra = extra


@dataclass
class Piece:
    """Une pièce à joindre : ce que la carte montre (`part`) et les octets (jamais montrés au modèle)."""

    part: FilePart
    data: bytes


@dataclass
class Offer:
    ref: str
    id: int
    url: str
    company: str
    title: str
    location: str
    ats: str  # clé de l'ATS donnée par le service (`workday`…), ou vide


@dataclass
class ScanResult:
    status: str  # questions | ready | account
    plan: Plan
    questions: list[Question] = field(default_factory=list)
    answered: dict[str, str] = field(default_factory=dict)  # réponses utilisées (à proposer pour la fiche)


@dataclass
class Outcome:
    status: str  # sent | unconfirmed
    deposit: str
    reference: str = ""
    proof: dict[str, Any] = field(default_factory=dict)


def generate_password(min_len: int, max_len: int) -> str:
    """Mot de passe d'un compte de portail (aléa système) : majuscule, minuscule, chiffre, dans les bornes du portail."""
    n = max(min_len, min(max_len, 16))
    alphabet = string.ascii_letters + string.digits
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(n))
        if re.search(r"[A-Z]", pw) and re.search(r"[a-z]", pw) and re.search(r"\d", pw):
            return pw


def _sha8(text: str) -> str:
    return hashlib.sha1(norm(text).encode("utf-8")).hexdigest()[:8]  # noqa: S324 — identifiant, pas une sécurité


def field_id(recipe: PortalRecipe, f: Seen) -> str:
    """Identifiant stable d'un champ : rôle de pièce, clé de la fiche, consentement ou question."""
    if f.kind == "file":
        return recipe.file_labels().get(norm(f.label), "file_" + _sha8(f.label))
    if f.kind == "check":
        return "c_" + _sha8(f.label)
    return LABEL_KEY.get(norm(f.label)) or "q_" + _sha8(f.label)


class PortalNav:
    """Ce que voit une recette de portail : hôtes de la recette, allure du gouverneur, gestes du pilote masqué."""

    def __init__(self, conn: Connector, drv: MaskedDriver, recipe: PortalRecipe, gov: Governor) -> None:
        self.conn, self.drv, self.recipe, self.gov = conn, drv, recipe, gov
        self.uploaded: dict[str, str] = {}  # libellé du champ → nom du fichier relu dans la page à la pose

    def goto(self, url: str) -> None:
        if not _host_ok(url, self.recipe.hosts):
            raise DeskError("recipe_broken", step="adresse hors du portail")
        self.gov.page(self.conn.id)
        self.drv.goto(url)

    def nodes(self) -> list[Node]:
        return self.drv.nodes()

    def url(self) -> str:
        return self.drv.url()

    def type(self, ref: str, text: str) -> None:
        self.drv.type(ref, text)

    def click(self, ref: str) -> None:
        self.drv.click(ref)

    def fill_secret(self, ref: str, value: str) -> None:
        self.drv.fill_secret(ref, value)

    def select(self, ref: str, value: str) -> None:
        self.drv.select(ref, value)

    def check(self, ref: str, on: bool) -> None:
        self.drv.check(ref, on)

    def upload(self, ref: str, name: str, data: bytes) -> None:
        self.drv.upload(ref, name, data)


def _host_ok(url: str, hosts: tuple[str, ...]) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return parts.scheme in ("https", "http") and any(host == h or host.endswith("." + h) for h in hosts) and (
        parts.scheme == "https" or host.endswith(".test") or ":" in (parts.netloc or ""))


class JobsDesk:
    """`jobsd`. Dépend de `sitesd` (pilotes, coffre, demandes, passation, notifications)."""

    def __init__(self, *, sites: Any, master: bytes, state_dir: Any = None,
                 recipes: dict[str, PortalRecipe] | None = None,
                 driver_factory: Callable[[Connector, bytes | None], Any] | None = None,
                 mail_code: Callable[[Connector, str, float], str | None] | None = None,
                 mail_ack: Callable[[Connector, str, float], str | None] | None = None,
                 governor_factory: Callable[[PortalRecipe], Governor] | None = None,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
                 handoff_wait_s: float = 900.0, handoff_poll_s: float = 5.0, code_wait_s: float = 180.0) -> None:
        from pathlib import Path

        root = Path(state_dir) / "jobs" if state_dir else None
        self.sites = sites
        self.recipes = recipes if recipes is not None else dict(PORTALS)
        self.store = SitesStore(root / "portals.json" if root else None)
        self.store.max_per_account = PORTAL_MAX
        # passation : le lien à usage unique de `sitesd` retrouve aussi un connecteur de portail
        sites.extra_stores.append(self.store)
        self.credentials = Credentials(sites.credentials.vault, self.store, sites.credentials.known,
                                       sites.credentials.on_known)
        vault = Vaulted(master, root / "vault" if root else None)
        self.sheets = SheetStore(vault)
        self.plans = Plans(vault, clock)
        self.journal = DepositJournal(master, root, clock)
        self.driver_factory = driver_factory or sites.driver_factory
        self.mail_code, self.mail_ack = mail_code, mail_ack
        self._gov_factory = governor_factory or (lambda r: Governor(limits=dict(PORTAL_LIMITS), clock=clock,
                                                                    sleep=sleep))
        self._govs: dict[str, Governor] = {}
        self.clock, self.sleep = clock, sleep
        self.handoff_wait_s, self.handoff_poll_s, self.code_wait_s = handoff_wait_s, handoff_poll_s, code_wait_s
        self._lock = threading.Lock()

    # ── portails, connecteurs ──
    def recipe_for(self, offer: Offer) -> PortalRecipe | None:
        r = self.recipes.get(offer.ats)
        if r is not None and _host_ok(offer.url, r.hosts):
            return r
        return next((r for r in self.recipes.values() if _host_ok(offer.url, r.hosts)), None)

    def gov(self, conn: Connector, recipe: PortalRecipe) -> Governor:
        with self._lock:
            if conn.id not in self._govs:
                self._govs[conn.id] = self._gov_factory(recipe)
            return self._govs[conn.id]

    def connector(self, account: str, recipe: PortalRecipe, offer: Offer, username: str) -> Connector:
        """Le connecteur du portail de CET employeur (un par ATS et hôte), créé au premier dépôt (sans compte)."""
        host = (urlsplit(offer.url).hostname or "").lower()
        site = f"portal:{recipe.ats}"
        for c in self.store.of(account):
            if c.site == site and host in c.hosts:
                return c
        label = f"{recipe.name} {offer.company}"[:40].replace("/", " ").replace("|", " ").replace("·", " ")
        try:
            return self.store.add(account=account, site=site, site_name=recipe.name, username=username or "invité",
                                  label=label, hosts=[host])
        except Exception:  # noqa: BLE001 — libellé déjà pris (même entreprise, autre hôte) : libellé numéroté
            return self.store.add(account=account, site=site, site_name=recipe.name, username=username or "invité",
                                  hosts=[host])

    def has_account(self, conn: Connector) -> bool:
        return self.credentials.has(conn)

    # ── un pilote ──
    def _session(self, conn: Connector, recipe: PortalRecipe, fn: Callable[[PortalNav, MaskedDriver], Any]) -> Any:
        gov = self.gov(conn, recipe)
        try:
            gov.check_pause(conn.id)
            with gov.driver(conn.id):
                try:
                    raw = self.driver_factory(conn, self.sites.profiles.load(conn.account, conn.id))
                except RuntimeError:
                    raise DeskError("sites_absent") from None
                drv = MaskedDriver(raw)
                try:
                    out = fn(PortalNav(conn, drv, recipe, gov), drv)
                    try:
                        self.sites.profiles.save(conn.account, conn.id, drv.export_profile())
                    except Exception:  # noqa: BLE001 — profil non sauvé : session à refaire au prochain dépôt
                        logger.warning("jobsd : profil du portail non sauvé (%s)", conn.id)
                    return out
                finally:
                    drv.close()  # jamais un formulaire gardé ouvert pendant l'attente d'une carte
        except GovernorError as e:
            raise DeskError(e.code, label=conn.label, **e.extra) from None
        except Captcha:
            self._handoff(conn, recipe)
            raise DeskError("site_needs_you", label=conn.label) from None
        except PortalBroken as e:
            raise DeskError("recipe_broken", portal=recipe.name, step=e.step) from None

    def _login_fn(self, conn: Connector, recipe: PortalRecipe, drv: MaskedDriver) -> Callable[[Any], None]:
        reason = REASON_EXPIRED if conn.last_ok_at else REASON_FIRST

        def login(nav: Any) -> None:
            drv.in_login = True
            try:
                recipe.login(nav, conn.username, lambda ref: self.credentials.use_password(
                    conn, reason, lambda v: drv.fill_secret(ref, v) and None))
            finally:
                drv.in_login = False

        return login

    def _handoff(self, conn: Connector, recipe: PortalRecipe) -> str:
        """CAPTCHA ou écran inconnu : lien de passation à usage unique, la personne fait la vérification elle-même."""
        token = self.sites._new_handoff(conn)
        conn = self.store.get(conn.id) or conn
        conn.state, conn.signal = State.NEEDS_YOU, "captcha"
        self.store.put(conn)
        self.sites._notify(conn, "handoff", {"why": "captcha", "handoff_url": f"{self.sites.origin}/sites/h/{token}"})
        return token

    def _ask_code(self, conn: Connector, recipe: PortalRecipe, since: float) -> str | None:
        """Code envoyé par le portail : lu dans la boîte reliée (068) si elle l'est, sinon demandé à la personne."""
        if self.mail_code is not None:
            try:
                code = self.mail_code(conn, recipe.labels.get("mail_from", ""), since)
            except Exception:  # noqa: BLE001 — boîte injoignable : on demande le code
                code = None
            if code:
                return code
        req = self.sites.requests.open(conn.id, conn.account, conn.label, "code")
        self.sites._notify(conn, "request", {"kind": "code", "request_id": req.id,
                                             "link": self.sites.secure_link(req.id)})
        return self.sites.requests.wait(req.id, self.code_wait_s)

    # ── 1. relevé à blanc ──
    def scan(self, *, account: str, profile: str, offer: Offer, pieces: dict[str, Piece], answers: dict[str, str],
             username: str, letter_required_hint: bool = False) -> ScanResult:
        recipe = self.recipe_for(offer)
        if recipe is None:
            raise DeskError("canal_manuel")
        conn = self.connector(account, recipe, offer, username)
        sheet = self.sheets.get(account, profile)
        plan = Plan(id=new_plan_id(), profile=profile, owner_account=account, ats=recipe.ats, portal=recipe.name,
                    offer_ref=offer.ref, offer_id=offer.id, company=offer.company, title=offer.title,
                    location=offer.location, url=offer.url, account="guest", username="", connector=conn.id,
                    files=[p.part for p in pieces.values()], created_at=self.clock())
        if self.has_account(conn):
            plan.account, plan.username = "login", conn.username
        elif recipe.account == "required":
            # Le formulaire n'est visible qu'avec un compte : carte FORTE de création d'abord (§6.2, §7.1).
            plan.kind, plan.account, plan.username = "account", "create", username
            plan.strong.append("account")
            if not username:
                raise DeskError("argument_invalide", case="username")
            self._offer_alive(conn, recipe, offer)
            self.plans.put(plan)
            return ScanResult("account", plan)
        return self._scan_form(conn, recipe, plan, offer, pieces, answers, sheet)

    def _offer_alive(self, conn: Connector, recipe: PortalRecipe, offer: Offer) -> None:
        def run(nav: PortalNav, drv: MaskedDriver) -> None:
            nav.goto(offer.url)
            if recipe.closed(nav.nodes()):
                raise DeskError("offer_closed")

        self._session(conn, recipe, run)

    def _scan_form(self, conn: Connector, recipe: PortalRecipe, plan: Plan, offer: Offer, pieces: dict[str, Piece],
                   answers: dict[str, str], sheet: dict[str, Any], *, open_form: Callable | None = None
                   ) -> ScanResult:
        questions: list[Question] = []
        used: dict[str, str] = {}

        def run(nav: PortalNav, drv: MaskedDriver) -> dict[str, Any]:
            if open_form is not None:
                state = open_form(nav, drv)
            else:
                state = recipe.to_form(nav, offer.url, mode=plan.account,
                                       login=self._login_fn(conn, recipe, drv) if plan.account == "login" else None)
            if state == "closed":
                raise DeskError("offer_closed")
            if state == "bad_login":
                raise DeskError("site_suspended", label=conn.label)
            if state != "form":
                raise DeskError("recipe_broken", portal=recipe.name, step="ouverture du formulaire")

            def fill(page: int, seen: list[Seen]) -> list[Seen]:
                missing = []
                for f in seen:
                    pf = self._decide(recipe, f, pieces, answers, sheet, plan, used)
                    plan.fields.append(pf)
                    if pf.value and pf.value not in ("false",):
                        _apply_value(nav, recipe, f, pf, pieces)
                    elif f.required and not pf.value:
                        missing.append(f)
                        questions.append(Question(id=pf.id, label=f.label, kind=_qkind(f), options=list(f.options),
                                                  required=True, sensitive=pf.sensitive))
                return missing

            return recipe.walk(nav, fill, submit=False)

        walked = self._session(conn, recipe, run)
        conn = self.store.get(conn.id) or conn
        if plan.account == "login":
            conn.last_ok_at = self.clock()
            conn.state = State.CONNECTED
            self.store.put(conn)
        if walked.get("stopped") is not None or questions:
            return ScanResult("questions", plan, questions, used)
        for f in plan.fields:
            if f.consent in BEYOND and f.value == "true":
                plan.strong.append(f"consent:{f.consent}")
        self.plans.put(plan)
        return ScanResult("ready", plan, [], used)

    def _decide(self, recipe: PortalRecipe, f: Seen, pieces: dict[str, Piece], answers: dict[str, str],
                sheet: dict[str, Any], plan: Plan, used: dict[str, str]) -> PlanField:
        fid = field_id(recipe, f)
        pf = PlanField(page=f.page, id=fid, label=f.label, kind=f.kind, required=f.required,
                       options=list(f.options))
        if f.kind == "file":
            role = fid if fid in ("cv", "letter") else ""
            if role and role in pieces:
                pf.value, pf.source = role, "pièce"
            elif role == "letter" and f.required:
                raise DeskError("needs_letter", offer=plan.offer_ref)
            else:
                pf.source = "vide"
            return pf
        if f.kind == "check":
            pf.consent = consent_kind(f.label)
            given = _answer(answers, fid, f.label)
            if given is not None:
                pf.value, pf.source = ("true" if _truthy(given) else "false"), "réponse"
            elif f.required and pf.consent != "marketing":
                pf.value, pf.source = "true", "consentement"  # vaut accord seulement par la carte
            else:
                pf.value, pf.source = "false", "vide"
            return pf
        key = fid if not fid.startswith("q_") else ""
        pf.sensitive = key in SENSITIVE
        given = _answer(answers, fid, f.label)
        value, source = None, ""
        if given is not None:
            value, source = given, "réponse"
            used[fid] = given
        elif key and not pf.sensitive and sheet["fields"].get(key):
            value, source = sheet["fields"][key], "fiche"
        elif not pf.sensitive and sheet["answers"].get(norm(f.label)):
            value, source = sheet["answers"][norm(f.label)], "fiche"
        elif pf.sensitive and f.kind == "choice":
            decline = next((o for o in f.options if DECLINE.search(o)), None)
            if decline is not None:
                value, source = decline, "défaut"
        if value is not None and f.kind == "choice":
            value = next((o for o in f.options if o.casefold() == str(value).strip().casefold()), None)
        if value:
            pf.value, pf.source = str(value), source
            if key in STRONG_KEYS and source == "réponse" and sheet["fields"].get(key) \
                    and norm(sheet["fields"][key]) != norm(value):
                plan.strong.append(f"differs:{key}")
        else:
            pf.source = "vide"
        return pf

    # ── création du compte (carte forte « compte ») ──
    def create_account(self, plan: Plan, offer: Offer, pieces: dict[str, Piece], answers: dict[str, str]
                       ) -> ScanResult:
        recipe = self.recipes[plan.ats]
        conn = self.store.get(plan.connector)
        if conn is None:
            raise DeskError("ref_unknown")
        sheet = self.sheets.get(plan.owner_account, plan.profile)
        since = self.clock()

        def open_form(nav: PortalNav, drv: MaskedDriver) -> str:
            state = recipe.to_form(nav, offer.url, mode="guest")
            if state != "needs_account":
                return state
            password = generate_password(*recipe.password_rule)
            conn.username = plan.username
            self.store.put(conn)
            self.credentials.save(conn, password)  # au coffre AVANT la frappe : jamais perdu, jamais montré
            drv.in_login = True
            try:
                recipe.create_account(nav, plan.username, lambda ref: drv.fill_secret(ref, password),
                                      lambda: self._ask_code(conn, recipe, since))
            finally:
                drv.in_login = False
            self.journal.append(plan.owner_account, {"deposit": f"acct-{plan.id}", "event": "account_created",
                                                     "ats": recipe.ats, "connector": conn.id, "host": conn.hosts})
            return "form"

        fresh = Plan(id=new_plan_id(), profile=plan.profile, owner_account=plan.owner_account, ats=plan.ats,
                     portal=plan.portal, offer_ref=plan.offer_ref, offer_id=plan.offer_id, company=plan.company,
                     title=plan.title, location=plan.location, url=plan.url, account="login",
                     username=plan.username, connector=conn.id, files=list(plan.files), created_at=self.clock())
        self.plans.drop(plan.owner_account, plan.id)
        return self._scan_form(conn, recipe, fresh, offer, pieces, answers, sheet, open_form=open_form)

    # ── 3. rejeu exact à l'approbation ──
    def submit(self, plan: Plan, pieces: dict[str, Piece]) -> Outcome:
        recipe = self.recipes[plan.ats]
        conn = self.store.get(plan.connector)
        if conn is None:
            raise DeskError("ref_unknown")
        for p in plan.files:
            got = pieces.get(p.role)
            if got is None or hashlib.sha256(got.data).hexdigest() != p.sha256:
                raise DeskError("stale", change=f"pièce modifiée depuis la carte ({p.role})")
        deposit = "dp_" + plan.id[3:]
        gov = self.gov(conn, recipe)
        try:
            gov.check(conn.id, "applies")
        except GovernorError as e:
            raise DeskError(e.code, label=conn.label, **e.extra) from None
        by_key = {(f.page, f.id): f for f in plan.fields}
        since = self.clock()
        state: dict[str, Any] = {"sent": False}

        def run(nav: PortalNav, drv: MaskedDriver) -> dict[str, Any]:
            st = recipe.to_form(nav, plan.url, mode=plan.account,
                                login=self._login_fn(conn, recipe, drv) if plan.account == "login" else None)
            if st == "closed":
                raise DeskError("offer_closed")
            if st == "bad_login":
                raise DeskError("site_suspended", label=conn.label)
            if st != "form":
                raise Stale("le portail demande un compte")
            seen_ids: set[tuple[int, str]] = set()

            def fill(page: int, seen: list[Seen]) -> list[Seen]:
                for f in seen:
                    fid = field_id(recipe, f)
                    seen_ids.add((page, fid))
                    pf = by_key.get((page, fid))
                    if pf is None:
                        if f.required:
                            raise Stale(f"nouveau champ obligatoire « {f.label} »")
                        continue
                    if f.kind == "choice" and pf.value and pf.value not in f.options:
                        raise Stale(f"option « {pf.value} » disparue de « {f.label} »")
                    if pf.value and pf.value != "false":
                        _apply_value(nav, recipe, f, pf, pieces)
                gone = [pf for (pg, _), pf in by_key.items() if pg == page and (pg, pf.id) not in seen_ids
                        and pf.required]
                if gone:
                    raise Stale(f"champ « {gone[0].label} » disparu")
                return []

            def verify(nodes: list[Node]) -> str | None:
                return _verify(nodes, plan, recipe, nav.uploaded)

            try:
                out = recipe.walk(nav, fill, submit=True, verify=verify)
            except Captcha:
                # Vérification au moment d'envoyer : la personne la fait sur l'écran du navigateur, jamais Nokker.
                token = self._handoff(conn, recipe)
                logger.info("jobsd : passation de l'écran (%s, jeton %s…)", conn.id, token[:4])
                out = self._wait_human(nav, drv, recipe)
            state["sent"] = bool(out.get("sent"))
            if out.get("stopped") is not None:
                raise Stale("champ obligatoire sans valeur")
            proof: dict[str, Any] = {}
            if out.get("confirmed"):
                shot = drv.screenshot()
                self.journal.save_capture(plan.owner_account, deposit, shot)
                proof = {"kind": "confirmation_page", "captured_at": self.clock(),
                         "sha256": hashlib.sha256(shot).hexdigest(), "reference": out.get("reference", "")}
            return {**out, "proof": proof}

        self.journal.append(plan.owner_account, {"deposit": deposit, "event": "started", "plan": plan.id,
                                                 "plan_sha256": plan.sha256(), "offer": plan.offer_ref,
                                                 "company": plan.company, "title": plan.title, "ats": plan.ats})
        try:
            out = self._session(conn, recipe, run)
        except Stale as e:
            self.journal.append(plan.owner_account, {"deposit": deposit, "event": "stale", "status": "stale"})
            raise DeskError("stale", change=e.change) from None
        except DeskError as e:
            status = "unconfirmed" if state["sent"] else "failed"
            self.journal.append(plan.owner_account, {"deposit": deposit, "event": e.code, "status": status})
            if state["sent"]:
                return Outcome("unconfirmed", deposit)
            raise
        except Exception:  # noqa: BLE001 — envoi peut-être parti : jamais rejoué, `unconfirmed`
            logger.exception("jobsd : dépôt %s interrompu", deposit)
            self.journal.append(plan.owner_account, {"deposit": deposit, "event": "error",
                                                     "status": "unconfirmed" if state["sent"] else "failed"})
            if state["sent"]:
                return Outcome("unconfirmed", deposit)
            raise DeskError("recipe_broken", portal=recipe.name, step="envoi") from None
        gov.record(conn.id, "applies")
        proof = out.get("proof") or {}
        if self.mail_ack is not None and proof:
            try:
                ack = self.mail_ack(conn, recipe.labels.get("mail_from", ""), since)
            except Exception:  # noqa: BLE001 — boîte injoignable : la page capturée suffit
                ack = None
            if ack:
                proof["ack_message_id"] = ack
        status = "sent" if proof else "unconfirmed"
        self.journal.append(plan.owner_account, {"deposit": deposit, "event": status, "status": status,
                                                 "proof": proof, "reference": out.get("reference", ""),
                                                 "sent_at": self.clock()})
        self.plans.drop(plan.owner_account, plan.id)
        return Outcome(status, deposit, out.get("reference", ""), proof)

    def _wait_human(self, nav: PortalNav, drv: MaskedDriver, recipe: PortalRecipe) -> dict[str, Any]:
        deadline = self.clock() + self.handoff_wait_s
        while self.clock() < deadline:
            self.sleep(self.handoff_poll_s)
            drv.refresh()
            nodes = nav.nodes()
            if recipe.captcha(nodes):
                continue
            ok, ref = recipe.confirmed(nodes)
            return {"sent": True, "confirmed": ok, "reference": ref}
        raise DeskError("site_needs_you", label=nav.conn.label)

    def mark_registered(self, account: str, deposit: str, application_id: int | None) -> None:
        # en attente : l'id de candidature déjà lié (`application_linked`) n'est pas effacé
        self.journal.append(account, {"deposit": deposit, "event": "registered" if application_id else
                                      "register_pending", "registered": bool(application_id),
                                      **({"application_id": application_id} if application_id else {})})

    def sent_for(self, account: str, offer_ref: str) -> dict[str, Any] | None:
        for d in self.journal.deposits(account).values():
            if d.get("offer") == offer_ref and d.get("status") in ("sent", "unconfirmed"):
                return d
        return None


def _answer(answers: dict[str, str], fid: str, label: str) -> str | None:
    if fid in answers and str(answers[fid]).strip():
        return str(answers[fid]).strip()
    n = norm(label)
    for k, v in answers.items():
        if norm(k) == n and str(v).strip():
            return str(v).strip()
    return None


def _truthy(v: str) -> bool:
    return str(v).strip().casefold() in ("true", "oui", "yes", "1", "ja", "si", "sì", "vrai", "coché")


def _qkind(f: Seen) -> str:
    return {"text": "texte", "choice": "choix", "check": "case", "file": "fichier"}[f.kind]


def _apply_value(nav: PortalNav, recipe: PortalRecipe, f: Seen, pf: PlanField, pieces: dict[str, Piece]) -> None:
    if f.disabled:
        raise PortalBroken(f"champ inactif « {f.label} »")
    if f.kind == "text":
        if f.value != pf.value:
            if f.value:
                raise PortalBroken(f"champ déjà rempli « {f.label} »")
            nav.type(f.ref, pf.value)
    elif f.kind == "choice":
        if f.value != pf.value:
            nav.select(f.ref, pf.value)
    elif f.kind == "check":
        want = pf.value == "true"
        if (f.value == "true") != want:
            nav.check(f.ref, want)
    elif f.kind == "file":
        piece = pieces.get(pf.value)
        if piece is not None:
            nav.upload(f.ref, piece.part.name, piece.data)
            nav.uploaded[norm(f.label)] = piece.part.name


def _verify(nodes: list[Node], plan: Plan, recipe: PortalRecipe, uploaded: dict[str, str] | None = None
            ) -> str | None:
    """Relecture avant « Envoyer » : chaque valeur et chaque nom de fichier du plan est à l'écran."""
    page_text = "\n".join(f"{n.name} {n.value}" for n in nodes)
    files = {p.role: p.name for p in plan.files}
    review = any(n.attrs.get("role_hint") == "review" for n in nodes) or recipe.ats == "workday"
    by_label = {norm(re.sub(r"\s*\*\s*$", "", n.name)): n for n in nodes}
    for f in plan.fields:
        if not f.value or f.value == "false":
            continue
        shown = files.get(f.value, "") if f.kind == "file" else ("oui" if f.kind == "check" else f.value)
        if review:
            if f"{f.label} : {shown}" not in page_text:
                return f"« {f.label} »"
            continue
        if f.kind == "file":  # nom affiché sous le champ (§6.3), ou relu sur le champ à la pose, juste avant l'envoi
            if shown not in page_text and (uploaded or {}).get(norm(f.label)) != shown:
                return f"« {f.label} »"
            continue
        n = by_label.get(norm(f.label))
        if n is None:
            return f"« {f.label} »"
        if f.kind == "check":
            if n.attrs.get("checked") != "true":
                return f"« {f.label} »"
        elif (n.value or "").strip() != shown:
            return f"« {f.label} »"
    return None


__all__ = ["DeskError", "JobsDesk", "Offer", "Outcome", "Piece", "PortalNav", "ScanResult", "field_id",
           "generate_password"]
