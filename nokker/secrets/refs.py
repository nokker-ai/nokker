"""Références `secret:NOM` : une config porte le NOM, la valeur est lue au coffre au moment de l'usage.

Où une référence est permise (docs/en/secrets.md) : en-têtes et jeton des serveurs MCP (`mcp_tool_servers`),
identifiant d'un connecteur, environnement d'un processus lancé hors du modèle (MCP stdio, ssh, agents à venir) —
partout où le NOYAU, pas le modèle, a besoin de la valeur. La résolution se fait juste avant l'usage (connexion,
lancement), jamais à la lecture de la config, et la valeur ne repasse jamais par un message du modèle : un outil qui
la renverrait quand même est caviardé (nokker/secrets/vault.py → `qa/secrets.py`).

Une chaîne qui ne commence pas par `secret:` est rendue telle quelle ; `secret:` suivi d'un nom invalide est une
erreur de config (`bad_name`) ; un nom absent du coffre est `missing` (message sans valeur, avec le nom).
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from nokker.secrets.base import SecretsError, SecretsProvider, check_name

REF_PREFIX = "secret:"


def ref_name(value: object) -> str | None:
    """Le NOM d'une référence `secret:NOM`, ou None si `value` n'en est pas une."""
    if not isinstance(value, str) or not value.startswith(REF_PREFIX):
        return None
    return check_name(value[len(REF_PREFIX):])


def refs_in(obj: object) -> list[str]:
    """Noms référencés dans une structure (dict, liste, chaîne), dans l'ordre, sans doublon."""
    out: list[str] = []

    def walk(o: object) -> None:
        if isinstance(o, Mapping):
            for v in o.values():
                walk(v)
        elif isinstance(o, (list, tuple)):
            for v in o:
                walk(v)
        elif (n := ref_name(o)) is not None and n not in out:
            out.append(n)

    walk(obj)
    return out


class SecretResolver:
    """Résout les références contre la portée `user` du coffre. `provider=None` : toute référence échoue
    (`unavailable`), une chaîne ordinaire passe."""

    def __init__(self, provider: SecretsProvider | None) -> None:
        self.provider = provider

    def __repr__(self) -> str:
        return f"SecretResolver({self.provider!r})"

    def value(self, name: str) -> str:
        check_name(name)
        if self.provider is None:
            raise SecretsError(f"référence secret:{name} : pas de coffre sur cette instance")
        try:
            got = self.provider.get(name)
        except SecretsError as e:
            raise SecretsError(f"référence secret:{name} : {e}", e.code) from None
        if got is None:
            raise SecretsError(f"référence secret:{name} : nom absent du coffre (nokker secret set {name})",
                               "missing")
        return got

    def resolve(self, obj: Any) -> Any:
        """Copie de `obj` où chaque `secret:NOM` est remplacé par sa valeur (dict, liste, tuple, chaîne)."""
        if isinstance(obj, Mapping):
            return {k: self.resolve(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.resolve(v) for v in obj]
        if isinstance(obj, tuple):
            return tuple(self.resolve(v) for v in obj)
        name = ref_name(obj)
        return self.value(name) if name is not None else obj

    def env(self, mapping: Mapping[str, str] | None) -> dict[str, str]:
        """Environnement d'un sous-processus (MCP stdio, ssh, agent) : `{VAR: "secret:NOM" | valeur}` résolu. À
        passer en `env=` du processus, JAMAIS à fusionner dans `os.environ` du noyau."""
        return {str(k): str(self.resolve(v)) for k, v in (mapping or {}).items()}


__all__ = ["REF_PREFIX", "SecretResolver", "ref_name", "refs_in"]
