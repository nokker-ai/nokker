"""Branchement des canaux Slack et WhatsApp au démarrage (`api/server.py::serve`, après Telegram).

Config (`NokkerConfig.slack` / `.whatsapp`, docs/CHANNELS.md §4) : un bloc absent, ou `enabled: false` (défaut), et
le canal n'existe pas — aucune route, aucun appel au fournisseur. Les secrets sont des RÉFÉRENCES `secret:NOM`
(coffre système du bloc : `secrets: {provider: file|openbao, file: {…}, openbao: {…}}`, `state_dir`) ; une valeur en
clair dans la config refuse le canal (journalisé). Chaque valeur est relue au coffre (cache 60 s) et rejoint les
valeurs connues du caviardage (`[secret:nokker:<NOM>]`).
"""
from __future__ import annotations

import importlib.util
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from nokker.channels import http as channels_http
from nokker.channels.common import ChannelCardNotifier, ChannelCore, Dispatcher, FanoutNotifier, \
    FasterWhisperTranscriber, MemoryDedup, MemoryPairing, MemoryWindows, PgChannelPairing, PgDedup, PgWindows, \
    RateLimiter

logger = logging.getLogger("nokker.channels.setup")

SLACK_SECRETS = ("bot_token", "signing_secret")
WHATSAPP_SECRETS = ("access_token", "app_secret", "verify_token")


class ChannelConfigError(ValueError):
    pass


def _ref(block: dict[str, Any], key: str, channel: str) -> str:
    from nokker.secrets.refs import ref_name

    raw = block.get(key)
    try:
        name = ref_name(raw)
    except Exception as e:  # noqa: BLE001 — nom invalide
        raise ChannelConfigError(f"{channel}.{key} : référence invalide ({e})") from None
    if name is None:
        raise ChannelConfigError(f"{channel}.{key} doit être une référence secret:NOM (jamais une valeur en clair)")
    return name


class _Lazy:
    """Valeur d'un secret relue au coffre au plus toutes les `ttl_s` secondes ; ajoutée aux valeurs connues."""

    def __init__(self, resolver: Any, name: str, known: Any, ttl_s: float = 60.0) -> None:
        self.resolver, self.name, self.known, self.ttl_s = resolver, name, known, ttl_s
        self._value, self._at = "", 0.0
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return f"secret:{self.name}"

    def __call__(self) -> str:
        with self._lock:
            if not self._value or time.monotonic() - self._at > self.ttl_s:
                try:
                    value = str(self.resolver.value(self.name))
                except Exception as e:  # noqa: BLE001 — coffre indisponible : on garde la dernière valeur connue
                    if not self._value:
                        logger.error("canal : secret %s illisible (%s)", self.name, type(e).__name__)
                        return ""
                    return self._value
                self._value, self._at = value, time.monotonic()
                if self.known is not None:
                    self.known.add(f"nokker:{self.name}", value)
            return self._value


def _resolver(block: dict[str, Any]) -> Any:
    from pathlib import Path

    from nokker.secrets.refs import SecretResolver
    from nokker.secrets.vault import build_provider, chosen_provider

    scfg = dict(block.get("secrets") or {})
    state_dir = Path(str(block.get("state_dir") or "~/.config/nokker/channels")).expanduser()
    return SecretResolver(build_provider(chosen_provider(scfg), "system", scfg, state_dir))


def _transcriber(block: dict[str, Any], channel: str) -> Any:
    voice = dict(block.get("voice") or {})
    if not voice.get("enabled"):
        return None
    if importlib.util.find_spec("faster_whisper") is None:
        logger.warning("%s.voice.enabled mais faster-whisper absent (extra stt-whisper) : notes vocales refusées",
                       channel)
        return None
    return FasterWhisperTranscriber(model=str(voice.get("model") or "small"))


def _run_turn(kernel: Any, hub: Any) -> Callable[[Any], Any]:
    def run_turn(inp: Any) -> Any:  # même chemin que l'API et Telegram : fil du hub, verrou du fil
        if hub is not None:
            hub.ensure_thread_for_turn(inp.thread_id, inp.sender, inp.user_text)
            with hub.thread_lock(inp.thread_id):
                return kernel.run_turn(inp)
        return kernel.run_turn(inp)

    return run_turn


