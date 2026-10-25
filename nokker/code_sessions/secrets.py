"""Identifiants de forge des sessions de code : coffre du noyau, portée PAR LOCATAIRE imposée par le noyau.

`code.secrets` a la forme de `local.secrets` et de `sso_settings.secrets` (`provider: file | openbao`, blocs `file` et
`openbao` ; `NOKKER_SECRETS_PROVIDER` prime). Les fournisseurs sont ceux de `nokker/secrets/`, une seule session OpenBao
pour tous les locataires :
  - `openbao` : `<mount>/data/<prefix>/tenants/<tenant_id>/forge/<NOM>` (KV v2) — la politique de l'AppRole n'a besoin
    que de `<mount>/data/<prefix>/tenants/*` et `<mount>/metadata/<prefix>/tenants/*` ;
  - `file` : un fichier chiffré par locataire, `<file.path>/tenants/<tenant_id>/vault.bin` (`file.path` = un DOSSIER,
    défaut `<code.state_dir>/secrets`), même clé (`key_env` ou
    `key_file`).
Un locataire ne peut ni lire ni lister le nom d'un autre : le chemin vient du locataire courant, jamais d'une requête.
La base ne garde que le NOM (`nokker_code_sessions.credential`) ; la valeur n'est lue qu'au clone, jamais rendue."""
from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from nokker.code_sessions.paths import TENANT_RE
from nokker.secrets.base import SecretsError

SCOPE = "forge"


class TenantSecrets:
    """`for_tenant(tn)` → un `SecretsProvider` rangé sous le locataire (en cache)."""

    def __init__(self, build: Callable[[str], Any]) -> None:
        self._build = build
        self._cache: dict[str, Any] = {}
        self._lock = threading.Lock()

    def __repr__(self) -> str:  # jamais une valeur
        return f"TenantSecrets({len(self._cache)} locataires)"

    def for_tenant(self, tenant: str) -> Any:
        if not isinstance(tenant, str) or not TENANT_RE.fullmatch(tenant):
            raise SecretsError("locataire invalide pour le coffre des sessions de code", "bad_name")
        with self._lock:
            p = self._cache.get(tenant)
            if p is None:
                p = self._cache[tenant] = self._build(tenant)
            return p

    @classmethod
    def from_config(cls, cfg: dict[str, Any], state_dir: str | Path) -> TenantSecrets:
        from nokker.secrets.vault import chosen_provider

        kind = chosen_provider(cfg)
        if kind == "file":
            from nokker.secrets.file import DEFAULT_KEY_ENV, FileSecrets

            f = dict(cfg.get("file") or {})
            base = Path(f.get("path") or state_dir).expanduser()
            key_file = Path(f.get("key_file") or base / "vault.key").expanduser()
            return cls(lambda tn: FileSecrets(path=base / "tenants" / tn / "vault.bin", scope="user",
                                              key_env=f.get("key_env", DEFAULT_KEY_ENV), key_file=key_file,
                                              create_key=bool(f.get("create_key", True))))
        from nokker.secrets import openbao

        o = dict(cfg.get("openbao") or {})
        session = openbao.session_from_config(o)  # UNE session (jeton AppRole) pour tous les locataires
        mount, prefix = o.get("mount") or "nokker", str(o.get("prefix") or "").strip("/")
        return cls(lambda tn: openbao.OpenBaoSecrets(session=session, scope=SCOPE, mount=mount,
                                                     prefix="/".join(p for p in (prefix, "tenants", tn) if p)))


__all__ = ["SCOPE", "TenantSecrets"]
