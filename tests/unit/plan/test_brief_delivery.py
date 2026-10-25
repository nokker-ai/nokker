"""Livraison du brief du matin hors de l'app (docs/LOKKAL.md §7.18.5) : Telegram (bot existant, `sendMessage`
seulement) et/ou e-mail (connecteur mail de la personne), choisis par personne et désactivables ; l'e-mail s'arme par
une carte FORTE la première fois, puis ce brief part tout seul vers CETTE adresse seulement ; même caviardage final que
les tours ; aucun lien qui contourne l'authentification. Faux transports : rien n'est envoyé pour de vrai."""
from __future__ import annotations

import pytest

from nokker.plan.delivery import DELIVERY_TOOL, BriefDelivery, strip_capability_links
from nokker.plan.types import PlanError
from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope
from tests.unit.agents.conftest import SECRET_VALUE
from tests.unit.plan.test_plan import plan_world

BRIEF = (f"Brief : DiskFull brûle. Jeton vu dans un log : {SECRET_VALUE}. Page : "
         "https://poste--pages.nokker.app/_s/cap123/rapport.html et https://app.example.com/plan?token=abc ; "
         "fiche https://docs.example.com/runbook.")


@pytest.fixture(autouse=True)
def _scope():
    with tenant_scope(INTERNAL_TENANT):
        yield


class FakeTransports:
    """Telegram et mail factices : on note ce qui serait parti."""

    def __init__(self):
        self.chats = {"alice": ["4242"]}
        self.sender = {"alice": "alice@example.org"}
        self.telegram: list[tuple[str, str]] = []
        self.mails: list[dict] = []
        self.fail_telegram = False

    def telegram_chats(self, owner):
        return list(self.chats.get(owner, []))

    def send_telegram(self, chat_id, text):
        if self.fail_telegram:
            raise OSError("réseau")
        self.telegram.append((chat_id, text))

    def mail_sender(self, owner):
        return self.sender.get(owner)

    def send_mail(self, owner, to, subject, body):
        self.mails.append({"owner": owner, "to": to, "subject": subject, "body": body})


@pytest.fixture
def dw(tmp_path):
    w, plan = plan_world(tmp_path, [("text", BRIEF)] * 5)
    t = FakeTransports()
    d = BriefDelivery(store=plan.store, transports=t, approvals=w.approvals, scrub=w.kernel.invariants.scrub_output,
                      app_url="https://app.example.com", clock=w.clock)
    plan.delivery = d
    w.executor.handlers[DELIVERY_TOOL] = d.on_arm_decision
    a, _ = plan.ensure_brief({}, "alice")
    w.__dict__.update(plan=plan, d=d, t=t, brief=a)
    return w


def run_brief(w):
    w.clock.t = w.agents.store.get(w.brief.id).next_run_at + 1
    w.engine.tick()


def _err(fn, *args):
    with pytest.raises(PlanError) as e:
        fn(*args)
    return e.value.status, e.value.code


def test_off_by_default_nothing_leaves_the_app(dw):
    assert dw.d.get("alice") == {"telegram": {"enabled": False, "chats": 1},
                                 "email": {"address": None, "from": "alice@example.org", "state": "off",
                                           "approval_id": None},
                                 "last": None}
    run_brief(dw)
    assert dw.t.telegram == [] and dw.t.mails == []


def test_telegram_delivery_is_redacted_and_carries_no_capability_link(dw):
    got = dw.d.put("alice", {"telegram": True})
    assert got["telegram"]["enabled"] is True
    run_brief(dw)
    [(chat, text)] = dw.t.telegram
    assert chat == "4242" and text.startswith("Brief du ")
    assert SECRET_VALUE not in text and "[secret:API_TOKEN]" in text  # même caviardage final que les tours
    assert "/_s/" not in text and "token=" not in text and text.count("[lien retiré]") == 2
    assert "https://docs.example.com/runbook" in text  # un lien ordinaire reste
    assert dw.d.get("alice")["last"]["telegram"] == "sent"


def test_telegram_needs_a_linked_chat_and_can_be_turned_off(dw):
    dw.t.chats = {}
    assert _err(dw.d.put, "alice", {"telegram": True}) == (409, "no_telegram")
    dw.t.chats = {"alice": ["4242"]}
    dw.d.put("alice", {"telegram": True})
    dw.d.put("alice", {"telegram": False})
    run_brief(dw)
    assert dw.t.telegram == []


