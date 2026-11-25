"""Vue navigateur en direct (lot V3 « Agent qui montre », contrat docs/BROWSER-LIVE.md).

Le noyau suit chaque session navigateur d'un fil (pool de la bêta, `tools/browser_pool.py`) et publie sur le flux
`/v1/events` du titulaire (même bus, même writer `id:`/`event:` que les autres événements) :

- `browser.action` après chaque geste — JAMAIS le texte saisi (`type` : « texte saisi (n car.) ») ;
- `browser.frame` à chaque changement de page et après chaque action, au plus une par seconde ; capture gardée EN
  MÉMOIRE (anneau des 20 dernières, `frames.FrameStore`), URL signée 60 s ; une frame SENSIBLE n'est jamais capturée
  (aucun pixel gardé, pas de `frame_url`) ;
- `browser.handoff` sur CAPTCHA (toujours), anti-bot, connexion ou 2FA ; l'agent ne résout jamais un CAPTCHA : tant que
  l'humain n'a pas rendu la main (`resume`), le noyau refuse les gestes ;
- `browser.resumed`, puis `browser.ended` — émis UNE fois, TOUJOURS (fin du tour, erreur, inactivité, handoff expiré).

Une bulle `kind: "browser"` est posée dans le fil au démarrage et mise à jour aux transitions seulement.

Sensibilité (décidée par le noyau) : le signal `sensitive` du navigateur (`_meta.nokker_action.sensitive` : champ
password, `cc-*`, `one-time-code` focalisé ou non vide), OU, côté noyau, une saisie dans un champ dont le libellé est
sensible (carte, mot de passe, code à usage unique) ou dont la valeur vient du coffre (poignée `[secret:…]`, marqueur
de l'appareil, motif de secret). La sensibilité du noyau tient jusqu'à une navigation ou jusqu'à ce que ces champs
soient vidés (saisie vide sur la même référence).

Tout est en mémoire, rien en base : un redémarrage du noyau termine les sessions (le client relit l'état par
`GET /v1/browser/sessions/{id}` → 404).
"""
from __future__ import annotations

import logging
import re
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from nokker.browserlive.frames import FrameStore
from nokker.core import model_texts as mt
from nokker.core.refusal import REFUSED_PREFIX, refused_prefix
from nokker.qa.untrusted import inline

logger = logging.getLogger("nokker.browserlive")

ACTIONS_KEPT = 20
FRAME_MIN_INTERVAL_S = 1.0
IDLE_TIMEOUT_S = 300.0  # = inactivité du pool (docs/BROWSER-POOL.md)
HANDOFF_TTL_S = 600.0
TAKEOVER_COOKIE_TTL_S = 900.0
ENDED_RETENTION_S = 900.0
TARGET_MAX = 80
TITLE_MAX = 120
TASK_MAX = 120
COOKIE_NAME = "nk_browser_takeover"

# Outil MCP → genre d'action du contrat. Absent = pas d'événement d'action (browser_close, browser_handoff, état…).
KIND: dict[str, str] = {
    "browser_navigate": "navigate", "browser_navigate_back": "navigate", "browser_tabs": "navigate",
    "browser_click": "click", "browser_hover": "click", "browser_select_option": "click", "browser_drag": "click",
    "browser_handle_dialog": "click",
    "browser_type": "type", "browser_fill": "type", "browser_fill_form": "type", "browser_press_key": "type",
    "browser_file_upload": "type",
    "browser_scroll": "scroll",
    "browser_snapshot": "read", "browser_find": "read", "browser_read_text": "read", "browser_take_screenshot": "read",
    "browser_console_messages": "read", "browser_network_requests": "read",
    "browser_wait_for": "wait",
}
PAGE_CHANGE = frozenset({"browser_navigate", "browser_navigate_back", "browser_tabs"})
# Pendant une passation, AUCUN outil navigateur ne passe, lectures comprises : un instantané peut porter la valeur d'un
# champ pendant que la personne y tape son code ou son mot de passe (relevé par le front, 07.11).
ALLOWED_DURING_HANDOFF: frozenset[str] = frozenset()
BLOCK_REASON = {"captcha": "captcha", "antibot": "challenge", "challenge": "challenge", "login": "login",
                "2fa": "2fa", "mfa": "2fa", "otp": "2fa"}
_CHALLENGE_TEXT = re.compile(r"^\s*(?:\S+\s+)?(CHALLENGE|BLOCAGE)\b", re.MULTILINE)
_CAPTCHA_WORD = re.compile(r"captcha|recaptcha|hcaptcha|turnstile|datadome", re.IGNORECASE)
_TITLE_LINE = re.compile(r"^\s*-?\s*Page Title:\s*(.+)$", re.MULTILINE)
_URL_LINE = re.compile(r"^\s*-?\s*Page URL:\s*(\S+)", re.MULTILINE)
_PASSWORD_TERMS = re.compile(
    r"\b(pass\s?words?|mots? de passe|passwort|kennwort|parola d'ordine|passphrase|passcode|pin|"
    r"one[- ]time[- ]code|otp|2fa|mfa|totp|code (?:sms|de v[ée]rification|re[çc]u)|verification code|sms code|"
    r"bestätigungscode|codice di verifica|current-password|new-password)\b", re.IGNORECASE)

