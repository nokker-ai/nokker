"""Textes figés vus par le modèle pour les dépôts en attente d'enregistrement chez le service emploi (lot L2,
`jobsd`). Relus avant toute fusion ; l'empreinte `jobs_pending_sha256()` (« jobs-pending ») les fige : tout changement
repasse par la relecture et met à jour le sha256 du test.

Textes couverts : la phrase ajoutée au résultat de `jobs_apply` quand l'enregistrement échoue, la ligne d'un dépôt en
attente dans `jobs_applications` (relances en cours, ou arrêtées après 24 h ; l'entreprise et le poste restent dans la
clôture `jobs:company`), et la phrase de `jobs_update` quand la preuve stockée remplace un argument `proof` divergent. La relance elle-même (lectures, `jobs_apply`, tâche de fond) n'écrit rien au modèle."""
from __future__ import annotations

import hashlib
import json

PENDING = {
    # `jobs_apply` : dépôt fait, enregistrement chez le service emploi en échec (ajouté au résultat « done_pending »)
    "registered_later": "Enregistrement auprès du service emploi en attente : refait automatiquement pendant 24 h ; "
                        "pas de jobs_update.",
    # `jobs_applications` : dépôt fait (preuve capturée), pas encore enregistré, relances en cours (24 h au plus)
    "line": "- {date} · envoyée · offre {offer} · preuve : page capturée · enregistrement en attente (refait "
            "automatiquement, pas de jobs_update)",
    # `jobs_applications` : relances arrêtées après 24 h (`register_expired`) ; la personne l'enregistre elle-même
    "line_expired": "- {date} · envoyée · offre {offer} · preuve : page capturée · enregistrement impossible après "
                    "24 h : jobs_update offer={offer} result=sent proof=confirmation_page pour l'enregistrer",
    # `jobs_update result=sent` sur une offre au dépôt en attente : la preuve stockée gagne sur un argument `proof` d'un
    # autre type (ligne hors clôture, après « Enregistré : … »)
    "stored_proof": "Enregistré avec la preuve du dépôt du {date} (page capturée) ; l'argument proof est ignoré.",
}


def jobs_pending_sha256() -> str:
    """Empreinte « jobs-pending » des textes de `PENDING`."""
    blob = json.dumps({"pending": PENDING}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


__all__ = ["PENDING", "jobs_pending_sha256"]
