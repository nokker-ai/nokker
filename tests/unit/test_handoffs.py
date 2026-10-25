"""Délégation web → Lokkal par besoin (lot X21, docs/LOKKAL.md §6.7 et §7.16) : localité (`local | cloud | both`) et
filtre à l'envoi du miroir et de la passerelle, inventaire des ressources par noms, choix de la machine (ressource,
charge, éteinte), acceptation obligatoire, résultat en bulle et dans le contexte du fil web, cloisonnement entre
comptes. Aucun appel réseau : passerelle et nokker.ai simulés (httpx.MockTransport, serveur local éphémère)."""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from nokker.cloud.handoff import (
    FRESH_S,
    HandoffError,
    HandoffService,
    HandoffTools,
    bubble_updater,
    choose,
    handoff_state,
    load_score,
)
from nokker.cloud.link import LinkService, Machine, Signer, verify_token
from nokker.core import locality
from nokker.core.ports import ToolContext
from tests.unit.context.test_project_memory import ctx, make_memory, make_repo

LOAD = {"v": 1, "ram": {"total_mb": 16000, "free_mb": 8000}, "cpu": {"count": 8, "load1": 0.8}, "gpus": [],
        "sessions": 0, "active_turns": 0}


# ─────────────────────────────── localité et filtre à l'envoi ───────────────────────────────


def test_locality_values_and_outbound_filter():
    assert locality.LOCALITIES == ("local", "cloud", "both")
    assert locality.may_leave("both") and locality.may_leave("cloud")
    assert not locality.may_leave("local") and not locality.may_leave(None) and not locality.may_leave("partout")
    assert locality.check(None) is None and locality.check("both") == "both"
    with pytest.raises(ValueError):
        locality.check("partout")
    items = [{"n": 1, "locality": "local"}, {"n": 2, "locality": "both"}, {"n": 3}, {"n": 4, "locality": "cloud"}]
    assert [i["n"] for i in locality.outbound(items, locality.of)] == [2, 4]  # sans localité : ne part pas


def test_local_vault_secrets_are_local():
    from datetime import UTC, datetime

    from nokker.secrets.base import SecretInfo

    assert SecretInfo("GH", datetime(2026, 10, 25, tzinfo=UTC)).as_dict()["locality"] == "local"


def _user_and_project_notes(tmp_path):
    repo = make_repo(tmp_path, remote="git@forge.example:org/app.git")
    pm, _ = make_memory(tmp_path)
    pm.open_session("th_1", repo, tmp_path)
    from nokker.context.project_memory import ProjectMemoryTools

    tools = ProjectMemoryTools(pm)
    said = "pour tous mes projets, réponds-moi en français"
    pm.core_block("th_1", 2, said)
    assert not tools.call("memory_note", {"kind": "rule", "title": "Tests", "text": "lancer pytest -q avant de conclure",
                                          "derived_from": "repo", "scope": "project"}, ctx(turn=2)).is_error
    assert not tools.call("memory_note", {"kind": "feedback", "title": "Langue", "text": "répondre en français",
                                          "derived_from": "user", "scope": "user", "quote": said}, ctx(turn=2)).is_error
    project = pm._session("th_1").project
    return pm, project


def test_note_locality_project_is_local_by_default_user_is_both(tmp_path):
    pm, project = _user_and_project_notes(tmp_path)
    [proj_note] = pm.learned(project)
    [user_note] = pm.learned("u-local")
    assert proj_note.locality == "local" and proj_note.to_json()["locality"] == "local"
    assert user_note.locality == "both"
    assert [n.name for n in pm.notes_for_mirror()] == [user_note.name]
    assert pm.projects() == [{"project": project, "label": "forge.example/org/app", "mirror": False}]
    assert pm.set_mirrored(project, True) and pm.learned(project)[0].locality == "both"
    assert not pm.set_mirrored("p-0000000000000000", True) and not pm.set_mirrored("u-local", True)


def test_mirror_sends_the_user_note_never_an_unmirrored_project_note(tmp_path):
    """La preuve demandée : une note de projet non mise en miroir ne part pas ; une note de l'utilisateur part ; un
    projet mis en miroir puis retiré envoie l'oubli ; rien ne part en pause."""
    from nokker.local.mirror import MirrorWorker

    pm, project = _user_and_project_notes(tmp_path)
    sent = []

    def cloud(req: httpx.Request) -> httpx.Response:
        sent.append(json.loads(req.content))
        assert req.url.path == "/v1/mirror/notes" and req.headers["Authorization"] == "Bearer mt"
        return httpx.Response(200, json={"accepted": 1})

    state: dict = {}
    w = MirrorWorker(store=None, base_url="https://app.nokker.ai", token=lambda: "mt", state=state, save=lambda: None,
                     client=httpx.Client(transport=httpx.MockTransport(cloud)), notes=pm)
    assert w.push_notes_once() == 1
    [body] = sent
    assert [(n["project"], n["locality"]) for n in body["notes"]] == [("u-local", "both")]
    assert "pytest" not in json.dumps(body)  # le texte de la note de projet n'a pas quitté la machine
    assert w.push_notes_once() == 0 and len(sent) == 1  # inchangée : rien ne repart

    pm.set_mirrored(project, True)  # choix explicite par projet
    assert w.push_notes_once() == 1 and sent[-1]["notes"][0]["project"] == project
    pm.set_mirrored(project, False)
    assert w.push_notes_once() == 1 and sent[-1] == {"notes": [], "forget": [
        {"project": project, "name": pm.learned(project)[0].name}]}

    # Même si la source se trompe (une note `local` dans la liste), le filtre à l'envoi la retient.
    class Liar:
        def notes_for_mirror(self):
            return [pm.learned(project)[0]]  # projet retiré du miroir : `local`

    w.notes, before = Liar(), len(sent)
    w.push_notes_once()
    assert all(n["locality"] != "local" for b in sent[before:] for n in b["notes"])
    state["mirror_paused"] = True
    w.notes = pm
    assert w.push_notes_once() == 0


