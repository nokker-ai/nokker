"""Deck multi-machines et agents placés (lot X15, docs/LOKKAL.md §6.7 et §7.19, docs/BUBBLES.md §9.5).

Une flotte complète en mémoire, sans réseau : nokker.ai (vrai moteur Crew, vrai `CloudCrew`) et N machines (chacune son
vrai moteur Crew, son vrai `LocalCrew`, son coffre, ses portes et ses cartes), reliées par une passerelle simulée qui
vérifie chaque jeton comme le noyau local (aucune requête réelle, faux modèle). On vérifie : `auto` sur la machine
choisie, `mc_…` qui attend sa machine, `web` chez nokker.ai, le déménagement refusé puis accepté (dans les deux sens),
le deck sans contenu privé, et le cloisonnement entre comptes."""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import httpx
import pytest

from nokker.agents.types import AgentConflict, AgentError, AgentMissing, AgentNotFound
from nokker.cloud.approvals import MemoryRelayStore, RelayService
from nokker.cloud.crew import CloudCrew, CloudPlacement, clean_deck
from nokker.cloud.link import LinkService, Signer, verify_token
from nokker.local.crew import LocalCrew, allows
from nokker.tenancy.context import INTERNAL_TENANT
from nokker.tenancy.store import MemoryTenancy
from tests.unit.agents.conftest import SECRET_VALUE, make_world


def _dir(path):
    path.mkdir(parents=True, exist_ok=True)
    return path


def load(free=8000, total=16000, count=8, load1=0.4, active=0, gpu=False):
    return {"v": 1, "ram": {"total_mb": total, "free_mb": free}, "cpu": {"count": count, "load1": load1},
            "gpus": [{"name": "NVIDIA GeForce RTX 3090", "memory_total_mb": 24576, "memory_free_mb": 22000,
                      "util_pct": 1}] if gpu else [], "sessions": 1, "active_turns": active}


class Fleet:
    """nokker.ai + des machines Lokkal, la passerelle entre les deux simulée (jetons vérifiés pour de vrai)."""

    def __init__(self, tmp_path, cloud_steps=()):
        self.tmp = tmp_path
        self.tenancy = MemoryTenancy()
        self.p = self.tenancy.create_account("pierre@example.ch")
        self.q = self.tenancy.create_account("quentin@example.ch")
        self.links, self.signer = LinkService(), Signer.generate()
        self.web_ok: set[str] = set()
        self.titles_ok: set[str] = set()
        self.down: set[str] = set()  # hôtes dont le tunnel est fermé
        self.seen: list[tuple[str, str, dict]] = []
        self.cloud = make_world(_dir(tmp_path / "cloud"), cloud_steps)
        self.relay = RelayService(store=MemoryRelayStore())
        self.crew = CloudCrew(links=self.links, signer=self.signer, web_allowed=lambda acc: acc in self.web_ok,
                              web_engine=True, titles_allowed=lambda acc: acc in self.titles_ok,
                              approvals_relay=self.relay, agent_tenant=lambda a: self.tenant_of(a.created_by),
                              client=httpx.Client(transport=httpx.MockTransport(self._gateway)))
        hub = self.cloud.agents
        hub.placement_policy = CloudPlacement(links=self.links, web_allowed=self.crew.web_allowed)
        hub.mover = self.crew
        self.cloud.engine.remote, self.crew.engine = self.crew, self.cloud.engine
        self.cloud.engine.tenant_of = lambda a: self.tenant_of(a.created_by) or INTERNAL_TENANT
        self.machines: dict[str, tuple] = {}

    def tenant_of(self, account):
        acc = self.tenancy.account(account) if account else None
        return acc.tenant_id if acc is not None else None

    def add_machine(self, account, name, *, steps=(), secrets=None, ld=None, repos=(), gpu=False):
        d = self.links.device(name)
        self.links.approve(d["user_code"], account.account_id, account.tenant_id)
        token = self.links.token(d["device_code"])["machine_token"]
        m = self.links.machine_for_token(token)
        mw = make_world(_dir(self.tmp / name), steps, secrets=secrets)
        lc = LocalCrew(hub=mw.agents, token=lambda: token, sync=True, approvals=mw.approvals,
                       client=httpx.Client(transport=httpx.MockTransport(self._cloud_routes)))
        mw.engine.on_guest = lc.report
        self.links.report_load(m, ld or load(gpu=gpu))
        self.links.report_resources(m, {"repos": list(repos)})
        self.machines[m.host] = (m, mw, lc, token)
        return m, mw, lc

    def offline(self, m):
        tenant, ld, _ = self.links.store.loads[m.id]
        self.links.store.loads[m.id] = (tenant, ld, time.time() - 3600)

    def _gateway(self, req: httpx.Request) -> httpx.Response:
        """nokker.ai → machine, par la passerelle : le noyau local vérifie le jeton et filtre la route."""
        m, _mw, lc, _ = self.machines[req.url.host]
        if req.url.host in self.down:
            raise httpx.ConnectError("tunnel fermé")
        claims = verify_token(req.headers["Authorization"][7:], self.signer.jwks(), machine=m.id)
        assert claims["scope"] == "lokkal:session" and claims["machine"] == m.id
        self.seen.append((req.url.host, req.url.path, claims))
        out = lc.handle("POST", req.url.path, json.loads(req.content or b"{}"), claims)
        status, body = out or (404, {"error": {"message": "not found"}})
        return httpx.Response(status, json=body)

    def _cloud_routes(self, req: httpx.Request) -> httpx.Response:
        """machine → nokker.ai : `POST /v1/gateway/agent-runs/{id}/result`, jeton de machine."""
        machine = self.links.machine_for_token(req.headers["Authorization"][7:])
        assert machine is not None and req.url.path.endswith("/result")
        status, body = self.crew.result(machine, req.url.path.split("/")[4], json.loads(req.content))
        return httpx.Response(status, json=body)

    def agent(self, account, **body):
        base = {"name": "Veille", "role": "scout", "instructions": "Cherche les offres du jour."}
        return self.cloud.agents.create_from_body({**base, **body}, account.account_id)


