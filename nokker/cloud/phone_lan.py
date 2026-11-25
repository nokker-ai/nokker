"""Découverte du réseau de la maison par le nœud maison (P10d, docs/LOKKAL-PHONE.md §4.4) : Bonjour sur le LAN, pour
la liste des écrans et enceintes (SCREEN-CAST, plus tard ; la diffusion n'est pas dans P10d).

- Op `lan.discover {types?, timeout_s ≤ 5}` → `{services: [{id, type | types, name}], at}` : lecture, permise sans
  attestation, cap `lan_home` (Wi-Fi « maison »), placement mode home d'abord, TITULAIRE seul.
- Liste FERMÉE de types (`phone.LAN_TYPES`) : un service d'un autre type est écarté, même si le nœud l'envoie.
- `name` : texte de tiers, `inline()` et 63 caractères ; clôturé comme non fiable s'il est un jour montré au modèle.
- Ni adresse IP, ni port, ni TXT : un résultat qui en porte est REFUSÉ en entier (contrat rompu, fail-closed).
- Éphémère : le résultat n'est jamais écrit en base (EPHEMERAL_OPS), il ne vit qu'en mémoire jusqu'à sa lecture.
- Route du titulaire : `GET /v1/link/phone/{machine_id}/lan/services?types=…` → `{services, at}` ; 409 node_offline,
  504 phone_timeout, codes de l'appareil (not_home_wifi, local_network_denied, bad_args) → 502 `{error: {code}}`.
Aucun texte vu par le modèle.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from nokker.cloud.phone_camera import CameraError as LanError

NAME_MAX = 63
FORBIDDEN = frozenset({"ip", "ips", "ipv4", "ipv6", "address", "addresses", "addr", "host", "hostname", "port",
                       "txt", "txt_record", "records", "mac"})
_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def clean_services(raw: Any) -> list[dict[str, Any]]:
    """Services de l'appareil, sur la liste fermée ; LanError(502, bad_result) si un champ interdit apparaît."""
    from nokker.cloud.phone import LAN_TYPES
    from nokker.qa.untrusted import inline

    if not isinstance(raw, list):
        raise LanError(502, "bad_result")
    out: list[dict[str, Any]] = []
    for s in raw[:200]:
        if not isinstance(s, dict):
            continue
        if FORBIDDEN & {str(k).lower() for k in s}:
            raise LanError(502, "bad_result")  # adresse, port ou TXT : le nœud a rompu le contrat
        types = s.get("types") if isinstance(s.get("types"), list) else [s.get("type")]
        types = [t for t in dict.fromkeys(str(t) for t in types if t) if t in LAN_TYPES]
        sid = s.get("id")
        if not types or not isinstance(sid, str) or not _ID.match(sid):
            continue
        out.append({"id": sid, "types": types,
                    "name": " ".join(inline(str(s.get("name") or "")).split())[:NAME_MAX]})
    return out


@dataclass
class PhoneLan:
    phone: Any  # PhoneService
    pad_s: float = 5.0

    def services(self, account: str, machine_id: str | None, types: list[str] | None = None,
                 timeout_s: float = 3.0) -> dict[str, Any]:
        from nokker.cloud import placement as pl

        cands = self.phone.candidates(account)
        if machine_id is not None:
            if not any(c.id == machine_id for c in cands):
                raise LanError(404, "not_found")
            cands = [c for c in cands if c.id == machine_id]
        p = pl.place(cands, pl.Needs(caps=("lan_home",)), self.phone.clock())
        if p.machine is None:
            raise LanError(409, "node_offline")
        args = {"timeout_s": timeout_s, **({"types": types} if types else {})}
        t = self.phone.enqueue(account, p.machine.node, "lan.discover", args, deadline_s=60)
        done = self.phone.wait(t.task_id, t.args["timeout_s"] + self.pad_s)
        if done is None or done.status in ("queued", "leased"):
            self.phone.cancel(t.task_id)
            raise LanError(504, "phone_timeout")
        if done.status == "failed":
            raise LanError(502, str((done.error or {}).get("code") or "lan_error")[:40])
        if done.status != "done":
            raise LanError(504, "phone_timeout")
        raw = self.phone.take_ephemeral(done.task_id)
        try:
            got = json.loads(raw or "")
        except ValueError:
            raise LanError(502, "bad_result") from None
        if not isinstance(got, dict):
            raise LanError(502, "bad_result")
        return {"services": clean_services(got.get("services")), "at": self.phone.clock()}


_SERVICE: PhoneLan | None = None


def configure(svc: PhoneLan | None) -> None:
    global _SERVICE
    _SERVICE = svc


def service() -> PhoneLan | None:
    return _SERVICE


__all__ = ["FORBIDDEN", "NAME_MAX", "LanError", "PhoneLan", "clean_services", "configure", "service"]
