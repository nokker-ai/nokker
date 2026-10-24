"""nokker/previews — service d'aperçus de liens pour les bulles (docs/BUBBLES.md §5).

Point d'accroche pour le producteur de bulles `search` (chantier `p1/bubbles`, `request_previews`) :
`preview_many(urls, limit=5)` ci-dessous, sur un singleton paresseux construit depuis
`NokkerConfig`. Un appelant qui veut son propre service (tests, config non-standard) construit
`PreviewService` directement plutôt que de passer par ce singleton.
"""
from __future__ import annotations

from typing import Any

from nokker.previews.service import PreviewError, PreviewService

_default_service: PreviewService | None = None


def get_default_service(config: Any | None = None) -> PreviewService:
    """Singleton paresseux — UNE instance par processus : les routes `/v1/previews*` et le producteur de
    bulles `search` doivent partager la même clé de signature des images (sinon 401 sur l'image signée,
    vu sur le canari 18.10). Le premier appel avec `config` fixe la configuration. `config` : une `NokkerConfig` (ou tout objet portant les mêmes attributs
    `previews_cache_dir`/`previews_ttl_s`/`previews_image_cache_max_bytes`) ; `None` -> défauts."""
    global _default_service
    if _default_service is None:
        if config is None:
            from nokker.config import NokkerConfig

            config = NokkerConfig()
        _default_service = PreviewService(
            cache_dir=config.previews_cache_dir,
            ttl_s=config.previews_ttl_s,
            image_cache_max_bytes=config.previews_image_cache_max_bytes,
            sign_secret=(config.previews_sign_secret() if hasattr(config, "previews_sign_secret") else None) or None,
        )
    return _default_service


def preview_many(urls: list[str], limit: int = 5) -> list[dict]:
    """Raccourci pour `request_previews(bubble)` côté producteur `search` (§5 : « demande les
    aperçus des 5 premiers résultats en tâche de fond, publie `bubble.updated` »)."""
    return get_default_service().preview_many(urls, limit=limit)


__all__ = ["PreviewError", "PreviewService", "get_default_service", "preview_many"]