# ─────────────────────────────── auto, mc_, web ───────────────────────────────


def test_auto_runs_on_the_chosen_machine_with_its_rules_and_the_result_comes_back(tmp_path):
    f = Fleet(tmp_path)
    m1, w1, _ = f.add_machine(f.p, "atelier", steps=[("text", "Rapport : 3 offres retenues.")],
                              ld=load(free=12000), repos=["monapp"])
    _m2, w2, _ = f.add_machine(f.p, "portable", ld=load(free=30000), repos=["autre"])  # plus de RAM, pas le dépôt
    a = f.agent(f.p, placement={"machine": "auto"}, needs={"repos": ["monapp"]}, budget={"max_calls": 5})
    assert a.placement == {"machine": "auto"} and a.needs["repos"] == ["monapp"] and a.status == "armed"
    run = f.cloud.agents.run_now(a.id, f.p.account_id)
    done = f.cloud.run(run.id)
    assert done.status == "succeeded" and done.summary == "Rapport : 3 offres retenues."
    assert done.machine_id == m1.id and done.thread_id == f"th_{run.id}"  # le fil vit sur la machine
    assert done.cost["tokens_in"] == 1000 and done.cost["currency"] == "EUR"  # coût rendu par la machine
    assert f.cloud.model.calls == 0  # rien n'a tourné chez nokker.ai
    host, path, claims = f.seen[0]
    assert (host, path) == (m1.host, "/v1/local/agent-runs") and claims["crew"] == {"op": "run", "id": run.id}
    # sur la machine : un passage invité, dans un fil marqué, sans agent posé chez elle
    guest = w1.run(run.id)
    assert guest.guest["id"] == a.id and guest.status == "succeeded" and w1.agents.list_agents() == []
    t = w1.bubbles.threads.get(guest.thread_id)
    assert t.origin == {"kind": "agent", "agent_id": a.id, "run_id": run.id, "from": "nokker.ai"}
    assert w2.model.calls == 0
    assert f.cloud.agents.get_agent(a.id).last_run["status"] == "succeeded"


def test_machine_applies_its_own_gates_and_cards_then_the_result_returns_to_the_run(tmp_path):
    f = Fleet(tmp_path)
    m1, w1, _ = f.add_machine(f.p, "atelier", steps=[("tool", "note_write", {"t": "x"}), ("text", "j'attends"),
                                                      ("text", "Noté.")])
    # la règle d'écriture levée chez nokker.ai ne vaut rien sur la machine : sa carte à elle
    a = f.agent(f.p, placement={"machine": m1.id}, auto_approve=["note_write"])
    assert a.status == "armed"  # placé sur une machine : pas de carte d'armement (rien n'y serait levé)
    run = f.cloud.agents.run_now(a.id, f.p.account_id)
    assert f.cloud.run(run.id).status == "waiting_approval"
    assert "carte" in f.cloud.run(run.id).summary
    f.cloud.engine.tick()  # l'ordonnanceur de nokker.ai ne balaie pas une attente dont la carte est sur la machine
    assert f.cloud.run(run.id).status == "waiting_approval"
    guest = w1.run(run.id)
    (card,) = [c for c in w1.approvals.pending() if c.thread_id == guest.thread_id]
    w1.decide(card.id)  # décidée sur la machine (ou au téléphone, X10)
    done = f.cloud.run(run.id)
    assert done.status == "succeeded" and done.summary == "Noté." and w1.tools.calls[-1][0] == "note_write"


def test_explicit_machine_waits_for_it_then_runs_when_its_load_returns(tmp_path):
    f = Fleet(tmp_path)
    m1, _w1, _ = f.add_machine(f.p, "atelier", steps=[("text", "fait")], ld=load(free=2000))
    f.add_machine(f.p, "portable", ld=load(free=60000))  # meilleure, mais ce n'est pas celle demandée
    a = f.agent(f.p, placement={"machine": m1.id})
    f.offline(m1)
    run = f.cloud.agents.run_now(a.id, f.p.account_id)
    waiting = f.cloud.run(run.id)
    assert waiting.status == "queued" and waiting.summary.startswith("en attente d'une machine : « atelier » hors")
    assert f.seen == [] and f.cloud.agents.get_agent(a.id).status == "running"  # la carte respire
    with pytest.raises(AgentConflict):
        f.cloud.agents.run_now(a.id, f.p.account_id)  # un passage vivant à la fois
    f.links.report_load(m1, load())
    assert f.crew.machine_online(m1) == 1
    assert f.cloud.run(run.id).status == "succeeded" and f.cloud.run(run.id).machine_id == m1.id


