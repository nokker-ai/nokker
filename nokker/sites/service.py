"""Sites connectés — `sitesd` : connecteurs, flux de connexion, gestes de lecture (docs/CONNECTED-SITES.md §3-§7).

Le service est le seul détenteur des profils et le seul à toucher le coffre des sites. Le noyau l'appelle par la
source d'outils (`tools.py`) ; l'écran par les routes (`http.py`). Rien de ce module ne rend un mot de passe, un code,
un lien magique ni un cookie.
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from nokker import bg_stop
from nokker.sites import recipes as _recipes
from nokker.sites.credentials import REASON_EXPIRED, REASON_FIRST, Credentials, CredentialsError
from nokker.sites.driver import Driver, MaskedDriver
from nokker.sites.governor import LINKEDIN_LIMITS, Governor, GovernorError
from nokker.sites.model import SIGNAL_TEXT, Connector, State
from nokker.sites.profiles import ProfileStore
from nokker.sites.recipes.base import (ASK_APP, ASK_CODE, ASK_PASSWORD, ASK_USERNAME, BAD_PASSWORD, CAPTCHA, LINK_SENT,
                                       LOGGED_IN, UNKNOWN, UNUSUAL, Item, RecipeBroken, SocialRecipe)
from nokker.sites.requests import Requests
from nokker.sites.store import SitesStore

logger = logging.getLogger("nokker.sites")
CONSENT_VERSION = "2026-11-05"


class SiteError(RuntimeError):
    """Erreur d'un geste ; `code` est celui du §8.6, `extra` remplit le texte."""

    def __init__(self, code: str, **extra: Any) -> None:
        super().__init__(code)
        self.code = code
        self.extra = extra


class _Expired(Exception):
    pass


class _Cancelled(Exception):
    pass


class _Signal(Exception):
    def __init__(self, what: str) -> None:
        super().__init__(what)
        self.what = what


def host_ok(url: str, hosts: tuple[str, ...] | list[str]) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and any(host == h or host.endswith("." + h) for h in hosts)


class Nav:
    """Ce que voit une recette : navigation bornée aux hôtes, au rythme du gouverneur, signaux détectés à chaque page."""

    def __init__(self, conn: Connector, drv: MaskedDriver, recipe: SocialRecipe, gov: Governor, login: bool = False):
        self.conn, self.drv, self.recipe, self.gov, self.login = conn, drv, recipe, gov, login

    def goto(self, url: str, secret: bool = False) -> None:
        if not host_ok(url, self.recipe.hosts):
            raise SiteError("url_hors_site", url=url)
        self.gov.page(self.conn.id)
        (self.drv.goto_secret if secret else self.drv.goto)(url)
        if not self.login:
            what = self.recipe.detect(self.drv.nodes(), self.drv.url())
            if what in (UNUSUAL, CAPTCHA):
                raise _Signal(what)
            if what in (ASK_USERNAME, ASK_PASSWORD):
                raise _Expired()

    def nodes(self):
        return self.drv.nodes()

    def text(self) -> str:
        return self.drv.text()

    def url(self) -> str:
        return self.drv.url()

    def type(self, ref: str, text: str) -> None:
        self.drv.type(ref, text)

    def click(self, ref: str) -> None:
        self.drv.click(ref)


@dataclass
class Flow:
    status: str
    request_id: str | None = None
    reason: str = ""