def test_cloud_refuses_a_local_note_even_if_a_machine_sends_it():
    from nokker.cloud.mirror import MemoryMirrorNotes, push_notes

    store = MemoryMirrorNotes()
    body = {"notes": [
        {"project": "u-local", "name": "feedback-langue-ab12cd", "kind": "feedback", "scope": "user",
         "locality": "both", "title": "Langue", "text": "répondre en français"},
        {"project": "p-0123456789abcdef", "name": "rule-tests-ab12cd", "kind": "rule", "scope": "project",
         "locality": "local", "title": "Tests", "text": "lancer pytest"},
        {"project": "../etc", "name": "x", "locality": "both", "text": "chemin"},
        {"project": "u-local", "name": "sans-localite", "text": "?"}]}
    assert push_notes(store, "tn_p", "mc_1", body) == {"accepted": 1, "refused": 3, "forgotten": 0}
    assert [r["name"] for r in store.of_tenant("tn_p")] == ["feedback-langue-ab12cd"]
    assert store.of_tenant("tn_autre") == []
    assert push_notes(store, "tn_p", "mc_1", {"forget": [{"project": "u-local", "name": "feedback-langue-ab12cd"}]}) \
        == {"accepted": 0, "refused": 0, "forgotten": 1}


# ─────────────────────────────── inventaire par noms ───────────────────────────────


def _roots(tmp_path) -> Path:
    root = tmp_path / "projets"
    for rel in ("monapp", "clients/acme-site", "clients/deep/trop-loin", ".cache/cache-repo"):
        (root / rel / ".git").mkdir(parents=True)
    (root / "monapp" / "vendored" / ".git").mkdir(parents=True)  # dépôt dans un dépôt : ignoré
    (root / "notes").mkdir()
    (root / "wt").mkdir()
    (root / "wt" / ".git").write_text("gitdir: ../monapp/.git/worktrees/wt\n")  # worktree : fichier .git
    return root


def test_inventory_publishes_names_only_and_resolves_locally(tmp_path):
    from nokker.local.resources import Inventory

    root = _roots(tmp_path)
    hosts = tmp_path / "hosts.yaml"
    hosts.write_text("hosts:\n  prod-db: {ssh: root@10.0.0.5, role: prod}\n  preprod: {ssh: x}\n  'bad/name': {}\n")
    inv = Inventory(roots=[str(root)], hosts_path=hosts, connectors=lambda: ["mail"], mcp=lambda: ["git", "a b"])
    got = inv.collect()
    assert got == {"v": 1, "repos": ["acme-site", "monapp", "wt"], "hosts": ["preprod", "prod-db"],
                   "connectors": ["mail"], "mcp": ["git"]}
    dump = json.dumps(got)
    assert str(tmp_path) not in dump and "/" not in dump and "10.0.0.5" not in dump  # ni chemin ni accès
    assert inv.resolve("repo", "acme-site") == str(root / "clients" / "acme-site")  # le chemin reste ici
    assert inv.resolve("host", "prod-db") == str(root) and inv.resolve("connector", "mail") == str(root)
    assert inv.resolve("repo", "trop-loin") is None and inv.resolve("host", "inconnu") is None
    assert inv.resolve("secret", "GH") is None  # le coffre n'est pas une ressource publiée
    hosts.write_text("- name: bastion\n- {name: ../../etc}\n")
    assert Inventory(roots=[], hosts_path=hosts).collect()["hosts"] == ["bastion"]


def test_clean_resources_drops_anything_that_is_not_a_name():
    from nokker.local.resources import clean_resources

    raw = {"repos": ["monapp", "/home/alice/secret", "~/x", "a" * 65, 3, "ok.v2"], "hosts": "prod",
           "connectors": ["mail", "mail"], "extra": ["contenu"], "mcp": ["git"]}
    assert clean_resources(raw) == {"v": 1, "repos": ["monapp", "ok.v2"], "hosts": [], "connectors": ["mail"],
                                    "mcp": ["git"]}
    assert clean_resources("x") is None


