"""Familles de secrets courantes et chemins vers le contexte du modèle (lot fix-batch-1, défaut 1).

Carnet D4 et banc D1 (bench/lokkal/JUGE-D1.md) : le Router refusait le tour (`400 secret_detected`) parce qu'un texte
envoyé au modèle ressemblait encore à un secret. Deux trous : des familles que le Router bloque et que Nokker ne
caviardait pas (GitLab, Stripe, URL à identifiants, `Bearer`, ligne `.env`…), et la poignée elle-même :
`[secret:aws-access-key-ce6d24]` se lit `secret: <valeur de 21 car. avec chiffres>` pour un détecteur clé/valeur.

Tous les jetons sont MORCELÉS (jamais un jeton complet en clair dans le dépôt) et factices.
"""
from __future__ import annotations

import json
import re

import pytest

from nokker.qa.secrets import HANDLE_RE, KnownSecrets, redact, redact_messages


def j(*parts: str) -> str:
    return "".join(parts)


AKIA = j("AKIA", "Z7Q3VNXW", "5LPR2K4M")
ASIA = j("ASIA", "QW4E7R2T", "9Y1U3I5O")
AWS_SECRET = j("wJal8rXUtn3F", "EMI9K7MDENGb", "PxRfiCYz4kQm2vT6")
GHP = j("ghp", "_", "a1B2c3D4e5F6g7H8i9J0", "k1L2m3N4o5P6q7R8")
GHO = j("gho", "_", "Z9y8X7w6V5u4T3s2R1q0", "p9O8n7M6l5K4j3I2")
GH_PAT = j("github", "_pat_", "11ABCDEFG0", "123456789_", "abcdefghijKLMNOPQRSTuvwxyz0123456789ABCDEFGHIJKLMNOPQRSTUV")
GLPAT = j("glpat", "-", "xY12zW34vU56", "tS78rQ90")
SLACK_B = j("xoxb", "-", "123456789012", "-", "abcdefABCDEF1234")
SLACK_P = j("xoxp", "-", "998877665544", "-", "QWErty123456")
STRIPE_SK = j("sk", "_live_", "51HxYzAbCdEf", "GhIjKlMnOpQr12")
STRIPE_RK = j("rk", "_live_", "51HqWeRtYuIo", "PaSdFgHjKl3456")
OPENAI = j("sk", "-proj-", "Ab3dEf5hIj7lMn", "9pQr1tUv3xYz5")
ANTHROPIC = j("sk", "-ant-", "api03-", "Xy9Zw8Vu7Ts6Rq5Po4Nm3")
GOOGLE = j("AIza", "SyA1b2C3d4E5f6G7h8", "I9j0K1l2M3n4O5p6Q")
JWT = j("eyJ", "hbGciOiJIUzI1NiJ9", ".", "eyJ", "zdWIiOiIxMjM0NSJ9", ".", "c2lnbmF0dXJlXzEyMzQ1Ng")
AZURE_ACCOUNT_KEY = j("Eby8vdM02xNOcqFl", "qUwJPLlmEtlCDXJ1", "OUzFT50uSRZ6IFsu", "Fq2UVErCz4I6tq/K",
                      "1SZFPTOtr/KBHBeksoGMGw", "==")
AZURE_CLIENT_SECRET = j("abc", "8Q~", "Xy7zW6vU5tS4rQ3pO2nM1lK0jI9hG8fE")
HF = j("hf", "_", "AbCdEfGhIjKlMnOpQrStUvWxYz012345")
PEM_BODY = j("MIIEvQIBADANBgkqhkiG9w0BAQEFAASC", "BKcwggSjAgEAAoIBAQC7")
PEM = j("-----BEGIN ", "PRIVATE KEY-----\n", PEM_BODY, "\n-----END ", "PRIVATE KEY-----")
URL_PASS = j("S3cr", "etP4ss")

# Ce que le Router lit comme « mot-clé : valeur » (infer/scrub.py, `_KeyValue`) : une valeur de 16 caractères ou plus,
# avec une lettre et un chiffre, juste après `secret`/`token`/`api_key`/`password` et `:` ou `=`.
ROUTER_KEY_VALUE = re.compile(
    r"(?i)(?<![A-Za-z0-9_])(?:api[_-]?key|secret|token|passwd|password)['\"]?\s*[:=]\s*['\"]?"
    r"(?=[A-Za-z0-9_\-]*\d)(?=[A-Za-z0-9_\-]*[A-Za-z])[A-Za-z0-9_\-]{16,}")


