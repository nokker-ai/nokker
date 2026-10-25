"""Le coffre d'une installation Lokkal : choix du fournisseur, deux portées, caviardage de toute valeur connue.

- **Choix explicite** : `NOKKER_SECRETS_PROVIDER` (posé par le compose) prime sur `local.secrets.provider` ; défaut
  `file`. Une adresse OpenBao présente sans ce choix ne bascule RIEN : elle donne un avis (`notice`, journalisé au
  démarrage, rendu par `GET /v1/local/secrets` et `nokker secret list`).
- **Portées** : `user` (les secrets de l'utilisateur, `nokker secret …`, références `secret:NOM`) et `system` (clés du
  noyau : clé du Router, jeton de machine, clés BYOK — nokker/local/setup.py). Les deux suivent le fournisseur choisi.
- **Caviardage** (`VaultKnownSecrets`) : toute valeur des deux portées entre dans le coffre de valeurs connues des
  invariants (`qa/secrets.py::KnownSecrets`), donc dans `redact_result` (chaque résultat d'outil) et `scrub_output`
  (chaque réponse) ; relu toutes les `ttl_s` secondes (une écriture par la commande, dans un autre processus, est
  vue au plus tard là) et tout de suite après une écriture par l'API. Une valeur remplacée ou effacée reste
  caviardée jusqu'au redémarrage (elle a pu entrer dans un fil avant). Valeurs de moins de 4 caractères : non
  caviardées (elles remplaceraient des mots ordinaires partout).
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nokker.qa.secrets import KnownSecrets
from nokker.secrets.base import SecretInfo, SecretsError, SecretsProvider
from nokker.secrets.file import DEFAULT_KEY_ENV, FileSecrets
from nokker.secrets.openbao import ENV_ADDR, OpenBaoSecrets, OpenBaoSession, session_from_config
from nokker.secrets.refs import SecretResolver

logger = logging.getLogger("nokker.secrets")

ENV_PROVIDER = "NOKKER_SECRETS_PROVIDER"
PROVIDERS = ("file", "openbao")
SYSTEM_HANDLE_PREFIX = "nokker:"  # poignée d'une clé du noyau : [secret:nokker:router]
MIN_REDACT_LEN = 4


def chosen_provider(cfg: dict[str, Any] | None) -> str:
    kind = (os.environ.get(ENV_PROVIDER) or (cfg or {}).get("provider") or "file").strip().lower()
    if kind not in PROVIDERS:
        raise SecretsError(f"fournisseur de secrets inconnu : {kind!r} (file | openbao)", "config")
    return kind


def build_provider(kind: str, scope: str, cfg: dict[str, Any], state_dir: Path,
                   session: OpenBaoSession | None = None) -> SecretsProvider:
    """Un fournisseur pour une portée. `file` : `<state_dir>/vault.bin` (les deux portées dans le même fichier)."""
    if kind == "file":
        f = cfg.get("file") or {}
        return FileSecrets(path=Path(f.get("path") or state_dir / "vault.bin"), scope=scope,
                           key_env=f.get("key_env", DEFAULT_KEY_ENV),
                           key_file=Path(f.get("key_file") or state_dir / "vault.key"),
                           create_key=bool(f.get("create_key", True)))
    o = cfg.get("openbao") or {}
    return OpenBaoSecrets(session=session or session_from_config(o), scope=scope,
                          mount=o.get("mount") or "nokker", prefix=o.get("prefix") or "")


def _openbao_prepared(o: dict[str, Any]) -> bool:
    from nokker.secrets.openbao import ENV_ROLE_ID_FILE, ENV_TOKEN_FILE

    if not (os.environ.get(ENV_ADDR) or o.get("address")):
        return False
    files = [os.environ.get(ENV_ROLE_ID_FILE) or o.get("role_id_file"), os.environ.get(ENV_TOKEN_FILE) or o.get("token_file")]
    return any(f and Path(f).expanduser().is_file() for f in files)


class VaultKnownSecrets(KnownSecrets):
    """`KnownSecrets` nourri par le coffre. Garde chaque valeur vue (valeur → nom) jusqu'au redémarrage."""

    def __init__(self, providers: list[SecretsProvider], ttl_s: float = 30.0) -> None:
        super().__init__()
        self._by_value: dict[str, str] = {}
        self._providers = providers
        self._ttl = ttl_s
        self._next = 0.0
        self._lock = threading.Lock()
        self._warned: set[str] = set()

    def __repr__(self) -> str:  # jamais les valeurs
        return f"VaultKnownSecrets({len(self._by_value)} valeurs)"

    def add(self, name: str, value: str) -> None:
        if value and len(value) >= MIN_REDACT_LEN:
            self._by_value[value] = name

    def merge(self, other: KnownSecrets) -> VaultKnownSecrets:
        for name, value in other.items_by_length_desc():
            self.add(name, value)
        return self

    def note(self, scope: str, name: str, value: str) -> None:
        self.add(SYSTEM_HANDLE_PREFIX + name if scope == "system" else name, value)

    def refresh(self, force: bool = False) -> None:
        if not force and time.monotonic() < self._next:
            return
        with self._lock:
            if not force and time.monotonic() < self._next:
                return
            self._next = time.monotonic() + self._ttl
            for p in self._providers:
                try:
                    pairs = p.values() if hasattr(p, "values") else \
                        [(i.name, v) for i in p.list() if (v := p.get(i.name)) is not None]
                except SecretsError as e:
                    if e.code not in self._warned:  # coffre scellé, clé absente : un avis, puis silence
                        self._warned.add(e.code)
                        logger.warning("caviardage du coffre (%s) : %s", p.scope, e)
                    continue
                for name, value in pairs:
                    self.note(p.scope, name, value)

    def names(self) -> list[str]:
        self.refresh()
        return sorted(set(self._by_value.values()))

    def items_by_length_desc(self) -> list[tuple[str, str]]:
        self.refresh()
        return sorted(((n, v) for v, n in list(self._by_value.items())), key=lambda kv: len(kv[1]), reverse=True)


