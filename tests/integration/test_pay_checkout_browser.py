"""Checkout own_card dans un VRAI Chromium contre le marchand de test (deploy/lokkal/testshop/shop.py), sans réseau
extérieur, sans prestataire de paiement, avec les numéros de test du marchand. Ignoré sans Playwright ni Chromium.

Prouve, de bout en bout côté agent : vérifications avant lecture du coffre, remplissage par le composant, défi par code
dans une iframe (code demandé à la personne, saisi par le composant), confirmation « dans l'appli », refus de la banque,
total supérieur au mandat."""
from __future__ import annotations

import importlib.util
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(importlib.util.find_spec("playwright") is None, reason="Playwright absent")]

SHOP = Path(__file__).resolve().parents[2] / "deploy" / "lokkal" / "testshop" / "shop.py"
HOST = "shop.nokker.test"


@pytest.fixture(scope="module")
def shop():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    proc = subprocess.Popen([sys.executable, str(SHOP), "--host", "127.0.0.1", "--port", str(port)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    yield port
    proc.terminate()


@pytest.fixture
def opener(shop):
    from payagent import checkout
    from payagent.browser import launched_page_opener

    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            p.chromium.launch().close()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Chromium indisponible : {e}")
    checkout.TEST_HTTP_HOSTS.add(HOST)
    yield launched_page_opener(args=[f"--host-resolver-rules=MAP {HOST} 127.0.0.1"])
    checkout.TEST_HTTP_HOSTS.discard(HOST)


class Secrets:
    kind, scope = "file", "system"

    def __init__(self):
        self.data, self.reads = {}, 0

    def get(self, n):
        self.reads += 1
        return self.data.get(n)

    def set(self, n, v):
        self.data[n] = v

    def delete(self, n):
        return self.data.pop(n, None) is not None

    def list(self):
        return []


def _agent(open_page):
    from payagent.config import AgentConfig
    from payagent.owncard import CardVault
    from payagent.service import PayAgent
    from payagent.store import MemoryDevices, MemoryStore

    sec = Secrets()
    agent = PayAgent(AgentConfig(issuer="own_card"), store=MemoryStore(), devices=MemoryDevices(),
                     cards=CardVault(sec), open_page=open_page, notify=lambda b: None, sleep=time.sleep)
    return agent, sec


def _card(agent, number, *, amount=4290, host=HOST):
    from payagent.store import Card

    now = time.time()
    agent.register_card("us_alice00000001", {"number": number, "exp_month": 12, "exp_year": 2031,
                                             "holder": "Alice Example", "keep_cvv": True, "cvv": "123"})
    c = Card(id="cd_t", mandate_id="md_00000000000000aa", mandate_sha256="x" * 64, account="us_alice00000001",
             issuer="own_card", profile_ref="", card_ref="own:md_t", vault_ref="", merchant_host=host,
             currency="CHF", max_amount_minor=amount, expires_at=int(now) + 3600, status="active", created_at=now,
             updated_at=now)
    agent.store.insert_card(c)
    return c


def test_frictionless_test_card_is_paid(shop, opener):
    agent, _ = _agent(opener)
    c = _card(agent, "4242 4242 4242 4242")
    out = agent._run_own(c, f"http://{HOST}:{shop}/checkout?sku=kettle")
    assert out["outcome"] == "paid", out
    assert agent.store.card_by_mandate(c.mandate_id).used


def test_otp_challenge_in_an_iframe_is_typed_by_the_component(shop, opener):
    agent, _ = _agent(opener)
    c = _card(agent, "4000 0027 6000 3184")

    def person():  # la personne voit le bandeau et tape le code reçu
        for _ in range(400):
            p = agent._prompts.get(c.mandate_id)
            if p and p["kind"] == "otp":
                agent.provide(c.account, c.mandate_id, "otp", "123456")
                return
            time.sleep(0.05)

    threading.Thread(target=person, daemon=True).start()
    out = agent._run_own(c, f"http://{HOST}:{shop}/checkout?sku=kettle")
    assert out["outcome"] == "paid", out


def test_wrong_bank_code_is_a_refusal(shop, opener):
    agent, _ = _agent(opener)
    c = _card(agent, "4000 0027 6000 3184")

    def person():
        for _ in range(400):
            p = agent._prompts.get(c.mandate_id)
            if p and p["kind"] == "otp":
                agent.provide(c.account, c.mandate_id, "otp", "000000")
                return
            time.sleep(0.05)

    threading.Thread(target=person, daemon=True).start()
    out = agent._run_own(c, f"http://{HOST}:{shop}/checkout?sku=kettle")
    assert out["outcome"] == "failed", out


def test_bank_app_confirmation_is_waited_for(shop, opener):
    agent, _ = _agent(opener)
    c = _card(agent, "4000 0082 6000 3178")
    out = agent._run_own(c, f"http://{HOST}:{shop}/checkout?sku=kettle")
    assert out["outcome"] == "paid", out


def test_total_above_the_mandate_reads_nothing_from_the_vault(shop, opener):
    agent, sec = _agent(opener)
    c = _card(agent, "4242 4242 4242 4242", amount=1000)
    reads = sec.reads
    out = agent._run_own(c, f"http://{HOST}:{shop}/checkout?sku=kettle")
    assert out["outcome"] == "refused" and sec.reads == reads


def test_cart_link_from_pay_request_goes_to_checkout_and_pays(shop, opener):
    """Le lien donné à pay_request est celui du PANIER : le composant suit « Passer au paiement » (même hôte)."""
    agent, _ = _agent(opener)
    c = _card(agent, "4242 4242 4242 4242")
    out = agent._run_own(c, f"http://{HOST}:{shop}/cart?sku=kettle")
    assert out["outcome"] == "paid", out