def test_a_run_that_waits_too_long_fails_and_a_machine_missing_a_secret_skips_it(tmp_path):
    f = Fleet(tmp_path)
    m1, _, _ = f.add_machine(f.p, "atelier")
    a = f.agent(f.p, placement={"machine": m1.id})
    f.offline(m1)
    run = f.cloud.agents.run_now(a.id, f.p.account_id)
    f.cloud.clock.t += 25 * 3600
    f.cloud.engine.retry_remote()
    assert f.cloud.run(run.id).status == "failed" and "aucune machine depuis 24 h" in f.cloud.run(run.id).summary
    f.links.report_load(m1, load())
    b = f.agent(f.p, name="Facturation", placement={"machine": m1.id}, secrets=["API_TOKEN"])
    run = f.cloud.agents.run_now(b.id, f.p.account_id)
    got = f.cloud.run(run.id)
    assert got.status == "skipped" and "API_TOKEN" in got.summary and SECRET_VALUE not in got.summary


def test_auto_tries_the_next_machine_when_the_best_one_lacks_the_secret(tmp_path):
    f = Fleet(tmp_path)
    m1, w1, _ = f.add_machine(f.p, "gros", ld=load(free=40000))  # pas le secret
    m2, _w2, _ = f.add_machine(f.p, "petit", steps=[("text", "ok")], secrets={"API_TOKEN": SECRET_VALUE},
                              ld=load(free=4000))
    a = f.agent(f.p, placement={"machine": "auto"}, secrets=["API_TOKEN"])
    run = f.cloud.agents.run_now(a.id, f.p.account_id)
    assert f.cloud.run(run.id).machine_id == m2.id and f.cloud.run(run.id).status == "succeeded"
    assert [h for h, _, _ in f.seen] == [m1.host, m2.host] and w1.model.calls == 0


def test_gpu_agent_goes_to_the_gpu_machine_and_waits_without_one(tmp_path):
    f = Fleet(tmp_path)
    f.add_machine(f.p, "serveur", ld=load(free=64000))
    m2, _w2, _ = f.add_machine(f.p, "atelier", steps=[("text", "vidéo rendue")], ld=load(free=8000, gpu=True))
    a = f.agent(f.p, name="Vidéo", placement={"machine": "auto"}, needs={"gpu": True})
    run = f.cloud.agents.run_now(a.id, f.p.account_id)
    assert f.cloud.run(run.id).machine_id == m2.id and f.cloud.run(run.id).summary == "vidéo rendue"
    f.offline(m2)
    run = f.cloud.agents.run_now(a.id, f.p.account_id)
    waiting = f.cloud.run(run.id)  # la machine GPU éteinte : on l'attend, la machine sans GPU ne la remplace pas
    assert waiting.status == "queued" and "« atelier » (hors ligne" in waiting.summary and "serveur" not in waiting.summary


def test_web_placement_is_an_offer_and_runs_at_nokker_ai(tmp_path):
    f = Fleet(tmp_path, cloud_steps=[("text", "veille faite au web")])
    with pytest.raises(AgentError, match="réservés aux abonnés"):
        f.agent(f.p, placement={"machine": "web"})
    f.web_ok.add(f.p.account_id)
    a = f.agent(f.p, placement={"machine": "web"})
    run = f.cloud.agents.run_now(a.id, f.p.account_id)
    assert f.cloud.run(run.id).status == "succeeded" and f.cloud.run(run.id).machine_id is None
    assert f.cloud.model.calls == 1 and f.seen == []
    # moteur web éteint (NOKKER_CREW_ENGINE absent) : rien ne tourne ici, et on le dit
    from nokker.agents.hub import EngineUnavailable

    f.cloud.engine.local_runs = False
    with pytest.raises(EngineUnavailable, match="NOKKER_CREW_ENGINE"):
        f.cloud.agents.run_now(a.id, f.p.account_id)


def test_placement_on_a_machine_of_another_account_is_refused(tmp_path):
    f = Fleet(tmp_path)
    mq, _, _ = f.add_machine(f.q, "ailleurs")
    with pytest.raises(AgentError, match="inconnue de ce compte"):
        f.agent(f.p, placement={"machine": mq.id})
    with pytest.raises(AgentError):
        f.agent(f.p, placement={"machine": "n'importe-quoi"})
    with pytest.raises(AgentError, match="nom invalide"):
        f.agent(f.p, needs={"repos": ["/home/p/monapp"]})


# ─────────────────────────────── déménagement ───────────────────────────────