def test_load_reporter_sends_the_inventory_when_it_changes():
    from nokker.local.load import LoadReporter

    posts = []

    def cloud(req: httpx.Request) -> httpx.Response:
        posts.append((req.url.path, json.loads(req.content)))
        return httpx.Response(200, json={"stored": True})

    inv = {"v": 1, "repos": ["monapp"], "hosts": [], "connectors": [], "mcp": []}
    r = LoadReporter(base_url="https://app.nokker.ai", token=lambda: "mt", measure=lambda: LOAD,
                     client=httpx.Client(transport=httpx.MockTransport(cloud)), resources=lambda: dict(inv))
    assert r.send_once()
    assert [p for p, _ in posts] == ["/v1/gateway/machine/load", "/v1/gateway/machine/resources"]
    r.send_once()
    assert [p for p, _ in posts].count("/v1/gateway/machine/resources") == 1  # inchangé : pas renvoyé
    inv["repos"] = ["monapp", "acme-site"]
    r.send_once()
    assert posts[-1] == ("/v1/gateway/machine/resources", {**inv})
    r.resources_every_s = 0  # au plus tard toutes les 10 min, même inchangé
    r.send_once()
    assert [p for p, _ in posts].count("/v1/gateway/machine/resources") == 3


# ─────────────────────────────── choix de la machine ───────────────────────────────


def _m(mid, name, *, repos=(), load=None, load_at=None, seen=None):
    return Machine(id=mid, account="us_p", tenant="tn_p", name=name, host=f"{name}.nokker.app", token_sha256="",
                   created_at=0, load=load, load_at=load_at, last_seen_at=seen,
                   resources={"v": 1, "repos": list(repos), "hosts": [], "connectors": [], "mcp": []})


def test_choose_the_machine_that_has_it_then_the_least_loaded_then_waits():
    now = 10_000.0
    heavy = {**LOAD, "cpu": {"count": 8, "load1": 7.0}, "active_turns": 2}
    light = {**LOAD, "cpu": {"count": 8, "load1": 0.2}}
    machines = [_m("mc_a", "atelier", repos=["monapp"], load=heavy, load_at=now - 30),
                _m("mc_b", "portable", repos=["monapp"], load=light, load_at=now - 60),
                _m("mc_c", "vieux", repos=["monapp"], load={**LOAD, "cpu": {"count": 8, "load1": 0}},
                   load_at=now - FRESH_S - 1),  # éteint : sa charge ne compte plus
                _m("mc_d", "nas", repos=["autre"], load=light, load_at=now)]
    assert load_score(heavy) > load_score(light)
    assert choose(machines, "repo", "monapp", now) == (machines[1], True)
    assert choose(machines, "repo", "autre", now) == (machines[3], True)
    assert choose(machines, "repo", "absent", now) == (None, False)
    off = [_m("mc_a", "atelier", repos=["x"], load_at=now - 4000), _m("mc_b", "portable", repos=["x"], load_at=now - 900)]
    assert choose(off, "repo", "x", now) == (off[1], False)  # aucune en ligne : la plus récemment vue, et on attend


# ─────────────────────────────── proposition, acceptation, exécution ───────────────────────────────


class Gateway:
    """La passerelle et la session Lokkal simulées : enregistre chaque requête (aucune n'est réelle)."""

    def __init__(self, signer, reply="corrigé, tests verts", pending=0, fail_sessions=False):
        self.signer, self.reply, self.pending, self.fail_sessions = signer, reply, pending, fail_sessions
        self.seen: list[tuple[str, str, dict, dict]] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        claims = verify_token(req.headers["Authorization"][7:], self.signer.jwks())
        body = json.loads(req.content or b"{}")
        self.seen.append((req.url.host, req.url.path, claims, body))
        if self.fail_sessions:
            raise httpx.ConnectError("tunnel fermé")
        if req.url.path == "/v1/local/sessions":
            return httpx.Response(201, json={"thread_id": "th_l1"})
        assert req.headers["X-Session-Id"] == "th_l1"
        return httpx.Response(200, json={"choices": [{"message": {"content": self.reply}}],
                                         "nokker": {"turn_index": 0, "pending_approvals": self.pending}})


def _linked(svc: LinkService, account="us_p", tenant="tn_p", name="atelier"):
    d = svc.device(name)
    svc.approve(d["user_code"], account, tenant)
    tok = svc.token(d["device_code"])["machine_token"]
    return svc.machine_for_token(tok)


def _service(gateway=None, clock=None):
    links, signer = LinkService(), Signer.generate()
    m = _linked(links)
    updates = []
    svc = HandoffService(links=links, signer=signer, spawn=lambda fn: fn(), on_update=lambda h: updates.append(h.state),
                         scrub=lambda s: s.replace("S3CR3T", "[secret:X]"), clock=clock or time.time)
    gw = gateway(signer) if gateway else Gateway(signer)
    svc.client = httpx.Client(transport=httpx.MockTransport(gw))
    return svc, links, m, gw, updates