HANDOFF_NOTE = mt.HANDOFF_NOTE["fr"]  # figés ; langue du tour : `model_texts`
HANDOFF_REFUSED = REFUSED_PREFIX + mt.HANDOFF_REFUSED["fr"]


def _handoff_refused(reason: str) -> str:
    return refused_prefix() + mt.pick(mt.HANDOFF_REFUSED).format(reason=reason)


def iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def url_host(url: str | None) -> str:
    if not url:
        return ""
    try:
        host = urlsplit(url if "://" in url else "https://" + url).hostname or ""
    except ValueError:
        return ""
    return inline(host, 253)


def sensitive_label(label: str | None) -> bool:
    """Libellé de champ sensible : carte bancaire, mot de passe, code à usage unique."""
    if not label:
        return False
    from nokker.payments.cardmask import otp_field
    from nokker.payments.detect import card_field

    return bool(card_field(label) or otp_field(label) or _PASSWORD_TERMS.search(label))


def vault_value(text: str | None) -> bool:
    """Valeur qui vient du coffre ou qui EST un secret : poignée `[secret:…]`, référence `secret:NOM`, marqueur de
    l'appareil (⟦…⟧), ou motif de secret connu (`qa/secrets.py`)."""
    if not text:
        return False
    s = str(text)
    if "secret:" in s:
        return True
    try:
        from nokker.redact.markers import MARKER_RE

        if MARKER_RE.search(s):
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        from nokker.qa.secrets import redact

        return bool(redact(s).names)
    except Exception:  # noqa: BLE001
        return False


def _typed_parts(name: str, args: dict[str, Any]) -> list[tuple[str, str, str]]:
    """(référence, libellé déclaré, texte) de chaque saisie d'un appel."""
    if name == "browser_fill_form":
        out = []
        for f in args.get("fields") or []:
            if isinstance(f, dict):
                out.append((str(f.get("ref") or ""), str(f.get("name") or f.get("element") or ""),
                            str(f.get("value") if f.get("value") is not None else "")))
        return out
    if name == "browser_press_key":
        return [("", "", "")]
    return [(str(args.get("ref") or ""), str(args.get("element") or ""),
             str(args.get("text") if args.get("text") is not None else args.get("value") or ""))]


@dataclass
class LiveSession:
    id: str
    tenant: str | None
    thread_id: str
    owner: str | None
    audience: set[str]
    started_at: float
    last_activity: float
    pool_session_id: str | None = None
    state: str = "running"  # running | handoff | ended
    seq: int = 0
    actions: deque = field(default_factory=lambda: deque(maxlen=ACTIONS_KEPT))
    handoff: dict[str, Any] | None = None
    ended: dict[str, Any] | None = None
    ended_at: float | None = None
    url: str = ""
    title: str = ""
    sensitive_refs: set[str] = field(default_factory=set)
    sensitive_kernel: bool = False
    last_frame_emit: float = 0.0
    last_frame: dict[str, Any] | None = None  # dernier `browser.frame` émis (sans frame_url : elle est refaite)
    seq_log: deque = field(default_factory=lambda: deque(maxlen=500))  # (ts, seq) : contexte des cartes
    bubble_id: str | None = None
    task: str = ""
    key: str = ""  # clé de recherche (le fil pour le pool ; `sites:<connecteur>` pour sites-chrome)
    # Opérations du navigateur derrière la session (`sites.SitesOps` : ticket d'écran, reprise) ; None = aucune (pool)
    ops: Any = None
    bubble: bool = True  # False : pas de fil connu (flux de connexion lancé hors tour), aucune bulle
    refs: dict[str, str] = field(default_factory=dict)  # références du dernier instantané → leur ligne (browser_point)
    points: deque = field(default_factory=lambda: deque(maxlen=ACTIONS_KEPT))  # 20 derniers browser.point
    box_fn: Any = None  # (ref) -> (texte, erreur, extra) : `browser_box` du navigateur (boîte + capture fraîche)

    @property
    def host(self) -> str:
        return url_host(self.url)

    @property
    def sensitive(self) -> bool:
        return self.sensitive_kernel or bool(self.sensitive_refs)


