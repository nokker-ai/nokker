"""`python -m nokker.egress serve` : proxy de sortie avec injection des secrets ; `python -m nokker.egress ca` : crée
l'AC (ca.key 0600 + ca.crt) dans un dossier.

Environnement de `serve` :
  NOKKER_EGRESS_CA_DIR            dossier de l'AC (ca.key, ca.crt ; créés s'ils manquent tous les deux)
  NOKKER_EGRESS_CONTROL_KEY(_FILE) clé du canal de contrôle (16 caractères au moins)
  LISTEN_HOST / LISTEN_PORT       0.0.0.0 / 3128 (sortie : bacs à sable, et le noyau sans jeton)
  CONTROL_HOST / CONTROL_PORT     0.0.0.0 / 3129 (contrôle : le noyau seul ; à lier à l'adresse du réseau du noyau
                                  quand elle est fixe, la clé reste exigée dans tous les cas)
  NOKKER_EGRESS_UPSTREAM_CA       fichier d'AC amont supplémentaire (tests) ; défaut : magasin système
  NOKKER_EGRESS_PINS              tests seulement : « hôte=ip,hôte=ip » (sans DNS ni contrôle des plages privées)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import ssl
import sys
from pathlib import Path


def _key() -> str:
    f = os.environ.get("NOKKER_EGRESS_CONTROL_KEY_FILE")
    if f:
        return Path(f).read_text().strip()
    return os.environ.get("NOKKER_EGRESS_CONTROL_KEY", "").strip()


def main(argv: list[str] | None = None) -> int:
    from nokker.egress.ca import CertAuthority

    ap = argparse.ArgumentParser(prog="python -m nokker.egress")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ca_p = sub.add_parser("ca", help="crée l'AC du proxy dans un dossier")
    ca_p.add_argument("--dir", required=True)
    sub.add_parser("serve", help="lance le proxy")
    args = ap.parse_args(argv)
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), stream=sys.stdout,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if args.cmd == "ca":
        ca = CertAuthority.load_or_create(args.dir)
        print(Path(args.dir, "ca.crt"), ca.cert.not_valid_after_utc.date())
        return 0
    from nokker.egress.proxy import ProxyConfig, serve

    ca = CertAuthority.load_or_create(os.environ.get("NOKKER_EGRESS_CA_DIR") or "/var/lib/nokker-egress/ca")
    upstream = None
    if os.environ.get("NOKKER_EGRESS_UPSTREAM_CA"):
        upstream = ssl.create_default_context()
        upstream.load_verify_locations(os.environ["NOKKER_EGRESS_UPSTREAM_CA"])
    pins = dict(p.split("=", 1) for p in os.environ.get("NOKKER_EGRESS_PINS", "").split(",") if "=" in p)
    cfg = ProxyConfig(ca=ca, control_key=_key(), listen_host=os.environ.get("LISTEN_HOST", "0.0.0.0"),
                      listen_port=int(os.environ.get("LISTEN_PORT", "3128")),
                      control_host=os.environ.get("CONTROL_HOST", "0.0.0.0"),
                      control_port=int(os.environ.get("CONTROL_PORT", "3129")), pins=pins, upstream_ssl=upstream)
    asyncio.run(serve(cfg))
    return 0


if __name__ == "__main__":
    sys.exit(main())
