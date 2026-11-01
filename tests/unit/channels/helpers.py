"""Faux partagés des canaux : aucun appel réseau, aucun compte réel."""
from __future__ import annotations

from nokker.approvals.memory import InMemoryApprovals
from nokker.channels.common import ChannelCore, Dispatcher, MemoryDedup, MemoryPairing, MemoryWindows, RateLimiter
from nokker.channels.slack import SlackAdapter
from nokker.channels.whatsapp import WhatsAppAdapter
from nokker.core.turn import Decision, Gate, InputCheck, ToolCall, Turn, Verdict
from nokker.tenancy import MemoryTenancy
from nokker.tenancy.context import current_tenant, scoped_thread_id, tenant_scope

TEAM = "T0TEST"
PHONE_ID = "1234567890"
SECRET_IN_REPLY = "sk-ant-api03-" + "Z" * 40  # forme de clé reconnue par le caviardage


def scrub(text: str) -> str:
    from nokker.qa.secrets import redact

    return redact(text).text


class FakeSlackApi:
    def __init__(self):
        self.posts, self.cards, self.downloads = [], [], []

    def post(self, channel, text, thread_ts=None):
        self.posts.append((channel, text, thread_ts))

    def post_card(self, user, card):
        self.cards.append((user, card))

    def download(self, url, max_bytes):
        self.downloads.append(url)
        return b"OggS-fake", "audio/ogg"


class FakeWaApi:
    def __init__(self):
        self.texts, self.ctas, self.templates, self.media_calls = [], [], [], []

    def send_text(self, to, text):
        self.texts.append((to, text))

    def send_cta(self, to, body, label, url):
        self.ctas.append((to, body, label, url))

    def send_template(self, to, name, lang, params, suffix):
        self.templates.append((to, name, lang, params, suffix))

    def media(self, media_id, max_bytes):
        self.media_calls.append(media_id)
        return b"OggS-fake", "audio/ogg"


class FakeTranscriber:
    def __init__(self, text="transcription de la note"):
        self.text, self.calls = text, 0

    def transcribe(self, data, mime):
        self.calls += 1
        return self.text


class World:
    def __init__(self, reply=None, transcriber=None, rate=(600.0, 100)):
        self.tenancy = MemoryTenancy()
        self.a = self.tenancy.create_account("a@example.org")
        self.b = self.tenancy.create_account("b@example.org", lang="en")
        self.turns = []
        self.reply = reply

        def run_turn(inp):
            self.turns.append((inp, current_tenant()))
            return Turn(thread_id=inp.thread_id, index=0, input=None,
                        reply=self.reply if self.reply is not None else f"réponse à {inp.user_text}")

        self.run_turn = run_turn

        def core(channel, name):
            return ChannelCore(channel=channel, display_name=name, tenancy=self.tenancy, pairing=MemoryPairing(),
                               run_turn=run_turn, scrub=scrub, limiter=RateLimiter(*rate), transcriber=transcriber)

        self.slack_api, self.wa_api = FakeSlackApi(), FakeWaApi()
        self.slack_dedup, self.wa_dedup = MemoryDedup(), MemoryDedup()
        self.slack = SlackAdapter(core=core("slack", "Slack"), api=self.slack_api, team_id=TEAM,
                                  bot_user_id="UBOT", dedup=self.slack_dedup, dispatcher=Dispatcher(sync=True))
        self.clock = [1_800_000_000.0]
        self.wa = WhatsAppAdapter(core=core("whatsapp", "WhatsApp"), api=self.wa_api, phone_number_id=PHONE_ID,
                                  windows=MemoryWindows(), dedup=self.wa_dedup, dispatcher=Dispatcher(sync=True),
                                  clock=lambda: self.clock[0])
        self._n = 0

    # ── Slack ──
    def slack_event(self, user, text, *, channel="D1", kind="message", channel_type="im", thread_ts=None,
                    event_id=None, team=TEAM, **extra):
        self._n += 1
        ev = {"type": kind, "user": user, "text": text, "channel": channel, "ts": f"17000000{self._n:02d}.0001",
              **({"channel_type": channel_type} if kind == "message" else {}), **extra}
        if thread_ts:
            ev["thread_ts"] = thread_ts
        payload = {"type": "event_callback", "team_id": team, "event_id": event_id or f"Ev{self._n:04d}", "event": ev}
        self.slack.handle_event(payload)
        return payload

    def slack_pair(self, account, user):
        code, _ = self.slack.core.pairing.issue(account.account_id)
        self.slack_event(user, f"pair {code}")

    # ── WhatsApp ──
    def wa_message(self, sender, text=None, *, kind="text", wamid=None, ts=None, phone_id=PHONE_ID, **extra):
        self._n += 1
        m = {"from": sender, "id": wamid or f"wamid.{self._n:04d}", "timestamp": str(int(ts or self.clock[0])),
             "type": kind, **extra}
        if kind == "text":
            m["text"] = {"body": text}
        payload = {"object": "whatsapp_business_account", "entry": [{"id": "WABA", "changes": [{"field": "messages",
                   "value": {"messaging_product": "whatsapp", "metadata": {"phone_number_id": phone_id},
                             "messages": [m]}}]}]}
        self.wa.handle(payload)
        return payload

    def wa_pair(self, account, sender):
        code, _ = self.wa.core.pairing.issue(account.account_id)
        self.wa_message(sender, f"pair {code}")


def card_for_account(account, approvals: InMemoryApprovals, strength: str = "strong"):
    with tenant_scope(account.tenant_id):
        turn = Turn(thread_id=scoped_thread_id(account.tenant_id, "s"), index=0, input=InputCheck(text="x"),
                    sender=account.account_id)
        rule = "effect:destructive" if strength == "strong" else "effect:send_external"
        return approvals.request(turn, ToolCall(id="c", name="files_delete", arguments={"path": "/x"}),
                                 Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule=rule,
                                         reason="suppression demandée", strength=strength), None)
