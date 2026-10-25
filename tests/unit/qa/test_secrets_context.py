"""Faux positifs du caviardage élargi qui dépendent du CONTEXTE (revue du train du 2.11, §0 décision 3) : `token: <hash
git>` et une clé `AIza…`.

Arbitrage dev (fix-batch-3) :
- un hash git (40 hex) après `token:` reste en clair quand le contexte est git (commit, sha, rev, HEAD, merge…) et
  qu'aucun mot d'identifiant ne l'entoure ; sans contexte git, il reste caviardé (un vieux jeton OAuth GitHub, beaucoup
  de jetons d'API, font aussi 40 hex) ; `access_token:`, `auth_token:`… restent caviardés quel que soit le contexte ;
- une clé `AIza…` est une clé d'API Google, qui PEUT être secrète (Maps, Gemini, YouTube côté serveur : facturée au
  propriétaire) : caviardée, SAUF dans une configuration web Firebase sans ambiguïté (`apiKey` d'un objet qui porte
  `authDomain: "<projet>.firebaseapp.com"` ou `appId: "1:<n>:web:<hex>"`, et rien de côté serveur à côté) ;
- le filet du fil (ce qui part au modèle, `redact_messages`) caviarde les deux quoi qu'il arrive : le Router refuse le
  tour entier sur ces formes (`400 secret_detected` : `AIza`, et `token: <16 car. lettres et chiffres>`).
Valeurs morcelées et factices."""
from __future__ import annotations

import pytest

from nokker.qa.secrets import HANDLE_RE, KnownSecrets, redact, redact_messages


def j(*parts: str) -> str:
    return "".join(parts)


SHA = j("9fceb02d", "0ae598e9", "5dc970b7", "4767f193", "72d61af8")  # 40 hex, factice
KEY = j("AIza", "SyD3mO-k9Xq2", "Lr7Tf4Wb8Nc1", "Vh6Jp0Gs5Ex")  # forme d'une clé Google, factice

FIREBASE_JS = f"""const firebaseConfig = {{
  apiKey: "{KEY}",
  authDomain: "demo-app.firebaseapp.com",
  projectId: "demo-app",
  storageBucket: "demo-app.appspot.com",
  appId: "1:123456789012:web:0a1b2c3d4e5f6a7b",
}};
initializeApp(firebaseConfig);"""
FIREBASE_JSON = (f'{{"apiKey": "{KEY}", "authDomain": "demo-app.firebaseapp.com", "projectId": "demo-app", '
                 '"appId": "1:123456789012:web:0a1b2c3d4e5f6a7b"}')

GIT_KEPT = [
    f"git rev-parse HEAD donne le commit de référence, token: {SHA}",
    f"commit {SHA[:7]} (merge de la branche) — reprise au token: {SHA}",
    f"Pagination de l'API des commits : sha du parent, next token: {SHA}",
]
GIT_REDACTED = [
    f"token: {SHA}",  # aucun contexte git : peut être un jeton d'API de 40 hex
    f"Voici le token: {SHA} pour l'API",
    f"commit {SHA[:7]} — access_token: {SHA}",  # mot d'identifiant : caviardé quel que soit le contexte
    f"git push avec le jeton : auth_token={SHA} (commit {SHA[:7]})",
    f"commit {SHA[:7]}, token: {SHA}, Authorization: à mettre dans l'en-tête",  # identifiant à côté : doute
]
KEY_REDACTED = [
    f"Maps {KEY}",
    f'apiKey: "{KEY}"',  # pas de marque Firebase : clé Google quelconque
    f"GOOGLE_API_KEY={KEY}",
    f"https://maps.googleapis.com/maps/api/js?key={KEY}&callback=init",
    f'const config = {{ apiKey: "{KEY}", authDomain: "exemple.ch" }}',  # pas un domaine firebaseapp.com
    FIREBASE_JS.replace(f'apiKey: "{KEY}"', f'serverKey: "{KEY}"'),  # pas l'apiKey du client
    FIREBASE_JSON[:-1] + ', "private_key": "…", "client_email": "x@demo-app.iam.gserviceaccount.com"}',  # serveur
]


@pytest.mark.parametrize("weak", [True, False])
@pytest.mark.parametrize("text", GIT_KEPT)
def test_git_hash_after_token_stays_in_a_git_context(text, weak):
    assert redact(text, None, weak=weak).text == text


@pytest.mark.parametrize("weak", [True, False])
@pytest.mark.parametrize("text", GIT_REDACTED)
def test_git_like_hash_is_still_redacted_without_an_unambiguous_git_context(text, weak):
    out = redact(text, None, weak=weak).text
    assert SHA not in out and HANDLE_RE.search(out), out


@pytest.mark.parametrize("weak", [True, False])
@pytest.mark.parametrize("text", [FIREBASE_JS, FIREBASE_JSON])
def test_firebase_web_config_key_stays(text, weak):
    assert redact(text, None, weak=weak).text == text


@pytest.mark.parametrize("weak", [True, False])
@pytest.mark.parametrize("text", KEY_REDACTED)
def test_google_key_is_redacted_when_in_doubt(text, weak):
    out = redact(text, None, weak=weak).text
    assert KEY not in out and HANDLE_RE.search(out), out


def test_a_known_secret_wins_over_the_public_context():
    known = KnownSecrets()
    known.add("FIREBASE_KEY", KEY)
    assert KEY not in redact(FIREBASE_JS, known).text


@pytest.mark.parametrize("text", [FIREBASE_JS, GIT_KEPT[0]])
def test_the_wire_still_redacts_both_because_the_router_refuses_them(text):
    [msg] = redact_messages([{"role": "user", "content": text}])
    assert KEY not in msg["content"] and SHA not in msg["content"] and HANDLE_RE.search(msg["content"])


def test_a_text_that_already_carries_the_internal_mark_gets_no_exception():
    crafted = "\x00\x015\x01\x00 " + FIREBASE_JS
    out = redact(crafted, None).text
    assert KEY not in out and out.startswith("\x00\x015\x01\x00")  # pas d'IndexError, rien de mis de côté
