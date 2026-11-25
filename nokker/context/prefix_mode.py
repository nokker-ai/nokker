"""Préfixe de base d'une session (`prefix` de `POST /v1/local/sessions`, docs/HOST-EMBED.md §13).

Absent (défaut) : le préfixe du noyau tel qu'il est, à l'octet près. `"host"` : l'hôte porte sa propre persona et sa propre
méthode (`system_prompt`) ; Nokker ne garde devant lui que ses invariants de sûreté (`prompts/prefix-invariants.md`), sans
persona ni méthode de code. Les portes (cartes, refus, filets de secrets) sont dans le code et ne dépendent pas de ce choix.

Gardé EN MÉMOIRE par fil (sous-agents `<fil>/sa-…` compris), comme `system_prompt` : l'hôte le redonne à la reprise, une
ouverture sans le champ l'efface, `DELETE /v1/local/sessions/{fil}` aussi."""
from __future__ import annotations

import re
import threading

MODES = ("host",)
CODE = "bad_prefix"
ERROR = "prefix : absent ou \"host\" attendu"


def validate(raw: object) -> tuple[str, str | None]:
    """(mode, erreur) : `null` = défaut (""), "host" ; toute autre valeur, ou un non-chaîne : erreur (sans reprendre la valeur)."""
    if raw is None:
        return "", None
    if isinstance(raw, str) and raw in MODES:
        return raw, None
    return "", ERROR


class PrefixModes:
    def __init__(self) -> None:
        self._by_thread: dict[str, str] = {}
        self._lock = threading.Lock()

    def set(self, thread_id: str, mode: str) -> None:
        with self._lock:
            if mode:
                self._by_thread[thread_id] = mode
                while len(self._by_thread) > 5000:  # borne mémoire : on oublie les plus anciens fils
                    self._by_thread.pop(next(iter(self._by_thread)), None)
            else:
                self._by_thread.pop(thread_id, None)

    def clear(self, thread_id: str) -> None:
        self.set(thread_id, "")

    def get(self, session_key: str | None) -> str:
        if not self._by_thread:
            return ""
        key = session_key or ""
        with self._lock:
            while key not in self._by_thread and "/sa-" in key:
                key = key.rsplit("/sa-", 1)[0]
            return self._by_thread.get(key, "")


_REGISTRY = PrefixModes()


def registry() -> PrefixModes:
    return _REGISTRY


# ───────────── variante de préfixe par session (`prefix_variant`, docs/HOST-EMBED.md §22) ─────────────
# Liste BLANCHE déclarée côté config (`local.prefix_variants: {nom: chemin}`) ; la session ne donne jamais un chemin, seulement un nom.
VARIANT_CODE = "unknown_prefix_variant"
VARIANT_CONFLICT_CODE = "prefix_variant_conflict"
VARIANT_TYPE_CODE = "bad_prefix_variant"
_VARIANT_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

_VARIANTS = PrefixModes()  # {fil: nom de variante}, mémoire seulement, sous-agents compris (comme `prefix`)


def variant_registry() -> PrefixModes:
    return _VARIANTS


def declared_variants(raw: object) -> dict[str, str]:
    """`local.prefix_variants` lu : {nom: chemin}. Absent ou vide : aucune variante. Nom hors `[a-z0-9][a-z0-9_-]{0,31}` ou chemin qui
    n'est pas une chaîne non vide : erreur de configuration (arrêt au démarrage, jamais une variante ignorée en silence)."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("local.prefix_variants : {nom: chemin} attendu")
    out: dict[str, str] = {}
    for name, path in raw.items():
        if not isinstance(name, str) or not _VARIANT_NAME.match(name):
            raise ValueError(f"local.prefix_variants : nom de variante invalide ({str(name)[:40]!r}) ; attendu [a-z0-9][a-z0-9_-]{{0,31}}")
        if not isinstance(path, str) or not path.strip():
            raise ValueError(f"local.prefix_variants.{name} : chemin attendu")
        out[name] = path
    return out


def validate_variant(raw: object, declared: tuple[str, ...] | list[str], prefix_mode: str = "") -> tuple[str, tuple[int, str, str, dict] | None]:
    """(nom, erreur). `null` = aucune variante (défaut inchangé). Erreur = (statut, code, message, champs en plus) : un non-chaîne
    400 `bad_prefix_variant` ; un nom hors liste blanche 400 `unknown_prefix_variant` avec `valid` ; avec `prefix: "host"` 400
    `prefix_variant_conflict` (les deux choisissent le préfixe). Le message ne reprend jamais plus de 40 caractères de la valeur."""
    if raw is None:
        return "", None
    if not isinstance(raw, str):
        return "", (400, VARIANT_TYPE_CODE, "prefix_variant : nom de variante (chaîne) attendu", {})
    if raw not in declared:
        return "", (400, VARIANT_CODE, f"prefix_variant : variante inconnue ({raw[:40]!r}) ; valeurs : {', '.join(declared) or '(aucune)'}",
                    {"valid": list(declared)})
    if prefix_mode:
        return "", (400, VARIANT_CONFLICT_CODE, f"prefix_variant : incompatible avec prefix: {prefix_mode!r} (les deux choisissent le préfixe)", {})
    return raw, None


__all__ = ["CODE", "ERROR", "MODES", "PrefixModes", "VARIANT_CODE", "declared_variants", "registry", "validate", "validate_variant",
           "variant_registry"]