def test_proposal_runs_nothing_until_the_user_accepts(tmp_path):
    svc, links, m, gw, updates = _service()
    links.report_resources(m, {"repos": ["monapp"], "connectors": ["mail"]})
    links.report_load(m, LOAD)
    tools = HandoffTools(service=svc, tenant_of=lambda a: "tn_p" if a == "us_p" else None)
    assert tools.specs()[0].effect.value == "read"  # proposer n'agit sur rien
    r = tools.call("lokkal_handoff", {"need": {"kind": "repo", "name": "monapp"}, "task": "corrige le test S3CR3T"},
                   ToolContext(thread_id="tn_p:th_web", turn_index=0, channel="api", sender="us_p"))
    assert not r.is_error and "Rien ne s'exécute" in r.content
    st = handoff_state(r.content)
    assert st["state"] == "proposed" and st["machine_name"] == "atelier" and st["online"] is True
    assert gw.seen == [] and updates == []  # aucune requête vers la machine sans acceptation
    with pytest.raises(HandoffError) as e:
        svc.decide("tn_p", "us_autre", st["handoff_id"], "accept")  # pas le compte de la demande
    assert e.value.status == 404 and gw.seen == []

    done = svc.decide("tn_p", "us_p", st["handoff_id"], "accept")
    assert done.state == "accepted"
    h = svc.get("tn_p", "us_p", st["handoff_id"])
    assert h.state == "done" and h.reply == "corrigé, tests verts" and h.lokkal_thread == "th_l1"
    assert updates == ["accepted", "running", "done"]
    (host1, path1, claims, body1), (_, path2, _, body2) = gw.seen
    assert (host1, path1, path2) == ("atelier.nokker.app", "/v1/local/sessions", "/v1/chat/completions")
    assert body1 == {"resource": {"kind": "repo", "name": "monapp"}}  # un nom, jamais un chemin
    assert claims["scope"] == "lokkal:session" and claims["handoff"] == h.id and claims["machine"] == m.id
    assert claims["web_session"] == "tn_p:th_web"
    task = body2["messages"][0]["content"]
    assert "corrige le test" in task and "Ta réponse finale revient dans le fil web, et elle seule" in task
    with pytest.raises(HandoffError) as e:
        svc.decide("tn_p", "us_p", h.id, "accept")  # une demande ne se lance qu'une fois
    assert e.value.status == 409 and len(gw.seen) == 2


def test_no_machine_declares_it_lists_what_exists_and_creates_nothing():
    svc, links, m, gw, _ = _service()
    links.report_resources(m, {"repos": ["monapp", "acme"]})
    h, msg = svc.propose("us_p", "tn_p", "tn_p:th", "repo", "inconnu", "fais x")
    assert h is None and "ne déclare le dépôt « inconnu »" in msg and "acme, monapp" in msg
    assert svc.list("tn_p", "us_p") == []
    assert svc.propose("us_p", "tn_p", "tn_p:th", "secret", "GH", "x")[0] is None  # pas une nature de besoin


def test_machine_off_the_request_waits_and_runs_when_its_load_returns():
    now = [50_000.0]
    svc, links, m, gw, updates = _service(clock=lambda: now[0])
    links.clock = lambda: now[0]
    links.report_resources(m, {"hosts": ["prod-db"]})
    links.report_load(m, LOAD)
    now[0] += FRESH_S + 60  # la machine s'est éteinte
    h, msg = svc.propose("us_p", "tn_p", "tn_p:th", "host", "prod-db", "regarde l'espace disque")
    assert h is not None and not h.online and "la demande attendra" in msg
    svc.decide("tn_p", "us_p", h.id, "accept")
    assert svc.get("tn_p", "us_p", h.id).state == "waiting" and gw.seen == []  # l'utilisateur voit « en attente »
    assert updates == ["accepted", "waiting"]
    links.report_load(m, LOAD)  # elle revient : la route de charge appelle machine_online
    assert svc.machine_online(m) == 1
    assert svc.get("tn_p", "us_p", h.id).state == "done" and [p for _, p, _, _ in gw.seen] == [
        "/v1/local/sessions", "/v1/chat/completions"]
    assert svc.machine_online(m) == 0  # rien ne repart deux fois


def test_refused_or_expired_requests_never_run():
    now = [1000.0]
    svc, links, m, gw, _ = _service(clock=lambda: now[0])
    links.clock = lambda: now[0]
    links.report_resources(m, {"repos": ["a"]})
    links.report_load(m, LOAD)
    h, _ = svc.propose("us_p", "tn_p", "tn_p:th", "repo", "a", "x")
    assert svc.decide("tn_p", "us_p", h.id, "refuse").state == "refused"
    with pytest.raises(HandoffError):
        svc.decide("tn_p", "us_p", h.id, "accept")
    h2, _ = svc.propose("us_p", "tn_p", "tn_p:th", "repo", "a", "y")
    now[0] += 3601
    assert svc.get("tn_p", "us_p", h2.id).state == "expired"
    with pytest.raises(HandoffError):
        svc.decide("tn_p", "us_p", h2.id, "accept")
    assert gw.seen == []


