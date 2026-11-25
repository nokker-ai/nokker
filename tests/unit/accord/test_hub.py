"""ACCORD côté hôte (docs/ACCORD-DEVICE.md v1.1) : appairage (fenêtre, mac, verrou, ambiguïté, clé de test), hello lié à
l'hôte, une carte à la fois avec `withdraw`, décisions vérifiées (signature, nonce, délai, shown, carte forte, débit,
révocation) puis tranchées par l'API d'approbation existante. Faux appareil (`SimulatedDevice` sans réseau), vraies
approbations en mémoire, horloge à la main."""
from __future__ import annotations

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from nokker.accord import protocol as P
from nokker.accord.hub import LOCK_S, RATE_MAX, TEST_PUBKEY, WINDOW_S, AccordHub, PairError
from nokker.accord.policy import level_of, title_of
from nokker.accord.simulator import SimulatedDevice
from nokker.accord.store import MemoryAccordStore
from nokker.accord.vectors import TEST_SEED
from nokker.approvals.memory import InMemoryApprovals
from nokker.core.turn import Decision, Gate, InputCheck, ToolCall, Turn, Verdict

NOW = 1_800_000_000.0
HOST_FP = "ab" * 32
OTHER_FP = "cd" * 32


class Clock:
    def __init__(self):
        self.t = NOW

    def __call__(self):
        return self.t


def card(b, *, rule="permissions:default", strength="standard", cid="c1", thread="th_salon", path="notes.md"):
    turn = Turn(thread_id=thread, index=0, input=InputCheck(text="écris"), channel="api", sender="local-owner")
    call = ToolCall(id=cid, name="write_file", arguments={"path": f"/home/u/projets/{path}", "content": "x"})
    v = Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule=rule, reason="r", strength=strength)
    return b.request(turn, call, v, None)


def make(allow_test_key=False):
    clock = Clock()
    b = InMemoryApprovals(clock=clock, is_authorized=lambda u: u == "local-owner")
    executed = []
    hub = AccordHub(b, MemoryAccordStore(), HOST_FP, clock=clock, allow_test_key=allow_test_key,
                    execute=executed.append)
    return hub, b, clock, executed


def device(key=None):
    d = SimulatedDevice(key)
    d.host_fp = HOST_FP
    return d


def pair(hub, d, code="123456"):
    s = hub.session()
    hub.open_window()
    assert s.handle(d.pair_message(code)) == []
    dev = hub.confirm_code(code)
    assert s.poll() == [{"type": "paired", "device_id": dev.device_id, "host_fp": HOST_FP, "key8": d.key8}]
    return dev


def connect(hub, d):
    s = hub.session()
    ch = s.handle({"type": "hello", "v": 1, "pubkey": d.pubkey})
    assert ch[0]["type"] == "challenge"
    out = s.handle({"type": "auth", "sig": P.sign(d.key, P.hello_bytes(ch[0]["nonce"], HOST_FP))})
    assert out[0]["type"] == "ready"
    return s


def ready(hub, b, **kw):
    d = device()
    pair(hub, d)
    s = connect(hub, d)
    a = card(b, **kw)
    msgs = s.poll()
    assert [m["type"] for m in msgs] == ["card"]
    d.card = msgs[0]
    return d, s, a


# ───────────────────────────── appairage ─────────────────────────────

def test_pair_only_inside_window_opened_by_the_person():
    hub, _b, clock, _ = make()
    d = device()
    s = hub.session()
    assert s.handle(d.pair_message("123456")) == [{"type": "error", "code": "pairing_closed"}]
    hub.open_window()
    clock.t += WINDOW_S + 1
    assert s.handle(d.pair_message("123456")) == [{"type": "error", "code": "pairing_closed"}]


def test_pair_success_shows_fingerprints_and_code_never_sent():
    hub, _b, _clock, _ = make()
    d = device()
    msg = d.pair_message("482913")
    assert "482913" not in str(msg) and set(msg) == {"type", "pubkey", "mac"}
    dev = pair(hub, d, "482913")
    st = hub.status()
    assert st["host_fp8"] == HOST_FP[:8] and st["devices"][0]["key8"] == d.key8 == dev.key8
    assert dev.device_id.startswith("acc_") and len(dev.device_id) == 16 and dev.host_fp == HOST_FP


