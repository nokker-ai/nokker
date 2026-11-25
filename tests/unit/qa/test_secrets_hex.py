"""Hexadécimal pur (07.11) : le trou du repli par entropie est fermé, sans caviarder les empreintes publiques annoncées
ni les identifiants que le noyau fabrique (et que le modèle doit pouvoir réécrire)."""
from __future__ import annotations

import random
import re
from pathlib import Path

import pytest

from nokker.qa.secrets import KERNEL_ID_PREFIXES, redact

HEX = "0123456789abcdef"


def draws(n: int = 5000, length: int = 24) -> list[str]:
    rng = random.Random(20261107)
    return ["".join(rng.choice(HEX) for _ in range(length)) for _ in range(n)]


@pytest.mark.parametrize("length", [16, 20, 24, 32, 48])
def test_every_random_hex_token_is_redacted_without_keyword(length):
    """Mesure du 07.11 : avant, 2815 jetons de 24 sur 5000 (56 %) restaient en clair. Maintenant : aucun, à toutes les
    longueurs (un tirage sans lettre ou sans chiffre est écarté : il n'a pas la forme d'un jeton hexadécimal)."""
    missed = []
    for tok in draws(5000, length):
        if not (re.search(r"[0-9]", tok) and re.search(r"[a-f]", tok)):
            continue
        for text in (f"la valeur est {tok} ok", f"https://api.example.com/v1/items?id=1&x={tok}", f"KEY={tok}"):
            if tok in redact(text).text:
                missed.append((tok, text))
                break
    assert missed == [], f"{len(missed)} jetons en clair, ex. {missed[:3]}"


PUBLIC = [
    # sha git courts : sous 16 caractères
    "voir le commit 3265b5e sur la branche", "fix en 4a7e707f12, puis c69670c",
    # empreintes annoncées (40 / 64)
    "commit 9d4e2564b1c0a6f6e2a1f8f0d3c2b1a09e8d7c6b est fusionné",
    "sha: 0960860d9a11536fa7d8de258aab25320d399d95",
    "image@sha256:0960860d9a11536fa7d8de258aab25320d399d95257ba077a50428bcbd25e195",
    "digest sha256:f1bee89fc11cca9a1b2c3d4e5f60718293a4b5c6d7e8f90123456789abcdef01",
    "sha256 = 926da289bfff3264e66b8be714094fb327673f2e39595ebdfeba14ab45054244",
    "git show 3f9a1c2b4d5e6f708192a3b4c5d6e7f8091a2b3c", "git checkout -b fix 3f9a1c2b4d5e6f708192a3b4c5d6e7f8091a2b3c",
    # clôture d'une donnée non fiable (nonce de 16 hexa, ouverture et fermeture)
    '<untrusted-data kind="mail" id="00aa11bb22cc33dd">\nx\n</untrusted-data id="00aa11bb22cc33dd">',
    # appId public Firebase
    'appId: "1:234567890123:web:0a1b2c3d4e5f6a7b"',
    # UUID (morceaux de 12 au plus)
    "fil 278ed3ad-2ee5-4a46-9573-64b7910405cd ouvert",
    # identifiants du noyau, que le modèle réécrit
    "page pg_3f2a9c1b7d4e8f60, version 2", "tâche dt_0123456789abcdef en attente", "nœud dk_a1b2c3d4e5f60718",
    "proposition px_9f8e7d6c5b4a3f2e", "call_0a1b2c3d4e5f prêt",
    # couleurs, adresses, nombres
    "#a1b2c3 et #ffffff", "adresse 0x52908400098527886E0F7030069857D2E4169EE7", "fe80::1ff:fe23:4567:890a",
    "commande 1234567890123456789",
]


@pytest.mark.parametrize("text", PUBLIC)
def test_public_digests_and_kernel_ids_stay_clear(text):
    assert redact(text).text == text


NOT_PUBLIC = [
    ("ObjectId 507f1f77bcf86cd799439011 du document", "507f1f77bcf86cd799439011"),  # faux positif ACCEPTÉ (décision)
    ("le sha 9d4e2564b1c0a6f6e2a1 tronqué", "9d4e2564b1c0a6f6e2a1"),  # 20 : ni court, ni 40/64
    ("valeur 9d4e2564b1c0a6f6e2a1f8f0d3c2b1a09e8d7c6b sans mot", "9d4e2564b1c0a6f6e2a1f8f0d3c2b1a09e8d7c6b"),
    ("token api_0123456789abcdef0123", "0123456789abcdef0123"),  # préfixe qui n'est pas du noyau
    ("md5 5d41402abc4b2a76b9719d911017c592", "5d41402abc4b2a76b9719d911017c592"),
    # un faux nonce de clôture plus long que 16 ne passe pas
    ('<untrusted-data id="0123456789abcdef0123456789">', "0123456789abcdef0123456789"),
]


@pytest.mark.parametrize("text,value", NOT_PUBLIC)
def test_other_hex_values_are_redacted(text, value):
    out = redact(text)
    assert value not in out.text and any(n.startswith("hex-") for n in out.names), out.text


def test_redaction_is_idempotent_and_handles_are_not_rescanned():
    once = redact("a 0123456789abcdef0123 b").text
    assert redact(once).text == once


def test_kernel_id_prefixes_cover_every_hex_id_the_code_makes():
    """Tout identifiant `<préfixe>_` + hexadécimal aléatoire fabriqué par le noyau doit être dans KERNEL_ID_PREFIXES ;
    sinon le modèle verrait ses propres identifiants caviardés (tâches du bureau, pages…)."""
    root = Path(__file__).resolve().parents[3] / "nokker"
    pat = re.compile(r"""f?["']([a-z]{1,8})_["']?\s*\+?\s*\{?(?:secrets\.|_s\.)?(?:token_hex\(|uuid\.uuid4\(\)\.hex)""")
    found = set()
    for p in root.rglob("*.py"):
        for m in pat.finditer(p.read_text(encoding="utf-8", errors="replace")):
            found.add(m.group(1))
    assert found, "motif de recherche cassé"
    assert found <= KERNEL_ID_PREFIXES, sorted(found - KERNEL_ID_PREFIXES)