def test_unreachable_after_the_task_is_sent_is_never_retried_blindly():
    class Flaky(Gateway):
        def __call__(self, req):
            if req.url.path == "/v1/chat/completions":
                raise httpx.ReadTimeout("plus de réponse")
            return super().__call__(req)

    svc, links, m, gw, _ = _service(gateway=Flaky)
    links.report_resources(m, {"repos": ["a"]})
    links.report_load(m, LOAD)
    h, _ = svc.propose("us_p", "tn_p", "tn_p:th", "repo", "a", "x")
    svc.decide("tn_p", "us_p", h.id, "accept")
    got = svc.get("tn_p", "us_p", h.id)
    assert got.state == "failed" and "a peut-être été faite" in got.error and "th_l1" in got.error


def test_session_not_opened_goes_back_to_waiting():
    svc, links, m, _, _ = _service(gateway=lambda s: Gateway(s, fail_sessions=True))
    links.report_resources(m, {"repos": ["a"]})
    links.report_load(m, LOAD)
    h, _ = svc.propose("us_p", "tn_p", "tn_p:th", "repo", "a", "x")
    svc.decide("tn_p", "us_p", h.id, "accept")
    assert svc.get("tn_p", "us_p", h.id).state == "waiting"  # rien n'a été envoyé : on réessaiera


def test_awaiting_a_card_then_the_machine_reports_the_rest_once():
    svc, links, m, gw, updates = _service(gateway=lambda s: Gateway(s, reply="j'attends ta carte", pending=1))
    links.report_resources(m, {"repos": ["a"]})
    links.report_load(m, LOAD)
    h, _ = svc.propose("us_p", "tn_p", "tn_p:th", "repo", "a", "supprime build/")
    svc.decide("tn_p", "us_p", h.id, "accept")
    got = svc.get("tn_p", "us_p", h.id)
    assert got.state == "awaiting_approval" and got.pending_approvals == 1
    assert svc.result("tn_p", h.id, machine_id="mc_autre", reply="faux", turn_index=1) is None  # autre machine
    done = svc.result("tn_p", h.id, machine_id=m.id, reply="supprimé S3CR3T", turn_index=1)
    assert done.state == "done" and done.reply == "supprimé [secret:X]"  # caviardé par nokker.ai aussi
    assert svc.result("tn_p", h.id, machine_id=m.id, reply="rejoué", turn_index=1) is None  # une fois par tour
    assert updates[-1] == "done"


def test_other_account_sees_nothing():
    svc, links, m, gw, _ = _service()
    links.report_resources(m, {"repos": ["monapp"]})
    links.report_load(m, LOAD)
    h, _ = svc.propose("us_p", "tn_p", "tn_p:th", "repo", "monapp", "x")
    other = _linked(links, account="us_q", tenant="tn_q", name="ailleurs")
    links.report_resources(other, {"repos": ["perso"]})
    assert svc.list("tn_q", "us_q") == []
    for tenant, account in (("tn_q", "us_q"), ("tn_p", "us_q"), ("tn_q", "us_p")):
        with pytest.raises(HandoffError):
            svc.get(tenant, account, h.id)
    assert svc.propose("us_q", "tn_q", "tn_q:th", "repo", "monapp", "x")[0] is None  # pas ses machines
    assert svc.propose("us_p", "tn_p", "tn_p:th", "repo", "perso", "x")[0] is None
    assert links.store.resources_of("tn_q", [m.id]) == {}  # l'inventaire suit le locataire (comme la RLS)


# ─────────────────────────────── bulle `handoff` ───────────────────────────────


def test_handoff_bubble_actions_are_the_users_decision_and_the_result_comes_back(tmp_path):
    from nokker.bubbles.hub import BubbleHub
    from nokker.bubbles.memory import InMemoryBubbles
    from nokker.bubbles.types import BubbleError, BubbleForbidden, BubbleNotFound
    from nokker.core.turn import ToolCall, ToolResult
    from nokker.tenancy.context import tenant_scope
    from nokker.threads.memory import InMemoryThreads

    svc, links, m, gw, _ = _service()
    links.report_resources(m, {"repos": ["monapp"]})
    links.report_load(m, LOAD)
    hub = BubbleHub(threads=InMemoryThreads(), bubbles=InMemoryBubbles(), spawn=lambda fn: fn())
    hub.handoffs, svc.on_update = svc, bubble_updater(hub)
    tools = HandoffTools(service=svc, tenant_of=lambda a: "tn_p")
    with tenant_scope("tn_p"):
        thread = hub.threads.create("web", "us_p", thread_id="tn_p:th_web")
        r = tools.call("lokkal_handoff", {"need": {"kind": "repo", "name": "monapp"}, "task": "corrige"},
                       ToolContext(thread_id=thread.id, turn_index=0, channel="api", sender="us_p"))
        produced = hub.producers.produce(ToolCall(id="c1", name="lokkal_handoff", arguments={}), None,
                                         ToolResult(call_id="c1", name="lokkal_handoff", content=r.content))
        assert produced.kind == "handoff" and [a["id"] for a in produced.actions] == ["accept", "refuse"]
        b = hub.bubbles.create(thread_id=thread.id, kind="handoff", title=produced.title, state=produced.state,
                               actions=produced.actions)
        assert b.state["state"] == "proposed" and "tenant" not in json.dumps(b.state)
        with pytest.raises(BubbleError):
            hub.create_bubble(thread.id, "us_p", kind="handoff", state={})  # jamais créée par un client
        with pytest.raises((BubbleNotFound, BubbleForbidden)):
            hub.action(b.id, "us_q", "accept")  # pas membre du fil
        assert gw.seen == []
        out = hub.action(b.id, "us_p", "accept")
        assert out["handoff"]["state"] == "accepted"
        b = hub.bubbles.get(b.id)
        assert b.status == "done" and b.state["state"] == "done" and b.state["reply"] == "corrigé, tests verts"
        ctx_blocks = hub.context_for(thread.id)
        assert ctx_blocks and "Résultat de la machine « atelier »" in ctx_blocks[0] and "tests verts" in ctx_blocks[0]
        with pytest.raises(BubbleError):
            hub.action(b.id, "us_p", "accept")  # déjà faite


