"""Nœud téléphone, côté nokker.ai (P7a, docs/LOKKAL-PHONE.md §3) : un téléphone du membre exécute des opérations
NOMMÉES et FERMÉES pour son compte. Ni tour, ni session, ni coffre.

- Drapeaux : `cloud_link.phone_nodes` (éteint par défaut) ; `cloud_link.phone_nodes_unattested` (liste de comptes ou
  `["*"]`, QA et équipe) permet une inscription sans attestation, sinon 400 `attestation_required`. Un nœud
  `attested: false` ne fait QUE des opérations de lecture (toutes celles de la phase S le sont).
- Inscription : `POST /v1/link/phone` (session du membre) → carte FORTE `phone:enroll` (nom, empreinte de la clé :
  8 premiers caractères du sha256), signée sur un AUTRE appareil enrôlé du membre ; `GET /v1/link/phone/{approval_id}`
  rend le jeton UNE fois (202 en attente, 201 puis 410, 403 refusée).
- Preuve : `X-Nokker-Node-Proof: v1;ts=<s>;nonce=<…>;sig=<b64url>`, ES256 (clé P-256 du nœud) sur
  `method\\nhost\\npath?query\\nts\\nnonce\\nsha256hex(corps)` ; ±60 s ; nonce déjà vu dans les 120 s refusé ; sinon
  401 `proof_invalid`. Le jeton seul ne vaut rien, et il ne vaut QUE sur /v1/gateway/phone/* (403 ailleurs).
- Battement (`POST /v1/gateway/phone/beat`, ≤ 4 Kio, 3/min) : en ligne si ≤ 180 s et `reach: live`.
- File `phone_tasks` : dépôt interne, relève en long poll (`GET /v1/gateway/phone/tasks?wait=25`, 10/min), bail de
  60 s (`POST …/{id}/lease`), résultat idempotent (`POST …/{id}/result`, texte ≤ 64 Kio, 60/min), échéance puis
  `expired`. 120 tâches par heure et par compte.
- Opérations fermées (lecture) : `zone.state`, `calendar.list` (forme de P8), `ocr.image {image_url}`,
  `stt.audio {audio_url}`. L'URL d'un média est signée pour 2 min, à usage unique et liée au nœud.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

PHONE_ENROLL_TOOL = "phone_register"  # carte forte d'inscription : un geste du noyau (contrat de front)
PHONE_ENROLL_RULE = "phone:enroll"
PROOF_HEADER = "X-Nokker-Node-Proof"
PROOF_SKEW_S = 60
NONCE_TTL_S = 120
FRESH_S = 180
BEAT_MAX_BYTES = 4096
LEASE_S = 60
RESULT_MAX = 64 * 1024
MEDIA_TTL_S = 120
TOKEN_PICKUP_TTL_S = 600
MAX_PHONES = 3  # par membre
READ_OPS = frozenset({"zone.state", "calendar.list", "ocr.image", "stt.audio", "fetch.get"})
FETCH_RESULT_MAX = 1024 * 1024  # fetch.get (P11) SEULEMENT ; les autres opérations restent à 64 Kio
# Nœud maison (P10a, nokker/cloud/phone_home.py) : opérations de home-bridge (mêmes noms, arguments, résultats).
# Exclues : ha.write_config, ha.delete_config, ha.camera_show. États, registre, historique, journal à 256 Kio ; une action
# n'atteint le téléphone qu'avec l'attestation de sa décision maison (`origin.decision`, JWT `nokker-phone-task`).
HA_READ_OPS = frozenset({"ha.states", "ha.registry", "ha.query", "ha.history", "ha.logbook", "ha.events"})
HA_ACTION_OPS = frozenset({"ha.call_service", "ha.notify", "ha.tracker_see"})
HA_EXCLUDED_OPS = frozenset({"ha.write_config", "ha.delete_config", "ha.camera_show"})
HA_RESULT_MAX = 256 * 1024
# Caméra de veille du nœud maison (P10c) : lectures. `camera.thumb_url` rend une URL de vignette sur le LAN et sa clé :
# ce résultat, comme les événements de `camera.watch_event`, n'est JAMAIS écrit en base ni journalisé : il ne vit qu'en
# mémoire (30 s) jusqu'à sa lecture par le noyau.
CAMERA_OPS = frozenset({"camera.watch_event", "camera.thumb_url"})
EPHEMERAL_OPS = frozenset({"camera.thumb_url", "camera.watch_event", "lan.discover"})  # rien de gardé au cloud
# Découverte du réseau de la maison (P10d) : liste FERMÉE de types Bonjour ; le noyau refuse tout autre type.
LAN_TYPES = ("_googlecast._tcp", "_airplay._tcp", "_raop._tcp", "_home-assistant._tcp", "_spotify-connect._tcp",
             "_sonos._tcp")
LAN_TIMEOUT_MAX_S = 5
EPHEMERAL_TTL_S = 30
CAMERA_LIMIT_MAX = 200
_CAMERA_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
# 256 Kio pour ces lectures seulement (phoneResultMax du SDK, `BIG_HA`) ; ha.query, ha.events et les actions : 64 Kio
HA_BIG_OPS = frozenset({"ha.states", "ha.registry", "ha.history", "ha.logbook"})
# Jamais une action `direct` (sans carte forte), quelle que soit la politique : la liste sensible du cloud (P10a).
NEVER_DIRECT_DOMAINS = frozenset({"lock", "alarm_control_panel", "valve", "siren", "camera", "script", "automation"})
# `cover` : sa classe d'appareil (état HA, lue par la source au moment de la décision) ; garage, porte, portail ou
# classe absente : jamais en direct (fail-closed).
NEVER_DIRECT_COVER_CLASSES = frozenset({"garage", "door", "gate"})
HA_EVENTS_MAX_S = 20
ATTESTATION_TTL_S = 120
STATUSES = ("queued", "leased", "done", "failed", "expired", "cancelled")
CAPS = ("location", "lan_home", "device_calendar", "camera", "ocr", "stt", "egress")
CAP_OF_OP = {"zone.state": "location", "calendar.list": "device_calendar", "ocr.image": "ocr", "stt.audio": "stt",
             "fetch.get": "egress", **{op: "lan_home" for op in HA_READ_OPS | HA_ACTION_OPS},
             **{op: "camera" for op in CAMERA_OPS}, "lan.discover": "lan_home"}
ALL_OPS = READ_OPS | HA_READ_OPS | HA_ACTION_OPS | CAMERA_OPS | {"lan.discover"}
_NAME = re.compile(r"^[^\x00-\x1f\x7f]{1,40}$")
_NONCE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_TASK = re.compile(r"^pt_[0-9a-f]{16}$")
ENROLL_FIELDS = frozenset({"name", "platform", "os_version", "app_version", "public_key_spki", "enc_key_spki",
                           "attestation", "key_storage", "push_token"})
KEY_STORAGES = ("secure_enclave", "strongbox", "software")
CAPS_ALLOWED = ("location", "device_calendar", "ocr", "stt", "lan_home", "camera")  # phase S, nœud maison (P10a, P10c)
CARD_TTL_S = 600


def _iso(ts: float | None) -> str | None:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(ts, tz=UTC).isoformat().replace("+00:00", "Z") if ts else None


def _null():
    from contextlib import nullcontext

    return nullcontext()


def err(code: str, message: str = "") -> dict[str, Any]:
    """Forme d'erreur du contrat de front : `{error: {code, message}}`."""
    return {"error": {"code": code, "message": message or code}}


