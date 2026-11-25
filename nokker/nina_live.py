"""Nina en direct (V6 live, docs/AVATAR.md §5) : Nina en vidéo temps réel (Runway Characters `gwm1_avatars`) avec la
voix de Nina (agent ElevenLabs), et **ce qu'elle dit vient du noyau Nokker**.

Flux :
  navigateur du titulaire ──POST /v1/nina-live/session──▶ noyau ──▶ ElevenLabs (URL signée de l'agent)
                                                           └──────▶ Runway (session, READY, consume)
  ◀── {session_id, livekit: {url, token}, thread_id, max_duration_s} : le jeton LiveKit éphémère de la salle,
      JAMAIS la clé Runway ni celle d'ElevenLabs.
  ElevenLabs (reconnaissance vocale) ──POST /v1/nina-live/llm/chat/completions (Custom LLM, SSE)──▶ noyau : un tour
  ORDINAIRE du titulaire (mêmes outils, cartes, portes), canal `relay` (une parole transcrite par un tiers ne vaut
  aucun GO textuel ; destructif, envoi et dépense refusés comme pour le relais), réponse nettoyée pour la voix.

Garde-fous :
- **titulaire seul** : le compte configuré (`nina_live.account`) ; tout autre compte : 403 `not_titular` ;
- **secret par agent** : la route Custom LLM n'accepte que `Authorization: Bearer <secret>` de l'agent configuré
  (comparaison à temps constant), lu dans une variable d'environnement, jamais dans la config ni un journal ;
- **plafond** : `max_session_s` (600 s) par session, transmis à Runway (`maxDuration`) ; une session à la fois ;
- **compteur** : secondes et coût estimé (0,20 $/min) par compte, en mémoire du processus, au journal ;
- drapeau `nina_live.enabled` (défaut false) : aucune route, aucun appel sortant.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("nokker.nina_live")

RUNWAY_URL = "https://api.dev.runwayml.com"
RUNWAY_VERSION = "2024-11-06"
ELEVENLABS_URL = "https://api.elevenlabs.io"
USD_PER_MIN = 0.20
LLM_PER_MIN = 30  # tours vocaux par minute au plus (route Custom LLM)
LLM_MAX_BODY = 256 * 1024  # corps d'une requête Custom LLM (l'historique d'ElevenLabs y est répété)
THREAD = "nina-live"
_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")

# Contexte des tours vocaux (texte vu par le modèle, revu : empreinte gelée en test).
VOICE_HINT = ("[Canal voix] Tu réponds à voix haute, en vidéo, dans la langue de la personne : une à trois phrases "
              "courtes, naturelles, sans liste, sans titre, sans lien ni code. Si la réponse est longue, dis "
              "l'essentiel et propose d'en dire plus. Une action à approuver attend sa carte sur son appareil : dis-le.")


class NinaLiveError(Exception):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status, self.code = status, code


@dataclass
class Session:
    id: str
    account: str
    started: float
    max_s: int
    thread: str = THREAD  # fil où Nina parle (choisi à l'ouverture, membre vérifié par le serveur)
    ended: float | None = None


@dataclass
class NinaLive:
    account: str
    agent_id: str
    avatar_id: str
    runway_key: Callable[[], str]
    elevenlabs_key: Callable[[], str]
    llm_secret: Callable[[], str]
    max_session_s: int = 600
    ready_timeout_s: float = 8.0
    runway_url: str = RUNWAY_URL
    elevenlabs_url: str = ELEVENLABS_URL
    opener: Callable[..., Any] = urllib.request.urlopen
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    _sessions: dict[str, Session] = field(default_factory=dict, repr=False)
    _seconds: dict[str, float] = field(default_factory=dict, repr=False)
    _llm_hits: list[float] = field(default_factory=list, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # Une ouverture à la fois : deux ouvertures simultanées (double clic, deux onglets) ne font jamais deux sessions.
    _opening: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # ── HTTP sortant : rien de la requête ni des clés dans les erreurs ──

    def _http(self, method: str, url: str, headers: dict[str, str], body: Any = None, timeout: float = 10.0) -> Any:
        data = body if isinstance(body, bytes) else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={**headers, **({"Content-Type": "application/json"} if data is not None else {})})
        try:
            with self.opener(req, timeout=timeout) as r:
                raw = r.read(1_000_000)
        except urllib.error.HTTPError as e:
            raise NinaLiveError(502, f"upstream_{e.code}") from None
        except Exception as e:  # noqa: BLE001
            raise NinaLiveError(502, f"upstream_{type(e).__name__}") from None
        return json.loads(raw or b"{}")

    def _runway(self, method: str, path: str, body: Any = None, bearer: str | None = None) -> Any:
        key = bearer or self.runway_key()
        if not key:
            raise NinaLiveError(503, "runway_key_missing")
        return self._http(method, self.runway_url + path, {"Authorization": f"Bearer {key}",
                                                           "X-Runway-Version": RUNWAY_VERSION}, body)

    # ── session ──

    def check_titular(self, account: str) -> None:
        if not account or account != self.account:
            raise NinaLiveError(403, "not_titular")

    def open(self, account: str, thread: str | None = None) -> dict[str, Any]:
        self.check_titular(account)
        thread = thread or THREAD
        if not _ID.match(thread):
            raise NinaLiveError(400, "bad_thread")
        if not self._opening.acquire(blocking=False):
            raise NinaLiveError(409, "session_busy")  # une autre ouverture est en cours
        try:
            self._take_over(account)
            return self._open(account, thread)
        finally:
            self._opening.release()

    def _take_over(self, account: str) -> None:
        """Reprise (r48.1) : le titulaire qui rouvre (onglet rechargé ou planté, sans `/end`) reprend la main. Sa
        session encore vivante est ANNULÉE chez Runway puis close au compteur, au lieu d'un 409 pendant le plafond.
        Si l'annulation échoue : 409 `session_busy`, et jamais deux sessions payées en même temps."""
        with self._lock:
            live = [s for s in self._sessions.values() if s.account == account and s.ended is None
                    and self.clock() - s.started < s.max_s]
        for old in live:
            if not self._cancel_strict(old.id):
                logger.warning("nina live : reprise impossible, annulation de %s refusée", old.id[:8])
                raise NinaLiveError(409, "session_busy")
            with self._lock:
                spent = 0.0
                if old.ended is None:
                    old.ended = self.clock()
                    spent = min(old.ended - old.started, old.max_s)
                    self._seconds[account] = self._seconds.get(account, 0.0) + spent
            logger.info("nina live : reprise — session %s annulée après %d s, nouvelle session", old.id[:8], spent)

    def _cancel_strict(self, sid: str) -> bool:
        """Annulation dont on sait si elle a eu lieu : 2xx, ou 404/410 (session déjà finie chez Runway) = vrai."""
        try:
            self._runway("DELETE", f"/v1/realtime_sessions/{sid}")
            return True
        except NinaLiveError as e:
            return e.code in ("upstream_404", "upstream_410")

    def _open(self, account: str, thread: str) -> dict[str, Any]:
        key = self.elevenlabs_key()
        if not key:
            raise NinaLiveError(503, "elevenlabs_key_missing")
        signed = self._http("GET", f"{self.elevenlabs_url}/v1/convai/conversation/get-signed-url?"
                            + urllib.parse.urlencode({"agent_id": self.agent_id}), {"xi-api-key": key})
        signed_url = str(signed.get("signed_url") or "")
        if not signed_url.startswith("wss://"):
            raise NinaLiveError(502, "elevenlabs_bad_signed_url")
        created = self._runway("POST", "/v1/realtime_sessions", {
            "model": "gwm1_avatars", "avatar": {"type": "custom", "avatarId": self.avatar_id},
            "integration": {"type": "elevenlabs", "signedUrl": signed_url}, "maxDuration": int(self.max_session_s)})
        sid = str(created.get("id") or "")
        if not _ID.match(sid):
            raise NinaLiveError(502, "runway_bad_session")
        deadline = self.clock() + self.ready_timeout_s
        got: dict[str, Any] = {}
        while True:
            got = self._runway("GET", f"/v1/realtime_sessions/{sid}")
            if got.get("status") != "NOT_READY" or self.clock() >= deadline:
                break
            self.sleep(0.4)
        if got.get("status") != "READY" or not got.get("sessionKey"):
            self._cancel(sid)
            raise NinaLiveError(504 if got.get("status") == "NOT_READY" else 502, "runway_not_ready")
        creds = self._runway("POST", f"/v1/realtime_sessions/{sid}/consume", b"", bearer=str(got["sessionKey"]))
        url, token = str(creds.get("url") or ""), str(creds.get("token") or "")
        if not url.startswith("wss://") or not token:
            self._cancel(sid)
            raise NinaLiveError(502, "runway_bad_credentials")
        with self._lock:
            self._sessions[sid] = Session(id=sid, account=account, started=self.clock(), max_s=int(self.max_session_s),
                                          thread=thread)
        logger.info("nina live : session ouverte pour %s (plafond %d s)", account, self.max_session_s)
        return {"session_id": sid, "livekit": {"url": url, "token": token}, "thread_id": thread,
                "max_duration_s": int(self.max_session_s)}

    def _cancel(self, sid: str) -> None:
        try:
            self._runway("DELETE", f"/v1/realtime_sessions/{sid}")
        except NinaLiveError:
            logger.info("nina live : annulation de session refusée ou déjà finie")

    def end(self, account: str, sid: str) -> dict[str, Any]:
        self.check_titular(account)
        with self._lock:
            s = self._sessions.get(sid)
            if s is None or s.account != account:
                raise NinaLiveError(404, "not_found")
            if s.ended is None:
                s.ended = self.clock()
                spent = min(s.ended - s.started, s.max_s)
                self._seconds[account] = self._seconds.get(account, 0.0) + spent
            else:
                spent = 0.0
        if spent:
            self._cancel(sid)
            logger.info("nina live : session finie, %d s (%.2f $ estimés)", spent, spent / 60 * USD_PER_MIN)
        return self.usage(account)

    def usage(self, account: str) -> dict[str, Any]:
        self.check_titular(account)
        with self._lock:
            now = self.clock()
            live = sum(min(now - s.started, s.max_s) for s in self._sessions.values()
                       if s.account == account and s.ended is None)
            sec = self._seconds.get(account, 0.0) + live
            n = sum(1 for s in self._sessions.values() if s.account == account)
        return {"sessions": n, "seconds": int(sec), "usd_estimate": round(sec / 60 * USD_PER_MIN, 2),
                "usd_per_min": USD_PER_MIN, "max_session_s": int(self.max_session_s)}

    def live_thread(self, account: str) -> str:
        """Le fil de la session en cours du titulaire (celui où Nina parle), sinon le fil par défaut."""
        with self._lock:
            now = self.clock()
            live = [s for s in self._sessions.values() if s.account == account and s.ended is None
                    and now - s.started < s.max_s]
        return live[-1].thread if live else THREAD

    # ── Custom LLM ──

    def llm_allowed(self) -> bool:
        """Débit de la route Custom LLM : `LLM_PER_MIN` tours par minute glissante (au-delà : 429)."""
        with self._lock:
            now = self.clock()
            self._llm_hits[:] = [t for t in self._llm_hits if now - t < 60]
            if len(self._llm_hits) >= LLM_PER_MIN:
                return False
            self._llm_hits.append(now)
            return True

    def llm_account(self, authorization: str) -> str | None:
        """Le titulaire si `authorization` porte le secret de l'agent, sinon None (temps constant)."""
        secret = self.llm_secret()
        token = authorization[7:].strip() if authorization.startswith("Bearer ") else ""
        if not secret or not token or not hmac.compare_digest(token.encode(), secret.encode()):
            return None
        return self.account


