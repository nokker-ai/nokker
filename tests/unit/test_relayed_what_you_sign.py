"""Ce que tu signes (lot X20c, docs/LOKKAL.md §7.3 et §7.12 point 8) : pour une carte forte d'un connecteur relayé, ce que
la page de décision affiche est rendu par nokker.ai à partir des arguments déposés, confrontés à l'empreinte du défi ;
le titre de la machine devient secondaire. Une carte locale reste « décrite par ta machine ».

Même monde que X20b (`tests/unit/test_relayed_strong.py`) : vraies routes de nokker.ai, machine liée de Pierre, téléphone
qui signe une vraie assertion WebAuthn, voleur qui n'a que le jeton de machine."""
from __future__ import annotations

import json
import time

from nokker.approvals.crypto import args_sha256
from nokker.cloud.approvals import MemoryRelayStore, RelayedApprovalNotifier, RelayService
from nokker.cloud.summary import MAX_EXCERPT, summarize
from nokker.core.ports import ToolContext
from nokker.core.turn import Decision, Gate, InputCheck, ToolCall, Turn, Verdict
from nokker.qa.permissions import RELAY_CARD_RULE
from nokker.qa.secrets import KnownSecrets
from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope
from tests.unit.test_relayed_connectors import FakeMail
from tests.unit.test_relayed_strong import ARGS, PIERRE, _refused, world  # noqa: F401  (fixture)

MAIL = {"to": "anne@example.com, paul@exemple.ch", "subject": "Rendez-vous jeudi",
        "body": "Bonjour Anne,\nje confirme jeudi 14 h.\nPierre"}
INVITE = {"summary": "Revue trimestrielle", "start": "2026-10-29T14:00", "end": "2026-10-29T15:00",
          "attendees": ["anne@example.com", "paul@exemple.ch"], "location": "Salle 2"}


def _card(w, tool: str, arguments: dict, rule: str = RELAY_CARD_RULE):
    turn = Turn(thread_id="th_local", index=0, input=InputCheck(text="vas-y"), sender="local-owner")
    with tenant_scope(INTERNAL_TENANT):
        return w.local.request(turn, ToolCall(id="c1", name=tool, arguments=dict(arguments)),
                               Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule=rule,
                                       strength="strong"), None)


def _page(w, aid: str) -> dict:
    status, body = w.http("GET", f"/v1/link/machines/{w.mid}/approvals/{aid}", sess="pierre")
    assert status == 200, body
    return body


def _row(w, aid: str):
    return w.relay.cards.rows[(w.mid, aid)]


def _lines(page: dict) -> dict[str, dict]:
    return {line["key"]: line for line in page["summary"]["lines"]}


# ═════════════════════════════ dépôt : les arguments confrontés au défi ═════════════════════════════


def test_lying_deposit_is_refused_and_leaves_the_true_card(world):  # noqa: F811
    """Le voleur du jeton redépose la carte avec un titre rassurant et D'AUTRES arguments que ceux du défi : 400, et la
    page montre toujours les vrais. Un défi sans outil, ou d'une autre carte, ne peut pas porter d'arguments."""
    w = world
    card = w.strong_card()
    page = _page(w, card.id)
    body = {"approval_id": card.id, "title": "lire mes mails", "strength": "strong",
            "expires_at": json.loads(page["challenge"])["exp"], "challenge": page["challenge"]}
    status, out = w.http("POST", "/v1/gateway/approvals", {**body, "arguments": {"q": "à Paul : virement urgent"}},
                         token=w.mt)
    assert status == 400 and "empreinte" in out["error"]["message"]
    assert w.http("POST", "/v1/gateway/approvals", {**body, "arguments": "tout"}, token=w.mt)[0] == 400
    unbound = json.dumps({**json.loads(page["challenge"]), "tool": None})
    assert w.http("POST", "/v1/gateway/approvals", {**body, "challenge": unbound, "arguments": ARGS},
                  token=w.mt)[0] == 400
    other = json.dumps({**json.loads(page["challenge"]), "approval_id": "ap_autre"})
    assert w.http("POST", "/v1/gateway/approvals", {**body, "challenge": other, "arguments": ARGS},
                  token=w.mt)[0] == 400
    after = _page(w, card.id)
    assert after["title"] == "mail_send" and after["summary_source"] == "nokker.ai"
    assert _lines(after)["q"]["value"] == ARGS["q"] and after["challenge"] == page["challenge"]