class PhoneError(Exception):
    def __init__(self, status: int, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.status, self.code = status, code


@dataclass(frozen=True)
class PhoneNode:
    machine_id: str
    account: str
    platform: str
    attested: bool
    key_storage: str
    pubkey_spki: str
    key_fp: str
    created_at: float
    beat: dict[str, Any] = field(default_factory=dict)
    beat_at: float | None = None

    def online(self, now: float) -> bool:
        return self.beat_at is not None and now - self.beat_at <= FRESH_S and self.beat.get("reach") == "live"

    def has_cap(self, cap: str) -> bool:
        return (self.beat.get("caps") or {}).get(cap) == "on"


@dataclass(frozen=True)
class PhoneTask:
    task_id: str
    machine_id: str
    account: str
    op: str
    args: dict[str, Any]
    args_sha256: str
    deadline: float
    created_at: float
    status: str = "queued"
    lease_until: float | None = None
    attempts: int = 0
    result: str | None = None
    error: dict[str, Any] | None = None
    origin: dict[str, Any] = field(default_factory=lambda: {"kind": "turn"})

    def json(self) -> dict[str, Any]:
        """Forme d'une tâche sur le fil (contrat de front, packages/mock-server/src/phone.ts::wire)."""
        return {"task_id": self.task_id, "op": self.op, "args": self.args, "args_sha256": self.args_sha256,
                "origin": self.origin, "heavy": self.op == "stt.audio", "deadline": _iso(self.deadline),
                "lease_until": _iso(self.lease_until)}


# ── nettoyage ──
def _b64any(text: str) -> bytes:
    """base64url (SDK, WebCrypto) ou base64 standard, avec ou sans bourrage."""
    t = str(text or "").strip()
    pad = "=" * (-len(t) % 4)
    return base64.urlsafe_b64decode(t + pad) if ("-" in t or "_" in t) else base64.b64decode(t + pad, validate=True)


def key_fingerprint(spki_b64: str) -> str:
    return hashlib.sha256(_b64any(spki_b64)).hexdigest()


def seal_token(enc_spki_b64: str, approval_id: str, token: str) -> dict[str, Any]:
    """Jeton du nœud SCELLÉ pour sa clé d'ACCORD (`enc_key_spki`, distincte de la clé de signature), même construction
    que `sealToken` du SDK (packages/sdk/src/node.ts) et `seal_value` des sites : ECDH P-256 éphémère, HKDF-SHA256
    (sel vide, `info = "nokker-phone-v1|<approval_id>"`), AES-256-GCM (iv 12 o, AAD = approval_id) sur
    `{"machine_token": jeton}`. Le jeton ne passe jamais en clair par la WebView ni par le corps de la réponse."""
    import os

    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from nokker.sites.crypto import _hkdf, _pub_jwk, b64u

    eph = ec.generate_private_key(ec.SECP256R1())
    key = _hkdf(eph.exchange(ec.ECDH(), load_pubkey(enc_spki_b64)), f"nokker-phone-v1|{approval_id}")
    iv = os.urandom(12)
    ct = AESGCM(key).encrypt(iv, json.dumps({"machine_token": token}).encode("utf-8"), approval_id.encode("utf-8"))
    return {"v": 1, "epk": _pub_jwk(eph.public_key()), "iv": b64u(iv), "ct": b64u(ct)}


def load_pubkey(spki_b64: str) -> Any:
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import load_der_public_key

    key = load_der_public_key(_b64any(spki_b64))
    if not isinstance(key, ec.EllipticCurvePublicKey) or key.curve.name != "secp256r1":
        raise ValueError("clé P-256 attendue")
    return key


def clean_enroll(body: Any) -> dict[str, Any]:
    """Corps de `POST /v1/link/phone` (contrat de front) : champs d'une liste fermée, codes d'erreur du faux noyau."""
    if not isinstance(body, dict):
        raise PhoneError(400, "bad_json", "corps JSON attendu")
    if set(body) - ENROLL_FIELDS:
        raise PhoneError(400, "unknown_field", f"champ inconnu : {', '.join(sorted(set(body) - ENROLL_FIELDS))[:80]}")
    name = body.get("name").strip() if isinstance(body.get("name"), str) else ""
    if not name or len(name) > 40 or not _NAME.match(name):
        raise PhoneError(400, "bad_name", "name : 1 à 40 caractères")
    if body.get("platform") not in ("ios", "android"):
        raise PhoneError(400, "bad_platform", "platform : ios | android")
    if body.get("key_storage") not in KEY_STORAGES:
        raise PhoneError(400, "bad_key_storage", "key_storage : secure_enclave | strongbox | software")
    if body.get("attestation") is not None:
        raise PhoneError(400, "attestation_unsupported", "phase S : pas d'attestation")
    if body.get("push_token") is not None:
        raise PhoneError(400, "push_unavailable", "phase S : pas de push")
    pub = str(body.get("public_key_spki") or "")
    try:
        load_pubkey(pub)
    except Exception:  # noqa: BLE001
        raise PhoneError(400, "bad_public_key", "public_key_spki : clé publique P-256 (SPKI DER, base64url)") from None
    enc = str(body.get("enc_key_spki") or "")
    try:
        load_pubkey(enc)
    except Exception:  # noqa: BLE001
        raise PhoneError(400, "bad_enc_key", "enc_key_spki : clé d'accord P-256 (SPKI DER, base64url)") from None
    if _b64any(enc) == _b64any(pub):
        raise PhoneError(400, "enc_key_reused", "enc_key_spki doit être distincte de la clé de signature")
    from nokker.qa.untrusted import inline

    return {"name": " ".join(inline(name).split())[:40], "platform": body["platform"],
            "key_storage": body["key_storage"], "pubkey": pub, "enc_key": enc, "key_fp": key_fingerprint(pub),
            "os_version": str(body.get("os_version") or "")[:20], "app_version": str(body.get("app_version") or "")[:20]}


def clean_beat(raw: Any) -> dict[str, Any]:
    """Battement borné (§3.3) : rien d'autre que ces clés ; aucune position."""
    if not isinstance(raw, dict) or len(json.dumps(raw, default=str)) > BEAT_MAX_BYTES:
        raise PhoneError(400, "bad_beat", "battement : objet JSON de 4 Kio au plus")
    power = raw.get("power") if isinstance(raw.get("power"), dict) else {}
    net = raw.get("net") if isinstance(raw.get("net"), dict) else {}
    caps = raw.get("caps") if isinstance(raw.get("caps"), dict) else {}
    level = power.get("level")
    return {"v": 1, "app_version": str(raw.get("app_version") or "")[:40], "os": str(raw.get("os") or "")[:40],
            "mode": raw.get("mode") if raw.get("mode") in ("pocket", "home") else "pocket",
            "reach": raw.get("reach") if raw.get("reach") in ("live", "push") else "push",
            "power": {"charging": power.get("charging") is True,
                      "level": float(level) if isinstance(level, (int, float)) and 0 <= level <= 1 else None},
            "net": {"type": net.get("type") if net.get("type") in ("wifi", "cellular", "none") else "none",
                    "metered": net.get("metered") is True, "home_wifi": net.get("home_wifi") is True},
            "caps": {c: caps.get(c) if caps.get(c) in ("on", "off", "denied") else "off" for c in CAPS}}


def _clean_ha(op: str, a: dict[str, Any]) -> dict[str, Any]:
    """Forme de home-bridge (nokker/homebridge/ha.py::HaOps), bornée ; le téléphone revérifie tout (liste blanche)."""
    if len(json.dumps(a, default=str)) > RESULT_MAX:
        raise PhoneError(400, "bad_op", f"{op} : arguments de 64 Kio au plus")
    if op in ("ha.states", "ha.registry"):
        return {}
    if op == "ha.events":
        try:
            after, timeout = int(a.get("after", -1)), float(a.get("timeout", HA_EVENTS_MAX_S))
        except (TypeError, ValueError):
            raise PhoneError(400, "bad_op", "ha.events : after et timeout numériques") from None
        return {"after": after, "timeout": max(0.0, min(timeout, float(HA_EVENTS_MAX_S)))}
    if op == "ha.tracker_see":
        if set(a) - {"dev_id", "location_name"}:
            raise PhoneError(400, "bad_op", "ha.tracker_see : dev_id et location_name seulement")
        return {"dev_id": str(a.get("dev_id") or ""), "location_name": str(a.get("location_name") or "")[:40]}
    if op == "ha.notify":
        return {"target": str(a.get("target") or "")[:120], "message": str(a.get("message") or "")[:1000],
                **({"title": str(a["title"])[:200]} if a.get("title") else {})}
    ids = a.get("entity_ids")
    if not isinstance(ids, list) or not ids or len(ids) > 10:
        raise PhoneError(400, "bad_op", f"{op} : entity_ids (liste non vide, 10 au plus)")
    if op in ("ha.history", "ha.logbook"):
        try:
            hours = float(a.get("hours", 24))
        except (TypeError, ValueError):
            raise PhoneError(400, "bad_op", f"{op} : hours numérique") from None
        return {"entity_ids": [str(e) for e in ids], "hours": hours}
    data = a.get("data") or {}
    if not isinstance(data, dict):
        raise PhoneError(400, "bad_op", f"{op} : data est un objet")
    return {"domain": str(a.get("domain") or ""), "service": str(a.get("service") or ""),
            "entity_ids": [str(e) for e in ids], "data": data}


def _decision_ok(d: Any, op: str, args: dict[str, Any]) -> bool:
    """Une action maison n'est portée que par une décision du noyau : `direct` (classe de la politique, mêmes entités
    et service) ou `strong` avec la carte décidée (`approval_id`). `notify` et `tracker_see` : une carte, ou la classe
    `system` posée par le noyau (traceur d'un membre, venu d'une zone de l'app)."""
    if not isinstance(d, dict):
        return False
    cls = d.get("class")
    if cls == "strong":
        return isinstance(d.get("approval_id"), str) and bool(d["approval_id"])
    if cls == "direct" and op == "ha.call_service":
        if str(args.get("domain") or "") in NEVER_DIRECT_DOMAINS:
            return False  # même mal réglée, la politique ne débloque jamais la liste sensible sans carte forte
        if args.get("domain") == "cover":  # garage, porte, portail : jamais en direct ; classe inconnue : refus
            classes = d.get("device_classes") if isinstance(d.get("device_classes"), dict) else {}
            if any(not classes.get(e) or classes[e] in NEVER_DIRECT_COVER_CLASSES for e in args.get("entity_ids") or []):
                return False
        return (d.get("domain"), d.get("service")) == (args.get("domain"), args.get("service")) \
            and sorted(d.get("entity_ids") or []) == sorted(args.get("entity_ids") or [])
    if cls == "card" and op == "ha.notify":
        return isinstance(d.get("approval_id"), str) and bool(d["approval_id"])
    return cls == "system" and op == "ha.tracker_see"


def clean_op_args(op: str, args: Any) -> dict[str, Any]:
    if op in HA_EXCLUDED_OPS:
        raise PhoneError(403, "op_not_allowed", f"{op} : jamais par le téléphone")
    if op not in ALL_OPS:
        raise PhoneError(400, "bad_op", f"opération fermée inconnue : {op}")
    a = args if isinstance(args, dict) else {}
    if op.startswith("ha."):
        return _clean_ha(op, a)
    if op == "camera.watch_event":
        try:
            after, limit = float(a.get("after", 0)), int(a.get("limit", CAMERA_LIMIT_MAX))
        except (TypeError, ValueError):
            raise PhoneError(400, "bad_op", "camera.watch_event : after et limit numériques") from None
        return {"after": after, "limit": max(1, min(limit, CAMERA_LIMIT_MAX))}
    if op == "lan.discover":
        types = a.get("types")
        if types is not None and (not isinstance(types, list) or any(t not in LAN_TYPES for t in types)):
            raise PhoneError(400, "bad_op", "lan.discover : types de la liste fermée seulement")
        try:
            timeout = float(a.get("timeout_s", LAN_TIMEOUT_MAX_S))
        except (TypeError, ValueError):
            raise PhoneError(400, "bad_op", "lan.discover : timeout_s numérique") from None
        return {**({"types": list(dict.fromkeys(types))} if types else {}),
                "timeout_s": max(0.5, min(timeout, float(LAN_TIMEOUT_MAX_S)))}
    if op == "camera.thumb_url":
        if not isinstance(a.get("id"), str) or not _CAMERA_ID.match(a["id"]):
            raise PhoneError(400, "bad_op", "camera.thumb_url : id")
        return {"id": a["id"]}
    if op == "zone.state":
        return {"zone_id": str(a.get("zone_id") or "")[:40]}
    if op == "calendar.list":
        from nokker.devices.shapes import clean_args

        return clean_args("calendar.list", {"from": a.get("from") or "", "to": a.get("to") or "", "q": a.get("q")})
    if op == "fetch.get":  # P11 (nokker/cloud/egress.py) : https:443 seulement, deux déclenchements
        from nokker.cloud.egress import normalize_host, url_check

        host, bad = url_check(a.get("url"))
        if bad or a.get("trigger") not in ("allowlist", "explicit") or not isinstance(a.get("turn_id"), str):
            raise PhoneError(400, "bad_op", "fetch.get : url https, trigger allowlist | explicit, turn_id")
        out = {"url": str(a["url"]), "trigger": a["trigger"], "turn_id": a["turn_id"]}
        if a["trigger"] == "allowlist":
            out["allow_hosts"] = [h for h in (normalize_host(x) for x in a.get("allow_hosts") or []) if h][:50]
        else:
            if not isinstance(a.get("grant_id"), str):
                raise PhoneError(400, "bad_op", "fetch.get explicite : grant_id")
            out["grant_id"] = a["grant_id"]
        return out
    key = "image_url" if op == "ocr.image" else "audio_url"
    if not isinstance(a.get(key), str) or not a[key]:
        raise PhoneError(400, "bad_op", f"{op} : {key} (URL signée par nokker.ai) attendu")
    return {key: a[key]}


def args_sha(args: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(args, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


# ── preuve ──
def proof_message(method: str, host: str, path_qs: str, ts: str, nonce: str, body: bytes) -> bytes:
    return f"{method.upper()}\n{host.lower()}\n{path_qs}\n{ts}\n{nonce}\n{hashlib.sha256(body or b'').hexdigest()}".encode()


def parse_proof(header: str | None) -> tuple[str, str, bytes]:
    parts = dict(p.split("=", 1) for p in str(header or "").split(";")[1:] if "=" in p)
    if not str(header or "").startswith("v1;") or set(parts) != {"ts", "nonce", "sig"}:
        raise PhoneError(401, "proof_invalid", "en-tête X-Nokker-Node-Proof v1 attendu")
    if not parts["ts"].isdigit() or not _NONCE.match(parts["nonce"]):
        raise PhoneError(401, "proof_invalid", "ts ou nonce invalide")
    try:
        sig = base64.urlsafe_b64decode(parts["sig"] + "=" * (-len(parts["sig"]) % 4))
    except Exception:  # noqa: BLE001
        raise PhoneError(401, "proof_invalid", "signature illisible") from None
    return parts["ts"], parts["nonce"], sig


def verify_signature(spki_b64: str, message: bytes, sig: bytes) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

    if len(sig) == 64:  # forme brute r||s (WebCrypto) → DER
        sig = encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big"))
    try:
        load_pubkey(spki_b64).verify(sig, message, ec.ECDSA(hashes.SHA256()))
    except (InvalidSignature, ValueError):
        return False
    return True


# ── magasins ──
@dataclass
class MemoryPhoneStore:
    nodes: dict[str, PhoneNode] = field(default_factory=dict)
    tasks: dict[str, PhoneTask] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def add_node(self, n: PhoneNode) -> None:
        with self.lock:
            self.nodes[n.machine_id] = n

    def node(self, machine_id: str) -> PhoneNode | None:
        with self.lock:
            return self.nodes.get(machine_id)

    def nodes_of(self, account: str) -> list[PhoneNode]:
        with self.lock:
            return [n for n in self.nodes.values() if n.account == account]

    def set_beat(self, machine_id: str, beat: dict[str, Any], at: float) -> None:
        with self.lock:
            n = self.nodes.get(machine_id)
            if n is not None:
                self.nodes[machine_id] = replace(n, beat=beat, beat_at=at)

    def add_task(self, t: PhoneTask) -> None:
        with self.lock:
            self.tasks[t.task_id] = t

    def task(self, task_id: str) -> PhoneTask | None:
        with self.lock:
            return self.tasks.get(task_id)

    def queued(self, machine_id: str) -> list[PhoneTask]:
        with self.lock:
            return sorted((t for t in self.tasks.values() if t.machine_id == machine_id and t.status in ("queued", "leased")),
                          key=lambda t: t.created_at)

    def put_task(self, t: PhoneTask) -> None:
        with self.lock:
            self.tasks[t.task_id] = t

    regs: dict[str, dict[str, Any]] = field(default_factory=dict)

    def put_reg(self, r: dict[str, Any]) -> None:
        from nokker.tenancy.context import current_tenant

        with self.lock:
            self.regs[r["approval_id"]] = {**r, "_tenant": current_tenant()}

    def reg(self, approval_id: str) -> dict[str, Any] | None:
        from nokker.tenancy.context import current_tenant

        with self.lock:
            r = self.regs.get(approval_id)
            return dict(r) if r is not None and r["_tenant"] == current_tenant() else None

    def set_reg(self, approval_id: str, **fields: Any) -> None:
        with self.lock:
            if approval_id in self.regs:
                self.regs[approval_id].update(fields)

    def claim_reg(self, approval_id: str) -> dict[str, Any] | None:
        """Retrait atomique : le sceau d'une inscription `ready`, effacé ; None sinon (déjà retiré, autre état)."""
        with self.lock:
            r = self.regs.get(approval_id)
            if r is None or r["state"] != "ready":
                return None
            sealed = r["sealed_token"]
            r.update(state="claimed", sealed_token=None)
            return sealed

    def purge_regs(self, account: str, now: float) -> int:
        with self.lock:
            gone = [k for k, r in self.regs.items() if r["account"] == account and r["expires_at"] < now]
            for k in gone:
                del self.regs[k]
            return len(gone)

    def regs_of(self, account: str) -> list[dict[str, Any]]:
        from nokker.tenancy.context import current_tenant

        with self.lock:
            return sorted((dict(r) for r in self.regs.values() if r["account"] == account
                           and r["_tenant"] == current_tenant()), key=lambda r: r["created_at"])

    def log(self, machine_id: str, limit: int) -> list[PhoneTask]:
        with self.lock:
            return sorted((t for t in self.tasks.values() if t.machine_id == machine_id),
                          key=lambda t: t.created_at, reverse=True)[:limit]

    def count_since(self, account: str, since: float) -> int:
        with self.lock:
            return sum(1 for t in self.tasks.values() if t.account == account and t.created_at >= since)


class PgPhoneStore:
    """Postgres (migration 077), connexion `nokker_app` sous RLS : l'appelant pose le locataire (le membre)."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def _c(self):
        from psycopg.rows import dict_row

        from nokker.store.conn import connect

        return connect(self.dsn, row_factory=dict_row)

    @staticmethod
    def _node(r: dict[str, Any]) -> PhoneNode:
        return PhoneNode(machine_id=r["machine_id"], account=r["account_id"], platform=r["platform"],
                         attested=r["attested"], key_storage=r["key_storage"], pubkey_spki=r["pubkey_spki"],
                         key_fp=r["key_fp"], created_at=r["created_at"], beat=dict(r["beat"] or {}), beat_at=r["beat_at"])

    @staticmethod
    def _task(r: dict[str, Any]) -> PhoneTask:
        return PhoneTask(task_id=r["task_id"], machine_id=r["machine_id"], account=r["account_id"], op=r["op"],
                         args=dict(r["args"] or {}), args_sha256=r["args_sha256"], deadline=r["deadline"],
                         created_at=r["created_at"], status=r["status"], lease_until=r["lease_until"],
                         attempts=r["attempts"], result=r["result"], error=r["error"],
                         origin=dict(r.get("origin") or {"kind": "turn"}))

    def add_node(self, n: PhoneNode) -> None:
        from psycopg.types.json import Jsonb

        with self._c() as c:
            c.execute("INSERT INTO phone_nodes (machine_id, account_id, platform, attested, key_storage, pubkey_spki, "
                      "key_fp, beat, beat_at, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                      (n.machine_id, n.account, n.platform, n.attested, n.key_storage, n.pubkey_spki, n.key_fp,
                       Jsonb(n.beat), n.beat_at, n.created_at))

    def node(self, machine_id: str) -> PhoneNode | None:
        with self._c() as c:
            r = c.execute("SELECT * FROM phone_nodes WHERE machine_id = %s", (machine_id,)).fetchone()
        return self._node(r) if r else None

    def nodes_of(self, account: str) -> list[PhoneNode]:
        with self._c() as c:
            rows = c.execute("SELECT * FROM phone_nodes WHERE account_id = %s ORDER BY created_at", (account,)).fetchall()
        return [self._node(r) for r in rows]

    def set_beat(self, machine_id: str, beat: dict[str, Any], at: float) -> None:
        from psycopg.types.json import Jsonb

        with self._c() as c:
            c.execute("UPDATE phone_nodes SET beat = %s, beat_at = %s WHERE machine_id = %s", (Jsonb(beat), at, machine_id))

    def add_task(self, t: PhoneTask) -> None:
        from psycopg.types.json import Jsonb

        with self._c() as c:
            c.execute("INSERT INTO phone_tasks (task_id, machine_id, account_id, op, args, args_sha256, status, "
                      "lease_until, deadline, attempts, created_at, origin) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                      (t.task_id, t.machine_id, t.account, t.op, Jsonb(t.args), t.args_sha256, t.status,
                       t.lease_until, t.deadline, t.attempts, t.created_at, Jsonb(t.origin)))

    def task(self, task_id: str) -> PhoneTask | None:
        with self._c() as c:
            r = c.execute("SELECT * FROM phone_tasks WHERE task_id = %s", (task_id,)).fetchone()
        return self._task(r) if r else None

    def queued(self, machine_id: str) -> list[PhoneTask]:
        with self._c() as c:
            rows = c.execute("SELECT * FROM phone_tasks WHERE machine_id = %s AND status IN ('queued', 'leased') "
                             "ORDER BY created_at LIMIT 50", (machine_id,)).fetchall()
        return [self._task(r) for r in rows]

    def put_task(self, t: PhoneTask) -> None:
        from psycopg.types.json import Jsonb

        with self._c() as c:
            c.execute("UPDATE phone_tasks SET status = %s, lease_until = %s, attempts = %s, result = %s, error = %s "
                      "WHERE task_id = %s", (t.status, t.lease_until, t.attempts, t.result,
                                             Jsonb(t.error) if t.error is not None else None, t.task_id))

    _RCOLS = ("approval_id", "account_id", "name", "platform", "key_storage", "pubkey_spki", "enc_key_spki", "key_fp",
              "state", "machine_id", "sealed_token", "expires_at", "created_at")
    _RMAP = {"account": "account_id", "pubkey": "pubkey_spki", "enc_key": "enc_key_spki"}

    def put_reg(self, r: dict[str, Any]) -> None:
        from psycopg.types.json import Jsonb

        row = {self._RMAP.get(k, k): v for k, v in r.items() if self._RMAP.get(k, k) in self._RCOLS}
        if row.get("sealed_token") is not None:
            row["sealed_token"] = Jsonb(row["sealed_token"])
        cols = list(row)
        with self._c() as c:
            c.execute(f"INSERT INTO phone_registrations ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})",
                      [row[k] for k in cols])

    def reg(self, approval_id: str) -> dict[str, Any] | None:
        with self._c() as c:
            r = c.execute("SELECT * FROM phone_registrations WHERE approval_id = %s", (approval_id,)).fetchone()
        if r is None:
            return None
        back = {v: k for k, v in self._RMAP.items()}
        return {back.get(k, k): v for k, v in r.items() if k != "tenant_id"}

    def set_reg(self, approval_id: str, **fields: Any) -> None:
        from psycopg.types.json import Jsonb

        row = {self._RMAP.get(k, k): (Jsonb(v) if k == "sealed_token" and v is not None else v) for k, v in fields.items()}
        with self._c() as c:
            c.execute(f"UPDATE phone_registrations SET {', '.join(f'{k} = %s' for k in row)} WHERE approval_id = %s",
                      [*row.values(), approval_id])

    def claim_reg(self, approval_id: str) -> dict[str, Any] | None:
        """Retrait atomique, même entre deux répliques : l'ancien sceau, une seule fois."""
        with self._c() as c:
            r = c.execute("UPDATE phone_registrations p SET state = 'claimed', sealed_token = NULL "
                          "FROM (SELECT approval_id, sealed_token FROM phone_registrations WHERE approval_id = %s "
                          "AND state = 'ready' FOR UPDATE) o WHERE p.approval_id = o.approval_id "
                          "RETURNING o.sealed_token", (approval_id,)).fetchone()
        return dict(r["sealed_token"]) if r and r["sealed_token"] else None

    def purge_regs(self, account: str, now: float) -> int:
        with self._c() as c:
            return c.execute("DELETE FROM phone_registrations WHERE account_id = %s AND expires_at < %s",
                             (account, now)).rowcount

    def regs_of(self, account: str) -> list[dict[str, Any]]:
        with self._c() as c:
            rows = c.execute("SELECT * FROM phone_registrations WHERE account_id = %s ORDER BY created_at",
                             (account,)).fetchall()
        back = {v: k for k, v in self._RMAP.items()}
        return [{back.get(k, k): v for k, v in r.items() if k != "tenant_id"} for r in rows]

    def log(self, machine_id: str, limit: int) -> list[PhoneTask]:
        with self._c() as c:
            rows = c.execute("SELECT * FROM phone_tasks WHERE machine_id = %s ORDER BY created_at DESC LIMIT %s",
                             (machine_id, limit)).fetchall()
        return [self._task(r) for r in rows]

    def count_since(self, account: str, since: float) -> int:
        with self._c() as c:
            return int(c.execute("SELECT count(*) AS n FROM phone_tasks WHERE account_id = %s AND created_at >= %s",
                                 (account, since)).fetchone()["n"])


# ── service ──
@dataclass
class PhoneCandidate:
    """Un téléphone vu par le placement (nokker/cloud/placement.py) : `load` = son battement, `load_at` = son heure."""

    id: str
    name: str
    load: dict[str, Any]
    load_at: float | None
    last_seen_at: float | None
    node: PhoneNode
    kind: str = "phone"
    offline_at: float | None = None
    resources: dict[str, Any] = field(default_factory=dict)


@dataclass
class PhoneService:
    links: Any  # LinkService
    store: Any  # MemoryPhoneStore | PgPhoneStore
    approvals: Any = None  # backend d'approbations : carte forte d'inscription
    secret: bytes = field(default_factory=lambda: secrets.token_bytes(32))  # URL de média signées
    unattested: frozenset[str] = frozenset()  # comptes permis sans attestation ; "*" = tous (QA, équipe)
    base_url: str = "https://app.nokker.ai"
    media_source: Callable[[str, str], tuple[bytes, str] | None] | None = None  # (compte, référence) → (octets, type)
    signer: Any = None  # nokker/cloud/link.py::Signer : attestation des actions maison (P10a)
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    _nonces: dict[tuple[str, str], float] = field(default_factory=dict, init=False)
    _media_used: dict[str, float] = field(default_factory=dict, init=False)
    _ephemeral: dict[str, tuple[str, float]] = field(default_factory=dict, init=False, repr=False)
    _hits: dict[str, deque] = field(default_factory=dict, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    # ── débit ──
    def rate(self, key: str, limit: int, window_s: float) -> None:
        now = self.clock()
        with self._lock:
            q = self._hits.setdefault(key, deque())
            while q and now - q[0] > window_s:
                q.popleft()
            if len(q) >= limit:
                raise PhoneError(429, "rate", "trop de requêtes")
            q.append(now)

    # ── inscription ──
    def enroll(self, account: str, tenant: str, body: Any, requester_device: str | None) -> dict[str, Any]:
        e = clean_enroll(body)
        if not ("*" in self.unattested or account in self.unattested):
            raise PhoneError(400, "attestation_required", "inscription sans attestation : réservée (QA, équipe)")
        if not requester_device:
            raise PhoneError(403, "device_required", "l'inscription se demande depuis une session d'appareil enrôlé")
        if len(self.links.phones_of(account)) >= MAX_PHONES:
            raise PhoneError(409, "too_many_phones", f"{MAX_PHONES} téléphones au plus")
        if self.approvals is None:
            raise PhoneError(503, "approvals_disabled", "approbations indisponibles")
        from nokker.core.turn import Decision, Gate, InputCheck, ToolCall, Turn, Verdict
        from nokker.tenancy.context import scoped_thread_id

        turn = Turn(thread_id=scoped_thread_id(tenant, f"phone-enroll-{account}"), index=0, input=InputCheck(text=""),
                    channel="kernel", sender=account)
        fp = e["key_fp"][:8]
        # arguments VUS par le client (contrat de front) ; `requested_from` : l'appareil qui demande, qui ne signe pas
        args = {"name": e["name"], "platform": e["platform"], "key_fingerprint": fp, "key_storage": e["key_storage"],
                "attested": False, "requested_from": requester_device}
        call = ToolCall(id=f"phone-{secrets.token_hex(6)}", name=PHONE_ENROLL_TOOL, arguments=args)
        reason = f"Inscrire le téléphone « {e['name']} » comme nœud (clé {fp})."
        verdict = Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule=PHONE_ENROLL_RULE, reason=reason,
                          subject=call.id, strength="strong")
        card = self.approvals.request(turn, call, verdict, "À approuver sur un AUTRE appareil déjà enrôlé.")
        now = self.clock()
        self.store.purge_regs(account, now)  # ménage : les inscriptions échues du membre
        # en base (078), dans le locataire du membre : la clé et le compte ne sortent pas sur la carte
        self.store.put_reg({"approval_id": card.id, "account": account, "name": e["name"], "platform": e["platform"],
                            "key_storage": e["key_storage"], "pubkey": e["pubkey"], "enc_key": e["enc_key"],
                            "key_fp": e["key_fp"], "state": "pending", "expires_at": now + CARD_TTL_S,
                            "created_at": now})
        return {"approval_id": card.id, "key_fingerprint": fp, "expires_at": _iso(self.clock() + CARD_TTL_S)}

    def on_decision(self, approval: Any) -> str:
        """Carte forte décidée (exécuteur) : machine `phone`, nœud, et le jeton SCELLÉ pour la clé d'accord du
        téléphone, gardé en base pour UN retrait (le jeton en clair n'est jamais écrit)."""
        from nokker.tenancy.context import tenant_of_thread, tenant_scope

        tenant = tenant_of_thread(approval.thread_id) if getattr(approval, "thread_id", "") else None
        with tenant_scope(tenant) if tenant else _null():
            reg = self.store.reg(approval.id)
            if reg is None or reg["state"] != "pending":
                return "inscription inconnue ou périmée"
            if approval.status != "approved":
                self.store.set_reg(approval.id, state="rejected")
                return "inscription refusée"
            machine, token = self.links.add_phone(reg["account"], tenant or "", reg["name"])
            now = self.clock()
            self.store.add_node(PhoneNode(machine_id=machine.id, account=reg["account"], platform=reg["platform"],
                                          attested=False, key_storage=reg["key_storage"], pubkey_spki=reg["pubkey"],
                                          key_fp=reg["key_fp"], created_at=now))
            self.store.set_reg(approval.id, state="ready", machine_id=machine.id,
                               sealed_token=seal_token(reg["enc_key"], approval.id, token),
                               expires_at=now + TOKEN_PICKUP_TTL_S)
        return f"téléphone {reg['name']} inscrit"

    def pickup(self, account: str, approval_id: str) -> tuple[int, dict[str, Any]]:
        """`GET /v1/link/phone/registrations/{id}` : 202 en attente, 201 UNE fois (jeton scellé pour la clé d'accord
        du nœud), puis 410 ; 403 refusée ; 404 inconnue ou d'un autre membre. Dans le locataire du membre."""
        with self._lock:  # un seul retrait, même pour deux requêtes simultanées du même processus
            reg = self.store.reg(approval_id)
            if reg is None or reg["account"] != account:
                return 404, err("not_found")
            if reg["state"] == "rejected":
                return 403, err("rejected")
            if reg["state"] == "claimed":
                return 410, err("gone", "jeton déjà rendu")
            if self.clock() > reg["expires_at"]:
                return 410, err("gone")
            if reg["state"] != "ready":
                return 202, {"status": "pending"}
            sealed = self.store.claim_reg(approval_id)
        if sealed is None:
            return 410, err("gone", "jeton déjà rendu")
        return 201, {"machine_id": reg["machine_id"], "sealed_token": sealed, "member": account,
                     "caps_allowed": list(CAPS_ALLOWED)}

    def phones(self, account: str) -> dict[str, Any]:
        """`GET /v1/link/phone` (alias ; la liste unique est `GET /v1/link/machines`, kind `phone`)."""
        now = self.clock()
        names = {m.id: m for m in self.links.phones_of(account)}
        out = []
        for n in self.store.nodes_of(account):
            m = names.get(n.machine_id)
            if m is None:
                continue
            out.append({"machine_id": n.machine_id, "name": m.name, "platform": n.platform,
                        "key_storage": n.key_storage, "key_fingerprint": n.key_fp[:8], "attested": n.attested,
                        "created_at": _iso(n.created_at), "last_beat_at": _iso(n.beat_at), "live": n.online(now)})
        return {"phones": out}

    def take_ephemeral(self, task_id: str) -> str | None:
        """Résultat d'une opération éphémère (vignette), rendu UNE fois puis oublié ; None s'il a expiré (30 s)."""
        with self._lock:
            got = self._ephemeral.pop(task_id, None)
        return got[0] if got is not None and self.clock() - got[1] <= EPHEMERAL_TTL_S else None

    def task_log(self, account: str, machine_id: str) -> tuple[int, dict[str, Any]]:
        """`GET /v1/link/phone/{machine}/tasks` : le journal SANS contenu (opération, origine, état, heure)."""
        node = self.store.node(machine_id)
        if node is None or node.account != account:
            return 404, err("not_found")
        rows = getattr(self.store, "log", None)
        tasks = rows(machine_id, 200) if rows is not None else []
        def line(t: PhoneTask) -> dict[str, Any]:
            row = {"task_id": t.task_id, "op": t.op, "origin": t.origin.get("kind"), "status": t.status,
                   "at": _iso(t.created_at)}
            if t.op == "fetch.get":  # l'hôte seulement : ni chemin ni requête
                from urllib.parse import urlsplit

                row["host"] = (urlsplit(str(t.args.get("url") or "")).hostname or "").lower()
            return row

        return 200, {"tasks": [line(t) for t in tasks]}

    def export(self, account: str) -> dict[str, Any]:
        """Export RGPD du compte (nokker/account/export.py) : ses téléphones, leurs tâches (opération, arguments, état,
        code d'erreur ; pas le contenu rendu, pages ou textes, qui n'est qu'un résultat de passage) et ses inscriptions
        (sans clé ni jeton scellé). Appelé dans le locataire du compte."""
        phones = self.phones(account)["phones"]
        fps = {n.machine_id: n.key_fp for n in self.store.nodes_of(account)}
        tasks = []
        for p in phones:
            p["key_fingerprint"] = fps.get(p["machine_id"], p["key_fingerprint"])
            p.pop("live", None)
            for t in self.store.log(p["machine_id"], 100_000):
                tasks.append({"task_id": t.task_id, "machine_id": t.machine_id, "op": t.op, "args": t.args,
                              "origin": t.origin.get("kind"), "status": t.status,
                              "error": (t.error or {}).get("code") if t.error else None, "at": _iso(t.created_at)})
        regs = [{"name": r.get("name"), "platform": r.get("platform"), "state": r.get("state"),
                 "created_at": _iso(r.get("created_at")), "expires_at": _iso(r.get("expires_at"))}
                for r in self.store.regs_of(account)]
        return {"phones": phones, "tasks": sorted(tasks, key=lambda t: t["at"] or ""), "registrations": regs}

    # ── preuve de clé ──
    def check_proof(self, machine: Any, node: PhoneNode, method: str, host: str, path_qs: str, body: bytes,
                    header: str | None) -> None:
        ts, nonce, sig = parse_proof(header)
        now = self.clock()
        if abs(now - int(ts)) > PROOF_SKEW_S:
            raise PhoneError(401, "proof_invalid", "horodatage hors des ±60 s")
        if not verify_signature(node.pubkey_spki, proof_message(method, host, path_qs, ts, nonce, body), sig):
            raise PhoneError(401, "proof_invalid", "signature invalide")
        with self._lock:
            for k in [k for k, t in self._nonces.items() if now - t > NONCE_TTL_S]:
                del self._nonces[k]
            if (machine.id, nonce) in self._nonces:
                raise PhoneError(401, "proof_invalid", "nonce déjà vu")
            self._nonces[(machine.id, nonce)] = now

    # ── battement ──
    def beat(self, machine: Any, raw: Any) -> dict[str, Any]:
        self.rate(f"beat:{machine.id}", 3, 60)
        if not isinstance(raw, dict) or raw.get("v") != 1 or raw.get("reach") not in ("live", "push"):
            raise PhoneError(400, "bad_beat", "battement v1, reach live | push")
        if raw.get("reach") == "push":
            raise PhoneError(400, "push_unavailable", "phase S : sans push, seul « live » est joignable")
        b = clean_beat(raw)
        self.store.set_beat(machine.id, b, self.clock())
        return {"ok": True, "state": "live", "next_beat_s": 60}

    def online_nodes(self, account: str, cap: str | None = None) -> list[PhoneNode]:
        now = self.clock()
        return [n for n in self.store.nodes_of(account) if n.online(now) and (cap is None or n.has_cap(cap))]

    # ── file ──
    def enqueue(self, account: str, node: PhoneNode, op: str, args: Any, deadline_s: float = 600,
                origin: dict[str, Any] | None = None) -> PhoneTask:
        if op not in READ_OPS | HA_READ_OPS | CAMERA_OPS | {"lan.discover"} and not node.attested:
            raise PhoneError(403, "attestation_required", "un nœud sans attestation ne fait que des lectures")
        clean = clean_op_args(op, args)
        if op in HA_ACTION_OPS and not _decision_ok((origin or {}).get("decision"), op, clean):
            raise PhoneError(403, "decision_required", f"{op} : seulement porté par une décision maison")
        now = self.clock()
        if self.store.count_since(account, now - 3600) >= 120:
            raise PhoneError(429, "rate", "120 tâches par heure et par compte")
        t = PhoneTask(task_id="pt_" + secrets.token_hex(8), machine_id=node.machine_id, account=account, op=op,
                      args=clean, args_sha256=args_sha(clean), deadline=now + deadline_s, created_at=now,
                      origin=dict(origin or {"kind": "turn"}))
        self.store.add_task(t)
        return t

    def wire(self, t: PhoneTask) -> dict[str, Any]:
        """Tâche sur le fil ; une action maison porte l'attestation de sa décision (§3.6), signée à la relève (≤ 120 s)."""
        out = t.json()
        if t.op in HA_ACTION_OPS:
            out["attestation"] = self.attest(t)
        return out

    def attest(self, t: PhoneTask) -> str:
        """JWT ES256 `nokker-phone-task` (clé de la passerelle, JWKS du lien) : {aud: mc_…, task_id, op, args_sha256,
        decision, approval_id|null, exp}. Le téléphone le vérifie et recalcule args_sha256 avant d'agir."""
        if self.signer is None:
            raise PhoneError(503, "no_signer", "attestation impossible : clé de la passerelle absente")
        d = dict(t.origin.get("decision") or {})
        now = int(self.clock())
        return self.signer.sign({"iss": self.signer.issuer, "aud": t.machine_id, "task_id": t.task_id, "op": t.op,
                                 "args_sha256": t.args_sha256, "decision": d, "approval_id": d.get("approval_id"),
                                 "iat": now, "exp": now + ATTESTATION_TTL_S}, typ="nokker-phone-task")

    def _expire(self, t: PhoneTask, now: float) -> PhoneTask:
        if t.status in ("queued", "leased") and now > t.deadline:
            t = replace(t, status="expired")
            self.store.put_task(t)
        elif t.status == "leased" and t.lease_until is not None and now > t.lease_until:
            t = replace(t, status="queued", lease_until=None)  # bail échu : remise en file
            self.store.put_task(t)
        return t

    def poll(self, machine: Any, wait_s: float) -> list[dict[str, Any]]:
        self.rate(f"poll:{machine.id}", 10, 60)
        end = self.clock() + max(0.0, min(25.0, wait_s))
        node = self.store.node(machine.id)
        if node is not None and node.beat_at is not None:  # une relève vaut battement (l'app est au premier plan)
            self.store.set_beat(machine.id, node.beat, self.clock())
        while True:
            now = self.clock()
            ready = [t for t in (self._expire(x, now) for x in self.store.queued(machine.id)) if t.status == "queued"]
            if ready or now >= end:
                out = []
                for t in ready[:4]:
                    t = replace(t, status="leased", lease_until=now + LEASE_S, attempts=t.attempts + 1)
                    self.store.put_task(t)
                    out.append(self.wire(t))
                return out
            self.sleep(0.5)

    def _mine(self, machine: Any, task_id: str) -> PhoneTask:
        t = self.store.task(task_id) if _TASK.match(task_id) else None
        if t is None or t.machine_id != machine.id:
            raise PhoneError(404, "not_found")
        return self._expire(t, self.clock())

    def lease(self, machine: Any, task_id: str) -> dict[str, Any]:
        t = self._mine(machine, task_id)
        if t.status != "leased":
            raise PhoneError(409, f"task_{t.status}")
        t = replace(t, lease_until=self.clock() + LEASE_S)
        self.store.put_task(t)
        return {"lease_until": _iso(t.lease_until)}

    def result(self, machine: Any, task_id: str, body: Any) -> dict[str, Any]:
        """Résultat idempotent (contrat de front) : un second envoi identique rend `{stored: false}`, un résultat
        différent après coup 409 `already_answered` ; une tâche qui n'est plus en bail : 409 `task_<état>`."""
        self.rate(f"result:{machine.id}", 60, 60)
        t = self._mine(machine, task_id)
        b = body if isinstance(body, dict) else {}
        if t.status in ("done", "failed"):
            same = b.get("status") == t.status and ((b.get("result") if t.status == "done" else None) == t.result
                                                    or (t.op in EPHEMERAL_OPS and t.status == "done"))
            if same:
                return {"stored": False}
            raise PhoneError(409, "already_answered")
        if t.status != "leased":
            raise PhoneError(409, f"task_{t.status}")
        if b.get("status") == "done":
            if not isinstance(b.get("result"), str):
                raise PhoneError(400, "result_required")
            limit = (FETCH_RESULT_MAX if t.op == "fetch.get" else HA_RESULT_MAX if t.op in HA_BIG_OPS
                     else RESULT_MAX)
            if len(b["result"].encode("utf-8")) > limit:
                raise PhoneError(413, "result_too_large", f"texte de {limit // 1024} Kio au plus")
            if t.op in EPHEMERAL_OPS:  # vignette (P10c) : jamais en base, en mémoire jusqu'à la route dédiée
                with self._lock:
                    now = self.clock()
                    for k in [k for k, (_, at) in self._ephemeral.items() if now - at > EPHEMERAL_TTL_S]:
                        del self._ephemeral[k]
                    self._ephemeral[t.task_id] = (b["result"], now)
                t = replace(t, status="done", result="")
            else:
                t = replace(t, status="done", result=b["result"])
        elif b.get("status") == "failed":
            e = b.get("error") if isinstance(b.get("error"), dict) else None
            if e is None or not isinstance(e.get("code"), str):
                raise PhoneError(400, "error_required")
            t = replace(t, status="failed", error={"code": e["code"][:40],
                                                   **({"message": e["message"][:200]} if isinstance(e.get("message"), str)
                                                      else {})})
        else:
            raise PhoneError(400, "bad_status")
        self.store.put_task(t)
        return {"stored": True}

    def cancel(self, task_id: str) -> None:
        t = self.store.task(task_id)
        if t is not None and t.status in ("queued", "leased"):
            self.store.put_task(replace(t, status="cancelled", lease_until=None))

    def cancel_all(self, machine_id: str) -> int:
        n = 0
        for t in self.store.queued(machine_id):
            self.store.put_task(replace(t, status="cancelled", lease_until=None))
            n += 1
        return n

    def wait(self, task_id: str, timeout_s: float = 20.0) -> PhoneTask | None:
        """Attente côté noyau (tour) : le résultat, ou None après `timeout_s` (continuation : P7b)."""
        end = self.clock() + timeout_s
        while True:
            t = self.store.task(task_id)
            if t is not None and t.status in ("done", "failed", "expired", "cancelled"):
                return t
            if self.clock() >= end:
                return None
            self.sleep(0.25)

    # ── médias ──
    def image_url(self, machine_id: str, thread_id: str, sha: str) -> str:
        """Image X7 d'un fil pour `ocr.image` : référence `img:<fil>|<sha256>`, signée pour CE téléphone."""
        return self.media_url(machine_id, f"img:{thread_id}|{sha}")

    def audio_url(self, machine_id: str, ref: str) -> str:
        """Audio pour `stt.audio` : référence `audio:<ref>` (plomberie ; aucune pièce jointe audio n'existe encore)."""
        return self.media_url(machine_id, f"audio:{ref}")

    def media_url(self, machine_id: str, ref: str) -> str:
        claims = {"m": machine_id, "r": ref, "e": int(self.clock()) + MEDIA_TTL_S, "n": secrets.token_hex(8)}
        body = base64.urlsafe_b64encode(json.dumps(claims, separators=(",", ":")).encode()).decode().rstrip("=")
        sig = base64.urlsafe_b64encode(hmac.new(self.secret, body.encode(), "sha256").digest()).decode().rstrip("=")
        return f"{self.base_url.rstrip('/')}/v1/gateway/phone/blobs/{body}.{sig}"

    def take_media(self, machine: Any, token: str) -> str:
        """Référence d'un média : signature et échéance (2 min) sinon 410 `gone`, usage unique (410), lié au nœud
        (403 `not_your_blob`)."""
        body, _, sig = token.partition(".")
        good = base64.urlsafe_b64encode(hmac.new(self.secret, body.encode(), "sha256").digest()).decode().rstrip("=")
        if not sig or not hmac.compare_digest(sig, good):
            raise PhoneError(410, "gone")
        try:
            claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        except Exception:  # noqa: BLE001
            raise PhoneError(410, "gone") from None
        now = self.clock()
        if now > float(claims.get("e") or 0):
            raise PhoneError(410, "gone")
        if claims.get("m") != machine.id:
            raise PhoneError(403, "not_your_blob")
        with self._lock:
            for k in [k for k, t in self._media_used.items() if now - t > MEDIA_TTL_S]:
                del self._media_used[k]
            if claims["n"] in self._media_used:
                raise PhoneError(410, "gone")
            self._media_used[claims["n"]] = now
        return str(claims["r"])

    # ── placement (P7b, X15) ──
    def candidates(self, account: str) -> list[PhoneCandidate]:
        """Les téléphones du compte sous la forme que lit `placement.place` (battement = charge)."""
        nodes = {n.machine_id: n for n in self.store.nodes_of(account)}
        out = []
        for m in self.links.phones_of(account):
            n = nodes.get(m.id)
            if n is not None:
                out.append(PhoneCandidate(id=m.id, name=m.name, load=dict(n.beat or {}), load_at=n.beat_at,
                                          last_seen_at=n.beat_at, node=n))
        return out

    def place(self, account: str, op: str) -> Any:
        """Le téléphone qui fera `op` : `placement.place` sur les téléphones DU MEMBRE (le compte), besoin = la capacité
        de l'opération. Déterministe ; le modèle ne choisit jamais la machine."""
        from nokker.cloud import placement as pl

        return pl.place(self.candidates(account), pl.Needs(caps=(CAP_OF_OP[op],)), self.clock())

    def run(self, account: str, op: str, args: Any, *, origin: dict[str, Any] | None = None, wait_s: float = 20.0,
            deadline_s: float = 600, args_of: Callable[[PhoneNode], Any] | None = None
            ) -> tuple[PhoneCandidate | None, PhoneTask | None, PhoneTask | None]:
        """Place, dépose, attend `wait_s` : (téléphone, tâche déposée, tâche close | None si toujours en cours).
        Aucun téléphone en ligne avec la capacité : (None, None, None). `args_of` : arguments liés au téléphone choisi
        (URL de média signée pour LUI)."""
        p = self.place(account, op)
        if p.machine is None:
            return None, None, None
        node = p.machine.node
        t = self.enqueue(account, node, op, args_of(node) if args_of is not None else args, deadline_s=deadline_s,
                         origin=origin)
        return p.machine, t, self.wait(t.task_id, wait_s)

    # ── P8 par la file ──
    def calendar_list(self, account: str, args: dict[str, Any]) -> list[dict[str, Any]] | None:
        """P8 par un téléphone EN LIGNE du membre qui a l'agenda (placement X15) : la file, 20 s ; None = pas de tel
        téléphone (repli : device.request). Échec sur le téléphone : DeviceRequestError, comme une réponse de l'app."""
        from nokker.devices.broker import DeviceRequestError

        p = self.place(account, "calendar.list")
        if p.machine is None:
            return None
        t = self.enqueue(account, p.machine.node, "calendar.list", args, deadline_s=30)
        done = self.wait(t.task_id, 20.0)
        if done is None or done.status != "done":
            code = (done.error or {}).get("code") if done is not None and done.status == "failed" else None
            raise DeviceRequestError({"denied": "device_denied", "too_large": "device_too_large"}.get(code or "",
                                                                                                      "device_offline"))
        try:
            items = json.loads(done.result or "[]")
        except ValueError:
            raise DeviceRequestError("device_too_large") from None
        return items


_SERVICE: PhoneService | None = None


def configure(svc: PhoneService | None) -> None:
    """Le service du processus (cloud) : outils du téléphone (nokker/cloud/phone_tools.py), champs des messages."""
    global _SERVICE
    _SERVICE = svc


def service() -> PhoneService | None:
    return _SERVICE


__all__ = ["CAPS", "PHONE_ENROLL_RULE", "PHONE_ENROLL_TOOL", "PROOF_HEADER", "READ_OPS", "MemoryPhoneStore",
           "PgPhoneStore", "PhoneCandidate", "PhoneError", "PhoneNode", "PhoneService", "PhoneTask", "clean_beat",
           "clean_enroll", "configure", "key_fingerprint", "proof_message", "service", "verify_signature"]
