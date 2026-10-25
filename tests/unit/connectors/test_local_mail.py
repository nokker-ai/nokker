"""Boîte mail posée DANS Lokkal (propriétaire local) : `/v1/local/connectors*` (jeton local seulement, 403 par la
passerelle), mot de passe par NOM du coffre local (X4, jamais en clair dans la config ni dans une réponse), outils mail
des tours du propriétaire (localité `local`, l'envoi reste une carte forte), relève du déclencheur `mail:` sur cette
boîte, et rien de la boîte dans le miroir. Faux serveurs IMAP/SMTP en mémoire (aucun réseau)."""
from __future__ import annotations

import dataclasses
import json
import logging

import pytest

from nokker.connectors import mail as mail_mod
from nokker.core.ports import ToolContext, ToolSpec
from nokker.core.turn import Decision, Effect, ToolCall
from nokker.local.api import LOCAL_OWNER, LocalApi
from nokker.local.connectors import LOCAL_STRONG_RULE, Mailboxes, keep_local_fn
from nokker.secrets.refs import secrets_scope
from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope
from tests.unit.agents.test_mail_trigger import HOP, PASSWORD, SETTINGS, Mailbox, Session, armed_mail_agent, poll
from tests.unit.vault.fakes import fernet_key

CTX = ToolContext(thread_id="th_local", turn_index=0, channel="api", sender=LOCAL_OWNER)


class FakeSMTP:
    def __init__(self, box):
        self.box = box

    def connect(self, host, port):
        return 220, b"ok"

    def login(self, user, password):
        if password != self.box.password:
            import smtplib

            raise smtplib.SMTPAuthenticationError(535, f"bad credentials {password}".encode())
        return 235, b"ok"

    def noop(self):
        return 250, b"ok"

    def quit(self):
        return 221, b"bye"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("NOKKER_LOCAL_SECRET_KEY", fernet_key())
    monkeypatch.delenv("NOKKER_SECRETS_PROVIDER", raising=False)
    monkeypatch.delenv("NOKKER_OPENBAO_ADDR", raising=False)


@pytest.fixture
def box(monkeypatch):
    b = Mailbox()
    monkeypatch.setattr(mail_mod, "check_endpoint", lambda host, port, proto, **k: dataclasses.replace(HOP, port=port))
    monkeypatch.setattr(mail_mod, "_PinnedIMAP", lambda hop, tls, timeout: Session(b))
    monkeypatch.setattr(mail_mod, "_PinnedSMTP", lambda hop, tls, timeout: FakeSMTP(b))
    return b


def _api(tmp_path) -> LocalApi:
    return LocalApi.from_config({"state_dir": str(tmp_path / "state"), "token_path": str(tmp_path / "tok"),
                                 "code": {"roots": [str(tmp_path)], "backend": "local"}}, port=8742)


def _linked(tmp_path, box):
    api = _api(tmp_path)
    api.vault.set("MAIL_PW", PASSWORD)
    status, card = api.handle("PUT", "/v1/local/connectors/mail", f"Bearer {api.token}",
                              {"settings": dict(SETTINGS), "secret": "MAIL_PW"})
    assert status == 200, card
    return api, card


# ─────────────────────────────── routes ───────────────────────────────


def test_link_a_local_mailbox_by_vault_name_and_never_show_the_password(tmp_path, box):
    api = _api(tmp_path)
    changes = []
    api.connectors.on_change = lambda: changes.append(1)
    auth = f"Bearer {api.token}"
    assert api.handle("GET", "/v1/local/connectors", auth, {})[1]["connectors"][0]["state"] == "absent"
    api.vault.set("MAIL_PW", PASSWORD)
    status, card = api.handle("PUT", "/v1/local/connectors/mail", auth,
                              {"settings": dict(SETTINGS), "secret": "secret:MAIL_PW"})
    assert status == 200 and card["state"] == "ok" and card["locality"] == "local" and card["secret"] == "MAIL_PW"
    assert card["settings"]["imap_host"] == "imap.corp.test" and "IMAP et SMTP ok" in card["message"]
    assert changes == [1]
    on_disk = (tmp_path / "state" / "connectors.json").read_text(encoding="utf-8")
    assert PASSWORD not in on_disk and '"secret:MAIL_PW"' in on_disk
    status, listed = api.handle("GET", "/v1/local/connectors", auth, {})
    assert status == 200 and listed["connectors"][0]["secret"] == "MAIL_PW" and "gmail" in listed["presets"]["mail"]
    assert api.handle("POST", "/v1/local/connectors/mail/test", auth, {})[1]["ok"] is True
    for body in (card, listed):
        assert PASSWORD not in json.dumps(body)
    assert api.handle("DELETE", "/v1/local/connectors/mail", auth, {}) == (200, {"revoked": True})
    assert api.handle("GET", "/v1/local/connectors", auth, {})[1]["connectors"][0]["state"] == "absent"
    assert changes == [1, 1]