def test_move_refused_with_the_list_of_what_is_missing_then_accepted(tmp_path):
    f = Fleet(tmp_path, cloud_steps=[("text", "web")])
    f.web_ok.add(f.p.account_id)
    m1, w1, _ = f.add_machine(f.p, "atelier", steps=[("text", "fait sur la machine")])
    a = f.agent(f.p, placement={"machine": "web"}, secrets=["API_TOKEN"], needs={"repos": ["monapp"], "gpu": True})
    with pytest.raises(AgentMissing) as e:
        f.cloud.agents.move(a.id, m1.id, f.p.account_id, f.p.tenant_id)
    kinds = {(x["kind"], x.get("name")) for x in e.value.missing}
    assert kinds == {("repo", "monapp"), ("gpu", None), ("secret", "API_TOKEN")}
    assert all(SECRET_VALUE not in x["message"] for x in e.value.missing)
    assert f.seen[-1][1] == "/v1/local/agents/check" and f.seen[-1][2]["crew"]["op"] == "check"
    assert f.cloud.agents.get_agent(a.id).placement == {"machine": "web"}  # rien n'a bougé
    # l'utilisateur pose ce qui manque à la destination : dépôt, GPU, secret dans le coffre de la machine
    f.links.report_resources(m1, {"repos": ["monapp"]})
    f.links.report_load(m1, load(gpu=True))
    w1.vault.values["API_TOKEN"] = SECRET_VALUE
    moved = f.cloud.agents.move(a.id, m1.id, f.p.account_id, f.p.tenant_id)
    assert moved.placement == {"machine": m1.id}
    run = f.cloud.agents.run_now(a.id, f.p.account_id)
    assert f.cloud.run(run.id).summary == "fait sur la machine" and f.cloud.run(run.id).machine_id == m1.id
    # retour au web refusé : il exige un dépôt et un GPU qui n'existent que sur une machine
    with pytest.raises(AgentMissing) as e:
        f.cloud.agents.move(a.id, "web", f.p.account_id, f.p.tenant_id)
    assert {x["kind"] for x in e.value.missing} >= {"repo", "gpu", "secret"}
    with pytest.raises(AgentError):
        f.cloud.agents.move(a.id, "ailleurs", f.p.account_id, f.p.tenant_id)


def test_move_a_machine_agent_to_another_machine_keeps_its_history_where_it_ran(tmp_path):
    f = Fleet(tmp_path)
    m1, w1, lc1 = f.add_machine(f.p, "atelier", steps=[("text", "CONFIDENTIEL : salaire de Paul")])
    m2, _w2, _ = f.add_machine(f.p, "portable", steps=[("text", "fait sur le portable")],
                              secrets={"API_TOKEN": SECRET_VALUE})
    local = w1.agents.create_from_body({"name": "Paie", "role": "rh", "instructions": "x", "secrets": ["API_TOKEN"]},
                                       "local-owner")
    w1.vault.values["API_TOKEN"] = SECRET_VALUE
    w1.agents.run_now(local.id, "local-owner")
    assert w1.runs(local.id)[0].status == "succeeded"
    f.crew.report_deck(m1, lc1.snapshot())
    with pytest.raises(AgentMissing) as e:  # vers le web : pas d'offre, et un secret sans coffre
        f.cloud.agents.move(local.id, "web", f.p.account_id, f.p.tenant_id)
    assert {x["kind"] for x in e.value.missing} == {"offer", "secret"}
    assert w1.agents.get_agent(local.id).armed  # rien n'a bougé sur la machine
    moved = f.cloud.agents.move(local.id, m2.id, f.p.account_id, f.p.tenant_id)
    assert moved.id == local.id and moved.placement == {"machine": m2.id} and moved.status == "armed"
    assert [(h, p) for h, p, _ in f.seen][-3:] == [(m1.host, f"/v1/local/agents/{local.id}/export"),
                                                    (m2.host, "/v1/local/agents/check"),  # ses secrets, par nom
                                                    (m1.host, f"/v1/local/agents/{local.id}/released")]
    tomb = w1.agents.get_agent(local.id)  # pierre tombale : désarmé, marqué déménagé, ses passages restent
    assert not tomb.armed and tomb.placement["machine"] == m2.id and tomb.placement["moved_at"]
    assert w1.runs(local.id)[0].summary.startswith("CONFIDENTIEL") and f.cloud.runs(local.id) == []
    with pytest.raises(AgentConflict):
        w1.agents.run_now(local.id, "local-owner")  # il ne tourne plus là-bas
    assert all(x["id"] != local.id for x in lc1.snapshot()["agents"])  # ni dans le deck de la machine
    run = f.cloud.agents.run_now(local.id, f.p.account_id)
    assert f.cloud.run(run.id).machine_id == m2.id and f.cloud.run(run.id).summary == "fait sur le portable"


def test_machine_that_does_not_confirm_leaves_everything_in_place(tmp_path):
    f = Fleet(tmp_path)
    m1, w1, lc1 = f.add_machine(f.p, "atelier")
    m2, _, _ = f.add_machine(f.p, "portable")
    local = w1.agents.create_from_body({"name": "Tri", "role": "x"}, "local-owner")
    f.crew.report_deck(m1, lc1.snapshot())
    real = lc1.handle

    def refuse_release(method, path, body, claims):
        return (500, {"error": {"message": "panne"}}) if path.endswith("/released") else real(method, path, body,
                                                                                               claims)

    lc1.handle = refuse_release
    with pytest.raises(AgentConflict, match="rien n'a bougé"):
        f.cloud.agents.move(local.id, m2.id, f.p.account_id, f.p.tenant_id)
    with pytest.raises(AgentNotFound):
        f.cloud.agents.get_agent(local.id)
    assert w1.agents.get_agent(local.id).armed and not f.cloud.agents.moved_to(w1.agents.get_agent(local.id))


# ─────────────────────────────── deck ───────────────────────────────


