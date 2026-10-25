"""Lien Lokkal côté nokker.ai sur un vrai Postgres (migration 014, nokker/cloud/pg.py), avec le rôle du pod
(`nokker_app`) : flux complet, empreintes seulement en base, révocation, effacement du compte couvre les machines."""
from __future__ import annotations

import psycopg
import pytest

from nokker.cloud.link import LinkService
from nokker.cloud.pg import PgLinkStore
from tests.integration.test_rls import pg, requires_pg  # noqa: F401  (pg : fixture)


@requires_pg
def test_device_flow_on_postgres(pg):  # noqa: F811
    svc = LinkService(store=PgLinkStore(pg["app"]))
    d = svc.device("atelier")
    assert svc.token(d["device_code"]) == {"state": "pending"}
    assert svc.approve(d["user_code"], "us_pg", "tn_pg")["host"] == "atelier.nokker.app"
    t = svc.token(d["device_code"])
    assert t["state"] == "approved" and svc.token(d["device_code"]) == {"state": "expired"}
    m = svc.machine_for_token(t["machine_token"])
    assert m is not None and m.tenant == "tn_pg" and [x.id for x in svc.machines_of("us_pg")] == [m.id]
    with psycopg.connect(pg["su"]) as conn:
        dump = str(conn.execute("SELECT * FROM lokkal_machines").fetchall())
        assert t["machine_token"] not in dump  # jamais en clair
        assert conn.execute("SELECT count(*) FROM lokkal_link_requests").fetchone()[0] == 0
    d2 = svc.device("atelier")
    assert svc.approve(d2["user_code"], "us_pg2", "tn_pg2")["host"] == "atelier-2.nokker.app"
    assert svc.has_machine("tn_pg") and not svc.has_machine("tn_pg2")  # tn_pg2 : approuvée, jeton pas relevé
    assert svc.revoke(t["machine_token"]) and svc.machine_for_token(t["machine_token"]) is None
    assert not svc.has_machine("tn_pg")  # outils Lokkal du compte : plus aucune machine liée


@requires_pg
def test_my_machines_last_seen_and_account_revoke_on_postgres(pg):  # noqa: F811
    import uuid

    store = PgLinkStore(pg["app"])
    svc = LinkService(store=store)
    acc = f"us_seen{uuid.uuid4().hex[:8]}"  # compte propre à ce test : la base est partagée par la session
    d = svc.device("portable")
    svc.approve(d["user_code"], acc, "tn_seen")
    m = svc.machine_for_token(svc.token(d["device_code"])["machine_token"])
    assert m.last_seen_at is None
    store.seen(m.id, 1_792_000_000.0)
    assert svc.machines_of(acc)[0].last_seen_at == 1_792_000_000.0
    assert not svc.revoke_machine("us_autre", m.id) and svc.machines_of(acc)
    assert svc.revoke_machine(acc, m.id) and svc.machines_of(acc) == []


@requires_pg
def test_machine_load_one_row_per_machine_under_tenant_rls(pg):  # noqa: F811
    """Lot X5 : une ligne courante par machine (pas d'historique), lue par « mes machines » du compte, invisible à un
    autre locataire (RLS forcée, rôle nokker_app)."""
    import uuid

    store = PgLinkStore(pg["app"])
    svc = LinkService(store=store)
    acc, tenant = f"us_load{uuid.uuid4().hex[:8]}", f"tn_load{uuid.uuid4().hex[:8]}"
    d = svc.device(f"calc{uuid.uuid4().hex[:6]}")
    svc.approve(d["user_code"], acc, tenant)
    m = svc.machine_for_token(svc.token(d["device_code"])["machine_token"])
    assert svc.machines_with_load(acc, tenant)[0].load is None
    assert svc.report_load(m, {"ram": {"total_mb": 1024, "free_mb": 512, "source": "cgroup"}, "sessions": 1})
    assert svc.report_load(m, {"ram": {"total_mb": 1024, "free_mb": 128, "source": "cgroup"}, "sessions": 3,
                               "pirate": 1})
    [mine] = svc.machines_with_load(acc, tenant)
    assert mine.load["ram"]["free_mb"] == 128 and mine.load["sessions"] == 3 and "pirate" not in mine.load
    assert mine.load_at and mine.last_seen_at is None
    assert store.loads_of("tn_autreautre", [m.id]) == {}  # un autre locataire ne lit rien
    with psycopg.connect(pg["su"]) as conn:
        rows = conn.execute("SELECT tenant_id, load->'ram'->>'free_mb' FROM lokkal_machine_load WHERE machine_id = %s",
                            (m.id,)).fetchall()
    assert rows == [(tenant, "128")]
    with psycopg.connect(pg["app"]) as conn:  # nokker_app sans locataire : rien
        assert conn.execute("SELECT count(*) FROM lokkal_machine_load").fetchone()[0] == 0