def test_the_password_itself_is_refused_and_never_echoed(tmp_path, box):
    api = _api(tmp_path)
    auth = f"Bearer {api.token}"
    pasted = "Zq81TotallyAPasswordXyz"
    for secret in (pasted, PASSWORD, f"secret:{pasted}", ""):
        status, out = api.handle("PUT", "/v1/local/connectors/mail", auth,
                                 {"settings": dict(SETTINGS), "secret": secret})
        assert status == 400 and "coffre" in out["message"], (secret, out)
        assert (pasted not in json.dumps(out)) and (PASSWORD not in json.dumps(out))
    status, out = api.handle("PUT", "/v1/local/connectors/mail", auth,
                             {"settings": {**SETTINGS, "password": pasted}, "secret": "MAIL_PW"})
    assert status == 400 and pasted not in json.dumps(out)
    status, out = api.handle("PUT", "/v1/local/connectors/mail", auth,
                             {"settings": dict(SETTINGS), "secret": "MAIL_PW", "password": pasted})
    assert status == 400 and pasted not in json.dumps(out)
    assert not (tmp_path / "state" / "connectors.json").exists()
    assert api.handle("PUT", "/v1/local/connectors/calendar", auth,
                      {"settings": {}, "secret": "MAIL_PW"})[0] == 400  # mail seulement


def test_a_refused_login_stores_nothing_and_leaks_nothing(tmp_path, box, caplog):
    caplog.set_level(logging.DEBUG)
    api = _api(tmp_path)
    api.vault.set("MAIL_PW", "autre-mot-de-passe-faux")
    status, out = api.handle("PUT", "/v1/local/connectors/mail", f"Bearer {api.token}",
                             {"settings": dict(SETTINGS), "secret": "MAIL_PW"})
    assert status == 422 and out["error"] == "auth_failed"
    assert "autre-mot-de-passe-faux" not in json.dumps(out) + caplog.text
    assert not (tmp_path / "state" / "connectors.json").exists()


def test_routes_need_the_local_token_and_the_gateway_gets_403(tmp_path, box, monkeypatch):
    api, _ = _linked(tmp_path, box)
    calls = (("GET", "/v1/local/connectors", {}), ("PUT", "/v1/local/connectors/mail",
                                                   {"settings": dict(SETTINGS), "secret": "MAIL_PW"}),
             ("POST", "/v1/local/connectors/mail/test", {}), ("DELETE", "/v1/local/connectors/mail", {}))
    for method, path, body in calls:
        assert api.handle(method, path, None, body)[0] == 401
        assert api.handle(method, path, "Bearer faux", body)[0] == 401
    monkeypatch.setattr(api, "relayed_owner", lambda auth: {"sub": "acc"})
    for method, path, body in calls:
        status, out = api.handle(method, path, "Bearer x.y.z", body, relayed=True)
        assert status == 403 and "machine" in out["error"]["message"]
    assert api.connectors.configured()  # rien n'a été effacé par la passerelle


# ─────────────────────────────── tours du propriétaire ───────────────────────────────


