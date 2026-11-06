"""`python -m payagent` : démarre l'agent de paiement (refuse de démarrer à moitié configuré)."""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import urllib.request

from payagent.config import AgentConfig
from payagent.http import serve
from payagent.service import PayAgent, sign_callback
from payagent.issuers.stripe_spt import Stripe
from payagent.issuers.wise import Wise
from payagent.store import PgDevices, PgStore
from payagent.vault import EvervaultVault


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg = AgentConfig.from_env()
    problems = cfg.problems()
    if problems:
        for p in problems:
            logging.error("configuration : %s", p)
        return 2

    def notify(body: dict) -> None:
        if not cfg.callback_url:
            return
        raw = json.dumps(body, ensure_ascii=False).encode()
        headers = {"Content-Type": "application/json",
                   "X-Nokker-PayAgent-Signature": sign_callback(raw, cfg.callback_secret, int(time.time()))}
        if os.environ.get("PAYAGENT_CALLBACK_HOST"):
            # Lokkal : le noyau n'accepte que ses Host déclarés ; 127.0.0.1:8742 l'est toujours (sonde de santé).
            headers["Host"] = os.environ["PAYAGENT_CALLBACK_HOST"]
        req = urllib.request.Request(cfg.callback_url, data=raw, method="POST", headers=headers)
        urllib.request.urlopen(req, timeout=10).close()

    stripe = issuer = vault = cards = open_page = None
    if cfg.issuer == "stripe_spt":
        stripe = Stripe(cfg.stripe_key, base=cfg.stripe_base)
    elif cfg.issuer == "own_card":
        from nokker.secrets.openbao import OpenBaoSecrets, session_from_config
        from payagent.owncard import CardVault

        cards = CardVault(OpenBaoSecrets(session=session_from_config({}), scope="system", mount=cfg.card_mount,
                                         prefix=""))
        if cfg.test_http_hosts:  # marchand de test seulement (deploy/lokkal/testshop)
            from payagent import checkout as _co

            _co.TEST_HTTP_HOSTS.update(cfg.test_http_hosts)
        if cfg.checkout_cdp_url:
            from payagent.browser import cdp_page_opener

            open_page = cdp_page_opener(cfg.checkout_cdp_url)
    else:
        issuer = Wise(cfg.wise_client_id, cfg.wise_client_secret, base=cfg.wise_base)
        vault = EvervaultVault(app_id=cfg.evervault_app_id, api_key=cfg.evervault_api_key,
                               function=cfg.evervault_function)
    agent = PayAgent(cfg, store=PgStore(cfg.dsn), devices=PgDevices(cfg.devices_dsn), stripe=stripe, issuer=issuer,
                     vault=vault, notify=notify, background=True, cards=cards, open_page=open_page)
    if issuer is not None:  # cartes dont le mandat a expiré : bloquées chaque minute
        def sweep() -> None:
            while True:
                time.sleep(60)
                try:
                    agent.expire_due()
                except Exception:
                    logging.exception("balayage des cartes expirées en échec")

        threading.Thread(target=sweep, daemon=True).start()
    serve(agent)
    return 0


if __name__ == "__main__":
    sys.exit(main())
