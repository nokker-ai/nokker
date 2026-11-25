"""Textes de limite, de quota du foyer et de notification d'une machine, vus de la personne, en fr/en/de/it : français et
anglais figés mot pour mot (empreintes), mêmes trous dans les quatre langues, langue du tour pour les refus et les
avertissements (vrai serveur, vrai noyau), langue du compte pour la notification."""
from __future__ import annotations

import hashlib
import re
from types import SimpleNamespace

import pytest

from nokker.approvals.rule_labels import LANGS
from nokker.cloud import notify as nt
from nokker.core.turn import TurnInput
from nokker.household import quota as hq
from nokker.tenancy import limits as lim
from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope
from tests.unit.test_thread_serialization import _req, _server


def H(t):
    return hashlib.sha256(t.encode()).hexdigest()[:16]

TENANT = "tn_lang"


def _holes(text: str) -> set[str]:
    return set(re.findall(r"\{(\w+)\}", text))


TABLES = {
    **{f"limit.{k}": v for k, v in lim.LIMIT_REPLY_TEXTS.items()},
    **{f"notice.{k}": v for k, v in lim.NOTICE_TEXTS.items()},
    **{f"household.reply.{k}": v for k, v in hq.REPLY_TEXTS.items()},
    **{f"household.notice.{k}": v for k, v in hq.NOTICE_TEXTS.items()},
}


@pytest.mark.parametrize("key", sorted(TABLES))
def test_tables_have_four_languages_and_the_same_holes(key):
    table = TABLES[key]
    assert set(table) == set(LANGS) and all(v.strip() for v in table.values())
    assert _holes(table["en"]) == _holes(table["de"]) == _holes(table["it"]) == _holes(table["fr"]), key
    assert "→" not in table["de"] + table["it"]


def test_limit_and_notice_french_and_english_are_frozen():
    assert {k: H(getattr(lim, k)) for k in ("LIMIT_REPLY_DAY", "LIMIT_REPLY_WEEK", "NOTICE_DAY", "NOTICE_WEEK")} == {
        "LIMIT_REPLY_DAY": "a3e73cd4d8800fae", "LIMIT_REPLY_WEEK": "4fc7cd05683d89a9",
        "NOTICE_DAY": "a63eb75957d4dcf6", "NOTICE_WEEK": "a2a6a1d8f6cfdf0a"}
    for k, whole in (("day", lim.LIMIT_REPLY_DAY), ("week", lim.LIMIT_REPLY_WEEK)):
        assert lim.LIMIT_REPLY_TEXTS[k]["fr"] == whole and lim.LIMIT_REPLY_TEXTS[k]["en"] == whole.split("\n\n")[1]
    for k, whole in (("day", lim.NOTICE_DAY), ("week", lim.NOTICE_WEEK)):
        assert lim.NOTICE_TEXTS[k]["fr"] == whole and lim.NOTICE_TEXTS[k]["en"] == whole.split("\n")[1]
    assert lim.LIMIT_REPLY_TEXTS["day"]["en"].startswith("Daily limit reached: {n} messages per day")


def test_household_french_and_english_are_frozen():
    assert {k: H(v) for k, v in hq.REPLY.items()} == {
        "member_turns_per_day": "8e12fcf01086af5b", "turns_per_day": "e944e0fb88b6f518",
        "turns_per_week": "71adbb50802d6bf0", "suspended": "f972b4645f6b6248", "unavailable": "80c7bed0e42d6172"}
    assert {k: H(v) for k, v in hq.NOTICE.items()} == {
        "turns_per_day": "ae2f15b9d789c7d6", "turns_per_week": "0baf76dc0c985f56",
        "member_turns_per_day": "f335fb1c53b0a59a"}
    for k, v in hq.REPLY.items():
        assert hq.REPLY_TEXTS[k]["fr"] == v and hq.REPLY_TEXTS[k]["en"] == v.split("\n\n")[1]
    for k, v in hq.NOTICE.items():
        assert hq.NOTICE_TEXTS[k]["fr"] == v and hq.NOTICE_TEXTS[k]["en"] == v.split("\n")[1]


