"""Noms de dossiers IMAP : UTF-7 modifié (RFC 3501 §5.1.3)."""
from __future__ import annotations

import base64
import re

_B64 = re.compile(r"&([^-]*)-")


def decode(name: str) -> str:
    def one(m: re.Match[str]) -> str:
        if not m.group(1):
            return "&"
        raw = m.group(1).replace(",", "/")
        raw += "=" * (-len(raw) % 4)
        return base64.b64decode(raw).decode("utf-16-be")

    return _B64.sub(one, name)


def encode(name: str) -> str:
    out: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            b = base64.b64encode("".join(buf).encode("utf-16-be")).decode().rstrip("=").replace("/", ",")
            out.append(f"&{b}-")
            buf.clear()

    for ch in name:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            buf.append(ch)
    flush()
    return "".join(out)