@requires_pg
def test_gateway_login_store_on_postgres(pg):  # noqa: F811
    import base64
    import hashlib
    import uuid

    from nokker.cloud.gateway_auth import GatewayAuth, GrantError
    from nokker.cloud.link import Signer
    from nokker.cloud.pg import PgGatewayAuthStore

    svc = LinkService(store=PgLinkStore(pg["app"]))
    acc = f"us_gw{uuid.uuid4().hex[:8]}"
    d = svc.device(f"gw{uuid.uuid4().hex[:6]}")
    host = svc.approve(d["user_code"], acc, "tn_gw")["host"]
    svc.token(d["device_code"])
    ga = GatewayAuth(links=svc, signer=Signer.generate(), store=PgGatewayAuthStore(pg["app"]))
    verifier = "w" * 60
    ch = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    code = ga.authorize(acc, "tn_gw", host, ch)["code"]
    t1 = ga.token(code, verifier, host)
    with pytest.raises(GrantError):
        ga.token(code, verifier, host)  # usage unique
    t2 = ga.refresh(t1["refresh_token"], host)
    with pytest.raises(GrantError):
        ga.refresh(t1["refresh_token"], host)  # réutilisation → famille révoquée
    with pytest.raises(GrantError):
        ga.refresh(t2["refresh_token"], host)
    with psycopg.connect(pg["su"]) as conn:
        dump = str(conn.execute("SELECT * FROM lokkal_gateway_refresh").fetchall())
    assert t1["refresh_token"] not in dump and t2["refresh_token"] not in dump  # empreintes seulement


@requires_pg
def test_relay_audit_one_use_per_card_under_tenant_rls(pg):  # noqa: F811
    """Lot X20 : journal des appels relayés (migration 022) ; une carte ne sert qu'une fois par machine (index unique),
    invisible à un autre locataire et sans locataire (RLS forcée, rôle nokker_app) ; ni arguments ni résultat."""
    import uuid

    from nokker.cloud.relay import ApprovalReused, PgRelayAudit

    audit = PgRelayAudit(pg["app"])
    tenant, machine = f"tn_rel{uuid.uuid4().hex[:8]}", f"mc_{uuid.uuid4().hex[:16]}"
    entry = {"tenant_id": tenant, "machine_id": machine, "account_id": "us_rel", "connector": "mail",
             "tool": "mail_send", "effect": "send_external", "approval_id": "ap_1", "args_sha256": "0" * 64,
             "approval": {"id": "ap_1", "decided_via": "cli"}, "outcome": "started", "code": None}
    first = audit.begin(entry)
    with pytest.raises(ApprovalReused):
        audit.begin(entry)
    audit.begin({**entry, "machine_id": f"mc_{uuid.uuid4().hex[:16]}"})  # même id de carte, autre machine : à part
    audit.begin({**entry, "tool": "mail_search", "effect": "read", "approval_id": None, "approval": None})
    audit.begin({**entry, "tool": "mail_search", "effect": "read", "approval_id": None, "approval": None})
    audit.finish(tenant, first, "executed", None, "f" * 64)
    rows = audit.of_tenant(tenant)
    assert len(rows) == 4 and [r["outcome"] for r in rows if r["audit_id"] == first] == ["executed"]
    assert rows[0]["approval"] == {"id": "ap_1", "decided_via": "cli"}
    assert audit.of_tenant("tn_autreautre") == []
    with psycopg.connect(pg["app"]) as conn:  # nokker_app sans locataire : rien
        assert conn.execute("SELECT count(*) FROM lokkal_relay_calls").fetchone()[0] == 0
    with psycopg.connect(pg["su"]) as conn:
        assert conn.execute("SELECT count(*) FROM lokkal_relay_calls WHERE tenant_id = %s",
                            (tenant,)).fetchone()[0] == 4


