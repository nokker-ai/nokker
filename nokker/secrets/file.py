"""Fournisseur `file` (défaut) : le coffre local `<state_dir>/vault.bin`, repris tel quel.

- **Fichier** : celui du premier lancement (nokker/local/setup.py, docs/LOKKAL.md §7.1), même format : un objet JSON
  `{clé: valeur}` (chaînes), chiffré d'un bloc en Fernet (`SecretBox`, la crypto des identifiants des connecteurs,
  nokker/connectors/crypto.py), fichier 0600. Pas de second format, pas de second fichier : les deux portées y
  vivent ensemble.
    - portée `system` (clés du noyau) : la clé est le nom tel quel (`router`, `byok:<nom>`, `machine_token`) ;
    - portée `user` (`nokker secret set`) : la clé est `user:<NOM>` ;
    - dates de mise à jour : sous la clé réservée `meta:updated_at`, une chaîne JSON `{clé: "…Z"}` (aucune valeur).
  Les préfixes `user:` et `meta:` sont donc interdits aux noms du noyau. Un coffre écrit avant X4 se relit sans
  conversion (ses entrées sont celles de la portée `system`, sans date).
- **Clé** : `key_env` (`NOKKER_LOCAL_SECRET_KEY`, posée par l'installeur dans le `.env` du compose) si elle est
  présente ; sinon le fichier `key_file` (0600). Ce fichier n'est créé que si le coffre n'existe pas encore : on ne
  génère jamais une clé neuve à côté d'un coffre écrit avec une autre. Sans clé : coffre absent = vide, écriture
  refusée (`SecretsError`), jamais rien en clair.
- **Écritures** : verrou `fcntl` sur `<fichier>.lock` (la commande `python -m nokker secret` et le noyau peuvent
  écrire en même temps), fichier temporaire 0600 puis `os.replace` (jamais un coffre à moitié écrit).

Ce que ce fournisseur protège : une copie du fichier seul (sauvegarde, partage par erreur), un outil qui lirait le
dossier d'état (les bacs à sable ne le montent pas). Ce qu'il ne protège pas : quelqu'un qui a le compte de
l'utilisateur sur la machine (il lit la clé). Pour ça : `openbao`.
"""
from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from nokker.connectors.crypto import SecretBox
from nokker.connectors.errors import ConnectorError
from nokker.secrets.base import SecretInfo, SecretsError, check_name, check_value

DEFAULT_KEY_ENV = "NOKKER_LOCAL_SECRET_KEY"
USER_PREFIX = "user:"
META_KEY = "meta:updated_at"


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _iso(ts: datetime) -> str:
    return ts.isoformat().replace("+00:00", "Z")