def test_the_owner_turns_get_local_mail_tools_resolved_from_the_vault(tmp_path, box):
    api = _api(tmp_path)
    src = api.connectors.source()
    assert src.specs() == []  # aucune boîte : aucun outil
    api.vault.set("MAIL_PW", PASSWORD)
    api.handle("PUT", "/v1/local/connectors/mail", f"Bearer {api.token}",
               {"settings": dict(SETTINGS), "secret": "MAIL_PW"})
    specs = {s.name: s for s in src.specs()}
    assert {"mail_search", "mail_read", "mail_send", "mail_trash"} <= set(specs)
    assert {s.locality for s in specs.values()} == {"local"} and specs["mail_send"].effect is Effect.SEND_EXTERNAL
    uid = box.deliver("billing@example.com", "Facture locale")
    out = src.call("mail_search", {"query": "from:billing@example.com"}, CTX)
    assert not out.is_error and "Facture locale" in out.content and f"INBOX:41:{uid}" in out.content
    with secrets_scope(["AUTRE"]):  # passage d'un agent qui n'a pas déclaré le nom du mot de passe
        out = src.call("mail_search", {"query": "x"}, CTX)
    assert out.is_error and "MAIL_PW" in out.content and "secrets" in out.content and PASSWORD not in out.content
    with secrets_scope(["MAIL_PW"]):
        assert not src.call("mail_search", {"query": "x"}, CTX).is_error


def _gates(mode, allow=()):
    from nokker.qa.gates import GatesV0
    from nokker.qa.invariants import DeterministicInvariants
    from nokker.qa.permissions import LocalPermissions, Mode, PermissionGates, Rule

    return PermissionGates(inner=GatesV0(invariants=DeterministicInvariants()),
                           permissions=LocalPermissions(mode=Mode(mode), allow=[Rule.parse(r) for r in allow]))


def _turn(text="envoie-le, GO"):
    from nokker.core.turn import InputCheck, Turn

    return Turn(thread_id="th_local", index=0, input=InputCheck(text=text), sender=LOCAL_OWNER)


@pytest.mark.parametrize("mode", ["auto", "accept_edits", "default"])
def test_sending_from_the_local_mailbox_is_always_a_strong_card(mode):
    gates = _gates(mode, allow=["mail_send", "mail_trash", "mail_draft"])
    for name, effect in (("mail_send", Effect.SEND_EXTERNAL), ("mail_trash", Effect.DESTRUCTIVE)):
        spec = ToolSpec(name=name, description="", parameters={}, effect=effect, source="connector:mail",
                        locality="local")
        [v] = gates.action(ToolCall(id="c", name=name, arguments={"to": "a@example.com"}), spec, _turn())
        assert v.decision is Decision.HITL and v.strength == "strong" and v.rule == LOCAL_STRONG_RULE
    draft = ToolSpec(name="mail_draft", description="", parameters={}, effect=Effect.WRITE, source="connector:mail",
                     locality="local")
    if mode == "auto":  # un brouillon reste une écriture ordinaire (règles et mode locaux)
        assert gates.action(ToolCall(id="c", name="mail_draft", arguments={}), draft, _turn())[0].decision \
            is Decision.ALLOW


def test_an_agent_rule_never_lifts_the_local_send_card():
    from nokker.agents.engine import AgentRunGates
    from nokker.qa.permissions import Rule

    spec = ToolSpec(name="mail_send", description="", parameters={}, effect=Effect.SEND_EXTERNAL,
                    source="connector:mail", locality="local")
    g = AgentRunGates(inner=_gates("auto"), rules=[Rule.parse("mail_send")])
    [v] = [x for x in g.action(ToolCall(id="c", name="mail_send", arguments={}), spec, _turn())
           if x.decision is not Decision.ALLOW]
    assert v.decision is Decision.HITL and v.rule == LOCAL_STRONG_RULE


# ─────────────────────────────── relève mail: ───────────────────────────────


