"""Valeurs des champs sensibles retirées de tout texte du navigateur qui remonte au modèle (docs/BROWSER-LIVE.md §4).

Défense en profondeur, hors passation comme pendant : un instantané d'accessibilité (ou un texte lu) peut porter la
valeur d'un champ pendant qu'une personne y tape un mot de passe ou un code. Sur chaque ligne qui désigne un champ
sensible, la valeur (`…]: valeur`, `"Nom": valeur`, `value="…"`) devient « [masqué] ».

Champs sensibles (mêmes que `sites/driver_mcp.py::BLUR_SCRIPT` et sites-chrome) : mot de passe (`[password]`,
`type=password`, libellé), `autocomplete=cc-*`, `one-time-code`, `data-nk-secret`, champ de carte (libellé), champ de
code 2FA reconnu par son libellé, et les références que le noyau sait sensibles (saisie du coffre, `mark_secret`).
"""
from __future__ import annotations

import re
from collections.abc import Iterable

MASKED = "[masqué]"
_REF = re.compile(r"\[ref=(e\d+)\]")
_FLAGS = re.compile(r"\[password\]|type\s*[=:]\s*[\"']?password|data-nk-secret|"
                    r"autocomplete\s*[=:]\s*[\"']?(?:cc-[a-z-]+|one-time-code|current-password|new-password)",
                    re.IGNORECASE)
_AFTER_BRACKET = re.compile(r"(\]\s*:\s*)(\S.*)$")
_AFTER_NAME = re.compile(r'^(\s*-\s+[a-z]+\s+"(?:[^"\\]|\\.)*"\s*:\s*)(\S.*)$')
_ATTR_VALUE = re.compile(r"""(\bvalue\s*[=:]\s*)(["'])(.*?)\2""")


_ROLE = re.compile(r"^\s*-\s+([a-z]+)\b")
INPUT_ROLES = frozenset({"textbox", "searchbox", "combobox", "spinbutton", "input"})


def sensitive_line(head: str, refs: Iterable[str] = ()) -> bool:
    """Ligne d'un champ sensible. Le libellé seul ne compte que pour un champ de saisie (un titre « Code de
    vérification » ou un lien « Mot de passe oublié ? » gardent leur texte)."""
    if _FLAGS.search(head):
        return True
    m = _REF.search(head)
    if m and m.group(1) in set(refs):
        return True
    role = _ROLE.match(head)
    if role is None or role.group(1) not in INPUT_ROLES:
        return False
    from nokker.browserlive.live import sensitive_label
    from nokker.sites.driver_mcp import OTP_LABEL_RE

    return sensitive_label(head) or re.search(OTP_LABEL_RE, head, re.IGNORECASE) is not None


def mask_values(text: str, refs: Iterable[str] = ()) -> str:
    """`text` avec la valeur de chaque champ sensible remplacée par « [masqué] » (idempotent)."""
    if not text:
        return text
    refs = set(refs)
    out = []
    for line in text.split("\n"):
        m = _AFTER_BRACKET.search(line) or _AFTER_NAME.search(line)
        head = line[: m.start(2)] if m else line
        if sensitive_line(head, refs):
            if m and m.group(2).strip() != MASKED:
                line = line[: m.start(2)] + MASKED
            line = _ATTR_VALUE.sub(lambda a: f"{a.group(1)}{a.group(2)}{MASKED}{a.group(2)}", line)
        out.append(line)
    return "\n".join(out)


__all__ = ["MASKED", "mask_values", "sensitive_line"]
