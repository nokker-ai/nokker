"""Sites connectés — un mot de passe écrit dans le chat (docs/CONNECTED-SITES.md §4.6, textes §8.6).

Filet SILENCIEUX : Nina propose toujours la saisie sécurisée ; si la personne écrit quand même un mot de passe ou un
code, `SecretCapture.intercept` le prend AVANT tout le reste du tour (avant `check_input`, avant le stockage, avant le
modèle, y compris pour un tour refusé par un quota) et remplace le message.

- **Armé** (par le résultat `needs_password` / `needs_code` de `sites_connect`, pour UN message de ce fil et de cet
  expéditeur) : le message est pris entier s'il fait une ligne de 1 à 200 caractères sans espace, ou si les règles
  spontanées y trouvent une valeur. Mot de passe → coffre ; code → page (jamais au coffre). Réponse directe du noyau
  (conseil), sans appel du modèle ; le fil garde le placeholder.
- **Spontané** (rien d'armé) : règles exactes `detect_password` ; la valeur est retirée, gardée chiffrée 10 min en
  mémoire, et le noyau propose de l'enregistrer (réponse « LinkedIn pro » / « non » prise elle aussi par le noyau).
"""
from __future__ import annotations

import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PH_ARMED_PASSWORD = "[mot de passe enregistré pour « {label} » — retiré du fil, jamais vu par l'assistant]"
PH_ARMED_CODE = "[code de vérification transmis au site — non conservé]"
PH_SPONTANEOUS = "[mot de passe retiré du fil par sécurité — non enregistré]"

ADVICE_PASSWORD = ("Ton mot de passe est enregistré en sécurité et effacé de la conversation. La prochaine fois, "
                   "utilise la saisie sécurisée : c'est plus sûr que de l'écrire ici.")
ADVICE_CODE = ("Ton code a été transmis au site et effacé de la conversation. La prochaine fois, utilise la saisie "
               "sécurisée : c'est plus sûr que de l'écrire ici.")
OFFER = "J'ai retiré un mot de passe de ton message avant de le lire. Veux-tu l'enregistrer pour un de tes sites ?"
CHANGE_HINT = ("S'il a pu être vu ailleurs (copie d'écran, appareil partagé), tu peux le changer sur le site.")
SAVED_SPONTANEOUS = ADVICE_PASSWORD + " " + CHANGE_HINT
NOT_SAVED = "D'accord, il n'est enregistré nulle part et il est effacé de la conversation. " + CHANGE_HINT

# ─────────────────────────────── règles exactes (§4.6) ───────────────────────────────

_KW = re.compile(r"(?i)\b(?:mot[ -]de[ -]passe|motdepasse|mdp|password|passwd|pwd|passwort)\b|\bpass(?=\s*[:=])")
_STOP = {"oublié", "oubliée", "oublie", "perdu", "perdue", "inconnu", "changé", "change", "expiré", "réinitialisé",
         "vide", "aucun", "none", "?", "…", "...", "***", "xxx", "sur", "dans", "le", "la", "les", "un", "une", "mon",
         "ma", "ton", "ta", "pas", "est", "c'est", "is", "trop"}