def test_deck_aggregates_machines_by_names_and_states_never_private_content(tmp_path):
    f = Fleet(tmp_path)
    m1, w1, lc1 = f.add_machine(f.p, "atelier", steps=[("text", "CONFIDENTIEL-42 : la réponse privée")],
                                ld=load(free=9000, load1=2.0, gpu=True))
    m2, _, _lc2 = f.add_machine(f.p, "portable")
    f.offline(m2)
    lc1.sessions = lambda: {"th_s1": "/home/pierre/projets/monapp", "th_s2": "/home/pierre/acme"}
    lc1.active = lambda: {"th_s1"}
    lc1.title_of = lambda tid: "Corriger la facture CONFIDENTIEL-42"
    local = w1.agents.create_from_body({"name": "Tri du courrier", "role": "x"}, "local-owner")
    w1.agents.run_now(local.id, "local-owner")  # sa réponse privée reste sur la machine
    on_m1 = f.agent(f.p, name="Placé", placement={"machine": m1.id})
    auto = f.agent(f.p, name="Partout", placement={"machine": "auto"}, trigger={"kind": "schedule", "cron": "0 7 * * *"})
    f.web_ok.add(f.p.account_id)
    web = f.agent(f.p, name="Au web", placement={"machine": "web"})
    f.crew.report_deck(m1, lc1.snapshot())
    deck = f.crew.deck(f.p.account_id, f.p.tenant_id, f.cloud.agents)
    blob = json.dumps(deck, ensure_ascii=False)
    assert "CONFIDENTIEL" not in blob and "/home/pierre" not in blob and "Cherche les offres" not in blob
    one, two = deck["machines"]
    assert (one["name"], one["online"], two["name"], two["online"]) == ("atelier", True, "portable", False)
    assert one["load"]["ram"] == {"total_mb": 16000, "free_mb": 9000} and one["load"]["cpu"]["rel"] == 0.25
    assert one["load"]["gpus"][0]["name"].startswith("NVIDIA")
    assert one["sessions"] == [{"thread_id": "th_s1", "name": "monapp", "state": "working", "title": None},
                               {"thread_id": "th_s2", "name": "acme", "state": "idle", "title": None}]
    cards = {c["name"]: c for c in one["agents"]}
    assert cards["Tri du courrier"]["defined_at"] == "machine" and cards["Tri du courrier"]["last_run"]["status"] == \
        "succeeded"
    assert cards["Placé"]["defined_at"] == "nokker.ai" and cards["Placé"]["placement"] == m1.id
    assert [c["name"] for c in deck["auto"]["agents"]] == ["Partout"] and deck["auto"]["agents"][0]["trigger"] == \
        "schedule"
    assert [c["id"] for c in deck["web"]["agents"]] == [web.id] and deck["web"]["engine"] is True
    assert two["sessions"] == [] and two["deck_at"] is None
    # titres : seulement si la machine les envoie (fils en miroir) ET si le compte a le miroir
    lc1.mirrored = lambda: True
    f.crew.report_deck(m1, lc1.snapshot())
    assert f.crew.deck(f.p.account_id, f.p.tenant_id, f.cloud.agents)["machines"][0]["sessions"][0]["title"] is None
    f.titles_ok.add(f.p.account_id)
    f.crew.report_deck(m1, lc1.snapshot())
    title = f.crew.deck(f.p.account_id, f.p.tenant_id, f.cloud.agents)["machines"][0]["sessions"][0]["title"]
    assert title == "Corriger la facture CONFIDENTIEL-42"
    assert on_m1.id and auto.id


def test_deck_cards_waiting_are_counted_and_strong_ones_link_to_the_phone(tmp_path):
    f = Fleet(tmp_path)
    m1, w1, lc1 = f.add_machine(f.p, "atelier", steps=[("tool", "wipe", {}), ("text", "x")])
    local = w1.agents.create_from_body({"name": "Ménage", "role": "x"}, "local-owner")
    w1.agents.run_now(local.id, "local-owner")
    assert len(w1.approvals.pending()) == 1
    f.relay.deposit(m1, {"approval_id": "ap_1", "title": "wipe : /", "strength": "strong",
                         "expires_at": time.time() + 600, "challenge": "c" * 32})
    f.crew.report_deck(m1, lc1.snapshot())
    one = f.crew.deck(f.p.account_id, f.p.tenant_id, f.cloud.agents)["machines"][0]
    assert one["pending_approvals"] == 1 and one["agents"][0]["status"] == "running"
    assert one["strong_approvals"][0]["approval_id"] == "ap_1"
    assert one["strong_approvals"][0]["url"].endswith(f"/approve/{m1.id}/ap_1")
    assert "wipe" not in json.dumps(one)  # la carte : un lien, pas son texte


def test_clean_deck_keeps_names_and_states_only():
    raw = {"sessions": [{"thread_id": "th_1", "name": "/home/p/secret/monapp", "state": "pense", "title": "t",
                         "messages": ["conversation"]}, {"thread_id": "../x"}],
           "agents": [{"id": "ag_" + "a" * 12, "name": "A", "status": "volant", "instructions": "mission",
                       "summary": "réponse"}, {"id": "pas-un-id"}], "pending_approvals": "beaucoup", "turns": [1]}
    d = clean_deck(raw, titles=False)
    assert d["sessions"] == [{"thread_id": "th_1", "name": "monapp", "state": "idle", "title": None}]
    assert d["agents"] == [{"id": "ag_" + "a" * 12, "name": "A", "status": "paused", "trigger": "manual",
                            "next_run_at": None, "last_run": None}]
    assert d["pending_approvals"] == 0 and "turns" not in d and clean_deck("x", titles=True) is None


# ─────────────────────────────── cloisonnement ───────────────────────────────