# ─────────────────────────────── routes de nokker.ai ───────────────────────────────


@pytest.fixture
def cloud_routes():
    from nokker.api.server import make_handler
    from nokker.cloud import http as cloud_http
    from nokker.cloud.http import CloudLink
    from nokker.cloud.mirror import MemoryMirrorNotes
    from tests.unit.api.test_server import make_kernel

    links, signer = LinkService(), Signer.generate()
    svc = HandoffService(links=links, signer=signer, spawn=lambda fn: fn())
    gw = Gateway(signer)
    svc.client = httpx.Client(transport=httpx.MockTransport(gw))
    sessions = {"Bearer sess-p": "us_p", "Bearer sess-q": "us_q"}
    notes = MemoryMirrorNotes()
    cloud_http.configure(CloudLink(
        links=links, signer=signer, identity=lambda h: sessions.get(h.headers.get("Authorization")),
        tenant_of={"us_p": "tn_p", "us_q": "tn_q"}.get, router_base="http://127.0.0.1:1", router_key=lambda: "x",
        handoffs=svc, mirror_notes=notes, store=object(), mirror_allowed=lambda acc: True))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(make_kernel(), {}))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1], links, svc, gw, notes
    cloud_http.configure(None)
    httpd.shutdown()


def _call(port, path, body=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _machine_token(links, account, tenant, name):
    d = links.device(name)
    links.approve(d["user_code"], account, tenant)
    return links.token(d["device_code"])["machine_token"]


def test_routes_inventory_accept_result_and_isolation(cloud_routes):
    port, links, svc, gw, notes = cloud_routes
    mt = _machine_token(links, "us_p", "tn_p", "atelier")
    mq = _machine_token(links, "us_q", "tn_q", "ailleurs")
    status, _ = _call(port, "/v1/gateway/machine/resources", {"repos": ["monapp", "/home/p/secret"]}, token=mt)
    assert status == 200
    assert _call(port, "/v1/gateway/machine/resources", {"repos": ["x"]})[0] == 401  # jeton de machine requis
    assert _call(port, "/v1/gateway/machine/load", LOAD, token=mt)[0] == 200
    machines = _call(port, "/v1/link/machines", token="sess-p")[1]["machines"]
    assert machines[0]["resources"]["repos"] == ["monapp"] and machines[0]["resources_at"]
    assert _call(port, "/v1/link/machines", token="sess-q")[1]["machines"][0]["resources"] is None

    h, _ = svc.propose("us_p", "tn_p", "tn_p:th_web", "repo", "monapp", "corrige")
    assert _call(port, "/v1/handoffs")[0] == 401
    assert _call(port, f"/v1/handoffs/{h.id}", token="sess-q")[0] == 404  # autre compte : n'existe pas
    assert _call(port, f"/v1/handoffs/{h.id}/accept", {}, token="sess-q")[0] == 404
    assert _call(port, "/v1/handoffs", token="sess-q")[1] == {"handoffs": []}
    listed = _call(port, "/v1/handoffs?thread_id=th_web", token="sess-p")[1]["handoffs"]
    assert [x["handoff_id"] for x in listed] == [h.id] and "tenant" not in listed[0]
    assert gw.seen == []
    status, body = _call(port, f"/v1/handoffs/{h.id}/accept", {}, token="sess-p")
    assert status == 200 and body["state"] == "accepted"
    assert _call(port, f"/v1/handoffs/{h.id}", token="sess-p")[1]["state"] == "done"
    assert _call(port, f"/v1/handoffs/{h.id}/accept", {}, token="sess-p")[0] == 409

    # résultat relevé par la machine : seulement la sienne, une fois par tour
    assert _call(port, f"/v1/gateway/handoffs/{h.id}/result", {"turn_index": 3, "reply": "x"}, token=mq)[0] == 404
    assert _call(port, f"/v1/gateway/handoffs/{h.id}/result", {"turn_index": "x"}, token=mt)[0] == 400
    assert _call(port, f"/v1/gateway/handoffs/{h.id}/result", {"turn_index": 0, "reply": "x"}, token=mt)[1] == {
        "stored": False, "state": "done"}

    # miroir des notes : `local` refusée à l'arrivée, effacement de la machine compris
    status, body = _call(port, "/v1/mirror/notes", {"notes": [
        {"project": "u-local", "name": "fb-a", "locality": "both", "text": "t"},
        {"project": "u-local", "name": "fb-b", "locality": "local", "text": "t"}]}, token=mt)
    assert status == 200 and body == {"accepted": 1, "refused": 1, "forgotten": 0}
    assert len(notes.of_tenant("tn_p")) == 1 and notes.of_tenant("tn_q") == []


# ─────────────────────────────── côté machine : la passerelle filtre la délégation ───────────────────────────────


@pytest.fixture
def machine(tmp_path):
    from nokker.api.server import make_handler
    from nokker.local.api import LOCAL_OWNER, LocalApi
    from nokker.local.setup import LocalSetup, LocalVault
    from tests.unit.api.test_server import make_kernel

    root = _roots(tmp_path)
    api = LocalApi.from_config({"token_path": str(tmp_path / "tok"), "state_dir": str(tmp_path / "state"),
                                "code": {"roots": [str(root)], "backend": "local"}}, port=8742)
    signer = Signer.generate()
    api.setup = LocalSetup(state_path=tmp_path / "setup.json", vault=LocalVault(tmp_path / "v.bin"), detect=lambda: [])
    api.setup.state.update(jwks=signer.jwks(), account="us_p", machine_id="mc_1")

    class Up:
        state = "up"

    api.relay_host, api.tunnel = "atelier.nokker.app", Up()
    kernel = make_kernel("fait : 3 tests corrigés")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(kernel, {api.token: LOCAL_OWNER}, local_api=api))
    api.allowed_hosts = frozenset({f"127.0.0.1:{httpd.server_address[1]}"})
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield api, httpd.server_address[1], signer, root
    httpd.shutdown()


