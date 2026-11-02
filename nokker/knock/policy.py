"""Heurtoir — réglages et règles (docs/KNOCK.md §4-§5). Aucun réseau, aucun audio.

**Frapper pour approuver** ne vaut que pour une carte STANDARD dont la règle est dans `KNOCK_APPROVABLE_RULES` (liste
FERMÉE : la configuration peut la réduire, jamais l'étendre). Une carte forte (`strength == "strong"`) et toute carte à
plancher « clé d'appareil » (prod, publication de page, profil mineur, connecteur relayé ou boîte locale, maison
sensible) restent sur passkey, Face ID ou Mini App : `knock_refusal` le dit, `tests/unit/knock/test_policy.py` le
vérifie règle par règle.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from nokker.knock.types import KINDS

# Seules règles de carte qu'un toc peut approuver (toujours avec `strength == "standard"`). Fermée : ajouter une règle
# ici est une décision produit, avec son test.
# Décision produit du 02.11.2026 (revue des textes modèle) : une frappe n'est pas une preuve d'identité et un clap peut être un
# faux positif ; ni envoi externe ni dépense au heurtoir.
KNOCK_APPROVABLE_RULES = frozenset({
    "permissions:default",       # écriture demandée en mode « default »
    "permissions:accept_edits",  # idem en mode « accept_edits »
})

# Jamais par un toc, même si une carte de ces règles devenait standard un jour (défense en profondeur).
NEVER_BY_KNOCK = frozenset({
    "household:guardian", "pages:publish", "ops:prod", "home:strong",
    "permissions:relayed_connector", "permissions:local_connector_strong",
    "effect:send_external", "effect:spend",
})
# Familles entières jamais par un toc (paiements, auto-amélioration), quelle que soit la règle exacte.
NEVER_BY_KNOCK_PREFIXES = ("payments:", "improve:", "ops:prod:")

# Fenêtre d'une carte armée : 60 s au plus (la config peut la réduire, jamais l'allonger).
APPROVE_MAX_WINDOW_S = 60.0

DECIDED_VIA = "knock+presence"
PRESENCE_SOURCES = ("home_assistant", "wifi")

_LABEL_BAD = re.compile(r"[\[\]\x00-\x1f\x7f<>`]")
_ROOM_ID = re.compile(r"^[a-z0-9_]{1,40}$")  # = nokker.ambient.presence.valid_room


def clean_label(text: str, limit: int = 40) -> str:
    """Nom de pièce montré au modèle : sans crochets (pas de faux bloc), sans contrôle, borné."""
    return " ".join(_LABEL_BAD.sub(" ", str(text or "")).split())[:limit] or "pièce"


@dataclass(frozen=True)
class Room:
    id: str
    label: str
    thread_id: str   # fil de la session vocale de cette pièce (son locataire = celui du fil)
    sender: str      # identité sous laquelle tourne la session vocale (profil du foyer, pas un fil privé)


@dataclass(frozen=True)
class ApproveSettings:
    enabled: bool = False
    rules: frozenset[str] = KNOCK_APPROVABLE_RULES
    window_s: float = APPROVE_MAX_WINDOW_S  # une carte armée l'est pour 60 s au plus
    presence_max_age_s: float = 300.0  # présence vue il y a 5 min au plus
    sources: tuple[str, ...] = PRESENCE_SOURCES
    same_room: bool = False            # exiger que la présence soit dans la pièce du toc (si la source le sait)


@dataclass(frozen=True)
class KnockConfig:
    enabled: bool = False
    rooms: dict[str, Room] = field(default_factory=dict)
    kinds: tuple[str, ...] = KINDS
    cooldown_s: float = 4.0            # un seul déclencheur par pièce dans cette fenêtre
    max_skew_s: float = 30.0           # un événement plus vieux est ignoré
    approve: ApproveSettings = ApproveSettings()
    voice_privacy: str = "personal"    # réponse de session : personal (lue si le destinataire est seul) | common

    @classmethod
    def from_raw(cls, raw: dict[str, Any] | None) -> KnockConfig:
        raw = raw or {}
        if not raw.get("enabled"):
            return cls()
        rooms: dict[str, Room] = {}
        for rid, r in (raw.get("rooms") or {}).items():
            rid = str(rid)
            if not _ROOM_ID.match(rid):
                raise ValueError(f"knock.rooms : identifiant de pièce invalide {rid!r} ([a-z0-9_], 40 car.)")
            r = r or {}
            if not r.get("thread_id") or not r.get("sender"):
                raise ValueError(f"knock.rooms.{rid} : thread_id et sender requis")
            rooms[rid] = Room(id=rid, label=clean_label(r.get("label") or rid), thread_id=str(r["thread_id"]),
                              sender=str(r["sender"]))
        a = raw.get("approve") or {}
        rules = frozenset(a.get("rules") or KNOCK_APPROVABLE_RULES)
        extra = rules - KNOCK_APPROVABLE_RULES
        if extra:
            raise ValueError(f"knock.approve.rules : hors de la liste fermée : {sorted(extra)}")
        window_s = float(a.get("window_s", APPROVE_MAX_WINDOW_S))
        if not 0 < window_s <= APPROVE_MAX_WINDOW_S:
            raise ValueError(f"knock.approve.window_s : {window_s} (0 < fenêtre ≤ {APPROVE_MAX_WINDOW_S:g} s)")
        sources = tuple(a.get("sources") or PRESENCE_SOURCES)
        if set(sources) - set(PRESENCE_SOURCES):
            raise ValueError(f"knock.approve.sources : {sources} (permis : {PRESENCE_SOURCES})")
        kinds = tuple(raw.get("kinds") or KINDS)
        if set(kinds) - set(KINDS):
            raise ValueError(f"knock.kinds : {kinds} (permis : {KINDS})")
        privacy = str(raw.get("voice_privacy") or "personal")
        if privacy not in ("personal", "common"):
            raise ValueError("knock.voice_privacy : personal | common")
        return cls(enabled=True, rooms=rooms, kinds=kinds, cooldown_s=float(raw.get("cooldown_s", 4.0)),
                   voice_privacy=privacy,
                   max_skew_s=float(raw.get("max_skew_s", 30.0)),
                   approve=ApproveSettings(enabled=bool(a.get("enabled", False)), rules=rules,
                                           window_s=window_s,
                                           presence_max_age_s=float(a.get("presence_max_age_s", 300.0)),
                                           sources=sources, same_room=bool(a.get("same_room", False))))


def knock_refusal(approval: Any, allowed_rules: frozenset[str] = KNOCK_APPROVABLE_RULES) -> str | None:
    """None si un toc peut approuver cette carte ; sinon la raison (en français, pour le journal et la voix)."""
    rule = str(getattr(approval, "rule", "") or "")
    if getattr(approval, "strength", None) != "standard":
        return "carte forte : passkey, Face ID ou Mini App seulement"
    if rule in NEVER_BY_KNOCK or rule.startswith(NEVER_BY_KNOCK_PREFIXES):
        return "carte à clé d'appareil : passkey, Face ID ou Mini App seulement"
    if rule not in allowed_rules or rule not in KNOCK_APPROVABLE_RULES:
        return f"règle {rule or '?'} hors de la liste des cartes approuvables au heurtoir"
    return None


TRIGGER_TEXT = {
    "knock": "trois coups frappés (toc toc toc)",
    "clap": "trois claps de main",
    "deck": "touche physique",
    "nfc": "étiquette NFC",
}


def session_block(kind: str, room_label: str, speaker: str | None = None, means: str | None = None) -> str:
    """Le SEUL texte que le modèle voit à l'ouverture d'une session vocale par déclencheur physique (docs/KNOCK.md
    §5.1). Il est le message du tour : pas de son, pas de transcription, rien d'autre. Trois coups ne sont pas une
    authentification : sans identification par le hub (`speaker` + `means`), l'interlocuteur est « non identifié »."""
    if speaker and means:
        who = f"Interlocuteur : {clean_label(speaker)} (identifié par {clean_label(means)})\n"
    else:
        who = ("Interlocuteur : non identifié (droits de la pièce). Ne dis à voix haute aucune information personnelle "
               "d'un membre (courrier, agenda, messages) ; s'il en faut une, propose de l'envoyer sur son appareil.\n")
    return ("[Session vocale]\n"
            f"Déclencheur : {TRIGGER_TEXT.get(kind, 'déclencheur physique')}\n"
            f"Pièce : {clean_label(room_label)}\n"
            + who +
            "Sortie : voix dans la pièce. Réponds en une phrase courte, à l'oral, sans mise en forme, ni liste, ni lien : "
            "la personne va parler.\n"
            "[/Session vocale]")