def test_accounts_are_isolated_everywhere(tmp_path):
    f = Fleet(tmp_path)
    mp, _wp, lcp = f.add_machine(f.p, "atelier", steps=[("text", "ok")])
    mq, wq, lcq = f.add_machine(f.q, "ailleurs")
    a = f.agent(f.p, placement={"machine": mp.id})
    run = f.cloud.agents.run_now(a.id, f.p.account_id)
    wq.agents.create_from_body({"name": "Secret de Q", "role": "x"}, "local-owner")
    f.crew.report_deck(mq, lcq.snapshot())
    f.crew.report_deck(mp, lcp.snapshot())
    dq = f.crew.deck(f.q.account_id, f.q.tenant_id, f.cloud.agents)
    assert [m["name"] for m in dq["machines"]] == ["ailleurs"] and a.id not in json.dumps(dq)
    dp = f.crew.deck(f.p.account_id, f.p.tenant_id, f.cloud.agents)
    assert "Secret de Q" not in json.dumps(dp) and [m["name"] for m in dp["machines"]] == ["atelier"]
    # un passage ne prend de résultat que de SA machine
    assert f.crew.result(mq, run.id, {"status": "failed"})[0] == 404
    assert f.crew.result(mp, run.id, {"status": "volé"})[0] in (200, 400)
    # déménager l'agent d'un autre compte : il n'existe pas
    q_agent = f.agent(f.q, name="De Q")
    with pytest.raises(AgentNotFound):
        f.cloud.agents.move(q_agent.id, mp.id, f.p.account_id, f.p.tenant_id)
    with pytest.raises(AgentNotFound):  # ni celui posé sur une machine de Q
        f.cloud.agents.move(wq.agents.list_agents()[0].id, "auto", f.p.account_id, f.p.tenant_id)


def test_crew_tokens_open_their_operation_only():
    run_claims = {"crew": {"op": "run", "id": "ar_" + "1" * 16}}
    assert allows("POST", "/v1/local/agent-runs", run_claims["crew"])
    assert allows("POST", f"/v1/local/agent-runs/ar_{'1' * 16}/cancel", run_claims["crew"])
    assert not allows("POST", f"/v1/local/agent-runs/ar_{'2' * 16}/cancel", run_claims["crew"])
    assert not allows("POST", "/v1/local/sessions", run_claims["crew"])
    assert not allows("GET", "/v1/local/agent-runs", run_claims["crew"])
    move = {"op": "move", "id": "ag_" + "a" * 12}
    assert allows("POST", f"/v1/local/agents/ag_{'a' * 12}/export", move)
    assert not allows("POST", f"/v1/local/agents/ag_{'b' * 12}/export", move)
    assert not allows("POST", "/v1/local/agents/check", move)
    assert allows("POST", "/v1/local/agents/check", {"op": "check", "id": "ag_x"})
    assert not allows("POST", "/v1/local/agent-runs", {"op": "check", "id": "ar_" + "1" * 16})


# ─────────────────────────────── routes HTTP (nokker.ai et machine) ───────────────────────────────


def _call(port, method, path, body=None, token=None, headers=None, host=None):
    h = {"Content-Type": "application/json", **(headers or {})}
    if token:
        h["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, headers=h,
                                 data=json.dumps(body).encode() if body is not None else None)
    if host:
        req.add_unredirected_header("Host", host)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


@pytest.fixture
def cloud_http(tmp_path):
    from nokker.api.server import make_handler
    from nokker.cloud import http as cloud_http_mod
    from nokker.cloud.http import CloudLink

    f = Fleet(tmp_path)
    sessions = {"Bearer sess-p": f.p.account_id, "Bearer sess-q": f.q.account_id}
    cloud_http_mod.configure(CloudLink(
        links=f.links, signer=f.signer, identity=lambda h: sessions.get(h.headers.get("Authorization")),
        tenant_of=f.tenant_of, router_base="http://127.0.0.1:1", router_key=lambda: "x", crew=f.crew,
        agents=f.cloud.agents))
    keys = {"sess-p": f.p.account_id, "sess-q": f.q.account_id}
    handler = make_handler(f.cloud.kernel, keys, None, approvals=f.cloud.approvals, hub=f.cloud.bubbles,
                           agents=f.cloud.agents, tenancy=f.tenancy)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f, httpd.server_address[1]
    cloud_http_mod.configure(None)
    httpd.shutdown()


