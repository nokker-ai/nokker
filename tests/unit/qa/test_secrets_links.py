"""Liens ordinaires et liens à jeton en mode strict (filet avant le modèle, `weak=False`) et en mode complet.

Revue du train du 2.11, défaut 5 (arbitrage dev, mesuré contre `main`) : une URL avec un port, et un lien de prise de
rendez-vous sans paramètre de jeton, ne sont PAS des secrets (comportement de `main`) ; un paramètre de requête qui
porte un jeton (`?token=`, `&key=`, `access_token=`, `sig=`…) reste caviardé : c'est un identifiant, souvent un lien
magique. Jetons morcelés et factices."""
from __future__ import annotations

import pytest

from nokker.qa.secrets import HANDLE_RE, redact


def j(*parts: str) -> str:
    return "".join(parts)


TOK = j("8f3a9c1d", "2e7b4a6f", "0e5d3c2b", "1a09")  # 28 car. lettres et chiffres
SIG = j(TOK, TOK)
PASSWORD = j("S3cr", "etPassw0rd", "99")

PLAIN = [
    "https://shop.exemple.ch:443?email=jean.dupont@exemple.ch",  # port puis paramètre : pas un mot de passe
    "https://shop.exemple.ch:8443/panier?email=jean.dupont@exemple.ch",
    "http://localhost:8080/api?user=a@b.ch",
    "https://intranet.exemple.ch:9443#contact=marie@exemple.ch",
    "https://calendly.com/alice-acme/30min",
    "https://calendly.com/d/ck2b-7xq-9mz/rendez-vous-30-min?month=2026-10",
    "https://calendly.com/alice-martin/30min?back=1&month=2026-10&date=2026-10-27",
    "https://cal.com/alice/30min?date=2026-10-27&layout=month_view",
    "https://zcal.co/alice/30min?utm_source=mail&utm_campaign=rdv",
]

TOKENED = [
    (f"https://exemple.ch/confirm?token={TOK}", TOK),
    (f"https://calendly.com/events/x/invitees?invitee_token={TOK}", TOK),
    (f"https://x.ch/a?key={TOK}&b=1", TOK),
    (f"https://x.ch/a?b=1&key={TOK}", TOK),
    (f"https://x.ch/oauth?access_token={TOK}", TOK),
    (f"https://img.exemple.ch/a.png?sv=2024&sig={SIG}", SIG),
    (f"https://x.ch/dl?signature={TOK}&expires=1792000000", TOK),
    (f"postgres://nokker:{PASSWORD}@db.local:5432/nokker", PASSWORD),
    (f"https://admin:{PASSWORD}@shop.exemple.ch:8443/panier", PASSWORD),
]


@pytest.mark.parametrize("weak", [False, True])
@pytest.mark.parametrize("text", PLAIN)
def test_plain_links_with_a_port_or_a_booking_link_stay(text, weak):
    assert redact(text, None, weak=weak).text == text


@pytest.mark.parametrize("weak", [False, True])
@pytest.mark.parametrize("text,value", TOKENED)
def test_links_that_carry_a_token_are_still_redacted(text, value, weak):
    out = redact(text, None, weak=weak).text
    assert value not in out and HANDLE_RE.search(out), out


def test_short_or_wordy_query_values_are_not_tokens():
    for text in ("https://x.ch/a?key=prix&sort=asc", "https://x.ch/a?key=2026-10-27",
                 "https://x.ch/carte?key=compute_token_v2", "https://x.ch/a?sig=abc123"):
        assert redact(text, None, weak=False).text == text, text
