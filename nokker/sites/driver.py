"""Sites connectés — pilote du navigateur et masquage (docs/CONNECTED-SITES.md §5.5, §7.2).

Les recettes ne voient la page que par son arbre d'accessibilité (`Node` : rôle, nom, valeur, ref), jamais par des
classes CSS. Le pilote réel (`driver_mcp.py`) parle au Chrome de `sites-chrome` (moteur humain du pod navigateur) ; le site
simulé (`fakesocial.py`) implémente le même contrat en mémoire.

`MaskedDriver` est la seule porte de sortie d'un pilote vers `sitesd` : tout arbre, tout texte et toute capture y
passent, et
  - un champ `type=password`, ou rempli par `fill_secret` / `fill_code` (même repassé en texte par un bouton
    « Afficher »), a sa valeur remplacée par `••••` ;
  - toute valeur secrète connue (mot de passe tapé, code tapé) est remplacée par `••••` partout ailleurs ;
  - une capture pendant une connexion est refusée (`in_login`).
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from typing import Protocol

MASK = "••••"


@dataclass
class Node:
    ref: str
    role: str
    name: str = ""
    value: str = ""
    attrs: dict[str, str] = field(default_factory=dict)

    def line(self) -> str:
        val = f" value={self.value!r}" if self.value else ""
        return f'- {self.role} "{self.name}"{val} [ref={self.ref}]'


class Driver(Protocol):
    def goto(self, url: str) -> None: ...

    def url(self) -> str: ...

    def nodes(self) -> list[Node]: ...

    def text(self) -> str: ...

    def click(self, ref: str) -> None: ...

    def type(self, ref: str, text: str) -> None: ...

    def secret_fields(self) -> set[str]: ...  # refs des champs remplis par fill_secret/fill_code sur cette page

    def mark_secret(self, ref: str) -> None: ...

    def screenshot(self) -> bytes: ...

    def refresh(self) -> None: ...  # recharger la page (validation faite ailleurs)

    def export_profile(self) -> bytes: ...  # archive du profil (cookies, stockage), sans caches

    def close(self) -> None: ...

    # Gestes de formulaire (pack Emploi, dépôt par Lokkal) : facultatifs pour un site qui ne remplit pas de formulaire.
    def select(self, ref: str, value: str) -> None: ...  # option d'une liste (par son libellé)

    def check(self, ref: str, on: bool) -> None: ...  # case à cocher

    def upload(self, ref: str, filename: str, data: bytes, mime: str = "application/pdf") -> None: ...


def find(nodes: Iterable[Node], role: str, name: str | re.Pattern[str]) -> Node | None:
    rx = name if isinstance(name, re.Pattern) else re.compile(re.escape(name), re.IGNORECASE)
    for n in nodes:
        if n.role == role and rx.search(n.name or ""):
            return n
    return None


def find_all(nodes: Iterable[Node], role: str, name: str | re.Pattern[str] = "") -> list[Node]:
    rx = name if isinstance(name, re.Pattern) else re.compile(re.escape(name), re.IGNORECASE)
    return [n for n in nodes if n.role == role and rx.search(n.name or "")]


class MaskedDriver:
    """Enveloppe obligatoire d'un pilote (voir l'en-tête du module)."""

    def __init__(self, inner: Driver) -> None:
        self.inner = inner
        self._secrets: list[str] = []
        self.in_login = False

    def _scrub(self, text: str) -> str:
        for v in sorted(self._secrets, key=len, reverse=True):
            if v:
                text = text.replace(v, MASK)
        return text

    # ── lecture (masquée) ──
    def nodes(self) -> list[Node]:
        secret_refs = self.inner.secret_fields()
        out = []
        for n in self.inner.nodes():
            masked = n.attrs.get("type") == "password" or n.ref in secret_refs or n.attrs.get("secret") == "1"
            value = MASK if (masked and n.value) else self._scrub(n.value)
            out.append(replace(n, name=self._scrub(n.name), value=value))
        return out

    def text(self) -> str:
        return self._scrub(self.inner.text())

    def snapshot(self) -> str:
        return "\n".join(n.line() for n in self.nodes())

    def screenshot(self) -> bytes:
        if self.in_login:
            raise PermissionError("aucune capture pendant une connexion")
        raw = self.inner.screenshot()
        for v in self._secrets:  # pilote simulé : la « capture » est du texte rendu ; réel : champs couverts (CSS)
            raw = raw.replace(v.encode("utf-8"), MASK.encode("utf-8"))
        return raw

    def url(self) -> str:
        return self._scrub(self.inner.url())

    # ── gestes ──
    def goto(self, url: str) -> None:
        self.inner.goto(url)

    def click(self, ref: str) -> None:
        self.inner.click(ref)

    def type(self, ref: str, text: str) -> None:
        self.inner.type(ref, text)

    def fill_secret(self, ref: str, value: str) -> None:
        """Tape une valeur secrète (mot de passe, code, lien magique) : retenue pour le masquage, le champ marqué."""
        self._secrets.append(value)
        self.inner.mark_secret(ref)
        self.inner.type(ref, value)

    def goto_secret(self, url: str) -> None:
        """Ouvre un lien secret (lien magique) : l'URL est retenue pour le masquage (barre d'adresse, textes)."""
        self._secrets.append(url)
        self.inner.goto(url)

    def refresh(self) -> None:
        self.inner.refresh()

    def select(self, ref: str, value: str) -> None:
        self.inner.select(ref, value)

    def check(self, ref: str, on: bool) -> None:
        self.inner.check(ref, on)

    def upload(self, ref: str, filename: str, data: bytes, mime: str = "application/pdf") -> None:
        self.inner.upload(ref, filename, data, mime)

    def export_profile(self) -> bytes:
        return self.inner.export_profile()

    def close(self) -> None:
        self.inner.close()


__all__ = ["MASK", "Driver", "MaskedDriver", "Node", "find", "find_all"]