def _relayed(port, method, path, token, body=None, session=None):
    headers = {"Authorization": f"Bearer {token}", "X-Nokker-Relay": "1", "Content-Type": "application/json"}
    if session:
        headers["X-Session-Id"] = session
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, headers=headers,
                                 data=json.dumps(body).encode() if body is not None else None)
    req.add_unredirected_header("Host", "atelier.nokker.app")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_gateway_lets_a_delegation_reach_its_session_and_nothing_else(machine):
    _api, port, signer, root = machine
    dele = signer.delegation_token("us_p", "tn_p", machine="mc_1", web_session="tn_p:th_web", handoff="ho_" + "a" * 16)
    for method, path in (("GET", "/v1/local/secrets"), ("GET", "/v1/local/sessions"), ("GET", "/v1/local/setup"),
                         ("GET", "/v1/local/resources"), ("GET", "/v1/local/approvals"), ("GET", "/v1/threads"),
                         ("GET", "/v1/local/mirror/projects"), ("POST", "/v1/local/tools/call")):
        assert _relayed(port, method, path, dele, {} if method == "POST" else None)[0] == 403, path
    status, body = _relayed(port, "POST", "/v1/local/sessions", dele, {"resource": {"kind": "repo", "name": "acme-site"}})
    assert status == 201 and body["cwd"] == str((root / "clients" / "acme-site").resolve())
    thread = body["thread_id"]
    assert _relayed(port, "POST", "/v1/local/sessions", dele, {"resource": {"kind": "repo", "name": "absent"}})[0] == 404
    assert _relayed(port, "POST", "/v1/chat/completions", dele, {"messages": [{"role": "user", "content": "x"}]})[0] \
        == 403  # sans fil délégué
    assert _relayed(port, "POST", "/v1/chat/completions", dele, {"messages": []}, session="th_local")[0] == 403
    other = signer.delegation_token("us_p", "tn_p", machine="mc_1", web_session="tn_p:autre")
    assert _relayed(port, "POST", "/v1/chat/completions", other, {"messages": []}, session=thread)[0] == 403
    status, body = _relayed(port, "POST", "/v1/chat/completions", dele,
                            {"messages": [{"role": "user", "content": "corrige"}]}, session=thread)
    assert status == 200 and body["choices"][0]["message"]["content"] == "fait : 3 tests corrigés"
    assert body["nokker"] == {"turn_index": 0, "pending_approvals": 0}
    # le propriétaire (jeton d'accès) garde son pilotage ; un lien de pages n'est jamais le propriétaire
    assert _relayed(port, "GET", "/v1/local/sessions", signer.access_token("us_p", "tn_p"))[0] == 200
    assert _relayed(port, "GET", "/v1/local/sessions", signer.pages_token("us_p", "tn_p", "mc_1"))[0] == 401


