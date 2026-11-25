"""Caméra de veille du nœud maison (P10c, docs/LOKKAL-PHONE.md §4.4) : détection de mouvement SUR l'appareil, image
gardée sur l'appareil (24 h), vignette lisible depuis le même Wi-Fi seulement. Le noyau ne DÉTIENT aucune donnée de
caméra (doctrine de la maison) : il lit le nœud à la demande, par la file.

- `camera.watch_event {after, limit ≤ 200}` → `{items, now}` : forme réduite de `cameras.reviews` (texte seulement),
  nettoyée sur liste blanche ; un champ en plus est ignoré. Comme la vignette, jamais écrit en base (EPHEMERAL_OPS).
- `camera.thumb_url {id}` → `{id?, url, key, expires_in ≤ 300}` : l'URL de la vignette sur le LAN et la clé qui la
  déchiffre. JAMAIS écrite en base (la tâche se clôt avec un résultat vide, PhoneService.EPHEMERAL_OPS), jamais
  journalisée, jamais vue par le modèle : rendue une fois à l'app du titulaire par la route dédiée.
- Routes du TITULAIRE (session complète) : `GET /v1/link/phone/{machine}/camera/events?after=` et
  `POST /v1/link/phone/{machine}/camera/thumb {id}` (alias `GET …/camera/{id}/thumb`). 10 s au plus ; 409 node_offline,
  504 phone_timeout ; codes de l'appareil : thumb_gone → 410, not_found → 404, les autres → 502 `{error: {code}}`.
- Brief : au moment du brief, les 24 dernières heures lues sur le nœud, rendues par les gabarits de la veille
  (nokker/homewatch/source.py::camera_lines), niveau « inconnu ». Nœud injoignable : « Relève en échec : cameras. ».
  Pas de notification immédiate en v1. Aucun texte nouveau vu par le modèle.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

WAIT_S = 10.0
EXPIRES_MAX_S = 300
ITEM_KEYS = ("id", "camera", "start", "end", "severity", "niveau", "title", "summary", "objects", "zones", "has_thumb")
_STATUS = {"thumb_gone": 410, "not_found": 404}
# Titre vu par le MODÈLE (brief) : jamais celui de l'appareil (donnée de tiers), et jamais « personne », qui se lirait
# comme « la personne » (l'utilisateur). Mappé par le noyau : `objects` contient `person` → présence humaine.
MODEL_TITLES = {"person": "présence humaine", "other": "mouvement"}


def model_title(item: dict[str, Any]) -> str:
    return MODEL_TITLES["person" if "person" in (item.get("objects") or []) else "other"]


def texts_sha256() -> str:
    """Empreinte des libellés caméra vus par le modèle (la ligne elle-même : gabarit de la veille, HOME-WATCH §7)."""
    import hashlib

    return hashlib.sha256(json.dumps({"MODEL_TITLES": MODEL_TITLES, "TIME_FORMAT": TIME_FORMAT, "NAME_MAX": NAME_MAX},
                                     ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


TIME_FORMAT = "%d.%m %H:%M"  # heure suisse (nokker/jobs/localtime.py), formée par le noyau depuis `start`
NAME_MAX = 40


def model_row(item: dict[str, Any]) -> dict[str, Any]:
    """L'événement tel que le brief le rend : nom de caméra neutralisé (`inline`, une ligne, 40 car.), titre mappé."""
    from nokker.qa.untrusted import inline

    return {**item, "camera": " ".join(inline(item.get("camera") or "").split())[:NAME_MAX],
            "title": model_title(item)}


class CameraError(Exception):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status, self.code = status, code


def _text(v: Any, n: int) -> str | None:
    if v is None:
        return None
    s = "".join(c if c.isprintable() else " " for c in str(v))
    return " ".join(s.split())[:n] or None


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def clean_item(raw: Any) -> dict[str, Any] | None:
    """Un événement de l'appareil, sur liste blanche : forme courte ou longue, champs en plus ignorés. Aucune image,
    aucune URL. `niveau` vaut toujours « inconnu » (l'appareil ne résume pas)."""
    if not isinstance(raw, dict):
        return None
    from nokker.cloud.phone import _CAMERA_ID

    rid, start = raw.get("id"), _num(raw.get("start"))
    if not isinstance(rid, str) or not _CAMERA_ID.match(rid) or start is None or not 0 < start < 1e11:
        return None  # heure invalide : l'événement est écarté
    out: dict[str, Any] = {"id": rid, "camera": _text(raw.get("camera"), 40) or "", "start": start,
                           "end": _num(raw.get("end")), "severity": "detection", "niveau": "inconnu",
                           "title": _text(raw.get("title"), 140), "summary": None,
                           "objects": [o for o in (_text(x, 40) for x in raw.get("objects") or []
                                                   if isinstance(raw.get("objects"), list)) if o][:10],
                           "zones": [z for z in (_text(x, 40) for x in raw.get("zones") or []
                                                 if isinstance(raw.get("zones"), list)) if z][:10],
                           "has_thumb": raw.get("has_thumb") is True}
    return out