@dataclass
class Vault:
    provider: str
    user: SecretsProvider
    system: SecretsProvider
    session: OpenBaoSession | None = None
    notice: str | None = None
    known: VaultKnownSecrets = field(init=False)

    def __post_init__(self) -> None:
        self.known = VaultKnownSecrets([self.user, self.system])

    @classmethod
    def from_config(cls, cfg: dict[str, Any] | None, state_dir: Path) -> Vault:
        cfg = dict(cfg or {})
        kind = chosen_provider(cfg)
        state_dir = Path(state_dir).expanduser()
        session = session_from_config(cfg.get("openbao") or {}) if kind == "openbao" else None
        notice = None
        # Avis seulement si un OpenBao est vraiment préparé (adresse ET fichier role_id présent) : le compose pose
        # l'adresse et monte le dossier des identifiants même sans le profil `vault` (dossier vide).
        if kind == "file" and _openbao_prepared(cfg.get("openbao") or {}):
            notice = (f"adresse OpenBao présente mais fournisseur « file » : rien n'a basculé "
                      f"({ENV_PROVIDER}=openbao ou local.secrets.provider: openbao pour le choisir)")
        return cls(provider=kind, user=build_provider(kind, "user", cfg, state_dir, session),
                   system=build_provider(kind, "system", cfg, state_dir, session), session=session, notice=notice)

    def startup_report(self) -> None:
        if self.notice:
            logger.warning("[secrets] %s", self.notice)
        if self.session is not None:
            h = self.session.health()
            if not h["reachable"]:
                logger.warning("[secrets] OpenBao injoignable (%s) : routes de secrets en 503", self.session.address)
            elif h["sealed"]:
                logger.warning("[secrets] coffre scellé : routes de secrets en 503 jusqu'au descellement")
        logger.info("[secrets] fournisseur %s", self.provider)

    def resolver(self) -> SecretResolver:
        return SecretResolver(self.user)

    def status(self) -> dict[str, Any]:
        out: dict[str, Any] = {"provider": self.provider}
        if self.notice:
            out["notice"] = self.notice
        if self.session is not None:
            out["sealed"] = self.session.health()["sealed"]
        return out

    # Écritures de la portée `user` par l'API ou la commande : la valeur entre au caviardage tout de suite.
    def set(self, name: str, value: str) -> SecretInfo:
        info = self.user.set(name, value)
        self.known.note("user", name, value)
        return info

    def delete(self, name: str) -> bool:
        return self.user.delete(name)

    def list(self) -> list[SecretInfo]:
        return self.user.list()


__all__ = ["ENV_PROVIDER", "PROVIDERS", "Vault", "VaultKnownSecrets", "build_provider", "chosen_provider"]