def _env(name: str) -> Callable[[], str]:
    return lambda: (os.environ.get(name, "") if name else "").strip()


def _file_or_env(path: str, name: str) -> Callable[[], str]:
    def read() -> str:
        if path:
            try:
                with open(path, encoding="utf-8") as f:
                    return f.read().strip()
            except OSError:
                return ""
        return _env(name)()

    return read


def from_config(raw: dict[str, Any] | None) -> NinaLive | None:
    """`nina_live:` de la config. Éteint (défaut), ou sans compte/agent/avatar : None (aucune route)."""
    raw = raw or {}
    if not raw.get("enabled", False):
        return None
    account, agent, avatar = (str(raw.get(k) or "") for k in ("account", "agent_id", "avatar_id"))
    if not (account and _ID.match(agent) and re.match(r"^[0-9a-f-]{36}$", avatar)):
        logger.warning("nina_live : account, agent_id ou avatar_id absent ou invalide — éteint")
        return None
    return NinaLive(account=account, agent_id=agent, avatar_id=avatar,
                    runway_key=_file_or_env(str(raw.get("runway_key_file") or ""),
                                            str(raw.get("runway_key_env") or "NOKKER_RUNWAY_API_KEY")),
                    elevenlabs_key=_env(str(raw.get("elevenlabs_key_env") or "NOKKER_ELEVENLABS_API_KEY")),
                    llm_secret=_env(str(raw.get("llm_secret_env") or "NOKKER_NINA_LIVE_LLM_SECRET")),
                    max_session_s=min(1800, max(30, int(raw.get("max_session_s", 600)))),
                    ready_timeout_s=float(raw.get("ready_timeout_s", 8.0)))


_svc: NinaLive | None = None


def configure(svc: NinaLive | None) -> None:
    global _svc
    _svc = svc


def service() -> NinaLive | None:
    return _svc


__all__ = ["THREAD", "USD_PER_MIN", "VOICE_HINT", "NinaLive", "NinaLiveError", "configure", "from_config", "service"]
