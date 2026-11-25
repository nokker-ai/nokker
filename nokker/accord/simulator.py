"""ACCORD — appareil SIMULÉ en Python (tests et démo, en attendant l'émulateur QEMU du firmware ; docs/ACCORD-DEVICE.md
§7). Joue le protocole de l'appareil : clé Ed25519, épinglage de l'empreinte TLS de l'hôte, `pair` par mac, `hello` /
`auth`, réception d'une carte, décision signée. Il ne décide jamais seul : `decide()` est appelé par le test ou la démo.

    python -m nokker.accord.simulator wss://hôte:port  --pair 123456      (appairage, la personne saisit 123456)
    python -m nokker.accord.simulator wss://hôte:port  --key k.pem --pin <host_fp> --approve|--refuse"""
from __future__ import annotations

import argparse
import json
import ssl
import sys
import time
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from nokker.accord import protocol as P


class SimulatedDevice:
    def __init__(self, private: Ed25519PrivateKey | None = None, *, pin: str | None = None) -> None:
        self.key = private or Ed25519PrivateKey.generate()
        self.pubkey = P.raw_public(self.key)
        self.key8 = P.key8(self.pubkey)
        self.pin = pin  # empreinte épinglée de l'hôte (après appairage)
        self.ws: Any = None
        self.host_fp: str | None = None
        self.device_id: str | None = None
        self.card: dict[str, Any] | None = None

    # ─── transport ───
    def connect(self, url: str, timeout: float = 5.0) -> str:
        from websockets.sync.client import connect

        ctx = ssl.create_default_context()
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE  # l'hôte est authentifié par son EMPREINTE épinglée
        self.ws = connect(url, ssl=ctx, open_timeout=timeout, max_size=4096)
        der = self.ws.socket.getpeercert(binary_form=True)
        self.host_fp = P.cert_fingerprint(der)
        if self.pin is not None and self.host_fp != self.pin:
            self.close()
            raise ConnectionError("empreinte de l'hôte différente de celle épinglée : nouvel appairage requis")
        return self.host_fp

    def close(self) -> None:
        if self.ws is not None:
            self.ws.close()
            self.ws = None

    def send(self, msg: dict[str, Any]) -> None:
        self.ws.send(json.dumps(msg, ensure_ascii=False))

    def recv(self, timeout: float = 5.0) -> dict[str, Any]:
        return json.loads(self.ws.recv(timeout=timeout))

    def recv_type(self, kinds: tuple[str, ...], timeout: float = 5.0) -> dict[str, Any]:
        end = time.monotonic() + timeout
        while True:
            m = self.recv(max(0.05, end - time.monotonic()))
            if m.get("type") in kinds or m.get("type") == "error":
                return m

    # ─── protocole ───
    def pair_message(self, code: str) -> dict[str, Any]:
        return {"type": "pair", "pubkey": self.pubkey, "mac": P.pair_mac(code, self.pubkey, self.host_fp)}

    def hello(self) -> dict[str, Any]:
        self.send({"type": "hello", "v": P.VERSION, "pubkey": self.pubkey})
        m = self.recv_type(("challenge",))
        if m.get("type") != "challenge":
            return m
        self.send({"type": "auth", "sig": P.sign(self.key, P.hello_bytes(m["nonce"], self.host_fp))})
        m = self.recv_type(("ready",))
        if m.get("type") == "ready":
            self.device_id = m["device_id"]
        return m

    def wait_card(self, timeout: float = 5.0) -> dict[str, Any]:
        m = self.recv_type(("card",), timeout)
        if m.get("type") == "card":
            try:
                P.check_card(m)
            except P.BadCard:
                self.send({"type": "error", "code": "bad_card", "card_id": m.get("card_id")})
                return {"type": "error", "code": "bad_card", "card_id": m.get("card_id")}
            self.card = m
        return m

    def decision_message(self, decision: str, card: dict[str, Any] | None = None, *, ts: int | None = None,
                         shown: str | None = None) -> dict[str, Any]:
        c = card or self.card
        sh = shown or P.shown(c["title"], c["level"], c["expire_at"])
        ts = int(time.time()) if ts is None else ts
        sig = P.sign(self.key, P.decision_bytes(c["card_id"], decision, c["nonce"], sh, ts))
        return {"type": "decision", "card_id": c["card_id"], "decision": decision, "nonce": c["nonce"],
                "shown": sh, "ts": ts, "sig": sig}

    def save_key(self, path: str) -> None:
        import os

        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(self.key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))

    @staticmethod
    def load_key(path: str) -> Ed25519PrivateKey:
        with open(path, "rb") as f:
            k = serialization.load_pem_private_key(f.read(), password=None)
        assert isinstance(k, Ed25519PrivateKey)
        return k


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Appareil ACCORD SIMULÉ (émulé) — démo et tests")
    ap.add_argument("url")
    ap.add_argument("--key", help="fichier PEM de la clé de l'appareil (créé s'il manque)")
    ap.add_argument("--pin", help="empreinte de l'hôte épinglée")
    ap.add_argument("--pair", metavar="CODE", help="demande d'appairage avec ce code à 6 chiffres")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--approve", action="store_true")
    g.add_argument("--refuse", action="store_true")
    ap.add_argument("--wait", type=float, default=60.0, help="attente d'une carte (s)")
    a = ap.parse_args(argv)
    import os

    key = SimulatedDevice.load_key(a.key) if a.key and os.path.exists(a.key) else None
    dev = SimulatedDevice(key, pin=a.pin)
    if a.key and key is None:
        dev.save_key(a.key)
    fp = dev.connect(a.url)
    print(f"[appareil ÉMULÉ] hôte {fp[:8]}…  clé {dev.key8}")
    if a.pair:
        dev.send(dev.pair_message(a.pair))
        m = dev.recv_type(("paired",), timeout=130)
        print(json.dumps(m, ensure_ascii=False))
        return 0 if m.get("type") == "paired" else 1
    m = dev.hello()
    print(json.dumps(m, ensure_ascii=False))
    if m.get("type") != "ready":
        return 1
    m = dev.wait_card(a.wait)
    print(json.dumps(m, ensure_ascii=False))
    if m.get("type") != "card" or not (a.approve or a.refuse):
        return 0 if m.get("type") == "card" else 1
    dev.send(dev.decision_message("approve" if a.approve else "refuse"))
    try:
        print(json.dumps(dev.recv(3.0), ensure_ascii=False))
    except TimeoutError:
        print('{"accepted": true}')
    dev.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
