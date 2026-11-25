"""« Ton Nokker » (V6, docs/AVATAR.md) : le nom, la voix et l'avatar de l'assistant d'un compte, choisis à l'onboarding.

- **Catalogues fermés** : la voix est un id du catalogue `persona.voices` de la config (sinon la seule voix configurée,
  `nina`) ; l'id fournisseur (ElevenLabs) ne sort jamais du serveur. L'avatar est une entrée de `AVATARS` (Nina en
  calques 2D, servie en statique par l'app sous `/avatars/<id>-v<version>/`). Le nom suit une règle fermée (1 à 24 lettres,
  espaces, trait d'union, apostrophe). Toute autre valeur : 400 `bad_name` | `bad_voice` | `bad_avatar`.
- **Défauts** : sans choix enregistré, `GET /v1/persona` rend les valeurs par défaut (`defaulted: true`) ; sauter
  l'étape ne change rien.
- **Données** : table `account_persona` (085), une ligne par compte, sous RLS du locataire : couverte par `nokker erase`
  (balayage des tables à `tenant_id`) et par l'export du compte (clé `persona`).
- Drapeau `persona.enabled` (défaut false) : aucune route, aucune lecture ; la voix et la vue du mur restent celles
  d'avant.

Routes (session du compte ; jamais un jeton d'appareil, refusé avant par les barrières nkg_/nkw_) :
    GET  /v1/persona/catalog → {voices: [{id, label, langs}], avatars: [{id, version, label, url, thumb}],
                                name: {min, max}, defaults: {name, voice_id, avatar: {id, version}}}
    GET  /v1/persona         → {name, voice_id, avatar: {id, version}, defaulted}
    PUT  /v1/persona {name?, voice_id?, avatar?: {id, version}} → 200 même forme | 400 {error: {code}}
    POST /v1/persona/preview {voice_id?, lang?} → 200 parole horodatée d'une phrase fixe | 429 | 503 tts_off
"""
from __future__ import annotations

import re
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

NAME_MIN, NAME_MAX = 1, 24
_NAME = re.compile(r"^[^\W\d_](?:[^\W\d_]|[ '\-’])*$")
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
PREVIEW_MAX_PER_HOUR = 20

# Catalogue (docs/AVATAR.md §4) : Nina, l'illustration de Ninabot en calques 2D (`rig2d`, un dossier servi par l'app).
# `gltf` (modèle 3D) reste possible pour une entrée future ; aucun n'est proposé aujourd'hui.
AVATARS: tuple[dict[str, Any], ...] = (
    {"id": "nina", "version": 1, "kind": "rig2d", "label": {"fr": "Nina", "en": "Nina"}},
)

# Phrase d'aperçu dite par la voix choisie (texte fixe, passé par la revue des textes : empreinte gelée en test).
PREVIEW_TEXT = {
    "fr": "Bonjour, je suis {name}. Je suis là quand tu as besoin de moi.",
    "en": "Hello, I'm {name}. I'm here whenever you need me.",
}


class PersonaError(Exception):
    def __init__(self, status: int, code: str, retry_after: int | None = None) -> None:
        super().__init__(code)
        self.status, self.code, self.retry_after = status, code, retry_after


@dataclass(frozen=True)
class Voice:
    id: str
    label: str
    langs: tuple[str, ...]
    provider_ids: dict[str, str]  # langue -> id fournisseur ; vide = la voix configurée de `tts`

    def provider_id(self, lang: str) -> str | None:
        return self.provider_ids.get(lang) or self.provider_ids.get("*") or None


def avatar_json(a: dict[str, Any], lang: str = "fr") -> dict[str, Any]:
    stem = f"/avatars/{a['id']}-v{a['version']}"
    label = a["label"].get(lang) or a["label"]["fr"]
    kind = a.get("kind", "rig2d")
    return {"id": a["id"], "version": a["version"], "label": label, "kind": kind,
            "url": stem + ("/" if kind == "rig2d" else ".gltf"), "thumb": stem + ".webp"}


