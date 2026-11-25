"""Lecture des résultats d'outils clôturés (`qa/untrusted.py::fenced_result`) dans les tests : en-tête éventuel des
consignes suspectes, ligne JSON des métadonnées du noyau, bloc `<untrusted-data>` du contenu de tiers."""
from __future__ import annotations

import json
import re

_BLOCK = re.compile(r'<untrusted-data kind="([^"]+)" id="(\w+)"( suspect="consigne")?>\n(.*)\n</untrusted-data id="\2">\Z',
                    re.S)


def parse(content: str, field: str = "output") -> dict:
    """Métadonnées + le contenu clôturé sous `field` ; `_kind`, `_suspect`, `_header` pour les vérifications."""
    lines = content.split("\n")
    header = lines.pop(0) if lines and lines[0].startswith("[Consignes suspectes") else ""
    meta = json.loads(lines[0])
    m = _BLOCK.match("\n".join(lines[1:]))
    assert m, f"contenu non clôturé : {content[:200]!r}"
    return {**meta, field: m.group(4), "_kind": m.group(1), "_suspect": bool(m.group(3)), "_header": header}
