"""Sites connectés — modèle (docs/CONNECTED-SITES.md §4.2, §4.4, §15).

Un connecteur = (site, libellé) d'UN profil. Plusieurs comptes d'un même site sont possibles (« LinkedIn »,
« LinkedIn pro »). Id stable `st_<10>`, libellé unique par profil sans la casse, un défaut par (profil, site).
"""
from __future__ import annotations

import enum
import logging
import re
import secrets
import time
from dataclasses import asdict, dataclass, field
from typing import Any

ID_RE = re.compile(r"^st_[a-z0-9]{10}$")
_ID_ALPHABET = "abcdefghijkmnpqrstuvwxyz23456789"
LABEL_MAX = 40
MAX_PER_ACCOUNT = 10


def new_id() -> str:
    return "st_" + "".join(secrets.choice(_ID_ALPHABET) for _ in range(10))


class State(str, enum.Enum):
    ABSENT = "absent"
    CONNECTING = "connecting"
    NEEDS_PASSWORD = "needs_password"
    NEEDS_LINK = "needs_link"
    NEEDS_CODE = "needs_code"
    NEEDS_APP_APPROVAL = "needs_app_approval"
    NEEDS_YOU = "needs_you"
    NEEDS_CONSENT = "needs_consent"  # conditions d'utilisation du site à accepter (§4.1)
    CONNECTED = "connected"
    EXPIRED = "expired"
    PAUSED = "paused"
    SUSPENDED = "suspended"
    DISCONNECTED = "disconnected"
    FAILED = "failed"


# Textes de l'écran (§15) ; `{where}` = canal des demandes (§8.6 : « sur Telegram » / « dans l'application Nokker »),
# `{until}` et `{signal}` remplis par `state_text`. Ni « Session » ni « CAPTCHA » (relecture front, 05.11).
STATE_TEXT = {
    State.ABSENT: "Pas encore connecté",
    State.CONNECTING: "Connexion…",
    State.NEEDS_PASSWORD: "Mot de passe demandé {where}",
    State.NEEDS_LINK: "Lien de connexion envoyé par e-mail : récupération dans la boîte reliée…",
    State.NEEDS_CODE: "Code de vérification demandé {where}",
    State.NEEDS_APP_APPROVAL: "Valide sur ton téléphone, puis Fait",
    State.NEEDS_YOU: "Vérification à faire sur l'écran du navigateur",
    State.NEEDS_CONSENT: "Conditions d'utilisation du site à accepter",
    State.CONNECTED: "Connecté ✓",
    State.EXPIRED: "Connexion expirée : reconnexion…",
    State.PAUSED: "En pause jusqu'au {until} ({signal})",
    State.SUSPENDED: "Identifiant refusé : à remplacer",
    State.DISCONNECTED: "Déconnecté",
    State.FAILED: "Connexion abandonnée : {signal}",
}

# Codes stables de `signal` (exposés aux clients à côté de `state_text`) et leur texte.
SIGNAL_TEXT = {
    "": "raison non précisée",
    "password_timeout": "mot de passe non donné à temps",
    "code_timeout": "code non donné à temps",
    "code_rejected": "code refusé deux fois par le site",
    "wrong_code": "code refusé par le site, nouvelle demande",
    "approval_timeout": "validation non faite à temps",
    "link_timeout": "lien non donné à temps",
    "link_other_site": "lien reçu pour un autre site",
    "handoff_timeout": "vérification non faite à temps",
    "captcha": "vérification de sécurité du site",
    "unknown_screen": "écran du site non reconnu",
    "unusual_activity": "activité inhabituelle signalée par le site",
    "bad_credentials": "identifiant refusé par le site",
    "too_many_logins": "trop de reconnexions",
    "too_many_steps": "trop d'étapes",
    "login_page_changed": "page de connexion non reconnue",
    "cancelled": "annulée à la demande",
    "browser_missing": "navigateur des sites absent",
    "busy": "déjà en cours d'utilisation",
    "internal": "erreur interne",
}


UNSPECIFIED = "raison non précisée"


def signal_text(code: str) -> str:
    """Texte d'un signal ; repli FERMÉ sur un code inconnu (journalisé côté noyau), jamais le code brut."""
    if code in SIGNAL_TEXT:
        return SIGNAL_TEXT[code]
    logging.getLogger("nokker.sites").warning("sites : signal inconnu de la table (%r)", str(code)[:40])
    return UNSPECIFIED


class LabelError(ValueError):
    pass


def check_label(label: str) -> str:
    text = " ".join(str(label or "").split())
    if not text or len(text) > LABEL_MAX or any(c in text for c in "/|·") or ID_RE.fullmatch(text):
        raise LabelError("libellé invalide : 1 à 40 caractères, sans « / », « | » ni « · », jamais de la forme d'un id")
    return text


@dataclass
class Connector:
    id: str
    account: str  # profil propriétaire (compte du foyer)
    site: str  # clé du catalogue (`linkedin`, `fakesocial`, `custom`)
    site_name: str  # nom affiché du site (« LinkedIn »)
    label: str
    username: str  # e-mail ou nom d'utilisateur : affiché, pas secret
    default: bool = False
    url: str | None = None  # « Autre site » : URL de départ
    hosts: list[str] = field(default_factory=list)  # hôtes autorisés (recette ou « Autre site »)
    state: State = State.ABSENT
    signal: str = ""  # dernier signal ou raison (pause, échec)
    paused_until: float | None = None
    account_name: str = ""  # nom affiché du compte sur le site (relevé à la connexion)
    created_at: float = field(default_factory=time.time)
    last_ok_at: float | None = None
    last_auto_login_at: float | None = None
    grants: dict[str, str] = field(default_factory=dict)  # account_id -> read|send (§5.4)

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["state"] = self.state.value
        return d

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "Connector":
        d = dict(d)
        d["state"] = State(d.get("state", "absent"))
        return cls(**d)

    def state_text(self, where: str = "dans l'application Nokker") -> str:
        until = time.strftime("%d.%m %H:%M", time.localtime(self.paused_until)) if self.paused_until else ""
        return STATE_TEXT[self.state].format(until=until, signal=signal_text(self.signal), where=where)


__all__ = ["Connector", "ID_RE", "LABEL_MAX", "LabelError", "MAX_PER_ACCOUNT", "SIGNAL_TEXT", "STATE_TEXT", "State",
           "check_label", "new_id", "signal_text"]
