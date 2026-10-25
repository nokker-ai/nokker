"""Images dans la demande (lot X7, docs/LOKKAL.md §7.6) : validation des pièces jointes, magasin d'octets par sha256,
rendu des parties de contenu pour l'amont.

- `validate.py` : lit les pièces jointes d'un message (base64, data URL, chemin dans le dossier de la session),
  contrôle le type PAR LES OCTETS (png, jpeg, webp, gif), 5 Mo par image, 4 images par message ;
- `store.py` : magasin d'octets sous l'état, par locataire, dédupliqué par sha256 ; le tour ne garde que la
  référence (`core.turn.ImageRef`) ; l'effacement d'un fil ou d'un locataire emporte ses octets ;
- `render.py` : parties de contenu de l'API compatible OpenAI (`image_url` en data URL) pour un palier avec vision,
  note texte pour un palier sans vision.
"""
from nokker.images.render import NO_VISION_NOTE, IMAGE_PART, materialize
from nokker.images.store import ImageStore
from nokker.images.validate import (
    MAX_IMAGE_BYTES, MAX_IMAGES_PER_MESSAGE, MEDIA_TYPES, ImageError, RawImage, parse_attachments, sniff_media_type,
    split_content,
)

__all__ = ["IMAGE_PART", "MAX_IMAGES_PER_MESSAGE", "MAX_IMAGE_BYTES", "MEDIA_TYPES", "NO_VISION_NOTE", "ImageError",
           "ImageStore", "RawImage", "materialize", "parse_attachments", "sniff_media_type", "split_content"]