def test_routes_deck_machine_deck_run_result_and_move(cloud_http):
    f, port = cloud_http
    m1, _w1, lc1 = f.add_machine(f.p, "atelier", steps=[("text", "ok")])
    tok = f.machines[m1.host][3]
    assert _call(port, "POST", "/v1/gateway/machine/deck", lc1.snapshot())[0] == 401
    assert _call(port, "POST", "/v1/gateway/machine/deck", "pas un deck", token=tok)[0] == 400
    assert _call(port, "POST", "/v1/gateway/machine/deck", lc1.snapshot(), token=tok)[0] == 200
    assert _call(port, "GET", "/v1/link/deck")[0] == 401
    st, deck = _call(port, "GET", "/v1/link/deck", token="sess-p")
    assert st == 200 and deck["machines"][0]["machine_id"] == m1.id
    assert _call(port, "GET", "/v1/link/deck", token="sess-q")[1]["machines"] == []
    # agent placé, déménagé par la route ; refus = 409 avec la liste des manques
    st, a = _call(port, "POST", "/v1/agents", {"name": "V", "role": "x", "placement": {"machine": "auto"},
                                               "needs": {"repos": ["absent"]}}, token="sess-p")
    assert st == 201 and a["placement"] == {"machine": "auto"} and a["needs"]["repos"] == ["absent"]
    st, body = _call(port, "POST", f"/v1/agents/{a['id']}/move", {"to": m1.id}, token="sess-p")
    assert st == 409 and body["error"]["missing"][0] == {"kind": "repo", "name": "absent",
                                                         "message": "dépôt « absent » absent de « atelier »"}
    assert _call(port, "POST", f"/v1/agents/{a['id']}/move", {"to": m1.id}, token="sess-q")[0] == 404
    st, b = _call(port, "POST", "/v1/agents", {"name": "W", "role": "x", "placement": {"machine": m1.id}},
                  token="sess-p")
    st, moved = _call(port, "POST", f"/v1/agents/{b['id']}/move", {"to": "auto"}, token="sess-p")
    assert st == 200 and moved["placement"] == {"machine": "auto"}
    # résultat d'un passage : seulement la machine qui l'a reçu
    st, out = _call(port, "POST", f"/v1/agents/{b['id']}/run", {}, token="sess-p")
    run_id = out["run_id"]
    assert f.cloud.run(run_id).status == "succeeded"
    assert _call(port, "GET", f"/v1/agents/{b['id']}/runs", token="sess-p")[1]["runs"][0]["machine_id"] == m1.id
    mq, _, _ = f.add_machine(f.q, "ailleurs")
    assert _call(port, "POST", f"/v1/gateway/agent-runs/{run_id}/result", {"status": "failed"},
                 token=f.machines[mq.host][3])[0] == 404
    assert _call(port, "POST", f"/v1/gateway/agent-runs/{run_id}/result", {"status": "failed"}, token=tok)[1] == {
        "stored": False, "status": "succeeded"}  # déjà fini : rien ne change


@pytest.fixture
def machine_http(tmp_path):
    """Une machine Lokkal derrière la passerelle : le vrai Handler filtre les jetons relayés (parse_request)."""
    from nokker.api.server import make_handler
    from nokker.local.api import LOCAL_OWNER, LocalApi
    from nokker.local.setup import LocalSetup, LocalVault

    root = tmp_path / "projets"
    (root / "monapp" / ".git").mkdir(parents=True)
    api = LocalApi.from_config({"token_path": str(tmp_path / "tok"), "state_dir": str(tmp_path / "state"),
                                "code": {"roots": [str(root)], "backend": "local"}}, port=8742)
    signer = Signer.generate()
    api.setup = LocalSetup(state_path=tmp_path / "setup.json", vault=LocalVault(tmp_path / "v.bin"), detect=list)
    api.setup.state.update(jwks=signer.jwks(), account="us_p", machine_id="mc_" + "1" * 16)

    class Up:
        state = "up"

    api.relay_host, api.tunnel = "atelier.nokker.app", Up()
    w = make_world(_dir(tmp_path / "w"), [("text", "passage fait")])
    reports = []
    api.crew = LocalCrew(hub=w.agents, sync=True, token=lambda: None, approvals=w.approvals)
    w.engine.on_guest = lambda r: reports.append(r.status)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(w.kernel, {api.token: LOCAL_OWNER}, local_api=api))
    api.allowed_hosts = frozenset({f"127.0.0.1:{httpd.server_address[1]}"})
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield api, httpd.server_address[1], signer, w, reports
    httpd.shutdown()


def test_machine_gateway_lets_a_crew_token_reach_its_operation_and_nothing_else(machine_http):
    api, port, signer, w, reports = machine_http
    mid, run_id = "mc_" + "1" * 16, "ar_" + "9" * 16
    relay = {"X-Nokker-Relay": "1"}
    tok = signer.delegation_token("us_p", "tn_p", machine=mid, web_session="crew", crew={"op": "run", "id": run_id})
    body = {"run_id": run_id, "agent": {"id": "ag_" + "c" * 12, "name": "Veille", "role": "x", "instructions": "go"}}
    for method, path in (("POST", "/v1/local/sessions"), ("GET", "/v1/local/secrets"), ("GET", "/v1/threads"),
                         ("POST", "/v1/local/agents/check"), ("POST", "/v1/chat/completions")):
        assert _call(port, method, path, {} if method == "POST" else None, token=tok, headers=relay,
                     host="atelier.nokker.app")[0] == 403, path
    st, out = _call(port, "POST", "/v1/local/agent-runs", body, token=tok, headers=relay, host="atelier.nokker.app")
    assert st == 202 and out["run_id"] == run_id
    assert w.run(run_id).status == "succeeded" and reports == ["running", "succeeded"]
    assert w.run(run_id).summary == "passage fait"
    other = signer.delegation_token("us_p", "tn_p", machine=mid, web_session="crew",
                                    crew={"op": "run", "id": "ar_" + "8" * 16})
    assert _call(port, "POST", "/v1/local/agent-runs", body, token=other, headers=relay,
                 host="atelier.nokker.app")[0] == 400  # le jeton nomme un autre passage
    # un jeton de session déléguée (X21) n'ouvre pas les routes des agents ; le jeton local non plus
    session = signer.delegation_token("us_p", "tn_p", machine=mid, web_session="tn_p:th_web")
    assert _call(port, "POST", "/v1/local/agent-runs", body, token=session, headers=relay,
                 host="atelier.nokker.app")[0] == 403
    assert _call(port, "POST", "/v1/local/agent-runs", body, token=api.token)[0] == 403
    # rejoué (réponse perdue) : le même passage, pas un second tour
    st, again = _call(port, "POST", "/v1/local/agent-runs", body, token=tok, headers=relay, host="atelier.nokker.app")
    assert st == 202 and again["status"] == "succeeded" and w.model.calls == 1


