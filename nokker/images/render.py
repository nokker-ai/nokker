"""Rendu des images pour l'amont (X7).

Le Context Engine ne manipule jamais d'octets : il place dans le message `user` d'un tour une partie
`{"type": "nokker_image", "sha256", "media_type", "name"}` par image gardée dans la fenêtre (les plus anciennes sont
délestées en texte, comme les gros résultats). Juste avant l'appel, le noyau (`core/loop.py::_complete`) appelle
`materialize` avec la capacité du palier :
  - vision : partie `image_url` en data URL (forme de l'API compatible OpenAI) ;
  - sans vision : note texte « [image jointe non lue : modèle sans vision] » pour une image jointe au tour COURANT
    (comptée : `attachment.skipped`) ; une image d'un tour précédent devient la ligne de délestage habituelle
    (« [image délestée : … jointe au tour i] »), sans rien signaler (la question du tour ne la concerne pas) ;
  - octets absents (fil effacé, tour reçu par le miroir d'une autre machine) : note texte.
Un message dont il ne reste que du texte redevient une chaîne : un amont sans parties de contenu l'accepte tel quel.
"""
from __future__ import annotations

import base64
from typing import Any, Protocol

IMAGE_PART = "nokker_image"
NO_VISION_NOTE = "[image jointe non lue : modèle sans vision]"
MISSING_NOTE = "[image jointe indisponible : effacée ou absente de cette machine]"


class _Blobs(Protocol):
    def get(self, thread_id: str, sha: str) -> bytes | None: ...


def shed_line(name: str, media_type: str, size: int, turn_index: int) -> str:
    """Ligne qui remplace une image qui ne part pas en entier (délestée de la fenêtre, ou d'un tour précédent sur un
    palier sans vision)."""
    label = f"{name} " if name else ""
    return f"[image délestée : {label}({media_type}, {max(1, int(size or 0) // 1024)} Ko), jointe au tour {turn_index}]"


def _label(part: dict[str, Any]) -> str:
    name = str(part.get("name") or "")
    return f" — {name}" if name else ""


def has_images(messages: list[dict[str, Any]]) -> bool:
    """Au moins une partie image dans les messages assemblés (avant `materialize`)."""
    return any(isinstance(m.get("content"), list) and any(isinstance(p, dict) and p.get("type") == IMAGE_PART
                                                          for p in m["content"]) for m in messages)


def materialize(messages: list[dict[str, Any]], *, vision: bool, blobs: _Blobs | None,
                thread_id: str, current_turn: int | None = None) -> tuple[list[dict[str, Any]], int]:
    """Messages prêts pour l'amont + nombre d'images du tour courant NON envoyées faute de vision. Les messages sans
    image sont rendus tels quels (mêmes objets) ; ceux qui en portent sont copiés, jamais modifiés en place.
    `current_turn` : index du tour en cours ; une image d'un autre tour (`turn` de la partie) sans vision est délestée
    en une ligne, sans être comptée. None (inconnu) : toute image non lue est comptée, comme avant."""
    out: list[dict[str, Any]] = []
    skipped = 0
    for m in messages:
        content = m.get("content")
        if not isinstance(content, list) or not any(isinstance(p, dict) and p.get("type") == IMAGE_PART
                                                    for p in content):
            out.append(m)
            continue
        parts: list[dict[str, Any]] = []
        for p in content:
            if not (isinstance(p, dict) and p.get("type") == IMAGE_PART):
                parts.append(p)
                continue
            if not vision and current_turn is not None and p.get("turn") is not None \
                    and int(p["turn"]) != current_turn:
                parts.append({"type": "text", "text": shed_line(str(p.get("name") or ""),
                                                                str(p.get("media_type") or "image/png"),
                                                                int(p.get("size") or 0), int(p["turn"]))})
                continue
            if not vision:
                skipped += 1
                parts.append({"type": "text", "text": NO_VISION_NOTE[:-1] + _label(p) + "]"})
                continue
            data = blobs.get(thread_id, str(p.get("sha256", ""))) if blobs is not None else None
            if data is None:
                parts.append({"type": "text", "text": MISSING_NOTE[:-1] + _label(p) + "]"})
                continue
            url = f"data:{p.get('media_type') or 'image/png'};base64,{base64.b64encode(data).decode('ascii')}"
            parts.append({"type": "image_url", "image_url": {"url": url}})
        if all(p.get("type") == "text" for p in parts):
            out.append({**m, "content": "\n\n".join(str(p.get("text") or "") for p in parts if p.get("text"))})
        else:
            out.append({**m, "content": parts})
    return out, skipped


__all__ = ["IMAGE_PART", "MISSING_NOTE", "NO_VISION_NOTE", "materialize", "shed_line"]