def build_channels(config: Any, tenancy: Any, kernel: Any, hub: Any, *, resolver: Any = None,
                   slack_api: Any = None, whatsapp_api: Any = None, sync: bool = False,
                   transcriber: Any = None) -> tuple[Any, Any, list[Any]]:
    """(SlackEndpoint | None, WhatsAppEndpoint | None, [send_card par canal]). `resolver`, `*_api`, `sync`,
    `transcriber` : injection des tests (aucun appel réseau)."""
    from nokker.channels.slack import SlackAdapter, SlackApi
    from nokker.channels.whatsapp import WhatsAppAdapter, WhatsAppApi

    dsn = config.pg_dsn() if hasattr(config, "pg_dsn") else ""
    scrub = kernel.invariants.scrub_output
    known = getattr(kernel.invariants, "known", None)
    run_turn = _run_turn(kernel, hub)
    out_slack = out_wa = None
    cards: list[tuple[str, Callable[..., None]]] = []

    sb = dict(getattr(config, "slack", None) or {})
    if sb.get("enabled"):
        try:
            names = {k: _ref(sb, k, "slack") for k in SLACK_SECRETS}
            team_id = str(sb.get("team_id") or "")
            if not team_id:
                raise ChannelConfigError("slack.team_id requis")
            res = resolver or _resolver(sb)
            sec = {k: _Lazy(res, n, known) for k, n in names.items()}
            core = ChannelCore(channel="slack", display_name="Slack", tenancy=tenancy,
                               pairing=PgChannelPairing(dsn, "slack") if dsn else MemoryPairing(),
                               run_turn=run_turn, scrub=scrub,
                               limiter=RateLimiter(float(sb.get("rate_per_minute", 20)), int(sb.get("rate_burst", 10))),
                               transcriber=transcriber if transcriber is not None else _transcriber(sb, "slack"),
                               default_lang="en" if sb.get("lang") == "en" else "fr")
            adapter = SlackAdapter(core=core, api=slack_api or SlackApi(token=sec["bot_token"]), team_id=team_id,
                                   app_id=str(sb.get("app_id") or ""), bot_user_id=str(sb.get("bot_user_id") or ""),
                                   channel_mentions=bool(sb.get("channel_mentions", True)),
                                   max_voice_bytes=int((sb.get("voice") or {}).get("max_bytes", 8_000_000)),
                                   dedup=PgDedup(dsn) if dsn else MemoryDedup(),
                                   dispatcher=Dispatcher(sync=sync, name="nokker-slack"))
            out_slack = channels_http.SlackEndpoint(adapter=adapter, pairing=core.pairing,
                                                    signing_secret=sec["signing_secret"], app_id=adapter.app_id,
                                                    team_id=team_id)
            cards.append(("slack", adapter.send_card))
        except ChannelConfigError as e:
            logger.error("canal Slack désactivé : %s", e)

    wb = dict(getattr(config, "whatsapp", None) or {})
    if wb.get("enabled"):
        try:
            names = {k: _ref(wb, k, "whatsapp") for k in WHATSAPP_SECRETS}
            phone_id = str(wb.get("phone_number_id") or "")
            if not phone_id.isdigit():
                raise ChannelConfigError("whatsapp.phone_number_id requis (identifiant numérique Meta)")
            res = resolver or _resolver(wb)
            sec = {k: _Lazy(res, n, known) for k, n in names.items()}
            core = ChannelCore(channel="whatsapp", display_name="WhatsApp", tenancy=tenancy,
                               pairing=PgChannelPairing(dsn, "whatsapp") if dsn else MemoryPairing(),
                               run_turn=run_turn, scrub=scrub,
                               limiter=RateLimiter(float(wb.get("rate_per_minute", 20)), int(wb.get("rate_burst", 10))),
                               transcriber=transcriber if transcriber is not None else _transcriber(wb, "whatsapp"),
                               default_lang="en" if wb.get("lang") == "en" else "fr")
            api = whatsapp_api or WhatsAppApi(phone_number_id=phone_id, token=sec["access_token"],
                                              version=str(wb.get("graph_version") or "v21.0"))
            adapter = WhatsAppAdapter(core=core, api=api, phone_number_id=phone_id,
                                      windows=PgWindows(dsn) if dsn else MemoryWindows(),
                                      dedup=PgDedup(dsn) if dsn else MemoryDedup(),
                                      dispatcher=Dispatcher(sync=sync, name="nokker-whatsapp"),
                                      card_template=dict(wb["card_template"]) if wb.get("card_template") else None,
                                      max_voice_bytes=int((wb.get("voice") or {}).get("max_bytes", 8_000_000)),
                                      max_age_s=float(wb.get("max_age_s", 24 * 3600)))
            out_wa = channels_http.WhatsAppEndpoint(adapter=adapter, pairing=core.pairing,
                                                    app_secret=sec["app_secret"], verify_token=sec["verify_token"],
                                                    display_number=str(wb.get("display_number") or ""))
            cards.append(("whatsapp", adapter.send_card))
        except ChannelConfigError as e:
            logger.error("canal WhatsApp désactivé : %s", e)
    return out_slack, out_wa, cards


def configure_channels(config: Any, tenancy: Any, kernel: Any, hub: Any, approvals: Any = None, **inject: Any) -> None:
    """Routes et notificateurs des canaux. Les cartes d'un testeur partent aussi vers ses comptes Slack/WhatsApp liés,
    à côté de Telegram (`FanoutNotifier` derrière `approvals.tester_notifier`), si `app_url` est posé."""
    slack_ep, wa_ep, cards = build_channels(config, tenancy, kernel, hub, **inject)
    channels_http.configure(slack_ep, wa_ep)
    app_url = str(getattr(config, "app_url", "") or "")
    if approvals is None or not cards or not app_url:
        return
    scrub = kernel.invariants.scrub_output
    notifiers = [n for n in [getattr(approvals, "tester_notifier", None)] if n is not None]
    notifiers += [ChannelCardNotifier(tenancy=tenancy, channel=ch, send_card=send, app_url=app_url, scrub=scrub)
                  for ch, send in cards]
    approvals.tester_notifier = FanoutNotifier(notifiers)


__all__ = ["ChannelConfigError", "build_channels", "configure_channels"]
