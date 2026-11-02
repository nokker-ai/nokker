"""`python -m payagent` : démarre l'agent de paiement (refuse de démarrer à moitié configuré)."""
from __future__ import annotations

import json
import logging
import sys
import time
import urllib.request

from payagent.config import AgentConfig
from payagent.http import serve
from payagent.service import PayAgent, sign_callback
from payagent.store import PgDevices, PgStore
from payagent.stripe_api import Stripe


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
        req = urllib.request.Request(cfg.callback_url, data=raw, method="POST", headers={
            "Content-Type": "application/json",
            "X-Nokker-PayAgent-Signature": sign_callback(raw, cfg.callback_secret, int(time.time()))})
        urllib.request.urlopen(req, timeout=10).close()

    agent = PayAgent(cfg, store=PgStore(cfg.dsn), devices=PgDevices(cfg.devices_dsn),
                     stripe=Stripe(cfg.stripe_key, base=cfg.stripe_base), notify=notify)
    serve(agent)
    return 0


if __name__ == "__main__":
    sys.exit(main())
