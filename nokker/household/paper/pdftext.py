"""Texte d'un PDF, au mieux et sans dépendance (le noyau n'embarque pas de bibliothèque PDF) : flux `FlateDecode` ou
non compressés, flux d'objets (`ObjStm`, PDF 1.5+), opérateurs de texte `Tj`, `TJ`, `'`, `"`, polices simples et
polices composites avec table `ToUnicode` (bfchar, bfrange). Pas d'OCR : un PDF fait d'images (scan) rend un texte
vide, et le document est alors traité comme une photo (lecture par le modèle sur une image jointe au message).

Garde-fous : taille d'entrée bornée par l'appelant, décompression bornée (`MAX_INFLATE`), nombre d'objets borné, aucune
exécution (pas de JavaScript, pas de formulaires), aucune ressource externe.
"""
from __future__ import annotations

import re
import zlib

MAX_INFLATE = 8 * 1024 * 1024  # octets décompressés par flux
MAX_OBJECTS = 20_000
MAX_TEXT = 200_000

_OBJ_RE = re.compile(rb"(\d+)\s+(\d+)\s+obj\b(.*?)\bendobj", re.DOTALL)
_STREAM_RE = re.compile(rb"^(.*?)\bstream\r?\n(.*)\bendstream", re.DOTALL)
_REF_RE = re.compile(rb"(\d+)\s+\d+\s+R")


def _inflate(data: bytes) -> bytes:
    d = zlib.decompressobj()
    try:
        out = d.decompress(data, MAX_INFLATE)
    except zlib.error:
        return b""
    return out


def _stream(dict_part: bytes, raw: bytes) -> bytes | None:
    if b"/FlateDecode" in dict_part or b"/Fl " in dict_part or b"/Fl]" in dict_part:
        return _inflate(raw.rstrip(b"\r\n"))
    if b"/Filter" in dict_part:
        return None  # DCT (image), LZW, ASCII85… : pas du texte qu'on lit ici
    return raw


def _objects(pdf: bytes) -> dict[int, tuple[bytes, bytes | None]]:
    """num → (dictionnaire, flux décodé ou None). Les objets des flux d'objets sont dépliés."""
    objs: dict[int, tuple[bytes, bytes | None]] = {}
    for m in _OBJ_RE.finditer(pdf):
        if len(objs) >= MAX_OBJECTS:
            break
        num, body = int(m.group(1)), m.group(3)
        sm = _STREAM_RE.match(body)
        if sm:
            objs[num] = (sm.group(1), _stream(sm.group(1), sm.group(2)))
        else:
            objs[num] = (body, None)
    for num, (head, data) in list(objs.items()):
        if data is None or b"/ObjStm" not in head:
            continue
        n = re.search(rb"/N\s+(\d+)", head)
        first = re.search(rb"/First\s+(\d+)", head)
        if not n or not first:
            continue
        nums = re.findall(rb"\d+", data[:int(first.group(1))])
        pairs = [(int(nums[i]), int(nums[i + 1])) for i in range(0, min(len(nums) - 1, 2 * int(n.group(1))), 2)]
        base = int(first.group(1))
        for i, (onum, off) in enumerate(pairs):
            end = pairs[i + 1][1] if i + 1 < len(pairs) else len(data) - base
            if onum not in objs and len(objs) < MAX_OBJECTS:
                objs[onum] = (data[base + off: base + end], None)
    return objs


def _hex(s: bytes) -> bytes:
    s = re.sub(rb"\s+", b"", s)
    if len(s) % 2:
        s += b"0"
    try:
        return bytes.fromhex(s.decode("ascii"))
    except ValueError:
        return b""