def test_email_is_armed_by_a_strong_card_then_goes_to_that_address_only(dw):
    got = dw.d.put("alice", {"email": "alice.perso@example.com"})
    assert got["email"]["state"] == "pending" and got["email"]["approval_id"]
    card = dw.approvals.get(got["email"]["approval_id"])
    assert card.strength == "strong" and card.tool_name == DELIVERY_TOOL
    assert card.arguments["to"] == "alice.perso@example.com" and card.arguments["from"] == "alice@example.org"
    run_brief(dw)
    assert dw.t.mails == []  # pas armé : rien ne part
    assert dw.d.get("alice")["last"]["email"] == "not_armed"
    dw.decide(card.id)
    assert dw.d.get("alice")["email"]["state"] == "armed"
    run_brief(dw)
    [mail] = dw.t.mails
    assert mail["to"] == "alice.perso@example.com" and mail["owner"] == "alice"
    assert mail["subject"].startswith("Brief du ") and SECRET_VALUE not in mail["body"]
    assert "/_s/" not in mail["body"] and "https://app.example.com" in mail["body"]


def test_changing_the_address_disarms_and_asks_again(dw):
    first = dw.d.put("alice", {"email": "alice.perso@example.com"})["email"]["approval_id"]
    dw.decide(first)
    second = dw.d.put("alice", {"email": "autre@example.com"})
    assert second["email"]["state"] == "pending" and second["email"]["approval_id"] != first
    run_brief(dw)
    assert dw.t.mails == []  # ni l'ancienne adresse, ni la nouvelle tant que la carte n'est pas signée


def test_a_card_approved_for_an_old_address_arms_nothing(dw):
    first = dw.d.put("alice", {"email": "alice.perso@example.com"})["email"]["approval_id"]
    dw.d.put("alice", {"email": "autre@example.com"})
    assert dw.approvals.get(first).status != "pending"  # l'ancienne carte est retirée
    assert dw.d.get("alice")["email"]["state"] == "pending"


def test_rejected_card_or_changed_mailbox_sends_nothing(dw):
    aid = dw.d.put("alice", {"email": "alice.perso@example.com"})["email"]["approval_id"]
    dw.decide(aid, "reject")
    assert dw.d.get("alice")["email"]["state"] == "not_armed"
    aid = dw.d.put("alice", {"email": "alice.perso@example.com"})["email"]["approval_id"]  # redemandée
    dw.decide(aid)
    dw.t.sender["alice"] = "alice@other.example.org"  # la boîte d'envoi a changé depuis la signature
    run_brief(dw)
    assert dw.t.mails == [] and dw.d.get("alice")["email"]["state"] == "not_armed"


def test_email_needs_a_mail_connector_and_a_valid_address(dw):
    assert _err(dw.d.put, "alice", {"email": "pas-une-adresse"}) == (400, "invalid")
    dw.t.sender = {}
    assert _err(dw.d.put, "alice", {"email": "alice.perso@example.com"}) == (409, "no_mail_connector")
    assert _err(dw.d.put, "alice", {"sms": True}) == (400, "invalid")


def test_turning_email_off(dw):
    dw.decide(dw.d.put("alice", {"email": "alice.perso@example.com"})["email"]["approval_id"])
    assert dw.d.put("alice", {"email": None})["email"]["state"] == "off"
    run_brief(dw)
    assert dw.t.mails == []


def test_a_failed_transport_never_breaks_the_brief(dw):
    dw.d.put("alice", {"telegram": True})
    dw.t.fail_telegram = True
    run_brief(dw)
    [run] = dw.runs(dw.brief.id)
    assert run.status == "succeeded"
    assert dw.d.get("alice")["last"]["telegram"] == "failed"
    assert any(b.kind == "brief" for b in dw.bubbles.bubbles.list_thread(run.thread_id))


def test_capability_links_are_stripped():
    text = ("a https://x.example/_auth?n=1 b https://h.example/p?code=42&x=1 c https://h.example/ok "
            "d https://h.example/s?sig=aa e http://h.example/a?access_token=z")
    out = strip_capability_links(text)
    assert out.count("[lien retiré]") == 4 and "https://h.example/ok" in out


def test_real_telegram_transport_calls_send_message_only():
    from nokker.plan.delivery import RealBriefTransports
    from nokker.telegram.adapter import BotApi

    class Bot(BotApi):
        def __init__(self):
            super().__init__(token="t")
            self.methods = []

        def _call(self, method, payload):
            self.methods.append(method)

    class Tenancy:
        def identities_of(self, account_id, kind):
            return ["4242"] if kind == "telegram" else []

    bot = Bot()
    tr = RealBriefTransports(tenancy=Tenancy(), bot=bot, connectors=None)
    assert tr.telegram_chats("us_x") == ["4242"]
    tr.send_telegram("4242", "x" * 9000)  # découpé en plusieurs messages
    assert bot.methods and set(bot.methods) == {"sendMessage"}
    assert tr.mail_sender("us_x") is None  # pas de connecteurs : pas d'e-mail