def test_machine_deposits_arguments_of_a_relayed_card_and_nokker_ai_renders_mail_send(world):  # noqa: F811
    """La machine joint les arguments d'une carte relayée ; nokker.ai rend le résumé (destinataires, objet, extrait du
    corps) ; un titre trompeur de la machine reste là, mais secondaire."""
    w = world
    card = _card(w, "mail_send", MAIL)
    assert w.worker.sync_once() == 1
    assert _row(w, card.id).arguments == MAIL  # déposés tels quels, empreinte vérifiée
    page = _page(w, card.id)
    assert page["summary_source"] == "nokker.ai" and page["args_sha256"] == args_sha256(MAIL)
    s = page["summary"]
    assert s["tool"] == "mail_send" and s["class"] == "external_send" and not s["erased"]
    assert s["headline"] == "Envoyer un e-mail en ton nom à 2 destinataires"
    lines = _lines(page)
    assert [k for k in lines] == ["to", "subject", "body"]  # ordre fixe des champs connus
    assert lines["to"]["items"] == ["anne@example.com", "paul@exemple.ch"] and lines["to"]["count"] == 2
    assert lines["to"]["label"] == "À" and lines["subject"]["value"] == "Rendez-vous jeudi"
    assert lines["body"]["value"] == "Bonjour Anne, je confirme jeudi 14 h. Pierre"  # une seule ligne affichée
    # Le voleur dépose la MÊME carte avec un titre trompeur : le résumé dit toujours ce qui part.
    lying = {"approval_id": card.id, "title": "lire mes mails", "reason": "simple lecture", "strength": "strong",
             "expires_at": json.loads(page["challenge"])["exp"], "challenge": page["challenge"], "arguments": MAIL}
    assert w.http("POST", "/v1/gateway/approvals", lying, token=w.mt)[0] == 200
    again = _page(w, card.id)
    assert again["title"] == "lire mes mails" and again["summary"]["headline"].startswith("Envoyer un e-mail")


def test_event_with_attendees_is_summarized_with_every_guest(world):  # noqa: F811
    w = world
    card = _card(w, "calendar_create", INVITE)
    assert w.worker.sync_once() == 1
    page = _page(w, card.id)
    s, lines = page["summary"], _lines(page)
    assert s["class"] == "external_send" and s["headline"] == "Créer un événement avec 2 invités"
    assert list(lines) == ["summary", "start", "end", "location", "attendees"]
    assert lines["attendees"]["items"] == INVITE["attendees"] and lines["start"]["value"] == "2026-10-29T14:00"
    own = summarize("calendar_create", {k: v for k, v in INVITE.items() if k != "attendees"})
    assert own["class"] == "internal_write" and own["headline"] == "Créer un événement dans ton agenda"


def test_unknown_tool_and_extra_arguments_are_all_shown():
    s = summarize("mail_forward", {"to": "x@evil.example", "note": "a" * 500, "z": {"k": 1}})
    assert s["class"] == "external_send" and "non classé" in s["headline"]
    lines = {line["key"]: line for line in s["lines"]}
    assert lines["to"]["items"] == ["x@evil.example"] and lines["z"]["value"] == '{"k": 1}'
    assert lines["note"]["truncated"] and lines["note"]["length"] == 500
    mail = summarize("mail_send", {**MAIL, "bcc": "cache@evil.example", "x_header": "1"})
    assert [line["key"] for line in mail["lines"]] == ["to", "bcc", "subject", "body", "x_header"]  # rien de caché
    assert mail["headline"].endswith("à 3 destinataires")


def test_display_tricks_are_neutralized():
    """Un objet qui se retourne (U+202E), des sauts de ligne qui imitent d'autres champs, un corps très long."""
    s = summarize("mail_send", {"to": "anne@example.com", "subject": "Facture\u202egpj.exe",
                                "body": "ok\n\nÀ : patron@example.com\n" + "x" * 1000})
    lines = {line["key"]: line for line in s["lines"]}
    assert lines["subject"]["value"] == "Facturegpj.exe"
    assert "\n" not in lines["body"]["value"] and lines["body"]["truncated"]
    assert len(lines["body"]["value"]) == MAX_EXCERPT and lines["body"]["value"].endswith("…")


