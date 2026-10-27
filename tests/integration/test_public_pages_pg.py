"""Pages publiques sur un vrai Postgres (migration 035, docs/LOKKAL.md §7.26) : la copie porte l'empreinte de son id
public sous la RLS du locataire, se dépublie, et le registre des jetons de pages consommés est partagé (hors RLS,
premier usage atomique). Ignoré sans `NOKKER_TEST_PG_DSN`."""
from __future__ import annotations

import hashlib
import threading

import pytest

from tests.integration.test_rls import A, B, pg, requires_pg  # noqa: F401 — fixture (base neuve, rôles, migrations)


@requires_pg
def test_public_copy_under_rls_and_unpublish(pg):  # noqa: F811
    from nokker.cloud.pg import PgMirrorPages
    from nokker.tenancy.context import tenant_scope

    s = PgMirrorPages(pg["app"])
    sha = hashlib.sha256(b"AbCdEfGhIjKlMnOpQrStUv_-").hexdigest()
    row = {"title": "Bilan", "version": 3, "size": 10, "sha256": "s", "html": b"<p>bilan</p>", "page_updated_at": 1.0,
           "mirrored_at": 2.0, "public_sha256": sha, "public_index": True}
    with tenant_scope(A):
        s.put(A, "mc_a", "pg_aaaaaaaaaaaaaaaa", row, quota=100)
        got = s.get_public(A, "mc_a", sha)
        assert got["page_id"] == "pg_aaaaaaaaaaaaaaaa" and got["public_index"] is True and got["html"] == b"<p>bilan</p>"
        assert s.get_public(A, "mc_autre", sha) is None
    with tenant_scope(B):
        assert s.get_public(B, "mc_a", sha) is None             # la RLS : un autre locataire ne la voit pas
        assert not s.clear_public(B, "mc_a", "pg_aaaaaaaaaaaaaaaa")
    with tenant_scope(A):
        # une copie sans publication la retire ; la republier, puis la dépublier par la route dédiée
        s.put(A, "mc_a", "pg_aaaaaaaaaaaaaaaa", {**row, "public_sha256": None, "public_index": False}, quota=100)
        assert s.get_public(A, "mc_a", sha) is None
        s.put(A, "mc_a", "pg_aaaaaaaaaaaaaaaa", row, quota=100)
        assert s.clear_public(A, "mc_a", "pg_aaaaaaaaaaaaaaaa") and not s.clear_public(A, "mc_a", "pg_aaaaaaaaaaaaaaaa")
        assert s.get_public(A, "mc_a", sha) is None and s.get(A, "mc_a", "pg_aaaaaaaaaaaaaaaa")["version"] == 3