def test_the_mail_trigger_watches_the_local_mailbox(tmp_path, box):
    from nokker.agents.mailwatch import MailWatcher
    from tests.unit.agents.conftest import make_world

    api, _ = _linked(tmp_path, box)
    w = make_world(tmp_path, [("text", "Trié.")] * 10)
    w.watch = MailWatcher(engine=w.engine, mailbox=Mailboxes(local=api.connectors).mail_for, clock=w.clock,
                          poll_s=60)
    w.engine.mail_watch = w.watch
    a = armed_mail_agent(w)  # créé par « alice », locataire interne : la boîte locale
    poll(w, 0)
    uid = box.deliver("billing@example.com", "Facture")
    poll(w)
    runs = [r for r in w.runs(a.id) if (r.event or "").startswith("mail:")]
    assert len(runs) == 1 and f"INBOX:41:{uid}" in runs[0].input
    assert Mailboxes(local=api.connectors).mail_for("tn_autre") is None  # un testeur n'a jamais la boîte locale
    with tenant_scope("tn_autre"):
        assert "aucune boîte" in w.watch.unavailable()


def test_with_both_a_local_and_a_relayed_mailbox_the_local_one_wins(tmp_path, box):
    """Règle de choix : la boîte posée dans Lokkal passe devant la boîte relayée (X20) — pour les outils (première
    source qui déclare un nom) comme pour la relève (qui ne lit jamais une boîte relayée)."""
    from nokker.tools.registry import Registry

    api, _ = _linked(tmp_path, box)

    class Relayed:
        def specs(self):
            return [ToolSpec(name="mail_search", description="relayé", parameters={}, effect=Effect.READ,
                             locality="cloud")]

        def call(self, name, arguments, ctx):
            raise AssertionError("la boîte relayée ne doit pas répondre")

    reg = Registry(sources=[api.connectors.source(), Relayed()])
    assert reg.spec("mail_search").locality == "local"
    box.deliver("billing@example.com", "Locale")
    assert "Locale" in reg.call("mail_search", {"query": "x"}, CTX).content


# ─────────────────────────────── localité ───────────────────────────────


def test_nothing_of_the_local_mailbox_goes_to_the_mirror(tmp_path, box):
    from nokker.core.turn import InputCheck, Message, Turn
    from nokker.local.mirror import MirrorWorker
    from nokker.store.pg import MemoryStore

    api, _ = _linked(tmp_path, box)
    sent = []

    class Client:
        def post(self, url, headers=None, json=None):
            import httpx

            sent.append(json)
            return httpx.Response(200, request=httpx.Request("POST", url), json={})

    def turn(th, text, tools=()):
        msgs = [Message("assistant", "", tool_calls=[ToolCall(id=f"c{i}", name=t, arguments={})])
                for i, t in enumerate(tools)]
        msgs += [Message("tool", f"résultat de {t}", tool_call_id=f"c{i}", name=t)
                 for i, t in enumerate(tools)]
        return Turn(thread_id=th, index=0, input=InputCheck(text=text), sender=LOCAL_OWNER, reply="ok",
                    messages=msgs)

    store = MemoryStore()
    with tenant_scope(INTERNAL_TENANT):
        store.append(turn("th_code", "corrige le test", ["terminal"]))
        store.append(turn("th_mail", "trie ma boîte", ["mail_search", "mail_read"]))
        store.append(turn("th_ar_x", "[Passage de l'agent « Tri »] relève du courrier"))
    runs = {"th_ar_x": "mail:from:billing@example.com"}
    state = {}
    worker = MirrorWorker(store=store, base_url="http://cloud.invalid", token=lambda: "mt", state=state,
                          save=lambda: None, client=Client(),
                          keep_local=keep_local_fn(api.connectors, lambda tid: runs.get(tid)))
    assert worker.push_once() == 1
    pushed = json.dumps(sent, ensure_ascii=False)
    assert "th_code" in pushed and "th_mail" not in pushed and "th_ar_x" not in pushed
    assert "résultat de terminal" in pushed and "résultat de mail_" not in pushed and "relève du courrier" not in pushed
    assert worker.push_once() == 0 and state["mirror_push_cursor"]  # le curseur a sauté les tours restés ici
    for text in (pushed, json.dumps(api.inventory.collect() if api.inventory else {})):
        assert PASSWORD not in text and "imap.corp.test" not in text and "MAIL_PW" not in text


