"""`nokker/previews/cache.py` — TTL mémoire (aperçus) et cache disque borné (images)."""
from __future__ import annotations

from nokker.previews.cache import (
    ImageDiskCache,
    TtlCache,
    is_valid_sha256,
    sign_image,
    verify_image_signature,
)


def test_ttl_cache_hit_then_miss_after_ttl():
    cache = TtlCache(maxsize=10, ttl_s=10.0)
    cache.set("k", "v", now=0.0)
    assert cache.get("k", now=5.0) == "v"
    assert cache.get("k", now=10.1) is None  # expiré


def test_ttl_cache_evicts_oldest_over_maxsize():
    cache = TtlCache(maxsize=2, ttl_s=1000.0)
    cache.set("a", 1, now=0.0)
    cache.set("b", 2, now=1.0)
    cache.set("c", 3, now=2.0)  # évince "a" (le plus ancien)
    assert cache.get("a", now=3.0) is None
    assert cache.get("b", now=3.0) == 2
    assert cache.get("c", now=3.0) == 3


def test_ttl_cache_get_refreshes_lru_order():
    cache = TtlCache(maxsize=2, ttl_s=1000.0)
    cache.set("a", 1, now=0.0)
    cache.set("b", 2, now=1.0)
    cache.get("a", now=2.0)  # "a" redevient le plus récent
    cache.set("c", 3, now=3.0)  # doit évincer "b", pas "a"
    assert cache.get("a", now=4.0) == 1
    assert cache.get("b", now=4.0) is None


def test_is_valid_sha256():
    assert is_valid_sha256("a" * 64)
    assert not is_valid_sha256("a" * 63)
    assert not is_valid_sha256("g" * 64)  # pas hexadécimal
    assert not is_valid_sha256("../../etc/passwd")  # tentative de traversée de chemin


def test_image_disk_cache_put_get_roundtrip(tmp_path):
    cache = ImageDiskCache(tmp_path, max_bytes=10_000_000)
    sha = cache.put(b"donnees-image", "image/png")
    assert is_valid_sha256(sha)
    data, content_type = cache.get(sha)
    assert data == b"donnees-image"
    assert content_type == "image/png"


def test_image_disk_cache_dedupes_identical_bytes(tmp_path):
    cache = ImageDiskCache(tmp_path, max_bytes=10_000_000)
    sha1 = cache.put(b"meme-contenu", "image/png")
    sha2 = cache.put(b"meme-contenu", "image/png")
    assert sha1 == sha2
    assert len(list(tmp_path.glob("*.bin"))) == 1


def test_image_disk_cache_get_unknown_sha_returns_none(tmp_path):
    cache = ImageDiskCache(tmp_path, max_bytes=10_000_000)
    assert cache.get("0" * 64) is None


def test_image_disk_cache_rejects_malformed_sha(tmp_path):
    cache = ImageDiskCache(tmp_path, max_bytes=10_000_000)
    assert cache.get("../../etc/passwd") is None


def test_image_disk_cache_evicts_oldest_when_over_budget(tmp_path):
    # 150 : assez pour UNE entrée de 100 octets, pas pour deux -> la 2e force l'éviction de la 1re.
    cache = ImageDiskCache(tmp_path, max_bytes=150)
    sha_old = cache.put(b"a" * 100, "image/png")
    assert cache.get(sha_old) is not None
    sha_new = cache.put(b"b" * 100, "image/png")
    assert cache.get(sha_new) is not None
    assert cache.get(sha_old) is None  # évincée (la plus ancienne par mtime) pour rester sous la borne


# ─────────────────────── images signées (docs/BUBBLES.md §9.3) ───────────────────────


def test_sign_then_verify_image_roundtrip():
    secret = b"s3cr3t-32-octets-pour-le-test!!"
    exp, sig = sign_image("a" * 64, secret, now=1_000.0)
    assert verify_image_signature("a" * 64, exp, sig, secret, now=1_500.0)


def test_verify_rejects_expired_signature():
    secret = b"s3cr3t"
    exp, sig = sign_image("a" * 64, secret, now=1_000.0, ttl_s=100.0)
    assert not verify_image_signature("a" * 64, exp, sig, secret, now=1_101.0)


def test_verify_rejects_tampered_signature_or_sha_or_wrong_secret():
    secret = b"s3cr3t"
    exp, sig = sign_image("a" * 64, secret, now=1_000.0)
    assert not verify_image_signature("a" * 64, exp, "0" * len(sig), secret, now=1_000.0)
    assert not verify_image_signature("b" * 64, exp, sig, secret, now=1_000.0)  # autre sha
    assert not verify_image_signature("a" * 64, exp, sig, b"autre-secret", now=1_000.0)


def test_verify_rejects_malformed_exp():
    secret = b"s3cr3t"
    assert not verify_image_signature("a" * 64, "pas-un-entier", "deadbeef", secret)
    assert not verify_image_signature("a" * 64, "", "", secret)