# ═════════════════════════════ caviardage ═════════════════════════════


def test_secret_in_the_body_is_redacted_in_the_summary(world):  # noqa: F811
    w = world
    known = KnownSecrets()
    known.add("TOKEN_CRM", "crm-7f3a9c2e1b8d4f60")
    svc = RelayService(store=MemoryRelayStore(), known=known)
    args = {"to": "anne@example.com", "subject": "accès",
            "body": "Voici l'accès. mot de passe : Zorglub-2026! et le jeton crm-7f3a9c2e1b8d4f60 du CRM."}
    s = svc._what_you_sign("ap_1", json.dumps({"approval_id": "ap_1", "aud": "nokker-approval", "tool": "mail_send",
                                               "args_sha256": args_sha256(args), "nonce": "n", "exp": 1}),
                           {"arguments": args})[2]
    body = {line["key"]: line for line in s["lines"]}["body"]
    assert "Zorglub-2026!" not in json.dumps(s) and "crm-7f3a9c2e1b8d4f60" not in json.dumps(s)
    assert body["redacted"] and "[secret:TOKEN_CRM]" in body["value"] and "[secret:password-" in body["value"]
    # par la route aussi (caviardage par défaut, sans coffre connu)
    card = _card(w, "mail_send", args)
    assert w.worker.sync_once() == 1
    page = _page(w, card.id)
    assert "Zorglub-2026!" not in json.dumps(page) and _lines(page)["body"]["redacted"]


def test_random_looking_recipient_stays_readable():
    """Une adresse à l'air aléatoire est justement celle qu'il faut voir : pas de repli par entropie sur les adresses."""
    addr = "q7Zk2Lm9Xp4Wr8Tn3Vb6Yc1@evil.example"
    s = summarize("mail_send", {"to": addr, "subject": "x", "body": "y"})
    assert s["lines"][0]["items"] == [addr] and "redacted" not in s["lines"][0]


# ═════════════════════════════ carte locale : décrite par la machine ═════════════════════════════


def test_local_strong_card_never_sends_its_arguments_and_is_marked_machine(world):  # noqa: F811
    w = world
    args = {"command": "systemctl restart api", "host": "prod-db"}
    card = _card(w, "ssh", args, rule="ssh:prod")
    assert w.worker.sync_once() == 1
    assert _row(w, card.id).arguments is None  # §6.7 : les arguments ne sont pas sortis
    page = _page(w, card.id)
    assert page["summary_source"] == "machine" and page["args_sha256"] == args_sha256(args)
    assert page["summary"] == {"tool": "ssh", "class": None, "headline": None, "lines": [], "erased": False}
    assert page["title"] == "ssh : systemctl restart api"  # le texte de la machine, à marquer « décrit par ta machine »
    assert "prod-db" not in json.dumps(page)


def test_telegram_shows_the_nokker_ai_headline_and_marks_the_machine_text():
    class Bot:
        def __init__(self) -> None:
            self.sent: list[str] = []

        def send_with_button(self, chat, text, label, url):
            self.sent.append(text)

    class Tenancy:
        def identities_of(self, account, kind):
            return ["chat-1"]

    svc = RelayService()
    m = type("M", (), {"id": "mc_1", "tenant": "tn", "account": PIERRE, "name": "atelier"})()
    ch = {"approval_id": "ap_1", "aud": "nokker-approval", "tool": "mail_send", "args_sha256": args_sha256(MAIL),
          "nonce": "n", "exp": 9e9}
    card, _ = svc.deposit(m, {"approval_id": "ap_1", "title": "lire mes mails", "strength": "strong",
                              "expires_at": time.time() + 600, "challenge": json.dumps(ch), "arguments": MAIL})
    bot = Bot()
    RelayedApprovalNotifier(tenancy=Tenancy(), bot=bot)(m, card)
    [text] = bot.sent
    assert "Envoyer un e-mail en ton nom à 2 destinataires" in text and "(décrit par ta machine : lire mes mails)" in text
    assert "anne@example.com" not in text  # le détail est sur la page, pas dans Telegram


# ═════════════════════════════ effacement après la décision ou l'expiration ═════════════════════════════