def test_serve_wires_the_watch_on_the_local_mailbox_without_cloud_connectors(tmp_path, box):
    from types import SimpleNamespace

    from nokker.api.server import build_mail_watch
    from tests.unit.agents.conftest import make_world

    api, _ = _linked(tmp_path, box)
    w = make_world(tmp_path)
    cfg = SimpleNamespace(local={"crew": {}}, pg_dsn=lambda: None)
    watch = build_mail_watch(cfg, w.engine, Mailboxes(local=api.connectors, provider=None))
    assert watch is not None and watch.mailbox(INTERNAL_TENANT) is not None


def test_the_new_mirror_worker_keeps_local_turns_and_the_notes_derived_from_them_on_every_channel(tmp_path, box):
    """Miroir refait (canaux, resynchronisation, notes, notes d'équipe) : le tri `keep_local` vaut pour tout canal qui
    envoie des tours (envoi ordinaire ET renvoi après resynchronisation), pour les notes qui en dérivent (`origin` =
    fil#tour) par le canal `notes` comme par les notes d'équipe, et dans le doute ça reste sur la machine."""
    from nokker.context.project_memory import Note
    from nokker.core.turn import InputCheck, Message, Turn
    from nokker.local.mirror import MirrorWorker
    from nokker.local.team_notes import TeamNotesSync
    from nokker.store.pg import MemoryStore

    api, _ = _linked(tmp_path, box)
    posts: dict[str, list] = {}

    class Client:
        def post(self, url, headers=None, json=None):
            import httpx

            posts.setdefault(url.rsplit("/v1/", 1)[1], []).append(json)
            return httpx.Response(200, request=httpx.Request("POST", url), json={})

        def get(self, url, headers=None, params=None):
            import httpx

            return httpx.Response(200, request=httpx.Request("GET", url), json={"notes": [], "turns": []})

    def turn(th, tools=()):
        msgs = [Message("assistant", "", tool_calls=[ToolCall(id=f"c{i}", name=t, arguments={})])
                for i, t in enumerate(tools)]
        return Turn(thread_id=th, index=0, input=InputCheck(text="x"), sender=LOCAL_OWNER, reply="ok", messages=msgs)

    store = MemoryStore()
    with tenant_scope(INTERNAL_TENANT):
        store.append(turn("th_code", ["terminal"]))
        store.append(turn("th_mail", ["mail_read"]))

    def note(name, origin):
        return Note(name=name, project="p-app", kind="fact", scope="user", title=name, text=f"texte {name}",
                    origin=origin)

    notes = [note("de-code", "th_code#0"), note("de-mail", "th_mail#0"), note("sans-tour", ""),
             note("fil-perdu", "th_absent#3")]

    class Memory:
        def notes_for_mirror(self):
            return list(notes)

        def learned(self, mp):
            return [dataclasses.replace(n, scope="project", mirrored=True) for n in notes]

    state = {}
    worker = MirrorWorker(store=store, base_url="http://cloud.invalid", token=lambda: "mt", state=state,
                          save=lambda: None, client=Client(), notes=Memory(),
                          keep_local=keep_local_fn(api.connectors, lambda tid: None))
    assert worker.push_once() == 1 and worker.status()["pending"]["turns"] == 0
    assert [t["thread_id"] for b in posts["mirror/turns"] for t in b["turns"]] == ["th_code"]
    worker.push_notes_once()
    sent_notes = {n["name"] for b in posts["mirror/notes"] for n in b["notes"]}
    assert sent_notes == {"de-code", "sans-tour", "fil-perdu"}  # « de-mail » dérive d'un tour gardé ici
    # resynchronisation : les tours repartent par le même canal, donc par le même tri
    assert worker.request_resync() and worker.resync_once()
    posts.pop("mirror/turns")
    assert worker.push_once() == 1
    assert [t["thread_id"] for b in posts["mirror/turns"] for t in b["turns"]] == ["th_code"]
    # notes d'équipe : le même tri
    sync = TeamNotesSync(base_url="http://cloud.invalid", token=lambda: "mt", state=state, save=lambda: None,
                         memory=Memory(), team=None, stays=worker.note_stays)
    _, leaving = sync._local("p-app")
    assert set(leaving) == {"de-code", "sans-tour", "fil-perdu"}
    # dans le doute (lecture du tour en échec) : la note reste sur la machine
    worker.store = None
    assert worker.note_stays(note("x", "th_code#0")) is True
