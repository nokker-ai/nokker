"""Correctif « rafale de cartes » (09.11) : sur 24 h simulées, un nœud que personne n'approuve, qui redémarre, dont le
noyau redémarre, et qui croise 410, 404, 429 et un noyau injoignable, ne pose qu'UNE carte « Relier le bureau » (donc
une seule notification relayée vers nokker.ai). Avant le correctif : inscription de 600 s, le nœud sortait sur 410 et
docker le relançait sur une nouvelle carte, soit environ 140 cartes par jour."""
from __future__ import annotations

import json
import os
import stat
from types import SimpleNamespace

import pytest

from nokker.desktop import node as N
from nokker.desktop import service as S
from nokker.desktop.node import NodeClient, NodeError
from nokker.desktop.proof import PROOF_HEADER
from nokker.desktop.service import DesktopError, build_desktop

from .test_desktop import _executor

T0 = 1_800_000_000.0
DAY = 24 * 3600.0


class Approvals:
    """Cartes en base : elles survivent au redémarrage du noyau (comme la vraie table), échéance 24 h."""

    def __init__(self, clock):
        self.cards, self.clock = [], clock

    def request(self, turn, call, verdict, note):
        now = self.clock()
        card = SimpleNamespace(id=f"ap_{len(self.cards) + 1}", call=call, verdict=verdict, tool_name=call.name,
                               status="pending", created_at=now, expires_at=now + DAY, error=None)
        self.cards.append(card)
        return card

    def get(self, aid):
        c = next((c for c in self.cards if c.id == aid), None)
        if c is not None and c.status == "pending" and self.clock() > c.expires_at:
            c.status = "expired"
        return c

    def pending(self, user=None):
        return [c for c in self.cards if self.get(c.id).status == "pending"]

    def mark_failed(self, aid, error):
        c = self.get(aid)
        c.status, c.error = "failed", error
        return c


class Sim:
    def __init__(self, tmp_path, monkeypatch):
        self.now = T0
        self.tmp = tmp_path
        self.approvals = Approvals(lambda: self.now)
        self.down_until = 0.0     # noyau injoignable (statut 0)
        self.rate_until = 0.0     # noyau qui répond 429
        self.forget = False       # une fois : 404 (inscription inconnue)
        self.at = []              # (instant, geste) joués au premier appel après l'instant
        monkeypatch.setattr(N.time, "time", lambda: self.now)  # horodatage des preuves du nœud
        self.boot_kernel()

    def boot_kernel(self):
        self.svc = build_desktop({"enabled": True}, self.tmp / "st")
        self.svc.clock = lambda: self.now
        self.svc.approvals = self.approvals

    def http(self, method, path, raw, headers):
        for t, fn in list(self.at):
            if self.now >= t:
                self.at.remove((t, fn))
                fn()
        if self.now < self.down_until:
            return 0, {}
        if self.now < self.rate_until:
            return 429, {"error": {"code": "rate"}}
        try:
            if method == "POST" and path == "/v1/desktop/node/enroll":
                return 202, self.svc.enroll(json.loads(raw))
            if method == "GET" and path.startswith("/v1/desktop/node/enroll/"):
                aid = path.rsplit("/", 1)[1]
                if self.forget:
                    self.forget = False
                    return 404, {"error": {"code": "not_found"}}
                return self.svc.pickup(aid, method, headers["Host"], path, raw, headers.get(PROOF_HEADER))
        except DesktopError as e:
            return e.status, {"error": {"code": e.code}}
        raise AssertionError(f"route inattendue {method} {path}")

    def node(self, until):
        """Un démarrage du nœud, jusqu'à `until` (puis arrêt, comme un `docker restart`)."""
        ex, _ = _executor(self.tmp)
        client = NodeClient(base="http://127.0.0.1:8742", state_dir=self.tmp / "node", executor=ex, http=self.http,
                            now=lambda: self.now)
        waits = []

        def wait(s=None):
            if s is None:  # « dort jusqu'à l'arrêt »
                self.now = until
            else:
                waits.append(s)
                self.now += s
            if self.now >= until:
                client.stop.set()
            return client.stop.is_set()

        client.stop.wait = wait
        with pytest.raises(NodeError) as e:
            client.credentials()
        assert e.value.detail == {"code": "stopped"}  # jamais une sortie sur une réponse du noyau
        return waits


def test_one_card_in_24_simulated_hours(tmp_path, monkeypatch):
    sim = Sim(tmp_path, monkeypatch)
    waits = []
    # 0-1 h : premier démarrage ; le relevé passe de 5 s à 30 s après 2 min.
    waits += sim.node(T0 + 3600)
    assert waits[:24] == [5.0] * 24 and set(waits[30:]) == {30.0}
    # 1 h : le nœud redémarre (même clé) ; 2 h : le noyau redémarre (inscription en mémoire perdue avant le correctif)
    waits += sim.node(T0 + 2 * 3600)
    sim.boot_kernel()
    reg = tmp_path / "st" / "desktop" / "enroll-pending.json"
    assert stat.S_IMODE(os.stat(reg).st_mode) == 0o600 and "dn_" not in reg.read_text()
    # 2-6 h : noyau injoignable 20 min, puis 429 pendant 30 min, puis une inscription « inconnue » (404).
    sim.down_until, sim.rate_until = T0 + 2 * 3600 + 1200, T0 + 2 * 3600 + 3000
    waits += sim.node(T0 + 6 * 3600)
    sim.forget = True
    waits += sim.node(T0 + 12 * 3600)
    sim.boot_kernel()
    waits += sim.node(T0 + DAY - 60)
    cards = sim.approvals.cards
    assert [c.id for c in cards] == ["ap_1"] and cards[0].status == "pending", [(c.id, c.status) for c in cards]
    assert len(waits) < 3500  # ~2900 relevés en 24 h (un toutes les 30 s), pas un toutes les 5 s


def test_expired_card_gives_one_new_card_not_a_storm(tmp_path, monkeypatch):
    """Au-delà de 24 h, la carte expire : UNE nouvelle demande (410 puis nouvelle carte), pas une rafale."""
    sim = Sim(tmp_path, monkeypatch)
    sim.node(T0 + DAY + 3600)
    assert [c.id for c in sim.approvals.cards] == ["ap_1", "ap_2"]


def test_refused_card_is_not_asked_again(tmp_path, monkeypatch):
    sim = Sim(tmp_path, monkeypatch)
    sim.at.append((T0 + 60, lambda: sim.svc.on_decision(SimpleNamespace(id="ap_1", status="refused"))))
    waits = sim.node(T0 + 7200)  # 403 : le nœud dort jusqu'à son arrêt, ne redemande pas
    assert len(sim.approvals.cards) == 1 and len(waits) < 20


def test_approval_refused_when_the_node_is_gone(tmp_path, monkeypatch):
    """Garde : une carte approuvée alors que le nœud ne la relève plus depuis 15 min ne relie rien."""
    sim = Sim(tmp_path, monkeypatch)
    sim.node(T0 + 60)
    sim.now += S.STALE_NODE_S + 1
    assert sim.svc.on_decision(SimpleNamespace(id="ap_1", status="approved")) == S.ENROLL_STALE
    assert not sim.svc.enrolled()
    # un nœud présent, lui, est relié
    sim2 = Sim(tmp_path / "b", monkeypatch)
    sim2.node(T0 + 60)
    assert sim2.svc.on_decision(SimpleNamespace(id="ap_1", status="approved")).startswith("bureau ")
