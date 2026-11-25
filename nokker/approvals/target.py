"""Cible lisible d'une carte d'approbation (champ `target` du JSON de carte, docs/CLIENT-API.md).

Le web n'affiche pas `arguments` (peut porter une valeur tapée par la personne) : on lui rend à la place ce que
l'outil va toucher — commande, chemin, URL, destinataire et sujet, entité et service. La cible est :
  * caviardée comme tout ce qui entre dans l'historique (`qa.secrets.redact`, puis AVS, puis cartes bancaires) ;
  * normalisée NFC, sans caractère de contrôle (Cc/Cf : retours à la ligne, tabulations → une espace) ;
  * d'au plus `MAX_LEN` caractères, tronquée APRÈS le caviardage sans couper une poignée `[secret:…]` en deux ;
  * absente (None) pour un outil sans extracteur ou sans valeur.
Calculée UNE fois, à la création de la carte (`BaseApprovals.request`), et stockée : jamais recalculée à la lecture.

Étendre : ajouter une entrée à `EXTRACTORS` (nom d'outil → fonction `arguments -> texte brut | None`)."""
from __future__ import annotations

import unicodedata
from collections.abc import Callable
from typing import Any

MAX_LEN = 200
_SEP = " — "
_ELLIPSIS = "…"


def _s(arguments: dict[str, Any], key: str) -> str:
    v = arguments.get(key)
    return v if isinstance(v, str) else ""


def _first(arguments: dict[str, Any], *keys: str) -> str:
    for k in keys:
        v = _s(arguments, k).strip()
        if v:
            return v
    return ""


def _join(*parts: str) -> str | None:
    kept = [p for p in parts if p.strip()]
    return _SEP.join(kept) if kept else None


def _command(a: dict[str, Any]) -> str | None:
    return _s(a, "command") or None


def _path(a: dict[str, Any]) -> str | None:
    return _s(a, "path") or None


def _urls(a: dict[str, Any]) -> str | None:
    raw = a.get("urls")
    urls = [u for u in raw if isinstance(u, str) and u.strip()] if isinstance(raw, list) else []
    if not urls:
        return _s(a, "url") or None
    return urls[0] if len(urls) == 1 else f"{urls[0]} (+{len(urls) - 1})"


def _url(a: dict[str, Any]) -> str | None:
    return _s(a, "url") or None


def _mail(a: dict[str, Any]) -> str | None:
    """`mail_send` / `mail_draft` (nokker/connectors/mail.py) : `to` (obligatoire) et `subject`."""
    return _join(_s(a, "to").strip(), _s(a, "subject"))


def _message(a: dict[str, Any]) -> str | None:
    """`send_message` du runtime amont : `target` = « plateforme:identifiant » (action send|react|unreact ; pas de sujet)."""
    return _s(a, "target").strip() or None


def _home(a: dict[str, Any]) -> str | None:
    domain, service, entity = _s(a, "domain").strip(), _s(a, "service").strip(), _s(a, "entity_id").strip()
    if not (service or entity):
        return None
    return _join(f"{domain}.{service}" if domain and service else (service or domain), entity)


EXTRACTORS: dict[str, Callable[[dict[str, Any]], str | None]] = {
    "terminal": _command,
    "process_start": _command,
    "write_file": _path,
    "patch": _path,  # mode « patch » (V4A, plusieurs fichiers) : pas de `path` → pas de cible
    "read_file": _path,
    "web_fetch": _urls,
    "browser_navigate": _url,
    "mail_send": _mail,
    "mail_draft": _mail,
    "send_message": _message,
    # send_email_reply, send_email_forward, send_telegram_message : aucun schéma connu (ni chez nous ni chez l'amont) →
    # pas d'extracteur (cible absente) tant qu'un schéma réel n'existe pas.
    "maison_action_forte": _home,
}


def _redact(text: str, known: Any = None) -> str:
    """Le caviardage de l'historique (`Invariants.check_input`) : coffre de valeurs connues (`known`), secrets, puis AVS,
    puis numéros de carte."""
    from nokker.payments.cardmask import mask_card_text
    from nokker.qa.secrets import redact
    from nokker.redact.avs import mask_avs

    text = redact(text, known).text
    text = mask_avs(text)[0]
    return mask_card_text(text)


def _clean(text: str) -> str:
    """NFC ; \\n \\r \\t (et autres espaces) → une espace ; le reste de Cc/Cf éliminé ; espaces réduites."""
    text = unicodedata.normalize("NFC", text)
    out = []
    for ch in text:
        if ch.isspace():
            out.append(" ")
        elif unicodedata.category(ch) in ("Cc", "Cf"):
            continue
        else:
            out.append(ch)
    return unicodedata.normalize("NFC", " ".join("".join(out).split()))


def _truncate(text: str, limit: int = MAX_LEN) -> str:
    if len(text) <= limit:
        return text
    from nokker.qa.secrets import HANDLE_RE

    cut = limit - len(_ELLIPSIS)
    for m in HANDLE_RE.finditer(text):
        if m.start() < cut < m.end():  # la coupe tombe dans une poignée : on s'arrête avant elle
            cut = m.start()
            break
    return text[:cut].rstrip() + _ELLIPSIS


def card_target(tool_name: str, arguments: Any, known: Any = None) -> str | None:
    """Cible à figer sur la carte, ou None (outil inconnu, arguments sans cible, extracteur en échec). `known` : le coffre
    de valeurs connues (`KnownSecrets`) : une valeur du coffre est masquée même sans motif reconnaissable."""
    fn = EXTRACTORS.get(tool_name)
    if fn is None or not isinstance(arguments, dict):
        return None
    try:
        raw = fn(arguments)
        if not raw:
            return None
        out = _truncate(_clean(_redact(raw, known)))
    except Exception:  # noqa: BLE001 — jamais d'échec de création de carte pour un champ d'affichage
        return None
    return out or None


__all__ = ["EXTRACTORS", "MAX_LEN", "card_target"]
