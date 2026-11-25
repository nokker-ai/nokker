"""Vecteurs de test communs ACCORD (docs/accord-vectors.json), rejoués par le simulateur de l'hôte et par le firmware.

`python -m nokker.accord.vectors > docs/accord-vectors.json` les régénère À L'IDENTIQUE : clé de TEST Ed25519 tirée
d'une graine publiée dans le fichier (jamais une vraie clé, jamais réutilisée ailleurs), nonces et empreinte d'hôte
fixes. `tests/unit/accord/test_vectors.py` vérifie que le fichier du dépôt est bien celui que ce module produit."""
from __future__ import annotations

import hashlib
import json
import sys
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from nokker.accord import protocol as P

# Graine de la clé de TEST : publique, choisie pour ce fichier seulement. NE PROTÈGE RIEN.
TEST_SEED = hashlib.sha256(b"accord-v1 test vectors - NOT A SECRET - 2026-11-18").digest()
HOST_FP = hashlib.sha256(b"accord-v1 test host certificate (DER)").hexdigest()
OTHER_HOST_FP = hashlib.sha256(b"accord-v1 test OTHER host certificate (DER)").hexdigest()


def _nonce(label: str) -> str:
    return P.b64u(hashlib.sha256(label.encode()).digest()[:16])


def _card(card_id: str, title: str, level: str, expire_at: int, nonce_label: str) -> dict[str, Any]:
    return {"type": "card", "card_id": card_id, "title": title, "level": level, "expire_at": expire_at,
            "nonce": _nonce(nonce_label)}


def _decision(key: Ed25519PrivateKey, card: dict[str, Any], decision: str, ts: int) -> dict[str, Any]:
    sh = P.shown(card["title"], card["level"], card["expire_at"])
    data = P.decision_bytes(card["card_id"], decision, card["nonce"], sh, ts)
    return {"message": {"type": "decision", "card_id": card["card_id"], "decision": decision, "nonce": card["nonce"],
                        "shown": sh, "ts": ts, "sig": P.sign(key, data)},
            "signed_bytes_hex": data.hex()}


def build() -> dict[str, Any]:
    key = Ed25519PrivateKey.from_private_bytes(TEST_SEED)
    pub = P.raw_public(key)
    ordinary = _card("ap_test_ordinary_1", "Écrire notes.md dans projets", "ordinary", 1763500000, "n-ordinary")
    strong = _card("ap_test_strong_1", "Envoyer un e-mail à 1 destinataire", "strong", 1763500300, "n-strong")
    hello_nonce = _nonce("n-hello")
    hello_data = P.hello_bytes(hello_nonce, HOST_FP)
    good = _decision(key, ordinary, "approve", 1763499950)
    wrong_sig = json.loads(json.dumps(good["message"]))
    wrong_sig["sig"] = P.sign(key, P.decision_bytes(ordinary["card_id"], "refuse", ordinary["nonce"],
                                                    wrong_sig["shown"], 1763499950))
    wrong_shown = _decision(key, ordinary, "approve", 1763499951)
    wrong_shown["message"]["shown"] = P.shown("Écrire autre.md dans projets", "ordinary", ordinary["expire_at"])
    wrong_shown["message"]["sig"] = P.sign(key, P.decision_bytes(
        ordinary["card_id"], "approve", ordinary["nonce"], wrong_shown["message"]["shown"], 1763499951))
    return {
        "contract": "docs/ACCORD-DEVICE.md v1.1",
        "warning": "TEST KEY ONLY — public seed, generated for these vectors, never used by a real device or host",
        "test_key": {"seed_hex": TEST_SEED.hex(), "pubkey": pub, "key8": P.key8(pub), "test": True},
        "host_fp": HOST_FP,
        "separator_hex": "1f",
        "pair": {"code": "123456", "pubkey": pub, "host_fp": HOST_FP,
                 "message": {"type": "pair", "pubkey": pub, "mac": P.pair_mac("123456", pub, HOST_FP)},
                 "mac_input_hex": (pub.encode() + P.SEP + HOST_FP.encode()).hex()},
        "hello": {"message": {"type": "hello", "v": P.VERSION, "pubkey": pub},
                  "challenge": {"type": "challenge", "nonce": hello_nonce},
                  "auth": {"type": "auth", "sig": P.sign(key, hello_data)}, "signed_bytes_hex": hello_data.hex()},
        "cards": [
            {"name": "ordinary", "card": ordinary,
             "shown": P.shown(ordinary["title"], ordinary["level"], ordinary["expire_at"]),
             "approve": _decision(key, ordinary, "approve", 1763499950),
             "refuse": _decision(key, ordinary, "refuse", 1763499960),
             "host_accepts": ["approve", "refuse"]},
            {"name": "strong", "card": strong,
             "shown": P.shown(strong["title"], strong["level"], strong["expire_at"]),
             "approve": _decision(key, strong, "approve", 1763500100),
             "refuse": _decision(key, strong, "refuse", 1763500110),
             "host_accepts": ["refuse"], "host_rejects": {"approve": "strong_card"}},
        ],
        "negative": [
            {"name": "title_with_0x1f", "expect": "bad_card",
             "card": {**ordinary, "card_id": "ap_test_bad_title", "title": "Écrire\u001fnotes.md"}},
            {"name": "title_not_nfc", "expect": "bad_card",
             "card": {**ordinary, "card_id": "ap_test_nfd", "title": "Écrire notes.md"}},
            {"name": "wrong_signature", "expect": "bad_sig", "card": ordinary, "message": wrong_sig},
            {"name": "wrong_shown", "expect": "bad_shown", "card": ordinary, "message": wrong_shown["message"]},
            {"name": "hello_relayed_to_other_host", "expect": "bad_sig", "host_fp": OTHER_HOST_FP,
             "challenge": {"type": "challenge", "nonce": hello_nonce},
             "auth": {"type": "auth", "sig": P.sign(key, hello_data)}},
        ],
    }


def main() -> None:
    json.dump(build(), sys.stdout, ensure_ascii=False, indent=1)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
