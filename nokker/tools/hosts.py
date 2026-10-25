"""nokker/tools/hosts.py — inventaire des hôtes ssh et `known_hosts` de l'utilisateur (lot X11, docs/LOKKAL.md §7.14).

Inventaire : `hosts.yaml` dans les réglages de l'UTILISATEUR (à côté de `settings.json`), jamais dans un dépôt — un
dépôt vient avec son code, donc d'un tiers possible, et ne déclare pas d'hôte. Lu une fois, à l'ouverture de la
session (comme les hooks) : un fichier réécrit pendant la session n'y change rien.

    hosts:
      - name: web1                 # nom donné au modèle (seul identifiant qu'il manie)
        address: 10.0.0.5          # nom DNS ou IP
        port: 22                   # défaut 22
        user: deploy
        key: secret:SSH_WEB1       # clé privée au coffre (X4), jamais un chemin ni une clé en clair
        role: prod                 # prod | preprod | dev, obligatoire (jamais déduit)
        jump: bastion              # facultatif : un autre hôte de l'inventaire, un seul saut

Une entrée invalide est ignorée EN ENTIER, avec un avis (un hôte sans rôle n'est jamais traité comme `dev`).

`known_hosts` : format OpenSSH, tenu par l'utilisateur (à côté de `hosts.yaml`). Le noyau le LIT à chaque appel et
n'y AJOUTE une ligne qu'après une carte approuvée qui montrait l'empreinte (`ssh_trust`) ; il ne remplace jamais une
clé déjà connue (une clé qui change se règle à la main, c'est peut-être une interception).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROLES = ("prod", "preprod", "dev")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
# Adresse : nom DNS, IPv4, IPv6 sans crochets. Ni espace, ni « - » en tête (option de ssh), ni « % » (jeton de
# ProxyCommand), ni « @ » (utilisateur glissé dans l'adresse).
_ADDRESS = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.:-]{0,252}$")
_USER = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,31}$")
_KEY_TYPE = re.compile(r"^(ssh-[a-z0-9-]+|ecdsa-sha2-[a-z0-9-]+|sk-[a-z0-9@.-]+|rsa-sha2-[0-9]+)$")
_B64 = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
_KEYS = {"name", "address", "port", "user", "key", "role", "jump"}


@dataclass(frozen=True)
class Host:
    name: str
    address: str
    port: int
    user: str
    key: str  # NOM du secret au coffre (sans le préfixe `secret:`)
    role: str
    jump: str | None = None

    @property
    def known_key(self) -> str:
        """Le nom sous lequel OpenSSH cherche l'hôte dans known_hosts : `adresse`, ou `[adresse]:port`."""
        return self.address if self.port == 22 else f"[{self.address}]:{self.port}"

    def view(self) -> dict[str, Any]:
        """Ce que le modèle et l'API voient d'un hôte : jamais la clé (ni son nom au coffre)."""
        return {"name": self.name, "address": self.address, "port": self.port, "user": self.user, "role": self.role,
                "jump": self.jump}


@dataclass
class Inventory:
    path: str
    hosts: dict[str, Host] = field(default_factory=dict)
    notices: list[str] = field(default_factory=list)

    def get(self, name: str) -> Host | None:
        return self.hosts.get(str(name or "").strip())

    def chain(self, host: Host) -> list[Host]:
        """Les hôtes touchés pour joindre `host`, dans l'ordre : le saut éventuel, puis l'hôte."""
        jump = self.hosts.get(host.jump) if host.jump else None
        return [jump, host] if jump is not None else [host]

    def names(self) -> list[str]:
        return sorted(self.hosts)


def _entry(raw: Any, index: int) -> tuple[Host | None, str | None]:
    if not isinstance(raw, dict):
        return None, f"hôte n°{index + 1} : un objet est attendu"
    label = str(raw.get("name") or f"n°{index + 1}")
    unknown = sorted(set(raw) - _KEYS)
    if unknown:
        return None, f"hôte {label} ignoré : clé(s) inconnue(s) {', '.join(unknown)}"
    name, address, user = (str(raw.get(k) or "").strip() for k in ("name", "address", "user"))
    if not _NAME.match(name):
        return None, f"hôte {label} ignoré : nom invalide"
    if not _ADDRESS.match(address):
        return None, f"hôte {name} ignoré : adresse invalide"
    if not _USER.match(user):
        return None, f"hôte {name} ignoré : utilisateur invalide"
    try:
        port = int(raw.get("port", 22))
    except (TypeError, ValueError):
        port = 0
    if not 1 <= port <= 65535:
        return None, f"hôte {name} ignoré : port invalide"
    role = str(raw.get("role") or "").strip()
    if role not in ROLES:
        return None, f"hôte {name} ignoré : role obligatoire, prod | preprod | dev"
    key = str(raw.get("key") or "").strip()
    from nokker.secrets.base import SecretsError
    from nokker.secrets.refs import ref_name

    try:
        key_name = ref_name(key)
    except SecretsError:
        key_name = None
    if key_name is None:
        return None, f"hôte {name} ignoré : key doit être une référence au coffre, secret:NOM"
    jump = str(raw.get("jump") or "").strip() or None
    if jump is not None and (jump == name or not _NAME.match(jump)):
        return None, f"hôte {name} ignoré : jump invalide"
    return Host(name=name, address=address, port=port, user=user, key=key_name, role=role, jump=jump), None


