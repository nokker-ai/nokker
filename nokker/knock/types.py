"""Heurtoir — types et interfaces (docs/KNOCK.md §4-§5).

Le noyau ne touche jamais le réseau du foyer ni un micro : il reçoit des **déclencheurs physiques** déjà reconnus par
la passerelle `home-bridge` (toc toc toc ou clap entendu par `home-audio` ou un satellite, touche Stream Deck,
étiquette NFC) et en tire deux actions : ouvrir une session vocale dans la pièce, ou approuver une carte STANDARD armée
pour cette pièce quand un membre autorisé est présent. Aucun son n'arrive au noyau avant le déclencheur.

Interfaces seulement pour ce qui vient d'autres chantiers :
- `AmbientOutput` : la sortie audio dans la pièce (chantier ambient-out : `RoomAudio.say`, adaptateur
  `nokker.knock.bridge.RoomAudioOutput`) ;
- `Presence` : qui est à la maison, et où (chantier home-assistant : présence HA, appareils sur le Wi-Fi).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

KINDS = ("knock", "clap", "deck", "nfc")
INTENTS = ("call", "approve")


@dataclass(frozen=True)
class PhysicalTrigger:
    """Un déclencheur reconnu par la passerelle. Aucun son : seulement le fait, la pièce et l'heure.

    `event_id` : identifiant unique côté passerelle (dédoublonnage) ; `source` : quel capteur (`mic:salon`,
    `sat:cuisine`, `deck:3`, `nfc:04a2…`) ; `intent` : None pour un toc (le noyau choisit : carte armée dans la pièce →
    approuver, sinon appeler), imposé pour une touche ou une étiquette configurée « appeler » ou « approuver »."""

    event_id: str
    kind: str
    room: str
    at: float
    source: str = ""
    confidence: float | None = None
    intent: str | None = None


@dataclass(frozen=True)
class PresenceFact:
    """Un membre vu à la maison : `source` = `home_assistant` (personne / zone HA) ou `wifi` (appareil associé)."""

    member: str
    source: str
    seen_at: float
    room: str | None = None


class Presence(Protocol):
    """Présence physique (à coder par le chantier home-assistant, docs/KNOCK.md §5.3).

    Rend les membres vus à la maison pour ce locataire ; le noyau filtre lui-même la fraîcheur (`seen_at`) et la
    pièce. Ne doit JAMAIS rendre un membre d'un autre locataire. Lève une exception si la source est injoignable : le
    noyau refuse alors l'approbation (jamais « présent » par défaut)."""

    def present(self, tenant: str) -> list[PresenceFact]: ...


@dataclass(frozen=True)
class SpokenLine:
    """Ce que la sortie ambiante doit dire ou jouer dans une pièce."""

    room: str
    text: str = ""
    chime: str | None = None   # "listening" | "approved" | "refused" | None
    private: bool = False      # contenu personnel : la sortie peut le remplacer par une annonce neutre
    recipient: str | None = None  # destinataire d'un contenu personnel (lu s'il est seul dans la pièce)


class AmbientOutput(Protocol):
    """Sortie audio dans la pièce (à coder par le chantier ambient-out, docs/KNOCK.md §5.2). Appelée par le noyau,
    exécutée par home-bridge (Music Assistant, Snapcast, Sonos, AirPlay…). Ne bloque pas le tour : erreur = journal."""

    def say(self, line: SpokenLine) -> None: ...


@dataclass
class Outcome:
    """Ce que le noyau a fait d'un déclencheur (journal, réponse à la passerelle, tests)."""

    action: str               # "voice_session" | "approved" | "refused" | "ignored" | "disabled"
    reason: str = ""
    room: str = ""
    approval_id: str | None = None
    thread_id: str | None = None
    decided_by: str | None = None
    details: dict = field(default_factory=dict)


class NullAmbientOutput:
    """Pas encore de sortie ambiante branchée : on ne dit rien (journal seulement)."""

    def __init__(self) -> None:
        self.lines: list[SpokenLine] = []

    def say(self, line: SpokenLine) -> None:
        self.lines.append(line)
        del self.lines[:-20]


class NoPresence:
    """Pas de source de présence branchée : personne n'est présent, donc aucune approbation par toc."""

    def present(self, tenant: str) -> list[PresenceFact]:
        return []