@requires_pg
def test_used_jti_registry_is_shared_and_atomic(pg):  # noqa: F811
    from nokker.cloud.pg import PgUsedJti

    regs = [PgUsedJti(pg["app"]), PgUsedJti(pg["app"])]           # deux répliques du relais, un seul registre
    now = 1_900_000_000.0
    assert regs[0].first_use("a" * 64, now + 120, now) is True
    assert regs[1].first_use("a" * 64, now + 120, now) is False
    # vingt présentations simultanées du même jeton : une seule passe
    wins: list[bool] = []
    lock = threading.Lock()

    def try_once(i: int) -> None:
        ok = regs[i % 2].first_use("b" * 64, now + 120, now)
        with lock:
            wins.append(ok)

    threads = [threading.Thread(target=try_once, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert wins.count(True) == 1
    # expiré : oublié (balayé), l'empreinte peut resservir (un nouveau jeton n'aura jamais le même jti en pratique)
    later = now + 1000
    assert regs[0].first_use("a" * 64, later + 120, later) is True
    import psycopg

    with psycopg.connect(pg["app"], autocommit=True) as conn:
        n = conn.execute("SELECT count(*) FROM lokkal_relay_used_jti WHERE expires_at < %s", (later,)).fetchone()[0]
    assert n == 0  # « b » expiré : balayé (au plus une fois par minute et par réplique)


@requires_pg
def test_moderation_registry_and_append_only_audit(pg):  # noqa: F811
    """Registre de modération (migration 040, §7.26 point 6) sur le vrai rôle nokker_app : hors RLS (relais avant le
    locataire, opérateur pour tous), fusion des lignes d'une même page, audit en ajout seul, effacé avec le locataire."""
    import psycopg

    from nokker.cloud.moderation import PgPublicPages

    s = PgPublicPages(pg["app"])
    sha = hashlib.sha256(b"AbCdEfGhIjKlMnOpQrStUv_-").hexdigest()
    sha2 = hashlib.sha256(b"ZyXwVuTsRqPoNmLkJiHgFe_-").hexdigest()
    assert s.view("mc_a", A, sha, "pg_aaaaaaaaaaaaaaaa", 1200, now=100.0) is False
    assert s.view("mc_a", A, sha, "pg_aaaaaaaaaaaaaaaa", 1200, now=110.0) is False  # vue récente : rien réécrit
    assert [(r["page_id"], r["size"], r["last_seen_at"]) for r in s.listing()] == [("pg_aaaaaaaaaaaaaaaa", 1200, 100.0)]
    # retrait par l'adresse publique d'une page jamais vue sous cet id, puis vue avec son page_id : une seule ligne
    s.set_moderation("mc_b", B, None, sha2, "unpublish", "signalement", now=200.0)
    assert s.view("mc_b", B, sha2, "pg_bbbbbbbbbbbbbbbb", 50, now=210.0) is True
    s.set_moderation("mc_a", A, "pg_aaaaaaaaaaaaaaaa", None, "unpublish", "spam", now=300.0)
    assert s.view("mc_a", A, sha, "pg_aaaaaaaaaaaaaaaa", 1200, now=310.0) is True
    assert s.moderated_of("mc_a") == {"page_ids": ["pg_aaaaaaaaaaaaaaaa"], "public_sha256": [sha]}
    assert {r["page_id"] for r in s.listing(moderated=True)} == {"pg_aaaaaaaaaaaaaaaa", "pg_bbbbbbbbbbbbbbbb"}
    s.set_moderation("mc_a", A, "pg_aaaaaaaaaaaaaaaa", None, "restore", "infondé", now=400.0)
    assert s.view("mc_a", A, sha, "pg_aaaaaaaaaaaaaaaa", 1200, now=410.0) is False
    assert [e["action"] for e in s.events()] == ["unpublish", "unpublish", "restore"]
    with psycopg.connect(pg["app"], autocommit=True) as conn:
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute("UPDATE lokkal_page_moderation SET reason = 'autre'")
        n = conn.execute("SELECT count(*) FROM lokkal_public_pages WHERE machine_id = 'mc_b'").fetchone()[0]
    assert n == 1


@requires_pg
def test_moderation_never_rewrites_a_removed_identity_pg(pg):  # noqa: F811
    """Revue du relais (25.10) : une machine qui ment sur le page_id d'une adresse retirée ne libère pas la page
    d'origine ; chaque identité vue avec une identité retirée est inscrite retirée (index uniques respectés)."""
    from nokker.cloud.moderation import PgPublicPages

    s = PgPublicPages(pg["app"])
    pid, other = "pg_cccccccccccccccc", "pg_dddddddddddddddd"
    sha = hashlib.sha256(b"CcCcCcCcCcCcCcCcCcCcCc_-").hexdigest()
    sha2 = hashlib.sha256(b"DdDdDdDdDdDdDdDdDdDdDd_-").hexdigest()
    assert s.view("mc_c", A, sha, pid, 10, now=100.0) is False
    s.set_moderation("mc_c", A, pid, sha, "unpublish", "contenu illicite", now=200.0)
    assert s.view("mc_c", A, sha, other, 10, now=210.0) is True
    assert s.view("mc_c", A, sha2, pid, 10, now=220.0) is True
    assert s.view("mc_c", A, sha2, other, 10, now=230.0) is True
    assert s.moderated_of("mc_c") == {"page_ids": sorted([pid, other]), "public_sha256": sorted([sha, sha2])}
    assert not [r for r in s.listing() if r["machine_id"] == "mc_c"]


@requires_pg
def test_moderation_notices_carry_the_reason_to_the_author_pg(pg):  # noqa: F811
    """Exposé des motifs (migration 041, DSA art. 17) : le registre garde le motif du retrait en cours, propagé à une
    identité retirée par contagion, effacé par `restore` ; l'audit reste en ajout seul ; ligne retirée avant 041 :
    le motif est repris de l'audit au démarrage (migration rejouée)."""
    import psycopg

    from nokker.cloud.moderation import APPEAL, PgPublicPages
    from nokker.store.pg import apply_migrations

    s = PgPublicPages(pg["app"])
    pid, other = "pg_eeeeeeeeeeeeeeee", "pg_ffffffffffffffff"
    sha = hashlib.sha256(b"EeEeEeEeEeEeEeEeEeEeEe_-").hexdigest()
    assert s.view("mc_e", A, sha, pid, 10, now=100.0) is False
    assert s.notices_of("mc_e") == []
    s.set_moderation("mc_e", A, None, sha, "unpublish", "contenu trompeur", now=200.0)
    assert s.notices_of("mc_e") == [{"page_id": pid, "public_sha256": sha, "at": 200.0, "reason": "contenu trompeur",
                                     "appeal": APPEAL}]
    assert s.view("mc_e", A, sha, other, 10, now=210.0) is True        # page_id mensonger : retiré, même motif
    assert {(n["page_id"], n["reason"]) for n in s.notices_of("mc_e")} == {(pid, "contenu trompeur"),
                                                                            (other, "contenu trompeur")}
    s.set_moderation("mc_e", A, pid, None, "restore", "infondé", now=300.0)
    assert [n["page_id"] for n in s.notices_of("mc_e")] == [other]
    with psycopg.connect(pg["app"], autocommit=True) as conn:
        assert conn.execute("SELECT moderation_reason FROM lokkal_public_pages WHERE page_id = %s",
                            (pid,)).fetchone()[0] is None
        with pytest.raises(psycopg.errors.RaiseException):           # l'audit ne bouge pas
            conn.execute("UPDATE lokkal_page_moderation SET reason = 'autre'")
    # une ligne retirée « d'avant 041 » (motif absent) : la migration rejouée reprend le dernier retrait de l'audit
    s.set_moderation("mc_e", A, pid, None, "unpublish", "récidive", now=400.0)
    with psycopg.connect(pg["app"], autocommit=True) as conn:
        conn.execute("UPDATE lokkal_public_pages SET moderation_reason = NULL WHERE page_id = %s", (pid,))
    assert {n["page_id"]: n["reason"] for n in s.notices_of("mc_e")}[pid] is None
    assert apply_migrations(pg["owner"], strict=True)
    assert {n["page_id"]: n["reason"] for n in s.notices_of("mc_e")} == {pid: "récidive", other: "contenu trompeur"}
