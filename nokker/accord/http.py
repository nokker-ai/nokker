"""ACCORD — routes de l'app sur Lokkal (docs/ACCORD-DEVICE.md §2) et démarrage du service. Propriétaire de la
machine seulement (jeton local), jamais par la passerelle : `api/server.py` filtre avant d'appeler `handle`.

    GET    /v1/local/accord                       → état : host_fp, fenêtre d'appairage, demandes (key8), appareils
    POST   /v1/local/accord/pair/open             → « Ajouter un appareil » : fenêtre de 2 min
    POST   /v1/local/accord/pair {code}           → code à 6 chiffres lu sur l'appareil → appareil appairé
    PATCH  /v1/local/accord/devices/{device_id} {name} → nom (≤ 40, NFC, sans contrôle ; vide = « ACCORD <key8> »)
    DELETE /v1/local/accord/devices/{device_id}   → révocation

Drapeau éteint (`local.accord.enabled` faux, défaut) : 404, aucun port ouvert, rien n'est même chargé."""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PREFIX = "/v1/local/accord"
_RE_DEVICE = re.compile(r"^/v1/local/accord/devices/(acc_[0-9a-f]{12})$")
_STATUS = {"pairing_closed": 409, "pairing_locked": 423, "bad_code": 400, "ambiguous": 409}


def is_path(path: str) -> bool:
    return path == PREFIX or path.startswith(PREFIX + "/")


def handle(handler: Any, method: str, path: str, hub: Any) -> None:
    from nokker.accord.hub import PairError

    if hub is None:
        handler._json(404, {"error": {"message": "not found"}})
        return
    try:
        if method == "GET" and path == PREFIX:
            handler._json(200, hub.status())
        elif method == "POST" and path == PREFIX + "/pair/open":
            handler._json(200, hub.open_window())
        elif method == "POST" and path == PREFIX + "/pair":
            body = handler._read_json_body() or {}
            handler._json(200, hub.confirm_code(body.get("code") if isinstance(body, dict) else None).to_json())
        elif method == "PATCH" and (m := _RE_DEVICE.match(path)):
            body = handler._read_json_body()
            if not isinstance(body, dict) or "name" not in body:
                handler._json(400, {"error": {"code": "bad_name", "message": "bad_name"}})
                return
            try:
                dev = hub.rename(m.group(1), body["name"])
            except ValueError:
                handler._json(400, {"error": {"code": "bad_name",
                                              "message": "nom refusé : 40 caractères au plus, sans caractère de contrôle"}})
                return
            if dev is None:
                handler._json(404, {"error": {"message": "appareil inconnu ou révoqué"}})
            else:
                handler._json(200, dev.to_json())
        elif method == "DELETE" and (m := _RE_DEVICE.match(path)):
            if hub.revoke(m.group(1)):
                handler._json(200, {"device_id": m.group(1), "revoked": True})
            else:
                handler._json(404, {"error": {"message": "appareil inconnu ou déjà révoqué"}})
        else:
            handler._json(404, {"error": {"message": "not found"}})
    except PairError as e:
        handler._json(_STATUS.get(e.code, 400), {"error": {"code": e.code, "message": e.code}})


def configure_accord(config: Any, approvals: Any, local_api: Any) -> Any:
    """Hub ACCORD de Lokkal, et son serveur WSS si `NOKKER_ACCORD_BIND` est posé. Drapeau éteint ou pas Lokkal : None."""
    import os

    from nokker.accord.hub import AccordHub
    from nokker.accord.server import AccordServer, ensure_cert, parse_bind, tls_context
    from nokker.accord.store import MemoryAccordStore, PgAccordStore

    raw = ((getattr(config, "local", None) or {}).get("accord") or {})
    if not raw.get("enabled", False) or local_api is None or approvals is None:
        return None
    state = Path(getattr(local_api, "state_dir", None) or "/opt/data")
    cert, key, host_fp = ensure_cert(state / "accord")
    dsn = config.pg_dsn() if hasattr(config, "pg_dsn") else ""
    hub = AccordHub(approvals, PgAccordStore(dsn) if dsn else MemoryAccordStore(), host_fp,
                    allow_test_key=bool(raw.get("allow_test_key", False)))
    if raw.get("allow_test_key"):
        logger.warning("accord : allow_test_key VRAI — la clé de test publique peut être appairée (démo, tests)")
    bind = parse_bind(os.environ.get("NOKKER_ACCORD_BIND", "") or str(raw.get("bind") or ""))
    hub.server = None
    if bind is None:
        logger.info("accord : actif sans port WSS (NOKKER_ACCORD_BIND absent)")
        return hub
    hub.server = AccordServer(hub, bind[0], bind[1], tls_context(cert, key)).start()
    port = hub.server.port
    try:  # annonce mDNS : extra optionnel `nokker[accord]` (zeroconf), sinon adresse saisie à la main
        from nokker.accord.mdns import announce

        hub.mdns = announce(port, host_fp, os.environ.get("NOKKER_ACCORD_LAN_IP", ""))
    except ImportError:
        logger.info("accord : zeroconf absent, pas d'annonce mDNS (adresse à saisir sur l'appareil)")
    return hub
