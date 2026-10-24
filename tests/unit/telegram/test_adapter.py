"""Canal Telegram de la bêta : appairage, un tour par compte dans SON locataire, rien pour un id non relié."""
from __future__ import annotations

from nokker.core.turn import Turn
from nokker.telegram.adapter import TelegramAdapter, split_message
from nokker.telegram.pairing import MemoryPairing
from nokker.tenancy import MemoryTenancy
from nokker.tenancy.context import current_tenant, tenant_of_thread


class FakeBot:
    def __init__(self):
        self.sent = []

    def send(self, chat_id, text):
        self.sent.append((chat_id, text))

    def typing(self, chat_id):
        pass


def setup():
    tenancy, pairing, bot, turns = MemoryTenancy(), MemoryPairing(), FakeBot(), []

    def run_turn(inp):
        turns.append((inp, current_tenant()))
        return Turn(thread_id=inp.thread_id, index=0, input=None, reply=f"réponse à {inp.user_text}")

    adapter = TelegramAdapter(tenancy=tenancy, pairing=pairing, run_turn=run_turn, bot=bot)
    adapter.handle_update = lambda u: adapter._process(u["message"])  # synchrone pour le test
    return tenancy, pairing, bot, turns, adapter


def msg(tg_id, text, chat_type="private", lang="fr"):
    return {"message": {"chat": {"id": tg_id, "type": chat_type}, "from": {"id": tg_id, "language_code": lang},
                        "text": text}}


def test_unpaired_id_gets_instructions_and_no_turn():
    tenancy, pairing, bot, turns, adapter = setup()
    adapter.handle_update(msg(111, "salut"))
    assert turns == [] and "/start" in bot.sent[-1][1]


def test_pairing_then_turns_run_in_the_account_tenant():
    tenancy, pairing, bot, turns, adapter = setup()
    a, b = tenancy.create_account("a@x.ch"), tenancy.create_account("b@x.ch")
    code_a, _ = pairing.issue(a.account_id)
    code_b, _ = pairing.issue(b.account_id)
    adapter.handle_update(msg(111, f"/start {code_a}"))
    adapter.handle_update(msg(222, f"/start {code_b.lower()}"))
    adapter.handle_update(msg(111, "bonjour"))
    adapter.handle_update(msg(222, "bonjour"))
    (inp_a, ten_a), (inp_b, ten_b) = turns
    assert ten_a == a.tenant_id and inp_a.sender == a.account_id and tenant_of_thread(inp_a.thread_id) == a.tenant_id
    assert ten_b == b.tenant_id and inp_a.thread_id != inp_b.thread_id
    assert bot.sent[-1] == (222, "réponse à bonjour")


def test_code_is_single_use_and_an_id_cannot_join_a_second_account():
    tenancy, pairing, bot, turns, adapter = setup()
    a, b = tenancy.create_account("a@x.ch"), tenancy.create_account("b@x.ch")
    code, _ = pairing.issue(a.account_id)
    adapter.handle_update(msg(111, f"/start {code}"))
    adapter.handle_update(msg(333, f"/start {code}"))
    assert "pas valable" in bot.sent[-1][1]
    code_b, _ = pairing.issue(b.account_id)
    adapter.handle_update(msg(111, f"/start {code_b}"))
    assert "autre compte" in bot.sent[-1][1]
    assert tenancy.account_for_identity("telegram", "111") == a


def test_groups_are_ignored():
    tenancy, pairing, bot, turns, adapter = setup()
    real = TelegramAdapter(tenancy=tenancy, pairing=pairing, run_turn=lambda i: None, bot=bot)
    real.handle_update(msg(-100, "salut", chat_type="group"))
    real._pool.shutdown(wait=True)
    assert bot.sent == []


def test_long_replies_are_split_under_the_telegram_limit():
    parts = split_message(("ligne\n" * 1500).strip())
    assert len(parts) > 1 and all(len(p) <= 4000 for p in parts)


def test_tester_card_goes_to_the_tester_telegram_only():
    from nokker.approvals.memory import InMemoryApprovals
    from nokker.core.turn import Decision, Gate, InputCheck, ToolCall, Turn, Verdict
    from nokker.telegram.notify import TesterApprovalNotifier
    from nokker.tenancy.context import scoped_thread_id, tenant_scope

    tenancy, bot = MemoryTenancy(), FakeBot()
    a, b = tenancy.create_account("a@x.ch"), tenancy.create_account("b@x.ch")
    tenancy.bind_identity(a.account_id, "telegram", "111")
    tenancy.bind_identity(b.account_id, "telegram", "222")
    approvals = InMemoryApprovals()
    approvals.tester_notifier = TesterApprovalNotifier(tenancy=tenancy, bot=bot, app_url="https://app.nokker.ai")
    with tenant_scope(a.tenant_id):
        turn = Turn(thread_id=scoped_thread_id(a.tenant_id, "s"), index=0, input=InputCheck(text="x"), sender=a.account_id)
        card = approvals.request(turn, ToolCall(id="c", name="mail_send", arguments={"to": "x@y.ch"}),
                                 Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule="effect:send_external",
                                         reason="hors allowlist"), None)
    assert approvals.notifier.sent == []  # jamais Alice ni Bruno
    assert [c for c, _ in bot.sent] == ["111"] and card.id in bot.sent[0][1] and "mail_send" in bot.sent[0][1]