def _cmap(data: bytes) -> tuple[dict[bytes, str], int]:
    """Table ToUnicode → ({code: texte}, largeur des codes en octets)."""
    out: dict[bytes, str] = {}
    width = 1

    def uni(h: bytes) -> str:
        raw = _hex(h)
        try:
            return raw.decode("utf-16-be")
        except UnicodeDecodeError:
            return ""

    for block in re.findall(rb"beginbfchar(.*?)endbfchar", data, re.DOTALL):
        for src, dst in re.findall(rb"<([0-9A-Fa-f\s]+)>\s*<([0-9A-Fa-f\s]*)>", block):
            code = _hex(src)
            width = max(width, len(code))
            out[code] = uni(dst)
    for block in re.findall(rb"beginbfrange(.*?)endbfrange", data, re.DOTALL):
        for lo, hi, rest in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(<[0-9A-Fa-f]+>|\[[^\]]*\])", block):
            a, b = _hex(lo), _hex(hi)
            width = max(width, len(a))
            ia, ib = int.from_bytes(a, "big"), int.from_bytes(b, "big")
            if ib - ia > 65535:
                continue
            if rest.startswith(b"["):
                dsts = re.findall(rb"<([0-9A-Fa-f]+)>", rest)
                for k, d in enumerate(dsts[: ib - ia + 1]):
                    out[(ia + k).to_bytes(len(a), "big")] = uni(d)
            else:
                start = _hex(rest[1:-1])
                for k in range(ib - ia + 1):
                    v = (int.from_bytes(start, "big") + k).to_bytes(max(2, len(start)), "big")
                    try:
                        out[(ia + k).to_bytes(len(a), "big")] = v.decode("utf-16-be")
                    except UnicodeDecodeError:
                        continue
    return out, width


def _fonts(objs: dict[int, tuple[bytes, bytes | None]]) -> dict[bytes, tuple[dict[bytes, str], int]]:
    """Nom de ressource (`/F1`) → table ToUnicode, pour toutes les pages (approximation : un même nom désigne la même
    police dans tout le document, ce que font les générateurs usuels)."""
    by_obj: dict[int, tuple[dict[bytes, str], int]] = {}
    for num, (head, _) in objs.items():
        m = re.search(rb"/ToUnicode\s+(\d+)\s+\d+\s+R", head)
        if m and int(m.group(1)) in objs and objs[int(m.group(1))][1]:
            by_obj[num] = _cmap(objs[int(m.group(1))][1] or b"")
    names: dict[bytes, tuple[dict[bytes, str], int]] = {}
    for head, _ in objs.values():
        for fm in re.finditer(rb"/Font\s*<<(.*?)>>", head, re.DOTALL):
            for name, ref in re.findall(rb"/([A-Za-z0-9_.+-]+)\s+(\d+)\s+\d+\s+R", fm.group(1)):
                if int(ref) in by_obj:
                    names[name] = by_obj[int(ref)]
        m = re.search(rb"/Font\s+(\d+)\s+\d+\s+R", head)
        if m and int(m.group(1)) in objs:
            for name, ref in re.findall(rb"/([A-Za-z0-9_.+-]+)\s+(\d+)\s+\d+\s+R", objs[int(m.group(1))][0]):
                if int(ref) in by_obj:
                    names[name] = by_obj[int(ref)]
    return names


_TOKEN_RE = re.compile(rb"\((?:\\.|[^\\)])*\)|<[0-9A-Fa-f\s]*>|\[|\]|/[^\s/\[\]()<>]+|[A-Za-z'\"*]+|-?\d*\.?\d+", re.DOTALL)


def _literal(tok: bytes) -> bytes:
    s = tok[1:-1]
    out = bytearray()
    i = 0
    esc = {ord("n"): b"\n", ord("r"): b"\r", ord("t"): b"\t", ord("b"): b"\b", ord("f"): b"\f"}
    while i < len(s):
        c = s[i]
        if c == 0x5C and i + 1 < len(s):
            n = s[i + 1]
            if n in esc:
                out += esc[n]
                i += 2
            elif 0x30 <= n <= 0x37:
                j = i + 1
                while j < len(s) and j < i + 4 and 0x30 <= s[j] <= 0x37:
                    j += 1
                out.append(int(s[i + 1:j], 8) & 0xFF)
                i = j
            elif n in (0x0A, 0x0D):
                i += 2
            else:
                out.append(n)
                i += 2
        else:
            out.append(c)
            i += 1
    return bytes(out)


def _decode(raw: bytes, font: tuple[dict[bytes, str], int] | None) -> str:
    if font is None or not font[0]:
        return raw.decode("latin-1")
    table, width = font
    out = []
    for k in range(0, len(raw), width):
        code = raw[k:k + width]
        out.append(table.get(code, "") if width > 1 else table.get(code, code.decode("latin-1")))
    return "".join(out)