@requires_pg
def test_relay_reads_the_machine_tenant_credentials_only(pg):  # noqa: F811
    """Lot X20 : le relais lit `connector_credentials` sous la RLS du locataire de la machine (rôle nokker_app) ; une
    machine d'un autre locataire ne voit rien, même en nommant le compte ; l'identifiant n'est jamais dans le journal."""
    import json
    import uuid

    from nokker.cloud.link import Machine
    from nokker.cloud.relay import ConnectorRelay, PgRelayAudit
    from nokker.connectors.crypto import SecretBox
    from nokker.connectors.store import PgConnectorStore
    from nokker.core.ports import ToolSpec
    from nokker.core.turn import Effect, ToolResult
    from nokker.tenancy.context import tenant_scope
    from tests.integration.test_rls import A, B

    secret = f"pw-relay-{uuid.uuid4().hex[:10]}"
    store = PgConnectorStore(pg["app"], SecretBox(SecretBox.generate_key()))
    acc = f"us_rl{uuid.uuid4().hex[:8]}"
    with tenant_scope(A):
        store.put(acc, "mail", {"preset": "gmail", "address": "a@example.com"}, secret)

    class Fake:
        def __init__(self, s):
            self.s = s

        def specs(self):
            return [ToolSpec(name="mail_search", description="", parameters={"type": "object"}, effect=Effect.READ)]

        def call(self, name, arguments, ctx):
            return ToolResult(call_id="", name=name, content=f"vu avec {self.s}")

    relay = ConnectorRelay(store=store, audit=PgRelayAudit(pg["app"]), build=lambda k, st, s, locality=None: Fake(s))
    mine = Machine(id=f"mc_{uuid.uuid4().hex[:16]}", account=acc, tenant=A, name="a", host="a.nokker.app",
                   token_sha256="", created_at=0.0)
    other = Machine(id=f"mc_{uuid.uuid4().hex[:16]}", account=acc, tenant=B, name="b", host="b.nokker.app",
                    token_sha256="", created_at=0.0)
    assert [c["id"] for c in relay.listing(mine)["connectors"]] == ["mail"]
    assert relay.listing(other) == {"connectors": []}
    status, out = relay.call(mine, "mail", {"tool": "mail_search", "arguments": {}})
    assert status == 200 and secret not in out["content"] and "***" in out["content"]
    assert relay.call(other, "mail", {"tool": "mail_search", "arguments": {}})[0] == 404
    with psycopg.connect(pg["su"]) as conn:
        dump = json.dumps(conn.execute("SELECT * FROM lokkal_relay_calls").fetchall(), default=str)
    assert secret not in dump and mine.id in dump