@dataclass
class PhoneCamera:
    phone: Any  # PhoneService
    wait_s: float = WAIT_S
    scrub: Callable[[str], str] = lambda s: s

    def _node(self, account: str, machine_id: str | None) -> Any:
        """Le téléphone `camera` en ligne du titulaire : celui nommé (route), sinon le placement (brief)."""
        from nokker.cloud import placement as pl

        cands = self.phone.candidates(account)
        if machine_id is not None:
            if not any(c.id == machine_id for c in cands):
                raise CameraError(404, "not_found")
            cands = [c for c in cands if c.id == machine_id]
        p = pl.place(cands, pl.Needs(caps=("camera",)), self.phone.clock())
        if p.machine is None:
            raise CameraError(409, "node_offline")
        return p.machine

    def _run(self, account: str, machine_id: str | None, op: str, args: dict[str, Any]) -> tuple[Any, str]:
        node = self._node(account, machine_id)
        t = self.phone.enqueue(account, node.node, op, args, deadline_s=60)
        done = self.phone.wait(t.task_id, self.wait_s)
        if done is None or done.status in ("queued", "leased"):
            self.phone.cancel(t.task_id)
            raise CameraError(504, "phone_timeout")
        if done.status == "failed":
            code = str((done.error or {}).get("code") or "camera_error")[:40]
            raise CameraError(_STATUS.get(code, 502), code)
        if done.status != "done":
            raise CameraError(504, "phone_timeout")
        return done, done.result or ""

    def events(self, account: str, machine_id: str | None, after: float) -> dict[str, Any]:
        done, _ = self._run(account, machine_id, "camera.watch_event", {"after": after, "limit": 200})
        raw = self.phone.take_ephemeral(done.task_id)  # jamais en base : le texte des événements ne vit qu'ici
        try:
            got = json.loads(raw or "{}")
        except ValueError:
            raise CameraError(502, "bad_result") from None
        items = [i for i in (clean_item(x) for x in (got.get("items") or [] if isinstance(got, dict) else [])) if i]
        now = _num(got.get("now")) if isinstance(got, dict) else None
        return {"items": items, "now": now if now is not None else self.phone.clock()}

    def thumb(self, account: str, machine_id: str, rid: Any) -> dict[str, Any]:
        """La vignette pour l'app du titulaire. Le résultat ne passe QUE par la mémoire (take_ephemeral) : rien en base,
        rien au journal ; la réponse HTTP porte Cache-Control: no-store."""
        from nokker.cloud.phone import _CAMERA_ID

        if not isinstance(rid, str) or not _CAMERA_ID.match(rid):
            raise CameraError(400, "bad_request")
        done, _ = self._run(account, machine_id, "camera.thumb_url", {"id": rid})
        raw = self.phone.take_ephemeral(done.task_id)
        try:
            got = json.loads(raw or "")
        except ValueError:
            raise CameraError(502, "bad_result") from None
        url, key, exp = (got.get("url"), got.get("key"), got.get("expires_in")) if isinstance(got, dict) else (None,) * 3
        if not isinstance(url, str) or not url.startswith("http") or not isinstance(key, str) or not key \
                or _num(exp) is None:
            raise CameraError(502, "bad_result")
        return {"id": rid, "url": url, "key": key, "expires_in": int(max(0, min(float(exp), EXPIRES_MAX_S)))}

    # ── brief ──
    @staticmethod
    def _fmt(ts: Any) -> str:
        from nokker.jobs.localtime import local

        return local(float(ts)).strftime(TIME_FORMAT) if ts else "?"

    def brief_text(self, sender: str | None, hours: int = 24) -> str | None:
        """Bloc `maison_veille` du brief du titulaire : MÊMES gabarits que la veille (HOME-WATCH §7). Sans nœud caméra :
        None. Nœud injoignable ou en échec : la ligne existante « Relève en échec : cameras. »."""
        from nokker.homewatch.source import brief_block, camera_lines

        if not sender:
            return None
        from nokker.cloud.placement import caps_of

        if not any("camera" in caps_of(c) or (c.node.beat.get("caps") or {}).get("camera") for c in
                   self.phone.candidates(sender)):
            return None  # aucun nœud caméra déclaré : rien à dire
        try:
            got = self.events(sender, None, self.phone.clock() - hours * 3600)
        except CameraError:
            return brief_block(["Relève en échec : cameras."], None, hours, self.scrub)
        rows = [model_row(i) for i in got["items"]]  # jamais le titre ni l'heure bruts de l'appareil
        return brief_block(camera_lines(rows, self._fmt), None, hours, self.scrub)


_SERVICE: PhoneCamera | None = None


def configure(svc: PhoneCamera | None) -> None:
    global _SERVICE
    _SERVICE = svc


def service() -> PhoneCamera | None:
    return _SERVICE


__all__ = ["CameraError", "ITEM_KEYS", "MODEL_TITLES", "TIME_FORMAT", "PhoneCamera", "clean_item", "configure",
           "model_row", "model_title",
           "service", "texts_sha256"]