@dataclass
class BrowserLive:
    publish: Callable[[str, dict], Any] | None = None  # EventBus.publish
    hub: Any = None  # BubbleHub (bulle `browser`) ; None = pas de bulle
    screen_ticket: Callable[[str], str | None] | None = None  # session du pool → URL noVNC à usage unique
    frames: FrameStore = field(default_factory=FrameStore)
    clock: Callable[[], float] = time.time
    idle_timeout_s: float = IDLE_TIMEOUT_S
    handoff_ttl_s: float = HANDOFF_TTL_S
    _sessions: dict[str, LiveSession] = field(default_factory=dict, repr=False)
    _by_thread: dict[tuple[str | None, str], str] = field(default_factory=dict, repr=False)
    _codes: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)
    _cookies: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    # ── publication ──
    def _emit(self, s: LiveSession, event_type: str, payload: dict[str, Any]) -> None:
        if self.publish is None:
            return
        data = {**payload, "_tenant": s.tenant}
        audience = sorted(a for a in s.audience if a)
        if audience:  # sans audience (sites-chrome dans Lokkal) : tout le locataire, comme les autres événements
            data["_audience"] = audience
        try:
            self.publish(event_type, data)
        except Exception:  # noqa: BLE001 — la vue en direct ne casse jamais un appel d'outil
            logger.warning("vue navigateur : publication %s en échec", event_type, exc_info=True)

    def _next_seq(self, s: LiveSession) -> int:
        s.seq += 1
        s.seq_log.append((self.clock(), s.seq))
        return s.seq

    def _bubble_state(self, s: LiveSession) -> dict[str, Any]:
        out: dict[str, Any] = {"session_id": s.id, "state": s.state}
        if s.host:
            out["url_host"] = s.host
        if s.title:
            out["title"] = s.title
        if s.task:
            out["task"] = s.task
        return out

    def _bubble(self, s: LiveSession, create: bool = False) -> None:
        hub = self.hub
        if hub is None or not s.bubble:
            return
        from nokker.tenancy.context import tenant_scope

        status = {"running": "working", "handoff": "working", "ended": "done"}[s.state]
        if s.state == "ended" and (s.ended or {}).get("reason") in ("error", "timeout"):
            status = "failed"
        try:
            with tenant_scope(s.tenant) if s.tenant else _null():
                if create:
                    title = s.task or "Navigateur"
                    b = hub.create_kernel_bubble(s.thread_id, kind="browser", title=inline(title, TASK_MAX),
                                                 state=self._bubble_state(s), status=status)
                    s.bubble_id = b.id
                elif s.bubble_id:
                    hub.update_from_kernel(s.bubble_id, state=self._bubble_state(s), status=status)
        except Exception:  # noqa: BLE001 — une bulle ratée ne casse jamais la session (fil inconnu du hub, etc.)
            logger.info("vue navigateur : bulle non posée/mise à jour (%s)", s.id, exc_info=True)

    # ── cycle de vie ──
    def session_for(self, tenant: str | None, thread_id: str, owner: str | None = None,
                    audience: set[str] | None = None, pool_session_id: str | None = None,
                    task: str = "", *, key: str | None = None, ops: Any = None,
                    bubble: bool = True) -> LiveSession:
        """La session vivante de `key` (par défaut le fil), ou une nouvelle (bulle `browser` posée au démarrage)."""
        self.sweep()
        key = key or thread_id
        with self._lock:
            sid = self._by_thread.get((tenant, key))
            s = self._sessions.get(sid) if sid else None
            if s is not None and s.state != "ended":
                s.last_activity = self.clock()
                if pool_session_id:
                    s.pool_session_id = pool_session_id
                return s
            now = self.clock()
            if audience is None:
                audience = {owner} if owner else set()
                if not bubble:
                    audience = set(audience)
                if self.hub is not None:
                    try:
                        audience |= set(self.hub.thread_audience(thread_id))
                    except Exception:  # noqa: BLE001
                        pass
            if not task and self.hub is not None:
                try:
                    t = self.hub.threads.get(thread_id)
                    task = getattr(t, "title", "") or ""
                except Exception:  # noqa: BLE001
                    task = ""
            s = LiveSession(id="bs_" + secrets.token_hex(8), tenant=tenant, thread_id=thread_id, owner=owner,
                            audience=set(audience), started_at=now,
                            last_activity=now, pool_session_id=pool_session_id, task=inline(task, TASK_MAX),
                            key=key, ops=ops, bubble=bubble)
            self._sessions[s.id] = s
            self._by_thread[(tenant, key)] = s.id
        self._bubble(s, create=True)
        return s

    def current(self, tenant: str | None, thread_id: str) -> LiveSession | None:
        with self._lock:
            sid = self._by_thread.get((tenant, thread_id))
            s = self._sessions.get(sid) if sid else None
            return s if s is not None and s.state != "ended" else None

    def refuse(self, tenant: str | None, thread_id: str, name: str) -> str | None:
        """Motif de refus d'un appel pendant une passation (geste), sinon None."""
        s = self.current(tenant, thread_id)
        if s is None or s.state != "handoff" or name in ALLOWED_DURING_HANDOFF:
            return None
        return _handoff_refused((s.handoff or {}).get("reason", "challenge"))

    def end(self, s: LiveSession | None, reason: str) -> bool:
        """Termine la session : `browser.ended` émis UNE fois, captures purgées, codes et cookies révoqués."""
        if s is None:
            return False
        with self._lock:
            if s.state == "ended":
                return False
            s.state = "ended"
            s.ended_at = self.clock()
            s.ended = {"reason": reason, "at": iso(s.ended_at)}
            seq = self._next_seq(s)
            self.frames.purge(s.id)
            s.last_frame = None
            for table in (self._codes, self._cookies):
                for k in [k for k, v in table.items() if v["session_id"] == s.id]:
                    table.pop(k, None)
        self._emit(s, "browser.ended", {"session_id": s.id, "seq": seq, "reason": reason})
        self._bubble(s)
        return True

    def end_thread(self, tenant: str | None, thread_id: str, reason: str = "done") -> bool:
        return self.end(self.current(tenant, thread_id), reason)

    def sweep(self) -> None:
        """Inactivité ou passation expirée → `ended` (timeout) ; sessions finies oubliées après la rétention."""
        now = self.clock()
        to_end: list[LiveSession] = []
        with self._lock:
            for s in list(self._sessions.values()):
                if s.state == "running" and now - s.last_activity > self.idle_timeout_s:
                    to_end.append(s)
                elif s.state == "handoff" and now > (s.handoff or {}).get("_exp", now + 1):
                    to_end.append(s)
                elif s.state == "ended" and s.ended_at is not None and now - s.ended_at > ENDED_RETENTION_S:
                    self._sessions.pop(s.id, None)
                    if self._by_thread.get((s.tenant, s.key)) == s.id:
                        self._by_thread.pop((s.tenant, s.key), None)
            for table in (self._codes, self._cookies):
                for k in [k for k, v in table.items() if v["exp"] < now - ENDED_RETENTION_S]:
                    table.pop(k, None)
        for s in to_end:
            self.end(s, "timeout")

    def start_sweeper(self, interval_s: float = 30.0) -> threading.Thread:
        """Balayage périodique (fil démon) : un tour qui a planté sans `turn_done` finit quand même sur `ended`."""
        def run() -> None:
            ev = threading.Event()
            while not ev.wait(interval_s):
                try:
                    self.sweep()
                except Exception:  # noqa: BLE001
                    logger.warning("vue navigateur : balayage en échec", exc_info=True)

        t = threading.Thread(target=run, name="browser-live-sweep", daemon=True)
        t.start()
        return t

    # ── un appel d'outil observé ──
    def observe(self, tenant: str | None, thread_id: str, owner: str | None, name: str, args: dict[str, Any],
                text: str, is_error: bool, extra: dict[str, Any] | None = None, *,
                pool_session_id: str | None = None, audience: set[str] | None = None, task: str = "",
                key: str | None = None, ops: Any = None, bubble: bool = True,
                auto_resume: bool = False) -> str | None:
        """Suit un appel `browser_*` rendu par le navigateur. Rend une note du noyau à ajouter au résultat (passation),
        ou None."""
        extra = extra or {}
        meta = extra.get("meta") or {}
        act = meta.get("nokker_action") if isinstance(meta.get("nokker_action"), dict) else {}
        block = meta.get("nokker_block") if isinstance(meta.get("nokker_block"), dict) else None
        s = self.session_for(tenant, thread_id, owner, audience, pool_session_id, task, key=key, ops=ops,
                             bubble=bubble)
        now = self.clock()
        kind = KIND.get(name)
        with self._lock:
            s.last_activity = now
            # URL et titre : `_meta` d'abord, sinon l'en-tête de l'instantané (« Page URL: … », « Page Title: … »)
            new_url = str(act.get("url") or "")
            if not new_url:
                m = _URL_LINE.search(text or "")
                new_url = m.group(1) if m else (str(args.get("url") or "") if name == "browser_navigate" else "")
            navigated = name in PAGE_CHANGE and not is_error
            if new_url and _page_key(new_url) != _page_key(s.url):
                navigated = navigated or bool(s.url)
                s.url = new_url
            t = act.get("title")
            if not t:
                m = _TITLE_LINE.search(text or "")
                t = m.group(1) if m else None
            if t:
                s.title = inline(t, TITLE_MAX)
            if navigated:  # la sensibilité du noyau tombe à la navigation
                s.sensitive_refs.clear()
                s.sensitive_kernel = False
            self._note_refs(s, name, text, navigated)
            # sensibilité décidée par le noyau, sur une saisie
            typed_total = 0
            if kind == "type" and name != "browser_press_key":
                for ref, label, value in _typed_parts(name, args):
                    typed_total += len(value)
                    lab = " ".join(x for x in (label, str(act.get("label") or "")) if x)
                    hot = sensitive_label(lab) or vault_value(value)
                    key = ref or lab or "?"
                    if hot and value:
                        s.sensitive_refs.add(key)
                    elif value == "" and key in s.sensitive_refs:
                        s.sensitive_refs.discard(key)  # champ vidé
            sensitive = s.sensitive or act.get("sensitive") is True
            action_payload = None
            if kind is not None:
                seq = self._next_seq(s)
                if kind == "type":
                    if name == "browser_press_key" and len(str(args.get("key") or "")) > 1:
                        label = f"touche {inline(args.get('key'), 20)}"
                    else:
                        n = typed_total if name != "browser_press_key" else len(str(args.get("key") or ""))
                        label = f"texte saisi ({n} car.)"
                elif kind == "navigate":
                    label = s.host or url_host(new_url)
                else:
                    label = inline(act.get("label") or args.get("element") or "", TARGET_MAX)
                # Émis seulement APRÈS une tentative réelle dans le navigateur (un geste refusé par une porte ou en
                # attente de carte n'arrive jamais ici) ; `ok` : `_meta.nokker_action.ok`, sinon l'absence d'erreur.
                ok = act.get("ok") if isinstance(act.get("ok"), bool) else not is_error
                action_payload = {"session_id": s.id, "seq": seq, "ts": iso(now), "kind": kind,
                                  "target_label": inline(label, TARGET_MAX), "url_host": s.host, "ok": bool(ok)}
                cur = act.get("cursor") if isinstance(act.get("cursor"), dict) else None
                if cur is not None:
                    try:
                        x, y = float(cur.get("x")), float(cur.get("y"))
                        if 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0:
                            action_payload["cursor"] = {"x": round(x, 4), "y": round(y, 4)}
                    except (TypeError, ValueError):
                        pass
                s.actions.append(action_payload)
        if action_payload is not None:
            self._emit(s, "browser.action", action_payload)
        # capture : jamais si sensible (défense en profondeur : une image reçue alors que sensitive=true est jetée)
        if kind is not None and kind != "read" or navigated or extra.get("images"):
            self._frame(s, extra.get("images") or [], sensitive, now, frame_seq=act.get("frame_seq"))
        # passation
        reason = None
        if block is not None:
            reason = BLOCK_REASON.get(str(block.get("kind") or "").lower(), "challenge")
        elif text and _CHALLENGE_TEXT.search(text):
            reason = "captcha" if _CAPTCHA_WORD.search(text) else "challenge"
        if reason is not None:
            self.handoff(s, reason)
            return mt.pick(mt.HANDOFF_NOTE).format(reason=reason)
        if auto_resume and s.state == "handoff" and act:
            # sites-chrome : `nokker_block` absent = plus rien ne bloque (l'humain a résolu, le flux reprend)
            self._resume(s, call_ops=False)
        return None

    def sensitive_refs(self, tenant: str | None, key: str) -> set[str]:
        s = self.current(tenant, key)
        with self._lock:
            return set(s.sensitive_refs) if s is not None else set()

    def mark_sensitive(self, tenant: str | None, key: str, ref: str) -> None:
        """Champ marqué par le coffre (`data-nk-secret`, `McpBrowserDriver.mark_secret`) : sensible dès sa saisie."""
        s = self.current(tenant, key)
        if s is not None and ref:
            with self._lock:
                s.sensitive_refs.add(ref)

    @staticmethod
    def _note_refs(s: LiveSession, name: str, text: str, navigated: bool) -> None:
        """Références que la page ne peut pas fabriquer (`result_fence.browser_refs`) → leur ligne (sans valeur)."""
        from nokker.tools.result_fence import browser_refs

        clean, refs = browser_refs(text or "")
        if refs:
            lines: dict[str, str] = {}
            for ln in clean.split("\n"):
                for m in _REF_ATTR.finditer(ln):
                    v = _VALUE_AFTER.search(ln)
                    lines.setdefault(m.group(1), ln[: v.start(2)] if v else ln)  # la ligne, sans la valeur
            fresh = {r: lines.get(r, "") for r in refs}
            if name == "browser_find":
                s.refs.update(fresh)
            else:
                s.refs = fresh
        elif navigated:
            s.refs.clear()

    def for_thread(self, tenant: str | None, thread_id: str) -> LiveSession | None:
        """La session vivante qui montre ce fil (pool : clé = fil ; sites-chrome : fil de la bulle)."""
        s = self.current(tenant, thread_id)
        if s is not None:
            return s
        with self._lock:
            found = [x for x in self._sessions.values()
                     if x.thread_id == thread_id and x.state != "ended" and self._same_tenant(x, tenant)]
        return max(found, key=lambda x: x.started_at) if found else None

    def point(self, tenant: str | None, thread_id: str, ref: str, why: str) -> tuple[bool, str]:
        """`browser_point` : désigne un élément du dernier instantané sur la frame courante. Aucun geste sur la page.
        Rend (montré, texte pour le modèle)."""
        from nokker.browserlive import texts as T
        from nokker.browserlive.mask import sensitive_line

        s = self.for_thread(tenant, thread_id)
        if s is None:
            return False, T.POINT_NO_SESSION
        if s.state == "handoff":
            return False, _handoff_refused((s.handoff or {}).get("reason", "challenge"))
        with self._lock:
            line = s.refs.get(ref)
            if line is None:
                return False, T.POINT_UNKNOWN_REF.format(ref=inline(ref, 20))
            if ref in s.sensitive_refs or sensitive_line(line, s.sensitive_refs):
                return False, T.POINT_SENSITIVE
            box_fn = s.box_fn
        if box_fn is None:
            return False, T.POINT_NO_BOX
        # `browser_box {ref, scroll: true}` (sites-chrome 00814ab) : élément amené dans la vue, capture FRAÎCHE, rien
        # n'est cliqué ; `_meta.nokker_box = {ref, x, y, w, h, in_view, frame_seq}` ; erreur si l'élément est sensible
        try:
            text, is_error, extra = box_fn(ref)
        except Exception:  # noqa: BLE001
            logger.info("vue navigateur : boîte de %s introuvable", ref, exc_info=True)
            return False, T.POINT_NO_BOX
        if is_error:
            return False, T.POINT_SENSITIVE if "sensible" in str(text or "").lower() else T.POINT_NO_BOX
        meta = (extra or {}).get("meta") or {}
        raw = meta.get("nokker_box") if isinstance(meta, dict) else None
        box = _box(raw)
        if box is None or not (raw or {}).get("in_view", True):
            return False, T.POINT_NO_BOX
        frame_seq = (raw or {}).get("frame_seq")
        if frame_seq is None:  # page sensible : pas de capture
            return False, T.POINT_NO_FRAME
        act = meta.get("nokker_action") if isinstance(meta.get("nokker_action"), dict) else {}
        now = self.clock()
        if act.get("sensitive") is True:
            return False, T.POINT_NO_FRAME
        # la capture fraîche part comme une frame (hors limite d'une par seconde) : la boîte est dans SON repère
        if not self._frame(s, (extra or {}).get("images") or [], False, now, force=True, frame_seq=frame_seq):
            return False, T.POINT_NO_FRAME
        label = inline(_name_of(line) or ref, TARGET_MAX)
        payload = {"session_id": s.id, "seq": frame_seq, "ts": iso(now), "box": box, "label": label,
                   "why": inline(why, 120)}
        with self._lock:
            s.points.append({k: v for k, v in payload.items() if k != "session_id"})  # écran ouvert après coup
        self._emit(s, "browser.point", payload)
        return True, T.POINT_OK.format(label=label)

    def _frame(self, s: LiveSession, images: list[Any], sensitive: bool, now: float, *, force: bool = False,
               frame_seq: Any = None) -> bool:
        """Publie une `browser.frame` (au plus une par seconde, sauf `force`) ; vrai si une capture a été gardée."""
        data = None
        for img in images:
            if isinstance(img, dict) and str(img.get("mime") or "").lower() == "image/webp":
                data = img.get("data")
                if frame_seq is None and isinstance(img.get("meta"), dict):
                    frame_seq = img["meta"].get("frame_seq")
                break
        if data is None and not sensitive:
            return False  # rien à montrer
        with self._lock:
            if not force and now - s.last_frame_emit < FRAME_MIN_INTERVAL_S:
                if sensitive:  # une sensibilité n'attend pas : l'ancienne image ne doit plus être resservie
                    self.frames.purge(s.id)
                    s.last_frame = None
                return False
            frame = None
            if not sensitive and data is not None:
                frame = self.frames.put(s.id, data)
                if frame is None:
                    return False  # pas un WebP, trop large : jamais servi
            elif sensitive:
                data = None  # jetée sans être gardée
            seq = self._next_seq(s)
            s.last_frame_emit = now
            payload = {"session_id": s.id, "seq": seq, "ts": iso(now), "url_host": s.host, "title": s.title,
                       "frame_url": self.frames.url(frame) if frame else None, "w": frame.w if frame else None,
                       "h": frame.h if frame else None, "sensitive": bool(sensitive)}
            if frame_seq is not None:
                payload["frame_seq"] = frame_seq  # numéro de capture du navigateur (repère de browser.point)
            s.last_frame = {**payload, "_frame_id": frame.id if frame else None}
        self._emit(s, "browser.frame", payload)
        return frame is not None

    def handoff(self, s: LiveSession, reason: str) -> dict[str, Any] | None:
        with self._lock:
            if s.state != "running":
                return s.handoff
            code = secrets.token_urlsafe(9)
            exp = self.clock() + self.handoff_ttl_s
            self._codes[code] = {"session_id": s.id, "owner": s.owner, "exp": exp, "used": False}
            s.state = "handoff"
            seq = self._next_seq(s)
            s.handoff = {"session_id": s.id, "seq": seq, "reason": reason, "url_host": s.host,
                         "takeover_url": f"/v1/browser/takeover/{code}", "expires_at": iso(exp), "_exp": exp}
            payload = {k: v for k, v in s.handoff.items() if not k.startswith("_")}
        self._emit(s, "browser.handoff", payload)
        self._bubble(s)
        return payload

    # ── titulaire ──
    @staticmethod
    def _same_tenant(s: LiveSession, tenant: str | None) -> bool:
        from nokker.tenancy.context import INTERNAL_TENANT

        return s.tenant == tenant or (s.tenant is None and tenant == INTERNAL_TENANT)

    def _visible(self, s: LiveSession | None, tenant: str | None, sender: str | None) -> bool:
        if s is None or not self._same_tenant(s, tenant):
            return False
        if s.owner is None and not s.audience:
            return sender is not None or tenant is not None  # sites-chrome (Lokkal) : toute identité du locataire
        return sender in s.audience or sender == s.owner

    def _is_holder(self, s: LiveSession, sender: str | None) -> bool:
        return sender == s.owner if s.owner is not None else sender is not None

    def get(self, session_id: str, tenant: str | None, sender: str | None) -> dict[str, Any] | None:
        self.sweep()
        with self._lock:
            s = self._sessions.get(session_id)
            if not self._visible(s, tenant, sender):
                return None
            last = None
            if s.last_frame is not None:
                last = {k: v for k, v in s.last_frame.items() if not k.startswith("_")}
                fid = s.last_frame.get("_frame_id")
                frame = self.frames.last(s.id)
                last["frame_url"] = self.frames.url(frame) if (fid and frame is not None and frame.id == fid) else None
            out: dict[str, Any] = {"session_id": s.id, "state": s.state, "seq": s.seq, "last_frame": last,
                                   "actions": list(s.actions), "points": list(s.points),
                                   "url_host": s.host, "title": s.title,
                                   "bubble_id": s.bubble_id}
            if s.handoff is not None and s.state == "handoff":
                out["handoff"] = {k: v for k, v in s.handoff.items() if not k.startswith("_")}
            if s.ended is not None:
                out["ended"] = dict(s.ended)
            return out

    def narration(self, session_id: str, tenant: str | None, sender: str | None, seq: int) -> str | None:
        """Texte d'une désignation (`browser.point`) DÉJÀ émis par le noyau pour ce `seq` : son `why` (repli : `label`).
        Titulaire de la session seulement ; session inconnue, autre locataire, hors audience, seq qui n'est pas une
        désignation ou désignation sans texte : None (404). Jamais un texte venu du client."""
        with self._lock:
            s = self._sessions.get(session_id)
            if not self._visible(s, tenant, sender) or not self._is_holder(s, sender):
                return None
            found = next((p for p in reversed(s.points) if p.get("seq") == seq), None)
        if found is None:
            return None
        text = str(found.get("why") or found.get("label") or "").strip()
        return text or None

    def resume(self, session_id: str, tenant: str | None, sender: str | None) -> tuple[int, dict[str, Any]]:
        """Le titulaire rend la main. Idempotent : déjà en cours ou finie → 200 avec l'état, sans nouvel événement."""
        with self._lock:
            s = self._sessions.get(session_id)
            if not self._visible(s, tenant, sender):
                return 404, {"error": {"code": "not_found", "message": "session navigateur inconnue"}}
            if not self._is_holder(s, sender):
                return 403, {"error": {"code": "forbidden", "message": "réservé au titulaire de la session"}}
            if s.state != "handoff":
                return 200, {"session_id": s.id, "state": s.state, "seq": s.seq}
        seq = self._resume(s, call_ops=True)
        return 200, {"session_id": s.id, "state": "running", "seq": seq}

    def _resume(self, s: LiveSession, *, call_ops: bool) -> int:
        with self._lock:
            if s.state != "handoff":
                return s.seq
            s.state = "running"
            s.handoff = None
            s.last_activity = self.clock()
            seq = self._next_seq(s)
            for table in (self._codes, self._cookies):
                for k in [k for k, v in table.items() if v["session_id"] == s.id]:
                    table.pop(k, None)
        self._emit(s, "browser.resumed", {"session_id": s.id, "seq": seq, "reason": "done"})
        self._bubble(s)
        resume = getattr(s.ops, "resume", None) if call_ops else None
        if resume is not None and s.pool_session_id:
            try:
                resume(s.pool_session_id)  # sites-chrome : POST /s/{id}/resume (lève needsHuman et block)
            except Exception:  # noqa: BLE001
                logger.warning("vue navigateur : reprise côté navigateur en échec", exc_info=True)
        return seq

    def takeover(self, code: str, tenant: str | None, sender: str | None) -> tuple[int, dict[str, Any]]:
        """Échange le code à usage unique contre un cookie lié au titulaire et à la session. Rejoué → 410 ; une autre
        identité → 403 (le code n'est pas consommé)."""
        now = self.clock()
        with self._lock:
            rec = self._codes.get(code)
            if rec is None:
                return 410, {"error": {"code": "gone", "message": "lien de reprise inconnu, utilisé ou expiré"}}
            s = self._sessions.get(rec["session_id"])
            if s is None or not self._same_tenant(s, tenant) or sender is None or \
                    (rec["owner"] is not None and sender != rec["owner"]):
                return 403, {"error": {"code": "forbidden", "message": "réservé au titulaire de la session"}}
            if rec["used"] or now > rec["exp"] or s.state != "handoff":
                return 410, {"error": {"code": "gone", "message": "lien de reprise déjà utilisé ou expiré"}}
            rec["used"] = True
            token = secrets.token_urlsafe(24)
            self._cookies[token] = {"session_id": s.id, "owner": sender, "tenant": tenant,
                                    "exp": min(now + TAKEOVER_COOKIE_TTL_S, rec["exp"])}
            return 200, {"session_id": s.id, "cookie": token, "screen_url": f"/v1/browser/sessions/{s.id}/screen",
                         "max_age": int(self._cookies[token]["exp"] - now)}

    def screen(self, session_id: str, cookie: str | None) -> tuple[int, str | None]:
        """Cookie de reprise valide pour CETTE session → URL de l'écran (ticket noVNC du pool, à usage unique)."""
        now = self.clock()
        with self._lock:
            rec = self._cookies.get(cookie or "")
            s = self._sessions.get(session_id)
            if rec is None or s is None or rec["session_id"] != session_id or now > rec["exp"]:
                return 403, None
            if s.state != "handoff":
                return 410, None
            pool_sid = s.pool_session_id
        ticket = getattr(s.ops, "screen_url", None) or self.screen_ticket
        if ticket is None or not pool_sid:
            return 503, None  # pool de la bêta : pas d'écran (après la démo)
        try:
            url = ticket(pool_sid)
        except Exception:  # noqa: BLE001
            logger.warning("vue navigateur : ticket d'écran en échec", exc_info=True)
            url = None
        return (302, url) if url else (503, None)

    # ── cartes d'approbation ──
    def context_for(self, thread_id: str, created_at: float) -> dict[str, Any] | None:
        """`{session_id, seq}` de la session navigateur du fil ouverte quand la carte a été posée, sinon None."""
        with self._lock:
            for s in self._sessions.values():
                if s.thread_id != thread_id or created_at < s.started_at:
                    continue
                if s.ended_at is not None and created_at > s.ended_at:
                    continue
                seq = 0
                for ts, n in s.seq_log:
                    if ts <= created_at:
                        seq = n
                    else:
                        break
                return {"session_id": s.id, "seq": seq}
        return None


