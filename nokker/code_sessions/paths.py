"""Racine d'une session : `<workspace_root>/<tenant>/<user>/<session>`, créée composant par composant.

Chaque composant est un identifiant contrôlé (jamais `..`, jamais `/`) ; chaque dossier est vérifié par `lstat` après sa
création : un lien symbolique (posé par un bac à sable qui aurait vu un dossier parent, ou par une erreur de montage)
est refusé, jamais suivi ; le chemin réel final doit être exactement le chemin attendu."""
from __future__ import annotations

import os
import re
import shutil
import stat

TENANT_RE = re.compile(r"^tn_[A-Za-z0-9_-]{1,64}$")
USER_RE = re.compile(r"^us_[A-Za-z0-9]{1,64}$")
SESSION_RE = re.compile(r"^cs_[0-9a-f]{16}$")


class PathRefused(PermissionError):
    """Composant invalide, lien symbolique ou chemin réel inattendu : la racine n'est pas ouverte."""


def check_ids(tenant: str, user: str, session: str) -> None:
    for rx, v, what in ((TENANT_RE, tenant, "locataire"), (USER_RE, user, "utilisateur"),
                        (SESSION_RE, session, "session")):
        if not isinstance(v, str) or not rx.fullmatch(v):
            raise PathRefused(f"identifiant de {what} invalide")


def relative_root(tenant: str, user: str, session: str) -> str:
    check_ids(tenant, user, session)
    return f"{tenant}/{user}/{session}"


def session_root(workspace_root: str, tenant: str, user: str, session: str, *, create: bool = True) -> str:
    """Chemin absolu (réel) de la racine de la session, créée au besoin (0770). `PathRefused` sinon."""
    check_ids(tenant, user, session)
    base = os.path.realpath(workspace_root)
    cur = base
    for part in (tenant, user, session):
        cur = os.path.join(cur, part)
        if create:
            try:
                os.mkdir(cur, 0o770)
            except FileExistsError:
                pass
        try:
            st = os.lstat(cur)
        except FileNotFoundError:
            raise PathRefused("racine de session absente") from None
        if stat.S_ISLNK(st.st_mode):
            raise PathRefused("lien symbolique dans le chemin de la session : refusé")
        if not stat.S_ISDIR(st.st_mode):
            raise PathRefused("le chemin de la session n'est pas un dossier")
    expected = os.path.join(base, tenant, user, session)
    if os.path.realpath(expected) != expected:
        raise PathRefused("chemin réel inattendu pour la session")
    return expected


def purge_root(workspace_root: str, tenant: str, user: str, session: str) -> bool:
    """Efface la racine de la session (sans suivre aucun lien : `rmtree` protégé) ; False si elle n'existait pas."""
    try:
        root = session_root(workspace_root, tenant, user, session, create=False)
    except PathRefused:
        return False
    shutil.rmtree(root)
    return True


__all__ = ["PathRefused", "SESSION_RE", "TENANT_RE", "USER_RE", "check_ids", "purge_root", "relative_root",
           "session_root"]
