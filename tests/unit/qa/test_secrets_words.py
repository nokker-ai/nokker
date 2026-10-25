"""Repli par entropie : mots faits seulement de lettres et identifiants juridiques dans une URL publique (rc.7, retour de
la session qui rend les données d'entraînement).

Faux positifs vus : mots composés allemands (« Speicherplatzverwaltung ») et URL Légifrance (« JURITEXT… ») ou ECLI,
caviardés en `[secret:entropy-…]`. Les jetons réels restent détectés. Aucun motif de jeton complet n'est écrit en clair
ici : tout est morcelé ou construit à l'exécution (générateur à graine fixe).
"""
from __future__ import annotations

import base64
import json
import random
import string

import pytest

from nokker.qa.secrets import redact

RNG = random.Random(20261025)


def rand(alphabet: str, n: int) -> str:
    return "".join(RNG.choice(alphabet) for _ in range(n))


ALNUM = string.ascii_letters + string.digits

# ─────────────────────────── négatifs : jamais pris pour un secret ───────────────────────────

GERMAN_WORDS = [
    "Speicherplatzverwaltung",
    "Datenschutzgrundverordnung",
    "Kraftfahrzeughaftpflichtversicherung",
    "Verfassungsbeschwerdeverfahren",
    "Bundesverfassungsgericht",
    "Gesundheitsdatenschutzverordnungsentwurf",
    "Haftpflichtversicherungsbedingungen",
    "Mehrwertsteuerabrechnungsformular",
    "Speicherplatzverwaltungsoberfläche",
    "Überweisungsbestätigungsformular",
    "Straßenverkehrsordnungswidrigkeit",
    "Rindfleischetikettierungsüberwachungsaufgabenübertragungsgesetz",
    "Arbeitsunfähigkeitsbescheinigungsformular",
    "AnticonstitutionnellementÉvidemment",
]


@pytest.mark.parametrize("word", GERMAN_WORDS)
def test_a_word_made_only_of_letters_is_not_a_secret(word):
    for text in (word, f"Die {word} gilt ab Montag.", f"Siehe {word}/Datenschutzgrundverordnung."):
        r = redact(text)
        assert r.names == [] and r.text == text, (text, r.text)


LEGAL_URLS = [
    "https://www.legifrance.gouv.fr/juri/id/JURITEXT000037999394",
    "https://www.legifrance.gouv.fr/juri/id/JURITEXT000037999394/",
    "https://www.legifrance.gouv.fr/juri/id/JURITEXT000037999394?init=true&page=1",
    "https://www.legifrance.gouv.fr/loda/id/LEGITEXT000006069577/",
    "https://www.legifrance.gouv.fr/codes/article_lc/LEGIARTI000006419292",
    "https://www.legifrance.gouv.fr/jorf/id/JORFTEXT000000886460",
    "https://www.legifrance.gouv.fr/ceta/id/CETATEXT000046783345",
    "https://www.legifrance.gouv.fr/conv_coll/id/KALICONT000005635221",
    "https://www.courdecassation.fr/decision/ECLI_FR_CCASS_2019_C100123",
    "https://juricaf.org/arret/ECLI:FR:CCASS:2019:C100123",
    "https://eur-lex.europa.eu/legal-content/FR/TXT/?uri=ecli:ECLI:EU:C:2014:317",
]


@pytest.mark.parametrize("url", LEGAL_URLS)
def test_a_known_legal_identifier_in_a_public_url_is_not_a_secret(url):
    for text in (url, f"Voir l'arrêt : {url} (consulté le 25.10.2026)."):
        r = redact(text)
        assert r.names == [] and r.text == text, (text, r.text)


# ─────────────────────────── positifs : toujours détectés ───────────────────────────


def tokens():
    header = base64.urlsafe_b64encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode()).decode().rstrip("=")
    body = base64.urlsafe_b64encode(json.dumps({"sub": "1234567890"}).encode()).decode().rstrip("=")
    sig = base64.urlsafe_b64encode(bytes(RNG.randrange(256) for _ in range(32))).decode().rstrip("=")
    return {
        "openai": "sk-" + rand(ALNUM, 40),
        "github": "gh" + "p_" + rand(ALNUM, 36),
        "aws-key-id": "AK" + "IA" + rand(string.ascii_uppercase + string.digits, 16),
        "jwt": ".".join([header, body, sig]),
        "hex-24": rand("0123456789abcdef", 24),
        "hex-48": rand("0123456789abcdef", 48),
        "base64-random": base64.b64encode(bytes(RNG.randrange(256) for _ in range(30))).decode(),
        "letters-random": rand(string.ascii_letters, 28),  # lettres seules mais tirées au hasard : casse mêlée
        "alnum-random": rand(ALNUM, 32),
    }


@pytest.mark.parametrize("kind", sorted(tokens()))
def test_real_tokens_are_still_redacted(kind):
    token = tokens()[kind]
    for text in (f"la valeur est {token} ok", f"https://api.example.com/v1/items?id=1&x={token}"):
        r = redact(text)
        assert token not in r.text and r.names, (kind, text, r.text)


def test_a_random_token_glued_to_a_legal_identifier_in_a_url_is_still_redacted():
    token = rand(ALNUM, 24)
    url = f"https://www.legifrance.gouv.fr/juri/id/JURITEXT000037999394/{token}"
    r = redact(url)
    assert token not in r.text and r.names


def test_a_fake_legal_prefix_with_a_random_tail_is_still_redacted():
    token = "JURI" + "TEXT" + rand(ALNUM, 18)
    r = redact(f"https://www.legifrance.gouv.fr/juri/id/{token}")
    assert token not in r.text and r.names


def test_a_mixed_case_letters_only_token_in_a_url_is_still_redacted():
    token = rand(string.ascii_letters, 30)
    r = redact(f"https://hooks.example.com/services/{token}")
    assert token not in r.text and r.names