def _num(tok: bytes) -> float | None:
    try:
        return float(tok)
    except ValueError:
        return None


def _new_line(op: bytes, stack: list[bytes], last_y: float | None) -> bool:
    """Un déplacement vertical (Td/TD : ty non nul ; Tm : autre ordonnée) ouvre une ligne ; un décalage horizontal
    sur la même ligne (fragments d'un traitement de texte) n'en ouvre pas."""
    if not stack:
        return True
    y = _num(stack[-1])
    if y is None:
        return True
    if op == b"Tm":
        return last_y is None or abs(y - last_y) > 1.0
    return abs(y) > 1.0


def _content_text(data: bytes, fonts: dict[bytes, tuple[dict[bytes, str], int]]) -> str:
    out: list[str] = []
    size = 0
    last_y: float | None = None
    stack: list[bytes] = []
    font = None
    in_array = False
    array: list[str] = []
    for tok in _TOKEN_RE.findall(data):
        if tok == b"[":
            in_array, array = True, []
            continue
        if tok == b"]":
            in_array = False
            stack.append(b"\x00ARRAY")
            continue
        if tok[:1] in (b"(", b"<"):
            raw = _literal(tok) if tok[:1] == b"(" else _hex(tok[1:-1])
            if in_array:
                array.append(_decode(raw, font))
            else:
                stack.append(b"\x00S" + _decode(raw, font).encode("utf-8"))
            continue
        if in_array:
            if re.fullmatch(rb"-?\d*\.?\d+", tok) and float(tok) < -200:
                array.append(" ")
            continue
        if tok == b"Tf" and len(stack) >= 2:
            name = stack[-2]
            font = fonts.get(name[1:]) if name.startswith(b"/") else None
        elif tok in (b"Tj", b"'", b'"') and stack and stack[-1].startswith(b"\x00S"):
            if tok != b"Tj":
                out.append("\n")
            piece = stack[-1][2:].decode("utf-8", "replace")
            out.append(piece)
            size += len(piece)
        elif tok == b"TJ" and stack and stack[-1] == b"\x00ARRAY":
            piece = "".join(array)
            out.append(piece)
            size += len(piece)
        elif tok == b"T*" or (tok in (b"Td", b"TD", b"Tm") and _new_line(tok, stack, last_y)):
            if out and not out[-1].endswith("\n"):
                out.append("\n")
        if tok == b"Tm" and len(stack) >= 6:
            last_y = _num(stack[-1])
        if tok[:1].isalpha() or tok in (b"'", b'"'):
            stack = []
        else:
            stack.append(tok)
        if size > MAX_TEXT:
            break
    return "".join(out)


def extract_text(pdf: bytes) -> str:
    """Texte des pages, dans l'ordre des flux de contenu. Vide si rien n'est lisible."""
    if not pdf.startswith(b"%PDF-"):
        return ""
    objs = _objects(pdf)
    fonts = _fonts(objs)
    contents: list[int] = []
    for head, _ in objs.values():
        if re.search(rb"/Type\s*/Page\b", head):
            m = re.search(rb"/Contents\s*(\[[^\]]*\]|\d+\s+\d+\s+R)", head)
            if m:
                contents += [int(r) for r in _REF_RE.findall(m.group(1))]
    parts = []
    for num in contents:
        data = objs.get(num, (b"", None))[1]
        if data:
            parts.append(_content_text(data, fonts))
    text = "\n".join(p.strip("\n") for p in parts if p.strip())
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text)[:MAX_TEXT]


def make_pdf(lines: list[str]) -> bytes:
    """PDF minimal d'une page (police Helvetica, texte latin-1, flux compressé) : pour les tests et les exemples."""
    def esc(s: str) -> bytes:
        return s.encode("latin-1", "replace").replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")

    body = b"BT /F1 11 Tf 50 800 Td 14 TL\n" + b"".join(b"(" + esc(ln) + b") Tj T*\n" for ln in lines) + b"ET"
    stream = zlib.compress(body)
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 5 0 R >> >> "
            b"/Contents 4 0 R >>",
            b"<< /Length " + str(len(stream)).encode() + b" /Filter /FlateDecode >>\nstream\n" + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, o in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + o + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{off:010d} 00000 n \n".encode() for off in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


__all__ = ["extract_text", "make_pdf"]
