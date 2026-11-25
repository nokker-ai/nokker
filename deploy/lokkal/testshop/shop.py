"""Marchand de TEST de l'agent de paiement own_card (docs/PAY-AGENT.md §11.4) — aucun prestataire de paiement, aucun
paiement réel, aucune carte gardée ni journalisée. Bibliothèque standard seulement.

    python shop.py --port 8099            # http://<hôte>:8099/

Parcours : `/` (produits) → `/cart?sku=…` (panier, lien à donner à pay_request) → `/checkout?sku=…` (formulaire de
carte : autocomplete cc-number, cc-exp, cc-csc, cc-name ; total affiché « Total CHF 42.90 », `data-total-minor`) →
`POST /pay` →
- numéro 4242 4242 4242 4242 : accepté sans défi ;
- 4000 0027 6000 3184 : défi 3-D Secure par CODE dans une iframe (`/acs`, champ autocomplete one-time-code ; code de
  test 123456 ; tout autre code : refus) ;
- 4000 0082 6000 3178 : confirmation « dans l'appli de la banque » (iframe d'attente, acceptée après 6 s) ;
- 4000 0000 0000 0002 : refusé ;
- tout autre numéro : refusé (« carte de test inconnue »).
Puis `/success` (« Merci, paiement accepté ») ou `/failed` (« Paiement refusé »). Les transactions ne vivent qu'en
mémoire, sans numéro de carte (seulement l'issue).
"""
from __future__ import annotations

import argparse
import html
import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

PRODUCTS = {"kettle": ("Bouilloire inox", 4290), "mug": ("Tasse émaillée", 1250)}
CARDS = {"4242424242424242": "ok", "4000002760003184": "otp", "4000008260003178": "app", "4000000000000002": "declined"}
OTP_CODE = "123456"
TX: dict[str, dict] = {}
LOCK = threading.Lock()

STYLE = ("body{font-family:system-ui,sans-serif;max-width:560px;margin:24px auto;padding:0 16px}"
         "label{display:block;margin:8px 0}input{font:inherit;padding:6px;width:100%;box-sizing:border-box}"
         "button{font:inherit;padding:8px 16px;margin-top:12px}.total{font-size:1.3em;font-weight:700}"
         "iframe{width:100%;height:260px;border:1px solid #888}")


def page(title: str, body: str) -> bytes:
    return (f"<!doctype html><html lang='fr'><head><meta charset='utf-8'><meta name='viewport' "
            f"content='width=device-width,initial-scale=1'><title>{html.escape(title)}</title><style>{STYLE}</style>"
            f"</head><body><h1>Boutique de test</h1>{body}</body></html>").encode()


def fmt(minor: int) -> str:
    return f"{minor // 100}.{minor % 100:02d}"


