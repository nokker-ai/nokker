"""Validation des images d'un message (X7). Aucune confiance dans ce que dit le client : ni le nom, ni le type déclaré.

Formes acceptées pour une pièce jointe (docs/LOKKAL.md §7.6) :
  {"data": "<base64>", "media_type"?: "image/png", "name"?: "capture.png"}   octets en base64 (standard, sans retour)
  {"url": "data:image/png;base64,<…>", "name"?: …}                            data URL
  {"path": "captures/a.png"}                                                  fichier sous le dossier de la session
et, dans le `content` d'un message au format OpenAI, la partie `{"type": "image_url", "image_url": {"url": "data:…"}}`.
Jamais d'adresse http(s) : le noyau ne va rien chercher sur le réseau pour le compte d'un message.
"""
from __future__ import annotations

import base64
import binascii
import os
from dataclasses import dataclass
from typing import Any

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGES_PER_MESSAGE = 4
MEDIA_TYPES = ("image/png", "image/jpeg", "image/webp", "image/gif")
# base64 de 5 Mo : 4 * ceil(n / 3) caractères ; au-delà, refus AVANT de décoder (pas de pic mémoire inutile)
_MAX_B64_CHARS = 4 * ((MAX_IMAGE_BYTES + 2) // 3)


class ImageError(ValueError):
    """Pièce jointe refusée : le message dit pourquoi, sans recopier le contenu."""


@dataclass
class RawImage:
    data: bytes
    media_type: str  # lu dans les octets
    name: str = ""


def sniff_media_type(data: bytes) -> str | None:
    """Type d'après les premiers octets (signatures des formats), None si ce n'est aucun des quatre."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _checked(data: bytes, name: str, label: str) -> RawImage:
    if not data:
        raise ImageError(f"{label} : image vide")
    if len(data) > MAX_IMAGE_BYTES:
        raise ImageError(f"{label} : {len(data) // 1024} Ko, au-delà de {MAX_IMAGE_BYTES // (1024 * 1024)} Mo par image")
    media = sniff_media_type(data)
    if media is None:
        raise ImageError(f"{label} : ce n'est pas une image png, jpeg, webp ou gif (contrôle des octets)")
    return RawImage(data=data, media_type=media, name=name)


def _b64(text: str, label: str) -> bytes:
    text = "".join(text.split())  # retours à la ligne tolérés (base64 MIME)
    if len(text) > _MAX_B64_CHARS:
        raise ImageError(f"{label} : au-delà de {MAX_IMAGE_BYTES // (1024 * 1024)} Mo par image")
    try:
        return base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        raise ImageError(f"{label} : base64 invalide") from None


def _from_data_url(url: str, label: str) -> bytes:
    if not url.startswith("data:"):
        raise ImageError(f"{label} : seules les data URL sont acceptées (le noyau ne télécharge pas d'image)")
    head, sep, payload = url.partition(",")
    if not sep or not head.endswith(";base64"):
        raise ImageError(f"{label} : data URL sans base64")
    return _b64(payload, label)


def _from_path(path: str, cwd: str | None, label: str) -> tuple[bytes, str]:
    if not cwd:
        raise ImageError(f"{label} : un chemin n'est accepté que dans une session locale (POST /v1/local/sessions)")
    root = os.path.realpath(cwd)
    full = os.path.realpath(os.path.join(root, os.path.expanduser(path)))
    if full != root and not full.startswith(root + os.sep):
        raise ImageError(f"{label} : chemin hors du dossier de la session")
    if not os.path.isfile(full):
        raise ImageError(f"{label} : fichier introuvable dans le dossier de la session")
    if os.path.getsize(full) > MAX_IMAGE_BYTES:
        raise ImageError(f"{label} : au-delà de {MAX_IMAGE_BYTES // (1024 * 1024)} Mo par image")
    with open(full, "rb") as f:
        return f.read(MAX_IMAGE_BYTES + 1), os.path.basename(full)


def _one(item: Any, cwd: str | None, label: str) -> RawImage:
    if not isinstance(item, dict):
        raise ImageError(f"{label} : objet attendu")
    declared = item.get("media_type")
    if declared is not None and str(declared).lower() not in MEDIA_TYPES:
        raise ImageError(f"{label} : type {str(declared)[:40]!r} non pris en charge (png, jpeg, webp, gif)")
    name = os.path.basename(str(item.get("name") or ""))[:120]
    if item.get("data") is not None:
        return _checked(_b64(str(item["data"]), label), name, label)
    if item.get("url") is not None:
        return _checked(_from_data_url(str(item["url"]), label), name, label)
    if item.get("path") is not None:
        data, base = _from_path(str(item["path"]), cwd, label)
        return _checked(data, name or base, label)
    raise ImageError(f"{label} : « data », « url » ou « path » attendu")


def parse_attachments(items: Any, *, cwd: str | None = None) -> list[RawImage]:
    """Pièces jointes d'UN message → images validées (ordre conservé). `cwd` : dossier de la session locale, seul
    endroit où un `path` peut pointer. Lève `ImageError` au premier refus (le message entier est refusé)."""
    if items is None:
        return []
    if not isinstance(items, list):
        raise ImageError("attachments doit être une liste")
    if len(items) > MAX_IMAGES_PER_MESSAGE:
        raise ImageError(f"{len(items)} images : au plus {MAX_IMAGES_PER_MESSAGE} par message")
    return [_one(it, cwd, f"image {i + 1}") for i, it in enumerate(items)]


def split_content(content: Any) -> tuple[str, list[dict[str, Any]]]:
    """`content` d'un message au format OpenAI → (texte, pièces jointes). Une chaîne passe telle quelle ; une liste de
    parties donne le texte des parties `text` (joint par des retours) et une pièce jointe `{"url": …}` par partie
    `image_url`. Toute autre partie est refusée plutôt qu'ignorée en silence."""
    if content is None:
        return "", []
    if isinstance(content, str):
        return content, []
    if not isinstance(content, list):
        raise ImageError("content : chaîne ou liste de parties attendue")
    texts: list[str] = []
    images: list[dict[str, Any]] = []
    for part in content:
        kind = part.get("type") if isinstance(part, dict) else None
        if kind == "text":
            texts.append(str(part.get("text") or ""))
        elif kind == "image_url":
            iu = part.get("image_url")
            url = iu.get("url") if isinstance(iu, dict) else iu
            images.append({"url": str(url or "")})
        else:
            raise ImageError(f"content : partie {str(kind)[:30]!r} non prise en charge (text, image_url)")
    return "\n".join(t for t in texts if t), images


__all__ = ["MAX_IMAGES_PER_MESSAGE", "MAX_IMAGE_BYTES", "MEDIA_TYPES", "ImageError", "RawImage", "parse_attachments",
           "sniff_media_type", "split_content"]