class SitesService:
    def __init__(self, *, store: SitesStore, credentials: Credentials, requests: Requests, profiles: ProfileStore,
                 driver_factory: Callable[[Connector, bytes | None], Driver],
                 recipes: dict[str, SocialRecipe] | None = None,
                 governor_factory: Callable[[SocialRecipe], Governor] | None = None,
                 mail_links: Callable[[Connector, str, float], str | None] | None = None,
                 notify: Callable[[Connector, str, dict], None] | None = None,
                 secure_link: Callable[[str], str] | None = None, origin: str = "https://app.nokker.ai",
                 screen_url: Callable[[Connector], str] | None = None, where: Callable[[str], str] | None = None,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
                 handoff_wait_s: float = 900.0, handoff_poll_s: float = 5.0, relogin_wait_s: float = 25.0,
                 write_enabled: bool = False) -> None:
        self.store, self.credentials, self.requests, self.profiles = store, credentials, requests, profiles
        self.driver_factory = driver_factory
        self.recipes = recipes if recipes is not None else dict(_recipes.RECIPES)
        self._gov_factory = governor_factory or (lambda r: Governor(limits=dict(LINKEDIN_LIMITS), clock=clock))
        self._govs: dict[str, Governor] = {}
        self.mail_links = mail_links  # (connecteur, expéditeur, depuis) -> lien (boîte 068), jamais montré au modèle
        self.notify = notify  # (connecteur, événement, données) : Telegram, écran
        self.origin = origin.rstrip("/")
        # lien de saisie sécurisée : https://<origine>/sites/r/<rid> (§8.6) ; page servie par le web
        self.secure_link = secure_link or (lambda rid: f"{self.origin}/sites/r/{rid}")
        # écran du navigateur d'un connecteur (noVNC de sites-chrome), ouvert par un lien de passation à usage unique
        self.screen_url = screen_url or (lambda conn: f"{self.origin}/sites/screen/{conn.id}")
        self.where = where or (lambda account: "dans l'application Nokker")
        self._cancelled: set[str] = set()
        self._answer_here: dict[str, float] = {}
        self._handoffs: dict[str, tuple[str, float]] = {}  # jeton -> (connecteur, échéance) ; usage unique
        self._handoff_of: dict[str, str] = {}  # connecteur -> jeton courant
        self.clock, self.sleep = clock, sleep
        self.handoff_wait_s, self.handoff_poll_s, self.relogin_wait_s = handoff_wait_s, handoff_poll_s, relogin_wait_s
        self.write_enabled = write_enabled  # `sites.write` : éteint (lot L2)
        self._flows: dict[str, threading.Thread] = {}
        bg_stop.track(self)  # arrêt en test seulement (`bg_stop.stop_all`)
        self._flow_state: dict[str, Flow] = {}
        self._listeners: dict[str, list[Callable[[dict], None]]] = {}
        self._refs: dict[str, dict[str, tuple[str, str]]] = {}
        # Magasins d'autres connecteurs servis par ce service (portails emploi de `jobsd`, pack Emploi §6.2) : le lien
        # de passation à usage unique les retrouve aussi.
        self.extra_stores: list[Any] = []
        self._lock = threading.Lock()

    # ── catalogue, accès ──
    def recipe(self, conn: Connector) -> SocialRecipe:
        r = self.recipes.get(conn.site)
        if r is None:
            raise SiteError("unsupported", label=conn.label)
        return r

    def gov(self, conn: Connector) -> Governor:
        with self._lock:
            if conn.id not in self._govs:
                self._govs[conn.id] = self._gov_factory(self.recipe(conn))
            return self._govs[conn.id]

    def catalog(self) -> list[dict[str, Any]]:
        return [{"site": r.site, "name": r.name, "recipe": True, "gestures": r.gestures_line()}
                for r in self.recipes.values()]

    def find_site(self, text: str) -> SocialRecipe | None:
        t = str(text or "").strip().casefold()
        for r in self.recipes.values():
            if t in (r.site, r.name.casefold()) or any(t == h or t.endswith("." + h) for h in r.hosts):
                return r
        if t.startswith("https://"):
            for r in self.recipes.values():
                if host_ok(t, r.hosts):
                    return r
        return None

    def access(self, account: str, conn: Connector) -> str | None:
        """`owner`, `send`, `read` ou None (§5.4 ; un mineur n'a pas de site : refusé à la frontière)."""
        if conn.account == account:
            return "owner"
        return conn.grants.get(account)

    def visible(self, account: str) -> list[tuple[Connector, str]]:
        out = []
        for c in self.store.all():
            a = self.access(account, c)
            if a:
                out.append((c, a))
        out.sort(key=lambda ca: (ca[1] != "owner", not ca[0].default, ca[0].created_at))
        return out

    def resolve(self, account: str, site: str | None) -> tuple[Connector, str]:
        vis = self.visible(account)
        if not vis:
            raise SiteError("site_unknown", given=site or "", valid=[])
        if not site:
            if len(vis) == 1:
                return vis[0]
            raise SiteError("argument_invalide", case="site_required", labels=[c.label for c, _ in vis])
        key = str(site).strip()
        by_id = [ca for ca in vis if ca[0].id == key]
        if by_id:
            return by_id[0]
        hits = [ca for ca in vis if ca[0].label.casefold() == key.casefold()]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise SiteError("site_ambiguous", given=key, hits=hits)
        raise SiteError("site_unknown", given=key, valid=[c for c, _ in vis])

    # ── événements (écran, SSE) ──
    def subscribe(self, cid: str, cb: Callable[[dict], None]) -> Callable[[], None]:
        self._listeners.setdefault(cid, []).append(cb)
        return lambda: self._listeners.get(cid, []).remove(cb)

    def _set_state(self, conn: Connector, state: State, signal: str = "", request: Any = None) -> Connector:
        conn = self.store.get(conn.id) or conn
        conn.state, conn.signal = state, signal
        self.store.put(conn)
        ev = {"id": conn.id, "state": state.value, "signal": conn.signal,
              "state_text": conn.state_text(self.where(conn.account))}
        if request is not None:
            ev["request"] = {"id": request.id, "kind": request.kind, "expires_at": request.expires_at}
        for cb in list(self._listeners.get(conn.id, [])):
            try:
                cb(ev)
            except Exception:  # noqa: BLE001
                logger.warning("sites : écouteur en échec", exc_info=True)
        return conn

    def _notify(self, conn: Connector, event: str, data: dict) -> None:
        if self.notify is not None:
            try:
                self.notify(conn, event, data)
            except Exception:  # noqa: BLE001
                logger.warning("sites : notification en échec (%s)", event, exc_info=True)

    # ── ajout, connexion ──
    def add(self, account: str, site: str, username: str, label: str | None = None) -> Connector:
        recipe = self.find_site(site)
        if recipe is None:
            raise SiteError("unsupported", label=site)
        return self.store.add(account=account, site=recipe.site, site_name=recipe.name, username=username,
                              label=label, hosts=list(recipe.hosts))

    def connect(self, conn: Connector, *, reason: str = REASON_FIRST, wait_s: float = 3.0) -> Flow:
        """Lance (ou suit) le flux de connexion ; attend au plus `wait_s` qu'il quitte « Connexion… »."""
        with self._lock:
            t = self._flows.get(conn.id)
            if t is None or not t.is_alive():
                self._cancelled.discard(conn.id)
                self._flow_state[conn.id] = Flow(status=State.CONNECTING.value)
                t = threading.Thread(target=self._flow, args=(conn.id, reason), name=f"sites-flow-{conn.id}",
                                     daemon=True)
                self._flows[conn.id] = t
                t.start()
        deadline = time.monotonic() + wait_s  # attente réelle (l'horloge du service peut être simulée)
        while time.monotonic() < deadline and t.is_alive():
            if self._flow_state[conn.id].status != State.CONNECTING.value:
                break
            time.sleep(0.02)
        return self._flow_state[conn.id]

    def wait_flow(self, cid: str, timeout_s: float) -> Flow | None:
        t = self._flows.get(cid)
        if t is not None:
            t.join(timeout_s)
        return self._flow_state.get(cid)

    def _flow_set(self, conn: Connector, state: State, request: Any = None, reason: str = "") -> Connector:
        self._flow_state[conn.id] = Flow(status=state.value, request_id=request.id if request else None,
                                         reason=reason)
        return self._set_state(conn, state, reason, request)

    def _ask(self, conn: Connector, kind: str, state: State) -> str | None:
        req = self.requests.open(conn.id, conn.account, conn.label, kind)
        self._flow_set(conn, state, req)
        payload = {"kind": kind, "request_id": req.id, "link": self.secure_link(req.id)}
        here = self._answer_here.pop(conn.id, None)
        if kind == "password" and here is not None and time.monotonic() < here:
            # mot de passe saisi à l'écran (`answer_here`) : la demande Telegram ne part qu'après 30 s sans réponse
            def later() -> None:
                if not req.answered and not req.cancelled:
                    self._notify(conn, "request", payload)

            timer = threading.Timer(30.0, later)
            timer.daemon = True
            timer.start()
        else:
            self._notify(conn, "request", payload)
        value = self.requests.wait(req.id)
        if conn.id in self._cancelled:
            raise _Cancelled()
        return value

    def answer_here(self, conn: Connector) -> None:
        """L'écran saisit lui-même le prochain mot de passe : la demande Telegram attend 30 s (§15)."""
        self._answer_here[conn.id] = time.monotonic() + 60.0

    def close(self, timeout_s: float = 2.0) -> None:
        """Arrête les flux en cours (annulation de leur demande ouverte), puis les attend ; idempotent."""
        with self._lock:
            live = [(cid, t) for cid, t in self._flows.items() if t.is_alive()]
        for cid, _t in live:
            self._cancelled.add(cid)
            req = self.requests.open_for(cid)
            if req is not None:
                self.requests.cancel(req.id)
        for _cid, t in live:
            t.join(timeout_s)

    def cancel(self, conn: Connector) -> bool:
        """Annuler une connexion en cours (§15) : la demande ouverte tombe, le flux s'arrête, rien n'est rejoué."""
        t = self._flows.get(conn.id)
        if t is None or not t.is_alive():
            return False
        self._cancelled.add(conn.id)
        req = self.requests.open_for(conn.id)
        if req is not None:
            self.requests.cancel(req.id)
        return True

    def handoff_url(self, conn: Connector) -> str | None:
        token = self._handoff_of.get(conn.id)
        hit = self._handoffs.get(token or "")
        if hit is None or time.monotonic() > hit[1]:
            return None
        return f"{self.origin}/sites/h/{token}"

    def open_handoff(self, token: str, account: str) -> str | None:
        """Lien de passation : propriétaire seulement, usage unique, 15 min ; rend l'URL de l'écran."""
        hit = self._handoffs.get(token)
        if hit is None or time.monotonic() > hit[1]:
            return None
        conn = self.store.get(hit[0]) or next((c for c in (s.get(hit[0]) for s in self.extra_stores) if c), None)
        if conn is None or conn.account != account:
            return None
        self._handoffs.pop(token, None)
        self._handoff_of.pop(conn.id, None)
        return self.screen_url(conn)

    def _new_handoff(self, conn: Connector) -> str:
        import secrets as _s

        old = self._handoff_of.pop(conn.id, None)
        if old:
            self._handoffs.pop(old, None)
        token = _s.token_urlsafe(24)
        self._handoffs[token] = (conn.id, time.monotonic() + 900.0)
        self._handoff_of[conn.id] = token
        return token

    def _flow(self, cid: str, reason: str) -> None:
        conn = self.store.get(cid)
        if conn is None:
            return
        recipe = self.recipe(conn)
        gov = self.gov(conn)
        started = self.clock()
        try:
            with gov.driver(cid):
                try:
                    raw = self.driver_factory(conn, self.profiles.load(conn.account, cid))
                except RuntimeError:
                    self._flow_set(conn, State.FAILED, reason="browser_missing")
                    return
                drv = MaskedDriver(raw)
                drv.in_login = True
                nav = Nav(conn, drv, recipe, gov, login=True)
                typed_password_from_request: str | None = None
                codes_tried = 0
                try:
                    self._flow_set(conn, State.CONNECTING)
                    nav.goto(recipe.login_url)
                    for _ in range(16):
                        nodes = drv.nodes()
                        what = recipe.detect(nodes, drv.url())
                        if what == LOGGED_IN:
                            if typed_password_from_request is not None:
                                self.credentials.save(conn, typed_password_from_request)
                            conn = self.store.get(cid) or conn
                            conn.account_name = recipe.account_name(nodes)
                            conn.last_ok_at = self.clock()
                            if reason == REASON_EXPIRED:
                                conn.last_auto_login_at = self.clock()
                            self.store.put(conn)
                            drv.in_login = False
                            self.profiles.save(conn.account, cid, drv.export_profile())
                            self._flow_set(conn, State.CONNECTED)
                            return
                        if what == ASK_USERNAME:
                            recipe.type_username(nav, conn.username)
                        elif what == ASK_PASSWORD:
                            if self.credentials.has(conn) and typed_password_from_request is None:
                                self.credentials.use_password(conn, reason, lambda v: recipe.fill_password(
                                    nav, lambda ref, v=v: drv.fill_secret(ref, v)) and None)
                            else:
                                value = self._ask(conn, "password", State.NEEDS_PASSWORD)
                                if value is None:
                                    self._flow_set(conn, State.FAILED, reason="password_timeout")
                                    return
                                typed_password_from_request = value
                                self.credentials.remember(f"sites.{cid}.password", value)
                                self._flow_set(conn, State.CONNECTING)
                                recipe.fill_password(nav, lambda ref: drv.fill_secret(ref, value))
                        elif what == BAD_PASSWORD:
                            self._flow_set(conn, State.SUSPENDED, reason="bad_credentials")
                            self._notify(conn, "suspended", {})
                            return
                        elif what == ASK_CODE:
                            # Code faux : le site garde l'écran du code ; une nouvelle demande (une seule), puis abandon.
                            if codes_tried >= 2:
                                self._flow_set(conn, State.FAILED, reason="code_rejected")
                                return
                            if codes_tried == 1:
                                self._notify(conn, "wrong_code", {})
                            codes_tried += 1
                            code = self._ask(conn, "code", State.NEEDS_CODE)
                            if code is None:
                                self._flow_set(conn, State.FAILED, reason="code_timeout")
                                return
                            self._flow_set(conn, State.CONNECTING)
                            recipe.fill_code(nav, lambda ref: drv.fill_secret(ref, code))
                        elif what == ASK_APP:
                            done = self._ask(conn, "app_approval", State.NEEDS_APP_APPROVAL)
                            if done is None:
                                self._flow_set(conn, State.FAILED, reason="approval_timeout")
                                return
                            self._flow_set(conn, State.CONNECTING)
                            drv.refresh()
                        elif what == LINK_SENT:
                            # Le flux ne rend « needs_link » qu'avec sa demande (posés ensemble par `_ask`) : sinon
                            # sites_connect transmettrait « une demande est partie … : . » sans lien. Pendant la lecture
                            # de la boîte reliée, l'écran voit l'état ; le flux reste « Connexion… ».
                            link = None
                            if self.mail_links is not None:
                                self._set_state(conn, State.NEEDS_LINK)
                                try:
                                    link = self.mail_links(conn, recipe.labels.get("mail_from", ""), started)
                                except Exception:  # noqa: BLE001 — boîte injoignable : on demande le lien
                                    logger.warning("sites : lien magique introuvable dans la boîte", exc_info=False)
                            if not link:
                                link = self._ask(conn, "link", State.NEEDS_LINK)
                            if not link:
                                self._flow_set(conn, State.FAILED, reason="link_timeout")
                                return
                            if not host_ok(link, recipe.hosts):
                                self._flow_set(conn, State.FAILED, reason="link_other_site")
                                return
                            self._flow_set(conn, State.CONNECTING)
                            nav.goto(link, secret=True)
                        elif what == UNUSUAL:
                            self._pause(conn, "unusual_activity")
                            return
                        else:  # CAPTCHA, écran inconnu : passation VNC par Telegram
                            # Le lien de passation EXISTE avant que l'état `needs_you` soit visible : un client qui lit la
                            # carte à cet instant reçoit son `handoff_url` (sinon il manquait, test instable du 07.11).
                            token = self._new_handoff(conn)
                            self._flow_set(conn, State.NEEDS_YOU, reason="captcha" if what == CAPTCHA else
                                           "unknown_screen")
                            self._notify(conn, "handoff", {"why": what, "handoff_url": f"{self.origin}/sites/h/{token}"})
                            deadline = self.clock() + self.handoff_wait_s
                            while self.clock() < deadline:
                                if cid in self._cancelled:
                                    raise _Cancelled()
                                self.sleep(self.handoff_poll_s)
                                drv.refresh()
                                if recipe.detect(drv.nodes(), drv.url()) not in (CAPTCHA, UNKNOWN):
                                    break
                            else:
                                self._flow_set(conn, State.FAILED, reason="handoff_timeout")
                                return
                            self._flow_set(conn, State.CONNECTING)
                    self._flow_set(conn, State.FAILED, reason="too_many_steps")
                except _Cancelled:
                    self._flow_set(conn, State.FAILED, reason="cancelled")
                except RecipeBroken:
                    self._flow_set(conn, State.FAILED, reason="login_page_changed")
                finally:
                    drv.close()
        except GovernorError as e:
            self._flow_set(conn, State.FAILED, reason=e.code if e.code in SIGNAL_TEXT else "internal")
        except CredentialsError as e:
            self._flow_set(conn, State.FAILED, reason=e.code if e.code in SIGNAL_TEXT else "internal")
        except Exception:  # noqa: BLE001 — jamais de détail (une valeur pourrait être dans l'exception)
            logger.error("sites : flux de connexion en échec (%s)", cid)
            self._flow_set(conn, State.FAILED, reason="internal")

    def _pause(self, conn: Connector, signal: str) -> None:
        until = self.gov(conn).signal(conn.id, signal)
        conn = self.store.get(conn.id) or conn
        conn.paused_until = until if until != float("inf") else 4102444800.0  # levée manuelle
        self.store.put(conn)
        self._flow_set(conn, State.PAUSED, reason=signal)
        self._notify(conn, "paused", {"signal": signal})

    def lift_pause(self, conn: Connector, wait_s: float = 0.0) -> Connector:
        """Levée d'une pause par l'utilisateur (§15 ; jamais le modèle). L'état revient au dernier état utile : une page
        d'accueil du site est ouverte (une seule, au rythme du gouverneur) ; session valide → `connected`, sinon ce que
        la page montre (connexion demandée → `expired`, nouveau signal → nouvelle pause, écran inconnu → `expired`)."""
        self.gov(conn).lift_pause(conn.id)
        conn = self.store.get(conn.id) or conn
        conn.paused_until = None
        self.store.put(conn)
        if conn.state != State.PAUSED:
            return conn
        with self._lock:
            t = self._flows.get(conn.id)
            if t is None or not t.is_alive():
                t = threading.Thread(target=self._after_pause, args=(conn.id,), name=f"sites-unpause-{conn.id}",
                                     daemon=True)
                self._flows[conn.id] = t
                t.start()
        if wait_s:
            t.join(wait_s)
        return self.store.get(conn.id) or conn

    def _after_pause(self, cid: str) -> None:
        conn = self.store.get(cid)
        if conn is None:
            return
        recipe = self.recipe(conn)
        gov = self.gov(conn)
        try:
            with gov.driver(cid):
                drv = MaskedDriver(self.driver_factory(conn, self.profiles.load(conn.account, cid)))
                try:
                    Nav(conn, drv, recipe, gov, login=True).goto(recipe.home_url)
                    what = recipe.detect(drv.nodes(), drv.url())
                    if what == LOGGED_IN:
                        self.profiles.save(conn.account, cid, drv.export_profile())
                        self._set_state(conn, State.CONNECTED)
                    elif what in (UNUSUAL, CAPTCHA):
                        self._pause(conn, "unusual_activity" if what == UNUSUAL else "captcha")
                    else:  # connexion demandée, écran inconnu : la prochaine lecture reconnecte (ou passe la main)
                        self._set_state(conn, State.EXPIRED)
                finally:
                    drv.close()
        except Exception:  # noqa: BLE001 — navigateur absent, quota, occupé : pas de détail, une lecture tranchera
            logger.warning("sites : vérification après pause en échec (%s)", cid)
            self._set_state(conn, State.EXPIRED)

    # ── gestes de lecture ──
    def _ref(self, conn: Connector, item: Item) -> str | None:
        if not item.url:
            return None
        token = hashlib.sha256(item.url.encode("utf-8")).hexdigest()[:12]
        ref = f"{conn.id}|{item.ref_kind}:{token}"
        self._refs.setdefault(conn.id, {})[ref] = (item.ref_kind, item.url)
        return ref

    def ref_target(self, ref: str) -> tuple[Connector, str, str]:
        cid = ref.split("|", 1)[0]
        target = self._refs.get(cid, {}).get(ref)
        conn = self.store.get(cid)
        if target is None or conn is None:
            raise SiteError("ref_unknown")
        return conn, target[0], target[1]

    def _check_usable(self, conn: Connector) -> None:
        conn = self.store.get(conn.id) or conn
        if conn.paused_until and self.clock() < conn.paused_until:
            raise SiteError("site_paused", label=conn.label, until=conn.paused_until, signal=conn.signal)
        if conn.state == State.SUSPENDED:
            raise SiteError("site_suspended", label=conn.label)
        if conn.state == State.NEEDS_YOU:
            raise SiteError("site_needs_you", label=conn.label)
        if conn.state in (State.ABSENT, State.FAILED, State.DISCONNECTED) and conn.last_ok_at is None:
            raise SiteError("site_reconnect", label=conn.label)

    def run(self, conn: Connector, gesture: Callable[[Nav], Any], *, counter: str | None = None,
            live: bool = True, _retry: bool = True) -> Any:
        """Un geste de lecture : pause, heures, quota, un pilote ; expiration → reconnexion automatique (une fois)."""
        self._check_usable(conn)
        gov = self.gov(conn)
        try:
            gov.check_hours(live)
            if counter:
                gov.check(conn.id, counter)
        except GovernorError as e:
            raise SiteError(e.code, label=conn.label, **e.extra) from None
        try:
            with gov.driver(conn.id):
                drv = MaskedDriver(self.driver_factory(conn, self.profiles.load(conn.account, conn.id)))
                try:
                    out = gesture(Nav(conn, drv, self.recipe(conn), gov))
                    if counter:
                        gov.record(conn.id, counter)
                    self.profiles.save(conn.account, conn.id, drv.export_profile())
                    return out
                finally:
                    drv.close()
        except GovernorError as e:
            raise SiteError(e.code, label=conn.label, **e.extra) from None
        except RecipeBroken as e:
            raise SiteError("recipe_broken", label=conn.label, gesture=e.gesture, step=e.step) from None
        except _Signal as s:
            self._pause(conn, "unusual_activity" if s.what == UNUSUAL else "captcha")
            conn = self.store.get(conn.id) or conn
            raise SiteError("site_paused", label=conn.label, until=conn.paused_until, signal=conn.signal) from None
        except _Expired:
            self._set_state(conn, State.EXPIRED)
            if not _retry:
                raise SiteError("site_reconnect", label=conn.label) from None
            try:
                gov.check(conn.id, "logins")
            except GovernorError:
                self._set_state(conn, State.SUSPENDED, "too_many_logins")
                raise SiteError("site_suspended", label=conn.label) from None
            gov.record(conn.id, "logins")
            if not self.credentials.has(conn):
                self.connect(conn, reason=REASON_FIRST, wait_s=0.1)  # demande de mot de passe sur Telegram
                raise SiteError("site_reconnect", label=conn.label) from None
            self.connect(conn, reason=REASON_EXPIRED, wait_s=0.1)
            flow = self.wait_flow(conn.id, self.relogin_wait_s)
            if flow is None or flow.status != State.CONNECTED.value:
                raise SiteError("site_reconnect", label=conn.label) from None
            return self.run(conn, gesture, counter=counter, live=live, _retry=False)

    def list_items(self, conn: Connector, kind: str, max_items: int, live: bool = True) -> list[dict]:
        recipe = self.recipe(conn)
        items = self.run(conn, lambda nav: recipe.list(nav, kind, max_items), live=live)
        return [self._item(conn, i) for i in items]

    def read(self, conn: Connector, *, ref: str | None = None, url: str | None = None, live: bool = True) -> dict:
        recipe = self.recipe(conn)
        if ref is not None:
            _, kind, target = self.ref_target(ref)
        else:
            if not host_ok(url or "", recipe.hosts):
                raise SiteError("url_hors_site", url=url, label=conn.label, hosts=recipe.hosts)
            kind, target = "page", url
        counter = "profiles" if kind == "in" else None
        what, items = self.run(conn, lambda nav: recipe.read(nav, kind, target), counter=counter, live=live)
        return {"what": what, "items": [self._item(conn, i) for i in items]}

    def search(self, conn: Connector, kind: str, query: str, max_items: int, live: bool = True) -> list[dict]:
        recipe = self.recipe(conn)
        items = self.run(conn, lambda nav: recipe.search(nav, kind, query, max_items), counter="searches", live=live)
        return [self._item(conn, i) for i in items]

    def _item(self, conn: Connector, item: Item) -> dict:
        return {"ref": self._ref(conn, item), "kind": item.ref_kind, "fields": item.fields, "meta": item.meta}

    # ── gestion (écran) ──
    def replace_password(self, conn: Connector) -> Any:
        """Remplacer le mot de passe (écran) : demande `password` ; à la réponse, coffre puis nouvelle connexion."""
        req = self.requests.open(conn.id, conn.account, conn.label, "password")

        def waiter() -> None:
            value = self.requests.wait(req.id)
            if value is None:
                return
            self.credentials.save(conn, value)
            fresh = self.store.get(conn.id)
            if fresh is not None:
                fresh.state, fresh.signal = State.ABSENT, ""
                self.store.put(fresh)
                self.connect(fresh, reason=REASON_FIRST, wait_s=0.0)

        threading.Thread(target=waiter, name=f"sites-pw-{conn.id}", daemon=True).start()
        return req

    def delete(self, conn: Connector, forget_credentials: bool = True) -> None:
        self.profiles.delete(conn.account, conn.id)
        if forget_credentials:
            self.credentials.delete(conn)
        self.store.delete(conn.id)


__all__ = ["CONSENT_VERSION", "Flow", "Nav", "SiteError", "SitesService", "host_ok"]