class Shop(BaseHTTPRequestHandler):
    server_version = "nokker-testshop"

    def log_message(self, fmt_: str, *args) -> None:  # rien de la requête n'est journalisé (ni corps ni requête)
        return

    def _send(self, status: int, body: bytes, ctype: str = "text/html; charset=utf-8", extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, to: str) -> None:
        self._send(303, b"", extra={"Location": to})

    def do_GET(self) -> None:  # noqa: N802
        u = urlsplit(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        sku = q.get("sku", "kettle")
        name, price = PRODUCTS.get(sku, PRODUCTS["kettle"])
        if u.path == "/":
            items = "".join(f"<li><a href='/cart?sku={k}'>{html.escape(n)}</a> — CHF {fmt(p)}</li>"
                            for k, (n, p) in PRODUCTS.items())
            return self._send(200, page("Produits", f"<ul>{items}</ul>"))
        if u.path == "/cart":
            return self._send(200, page("Panier", f"<p>{html.escape(name)} × 1</p><p class='total' "
                                                  f"data-total-minor='{price}' data-currency='CHF'>Total CHF {fmt(price)}"
                                                  f"</p><p><a href='/checkout?sku={sku}'>Passer au paiement</a></p>"))
        if u.path == "/checkout":
            form = (f"<form method='post' action='/pay?sku={sku}'>"
                    "<label>Numéro de carte<input name='cardnumber' autocomplete='cc-number' inputmode='numeric'></label>"
                    "<label>Expiration (MM/AA)<input name='exp' autocomplete='cc-exp' placeholder='MM/AA'></label>"
                    "<label>Code de sécurité<input name='cvc' autocomplete='cc-csc' inputmode='numeric'></label>"
                    "<label>Titulaire<input name='ccname' autocomplete='cc-name'></label>"
                    f"<p class='total' data-total-minor='{price}' data-currency='CHF'>Total CHF {fmt(price)}</p>"
                    "<button type='submit'>Payer</button></form>")
            return self._send(200, page("Paiement", form))
        if u.path == "/3ds":
            tx = q.get("tx", "")
            return self._send(200, page("Vérification", f"<p>Vérification de votre banque…</p>"
                                        f"<iframe name='acs-challenge' src='/acs?tx={html.escape(tx)}'></iframe>"
                                        f"<script>setInterval(async()=>{{const r=await fetch('/status?tx="
                                        f"{html.escape(tx)}');const s=(await r.json()).state;"
                                        "if(s==='ok')location='/success';if(s==='declined')location='/failed';},700)"
                                        "</script>"))
        if u.path == "/acs":
            tx = q.get("tx", "")
            with LOCK:
                kind = (TX.get(tx) or {}).get("kind")
            if kind == "otp":
                return self._send(200, page("Code", f"<form method='post' action='/acs?tx={html.escape(tx)}'>"
                                            "<label>Code reçu par SMS<input name='otp' autocomplete='one-time-code' "
                                            "inputmode='numeric'></label><button type='submit'>Valider</button></form>"))
            return self._send(200, page("Appli", "<p>Confirmez ce paiement dans l'application de votre banque.</p>"))
        if u.path == "/status":
            with LOCK:
                t = TX.get(q.get("tx", "")) or {}
                if t.get("kind") == "app" and t.get("state") == "pending" and time.time() - t["at"] > 6:
                    t["state"] = "ok"
                state = t.get("state", "unknown")
            return self._send(200, json.dumps({"state": state}).encode(), "application/json")
        if u.path == "/success":
            return self._send(200, page("Merci", "<p class='done'>Merci, paiement accepté. Commande confirmée.</p>"))
        if u.path == "/failed":
            return self._send(200, page("Refus", "<p class='fail'>Paiement refusé.</p>"))
        self._send(404, page("Introuvable", "<p>Introuvable</p>"))

    def _form(self) -> dict[str, str]:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(min(n, 4096)).decode(errors="replace")
        return {k: v[0] for k, v in parse_qs(raw).items()}

    def do_POST(self) -> None:  # noqa: N802
        u = urlsplit(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        f = self._form()
        if u.path == "/pay":
            pan = "".join(ch for ch in f.get("cardnumber", "") if ch.isdigit())
            ok_fields = len(f.get("cvc", "")) in (3, 4) and "/" in f.get("exp", "") and f.get("ccname", "").strip()
            kind = CARDS.get(pan, "unknown") if ok_fields else "declined"
            f.clear()
            pan = ""  # rien de la carte ne reste
            if kind == "ok":
                return self._redirect("/success")
            if kind in ("otp", "app"):
                tx = secrets.token_hex(8)
                with LOCK:
                    TX[tx] = {"kind": kind, "state": "pending", "at": time.time()}
                return self._redirect(f"/3ds?tx={tx}")
            return self._redirect("/failed")
        if u.path == "/acs":
            tx = q.get("tx", "")
            with LOCK:
                t = TX.get(tx)
                if t is not None and t["kind"] == "otp" and t["state"] == "pending":
                    t["state"] = "ok" if f.get("otp", "").strip() == OTP_CODE else "declined"
            return self._send(200, page("Code", "<p>Merci.</p>"))
        self._send(404, page("Introuvable", "<p>Introuvable</p>"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8099)
    a = ap.parse_args()
    ThreadingHTTPServer((a.host, a.port), Shop).serve_forever()


if __name__ == "__main__":
    main()