def test_three_wrong_codes_lock_pairing_ten_minutes():
    hub, _b, clock, _ = make()
    d = device()
    s = hub.session()
    hub.open_window()
    s.handle(d.pair_message("111111"))
    for _ in range(2):
        with pytest.raises(PairError, match="bad_code"):
            hub.confirm_code("222222")
    with pytest.raises(PairError, match="pairing_locked"):
        hub.confirm_code("222222")
    assert s.handle(d.pair_message("111111")) == [{"type": "error", "code": "pairing_locked"}]
    with pytest.raises(PairError, match="pairing_locked"):
        hub.open_window()
    clock.t += LOCK_S + 1
    hub.open_window()  # le verrou tombe après 10 min


def test_code_matching_two_requests_is_refused_and_window_closed():
    hub, _b, _clock, _ = make()
    a, c = device(), device()
    hub.open_window()
    hub.session().handle(a.pair_message("123456"))
    hub.session().handle(c.pair_message("123456"))
    with pytest.raises(PairError, match="ambiguous"):
        hub.confirm_code("123456")
    assert hub.status()["pairing"]["open"] is False and hub.status()["devices"] == []


def test_at_most_three_pending_requests():
    hub, _b, _clock, _ = make()
    hub.open_window()
    for i in range(3):
        assert hub.session().handle(device().pair_message(f"00000{i}")) == []
    assert hub.session().handle(device().pair_message("000009")) == [{"type": "error", "code": "pairing_closed"}]


def test_public_test_key_is_never_paired_by_default():
    hub, _b, _clock, _ = make()
    d = device(Ed25519PrivateKey.from_private_bytes(TEST_SEED))
    assert d.pubkey == TEST_PUBKEY
    hub.open_window()
    assert hub.session().handle(d.pair_message("123456")) == [{"type": "error", "code": "test_key"}]


def test_public_test_key_paired_only_with_explicit_flag():
    hub, _b, _clock, _ = make(allow_test_key=True)
    d = device(Ed25519PrivateKey.from_private_bytes(TEST_SEED))
    assert pair(hub, d).pubkey == TEST_PUBKEY


# ───────────────────────────── connexion ─────────────────────────────

def test_hello_unpaired_bad_version_and_relayed_auth():
    hub, _b, _clock, _ = make()
    d = device()
    s = hub.session()
    assert s.handle({"type": "hello", "v": 1, "pubkey": d.pubkey}) == [{"type": "error", "code": "unpaired"}]
    assert s.handle({"type": "hello", "v": 2, "pubkey": d.pubkey}) == [{"type": "error", "code": "bad_version"}]
    pair(hub, d)
    ch = s.handle({"type": "hello", "v": 1, "pubkey": d.pubkey})
    # auth calculée pour un AUTRE hôte (relais) : refusée
    relayed = P.sign(d.key, P.hello_bytes(ch[0]["nonce"], OTHER_FP))
    assert s.handle({"type": "auth", "sig": relayed}) == [{"type": "error", "code": "bad_sig"}]
    assert s.poll() == []  # pas de carte sans auth


def test_unauthenticated_session_gets_no_card(  ):
    hub, b, _clock, _ = make()
    d = device()
    pair(hub, d)
    s = hub.session()
    s.handle({"type": "hello", "v": 1, "pubkey": d.pubkey})  # clé publique connue, mais pas de signature
    card(b)
    assert s.poll() == []


# ───────────────────────────── cartes ─────────────────────────────

def test_one_card_at_a_time_with_withdraw_when_decided_elsewhere():
    hub, b, _clock, _ = make()
    d, s, a1 = ready(hub, b, cid="c1")
    a2 = card(b, cid="c2", path="autre.md")
    assert s.poll() == []  # une seule carte montrée
    assert d.card["card_id"] == a1.id and d.card["level"] == "ordinary" and len(d.card["title"]) <= 60
    assert "/home/u" not in d.card["title"]  # pas de chemin complet sur l'écran
    b.decide_local(a1.id, "approve", "local-owner")  # tranchée ailleurs
    msgs = s.poll()
    assert msgs[0] == {"type": "withdraw", "card_id": a1.id} and msgs[1]["card_id"] == a2.id


def test_expired_card_is_withdrawn_by_host_clock():
    hub, b, clock, _ = make()
    _d, s, a = ready(hub, b)
    clock.t = a.expires_at + 1
    assert s.poll()[0] == {"type": "withdraw", "card_id": a.id}


def test_card_with_control_character_is_never_sent():
    hub, b, _clock, _ = make()
    d = device()
    pair(hub, d)
    s = connect(hub, d)
    card(b, path="a\u001fb.md")
    out = s.poll()
    assert out == [] or "\u001f" not in out[0]["title"]
    for m in out:
        P.check_card(m)


# ───────────────────────────── décisions ─────────────────────────────