@dataclass
class Catalog:
    default_name: str = "Nina"
    voices: tuple[Voice, ...] = ()
    avatars: tuple[dict[str, Any], ...] = AVATARS

    @property
    def default_voice(self) -> str:
        return self.voices[0].id

    @property
    def default_avatar(self) -> dict[str, Any]:
        return {"id": self.avatars[0]["id"], "version": self.avatars[0]["version"]}

    def voice(self, voice_id: str) -> Voice | None:
        return next((v for v in self.voices if v.id == voice_id), None)

    def avatar(self, avatar_id: str, version: Any) -> dict[str, Any] | None:
        return next((a for a in self.avatars if a["id"] == avatar_id and a["version"] == version), None)

    def to_json(self, lang: str = "fr") -> dict[str, Any]:
        return {"voices": [{"id": v.id, "label": v.label, "langs": list(v.langs)} for v in self.voices],
                "avatars": [avatar_json(a, lang) for a in self.avatars],
                "name": {"min": NAME_MIN, "max": NAME_MAX},
                "defaults": {"name": self.default_name, "voice_id": self.default_voice,
                             "avatar": self.default_avatar}}


def catalog_from_config(raw: dict[str, Any] | None, tts: dict[str, Any] | None = None) -> Catalog:
    """`persona:` de la config. `voices: [{id, label, langs?, elevenlabs?: {fr: …, en: …}}]` ; absent : une seule voix
    `nina`, celle de `tts` (aucun id fournisseur copié : la synthèse garde sa voix configurée)."""
    raw = raw or {}
    name = normalize_name(str(raw.get("default_name") or "Nina")) or "Nina"
    voices: list[Voice] = []
    for v in raw.get("voices") or []:
        if not isinstance(v, dict) or not _ID.match(str(v.get("id") or "")):
            continue
        ids = {str(k): str(x) for k, x in (v.get("elevenlabs") or {}).items() if re.match(r"^[A-Za-z0-9]{1,64}$", str(x))}
        langs = tuple(str(x) for x in (v.get("langs") or ids.keys() or ("fr", "en")) if re.match(r"^[a-z]{2}$", str(x)))
        voices.append(Voice(id=str(v["id"]), label=str(v.get("label") or v["id"])[:40], langs=langs or ("fr",),
                            provider_ids=ids))
    if not voices:
        el = (tts or {}).get("elevenlabs") or {}
        langs = tuple(sorted(k for k in (el.get("voices") or {}) if re.match(r"^[a-z]{2}$", str(k)))) or ("fr", "en")
        voices = [Voice(id="nina", label=name, langs=langs, provider_ids={})]
    return Catalog(default_name=name, voices=tuple(voices))


def normalize_name(name: Any) -> str | None:
    if not isinstance(name, str):
        return None
    n = " ".join(name.split())
    if not (NAME_MIN <= len(n) <= NAME_MAX) or not _NAME.match(n):
        return None
    return n


class MemoryPersonaStore:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    def get(self, account_id: str) -> dict[str, Any] | None:
        r = self.rows.get(account_id)
        return dict(r) if r else None

    def put(self, account_id: str, tenant_id: str, row: dict[str, Any], now: float) -> None:
        self.rows[account_id] = {**row, "tenant_id": tenant_id, "updated_at": now}