def test_cancel_at_nokker_ai_stops_the_run_on_its_machine(tmp_path):
    f = Fleet(tmp_path)
    m1, w1, _ = f.add_machine(f.p, "atelier", steps=[("tool", "wipe", {}), ("text", "j'attends")])
    a = f.agent(f.p, placement={"machine": m1.id})
    run = f.cloud.agents.run_now(a.id, f.p.account_id)
    assert f.cloud.run(run.id).status == "waiting_approval" and len(w1.approvals.pending()) == 1
    out = f.cloud.agents.cancel_run(a.id, run.id, f.p.account_id)
    assert out.status == "cancelled"
    assert f.seen[-1][1] == f"/v1/local/agent-runs/{run.id}/cancel"
    assert w1.run(run.id).status == "cancelled" and w1.approvals.pending() == []  # sa carte part avec lui
    assert f.cloud.run(run.id).status == "cancelled"


def test_load_reporter_sends_the_deck_when_it_changes():
    from nokker.local.load import LoadReporter

    posted = []

    def transport(req):
        posted.append(req.url.path)
        return httpx.Response(200, json={"stored": True})

    deck = {"v": 1, "sessions": [], "agents": [], "pending_approvals": 0}
    r = LoadReporter(base_url="https://app.nokker.ai", token=lambda: "mt", measure=lambda: load(), deck=lambda: deck,
                     client=httpx.Client(transport=httpx.MockTransport(transport)))
    assert r.send_once() and posted == ["/v1/gateway/machine/load", "/v1/gateway/machine/deck"]
    r.send_once()
    assert posted.count("/v1/gateway/machine/deck") == 1  # inchangé : pas renvoyé
    deck["pending_approvals"] = 1
    r.send_once()
    assert posted.count("/v1/gateway/machine/deck") == 2


def test_patch_cannot_move_an_agent_around_the_checks(tmp_path):
    f = Fleet(tmp_path)
    m1, _, _ = f.add_machine(f.p, "atelier")
    a = f.agent(f.p, placement={"machine": "auto"}, secrets=["API_TOKEN"])
    with pytest.raises(AgentError, match="move"):
        f.cloud.agents.patch_agent(a.id, {"placement": {"machine": m1.id}}, f.p.account_id)
    assert f.cloud.agents.patch_agent(a.id, {"placement": {"machine": "auto"}, "name": "V2"},
                                      f.p.account_id).name == "V2"  # même maison : permis


def test_video_pack_card_keeps_its_gpu_need_through_activation_to_placement(tmp_path, monkeypatch):
    """Revue de cohérence de la doc (25.10) : la carte `agent_draft` du pack video porte `needs: {gpu: true}` ;
    l'activation la perdait, et le placement `auto` choisissait la machine sans GPU (plus de RAM libre)."""
    from nokker.bubbles.tools import propose_agent_draft
    from nokker.core.ports import ToolContext
    from nokker.tools import skills as sk
    from nokker.tools.packs import PackToolSource
    from tests.unit.tools.test_packs import safe_guard

    monkeypatch.setattr(sk, "_guard_module", lambda: safe_guard())
    f = Fleet(tmp_path)
    f.add_machine(f.p, "serveur", ld=load(free=64000))  # le plus de RAM libre, pas de GPU
    gpu, _w, _ = f.add_machine(f.p, "atelier", steps=[("text", "storyboard prêt")], ld=load(free=8000, gpu=True))
    bubbles, user = f.cloud.bubbles, f.p.account_id
    bubbles.agents_hub = f.cloud.agents
    bubbles.known_users = frozenset({*bubbles.known_users, user})
    thread = bubbles.create_thread("Packs", user)
    ctx = ToolContext(thread_id=thread.id, turn_index=0, channel="api", sender=user)
    packs = PackToolSource(home=tmp_path / "home",
                           propose=lambda a, c, extra: propose_agent_draft(bubbles, c, a, extra))
    out = packs.call("pack_install", {"name": "video"}, ctx)
    assert not out.is_error, out.content
    [card] = [b for b in bubbles.bubbles.list_thread(thread.id) if b.kind == "agent_draft"]
    assert card.state["needs"] == {"gpu": True}
    agent = bubbles.action(card.id, user, "activate")["agent"]
    assert agent["needs"] == {"repos": [], "hosts": [], "connectors": [], "mcp": [], "gpu": True}
    # Le placement voit le besoin : `auto` part sur la machine GPU, jamais sur celle qui a seulement plus de RAM.
    moved = f.cloud.agents.move(agent["id"], "auto", user, f.p.tenant_id)
    assert moved.placement == {"machine": "auto"}
    run = f.cloud.agents.run_now(agent["id"], user)
    assert f.cloud.run(run.id).machine_id == gpu.id and f.cloud.run(run.id).summary == "storyboard prêt"