@requires_pg
def test_relayed_approvals_on_postgres_under_tenant_rls(pg):  # noqa: F811
    """Lot X10 : la boîte aux lettres des approbations fortes (021) avec le rôle du pod — dépôt, redépôt, passage à
    `forwarded` atomique et unique, relève, résultat, preuve refusée qui rouvre la carte ; invisible à un autre locataire
    et sans locataire (RLS forcée)."""
    import time
    import uuid

    from nokker.cloud.approvals import RelayError, RelayService
    from nokker.cloud.pg import PgRelayStore

    svc = LinkService(store=PgLinkStore(pg["app"]))
    acc, tenant = f"us_rel{uuid.uuid4().hex[:8]}", f"tn_rel{uuid.uuid4().hex[:8]}"
    d = svc.device(f"rel{uuid.uuid4().hex[:6]}")
    svc.approve(d["user_code"], acc, tenant)
    m = svc.machine_for_token(svc.token(d["device_code"])["machine_token"])
    relay = RelayService(store=PgRelayStore(pg["app"]))
    body = {"approval_id": "ap-1", "title": "terminal : rm -rf /srv", "reason": "destructif", "strength": "strong",
            "challenge": '{"approval_id":"ap-1"}', "expires_at": time.time() + 600}
    card, new = relay.deposit(m, body)
    assert new and card.status == "pending" and card.tenant == tenant
    created = card.created_at
    card, new = relay.deposit(m, {**body, "challenge": '{"approval_id":"ap-1","n":2}'})
    assert not new and card.created_at == created and card.challenge.endswith('"n":2}')
    assert [a["approval_id"] for a in relay.list(tenant, m.id)] == ["ap-1"]
    assert relay.list("tn_autreautre", m.id) == [] and relay.get("tn_autreautre", m.id, "ap-1") is None
    proof = {"device_id": "dev-1", "webauthn_client_data_json": "a", "webauthn_authenticator_data": "b",
             "webauthn_signature": "c", "pirate": "x"}
    out = relay.decide(tenant, m.id, "ap-1", {"decision": "approve", "proof": proof})
    assert out.status == "forwarded" and "pirate" not in out.proof
    with pytest.raises(RelayError) as e:
        relay.decide(tenant, m.id, "ap-1", {"decision": "reject", "proof": proof})
    assert e.value.status == 409  # une seule preuve en vol
    [fw] = relay.decisions(m, 0)
    assert fw.decision == "approve" and fw.proof["device_id"] == "dev-1"
    assert relay.result(m, "ap-1", {"status": "pending", "error": "preuve refusée"}).status == "pending"
    assert relay.decisions(m, 0) == [] and relay.get(tenant, m.id, "ap-1")["error"] == "preuve refusée"
    relay.decide(tenant, m.id, "ap-1", {"decision": "approve", "proof": proof})
    assert relay.result(m, "ap-1", {"status": "approved"}).status == "approved"
    assert relay.result(m, "ap-1", {"status": "approved", "outcome": "executed"}).outcome == "executed"
    with pytest.raises(RelayError):
        relay.deposit(m, body)  # décidée : on ne la rouvre pas
    with psycopg.connect(pg["su"]) as conn:
        rows = conn.execute("SELECT tenant_id, status, outcome, proof->>'device_id' FROM lokkal_relay_approvals "
                            "WHERE machine_id = %s", (m.id,)).fetchall()
    assert rows == [(tenant, "approved", "executed", "dev-1")]
    with psycopg.connect(pg["app"]) as conn:  # nokker_app sans locataire : rien
        assert conn.execute("SELECT count(*) FROM lokkal_relay_approvals").fetchone()[0] == 0


@requires_pg
def test_relayed_card_arguments_on_postgres_are_erased_after_decision_and_expiry(pg):  # noqa: F811
    """Lot X20c (migration 027) avec le rôle du pod : dépôt avec arguments confrontés au défi (menteur refusé), résumé
    rendu par nokker.ai, carte locale « machine » ; arguments effacés au statut final et à l'expiration, empreinte
    gardée ; RLS inchangée."""
    import json
    import time
    import uuid

    from nokker.approvals.crypto import args_sha256
    from nokker.cloud.approvals import RelayError, RelayService
    from nokker.cloud.pg import PgRelayStore

    svc = LinkService(store=PgLinkStore(pg["app"]))
    acc, tenant = f"us_wys{uuid.uuid4().hex[:8]}", f"tn_wys{uuid.uuid4().hex[:8]}"
    d = svc.device(f"wys{uuid.uuid4().hex[:6]}")
    svc.approve(d["user_code"], acc, tenant)
    m = svc.machine_for_token(svc.token(d["device_code"])["machine_token"])
    store = PgRelayStore(pg["app"])
    relay = RelayService(store=store)
    args = {"to": "anne@example.com", "subject": "Jeudi", "body": "mot de passe : Zorglub-2026! à garder"}

    def body(aid: str, arguments: dict | None, digest_of: dict, ttl: float = 600) -> dict:
        ch = json.dumps({"approval_id": aid, "aud": "nokker-approval", "tool": "mail_send",
                         "args_sha256": args_sha256(digest_of), "nonce": uuid.uuid4().hex, "exp": time.time() + ttl})
        out = {"approval_id": aid, "title": "lire mes mails", "strength": "strong", "expires_at": time.time() + ttl,
               "challenge": ch}
        return out if arguments is None else {**out, "arguments": arguments}

    with pytest.raises(RelayError) as e:
        relay.deposit(m, body("ap-1", {**args, "to": "x@evil.example"}, args))
    assert e.value.status == 400
    card, _ = relay.deposit(m, body("ap-1", args, args))
    assert card.summary_source == "nokker.ai" and card.arguments == args
    page = relay.get(tenant, m.id, "ap-1")
    assert page["summary"]["headline"].startswith("Envoyer un e-mail") and "Zorglub" not in json.dumps(page)
    local, _ = relay.deposit(m, body("ap-2", None, {"command": "rollback"}))
    assert relay.get(tenant, m.id, "ap-2")["summary_source"] == "machine" and local.arguments is None
    relay.decide(tenant, m.id, "ap-1", {"decision": "approve", "proof": {"device_id": "d", "signature": "s"}})
    assert store.get(tenant, m.id, "ap-1").arguments == args  # transmise : pas encore décidée
    out = relay.result(m, "ap-1", {"status": "approved"})
    assert out.arguments is None and out.args_sha256 == args_sha256(args) and out.summary["erased"]
    assert out.summary["lines"] == [] and out.summary["headline"].startswith("Envoyer")
    relay.deposit(m, body("ap-3", args, args, ttl=1))
    later = RelayService(store=store, clock=lambda: time.time() + 5)
    assert {a["approval_id"]: a["status"] for a in later.list(tenant, m.id)}["ap-3"] == "expired"
    with psycopg.connect(pg["su"]) as conn:
        rows = dict(conn.execute("SELECT approval_id, arguments IS NULL FROM lokkal_relay_approvals "
                                 "WHERE machine_id = %s", (m.id,)).fetchall())
        dump = str(conn.execute("SELECT * FROM lokkal_relay_approvals WHERE machine_id = %s", (m.id,)).fetchall())
    assert rows == {"ap-1": True, "ap-2": True, "ap-3": True} and "anne@example.com" not in dump
    assert relay.get("tn_autreautre", m.id, "ap-1") is None


