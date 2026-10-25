"""Lecture des alertes reçues (docs/LOKKAL.md §7.18) : webhook Prometheus Alertmanager (version 4) et format générique.

Tout ce qui sort d'ici est BORNÉ et normalisé, mais reste une donnée non fiable : rien de ce qui est lu ne devient une
consigne (le moteur Crew le passe aux agents dans un bloc `<untrusted-data>`), et le nom de l'alerte n'est qu'un
identifiant d'événement (`alert:<nom>`, réduit à `[A-Za-z0-9_.-]{1,64}`).

Alertmanager (https://prometheus.io/docs/alerting/latest/configuration/#webhook_config) :
    {"version": "4", "status": "firing|resolved", "receiver", "groupKey", "groupLabels", "commonLabels",
     "commonAnnotations", "externalURL", "alerts": [{"status", "labels", "annotations", "startsAt", "endsAt",
     "generatorURL", "fingerprint"}]}
Générique (une alerte, ou {"alerts": [...]}) :
    {"name" | "alertname", "status"?: "firing|resolved", "severity"?, "title"?, "summary"?, "description"?,
     "labels"?: {clé: valeur}, "fingerprint"?, "started_at"?: ISO 8601}
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from nokker.incidents.types import IncidentError

MAX_ALERTS = 200
MAX_LABELS = 40
_NAME = re.compile(r"[^A-Za-z0-9_.-]+")
_FP = re.compile(r"^[0-9a-f]{8,64}$")
_SEVERITY = re.compile(r"[^a-z0-9_-]+")


@dataclass
class ParsedAlert:
    name: str
    status: str  # firing | resolved
    fingerprint: str
    labels: dict[str, str] = field(default_factory=dict)
    title: str = ""
    summary: str = ""
    severity: str = "warning"
    started_at: float | None = None


def clean_name(value: Any) -> str:
    text = _NAME.sub("-", str(value or "")).strip("-.")[:64]
    return text or "alert"


def _text(value: Any, limit: int) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())[:limit]


def _labels(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in list(value.items())[:MAX_LABELS]:
        key = _text(k, 100)
        if key:
            out[key] = _text(v, 500)
    return out


def _severity(value: Any) -> str:
    text = _SEVERITY.sub("", str(value or "").lower())[:32]
    return text or "warning"


def _when(value: Any) -> float | None:
    if not isinstance(value, str) or not value.strip() or value.startswith("0001-"):
        return None  # Alertmanager envoie 0001-01-01T00:00:00Z pour « pas de fin »
    text = value.strip()
    if text[-1:] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    text = re.sub(r"(\.\d{6})\d+", r"\1", text)  # nanosecondes de Go → microsecondes
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt.timestamp() if dt.tzinfo is not None else None


def fingerprint_of(name: str, labels: dict[str, str]) -> str:
    """Empreinte d'une alerte qui n'en porte pas : son nom et ses étiquettes, triées (même alerte = même empreinte)."""
    raw = json.dumps({"name": name, "labels": sorted(labels.items())}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _status(value: Any) -> str:
    s = str(value or "firing").strip().lower()
    if s not in ("firing", "resolved"):
        raise IncidentError(f"status : firing ou resolved attendu, pas {s[:20]!r}")
    return s


def _one(raw: dict[str, Any], *, common_labels: dict[str, str], common_ann: dict[str, Any], am: bool) -> ParsedAlert:
    if not isinstance(raw, dict):
        raise IncidentError("alerte : objet attendu")
    labels = {**common_labels, **_labels(raw.get("labels"))}
    ann = {**common_ann, **(raw.get("annotations") if isinstance(raw.get("annotations"), dict) else {})}
    name_src = labels.get("alertname") if am else (raw.get("name") or raw.get("alertname") or labels.get("alertname"))
    if not name_src:
        raise IncidentError("alerte sans nom (labels.alertname, ou name)")
    name = clean_name(name_src)
    fp = str(raw.get("fingerprint") or "").strip().lower()
    if not _FP.match(fp):
        fp = fingerprint_of(name, labels)
    if am:
        title = _text(ann.get("summary") or labels.get("alertname"), 200)
        summary = _text(ann.get("description") or ann.get("summary"), 2000)
        severity = _severity(labels.get("severity"))
        started = _when(raw.get("startsAt"))
    else:
        title = _text(raw.get("title") or raw.get("summary") or name_src, 200)
        summary = _text(raw.get("description") or raw.get("summary"), 2000)
        severity = _severity(raw.get("severity") or labels.get("severity"))
        started = _when(raw.get("started_at"))
    return ParsedAlert(name=name, status=_status(raw.get("status")), fingerprint=fp, labels=labels, title=title,
                       summary=summary, severity=severity, started_at=started)


def parse_alerts(fmt: str, body: Any) -> list[ParsedAlert]:
    """Alertes d'un envoi ; `IncidentError` (400) si le corps n'a pas la forme attendue."""
    if not isinstance(body, dict):
        raise IncidentError("corps JSON (objet) attendu")
    if fmt == "alertmanager":
        if str(body.get("version", "")) != "4":
            raise IncidentError("webhook Alertmanager : version 4 attendue")
        alerts = body.get("alerts")
        if not isinstance(alerts, list) or not alerts:
            raise IncidentError("webhook Alertmanager : alerts vide ou absent")
        common = _labels(body.get("commonLabels"))
        ann = body.get("commonAnnotations") if isinstance(body.get("commonAnnotations"), dict) else {}
        am = True
    elif fmt == "generic":
        alerts = body.get("alerts") if "alerts" in body else [body]
        if not isinstance(alerts, list) or not alerts:
            raise IncidentError("alerts : liste non vide attendue")
        common, ann, am = {}, {}, False
    else:
        raise IncidentError(f"format inconnu : {fmt}")
    if len(alerts) > MAX_ALERTS:
        raise IncidentError(f"au plus {MAX_ALERTS} alertes par envoi", status=413, code="too_large")
    return [_one(a, common_labels=common, common_ann=ann, am=am) for a in alerts]


__all__ = ["MAX_ALERTS", "ParsedAlert", "clean_name", "fingerprint_of", "parse_alerts"]