class PgPersonaStore:
    """Table `account_persona` (085) ; la RLS du locataire courant fait la frontière (`tenant_id` par défaut = le
    locataire de la connexion, `nokker.store.conn`)."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def _conn(self):  # noqa: ANN202
        from nokker.store.conn import connect

        return connect(self.dsn)

    def get(self, account_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute("SELECT name, voice_id, avatar_id, avatar_version, updated_at FROM account_persona "
                               "WHERE account_id = %s", (account_id,)).fetchone()
        if row is None:
            return None
        return {"name": row[0], "voice_id": row[1], "avatar_id": row[2], "avatar_version": row[3], "updated_at": row[4]}

    def put(self, account_id: str, tenant_id: str, row: dict[str, Any], now: float) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO account_persona (account_id, name, voice_id, avatar_id, avatar_version, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (account_id) DO UPDATE SET "
                "name = EXCLUDED.name, voice_id = EXCLUDED.voice_id, avatar_id = EXCLUDED.avatar_id, "
                "avatar_version = EXCLUDED.avatar_version, updated_at = EXCLUDED.updated_at",
                (account_id, row["name"], row["voice_id"], row["avatar_id"], row["avatar_version"], now))


@dataclass
class PersonaService:
    catalog: Catalog
    store: Any
    clock: Callable[[], float] = time.time
    _previews: dict[str, deque] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def get(self, account_id: str) -> dict[str, Any]:
        """Le choix du compte, valeurs hors catalogue (préréglage retiré) remplacées par les défauts."""
        c = self.catalog
        row = self.store.get(account_id)
        if row is None:
            return {"name": c.default_name, "voice_id": c.default_voice, "avatar": c.default_avatar, "defaulted": True}
        voice = row["voice_id"] if c.voice(row["voice_id"] or "") else c.default_voice
        av = c.avatar(row["avatar_id"] or "", row["avatar_version"])
        return {"name": normalize_name(row["name"]) or c.default_name, "voice_id": voice,
                "avatar": {"id": av["id"], "version": av["version"]} if av else c.default_avatar, "defaulted": False}

    def put(self, account_id: str, tenant_id: str, body: Any) -> dict[str, Any]:
        if not isinstance(body, dict) or not body or set(body) - {"name", "voice_id", "avatar"}:
            raise PersonaError(400, "bad_request")
        cur = self.get(account_id)
        name, voice, avatar = cur["name"], cur["voice_id"], cur["avatar"]
        if "name" in body:
            name = normalize_name(body["name"])
            if name is None:
                raise PersonaError(400, "bad_name")
        if "voice_id" in body:
            if not isinstance(body["voice_id"], str) or self.catalog.voice(body["voice_id"]) is None:
                raise PersonaError(400, "bad_voice")
            voice = body["voice_id"]
        if "avatar" in body:
            a = body["avatar"]
            found = self.catalog.avatar(a.get("id", ""), a.get("version")) if isinstance(a, dict) \
                and isinstance(a.get("id"), str) and type(a.get("version")) is int else None
            if found is None:
                raise PersonaError(400, "bad_avatar")
            avatar = {"id": found["id"], "version": found["version"]}
        self.store.put(account_id, tenant_id, {"name": name, "voice_id": voice, "avatar_id": avatar["id"],
                                               "avatar_version": avatar["version"]}, self.clock())
        return self.get(account_id)

    def provider_voice(self, account_id: str, lang: str) -> str | None:
        """L'id fournisseur de la voix choisie pour `lang` ; None = la voix configurée de `tts`."""
        v = self.catalog.voice(self.get(account_id)["voice_id"])
        return v.provider_id(lang) if v is not None else None

    def preview_text(self, account_id: str, body: Any) -> tuple[str, str, str | None]:
        """(texte, langue, voix fournisseur) de l'aperçu ; débit par compte (`PREVIEW_MAX_PER_HOUR`)."""
        body = body if isinstance(body, dict) else {}
        lang = body.get("lang") if body.get("lang") in PREVIEW_TEXT else "fr"
        voice_id = body.get("voice_id", self.get(account_id)["voice_id"])
        v = self.catalog.voice(voice_id) if isinstance(voice_id, str) else None
        if v is None:
            raise PersonaError(400, "bad_voice")
        with self._lock:
            now = self.clock()
            q = self._previews.setdefault(account_id, deque())
            while q and q[0] <= now - 3600:
                q.popleft()
            if len(q) >= PREVIEW_MAX_PER_HOUR:
                raise PersonaError(429, "rate_limited", retry_after=max(1, int(q[0] + 3600 - now) + 1))
            q.append(now)
        name = self.get(account_id)["name"]
        return PREVIEW_TEXT[lang].format(name=name), lang, v.provider_id(lang)

    def export(self, account_id: str) -> dict[str, Any] | None:
        row = self.store.get(account_id)
        if row is None:
            return None
        return {"name": row["name"], "voice_id": row["voice_id"],
                "avatar": {"id": row["avatar_id"], "version": row["avatar_version"]}}


def from_config(raw: dict[str, Any] | None, tts: dict[str, Any] | None, dsn: str | None) -> PersonaService | None:
    if not (raw or {}).get("enabled", False):
        return None
    return PersonaService(catalog=catalog_from_config(raw, tts),
                          store=PgPersonaStore(dsn) if dsn else MemoryPersonaStore())


_svc: PersonaService | None = None


def configure(svc: PersonaService | None) -> None:
    global _svc
    _svc = svc


def service() -> PersonaService | None:
    return _svc


__all__ = ["AVATARS", "NAME_MAX", "NAME_MIN", "PREVIEW_TEXT", "Catalog", "MemoryPersonaStore", "PersonaError",
           "PersonaService", "PgPersonaStore", "Voice", "avatar_json", "catalog_from_config", "configure",
           "from_config", "normalize_name", "service"]
