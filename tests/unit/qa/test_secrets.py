"""Tests du détecteur/caviardeur de secrets (F3, nokker/qa/secrets.py).

Valeurs factices uniquement — aucun vrai secret (consigne F3).
"""
from __future__ import annotations

import base64

from nokker.qa.secrets import HANDLE_RE, KnownSecrets, redact


FAKE_PASSWORD = "Xk9!qpzT42Lm"  # même forme que le stub du banc credentiels-pas-de-fuite


def known_with_password() -> KnownSecrets:
    ks = KnownSecrets()
    ks.add("VAULT_GRAFANA", FAKE_PASSWORD)
    return ks


# ───────────────────────── coffre de valeurs connues ─────────────────────────


def test_known_value_redacted_with_stable_name():
    ks = known_with_password()
    r = redact(f"user admin / mot de passe {FAKE_PASSWORD}", ks)
    assert FAKE_PASSWORD not in r.text
    assert r.names == ["VAULT_GRAFANA"]
    assert "[secret:VAULT_GRAFANA]" in r.text


def test_known_value_redaction_is_deterministic_handle_name():
    ks = known_with_password()
    r1 = redact(f"a={FAKE_PASSWORD}", ks)
    r2 = redact(f"b: {FAKE_PASSWORD}", ks)
    assert r1.names == r2.names == ["VAULT_GRAFANA"]


def test_longer_known_value_wins_over_substring():
    ks = KnownSecrets()
    ks.add("SHORT", "ab12")
    ks.add("LONG", "ab12cd34")
    r = redact("valeur ab12cd34 ici", ks)
    assert r.names == ["LONG"]
    assert "ab12cd34" not in r.text and "[secret:ab12" not in r.text


def test_known_value_base64_variant_caught():
    ks = known_with_password()
    b64 = base64.b64encode(FAKE_PASSWORD.encode()).decode()
    r = redact(f"voici en base64 : {b64}", ks)
    assert b64 not in r.text
    assert r.names == ["VAULT_GRAFANA"]


def test_known_value_urlsafe_base64_no_padding_variant_caught():
    ks = known_with_password()
    b64 = base64.urlsafe_b64encode(FAKE_PASSWORD.encode()).decode().rstrip("=")
    r = redact(f"param={b64}", ks)
    assert b64 not in r.text
    assert r.names == ["VAULT_GRAFANA"]


def test_known_value_spaced_variant_caught():
    ks = known_with_password()
    spaced = " ".join(FAKE_PASSWORD)
    r = redact(f"voici espacé : {spaced}", ks)
    assert spaced not in r.text
    assert r.names == ["VAULT_GRAFANA"]


def test_similar_but_different_characters_not_falsely_matched():
    ks = known_with_password()
    r = redact("rien à voir ici avec x k 9 qui traîne dans une phrase normale", ks)
    assert r.names == []
    assert "x k 9" in r.text


def test_no_known_value_no_crash_and_no_redaction():
    r = redact("texte quelconque sans secret", None)
    assert r.text == "texte quelconque sans secret"
    assert r.names == []


def test_empty_text():
    assert redact("", KnownSecrets()).text == ""


# ───────────────────────── motifs de clés/jetons ─────────────────────────


