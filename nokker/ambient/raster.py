"""Rendu raster minimal pour les écrans e-ink (docs/AMBIENT.md §3.4) : une toile en niveaux de gris, du texte en police
bitmap (nokker/ambient/font.py), et un encodeur PNG en Python pur (zlib + struct), sans Pillow.

Deux profondeurs, celles que lisent les cadres e-ink :
  - `depth=1` : noir et blanc (PNG gris 1 bit) ;
  - `depth=2` : 4 gris (PNG gris 2 bits).
Les niveaux de la toile vont de 0 (noir) à `levels - 1` (blanc) ; le PNG les écrit tels quels (gris « 0 = noir »).
Le rendu est déterministe : mêmes entrées, mêmes octets (aucune date ni métadonnée dans le PNG).
"""
from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field

from nokker.ambient.font import GLYPHS
from nokker.ambient.font import HEIGHT as GLYPH_H
from nokker.ambient.font import WIDTH as GLYPH_W

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_FALLBACK = "?"


@dataclass
class Canvas:
    width: int
    height: int
    depth: int = 1  # 1 (noir/blanc) ou 2 (4 gris)
    pixels: bytearray = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.depth not in (1, 2):
            raise ValueError("profondeur 1 ou 2 bits")
        if not (1 <= self.width <= 4096 and 1 <= self.height <= 4096):
            raise ValueError("taille d'écran hors bornes")
        self.pixels = bytearray([self.white] * (self.width * self.height))

    @property
    def levels(self) -> int:
        return 1 << self.depth

    @property
    def white(self) -> int:
        return self.levels - 1

    def gray(self, fraction: float) -> int:
        """Niveau proche de `fraction` (0 = noir, 1 = blanc). En 1 bit, tout gris devient noir (lisible)."""
        if self.depth == 1:
            return 0 if fraction < 0.999 else 1
        return max(0, min(self.white, round(fraction * self.white)))

    def rect(self, x: int, y: int, w: int, h: int, level: int) -> None:
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(self.width, x + w), min(self.height, y + h)
        if x1 <= x0 or y1 <= y0:
            return
        row = bytes([level]) * (x1 - x0)
        for yy in range(y0, y1):
            off = yy * self.width
            self.pixels[off + x0:off + x1] = row

    def text(self, x: int, y: int, s: str, *, scale: int = 1, level: int = 0) -> int:
        """Écrit `s` sur une ligne ; rend la largeur dessinée. Un caractère hors police devient « ? »."""
        cx = x
        for ch in s:
            hx = GLYPHS.get(ch) or GLYPHS[_FALLBACK]
            rows = bytes.fromhex(hx)
            for ry, bits in enumerate(rows):
                if not bits:
                    continue
                for rx in range(GLYPH_W):
                    if bits & (0x80 >> rx):
                        self.rect(cx + rx * scale, y + ry * scale, scale, scale, level)
            cx += GLYPH_W * scale
        return cx - x

    def png(self) -> bytes:
        return encode_png(self.width, self.height, self.depth, self.pixels)


def text_width(s: str, scale: int = 1) -> int:
    return len(s) * GLYPH_W * scale


def line_height(scale: int = 1) -> int:
    return GLYPH_H * scale


def wrap(text: str, max_chars: int) -> list[str]:
    """Coupe aux espaces ; un mot plus long que la ligne est coupé net."""
    out: list[str] = []
    for para in (text or "").splitlines():
        words = para.split()
        cur = ""
        for w in words:
            while len(w) > max_chars:
                if cur:
                    out.append(cur)
                    cur = ""
                out.append(w[:max_chars])
                w = w[max_chars:]
            if not cur:
                cur = w
            elif len(cur) + 1 + len(w) <= max_chars:
                cur += " " + w
            else:
                out.append(cur)
                cur = w
        if cur:
            out.append(cur)
    return out


def _chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def encode_png(width: int, height: int, depth: int, pixels: bytes | bytearray) -> bytes:
    """PNG gris (type 0) à `depth` bits (1 ou 2) ; `pixels` = un niveau par octet, ligne par ligne."""
    if depth not in (1, 2):
        raise ValueError("profondeur 1 ou 2 bits")
    per_byte = 8 // depth
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filtre « None »
        row = pixels[y * width:(y + 1) * width]
        for i in range(0, width, per_byte):
            b = 0
            for j in range(per_byte):
                v = row[i + j] if i + j < width else 0
                b = (b << depth) | (v & ((1 << depth) - 1))
            raw.append(b)
    ihdr = struct.pack(">IIBBBBB", width, height, depth, 0, 0, 0, 0)
    return PNG_MAGIC + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + _chunk(b"IEND", b"")


def png_info(data: bytes) -> tuple[int, int, int, int]:
    """(largeur, hauteur, profondeur, type de couleur) lus dans l'en-tête IHDR — pour les tests et le contrôle."""
    if not data.startswith(PNG_MAGIC) or data[12:16] != b"IHDR":
        raise ValueError("pas un PNG")
    w, h, depth, ctype = struct.unpack(">IIBB", data[16:26])
    return w, h, depth, ctype


def decode_gray_png(data: bytes) -> tuple[int, int, int, list[int]]:
    """Relit un PNG écrit par `encode_png` (gris, filtre 0) : (largeur, hauteur, profondeur, niveaux). Tests seulement."""
    w, h, depth, _ = png_info(data)
    pos, idat = 8, b""
    while pos < len(data):
        (ln,) = struct.unpack(">I", data[pos:pos + 4])
        tag = data[pos + 4:pos + 8]
        if tag == b"IDAT":
            idat += data[pos + 8:pos + 8 + ln]
        pos += 12 + ln
    raw = zlib.decompress(idat)
    stride = (w * depth + 7) // 8
    out: list[int] = []
    for y in range(h):
        row = raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)]
        for x in range(w):
            bit = x * depth
            out.append((row[bit // 8] >> (8 - depth - bit % 8)) & ((1 << depth) - 1))
    return w, h, depth, out


__all__ = ["Canvas", "decode_gray_png", "encode_png", "line_height", "png_info", "text_width", "wrap"]
