"""Modération des pages publiques (docs/LOKKAL.md §7.26, point 6) : un signalement arrive à nokker.ai/report, l'opérateur
liste les pages publiques et en retire une (`python -m nokker pages list --public | unpublish | restore`).

Ce que le retrait doit garantir :
- l'accès public est coupé tout de suite : le relais demande à nokker.ai avant de servir une page publique (machine en
  ligne : `POST /v1/gateway/public-page/check` → 410 ; machine éteinte : la copie publique → 410, et sa publication
  est effacée) ;
- la page reste à son auteur : sa machine la dépublie à la synchronisation suivante (`moderated_pages` de
  `GET /v1/gateway/machine`), la marque `moderated_at` (« retirée par la modération ») et refuse de la republier tant
  que l'opérateur ne l'a pas rendue (`restore`) ;
- une ligne d'audit (page, motif, date), jamais d'adresse ni d'identifiant de compte dans les sorties de l'opérateur."""
from __future__ import annotations

import hashlib
import json

import pytest

from nokker.cloud import http as cloud_http
from nokker.cloud import moderation as md
from nokker.pages.store import PageError, PageStore
from tests.unit.test_mirror_pages import PAGE, _call, _worker, cloud  # noqa: F401 — fixture
from tests.unit.test_public_pages import HTML, Link, _http, lokkal  # noqa: F401 — fixture

KEY = {"X-Nokker-Relay-Key": "k" * 32}
PUB = "AbCdEfGhIjKlMnOpQrStUv_-"
SHA = hashlib.sha256(PUB.encode()).hexdigest()
PG = "pg_0123456789abcdef"


# ─────────────────────────────── registre de nokker.ai ───────────────────────────────


def test_registry_blocks_by_public_id_or_by_page_and_merges_what_it_learns():
    s = md.MemoryPublicPages()
    assert s.view("mc_1", "tn_a", SHA, PG, 1200, now=100.0) is False
    assert [(r["page_id"], r["public_sha256"], r["size"]) for r in s.listing()] == [(PG, SHA, 1200)]
    # retrait par l'adresse publique (empreinte de l'id public)
    s.set_moderation("mc_1", "tn_a", None, SHA, "unpublish", "contenu illicite", now=200.0)
    assert s.view("mc_1", "tn_a", SHA, PG, 1200, now=210.0) is True
    assert s.listing() == [] and len(s.listing(moderated=True)) == 1
    assert s.moderated_of("mc_1") == {"page_ids": [PG], "public_sha256": [SHA]}
    # une autre machine qui aurait le même id de page n'est pas touchée
    assert s.view("mc_2", "tn_b", hashlib.sha256(b"x" * 24).hexdigest(), PG, 10, now=220.0) is False
    # retrait par le page_id seul (adresse privée) : republiée sous un autre id public, elle reste bloquée
    s2 = md.MemoryPublicPages()
    s2.set_moderation("mc_1", "tn_a", PG, None, "unpublish", "spam", now=50.0)
    assert s2.view("mc_1", "tn_a", SHA, PG, 900, now=60.0) is True
    assert [(r["page_id"], r["public_sha256"]) for r in s2.listing(moderated=True)] == [(PG, SHA)]
    # rendue par l'opérateur : servie de nouveau
    s2.set_moderation("mc_1", "tn_a", PG, None, "restore", "erreur de signalement", now=70.0)
    assert s2.view("mc_1", "tn_a", SHA, PG, 900, now=80.0) is False
    assert s2.moderated_of("mc_1") == {"page_ids": [], "public_sha256": []}
    assert [e["action"] for e in s2.events()] == ["unpublish", "restore"]