FAMILIES = [
    ("aws-access-key", f"clé : {AKIA} fin", AKIA),
    ("aws-access-key", f"identifiant temporaire {ASIA}.", ASIA),
    ("aws-secret-key", f"aws_secret_access_key = {AWS_SECRET}", AWS_SECRET),
    ("aws-secret-key", json.dumps({"AccessKeyId": AKIA, "SecretAccessKey": AWS_SECRET}), AWS_SECRET),
    ("github-token", f"jeton {GHP} pour la CI", GHP),
    ("github-token", f"oauth {GHO}", GHO),
    ("github-token", f"token fin {GH_PAT}", GH_PAT),
    ("gitlab-token", f"GitLab : {GLPAT}", GLPAT),
    ("slack-token", f"bot {SLACK_B}", SLACK_B),
    ("slack-token", f"user {SLACK_P}", SLACK_P),
    ("stripe-key", f"Stripe {STRIPE_SK}", STRIPE_SK),
    ("stripe-key", f"restreinte {STRIPE_RK}", STRIPE_RK),
    ("api-key", f"OpenAI {OPENAI}", OPENAI),
    ("anthropic-key", f"Anthropic {ANTHROPIC}", ANTHROPIC),
    ("google-api-key", f"Maps {GOOGLE}", GOOGLE),
    ("jwt", f"Authorization {JWT}", JWT),
    ("azure-key", f"DefaultEndpointsProtocol=https;AccountName=demo;AccountKey={AZURE_ACCOUNT_KEY};"
                  "EndpointSuffix=core.windows.net", AZURE_ACCOUNT_KEY),
    ("azure-secret", f"client secret {AZURE_CLIENT_SECRET}", AZURE_CLIENT_SECRET),
    ("hf-token", f"Hugging Face {HF}", HF),
    ("pem", f"clé :\n{PEM}\n", PEM_BODY),
    ("url-password", f"DATABASE_URL=postgres://app:{URL_PASS}@db.internal:5432/app", URL_PASS),
]


@pytest.mark.parametrize("weak", [True, False], ids=["texte-humain", "resultat-strict"])
@pytest.mark.parametrize("kind,text,value", FAMILIES, ids=[f"{k}-{i}" for i, (k, _, _) in enumerate(FAMILIES)])
def test_common_secret_families_are_redacted(kind, text, value, weak):
    r = redact(text, weak=weak)
    assert value not in r.text, (kind, r.text)
    assert any(n.startswith(kind + "-") for n in r.names), (kind, r.names)


def test_gcp_service_account_json_private_key_is_redacted():
    """Clé de service GCP : le PEM est dans une chaîne JSON, retours à la ligne échappés (`\\n`)."""
    sa = json.dumps({"type": "service_account", "project_id": "demo-1234", "private_key": PEM + "\n",
                     "client_email": "bot@demo-1234.iam.gserviceaccount.com"})
    r = redact(sa, weak=False)
    assert PEM_BODY not in r.text and "BEGIN PRIVATE KEY" not in r.text
    assert json.loads(r.text)["client_email"].startswith("bot@")  # le JSON reste lisible


def test_truncated_pem_without_end_marker_is_redacted():
    """Un résultat coupé (lecture partielle d'un fichier) : l'en-tête suffit, le corps qui suit part avec."""
    r = redact(j("-----BEGIN OPENSSH ", "PRIVATE KEY-----\n", PEM_BODY, "\n"), weak=False)
    assert PEM_BODY not in r.text


def test_bearer_header_env_line_and_key_value_are_redacted():
    tok = j("a8Fk2LqZ9x", "Pw3RtY7uVb")
    for text, value in ((f"curl -H 'Authorization: Bearer {tok}' https://api.example", tok),
                        (f"export STRIPE_API_KEY={tok}\n", tok),
                        (f"DB_PASSWORD={j('p4ss', 'w0rd!x')}\n", j("p4ss", "w0rd!x")),
                        (f'{{"api_key": "{tok}"}}', tok),
                        (f"token={tok}&page=2", tok)):
        r = redact(text, weak=False)
        assert value not in r.text, text


def test_url_credentials_placeholders_and_plain_urls_stay():
    for text in ("postgres://user:password@localhost/db", "redis://:${REDIS_PASSWORD}@cache:6379",
                 "https://example.com/a/b?x=1", "git@github.com:org/repo.git", "http://admin:admin@127.0.0.1"):
        assert redact(text, weak=False).text == text, text


@pytest.mark.parametrize("kind,text,value", FAMILIES, ids=[f"{k}-{i}" for i, (k, _, _) in enumerate(FAMILIES)])
def test_handle_never_reads_as_secret_key_value_to_the_router(kind, text, value):
    """Le défaut D4 réel : `[secret:aws-access-key-ce6d24]` dans un message `user` était refusé par le Router
    (`secret: <valeur>`). Une poignée ne doit jamais avoir la forme d'un secret."""
    out = redact(text, weak=False).text
    assert HANDLE_RE.search(out)
    assert not ROUTER_KEY_VALUE.search(out), out


