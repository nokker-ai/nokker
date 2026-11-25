"""ACCORD de bout en bout sur la boucle locale : vrai TLS (certificat tiré dans un dossier temporaire, empreinte =
host_fp), vrai WebSocket, simulateur d'appareil : appairage par mac, épinglage, hello/auth, carte, décision tranchée par
les approbations en mémoire. Aucun réseau hors 127.0.0.1."""
from __future__ import annotations

import stat
import time

import pytest

from nokker.accord.hub import AccordHub
from nokker.accord.server import AccordServer, ensure_cert, parse_bind, tls_context
from nokker.accord.simulator import SimulatedDevice
from nokker.accord.store import MemoryAccordStore
from nokker.approvals.memory import InMemoryApprovals
from nokker.core.turn import Decision, Gate, InputCheck, ToolCall, Turn, Verdict


def _card(b):
    turn = Turn(thread_id="th_x", index=0, input=InputCheck(text="écris"), channel="api", sender="local-owner")
    call = ToolCall(id="c1", name="write_file", arguments={"path": "/w/notes.md", "content": "x"})
    v = Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule="permissions:default", reason="r",
                strength="standard")
    return b.request(turn, call, v, None)


@pytest.fixture
def served(tmp_path):
    cert, key, fp = ensure_cert(tmp_path / "accord")
    assert stat.S_IMODE((tmp_path / "accord" / "key.pem").stat().st_mode) == 0o600
    assert ensure_cert(tmp_path / "accord")[2] == fp  # stable tant que le dossier d'état reste
    b = InMemoryApprovals(is_authorized=lambda u: u == "local-owner")
    hub = AccordHub(b, MemoryAccordStore(), fp)
    srv = AccordServer(hub, "127.0.0.1", 0, tls_context(cert, key)).start()
    yield hub, b, fp, f"wss://127.0.0.1:{srv.port}"
    srv.stop()


def test_pair_then_decide_over_real_wss(served):
    hub, b, fp, url = served
    d = SimulatedDevice()
    assert d.connect(url, timeout=30.0) == fp
    hub.open_window()
    d.send(d.pair_message("135790"))
    time.sleep(0.3)
    dev = hub.confirm_code("135790")
    m = d.recv_type(("paired",))
    assert m == {"type": "paired", "device_id": dev.device_id, "host_fp": fp, "key8": d.key8}
    d.close()
    d.pin = m["host_fp"]
    d.connect(url, timeout=30.0)
    assert d.hello() == {"type": "ready", "device_id": dev.device_id}
    a = _card(b)
    c = d.wait_card()
    assert c["card_id"] == a.id and c["level"] == "ordinary"
    d.send(d.decision_message("approve"))
    end = time.time() + 3
    while b.get(a.id).status == "pending" and time.time() < end:
        time.sleep(0.05)
    assert b.get(a.id).status == "approved"
    d.close()


def test_device_refuses_another_host(served, tmp_path):
    _hub, _b, _fp, url = served
    d = SimulatedDevice(pin="0" * 64)
    with pytest.raises(ConnectionError):
        d.connect(url, timeout=30.0)  # suite complète en parallèle sur une machine chargée : ouverture TLS lente


def test_parse_bind():
    assert parse_bind("192.168.1.20:8443") == ("192.168.1.20", 8443)
    assert parse_bind("[::1]:8443") == ("::1", 8443)
    assert parse_bind("") is None and parse_bind("x:0") is None and parse_bind("x") is None


def test_no_tls13_session_tickets(tmp_path):
    cert, key, _ = ensure_cert(tmp_path / "accord")
    assert tls_context(cert, key).num_tickets == 0


def test_a_hundred_openings_in_a_row_all_answer_fast(served):
    """Régression du 18.11.2026 : avec les tickets TLS 1.3, ~1 ouverture sur 10 restait sans réponse d'upgrade (5 s
    perdues, ou « SSL internal error ») ; un vrai bouton qui ne répond pas. Délai court exprès : pas de marge cachée."""
    _, _, fp, url = served
    failures = []
    t0 = time.monotonic()
    for i in range(100):
        d = SimulatedDevice()
        try:
            assert d.connect(url, timeout=2.0) == fp
        except Exception as e:  # noqa: BLE001 — on compte, on ne s'arrête pas au premier
            failures.append(f"{i}: {type(e).__name__}")
        finally:
            d.close()
    assert failures == [] and time.monotonic() - t0 < 30
