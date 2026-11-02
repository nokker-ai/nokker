"""Câblage de l'ambiant (docs/AMBIENT.md §6) depuis le bloc `ambient` de la config. Éteint par défaut :

    ambient:
      room_audio:
        enabled: false        # voix de la pièce (Lokkal + passerelle du foyer)
        max_chars: 400
        quiet_hours: "22:00-07:00"
        rooms: {salon: {}, cuisine: {}}     # pièces connues (la table complète vit côté passerelle)
      glance:
        enabled: false        # vue lunettes en lecture seule (jeton glance, cloud et Lokkal)
      eink:
        enabled: false        # écrans e-ink du foyer (cloud et Lokkal)
        public_url: ""        # adresse publique du noyau, pour image_url (TRMNL) ; vide = Host de la requête
        publish_interval_s: 60  # Lokkal + passerelle : passe de publication

Rien n'est construit tant que `enabled` est faux : aucune route, aucun fil, aucune lecture de la passerelle.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from nokker.ambient import http as ambient_http

logger = logging.getLogger("nokker.ambient")


def build(raw: dict[str, Any] | None, *, scrub: Callable[[str], str], tts_raw: dict[str, Any] | None, dsn: str | None,
          bridge: Any = None, household_name: Callable[[str, str], str] | None = None,
          start_threads: bool = True) -> ambient_http.Ambient | None:
    raw = raw or {}
    audio_raw = raw.get("room_audio") or {}
    eink_raw = raw.get("eink") or {}
    glance_raw = raw.get("glance") or {}
    if not audio_raw.get("enabled") and not eink_raw.get("enabled") and not glance_raw.get("enabled"):
        return None
    from nokker.ambient.frame import FrameRenderer

    out = ambient_http.Ambient(public_url=str(eink_raw.get("public_url") or ""))
    if audio_raw.get("enabled"):
        if bridge is None:
            logger.warning("ambient.room_audio allumé sans passerelle du foyer (NOKKER_HOME_BRIDGE_URL) : voix éteinte")
        else:
            from nokker.ambient.room_audio import from_config
            from nokker.voice.tts import provider_from_config

            tts = provider_from_config(tts_raw or {}) if (tts_raw or {}).get("provider") else None
            if tts is None:
                logger.warning("ambient.room_audio : aucun fournisseur de voix (bloc tts) — seul l'audio fourni passe")
            from nokker.voice import speak_lexicon

            sl = speak_lexicon.service()  # None (drapeau éteint) : la voix de la pièce reste telle quelle
            out.room_audio = from_config(audio_raw, bridge=bridge, tts=tts, scrub=scrub,
                                         prepare=sl.prepare if sl is not None else None)
            if sl is not None:
                out.room_audio.speak_scope = sl.default_scope
    store = None
    if eink_raw.get("enabled") or glance_raw.get("enabled"):
        from nokker.ambient.displays import MemoryDisplayStore, PgDisplayStore

        store = PgDisplayStore(dsn) if dsn else MemoryDisplayStore()
    if glance_raw.get("enabled"):
        from nokker.ambient.glance import Glance

        out.glance = Glance(store=store)
    if eink_raw.get("enabled"):
        from nokker.ambient.displays import Displays

        displays = Displays(store=store, renderer=FrameRenderer(scrub=scrub))
        if household_name is not None:
            displays.household_name = household_name
        if bridge is not None:
            from nokker.ambient.publisher import EinkPublisher

            pub = EinkPublisher(displays=displays, bridge=bridge,
                                interval_s=float(eink_raw.get("publish_interval_s", 60)))
            displays.on_revoke = pub.revoked
            displays.on_board = pub.wake
            displays.setup_offer = lambda mac_sha, token, ttl: bridge.op(
                "eink.setup_offer", {"mac_sha256": mac_sha, "token": token, "ttl_s": ttl})
            if start_threads:
                pub.start()
        out.displays = displays
    return out


__all__ = ["build"]