@requires_pg
def test_handoffs_inventory_and_mirror_notes_on_postgres_under_tenant_rls(pg):  # noqa: F811
    """Lot X21 (migration 025) avec le rôle du pod : inventaire par noms (une ligne par machine), demandes de relais et
    leurs passages d'état atomiques (une seule exécution, un tour rangé une fois), notes en miroir ; rien n'est visible
    d'un autre locataire ni sans locataire (RLS forcée)."""
    import uuid

    import httpx

    from nokker.cloud.handoff import HandoffService
    from nokker.cloud.link import Signer
    from nokker.cloud.mirror import push_notes
    from nokker.cloud.pg import PgHandoffStore, PgMirrorNotes

    store = PgLinkStore(pg["app"])
    links = LinkService(store=store)
    acc, tenant = f"us_ho{uuid.uuid4().hex[:8]}", f"tn_ho{uuid.uuid4().hex[:8]}"
    d = links.device(f"ho{uuid.uuid4().hex[:6]}")
    links.approve(d["user_code"], acc, tenant)
    m = links.machine_for_token(links.token(d["device_code"])["machine_token"])
    assert links.report_resources(m, {"repos": ["monapp", "/etc/passwd"], "hosts": ["prod-db"]})
    links.report_resources(m, {"repos": ["monapp", "acme"], "hosts": ["prod-db"]})
    links.report_load(m, {"ram": {"total_mb": 1024, "free_mb": 512}, "cpu": {"count": 2, "load1": 0.1}})
    [mine] = links.machines_with_load(acc, tenant)
    assert mine.resources["repos"] == ["acme", "monapp"] and mine.resources_at
    assert store.resources_of("tn_autreautre", [m.id]) == {}

    signer = Signer.generate()
    seen = []

    def gateway(req):
        seen.append(req.url.path)
        if req.url.path == "/v1/local/sessions":
            return httpx.Response(201, json={"thread_id": "th_pg"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}],
                                         "nokker": {"turn_index": 0, "pending_approvals": 1}})

    svc = HandoffService(links=links, signer=signer, store=PgHandoffStore(pg["app"]), spawn=lambda fn: fn(),
                         client=httpx.Client(transport=httpx.MockTransport(gateway)))
    h, _ = svc.propose(acc, tenant, f"{tenant}:th_web", "repo", "monapp", "corrige")
    assert h.state == "proposed" and seen == []
    assert svc.list("tn_autreautre", acc) == [] and svc.list(tenant, "us_autre") == []
    svc.decide(tenant, acc, h.id, "accept")
    got = svc.get(tenant, acc, h.id)
    assert got.state == "awaiting_approval" and got.lokkal_thread == "th_pg" and seen == [
        "/v1/local/sessions", "/v1/chat/completions"]
    assert svc.store.transition(tenant, h.id, ("proposed",), state="accepted") is None  # une seule exécution
    assert svc.result(tenant, h.id, machine_id=m.id, reply="fini", turn_index=1).state == "done"
    assert svc.result(tenant, h.id, machine_id=m.id, reply="rejoué", turn_index=1) is None
    assert [x.id for x in svc.list(tenant, acc, f"{tenant}:th_web")] == [h.id]
    # Essais ratés faute de passerelle (migration 030, défaut 3 de la revue du train) : comptés en base.
    h2, _ = svc.propose(acc, tenant, f"{tenant}:th_web", "repo", "monapp", "relance")
    assert h2.attempts == 0
    assert svc.store.transition(tenant, h2.id, ("proposed",), state="waiting", attempts=2).attempts == 2
    assert svc.get(tenant, acc, h2.id).attempts == 2

    notes = PgMirrorNotes(pg["app"])
    out = push_notes(notes, tenant, m.id, {"notes": [
        {"project": "u-local", "name": "fb-langue", "locality": "both", "kind": "feedback", "text": "français"},
        {"project": "p-0123456789abcdef", "name": "rule-tests", "locality": "local", "text": "pytest"}]})
    assert out == {"accepted": 1, "refused": 1, "forgotten": 0}
    assert [n["name"] for n in notes.of_tenant(tenant)] == ["fb-langue"] and notes.of_tenant("tn_autreautre") == []
    with psycopg.connect(pg["app"]) as conn:  # nokker_app sans locataire : rien
        for table in ("lokkal_machine_resources", "lokkal_handoffs", "lokkal_mirror_notes"):
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0, table
    with psycopg.connect(pg["su"]) as conn:
        dump = str(conn.execute("SELECT resources FROM lokkal_machine_resources WHERE machine_id = %s",
                                (m.id,)).fetchall())
        assert "/etc/passwd" not in dump
        assert "pytest" not in str(conn.execute("SELECT text FROM lokkal_mirror_notes").fetchall())
    assert notes.erase(tenant, m.id) == 1 and notes.of_tenant(tenant) == []


