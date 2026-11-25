"""Vue en direct sur le navigateur des sites connectés de Lokkal (`sites-chrome`, docs/BROWSER-LIVE.md §7).

`sites-chrome` (`NOKKER_SITES_BROWSER_URL`, jeton `NOKKER_SITES_BROWSER_TOKEN`) rend, après chaque geste :
`_meta.nokker_action = {seq, tool, label, url, t, ok, sensitive, frame, title?, cursor?}`, un item `image` WebP sauf si
`sensitive`, et `_meta.nokker_block = {kind, vendor?, detail}` quand quelque chose bloque. Routes du gestionnaire :
`GET /s/{id}/frame` (200 + X-Nokker-Frame-Seq/-Time, 204 sans capture, 404 session inactive),
`POST /s/{id}/screen-ticket` → `{ticket, path, expires_in}` (`path` = `/sites/screen/<id>/?ticket=…`, servi par le WEB
de Lokkal, pas par sites-chrome), `POST /s/{id}/resume` (lève needsHuman et block).

Une session navigateur en direct par pilote (`McpBrowserDriver`, un par flux ou lecture d'un connecteur) : ouverte au
premier geste, finie à `browser_close` (le pilote le fait toujours, en `finally`). Le fil qui porte la bulle est celui du
dernier appel `sites_*` du compte (`note_thread`, 30 min) ; sans fil connu (flux relancé hors tour), pas de bulle.
Dans Lokkal, la session appartient au locataire de la machine : pas d'audience par membre.

Le flux de connexion (`sites/service.py`) garde sa propre passation (lien `/sites/h/…`) : ici, aucun geste n'est
refusé ; `nokker_block` ouvre `browser.handoff`, et son absence au geste suivant la referme (`browser.resumed`).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from nokker.browserlive.live import live
from nokker.browserlive.mask import mask_values

logger = logging.getLogger("nokker.browserlive.sites")
THREAD_HINT_S = 1800.0
_HINTS: dict[str, tuple[str, str | None, float]] = {}
_HINTS_LOCK = threading.Lock()


def note_thread(account: str | None, thread_id: str | None, sender: str | None) -> None:
    """Dernier fil où le compte a appelé un outil `sites_*` : la bulle `browser` d'un flux y sera posée."""
    if not account or not thread_id:
        return
    with _HINTS_LOCK:
        _HINTS[account] = (thread_id, sender, time.time())


def thread_for(account: str | None) -> str | None:
    with _HINTS_LOCK:
        hit = _HINTS.get(account or "")
    if hit is None or time.time() - hit[2] > THREAD_HINT_S:
        return None
    return hit[0]


def screen_base() -> str:
    """Origine du web de Lokkal (ou de la passerelle) qui sert `/sites/screen/…` ; vide = chemin relatif (la page qui
    ouvre l'écran est servie par ce même web, en local comme par `<nom>.nokker.app`)."""
    return os.environ.get("NOKKER_SITES_SCREEN_BASE", "").rstrip("/")