def _box(raw: Any) -> dict[str, float] | None:
    """{x, y, w, h} normalisés 0-1, non vides et dans l'image ; sinon None."""
    if not isinstance(raw, dict):
        return None
    try:
        x, y, w, h = (float(raw[k]) for k in ("x", "y", "w", "h"))
    except (KeyError, TypeError, ValueError):
        return None
    if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0 and 0.0 < w <= 1.0 and 0.0 < h <= 1.0):
        return None
    return {"x": round(x, 4), "y": round(y, 4), "w": round(min(w, 1.0 - x), 4), "h": round(min(h, 1.0 - y), 4)}


_REF_ATTR = re.compile(r"\[ref=(e\d+)\]")
_VALUE_AFTER = re.compile(r"(\]\s*:\s*)(\S.*)$")
_NAME = re.compile(r'^\s*-?\s*[a-z]+\s+"((?:[^"\\]|\\.)*)"')


def _name_of(line: str) -> str:
    m = _NAME.match(line or "")
    return m.group(1) if m else ""


def _page_key(url: str) -> str:
    try:
        p = urlsplit(url)
        return f"{p.scheme}://{p.netloc}{p.path}"
    except ValueError:
        return url


class _null:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *a: Any) -> None:
        return None


# ── instance du processus (posée par `serve`, lue par le pool, le serveur et `approval_to_json`) ──
_LIVE: BrowserLive | None = None


def configure(live: BrowserLive | None) -> None:
    global _LIVE
    _LIVE = live


def live() -> BrowserLive | None:
    return _LIVE


__all__ = ["BrowserLive", "LiveSession", "configure", "live", "sensitive_label", "vault_value"]