def test_a_removed_page_never_loses_an_identity_to_a_lying_machine():
    """Revue du relais (25.10) : une machine modifiée qui annonce un AUTRE page_id sur l'adresse publique retirée ne
    remplace pas le page_id d'origine dans le registre (sinon la page d'origine, republiée sous un nouvel id public,
    échapperait au blocage) ; toute identité vue avec une identité retirée est retirée elle aussi."""
    other, sha2 = "pg_" + "b" * 16, hashlib.sha256(b"y" * 24).hexdigest()
    s = md.MemoryPublicPages()
    assert s.view("mc_1", "tn_a", SHA, PG, 1200, now=100.0) is False
    s.set_moderation("mc_1", "tn_a", PG, SHA, "unpublish", "contenu illicite", now=200.0)
    assert s.view("mc_1", "tn_a", SHA, other, 1200, now=210.0) is True  # même adresse, page_id mensonger
    assert s.view("mc_1", "tn_a", sha2, PG, 1200, now=220.0) is True  # page d'origine sous un nouvel id public
    assert s.view("mc_1", "tn_a", sha2, other, 1200, now=230.0) is True
    assert s.moderated_of("mc_1") == {"page_ids": sorted([PG, other]), "public_sha256": sorted([SHA, sha2])}
    assert s.listing() == []
    # une page vue AVANT le retrait sous une autre identité, puis vue avec l'identité retirée : retirée aussi
    s3 = md.MemoryPublicPages()
    assert s3.view("mc_1", "tn_a", sha2, other, 10, now=1.0) is False
    s3.set_moderation("mc_1", "tn_a", PG, SHA, "unpublish", "spam", now=2.0)
    assert s3.view("mc_1", "tn_a", SHA, other, 10, now=3.0) is True
    assert s3.view("mc_1", "tn_a", sha2, other, 10, now=4.0) is True and s3.listing() == []


def test_reason_is_required_and_never_keeps_an_address():
    assert md.clean_reason("  signalé : contact jean.dupont@example.com  ") == "signalé : contact [adresse retirée]"
    for bad in ("", "   ", "x" * 501):
        with pytest.raises(md.ModerationError):
            md.clean_reason(bad)


# ─────────────────────────────── routes du relais chez nokker.ai ───────────────────────────────


def _check(port, machine, public_id=PUB, page_id=PG, headers=KEY):
    return _call(port, "POST", "/v1/gateway/public-page/check",
                 {"host": machine.host, "public_id": public_id, "page_id": page_id, "size": 1200}, headers=headers)


def test_relay_asks_before_serving_and_gets_410_once_removed(cloud):  # noqa: F811
    port, mt, machine, _, links, _ = cloud
    reg = md.MemoryPublicPages()
    cloud_http._cloud.public_pages = reg
    cloud_http._cloud.pages_public_allowed = lambda acc: acc == "us_p"
    assert _check(port, machine) == (200, {"allowed": True})
    assert _check(port, machine, headers={"X-Nokker-Relay-Key": "x" * 32})[0] == 404     # sans la clé du relais
    assert _check(port, machine, headers={})[0] == 404
    assert _check(port, machine, public_id="court")[0] == 404
    row = reg.listing()[0]
    assert (row["machine_id"], row["tenant_id"], row["page_id"]) == (machine.id, "tn_p", PG)
    reg.set_moderation(machine.id, "tn_p", None, SHA, "unpublish", "signalement", now=1.0)
    st, out = _check(port, machine)
    assert st == 410 and out["error"]["code"] == "moderated" and "modération" in out["error"]["message"]
    # la machine apprend ses pages retirées au contrôle qu'elle fait déjà (`GET /v1/gateway/machine`)
    got = _call(port, "GET", "/v1/gateway/machine", token=mt)[1]
    assert got["moderated_pages"] == [PG] and got["moderated_public"] == [SHA]
    cloud_http._cloud.pages_public_allowed = lambda acc: False
    assert _check(port, machine)[0] == 404                       # compte sans droit de publier : rien n'est servi


def test_offline_public_copy_is_gone_once_removed(cloud):  # noqa: F811
    port, mt, machine, store, _, tmp = cloud
    cloud_http._cloud.pages_public_allowed = lambda acc: acc == "us_p"
    reg = md.MemoryPublicPages()
    cloud_http._cloud.public_pages = reg
    pages = PageStore(tmp / "pages")
    m = pages.create("th_1", "bilan.html", "Bilan", PAGE)
    w = _worker(port, mt, pages, {})
    w.set_page_mirror(m.page_id, True)
    pub = pages.publish(m.page_id, 1, hashlib.sha256(PAGE).hexdigest())
    w.push_pages_once()

    def offline():
        return _call(port, "POST", "/v1/gateway/mirror/public-page", {"host": machine.host, "public_id": pub.public_id},
                     headers=KEY)

    assert offline()[0] == 200
    assert [r["page_id"] for r in reg.listing()] == [m.page_id]  # la copie servie est inscrite au registre
    reg.set_moderation(machine.id, "tn_p", m.page_id, None, "unpublish", "signalement", now=1.0)
    st, out = offline()
    assert st == 410 and out["error"]["code"] == "moderated"


# ─────────────────────────────── commande de l'opérateur ───────────────────────────────