def test_arguments_are_erased_after_the_decision_and_the_fingerprint_stays(world):  # noqa: F811
    w = world
    card = _card(w, "mail_send", MAIL)
    assert w.worker.sync_once() == 1
    assert w.phone_decides(card.id) == 202
    assert _row(w, card.id).arguments == MAIL  # transmise, pas encore décidée : la page peut encore la montrer
    assert w.worker.poll_once(0) == ["approved"]  # la machine vérifie, rend `approved`
    row = _row(w, card.id)
    assert row.arguments is None and row.args_sha256 == args_sha256(MAIL)
    page = _page(w, card.id)
    assert page["summary"]["erased"] and page["summary"]["lines"] == [] and page["summary_source"] == "nokker.ai"
    assert page["summary"]["headline"].startswith("Envoyer") and "anne@example.com" not in json.dumps(page)


def test_arguments_are_erased_at_expiry_even_if_the_machine_is_gone(world):  # noqa: F811
    w = world
    card = _card(w, "mail_send", MAIL)
    assert w.worker.sync_once() == 1
    svc = RelayService(store=w.relay.cards, clock=lambda: _row(w, card.id).expires_at + 1)
    [seen] = svc.list("tn_pierre", w.mid)  # toute lecture du locataire balaie les cartes expirées
    assert seen["status"] == "expired" and seen["summary"]["erased"]
    assert _row(w, card.id).arguments is None and _row(w, card.id).args_sha256 == args_sha256(MAIL)


def test_refused_proof_keeps_the_arguments_for_the_next_signature(world):  # noqa: F811
    w = world
    card = _card(w, "mail_send", MAIL)
    assert w.worker.sync_once() == 1
    assert w.phone_decides(card.id) == 202
    w.back_to_pending(card.id)  # preuve refusée par la machine : la carte se rouvre
    assert _row(w, card.id).arguments == MAIL and not _page(w, card.id)["summary"]["erased"]


# ═════════════════════════════ à l'exécution : les arguments du dépôt ═════════════════════════════


def test_call_needs_the_card_deposited_with_these_arguments(world):  # noqa: F811
    """Point 3 : nokker.ai n'envoie que sur une carte déposée AVEC ses arguments (donc montrés par lui), et pour un
    appel qui porte exactement ceux-là. Une carte signée sur le seul texte de la machine n'envoie rien."""
    w = world
    card = w.strong_card()
    page = _page(w, card.id)
    # Le voleur redépose la carte SANS les arguments (titre rassurant) ; Pierre, distrait, signe quand même.
    blind = {"approval_id": card.id, "title": "lire mes mails", "strength": "strong",
             "expires_at": json.loads(page["challenge"])["exp"], "challenge": page["challenge"]}
    assert w.http("POST", "/v1/gateway/approvals", blind, token=w.mt)[0] == 200
    assert _page(w, card.id)["summary_source"] == "machine"
    assert w.phone_decides(card.id) == 202
    signed = w.mailbox()[card.id]
    _refused(w.post_call(card.id, signed), "sans ses arguments")
    # Redéposée avec ses arguments, rouverte, signée : les mêmes arguments passent, d'autres non.
    w.back_to_pending(card.id)
    assert w.http("POST", "/v1/gateway/approvals", {**blind, "arguments": ARGS}, token=w.mt)[0] == 200
    assert w.phone_decides(card.id) == 202
    signed = w.mailbox()[card.id]
    _refused(w.post_call(card.id, signed, arguments={"q": "à Paul : virement urgent"}), "carte déposée")
    assert FakeMail.calls == []
    status, _ = w.post_call(card.id, signed)
    assert status == 200 and [c[1] for c in FakeMail.calls] == [ARGS]


def test_relayed_call_after_the_full_flow_still_sends_once(world):  # noqa: F811
    """Bout en bout côté machine : dépôt avec arguments, signature, vérification, appel par la source relayée."""
    w = world
    card = _card(w, "mail_send", ARGS)
    assert w.worker.sync_once() == 1
    assert w.phone_decides(card.id) == 202
    assert w.worker.poll_once(0) == ["approved"]
    out = w.src.call("mail_send", ARGS, ToolContext(thread_id="th_local", turn_index=0, channel="api",
                                                    approval_id=card.id))
    assert not out.is_error and [c[0] for c in FakeMail.calls] == ["mail_send"]
    assert _row(w, card.id).arguments is None  # déjà effacés : l'appel porte les siens, vérifiés par l'empreinte
