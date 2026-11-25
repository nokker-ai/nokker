"""En-têtes d'amont PAR SESSION (`upstream_headers` de l'ouverture d'une session, docs/HOST-EMBED.md §7).

Un hôte qui sert le modèle derrière une passerelle de contrôle lui doit trois identifiants OPAQUES, propres à la session :
`gate_session`, `gate_org`, `gate_class` (en-têtes `X-Gate-Session`, `X-Gate-Org`, `X-Gate-Class`). Le noyau les garde
EN MÉMOIRE pour le fil, les ajoute aux appels modèle du fil (et de ses sous-agents `<fil>/sa-…`) faits par un palier dont la
config porte `gate: true`, et par aucun autre. Il ne les lit jamais pour décider quoi que ce soit, ne les écrit ni au
journal, ni au ledger, ni dans un événement, ni sur disque, ni dans une réponse. Vide (défaut) : rien ne change."""
from __future__ import annotations

import re
import threading

# Champ du corps de session -> en-tête envoyé au palier `gate: true`. Liste fermée : toute autre clé est refusée.
FIELDS = {"gate_session": "X-Gate-Session", "gate_org": "X-Gate-Org", "gate_class": "X-Gate-Class"}
MAX_VALUE = 256
_VALUE = re.compile(rf"[A-Za-z0-9._:@/+=~-]{{1,{MAX_VALUE}}}")
ERROR = ("upstream_headers : objet {gate_session?, gate_org?, gate_class?} de chaînes opaques "
         f"({MAX_VALUE} caractères au plus, lettres, chiffres et . _ : @ / + = ~ -)")


def validate(raw: object) -> tuple[dict[str, str], str | None]:
    """(en-têtes à envoyer, erreur) d'après `upstream_headers`. `{}` ou `null` : aucun en-tête, pas d'erreur. Le message
    d'erreur ne reprend jamais une valeur reçue."""
    if raw is None:
        return {}, None
    if not isinstance(raw, dict):
        return {}, ERROR
    out: dict[str, str] = {}
    for key, value in raw.items():
        if key not in FIELDS or not isinstance(value, str) or not _VALUE.fullmatch(value):
            return {}, ERROR
        out[FIELDS[key]] = value
    return out, None


class UpstreamHeaders:
    def __init__(self) -> None:
        self._by_thread: dict[str, dict[str, str]] = {}
        self._lock = threading.Lock()

    def set(self, thread_id: str, headers: dict[str, str]) -> None:
        """Pose (remplace) les en-têtes d'un fil ; vide = aucun."""
        with self._lock:
            if headers:
                self._by_thread[thread_id] = dict(headers)
                while len(self._by_thread) > 5000:  # borne mémoire : on oublie les plus anciens fils
                    self._by_thread.pop(next(iter(self._by_thread)), None)
            else:
                self._by_thread.pop(thread_id, None)

    def clear(self, thread_id: str) -> None:
        self.set(thread_id, {})

    def get(self, session_key: str | None) -> dict[str, str]:
        """En-têtes du fil désigné par la clé de session d'un appel ; un sous-agent `<fil>/sa-…` a ceux de son fil."""
        if not self._by_thread:
            return {}
        key = session_key or ""
        with self._lock:
            while key not in self._by_thread and "/sa-" in key:
                key = key.rsplit("/sa-", 1)[0]
            return dict(self._by_thread.get(key, {}))

    def __repr__(self) -> str:  # jamais une valeur dans un journal
        return f"UpstreamHeaders(threads={len(self._by_thread)})"


_REGISTRY = UpstreamHeaders()


def registry() -> UpstreamHeaders:
    return _REGISTRY


def headers_for(provider_gate: bool, session_key: str | None) -> dict[str, str]:
    """En-têtes à ajouter à un appel : ceux du fil, SEULEMENT si le palier qui appelle porte `gate: true`."""
    return registry().get(session_key) if provider_gate else {}


__all__ = ["ERROR", "FIELDS", "MAX_VALUE", "UpstreamHeaders", "headers_for", "registry", "validate"]
