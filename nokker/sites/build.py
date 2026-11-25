"""Sites connectés — assemblage dans Lokkal (drapeau `local.sites`, éteint par défaut ; docs/CONNECTED-SITES.md §12).

`build_sites` rend le service, la source d'outils, les routes et la capture du chat. Le pilote réel parle au MCP de
`sites-chrome` (`NOKKER_SITES_BROWSER_URL`, jeton `NOKKER_SITES_BROWSER_TOKEN`) ; sans lui, aucune connexion ne part
(raison « navigateur des sites absent »).
"""
from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from nokker.sites.capture import SecretCapture
from nokker.sites.credentials import Credentials
from nokker.sites.crypto import master_from_text, new_master_key
from nokker.sites.http import SitesHttp
from nokker.sites.profiles import ProfileStore
from nokker.sites.requests import Requests
from nokker.sites.service import SitesService
from nokker.sites.store import SitesStore
from nokker.sites.tools import WHERE_APP, WHERE_TELEGRAM, SitesToolSource

MASTER_NAME = "sites-master"


def master_key(system_vault: Any) -> bytes:
    """Clé maître des profils, au coffre (portée système) ; créée au premier usage, jamais en miroir."""
    text = system_vault.get(MASTER_NAME)
    if not text:
        text = new_master_key()
        system_vault.set(MASTER_NAME, text)
    return master_from_text(text)


def _mcp_driver_factory() -> Callable[[Any, bytes | None], Any]:
    url = os.environ.get("NOKKER_SITES_BROWSER_URL", "")

    def factory(conn: Any, profile: bytes | None) -> Any:
        if not url:
            raise RuntimeError("navigateur des sites absent")
        from nokker.sites.driver_mcp import McpBrowserDriver
        from nokker.tools.browser_pool import _McpCaller

        from nokker.browserlive.sites import LiveCaller, SitesOps

        caller = _McpCaller(f"{url.rstrip('/')}/s/{conn.id}/mcp",
                            {"Authorization": f"Bearer {os.environ.get('NOKKER_SITES_BROWSER_TOKEN', '')}"})
        # (texte, erreur) au pilote ; captures, actions et blocages à la vue en direct (docs/BROWSER-LIVE.md §7)
        ops = SitesOps(url, lambda: os.environ.get("NOKKER_SITES_BROWSER_TOKEN", ""))
        return McpBrowserDriver(LiveCaller(caller, conn, ops), profile)

    return factory


def _screen_url_factory() -> Callable[[Any], str] | None:
    """Écran de passation (§4.3) : ticket demandé à `sites-chrome` pour CE connecteur (15 min, un seul flux ouvert à la
    fois), page noVNC `/sites/screen/<id>/` servie par le web de Lokkal, en local ou par la passerelle. Le ticket n'est
    rendu qu'au propriétaire, par le lien de passation à usage unique (`open_handoff`)."""
    url = os.environ.get("NOKKER_SITES_BROWSER_URL", "").rstrip("/")
    # RELATIF par défaut : la page qui ouvre la passation est servie par le web local (127.0.0.1) OU par la passerelle
    # (<nom>.nokker.app, téléphone) ; les deux relaient /sites/screen/ (flux WebSocket compris) vers sites-chrome
    base = os.environ.get("NOKKER_SITES_SCREEN_BASE", "").rstrip("/")
    if not url:
        return None

    def screen(conn: Any) -> str:
        import json
        import urllib.request

        req = urllib.request.Request(f"{url}/s/{conn.id}/screen-ticket", data=b"{}", method="POST", headers={
            "Authorization": f"Bearer {os.environ.get('NOKKER_SITES_BROWSER_TOKEN', '')}",
            "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 — réseau interne sitesnet
                path = str(json.load(r).get("path") or "")
        except Exception:  # noqa: BLE001 — navigateur du site arrêté : la page d'écran dira « fermé »
            path = ""
        if not path.startswith(f"/sites/screen/{conn.id}/"):
            path = f"/sites/screen/{conn.id}/"
        return base + path

    return screen


def build_sites(*, vault: Any, state_dir: Path, who: Callable[[], str], known: Any = None,
                notify: Callable[..., None] | None = None,
                mail_links: Callable[..., str | None] | None = None,
                driver_factory: Callable[[Any, bytes | None], Any] | None = None,
                telegram: bool = False, households: Any = None, origin: str = "https://app.nokker.ai",
                minor: Callable[[str], bool] | None = None) -> dict[str, Any]:
    where = (lambda account: WHERE_TELEGRAM) if telegram else (lambda account: WHERE_APP)
    root = Path(state_dir) / "sites"
    store = SitesStore(root / "sites.json")
    requests = Requests()
    credentials = Credentials(vault.user, store, known)
    master = master_key(vault.system)
    service = SitesService(store=store, credentials=credentials, requests=requests,
                           profiles=ProfileStore(master, root / "profiles"),
                           driver_factory=driver_factory or _mcp_driver_factory(), mail_links=mail_links,
                           notify=notify, origin=origin, where=where, screen_url=_screen_url_factory())

    def save_password(account: str, cid: str, value: str) -> str | None:
        conn = store.get(cid)
        if conn is None or conn.account != account:
            return None
        credentials.save(conn, value)
        return conn.label

    capture = SecretCapture(requests, save_password=save_password,
                            labels=lambda account: [(c.id, c.label) for c in store.of(account)],
                            who=lambda sender: who(), remember=(known.add if known is not None else None))
    return {"service": service, "source": SitesToolSource(service, who, where=where, households=households), "http": SitesHttp(service, who, minor=minor),
            "capture": capture, "master": master, "state_dir": Path(state_dir)}


__all__ = ["MASTER_NAME", "build_sites", "master_key"]