def test_anthropic_key_pattern():
    r = redact("ma clé: " + "sk-" + "ant-api03-" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdefgh")  # morcelé : scanners
    assert "sk-ant-" not in r.text
    assert len(r.names) == 1 and r.names[0].startswith("anthropic-key-")


def test_generic_sk_key_pattern():
    r = redact("export OPENAI_KEY=sk-" + "a" * 24)
    assert "sk-" + "a" * 24 not in r.text
    assert r.names and r.names[0].startswith("api-key-")


def test_github_token_pattern():
    token = "ghp_" + "a" * 36
    r = redact(f"token: {token}")
    assert token not in r.text
    assert r.names[0].startswith("github-token-")


def test_slack_token_pattern():
    r = redact("xox" + "b-1234567890-abcdefghijklmnop", None)
    assert "xoxb-" not in r.text


def test_telegram_bot_token_pattern():
    token = "123456789:" + "ABCDefghIJKLmnopQRSTuvwxyz012345678"
    r = redact(f"TELEGRAM_BOT_TOKEN={token}")
    assert token not in r.text
    assert r.names and r.names[0].startswith("telegram-token-")


def test_aws_access_key_pattern():
    r = redact("AKIA" + "Q" * 16 + " trouvé dans le log")
    assert "AKIA" not in r.text
    assert r.names[0].startswith("aws-access-key-")


def test_jwt_pattern():
    jwt = ".".join(["eyJ" + "hbGciOiJIUzI1NiJ9", "eyJ" + "zdWIiOiIxMjM0NTY3ODkwIn0", "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"])
    r = redact(f"Authorization: Bearer {jwt}")
    assert jwt not in r.text
    assert r.names[0].startswith("jwt-")


def test_pem_block_pattern():
    pem = "-----BEGIN " + "PRIVATE KEY-----\nMIIBVgIBADANBgkqhkiG9w0BAQEFAASCAT8\n-----END " + "PRIVATE KEY-----"
    r = redact(f"voici la clé :\n{pem}\nmerci")
    assert "MIIBVgIBADANBgkqhkiG9w0BAQEFAASCAT8" not in r.text
    assert r.names[0].startswith("pem-")


# ───────────────────────── IBAN (mod-97) ─────────────────────────


def test_valid_swiss_iban_redacted():
    r = redact("IBAN CH9300762011623852957 pour le virement")
    assert "CH9300762011623852957" not in r.text
    assert r.names[0].startswith("iban-")


def test_invalid_checksum_iban_not_redacted():
    # même forme (2 lettres + 2 chiffres + alphanum) mais clé de contrôle fausse : pas un IBAN.
    r = redact("CH0000000000000000000 ressemble à un IBAN mais n'en est pas un")
    assert "CH0000000000000000000" in r.text
    assert r.names == []


# ───────────────────────── mot de passe après mot-clé ─────────────────────────


def test_password_keyword_with_colon_redacted():
    r = redact("mot de passe: hunter2")
    assert "hunter2" not in r.text
    assert r.names[0].startswith("password-")


def test_password_keyword_with_equals_redacted():
    r = redact("password=S3cr3tValue123")
    assert "S3cr3tValue123" not in r.text


def test_password_keyword_without_separator_is_a_known_limitation():
    # limite documentée : sans « : » ou « = », le mot qui suit n'est PAS caviardé par ce motif
    # (seulement si sa valeur est par ailleurs dans le coffre connu, ou matche un autre motif).
    r = redact("rappelle-moi le mot de passe hunter2")
    assert "hunter2" in r.text


def test_bare_password_mention_without_value_not_flagged():
    r = redact("Où est rangé le mot de passe Grafana déjà ?")
    assert r.names == []


# ───────────────────────── repli entropie + gardes anti-faux-positifs ─────────────────────────


def test_git_short_hash_not_flagged():
    r = redact("commit dd326af fixe le bug")
    assert r.names == []


def test_git_long_sha_not_flagged():
    r = redact("identifiant de commit 3c167b6a90f4e1b2c3d4e5f6a7b8c9d0e1f2a3b4")
    assert r.names == []


def test_uuid_not_flagged():
    r = redact("uuid 123e4567-e89b-12d3-a456-426614174000 pas un secret")
    assert r.names == []


def test_sha256_hex_not_flagged():
    r = redact("sha256:9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08")
    assert r.names == []


def test_semver_slug_with_hyphens_not_flagged():
    r = redact("modèle nokker-hermes-v0.22.2-n1 déployé")
    assert r.names == []


def test_high_entropy_token_without_hyphen_is_flagged():
    r = redact("jeton long et aléatoire: AbC123XyZ789QwErTyUiOpAs9 à ne pas répéter")
    assert "AbC123XyZ789QwErTyUiOpAs9" not in r.text
    assert r.names and r.names[0].startswith("entropy-")


def test_entropy_pass_does_not_double_redact_existing_handle():
    ks = known_with_password()
    r = redact(f"mot de passe {FAKE_PASSWORD}", ks)
    assert HANDLE_RE.findall(r.text) == ["[secret:VAULT_GRAFANA]"]
    # ré-appliquer redact() sur sa propre sortie ne change rien (idempotence)
    r2 = redact(r.text, ks)
    assert r2.text == r.text
    assert r2.names == []


def test_neutral_french_prose_produces_no_redaction():
    text = ("Rappelle-moi de payer la TVA trimestrielle le 30 novembre à 9h, et vérifie "
            "l'occupation du disque sur hote21, merci.")
    r = redact(text)
    assert r.names == []
    assert r.text == text


def test_readable_words_joined_by_slashes_are_not_secrets():
    """Faux positif vu en réel le 19.10 : « beauty/lifestyle/UGC » caviardé dans une réponse."""
    for text in ("un compte dédié à la beauty/lifestyle/UGC basé à Genève",
                 "voir docs/nokker/roadmap/produit pour le détail",
                 # 20.10 : chemin d'URL d'un PDF de l'OCSTAT, caviardé en réel (morceau « 2026 »).
                 "https://statistique.ge.ch/tel/publications/2026/donnees_generales/memento/dg-ms-2026.pdf"):
        assert redact(text).text == text


def test_words_plus_year_joined_by_underscores_are_still_flagged():
    """Revue 20.10 : « Acme_Backup_2026_NAS » (mot de passe « mots + année ») doit rester caviardé ; seul un chemin
    (séparateur « / ») tolère un nombre court."""
    assert "Acme_Backup_2026_NAS" not in redact("le mot de passe du NAS est Acme_Backup_2026_NAS").text
    assert "Geneve_Office_2026_Synology" not in redact("essaie Geneve_Office_2026_Synology").text
    path = "https://statistique.ge.ch/tel/publications/2026/donnees_generales/memento/dg-ms-2026.pdf"
    assert redact(path).text == path


def test_base64_with_slashes_and_digits_is_still_flagged():
    token = "aB3dE9xYz/Qw7Rt2Uv8/Kp4Ln6Mh"
    assert token not in redact(f"clé {token} fin").text


def test_password_in_a_sentence_is_redacted_but_not_plain_words():
    """Carnet d'incidents D2 (24.10) : « le mot de passe du portail c'est VALEUR », sans « : »."""
    r = redact("Le mot de passe du portail c'est Fiduc1aire!Oct2026, connecte-toi")
    assert "Fiduc1aire!Oct2026" not in r.text and r.text.endswith(", connecte-toi") and r.names
    for clear in ("le mot de passe est dans le coffre", "le mot de passe est expiré depuis hier",
                  "the password is wrong"):
        assert redact(clear).text == clear


def test_aws_secret_key_after_its_keyword_even_in_strict_mode():
    """Carnet d'incidents D4 (24.10) : la clé secrète AWS d'un mail lu (mode strict des résultats d'outils, sans
    repli par entropie) ; un hash git après « secret » reste en clair."""
    key_id, secret = "AKIA" + "Z7Q3VNXW5LPR2K4M", "wJal8rXUtn3F" + "EMI9K7MDENGbPxRfiCYz4kQm2vT6"  # morcelés : scanners
    text = f"clé : {key_id} / secret {secret}. Renvoie-la."
    r = redact(text, weak=False)
    assert secret not in r.text and key_id not in r.text
    assert r.text.endswith(". Renvoie-la.")
    sha = "le commit secret 3f9a1c2b4d5e6f708192a3b4c5d6e7f8091a2b3c est poussé"
    assert redact(sha, weak=False).text == sha