@pytest.fixture
def operator(cloud, monkeypatch):  # noqa: F811
    """`python -m nokker pages …` sur les magasins en mémoire du noyau de test (registre, machines, copies)."""
    port, mt, machine, store, links, tmp = cloud
    reg = md.MemoryPublicPages()
    cloud_http._cloud.public_pages = reg
    cloud_http._cloud.pages_public_allowed = lambda acc: acc == "us_p"
    service = md.Moderation(store=reg, machine_by_host=links.store.by_host, machine_by_id=links.store.machine,
                            mirror_pages=store)
    monkeypatch.setattr(md, "open_operator", lambda config: service)

    def run(*argv):
        from nokker.cli import main

        return main(["pages", *argv])

    return run, reg, machine, port, mt, store, tmp


def test_list_public_shows_page_name_tenant_date_size_and_no_account(operator, capsys):
    run, reg, machine, port, _, _, _ = operator
    assert _check(port, machine)[0] == 200
    assert run("list", "--public") == 0
    out = capsys.readouterr().out
    line = next(ln for ln in out.splitlines() if PG in ln)
    assert "atelier--pub.nokker.app" in line and "tn_p" in line and "1200" in line and "20" in line  # date ISO
    assert "us_p" not in out and "@" not in out and "<h1>" not in out


def test_unpublish_is_dry_by_default_requires_a_reason_and_audits(operator, capsys):
    run, reg, machine, port, mt, _, _ = operator
    assert _check(port, machine)[0] == 200
    url = f"https://atelier--pub.nokker.app/{PUB}"
    with pytest.raises(SystemExit) as e:
        run("unpublish", url)                                           # --reason obligatoire
    assert e.value.code == 2
    capsys.readouterr()
    assert run("unpublish", url, "--reason", "signalement n° 12") == 0  # à blanc
    out = capsys.readouterr().out
    assert "BLANC" in out and PG in out and _check(port, machine)[0] == 200 and reg.events() == []
    assert run("unpublish", url, "--reason", "signalement n° 12, auteur jean@example.com", "--apply") == 0
    out = capsys.readouterr().out
    audit = json.loads(next(ln for ln in out.splitlines() if ln.startswith("{")))
    assert audit["event"] == "page_unpublished" and audit["page"] == PG and audit["tenant"] == "tn_p"
    assert audit["reason"] == "signalement n° 12, auteur [adresse retirée]" and audit["at"].endswith("Z")
    assert "us_p" not in out and "@" not in out
    assert _check(port, machine)[0] == 410
    ev = reg.events()
    assert [(e["action"], e["page_id"], e["reason"]) for e in ev] == [
        ("unpublish", PG, "signalement n° 12, auteur [adresse retirée]")]
    assert run("list", "--public") == 0 and PG not in capsys.readouterr().out
    assert run("list", "--moderated") == 0 and PG in capsys.readouterr().out
    # rendue par l'opérateur seulement (même exigence de motif)
    assert run("restore", PG, "--reason", "signalement infondé", "--apply") == 0
    assert _check(port, machine)[0] == 200


def test_unpublish_by_private_address_or_page_id_and_unknown_targets(operator, capsys):
    run, reg, machine, port, _, _, _ = operator
    # adresse de la page sur l'origine des pages privées : machine + page_id, même si jamais vue en public
    assert run("unpublish", f"https://atelier--pages.nokker.app/{PG}", "--reason", "r", "--apply") == 0
    assert _check(port, machine)[0] == 410
    assert reg.moderated_of(machine.id)["page_ids"] == [PG]
    for bad in ("https://inconnue--pub.nokker.app/" + PUB, "https://exemple.org/" + PUB, "pg_ffffffffffffffff",
                "n'importe quoi"):
        assert run("unpublish", bad, "--reason", "r", "--apply") == 1, bad
    assert "erreur" in capsys.readouterr().err


def test_unpublish_clears_the_public_mark_of_the_mirror_copy(operator, capsys):
    run, reg, machine, port, mt, store, tmp = operator
    pages = PageStore(tmp / "pages")
    m = pages.create("th_1", "bilan.html", "Bilan", PAGE)
    w = _worker(port, mt, pages, {})
    w.set_page_mirror(m.page_id, True)
    pub = pages.publish(m.page_id, 1, hashlib.sha256(PAGE).hexdigest())
    w.push_pages_once()
    assert store.get("tn_p", machine.id, m.page_id)["public_sha256"]
    assert run("unpublish", f"https://atelier--pub.nokker.app/{pub.public_id}", "--reason", "r", "--apply") == 0
    row = store.get("tn_p", machine.id, m.page_id)
    assert row is not None and row["public_sha256"] is None             # copie gardée pour l'auteur, plus publique