def load_inventory(path: str | Path) -> Inventory:
    """Lit `hosts.yaml`. Fichier absent : inventaire vide (aucun hôte, aucun avis). Illisible : vide, avec un avis."""
    import yaml

    p = Path(path).expanduser()
    inv = Inventory(path=str(p))
    if not p.is_file():
        return inv
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        inv.notices.append(f"{p.name} illisible : {type(e).__name__}")
        return inv
    raw = data.get("hosts") if isinstance(data, dict) else None
    if isinstance(raw, dict):  # forme { nom: {…} }
        raw = [{"name": k, **(v if isinstance(v, dict) else {})} for k, v in raw.items()]
    if raw is None:
        return inv
    if not isinstance(raw, list):
        inv.notices.append(f"{p.name} : « hosts » doit être une liste")
        return inv
    for i, item in enumerate(raw):
        host, why = _entry(item, i)
        if why:
            inv.notices.append(why)
        elif host is not None and host.name in inv.hosts:
            inv.notices.append(f"hôte {host.name} ignoré : nom en double")
        elif host is not None:
            inv.hosts[host.name] = host
    # Sauts : vers un hôte connu, et un seul (un saut qui a lui-même un saut est refusé).
    for name, host in list(inv.hosts.items()):
        if host.jump is None:
            continue
        jump = inv.hosts.get(host.jump)
        if jump is None:
            inv.notices.append(f"hôte {name} ignoré : jump {host.jump} absent de l'inventaire")
            del inv.hosts[name]
        elif jump.jump is not None:
            inv.notices.append(f"hôte {name} ignoré : un seul saut (jump {host.jump} a lui-même un jump)")
            del inv.hosts[name]
    return inv


# ─────────────────────────────── known_hosts ───────────────────────────────


def fingerprint(key_b64: str) -> str:
    """Empreinte OpenSSH d'une clé publique (champ base64 d'une ligne known_hosts) : `SHA256:…` sans « = »."""
    digest = hashlib.sha256(base64.b64decode(key_b64)).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


def _glob(pattern: str) -> re.Pattern[str]:
    """Motif d'un nom de known_hosts : seuls `*` et `?` sont des jokers (les crochets de `[adresse]:port` sont
    littéraux, à la différence de fnmatch)."""
    return re.compile("".join(".*" if c == "*" else "." if c == "?" else re.escape(c) for c in pattern.lower()))


def _host_matches(field_: str, key: str) -> bool:
    if field_.startswith("|1|"):  # nom haché (HashKnownHosts) : HMAC-SHA1(sel, nom)
        try:
            _, _, salt, digest = field_.split("|", 3)
            mac = hmac.new(base64.b64decode(salt), key.encode(), hashlib.sha1).digest()
            return hmac.compare_digest(base64.b64encode(mac).decode(), digest)
        except (ValueError, TypeError):
            return False
    hit = False
    for pat in field_.split(","):
        neg = pat.startswith("!")
        pat = pat[1:] if neg else pat
        if _glob(pat).fullmatch(key.lower()):
            if neg:
                return False
            hit = True
    return hit


def known_entries(path: str | Path, key: str) -> list[tuple[str, str]]:
    """(type, base64) des clés de `key` (`adresse` ou `[adresse]:port`) dans known_hosts. Les lignes à marqueur
    (`@cert-authority`, `@revoked`) ne valent pas confiance ici : ignorées."""
    p = Path(path).expanduser()
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        parts = line.strip().split()
        if len(parts) < 3 or parts[0].startswith("#") or parts[0].startswith("@"):
            continue
        if _host_matches(parts[0], key) and _KEY_TYPE.match(parts[1]) and _B64.match(parts[2]):
            out.append((parts[1], parts[2]))
    return out


def known_lines(path: str | Path, keys: list[str]) -> str:
    """Lignes known_hosts (réécrites en clair, une par clé) des hôtes `keys` : ce que le bac à sable reçoit, rien de
    plus (pas tout le fichier de l'utilisateur)."""
    return "".join(f"{k} {t} {b}\n" for k in keys for t, b in known_entries(path, k))


def parse_scanned(text: str) -> list[tuple[str, str, str]]:
    """(nom, type, base64) des lignes known_hosts d'un texte (sortie d'un premier contact)."""
    out = []
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) == 3 and not parts[0].startswith("#") and _KEY_TYPE.match(parts[1]) and _B64.match(parts[2]):
            out.append((parts[0], parts[1], parts[2]))
    return out


def append_known(path: str | Path, key: str, key_type: str, key_b64: str) -> None:
    """Ajoute une ligne à known_hosts (0600, dossier 0700 s'il faut le créer). Jamais de remplacement."""
    p = Path(path).expanduser()
    if not (_KEY_TYPE.match(key_type) and _B64.match(key_b64)):
        raise ValueError("clé d'hôte illisible")
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    sep = ""
    if p.is_file() and p.stat().st_size > 0:
        with open(p, "rb") as r:
            r.seek(-1, os.SEEK_END)
            sep = "" if r.read(1) == b"\n" else "\n"
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(f"{sep}{key} {key_type} {key_b64}\n")


__all__ = [
    "ROLES",
    "Host",
    "Inventory",
    "append_known",
    "fingerprint",
    "known_entries",
    "known_lines",
    "load_inventory",
    "parse_scanned",
]
