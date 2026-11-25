"""ACCORD — vecteurs communs (docs/accord-vectors.json) : le fichier du dépôt est celui que produit
`nokker.accord.vectors`, et chaque vecteur se vérifie avec `nokker.accord.protocol` (rejoué aussi par le firmware)."""
import json
from pathlib import Path

import pytest

from nokker.accord import protocol as P
from nokker.accord.vectors import build

VECTORS = Path(__file__).resolve().parents[3] / "docs" / "accord-vectors.json"
if not VECTORS.exists():
    pytest.skip("doc ou script privé, absent de l'export public", allow_module_level=True)


def _load() -> dict:
    return json.loads(VECTORS.read_text(encoding="utf-8"))


def test_file_is_the_generated_one():
    assert _load() == json.loads(json.dumps(build(), ensure_ascii=False))


def test_key_is_marked_test_only():
    v = _load()
    assert v["test_key"]["test"] is True and "TEST KEY ONLY" in v["warning"]


@pytest.mark.parametrize("name", ["ordinary", "strong"])
def test_cards_shown_and_signatures(name):
    v = _load()
    c = next(x for x in v["cards"] if x["name"] == name)
    card = c["card"]
    P.check_card(card)
    assert P.shown(card["title"], card["level"], card["expire_at"]) == c["shown"]
    for d in ("approve", "refuse"):
        m = c[d]["message"]
        data = P.decision_bytes(m["card_id"], m["decision"], m["nonce"], m["shown"], m["ts"])
        assert data.hex() == c[d]["signed_bytes_hex"]
        assert P.verify(v["test_key"]["pubkey"], data, m["sig"])
        assert m["shown"] == c["shown"] and m["nonce"] == card["nonce"]


def test_pair_mac_and_hello():
    v = _load()
    p = v["pair"]
    assert P.mac_matches(p["code"], p["pubkey"], p["host_fp"], p["message"]["mac"])
    assert not P.mac_matches("654321", p["pubkey"], p["host_fp"], p["message"]["mac"])
    h = v["hello"]
    data = P.hello_bytes(h["challenge"]["nonce"], v["host_fp"])
    assert data.hex() == h["signed_bytes_hex"] and P.verify(v["test_key"]["pubkey"], data, h["auth"]["sig"])
    # séparation des domaines : une signature de hello ne vaut pas décision
    assert not P.verify(v["test_key"]["pubkey"], P.DECISION_DOMAIN.encode() + data[len(P.HELLO_DOMAIN):],
                        h["auth"]["sig"])


def test_negative_vectors():
    v = _load()
    neg = {n["name"]: n for n in v["negative"]}
    for k in ("title_with_0x1f", "title_not_nfc"):
        with pytest.raises(P.BadCard):
            P.check_card(neg[k]["card"])
    m = neg["wrong_signature"]["message"]
    assert not P.verify(v["test_key"]["pubkey"],
                        P.decision_bytes(m["card_id"], m["decision"], m["nonce"], m["shown"], m["ts"]), m["sig"])
    m, card = neg["wrong_shown"]["message"], neg["wrong_shown"]["card"]
    assert m["shown"] != P.shown(card["title"], card["level"], card["expire_at"])
    r = neg["hello_relayed_to_other_host"]
    assert not P.verify(v["test_key"]["pubkey"], P.hello_bytes(r["challenge"]["nonce"], r["host_fp"]), r["auth"]["sig"])