def test_notify_french_and_english_are_frozen_and_de_it_have_the_same_keys_and_holes():
    fr = {k: [H(x) for x in v] if isinstance(v, tuple) else H(v) for k, v in nt._TEXTS["fr"].items()}
    en = {k: [H(x) for x in v] if isinstance(v, tuple) else H(v) for k, v in nt._TEXTS["en"].items()}
    assert fr == {"turn_done": ["8c9f085f7772757e", "ace0783e6fb69784"],
                  "awaiting_approval": ["6ad2466d93a81073", "0c6970a6ea473f92"],
                  "agent_failed": ["cc114b7481f6754f", "feba230e92a8ba74"],
                  "agent_done": ["055dd78392edcd86", "8a57b19627cf3235"], "open": "9fb440435a046647"}
    assert en == {"turn_done": ["284e13ca225f49c0", "5a7534556a25c1e6"],
                  "awaiting_approval": ["0545b9a6c4be6be2", "1152bd426ade94e7"],
                  "agent_failed": ["0b818ee4517b28bd", "11153c44e6aa1390"],
                  "agent_done": ["2453e172f8eccbec", "bbc87da5d54e5463"], "open": "ed077f3d8125d60d"}
    assert set(nt._TEXTS) == set(LANGS)
    for lang in LANGS:
        assert set(nt._TEXTS[lang]) == set(nt._TEXTS["fr"])
        for kind in nt.KINDS:
            with_title, bare = nt._TEXTS[lang][kind]
            assert _holes(with_title) == {"m", "t"} and _holes(bare) == {"m"}, (lang, kind)


# ── vrai serveur : limite du jour / de la semaine et avertissement, dans la langue de la requête ──


@pytest.fixture
def limited(tmp_path, monkeypatch):
    monkeypatch.setattr(lim, "current_tenant", lambda: TENANT)  # un compte de la bêta : le quota s'applique
    httpd, port, api, kernel, *_ = _server(tmp_path)

    def setup(**kw):
        kernel.limits = lim.TenantLimits(counter=lim.MemoryUsage(), **kw)

    def say(thread, lang):
        h = {"X-Session-Id": thread, **({"Accept-Language": lang} if lang else {})}
        return _req(port, "/v1/chat/completions", token=api.token, headers=h, timeout=20,
                    body={"messages": [{"role": "user", "content": "salut"}]})

    yield setup, say
    httpd.shutdown()


def _at(rendered: str) -> str:
    return re.search(r"\b(\d\d:\d\d)\b", rendered).group(1)


def test_http_daily_limit_is_in_the_request_language(limited):
    setup, say = limited
    setup(mode="hard", turns_per_day=0, turns_per_week=100, hard_turns_per_day=5)
    for lang, head in (("de", "Tageslimit erreicht: 0 Nachrichten pro Tag"), ("it-CH", "Limite giornaliero raggiunto: 0"),
                       ("en", "Daily limit reached: 0 messages per day"), (None, "Limite du jour atteinte : 0 messages")):
        status, body = say(f"th_lim_day_{lang}", lang)
        assert status == 200 and head in str(body), (lang, body)
    status, body = say("th_lim_day_de2", "de")
    assert "Daily limit" not in str(body) and "Limite du jour" not in str(body)
    assert "(Schweizer Zeit)" in str(body)


def test_http_weekly_limit_is_in_the_request_language(limited):
    setup, say = limited
    setup(mode="hard", turns_per_day=10, turns_per_week=0, hard_turns_per_day=5)
    status, body = say("th_lim_week_de", "de")
    assert status == 200 and "Wochenlimit erreicht: 0 Nachrichten pro Woche" in str(body)
    assert "am Montag um" in str(body) and "Weekly limit" not in str(body)
    status, body = say("th_lim_week_it", "it")
    assert "Limite settimanale raggiunto: 0 messaggi a settimana" in str(body) and "lunedì alle" in str(body)


def test_http_soft_notice_is_in_the_request_language(limited):
    setup, say = limited
    setup(mode="soft", turns_per_day=1, turns_per_week=100, hard_turns_per_day=50)
    assert say("th_notice", "de")[0] == 200  # tour n° 1 : dans le quota
    status, body = say("th_notice", "de")  # tour n° 2 : dépasse, avertissement une fois
    assert status == 200 and "Du hast soeben die 1 Nachrichten pro Tag überschritten" in str(body), body
    assert "Solo-Tarif" in str(body) and "Solo plan" not in str(body)
    assert "soeben" not in str(say("th_notice", "de")[1])  # une seule fois