def test_relay_strong_nonce_single_use_across_machines_and_tenants(pg):  # noqa: F811
    """Lot X20b (migration 026) : le nonce d'un défi signé ne sert qu'à UN appel exécuté, toutes machines et tous
    locataires confondus (l'index unique ne voit pas la RLS), alors que les lignes restent sous la RLS de leur
    locataire ; une ligne refusée (sans nonce) ne brûle rien."""
    import uuid

    from nokker.cloud.relay import ApprovalReused, PgRelayAudit

    audit = PgRelayAudit(pg["app"])
    ta, tb = f"tn_sa{uuid.uuid4().hex[:8]}", f"tn_sb{uuid.uuid4().hex[:8]}"
    nonce = uuid.uuid4().hex
    entry = {"tenant_id": ta, "machine_id": f"mc_{uuid.uuid4().hex[:16]}", "account_id": "us_sa", "connector": "mail",
             "tool": "mail_send", "effect": "send_external", "approval_id": "ap_s1", "args_sha256": "0" * 64,
             "approval": {"id": "ap_s1", "verified": "webauthn", "device_id": "dev_1"}, "outcome": "started",
             "code": None, "strong_nonce": nonce}
    for _ in range(2):  # refus : ni carte ni nonce, autant de fois qu'on veut
        audit.begin({**entry, "approval_id": None, "strong_nonce": None, "outcome": "refused",
                     "code": "strong_approval_required"})
    first = audit.begin(entry)
    with pytest.raises(ApprovalReused):  # autre machine du même locataire, même signature
        audit.begin({**entry, "machine_id": f"mc_{uuid.uuid4().hex[:16]}"})
    with pytest.raises(ApprovalReused):  # autre locataire, même signature
        audit.begin({**entry, "tenant_id": tb, "machine_id": f"mc_{uuid.uuid4().hex[:16]}"})
    audit.begin({**entry, "approval_id": "ap_s2", "strong_nonce": uuid.uuid4().hex})  # autre défi : passe
    rows = audit.of_tenant(ta)
    assert len(rows) == 4 and [r["strong_nonce"] for r in rows if r["audit_id"] == first] == [nonce]
    assert audit.of_tenant(tb) == []
    with psycopg.connect(pg["app"]) as conn:  # nokker_app sans locataire : rien
        assert conn.execute("SELECT count(*) FROM lokkal_relay_calls WHERE strong_nonce = %s",
                            (nonce,)).fetchone()[0] == 0


