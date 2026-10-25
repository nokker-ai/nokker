"""Effacement d'un locataire hors base pour les sessions de code (`nokker erase`, nokker/admin/erase.py) : ses espaces de
travail (`<workspace_root>/<tenant>`), ses points de retour et confiances de hooks (`<state_dir>/<tenant>`) et ses
identifiants de forge (coffre `code.secrets`, portée du locataire). Les lignes `nokker_code_sessions` partent avec la
base (colonne `tenant_id`).

Lit le bloc `code` brut, sans le valider comme au démarrage (le module kubernetes n'est pas requis, le drapeau peut
être éteint : des données restent peut-être d'avant). À blanc : compte seulement. Un lien symbolique à la place du
dossier du locataire est retiré, jamais suivi."""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from nokker.code_sessions.paths import SESSION_RE, TENANT_RE, USER_RE

KEYS = ("code: espaces de travail", "code: points de retour", "code: identifiants de forge")


def _sessions_under(base: Path, sub: tuple[str, ...] = ()) -> int:
    """Dossiers de session sous `<base>/<user>/<sub…>/<cs_…>` (sans suivre de lien)."""
    n = 0
    if base.is_symlink() or not base.is_dir():
        return 0
    for user in base.iterdir():
        if not USER_RE.fullmatch(user.name) or user.is_symlink() or not user.is_dir():
            continue
        d = user.joinpath(*sub)
        if d.is_symlink() or not d.is_dir():
            continue
        n += sum(1 for s in d.iterdir() if SESSION_RE.fullmatch(s.name) and not s.is_symlink() and s.is_dir())
    return n


def _remove(path: Path) -> None:
    if path.is_symlink():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def erase_code_tenant(raw: dict[str, Any] | None, tenant: str, *, apply: bool) -> dict[str, int]:
    """{libellé: nombre} ; {} sans bloc `code` (ou sans `workspace_root`)."""
    if not raw or not isinstance(raw, dict) or not raw.get("workspace_root"):
        return {}
    if not isinstance(tenant, str) or not TENANT_RE.fullmatch(tenant):
        raise ValueError(f"locataire invalide : {tenant!r}")
    root = Path(os.path.realpath(str(raw["workspace_root"])))
    state = Path(os.path.realpath(str(raw.get("state_dir") or root / ".nokker-state")))
    ws, ck = root / tenant, state / tenant
    counts = {KEYS[0]: _sessions_under(ws), KEYS[1]: _sessions_under(ck, ("checkpoints",)), KEYS[2]: 0}
    vault = None
    names: list[str] = []
    if raw.get("secrets") is not None:
        from nokker.code_sessions.secrets import TenantSecrets

        vault = TenantSecrets.from_config(dict(raw["secrets"]), state / "secrets").for_tenant(tenant)
        names = [i.name for i in vault.list()]
        counts[KEYS[2]] = len(names)
    if apply:
        for name in names:
            vault.delete(name)
        if vault is not None and getattr(vault, "kind", "") == "file":  # coffre fichier du locataire : le fichier aussi
            Path(vault.path).unlink(missing_ok=True)
        _remove(ws)
        _remove(ck)
    return counts


__all__ = ["KEYS", "erase_code_tenant"]