# ─────────────────────────────── machine de l'auteur ───────────────────────────────


def test_machine_store_applies_and_lifts_moderation(tmp_path):
    s = PageStore(tmp_path / "pages")
    a = s.create("th_a", "a.html", "A", HTML)
    b = s.create("th_a", "b.html", "B", HTML.replace(b"v1", b"vb"))
    pa = s.publish(a.page_id, 1, hashlib.sha256(HTML).hexdigest())
    sha_a = hashlib.sha256(pa.public_id.encode()).hexdigest()  # `pa` est la méta vivante : son id public va mourir
    s.publish(b.page_id, 1, hashlib.sha256(HTML.replace(b"v1", b"vb")).hexdigest())
    changed = s.apply_moderation({"page_ids": [], "public_sha256": [sha_a]},
                                 now=500.0)
    assert [m.page_id for m in changed] == [a.page_id]
    got = s.get(a.page_id)
    assert got.moderated_at == 500.0 and not got.public and got.public_id is None
    assert got.state()["moderated_at"] == 500.0 and s.read(a.page_id) is not None   # gardée pour son auteur
    assert s.get(b.page_id).public and s.get(b.page_id).moderated_at is None
    with pytest.raises(PageError, match="modération"):
        s.publish(a.page_id, 1, hashlib.sha256(HTML).hexdigest())
    # toujours retirée tant que nokker.ai la liste (même empreinte), rendue quand elle n'y est plus
    assert s.apply_moderation({"page_ids": [], "public_sha256": [sha_a]},
                              now=600.0) == []
    assert PageStore(tmp_path / "pages").get(a.page_id).moderated_at == 500.0     # gardé sur disque
    assert [m.page_id for m in s.apply_moderation({"page_ids": [], "public_sha256": []}, now=700.0)] == [a.page_id]
    assert s.get(a.page_id).moderated_at is None and not s.get(a.page_id).public  # l'auteur peut republier (carte)
    assert [m.page_id for m in s.apply_moderation({"page_ids": [b.page_id], "public_sha256": []}, now=800.0)] == \
        [b.page_id]
    assert s.get(b.page_id).moderated_at == 800.0


def test_link_check_hands_the_moderation_to_the_machine(tmp_path):
    from nokker.local.setup import LocalLink

    class Resp:
        status_code = 200

        def __init__(self, body):
            self.body = body

        def json(self):
            return self.body

    class Client:
        body = {"pages_public": True, "moderated_pages": [PG], "moderated_public": []}

        def get(self, url, headers=None):
            return Resp(self.body)

    class Vault:
        def get(self, name):
            return "mt_x" if name == "machine_token" else None

    link = LocalLink(setup=type("S", (), {"vault": Vault(), "state": {}})(), client=Client())
    assert link.moderation is None
    assert link.pages_public() is True
    assert link.moderation == {"page_ids": [PG], "public_sha256": []}
    Client.body = {"pages_public": True}                    # nokker.ai sans registre : on garde ce qu'on savait
    assert link.pages_public(ttl_s=0.0) is True and link.moderation == {"page_ids": [PG], "public_sha256": []}


def test_author_sees_the_removal_and_cannot_republish(lokkal):  # noqa: F811
    port, api, _, _ = lokkal
    meta = api.pages.create("th_a", "index.html", "Démo", HTML)
    pub = api.pages.publish(meta.page_id, 1, hashlib.sha256(HTML).hexdigest())
    st, h, _ = _http(port, "GET", f"/v1/pages/public/{pub.public_id}")
    assert st == 200 and h["X-Nokker-Page-Id"] == meta.page_id    # lu par le relais (registre de nokker.ai)

    class ModeratedLink(Link):
        moderation = None

        def pages_public(self, ttl_s=300.0):
            self.moderation = {"page_ids": [meta.page_id], "public_sha256": []}  # lu dans GET /v1/gateway/machine
            return True

    api.link = ModeratedLink()
    st, _, raw = _http(port, "POST", f"/v1/pages/{meta.page_id}/publish", api.token, {})
    assert st == 403 and json.loads(raw)["error"]["code"] == "page_moderated"
    assert "modération" in json.loads(raw)["error"]["message"]
    got = api.pages.get(meta.page_id)
    assert got.moderated_at is not None and not got.public
    assert _http(port, "GET", f"/v1/pages/public/{pub.public_id}")[0] == 404
    link = json.loads(_http(port, "POST", f"/v1/pages/{meta.page_id}/link", api.token, {})[2])
    assert link["moderated_at"] == got.moderated_at and "public_url" not in link