@requires_pg
def test_deck_and_placement_fields_on_postgres_under_tenant_rls(pg):  # noqa: F811
    """Lot X15 (migration 028) avec le rôle du pod : deck de chaque machine (une ligne, noms et états), invisible d'un
    autre locataire et sans locataire ; `needs` des agents, `machine_id` et `guest` des passages survivent."""
    import uuid

    from nokker.agents.pg import PgAgents
    from nokker.cloud.crew import CloudCrew, clean_deck
    from nokker.cloud.link import Signer
    from nokker.cloud.pg import PgDeckStore
    from nokker.tenancy.context import tenant_scope

    links = LinkService(store=PgLinkStore(pg["app"]))
    acc, tenant = f"us_dk{uuid.uuid4().hex[:8]}", f"tn_dk{uuid.uuid4().hex[:8]}"
    d = links.device(f"dk{uuid.uuid4().hex[:6]}")
    links.approve(d["user_code"], acc, tenant)
    m = links.machine_for_token(links.token(d["device_code"])["machine_token"])
    decks = PgDeckStore(pg["app"])
    crew = CloudCrew(links=links, signer=Signer.generate(), deck_store=decks)
    raw = {"sessions": [{"thread_id": "th_1", "name": "/home/p/monapp", "state": "working", "title": "privé"}],
           "agents": [{"id": "ag_" + "a" * 12, "name": "Tri", "status": "armed", "instructions": "mission"}],
           "pending_approvals": 2}
    assert crew.report_deck(m, raw) == clean_deck(raw, titles=False)
    crew.report_deck(m, {**raw, "pending_approvals": 3})  # une ligne courante, pas d'historique
    got = decks.decks_of(tenant, [m.id])[m.id][0]
    assert got["pending_approvals"] == 3 and got["sessions"][0] == {"thread_id": "th_1", "name": "monapp",
                                                                     "state": "working", "title": None}
    assert decks.decks_of("tn_autreautre", [m.id]) == {}
    deck = crew.deck(acc, tenant)
    assert deck["machines"][0]["machine_id"] == m.id and deck["machines"][0]["pending_approvals"] == 3
    with psycopg.connect(pg["app"]) as conn:  # nokker_app sans locataire : rien
        assert conn.execute("SELECT count(*) FROM lokkal_machine_deck").fetchone()[0] == 0
    with psycopg.connect(pg["su"]) as conn:
        dump = str(conn.execute("SELECT deck FROM lokkal_machine_deck WHERE machine_id = %s", (m.id,)).fetchall())
        assert "mission" not in dump and "/home/p" not in dump and "privé" not in dump

    agents = PgAgents(dsn=pg["app"])
    try:
        with tenant_scope(tenant):
            a = agents.create(name="Vidéo", role="x", model="", created_by=acc, placement={"machine": "auto"},
                              needs={"repos": ["monapp"], "hosts": [], "connectors": [], "mcp": [], "gpu": True})
            assert agents.get(a.id).needs["gpu"] is True and agents.get(a.id).placement == {"machine": "auto"}
            r = agents.new_run(a.id, "manual", status="running", machine_id=m.id)
            g = agents.new_run("ag_" + "b" * 12, "manual", guest={"id": "ag_" + "b" * 12, "name": "Invité"})
            assert agents.get_run(r.id).machine_id == m.id and agents.get_run(g.id).guest["name"] == "Invité"
        with tenant_scope("tn_autreautre"):
            assert agents.get(a.id) is None and agents.get_run(r.id) is None
    finally:
        agents.close()