_COMPL = r"(?:\s+[^\s:=]+){%d}"
_LINK = r"\s+(?:est|c'est|c’est|is|ist|→)\s+"
_URL = re.compile(r"(?i)^(https?://|www\.)")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
_UUID = re.compile(r"(?i)^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_HEX = re.compile(r"(?i)^[0-9a-f]{16,}$")
_HANDLE = re.compile(r"^\[secret:[^\]]+\]$")


def _clean(v: str) -> str:
    v = v.strip()
    while v and v[-1] in ".,;)»\"'" and len(v) > 1:
        v = v[:-1]
    return v.lstrip("«\"'(")


def strong(v: str) -> bool:
    """VALEUR « forte » : 8-128 caractères, sans espace, lettre ET chiffre, ET majuscule ou symbole ; ni URL, ni
    adresse, ni poignée, ni UUID, ni hash hexadécimal."""
    if not (8 <= len(v) <= 128) or any(c.isspace() for c in v):
        return False
    if _URL.match(v) or _EMAIL.match(v) or _UUID.match(v) or _HEX.match(v) or _HANDLE.match(v):
        return False
    has_letter = any(c.isalpha() for c in v)
    has_digit = any(c.isdigit() for c in v)
    has_upper_or_sym = any(c.isupper() for c in v) or any(not c.isalnum() for c in v)
    return has_letter and has_digit and has_upper_or_sym


def _value_ok(v: str, need_strong: bool) -> bool:
    if not v or v.casefold() in _STOP or _URL.match(v) or _EMAIL.match(v) or _HANDLE.match(v):
        return False
    return strong(v) if need_strong else len(v) >= 4


def detect_password(text: str) -> tuple[str, int, int] | None:
    """(valeur, début, fin) de la première valeur trouvée ; pour chaque mot-clé, R1 (« : » ou « = »), puis R2 (« est »
    et ses équivalents, VALEUR forte), puis R3 (VALEUR forte juste après), avec 0 à 3 mots de complément."""
    text = text or ""
    for kw in _KW.finditer(text):
        rest, base = text[kw.end():], kw.end()
        rules = [(re.compile(r"^" + _COMPL % n + r"\s*[:=](?!//)\s*(\S+)"), False) for n in range(4)]
        rules += [(re.compile(r"^" + _COMPL % n + _LINK + r"(\S+)", re.I), True) for n in range(4)]
        rules += [(re.compile(r"^" + _COMPL % n + r"\s+(\S+)"), True) for n in range(4)]
        for rx, need_strong in rules:
            m = rx.match(rest)
            if not m:
                continue
            raw = m.group(1)
            v = _clean(raw)
            if _value_ok(v, need_strong):
                start = base + m.start(1) + raw.find(v)
                return v, start, start + len(v)
    return None


# ─────────────────────────────── capture ───────────────────────────────

@dataclass
class Capture:
    text: str  # texte du message après capture (ce que voient le fil et le modèle)
    direct_reply: str | None = None  # réponse du noyau sans appel du modèle
    notice: str | None = None  # ajouté à la réponse du tour (hors contexte du modèle)
    kind: str = ""  # armed_password | armed_code | spontaneous | choice
    value: str | None = None  # la valeur captée, pour l'ajouter aux secrets connus (jamais rangée ailleurs)


class _Held:
    """Tampon chiffré de la capture spontanée : 10 min, en mémoire, clé du processus."""

    def __init__(self, ttl_s: float, clock: Callable[[], float]) -> None:
        self._key = AESGCM.generate_key(bit_length=256)
        self._items: dict[tuple[str, str], tuple[float, bytes, bytes]] = {}
        self.ttl_s, self.clock = ttl_s, clock
        self._lock = threading.Lock()

    def put(self, k: tuple[str, str], value: str) -> None:
        nonce = os.urandom(12)
        with self._lock:
            self._items[k] = (self.clock() + self.ttl_s, nonce, AESGCM(self._key).encrypt(nonce, value.encode(), None))

    def pop(self, k: tuple[str, str]) -> str | None:
        with self._lock:
            item = self._items.pop(k, None)
        if item is None or self.clock() > item[0]:
            return None
        return AESGCM(self._key).decrypt(item[1], item[2], None).decode()

    def has(self, k: tuple[str, str]) -> bool:
        item = self._items.get(k)
        return item is not None and self.clock() <= item[0]


class SecretCapture:
    def __init__(self, requests: Any, *, save_password: Callable[[str, str, str], str | None],
                 labels: Callable[[str], list[tuple[str, str]]], who: Callable[[str | None], str],
                 remember: Callable[[str, str], None] | None = None, clock: Callable[[], float] = time.time,
                 hold_s: float = 600.0) -> None:
        self.requests = requests
        self.save_password = save_password  # (compte, id ou libellé, valeur) -> libellé enregistré, ou None
        self.labels = labels  # compte -> [(id, libellé)] de SES connecteurs
        self.who = who  # expéditeur -> compte
        self.remember = remember  # (nom, valeur) -> secrets connus du caviardage
        self.clock = clock
        self._held = _Held(hold_s, clock)

    def _known(self, value: str) -> None:
        if self.remember is not None:
            self.remember("sites-chat-capture", value)

    def intercept(self, thread_id: str, sender: str | None, text: str) -> Capture | None:
        text = text or ""
        key = (thread_id, sender or "")
        # 1. réponse à une proposition d'enregistrement (capture spontanée en attente)
        if self._held.has(key):
            choice = self._choice(key, sender, text)
            if choice is not None:
                return choice
        # 2. capture armée par le résultat de sites_connect
        arm = self.requests.take_arm(thread_id, sender)
        if arm is not None:
            stripped = text.strip()
            value = None
            if stripped and "\n" not in stripped and len(stripped) <= 200 and not any(c.isspace() for c in stripped):
                value = stripped
            else:
                found = detect_password(text)
                value = found[0] if found else None
            if value is not None:
                self._known(value)
                try:
                    self.requests.answer_plain(arm.request_id, value)
                except Exception:  # noqa: BLE001 — demande expirée entre-temps : la valeur est retirée quand même
                    pass
                if arm.kind == "code":
                    return Capture(text=PH_ARMED_CODE, direct_reply=ADVICE_CODE, kind="armed_code", value=value)
                return Capture(text=PH_ARMED_PASSWORD.format(label=arm.label), direct_reply=ADVICE_PASSWORD,
                               kind="armed_password", value=value)
        # 3. capture spontanée
        found = detect_password(text)
        if found is None:
            return None
        value, start, end = found
        self._known(value)
        self._held.put(key, value)
        replaced = text[:start] + PH_SPONTANEOUS + text[end:]
        mine = self.labels(self.who(sender))
        offer = OFFER + (" Réponds avec le nom du site (" + ", ".join(lab for _, lab in mine) + "), ou « non »."
                         if mine else " Tu n'as pas encore de site connecté : réponds « non » pour l'oublier.")
        return Capture(text=replaced, notice=offer, kind="spontaneous", value=value)

    def _choice(self, key: tuple[str, str], sender: str | None, text: str) -> Capture | None:
        answer = " ".join(text.split()).strip(" .!").casefold()
        if not answer or len(answer) > 60:
            return None
        if answer in ("non", "no", "nein", "oublie", "oublie-le", "annule"):
            self._held.pop(key)
            return Capture(text=text, direct_reply=NOT_SAVED, kind="choice")
        account = self.who(sender)
        for cid, label in self.labels(account):
            if answer in (label.casefold(), cid):
                value = self._held.pop(key)
                if value is None:
                    return None
                saved = self.save_password(account, cid, value)
                if saved is None:
                    return None
                return Capture(text=text, direct_reply=SAVED_SPONTANEOUS, kind="choice")
        return None


__all__ = ["ADVICE_CODE", "ADVICE_PASSWORD", "CHANGE_HINT", "Capture", "NOT_SAVED", "OFFER", "PH_ARMED_CODE",
           "PH_ARMED_PASSWORD", "PH_SPONTANEOUS", "SAVED_SPONTANEOUS", "SecretCapture", "detect_password", "strong"]