def test_limits_reply_and_notice_take_an_explicit_language_and_default_to_french(monkeypatch):
    monkeypatch.setattr(lim, "current_tenant", lambda: TENANT)
    limits = lim.TenantLimits(counter=lim.MemoryUsage(), mode="hard", turns_per_day=3, turns_per_week=9)
    assert limits.reply(None, "it").startswith("Limite giornaliero raggiunto: 3 messaggi")
    assert limits.reply(None) == lim.LIMIT_REPLY_TEXTS["day"]["fr"].format(n=3, at=_at(limits.reply(None, "de")))


# ── adaptateurs : quota du foyer (langue du tour) et notification d'une machine (langue du compte) ──


def test_household_quota_refusal_and_notice_follow_the_turn_language(tmp_path):
    from nokker.household.model import Settings
    from tests.unit.household.test_quota_kernel import build

    with tenant_scope(INTERNAL_TENANT):
        w = build(tmp_path, settings=Settings(turns_per_day=1, member_turns_per_day=None, mode="hard"))
        def run(lang, th):
            return w.kernel.run_turn(TurnInput(
                thread_id=th, user_text="salut", channel="web", sender=w.mem.account_id, lang=lang))

        assert run("de", "th_q1").reply.startswith("réponse")
        refused = run("de", "th_q2")
        assert refused.reply.startswith("Tageslimit für den Haushalt erreicht: 1 Nachrichten pro Tag")
        assert "Daily limit" not in refused.reply and "Limite du jour" not in refused.reply
        assert run("it", "th_q3").reply.startswith("Limite giornaliero raggiunto per la famiglia: 1 messaggi")
        assert "Limite du jour atteinte pour le foyer" in run(None, "th_q4").reply
    (tmp_path / "soft").mkdir()
    with tenant_scope(INTERNAL_TENANT):
        w = build(tmp_path / "soft", settings=Settings(turns_per_day=1, member_turns_per_day=None, mode="soft"))
        def run(lang, th):
            return w.kernel.run_turn(TurnInput(
                thread_id=th, user_text="salut", channel="web", sender=w.mem.account_id, lang=lang))

        run("it", "th_s1")
        assert "La famiglia ha appena superato i suoi 1 messaggi al giorno" in run("it", "th_s2").reply


def test_household_quota_unreadable_and_suspended_are_localised(tmp_path):
    from nokker.approvals.rule_labels import lang_scope
    from tests.unit.household.test_quota_kernel import build

    with tenant_scope(INTERNAL_TENANT):
        w = build(tmp_path)
        w.quota._turns = lambda *a, **k: 1 / 0
        with lang_scope("de"):
            t = w.quota.check("th_u", w.mem.account_id)
        assert t.reply == hq.REPLY_TEXTS["unavailable"]["de"]
        with lang_scope("it"):
            assert w.quota.check("th_u", w.mem.account_id).reply == hq.REPLY_TEXTS["unavailable"]["it"]
        assert w.quota.check("th_u", w.mem.account_id).reply == hq.REPLY["unavailable"]


class _Bot:
    def __init__(self):
        self.sent = []

    def send_with_button(self, chat, text, label, url):
        self.sent.append((chat, text, label, url))


@pytest.mark.parametrize("lang,text,label", [
    ("de", "atelier: Deine Sitzung «site» wartet auf dein OK", "Öffnen"),
    ("it", "atelier: la tua sessione «site» attende il tuo ok", "Apri"),
    ("en", "atelier: your session “site” is waiting for your OK", "Open"),
    ("fr", "atelier : ta session « site » attend ton accord", "Ouvrir"),
    (None, "atelier : ta session « site » attend ton accord", "Ouvrir"),
    ("xx", "atelier : ta session « site » attend ton accord", "Ouvrir"),
])
def test_machine_notifier_speaks_the_account_language(lang, text, label):
    bot = _Bot()
    account = SimpleNamespace(lang=lang) if lang is not None else None
    tenancy = SimpleNamespace(account=lambda _id: account, identities_of=lambda _id, _kind: ["4242"])
    machine = SimpleNamespace(id="mc_1", name="atelier", account="us_a", tenant=INTERNAL_TENANT)
    used = nt.MachineNotifier(hub=None, tenancy=tenancy, bot=bot)(
        machine, {"kind": "awaiting_approval", "thread_id": "t1", "title": "site"})
    assert used == ["telegram"]
    assert bot.sent == [("4242", text, label, "https://app.nokker.ai/t/mc_1/t1")]
    assert nt.text_for("agent_done", "atelier", None, "de") == "atelier: Ein Agent ist fertig"