def test_ordinary_approve_goes_through_existing_approvals_api():
    hub, b, _clock, executed = make()
    d, s, a = ready(hub, b)
    assert s.handle(d.decision_message("approve")) == []
    got = b.get(a.id)
    assert got.status == "approved" and got.decided_via == "local-token" and got.decided_by == "local-owner"
    ev = [e for e in b._events if e[0] == a.id and e[1] == "approved"][-1][2]
    assert ev["via"] == f"accord:{d_id(hub)}"
    assert executed and executed[0].id == a.id


def d_id(hub):
    return hub.status()["devices"][0]["device_id"]


def test_refuse_keeps_device_in_ledger_only_never_in_model_text():
    hub, b, _clock, _ = make()
    d, s, a = ready(hub, b)
    assert s.handle(d.decision_message("refuse")) == []
    assert b.get(a.id).status == "rejected"
    assert b.decision_reason(a.id) is None  # le motif d'un refus remonte au modèle : vide au bouton
    ev = [e for e in b._events if e[0] == a.id and e[1] == "rejected"][-1][2]
    assert ev["via"] == f"accord:{d_id(hub)}" and "decision_reason" not in ev


def test_strong_card_cannot_be_approved_but_can_be_refused():
    hub, b, _clock, _ = make()
    d, s, a = ready(hub, b, strength="strong")
    assert d.card["level"] == "strong"
    assert s.handle(d.decision_message("approve")) == [{"type": "error", "code": "strong_card", "card_id": a.id}]
    assert b.get(a.id).status == "pending"
    assert s.handle(d.decision_message("refuse")) == []
    assert b.get(a.id).status == "rejected"


@pytest.mark.parametrize("rule", ["effect:send_external", "effect:spend", "payments:charge", "ops:prod",
                                  "home:strong", "pages:publish"])
def test_never_ordinary_rules_are_strong_even_if_standard(rule):
    _hub, b, _clock, _ = make()
    assert level_of(card(b, rule=rule)) == "strong"


def test_bad_signature_rejected():
    hub, b, _clock, _ = make()
    d, s, a = ready(hub, b)
    m = d.decision_message("approve")
    m["sig"] = device().decision_message("approve", d.card)["sig"]  # signée par une autre clé
    assert s.handle(m) == [{"type": "error", "code": "bad_sig", "card_id": a.id}]
    assert b.get(a.id).status == "pending"


def test_replay_is_rejected():
    hub, b, _clock, _ = make()
    d, s, _a = ready(hub, b)
    m = d.decision_message("refuse")
    assert s.handle(m) == []
    assert s.handle(m)[0]["code"] == "unknown_card"


def test_decision_after_expiry_by_host_clock_rejected_whatever_ts():
    hub, b, clock, _ = make()
    d, s, a = ready(hub, b)
    clock.t = a.expires_at + 1
    m = d.decision_message("approve", ts=int(NOW))  # ts de l'appareil « dans les temps » : ignoré
    assert s.handle(m) == [{"type": "error", "code": "expired", "card_id": a.id}]
    assert b.get(a.id).status != "approved"


def test_wrong_shown_rejected():
    hub, b, _clock, _ = make()
    d, s, a = ready(hub, b)
    m = d.decision_message("approve", shown=P.shown("autre chose", "ordinary", d.card["expire_at"]))
    assert s.handle(m) == [{"type": "error", "code": "bad_shown", "card_id": a.id}]
    assert b.get(a.id).status == "pending"


def test_rate_limit_five_decisions_per_minute():
    hub, b, clock, _ = make()
    d = device()
    pair(hub, d)
    s = connect(hub, d)
    for i in range(RATE_MAX):
        card(b, cid=f"c{i}", path=f"f{i}.md")
        d.card = s.poll()[-1]
        assert s.handle(d.decision_message("refuse")) == []
    card(b, cid="c9", path="f9.md")
    d.card = s.poll()[-1]
    assert s.handle(d.decision_message("refuse"))[0]["code"] == "rate_limited"
    clock.t += 61
    assert s.handle(d.decision_message("refuse")) == []


def test_revoked_device_is_refused():
    hub, b, _clock, _ = make()
    d, s, a = ready(hub, b)
    assert hub.revoke(d_id(hub)) is True
    assert s.handle(d.decision_message("approve"))[0]["code"] in ("revoked", "unpaired")
    assert b.get(a.id).status == "pending"
    s2 = hub.session()
    assert s2.handle({"type": "hello", "v": 1, "pubkey": d.pubkey}) == [{"type": "error", "code": "revoked"}]


def test_title_is_kernel_label_not_card_reason():
    _hub, b, _clock, _ = make()
    a = card(b)
    t = title_of(a)
    assert t and len(t) <= 60 and "r" != t and "/home" not in t