class SitesOps:
    """Routes du gestionnaire `sites-chrome` dont la vue en direct a besoin. `opener` injectable (tests)."""

    def __init__(self, base_url: str, token: Callable[[], str], *, screen_origin: Callable[[], str] = screen_base,
                 opener: Callable[..., Any] | None = None, timeout_s: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.screen_origin = screen_origin
        self.opener = opener or urllib.request.urlopen
        self.timeout_s = timeout_s

    def _req(self, method: str, path: str) -> tuple[int, bytes]:
        req = urllib.request.Request(self.base_url + path, method=method, data=b"{}" if method == "POST" else None,
                                     headers={"Authorization": f"Bearer {self.token()}",
                                              "Content-Type": "application/json"})
        try:
            with self.opener(req, timeout=self.timeout_s) as r:  # noqa: S310 — réseau interne sitesnet
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, b""

    def frame(self, cid: str) -> bytes | None:
        """Dernière capture (`GET /s/{id}/frame`) : 200 + WebP ; 204 rien encore ; 404 session inactive → None."""
        try:
            status, body = self._req("GET", f"/s/{cid}/frame")
        except Exception:  # noqa: BLE001
            return None
        return body if status == 200 and body else None

    def screen_url(self, cid: str) -> str | None:
        """Ticket noVNC à usage unique → origine du web de Lokkal + `path`. Seul `path` est lu (jamais une URL du
        navigateur), et il doit viser l'écran de CE connecteur."""
        try:
            status, body = self._req("POST", f"/s/{cid}/screen-ticket")
            path = str(json.loads(body or b"{}").get("path") or "") if status in (200, 201) else ""
        except Exception:  # noqa: BLE001
            return None
        if not path.startswith(f"/sites/screen/{cid}/") or "://" in path:
            return None
        return self.screen_origin() + path

    def resume(self, cid: str) -> None:
        self._req("POST", f"/s/{cid}/resume")


def _tenant_of(account: str | None) -> str | None:
    # Lokkal : `who()` = locataire courant, ou « local » hors portée — None = le locataire interne (seul abonné local)
    return account if account and account.startswith("tn_") else None


class LiveCaller:
    """Enveloppe le client MCP de `sites-chrome` (`_McpCaller`, rend (texte, erreur, code, extra)) : rend
    (texte, erreur) au pilote comme avant, et nourrit la vue en direct. Jamais bloquant."""

    def __init__(self, caller: Any, conn: Any, ops: SitesOps | None) -> None:
        self.caller, self.conn, self.ops = caller, conn, ops

    @property
    def key(self) -> str:
        return f"sites:{self.conn.id}"

    def call(self, name: str, args: dict) -> tuple[str, bool]:
        out = self.caller.call(name, args)
        text, is_error = out[0], out[1]
        if isinstance(text, str) and name.startswith("browser_"):
            # valeurs des champs sensibles retirées (instantané, texte lu) avant le pilote, donc avant le modèle (§4)
            lv = live()
            refs = lv.sensitive_refs(_tenant_of(getattr(self.conn, "account", None)), self.key) if lv else set()
            text = mask_values(text, refs)
        extra = out[3] if len(out) > 3 and isinstance(out[3], dict) else {}
        try:
            self._observe(name, args, text, bool(is_error), extra)
        except Exception:  # noqa: BLE001 — la vue en direct ne casse jamais un flux de connexion
            logger.warning("vue navigateur (sites) : observation de %s en échec", name, exc_info=True)
        return text, is_error

    def _observe(self, name: str, args: dict, text: str, is_error: bool, extra: dict) -> None:
        lv = live()
        if lv is None:
            return
        account = getattr(self.conn, "account", None)
        tenant = _tenant_of(account)
        if name == "browser_close":
            lv.end_thread(tenant, self.key, "done")
            return
        if name == "browser_evaluate":
            fn = str(args.get("function") or "")
            if "data-nk-secret" in fn and args.get("ref") and "setAttribute" in fn:
                lv.mark_sensitive(tenant, self.key, str(args["ref"]))
            return
        if not name.startswith("browser_"):
            return  # sites_profile_import/export : pas un geste
        act = (extra.get("meta") or {}).get("nokker_action")
        if not extra.get("images") and isinstance(act, dict) and act.get("frame") and act.get("sensitive") is not True \
                and self.ops is not None:
            data = self.ops.frame(self.conn.id)  # SC_FRAMES=0 : dernière capture par le gestionnaire
            if data:
                extra = {**extra, "images": [{"mime": "image/webp", "data": data}]}
        thread = thread_for(account)
        lv.observe(tenant, thread or self.key, None, name, args, text, is_error, extra,
                   pool_session_id=self.conn.id, audience=set(), task=str(getattr(self.conn, "label", "") or ""),
                   key=self.key, ops=self.ops, bubble=thread is not None, auto_resume=True)
        s = lv.current(tenant, self.key)
        if s is not None and s.box_fn is None:
            s.box_fn = self._box  # browser_point : boîte demandée à sites-chrome

    def _box(self, ref: str) -> tuple[str, bool, dict]:
        """`browser_box {ref, scroll: true}` (sites-chrome 00814ab) → (texte, erreur, extra) : boîte et capture fraîche."""
        out = self.caller.call("browser_box", {"ref": ref, "scroll": True})
        return out[0], bool(out[1]), (out[3] if len(out) > 3 and isinstance(out[3], dict) else {})


__all__ = ["LiveCaller", "SitesOps", "note_thread", "screen_base", "thread_for"]
