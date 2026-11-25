"""Quota d'inférence atteint, tel qu'un amont (le service cloud de Nokker) le dit : un 403 ou un 429 dont le CORPS porte
`{"error": {"code": "quota_exceeded", "retry_at": "<ISO 8601>"}}`. Ce n'est pas une panne : ni repli de palier, ni
réessai, ni escalade (`loop.py`, `providers/sokkan.py`, `providers/hedged.py`).

Lecture : d'abord les attributs `error_code` / `retry_at` posés par le fournisseur (`tag_error`, corps entier), puis,
en REPLI (autres fournisseurs, anciennes exceptions), le message de l'exception. Défensive : jamais d'exception, un préfixe borné du corps ; tout ce qui n'est pas exactement ce motif (corps
non JSON, `error` en chaîne, `code` absent ou autre) rend None et la classification par statut reste celle d'avant.
"""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

QUOTA_CODE = "quota_exceeded"
QUOTA_STATUSES = (403, 429)
BODY_PREFIX = 4000  # repli : préfixe lu dans un MESSAGE d'exception (déjà tronqué à 500 par le fournisseur)
BODY_MAX = 64 * 1024  # corps entier lu par le fournisseur : au-delà, aucun attribut structuré
FIELD_MAX = 200  # longueur max de `error_code` et de la chaîne brute de `retry_at`
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_HTTP_HEAD = re.compile(r"HTTP (\d{3})\b")
_DECODER = json.JSONDecoder()


def _iso_utc(value: Any) -> str | None:
    """`retry_at` d'un corps : ISO 8601 lisible, normalisé en UTC (`2026-11-19T12:00:00Z`) ; None si illisible."""
    if not isinstance(value, str) or not value.strip() or len(value) > 64:
        return None
    text = value.strip()
    try:
        at = datetime.fromisoformat(text[:-1] + "+00:00" if text[-1:] in ("Z", "z") else text)
    except ValueError:
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    return at.astimezone(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def quota_body(text: Any) -> dict[str, str | None] | None:
    """`{"retry_at": ISO | None}` si `text` (corps, ou message d'exception « HTTP 403 de … : {corps} ») porte le code
    `quota_exceeded` ; None sinon."""
    try:
        if not isinstance(text, str):
            return None
        head = text[:BODY_PREFIX]
        start = head.find("{")
        if start < 0:
            return None
        data, _ = _DECODER.raw_decode(head, start)
        err = data.get("error") if isinstance(data, dict) else None
        if not isinstance(err, dict) or err.get("code") != QUOTA_CODE:
            return None
        return {"retry_at": _iso_utc(err.get("retry_at"))}
    except Exception:  # noqa: BLE001 — une lecture de corps ne doit jamais faire échouer l'appel
        return None


def _short(value: Any) -> str | None:
    if not isinstance(value, str) or not value or len(value) > FIELD_MAX or _CONTROL.search(value):
        return None
    return value


def tag_error(exc: BaseException, body: Any) -> BaseException:
    """Pose sur l'exception d'une réponse HTTP d'erreur, au moment où le fournisseur lit le corps ENTIER (≤ 64 Ko),
    `error_code` (`error.code`) et `retry_at` (`error.retry_at`, ISO UTC). Rien posé si le corps est trop gros,
    n'est pas du JSON, si `error` n'est pas un objet ou si `code` n'est pas une chaîne courte. Jamais de levée : le
    message de l'exception, lui, reste celui que le fournisseur a construit (tronqué comme avant)."""
    try:
        if isinstance(body, str) and 0 < len(body) <= BODY_MAX:
            data = json.loads(body)
            err = data.get("error") if isinstance(data, dict) else None
            code = _short(err.get("code")) if isinstance(err, dict) else None
            if code is not None:
                exc.error_code = code  # type: ignore[attr-defined]
                retry_at = _iso_utc(err.get("retry_at")) if code == QUOTA_CODE else None
                if retry_at is not None:
                    exc.retry_at = retry_at  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 — jamais de levée à la lecture d'une erreur
        pass
    return exc


def _status(e: BaseException) -> int | None:
    for obj in (e, getattr(e, "response", None)):
        for attr in ("status", "status_code"):
            v = getattr(obj, attr, None)
            if isinstance(v, int) and not isinstance(v, bool) and 100 <= v <= 599:
                return v
    m = _HTTP_HEAD.match(str(e))
    return int(m.group(1)) if m else None


def quota_of(e: BaseException) -> dict[str, str | None] | None:
    """Quota atteint d'UNE exception (pas de chaîne `__cause__` ici) : statut 403/429 ET corps `quota_exceeded`."""
    try:
        if _status(e) not in QUOTA_STATUSES:
            return None
        code = getattr(e, "error_code", None)
        if isinstance(code, str):  # posé par le fournisseur d'après le corps entier : fait foi
            if code != QUOTA_CODE:
                return None
            return {"retry_at": _iso_utc(getattr(e, "retry_at", None))}
        for body in (getattr(e, "text", None), getattr(getattr(e, "response", None), "text", None), str(e)):
            got = quota_body(body)
            if got is not None:
                return got
    except Exception:  # noqa: BLE001
        pass
    return None


def quota_in_chain(failure: BaseException | None) -> dict[str, str | None] | None:
    """Comme `quota_of`, en suivant `__cause__` (le fournisseur enrobe les erreurs)."""
    e, seen = failure, 0
    while e is not None and seen < 6:
        got = quota_of(e)
        if got is not None:
            return got
        e, seen = e.__cause__, seen + 1
    return None


__all__ = ["QUOTA_CODE", "QUOTA_STATUSES", "quota_body", "quota_in_chain", "quota_of", "tag_error"]