def test_no_false_positive_on_paths_hashes_and_code():
    sha = j("3f9a1c2b4d5e6f70", "8192a3b4c5d6e7f8", "091a2b3c")
    plain = (f"git show {sha}", f"le commit {sha[:12]} corrige le token de session", f"token {sha}",
             "source /opt/miniconda3/bin/activate testbed", "/usr/lib/python3/dist-packages/apport",
             "max_tokens: 4096", "TOKEN_ENDPOINT=https://auth.example/token", "KEYCLOAK_URL=http://kc:8080",
             "Bearer token required", "MAX_TOKENS=4096", "export AWS_PROFILE=prod2")
    code = ("token = compute_token_v2(user)", "secret_key = settings.SECRET_KEY", "API_KEY=${API_KEY}",
            "api_key: your-api-key-here", 'token: "${{ secrets.GITHUB_TOKEN }}"', "password = os.environ['PW']")
    for text in plain:
        assert redact(text, weak=False).text == text, text
        assert redact(text).text == text, text
    for text in code:  # résultats d'outils (mode strict) : le code lu par une session reste tel quel
        assert redact(text, weak=False).text == text, text


def test_redact_messages_covers_every_role_and_tool_arguments():
    """Filet du fil (noyau et juge) : tout le texte qui part, y compris les arguments d'outils et les parties."""
    args = json.dumps({"path": "tests/fixtures.py", "content": f'KEY_ID = "{AKIA}"\n'})
    msgs = [{"role": "system", "content": f"[Contexte du plan]\nclé {AKIA}\n[/Contexte du plan]"},
            {"role": "user", "content": [{"type": "text", "text": f"voir {GLPAT}"},
                                         {"type": "nokker_image", "sha256": "ab" * 32, "name": "capture.png"}]},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "write_file", "arguments": args}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "ok"}]
    out = redact_messages(msgs)
    blob = json.dumps(out, ensure_ascii=False)
    assert AKIA not in blob and GLPAT not in blob
    assert json.loads(out[2]["tool_calls"][0]["function"]["arguments"])["path"] == "tests/fixtures.py"
    assert out[1]["content"][1] == msgs[1]["content"][1]  # partie image intacte
    assert AKIA in msgs[0]["content"]  # l'original n'est pas modifié (le tour garde ses messages)


def test_redact_messages_uses_known_values():
    ks = KnownSecrets()
    ks.add("db_pass", "Xk9!qpzT42Lm")
    out = redact_messages([{"role": "system", "content": "mot de passe de la base : Xk9!qpzT42Lm"}], ks)
    assert "Xk9!qpzT42Lm" not in out[0]["content"] and "[secret:db_pass]" in out[0]["content"]


def test_handles_are_never_redacted_again_and_legacy_typed_handles_are_rewritten():
    """Idempotent sur ses poignées ; une poignée typée d'avant le correctif (hash en chiffres, déjà rangée dans
    l'historique d'un fil) est réécrite en lettres, sinon le fil reste refusé à chaque appel."""
    out = redact(f"clé {AKIA}", weak=False).text
    assert redact(out, weak=False).text == out and redact(out).text == out
    legacy = "clé : [secret:aws-access-key-" + "ce6d24] et [secret:db_password_2026]"
    got = redact(legacy, weak=False).text
    assert got == "clé : [secret:aws-access-key-cemdik] et [secret:db_password_2026]"
    assert not ROUTER_KEY_VALUE.search(got.split(" et ")[0])


# ─────────── repli par entropie : identifiants de code (banc X9 §5.3) ───────────

CODE_IDENTIFIERS = ["test_remise_10_pourcent_sur_3150", "test_remise_30_pourcent_acceptee",
                    "test_remise_31_pourcent_refusee", "test_arrondi_5_centimes_sur_total_ttc",
                    "parse_iso8601_date_v2_utc", "_total_ttc_2026_par_ligne", "testRemise10PourcentSur3150",
                    "computeTotalTtc2026ParLigne"]


def test_code_identifiers_with_numbers_are_not_secrets():
    """Banc X9 (tour 70) : des noms de tests remplacés par `[secret:entropy-…]` dans la réponse puis dans la mémoire."""
    for name in CODE_IDENTIFIERS:
        for text in (f"Les tests {name} et test_export_csv passent.", f"def {name}(self):", name):
            assert redact(text).text == text, text


def test_real_tokens_shaped_like_identifiers_are_still_redacted():
    tokens = [j("k9x", "_qz7w", "_4mbt", "_p2ll8", "_x0rf"),  # minuscules et chiffres tirés au hasard
              j("api_key_", "9f8a7b6c5d4e", "3f2a1b0c"),  # un morceau aléatoire dans un nom
              j("token_", "a8Fk2LqZ9x", "Pw3RtY"),
              j("Acme", "_Backup_2026", "_NAS"),  # mots + année : la forme d'un mot de passe (revue du 20.10)
              j("aB3dE9xYz", "Qw7Rt2Uv8", "Kp4Ln6Mh"),
              j("xK9mQ2", "vL8nR3", "tP7wZ4", "sJ6")]
    for tok in tokens:
        assert tok not in redact(f"valeur {tok} fin").text, tok
