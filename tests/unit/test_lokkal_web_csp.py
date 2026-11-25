"""CSP de l'interface web Lokkal (deploy/lokkal/web/security-headers.conf) : la salle LiveKit de Nina en direct
(Runway) est permise, CET hôte seulement (ni joker, ni `wss:` nu), et le reste de la politique ne s'ouvre pas."""
from __future__ import annotations

import re
from pathlib import Path

CONF = Path(__file__).resolve().parents[2] / "deploy" / "lokkal" / "web" / "security-headers.conf"
LIVEKIT = "runway-5y2mkmh5.livekit.cloud"


def _directives() -> dict[str, list[str]]:
    text = CONF.read_text(encoding="utf-8")
    csp = re.search(r'add_header Content-Security-Policy "([^"]+)"', text).group(1)
    out = {}
    for part in csp.split(";"):
        words = part.split()
        if words:
            out[words[0]] = words[1:]
    return out


def test_nina_live_room_is_allowed_and_only_that_host():
    d = _directives()
    assert d["connect-src"] == ["'self'", f"wss://{LIVEKIT}", f"https://{LIVEKIT}"]
    assert not any("*" in v or v in ("wss:", "https:", "ws:") for v in d["connect-src"])


def test_rest_of_the_policy_stays_closed():
    d = _directives()
    assert d["default-src"] == ["'self'"] and d["object-src"] == ["'none'"] and d["frame-ancestors"] == ["'none'"]
    assert d["media-src"] == ["'self'", "blob:"]


def _app_conf() -> Path | None:
    """La CSP de l'app (nokker-apps deploy/web/security-headers.conf), seulement si `NOKKER_APPS_DIR` la désigne (le
    checkout à comparer est celui de la release : jamais un dépôt voisin sur une branche quelconque)."""
    import os

    base = os.environ.get("NOKKER_APPS_DIR")
    conf = Path(base) / "deploy" / "web" / "security-headers.conf" if base else None
    return conf if conf is not None and conf.exists() else None


def test_lokkal_connect_src_covers_the_app_nina_live_hosts():
    """Garde d'alignement : tout hôte LiveKit que l'app permet, Lokkal le permet aussi (sinon Nina live retombe sur le
    rig 2D sur Lokkal seulement, défaut vu le 08.11). À lancer à l'intégration avec `NOKKER_APPS_DIR=<nokker-apps au
    sha du web de la release>` ; sans lui : sauté, et l'hôte reste tenu par `test_nina_live_room_is_allowed_and_only_that_host`."""
    import pytest

    app = _app_conf()
    if app is None:
        pytest.skip("nokker-apps absent : NOKKER_APPS_DIR pour comparer")
    csp = re.search(r'add_header Content-Security-Policy "([^"]+)"', app.read_text(encoding="utf-8")).group(1)
    app_connect = next((p.split()[1:] for p in csp.split(";") if p.split() and p.split()[0] == "connect-src"), [])
    livekit = {v for v in app_connect if "livekit" in v}
    assert livekit, "l'app ne permet plus aucun hôte LiveKit : revoir Nina live"
    assert livekit <= set(_directives()["connect-src"])