def test_delegated_turns_report_their_reply_and_only_it():
    from nokker.core.turn import InputCheck, ToolCall, Turn
    from nokker.local.delegation import DelegatedSessions

    posts = []

    def cloud(req: httpx.Request) -> httpx.Response:
        posts.append((req.url.path, req.headers["Authorization"], json.loads(req.content)))
        return httpx.Response(200, json={"stored": True})

    d = DelegatedSessions(token=lambda: "mt", client=httpx.Client(transport=httpx.MockTransport(cloud)),
                          spawn=lambda fn: fn())
    d.register("th_d", {"sub": "us_p", "web_session": "w", "handoff": "ho_" + "b" * 16})
    d.register("th_skill", {"sub": "us_p", "web_session": "w"})  # skill « sessions Lokkal » : pas de demande nommée

    def run_turn(inp, *a, **k):
        return Turn(thread_id=inp.thread_id, index=2, input=InputCheck(text="x"), reply="supprimé",
                    pending_approvals=[ToolCall(id="1", name="terminal", arguments={"command": "rm -rf secret/"})])

    wrapped = d.wrap(run_turn)

    class Inp:
        def __init__(self, t):
            self.thread_id = t

    for t in ("th_d", "th_skill", "th_local"):
        wrapped(Inp(t))
    assert posts == [("/v1/gateway/handoffs/ho_" + "b" * 16 + "/result", "Bearer mt",
                      {"turn_index": 2, "reply": "supprimé", "pending_approvals": 1})]
    assert "rm -rf" not in json.dumps(posts)  # ni les appels ni les arguments : la réponse seule


def test_mirror_projects_route_is_owner_only(tmp_path):
    from nokker.local.api import LocalApi

    api = LocalApi.from_config({"token_path": str(tmp_path / "tok"), "project_memory": True,
                                "project_memory_dir": str(tmp_path / "pm"), "code": {"roots": [str(tmp_path)],
                                                                                     "backend": "local"}}, port=8742)
    (tmp_path / "pm" / "p-0123456789abcdef").mkdir(parents=True)
    auth = f"Bearer {api.token}"
    assert api.handle("GET", "/v1/local/mirror/projects", None, {})[0] == 401
    assert api.handle("GET", "/v1/local/mirror/projects", auth, {}) == (200, {"projects": [
        {"project": "p-0123456789abcdef", "label": "", "mirror": False}]})
    assert api.handle("PUT", "/v1/local/mirror/projects/p-0123456789abcdef", auth, {"mirror": "oui"})[0] == 400
    assert api.handle("PUT", "/v1/local/mirror/projects/p-ffffffffffffffff", auth, {"mirror": True})[0] == 404
    assert api.handle("PUT", "/v1/local/mirror/projects/p-0123456789abcdef", auth, {"mirror": True}) == (
        200, {"project": "p-0123456789abcdef", "mirror": True})
    assert api.project_memory.is_mirrored("p-0123456789abcdef")


def test_gateway_down_retries_are_bounded_then_fail_without_auto_retry():
    """Revue du train du 2.11, défaut 3 : un relais dont la passerelle ne répond pas repassait en `waiting` avec
    `updated_at` remis à l'heure, et chaque battement de la machine (60 s) le relançait, sans fin."""
    from nokker.cloud.handoff import RELAY_MAX_ATTEMPTS, relay_backoff_s

    now = [10_000.0]
    svc, links, m, gw, _ = _service(gateway=lambda s: Gateway(s, fail_sessions=True), clock=lambda: now[0])
    links.clock = lambda: now[0]
    links.report_resources(m, {"repos": ["a"]})
    links.report_load(m, LOAD)
    h, _ = svc.propose("us_p", "tn_p", "tn_p:th", "repo", "a", "x")
    svc.decide("tn_p", "us_p", h.id, "accept")
    got = svc.get("tn_p", "us_p", h.id)
    assert got.state == "waiting" and got.attempts == 1 and f"essai 1/{RELAY_MAX_ATTEMPTS}" in got.error
    assert len(gw.seen) == 1

    for n in range(1, RELAY_MAX_ATTEMPTS):
        now[0] += 60  # battement de la machine avant la fin de l'attente : pas de relance
        links.report_load(m, LOAD)
        if relay_backoff_s(n) > 60:
            assert svc.machine_online(m) == 0 and len(gw.seen) == n
        now[0] += relay_backoff_s(n)
        links.report_load(m, LOAD)
        assert svc.machine_online(m) == 1 and len(gw.seen) == n + 1
    got = svc.get("tn_p", "us_p", h.id)
    assert got.state == "failed" and got.attempts == RELAY_MAX_ATTEMPTS
    assert f"après {RELAY_MAX_ATTEMPTS} essais" in got.error and "rien n'a été envoyé" in got.error
    for _ in range(5):  # plus aucune relance automatique, quels que soient les battements
        now[0] += 3600
        links.report_load(m, LOAD)
        assert svc.machine_online(m) == 0
    assert len(gw.seen) == RELAY_MAX_ATTEMPTS