def _parse_ts(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _write_0600(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


@dataclass
class FileSecrets:
    path: Path
    scope: str = "user"
    key_env: str | None = DEFAULT_KEY_ENV
    key_file: Path | None = None
    create_key: bool = True
    kind: str = field(default="file", init=False)

    def __post_init__(self) -> None:
        self.path = Path(self.path).expanduser()
        self.key_file = Path(self.key_file).expanduser() if self.key_file else None

    def __repr__(self) -> str:
        return f"FileSecrets({str(self.path)!r}, scope={self.scope!r})"

    # ─────────────────────────────── noms ───────────────────────────────

    def _check(self, name: str) -> str:
        check_name(name, self.scope)
        if self.scope == "system" and name.startswith((USER_PREFIX, "meta:")):
            raise SecretsError("préfixe réservé (user:, meta:)", "bad_name")
        return name

    def _key_of(self, name: str) -> str:
        return USER_PREFIX + name if self.scope == "user" else name

    def _mine(self, key: str) -> str | None:
        """Le nom de cette portée pour une clé du fichier, ou None si la clé n'est pas à elle."""
        if key.startswith("meta:"):
            return None
        if self.scope == "user":
            return key[len(USER_PREFIX):] if key.startswith(USER_PREFIX) else None
        return None if key.startswith(USER_PREFIX) else key

    # ─────────────────────────────── clé ───────────────────────────────

    def _box(self, *, for_write: bool) -> SecretBox | None:
        key = ""
        if self.key_env:
            key = os.environ.get(self.key_env, "")
        if not key and self.key_file is not None and self.key_file.is_file():
            key = self.key_file.read_text(encoding="utf-8").strip()
        if not key:
            if not for_write:
                return None
            if self.key_file is None or not self.create_key or self.path.is_file():
                where = f"{self.key_env} ou {self.key_file}" if self.key_file else str(self.key_env)
                why = " (le coffre existe : sa clé manque)" if self.path.is_file() else ""
                raise SecretsError(f"{where} absente{why} : le coffre local ne range rien en clair")
            key = SecretBox.generate_key()
            _write_0600(self.key_file, (key + "\n").encode())
        try:
            return SecretBox(key)
        except ConnectorError:
            raise SecretsError("clé du coffre invalide (clé Fernet base64 de 32 octets attendue)", "config") from None

    # ─────────────────────────────── fichier ───────────────────────────────

    def _load(self, box: SecretBox | None = None) -> dict[str, str]:
        if not self.path.is_file():
            return {}
        box = box or self._box(for_write=False)
        if box is None:
            raise SecretsError(f"{self.key_env} absente : coffre local illisible")
        try:
            raw = json.loads(box.open(self.path.read_bytes()))
        except ConnectorError:
            raise SecretsError("coffre local illisible avec la clé actuelle") from None
        except ValueError:
            raise SecretsError("coffre local corrompu") from None
        if not isinstance(raw, dict):
            raise SecretsError("coffre local corrompu")
        return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)}

    @staticmethod
    def _dates(data: dict[str, str]) -> dict[str, str]:
        try:
            d = json.loads(data.get(META_KEY) or "{}")
        except ValueError:
            return {}
        return d if isinstance(d, dict) else {}

    def _save(self, box: SecretBox, data: dict[str, str]) -> None:
        _write_0600(self.path, box.seal(json.dumps(data, ensure_ascii=False)))

    @contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path.with_name(self.path.name + ".lock"), os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    # ─────────────────────────────── contrat ───────────────────────────────

    def get(self, name: str) -> str | None:
        if self._mine(self._key_of(name)) != name:
            return None
        return self._load().get(self._key_of(name))

    def set(self, name: str, value: str) -> SecretInfo:
        self._check(name)
        check_value(value)
        key, now = self._key_of(name), _now()
        with self._locked():
            box = self._box(for_write=True)
            data = self._load(box)
            dates = self._dates(data)
            data[key] = value
            dates[key] = _iso(now)
            data[META_KEY] = json.dumps(dates)
            self._save(box, data)
        return SecretInfo(name, now)

    def delete(self, name: str) -> bool:
        key = self._key_of(name)
        if self._mine(key) != name:
            return False
        with self._locked():
            if not self.path.is_file():
                return False
            box = self._box(for_write=True)
            data = self._load(box)
            if data.pop(key, None) is None:
                return False
            dates = self._dates(data)
            dates.pop(key, None)
            data[META_KEY] = json.dumps(dates)
            self._save(box, data)
        return True

    def list(self) -> list[SecretInfo]:
        data = self._load()
        dates = self._dates(data)
        out = [SecretInfo(n, _parse_ts(dates.get(k))) for k in data if (n := self._mine(k)) is not None]
        return sorted(out, key=lambda s: s.name)

    def values(self) -> list[tuple[str, str]]:
        """(nom, valeur) de la portée en UNE lecture : pour le caviardage seulement (nokker/secrets/vault.py)."""
        return [(n, v) for k, v in self._load().items() if (n := self._mine(k)) is not None]


__all__ = ["DEFAULT_KEY_ENV", "META_KEY", "USER_PREFIX", "FileSecrets"]
