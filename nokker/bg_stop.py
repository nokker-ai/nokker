"""Registre des ressources de fond (serveurs, boucles, fils) qui offrent un arrêt idempotent `close()` / `stop()`.

Production : inchangée (`track` ne fait qu'inscrire une référence faible ; rien n'est arrêté sans appel explicite à
`stop_all`). Tests : la fixture de nettoyage de `tests/conftest.py` appelle `stop_all()` après chaque test, pour qu'aucun
fil ou serveur ne survive à son test. Une ressource déjà arrêtée ou ramassée est sans effet."""
from __future__ import annotations

import logging
import threading
import weakref
from typing import Any

logger = logging.getLogger("nokker.bg_stop")

_LOCK = threading.Lock()
_LIVE: dict[int, tuple[weakref.ref, str, Any]] = {}


def track(obj: Any, stop: str = "close", *, hold: bool = False) -> Any:
    """Inscrit `obj` (référence faible, ou forte avec `hold=True` pour un objet que rien d'autre ne retient : un par
    process en production) ; `stop` = nom de sa méthode d'arrêt idempotente. Rend `obj`."""
    key = id(obj)

    def gone(_r: weakref.ref, key: int = key) -> None:
        with _LOCK:
            hit = _LIVE.get(key)
            if hit is not None and hit[0] is _r:
                del _LIVE[key]

    try:
        ref = weakref.ref(obj, gone)
    except TypeError:
        return obj
    with _LOCK:
        _LIVE[key] = (ref, stop, obj if hold else None)
    return obj


def stop_all() -> int:
    """Arrête toutes les ressources inscrites (idempotent) ; rend leur nombre. Ne lève jamais."""
    with _LOCK:
        items = list(_LIVE.values())
        _LIVE.clear()
    n = 0
    for ref, stop, _hold in items:
        obj = ref()
        if obj is None:
            continue
        try:
            getattr(obj, stop)()
            n += 1
        except Exception:  # noqa: BLE001 — le nettoyage ne masque pas le test
            logger.debug("bg_stop: arrêt en échec", exc_info=True)
    return n


__all__ = ["stop_all", "track"]
