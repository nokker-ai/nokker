"""Enregistrements TXT pour prouver un domaine (drapeau `sso`) : un résolveur DNS-over-HTTPS au format JSON
(`application/dns-json`), par le même client réseau gardé que le fournisseur (https, pas d'adresse privée). L'URL du
résolveur est un réglage de l'instance (`sso_settings.dns_over_https`) ; sans elle, aucun domaine ne se prouve."""
from __future__ import annotations

import json
from collections.abc import Callable
from urllib.parse import urlencode

from nokker.sso.idp import Http, IdpError

TXT = 16


def doh_resolver(http: Http, url: str) -> Callable[[str], list[str]]:
    sep = "&" if "?" in url else "?"

    def resolve(name: str) -> list[str]:
        status, body = http.get(url + sep + urlencode({"name": name, "type": "TXT", "ct": "application/dns-json"}))
        if status != 200:
            raise IdpError("unreachable", f"DoH HTTP {status}")
        try:
            d = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise IdpError("unreachable", "réponse DoH illisible") from None
        if d.get("Status") == 3:  # NXDOMAIN
            return []
        if d.get("Status") != 0:
            raise IdpError("unreachable", "résolution DoH en échec")
        out = []
        for a in d.get("Answer") or []:
            if isinstance(a, dict) and a.get("type") == TXT and isinstance(a.get("data"), str):
                # Un TXT peut être découpé en plusieurs chaînes entre guillemets : on les recolle (RFC 7208 §3.3).
                parts = [p for i, p in enumerate(a["data"].split('"')) if i % 2 == 1]
                out.append("".join(parts) if parts else a["data"])
        return out

    return resolve


__all__ = ["doh_resolver"]
