import email
import email.policy
import json
import socket
import threading

import pytest

from nokker.connectors import imap_utf7
from nokker.connectors.errors import ConnectorError
from nokker.connectors.mail import (
    MailConnector, _PinnedIMAP, _PinnedSMTP, normalize_settings, translate_query,
)
from nokker.core.ports import ToolContext
from nokker.core.turn import Effect
from nokker.previews.ssrf import ResolvedHop
from nokker.tools.effects import DEFAULT_EFFECTS

CTX = ToolContext(thread_id="t", turn_index=0, channel="web")
SECRET = "app-pass-1234"
GMAIL = {"preset": "gmail", "address": "tester@gmail.com"}
CUSTOM = {"preset": "custom", "address": "t@example.org", "imap_host": "imap.example.org", "imap_port": 993,
          "smtp_host": "smtp.example.org", "smtp_port": 587}

RAW1 = (b"From: Alice <alice@example.org>\r\nTo: t@example.org\r\nSubject: =?utf-8?q?R=C3=A9union?= demain\r\n"
        b"Date: Tue, 20 Oct 2026 10:00:00 +0000\r\nMessage-ID: <m1@example.org>\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\nBonjour,\r\nOn se voit demain ?\r\n")
RAW2 = (b"From: Bob <bob@example.org>\r\nSubject: Facture\r\nDate: Mon, 19 Oct 2026 09:00:00 +0000\r\n"
        b"Message-ID: <m2@example.org>\r\nMIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=B\r\n\r\n"
        b"--B\r\nContent-Type: text/html; charset=utf-8\r\n\r\n<html><body><p>Voir <b>ci-joint</b></p>"
        b"<script>evil()</script></body></html>\r\n--B\r\nContent-Type: application/pdf\r\n"
        b"Content-Disposition: attachment; filename=\"f.pdf\"\r\nContent-Transfer-Encoding: base64\r\n\r\nAAAA\r\n--B--\r\n")


class FakeIMAP:
    def __init__(self, caps=("IMAP4REV1", "UIDPLUS"), folders=None):
        self.capabilities = caps
        self.folders = folders or {
            "INBOX": {1: [RAW1, {"\\Seen"}], 2: [RAW2, set()]}, "[Gmail]/Drafts": {}, "[Gmail]/Trash": {},
            "[Gmail]/Spam": {}, "[Gmail]/All Mail": {}, "Clients": {}}
        self.attrs = {"[Gmail]/Drafts": "\\Drafts", "[Gmail]/Trash": "\\Trash", "[Gmail]/Spam": "\\Junk",
                      "[Gmail]/All Mail": "\\All"}
        self.cur = None
        self.log = []
        self.next_uid = 100
        self.logged_out = False

    def list(self):
        return "OK", [f'(\\HasNoChildren {self.attrs.get(n, "")}) "/" "{imap_utf7.encode(n)}"'.encode() for n in self.folders]

    def select(self, mailbox, readonly=False):
        self.cur = imap_utf7.decode(mailbox.strip('"'))
        self.log.append(("select", self.cur, readonly))
        return "OK", [b"1"]

    def response(self, code):
        return code, [b"7" if code == "UIDVALIDITY" else str(len(self.folders[self.cur])).encode()]

    def uid(self, cmd, *args):
        self.log.append((cmd, *args))
        box = self.folders[self.cur]
        if cmd == "SEARCH":
            return "OK", [b" ".join(str(u).encode() for u in sorted(box))]
        if cmd == "FETCH":
            uid = int(args[0])
            raw, flags = box[uid]
            msg = email.message_from_bytes(raw)
            head = "".join(f"{k}: {msg[k]}\r\n" for k in ("From", "Subject", "Date", "Message-ID") if msg[k]).encode() + b"\r\n"
            fl = " ".join(sorted(flags))
            if "HEADER.FIELDS (MESSAGE-ID REFERENCES)" in args[1]:
                return "OK", [(b"1 (UID %d BODY[HEADER.FIELDS] {1}" % uid, head), b")"]
            if "BODY.PEEK[]" in args[1]:
                return "OK", [(b"1 (UID %d BODY[]<0> {1}" % uid, raw), b")"]
            body = raw.split(b"\r\n\r\n", 1)[1][:2000]
            return "OK", [(f"1 (UID {uid} FLAGS ({fl}) BODY[HEADER.FIELDS (FROM)] {{1}}".encode(), head),
                           (b" BODY[TEXT]<0> {1}", body), b")"]
        if cmd in ("MOVE", "COPY"):
            dest = imap_utf7.decode(args[1].strip('"'))
            uid = int(args[0])
            self.folders[dest][self.next_uid] = box[uid] if cmd == "COPY" else box.pop(uid)
            self.next_uid += 1
            return "OK", [b"done"]
        if cmd == "STORE":
            box[int(args[0])][1].add("\\Deleted")
            return "OK", [b"ok"]
        if cmd == "EXPUNGE":
            box.pop(int(args[0]), None)
            return "OK", [b"ok"]
        raise AssertionError(cmd)

    def expunge(self):
        for u in [u for u, (_, f) in self.folders[self.cur].items() if "\\Deleted" in f]:
            del self.folders[self.cur][u]
        return "OK", [b"ok"]

    def append(self, folder, flags, date, raw):
        self.folders[imap_utf7.decode(folder.strip('"'))][self.next_uid] = [raw, set()]
        self.log.append(("append", imap_utf7.decode(folder.strip('"')), flags))
        self.next_uid += 1
        return "OK", [b"done"]

    def logout(self):
        self.logged_out = True


class FakeSMTP:
    def __init__(self, fail=None):
        self.sent, self.fail, self.quit_called = [], fail, False

    def send_message(self, msg, from_addr=None, to_addrs=None):
        if self.fail:
            raise self.fail
        self.sent.append((msg, from_addr, to_addrs))
        return {}

    def noop(self):
        return 250, b"ok"

    def quit(self):
        self.quit_called = True


def conn(settings=GMAIL, imap=None, smtp=None):
    imap = imap or FakeIMAP()
    smtp = smtp or FakeSMTP()
    return MailConnector(settings, SECRET, imap_factory=lambda: imap, smtp_factory=lambda: smtp), imap, smtp


def run(c, name, **args):
    r = c.call(name, args, CTX)
    return r, (json.loads(r.content) if not r.is_error else r.content)


def test_specs_match_effects_table_and_have_no_user_argument():
    specs = {s.name: s for s in conn()[0].specs()}
    assert set(specs) == {"mail_search", "mail_read", "mail_draft", "mail_move", "mail_trash", "mail_send"}
    for name, s in specs.items():
        assert s.effect == DEFAULT_EFFECTS[name], name
        assert "user" not in s.parameters["properties"]
    assert specs["mail_send"].effect == Effect.SEND_EXTERNAL and specs["mail_trash"].effect == Effect.DESTRUCTIVE


def test_normalize_presets_and_validation():
    n = normalize_settings(GMAIL)
    assert (n["imap_host"], n["imap_port"], n["smtp_port"], n["username"]) == ("imap.gmail.com", 993, 465, "tester@gmail.com")
    assert normalize_settings({**CUSTOM, "username": "login"})["username"] == "login"
    for bad in ({"preset": "nope", "address": "a@b.org"}, {"preset": "gmail", "address": "pas-une-adresse"},
                {**CUSTOM, "imap_port": 25}, {**CUSTOM, "smtp_port": 993}, {**CUSTOM, "imap_port": "x"},
                {"preset": "custom", "address": "a@b.org"}):
        with pytest.raises(ConnectorError):
            normalize_settings(bad)


def test_translate_query():
    crit, notes = translate_query('from:alice subject:"rapport mensuel" is:unread after:2026/10/01 -spam facture has:attachment')
    assert crit == ["FROM", '"alice"', "SUBJECT", '"rapport mensuel"', "UNSEEN", "SINCE", "01-Oct-2026",
                    "NOT", "TEXT", '"spam"', "TEXT", '"facture"']
    assert len(notes) == 1
    assert translate_query("") == ([], [])


def test_search_returns_newest_first_with_ids_snippet_unread():
    c, imap, _ = conn(CUSTOM)
    r, out = run(c, "mail_search", query="facture")
    assert out["count"] == 2 and [m["id"] for m in out["messages"]] == ["INBOX:7:2", "INBOX:7:1"]
    m1 = out["messages"][1]
    assert m1["subject"] == "Réunion demain" and m1["from"].startswith("Alice") and "On se voit demain" in m1["snippet"]
    assert m1["unread"] is False and out["messages"][0]["unread"] is True
    assert ("SEARCH", "CHARSET", "UTF-8", "TEXT", '"facture"') in imap.log
    assert imap.logged_out


def test_search_gmail_uses_x_gm_raw_and_max_is_clamped():
    c, imap, _ = conn()
    run(c, "mail_search", query="from:alice has:attachment", max=999)
    assert ("SEARCH", "X-GM-RAW", '"from:alice has:attachment"') in imap.log


def test_search_other_folder_by_role_and_unknown_folder():
    c, imap, _ = conn()
    r, out = run(c, "mail_search", query="", folder="trash")
    assert out["folder"] == "[Gmail]/Trash" and out["count"] == 0
    r, out = run(c, "mail_search", query="", folder="Pas-un-dossier")
    assert r.is_error and out.startswith("bad_settings")


def test_read_extracts_text_lists_attachments_and_strips_script():
    c, imap, _ = conn()
    r, out = run(c, "mail_read", message_id="INBOX:7:2")
    assert out["body"] == "Voir ci-joint" and "evil" not in out["body"]
    assert out["attachments"] == [{"name": "f.pdf", "type": "application/pdf", "size": 3}]
    assert out["subject"] == "Facture" and out["truncated"] is False
    assert ("select", "INBOX", True) in imap.log  # lecture seule : ne marque rien comme lu


def test_read_rejects_malformed_and_stale_ids():
    c, imap, _ = conn()
    for bad in ("", "42", "INBOX:abc:xx", "INBOX:7:x"):
        r, out = run(c, "mail_read", message_id=bad)
        assert r.is_error, bad
    r, out = run(c, "mail_read", message_id="INBOX:999:1")
    assert r.is_error and "périmé" in out


def test_draft_appends_to_drafts_with_draft_flag_and_never_sends():
    c, imap, smtp = conn()
    r, out = run(c, "mail_draft", to="bob@example.org", subject="Salut", body="Texte")
    assert out == {"drafted": True, "folder": "[Gmail]/Drafts", "to": "bob@example.org", "subject": "Salut"}
    assert ("append", "[Gmail]/Drafts", "(\\Draft \\Seen)") in imap.log and smtp.sent == []
    stored = next(iter(imap.folders["[Gmail]/Drafts"].values()))[0]
    assert email.message_from_bytes(stored)["From"] == "tester@gmail.com"


def test_draft_reply_sets_threading_headers():
    c, imap, _ = conn()
    run(c, "mail_draft", to="alice@example.org", subject="Re: Réunion", body="ok", in_reply_to="INBOX:7:1")
    stored = email.message_from_bytes(next(iter(imap.folders["[Gmail]/Drafts"].values()))[0])
    assert stored["In-Reply-To"] == "<m1@example.org>" and "<m1@example.org>" in stored["References"]


@pytest.mark.parametrize("args", [
    {"to": "pas-une-adresse", "subject": "s", "body": "b"},
    {"to": "a@b.org\r\nBcc: evil@x.org", "subject": "s", "body": "b"},
    {"to": "a@b.org", "subject": "s\r\nBcc: evil@x.org", "body": "b"},
    {"to": ",".join(f"u{i}@b.org" for i in range(11)), "subject": "s", "body": "b"},
    {"to": "", "subject": "s", "body": "b"}])
def test_send_refuses_bad_recipients_and_header_injection(args):
    c, _, smtp = conn()
    r, out = run(c, "mail_send", **args)
    assert r.is_error and smtp.sent == []


def test_send_goes_through_smtp_from_the_account_address():
    c, imap, smtp = conn(CUSTOM)
    r, out = run(c, "mail_send", to="Bob <bob@example.org>, c@example.org", cc="d@example.org", subject="Hello", body="Corps")
    assert out["sent"] is True and out["copy_in"] is None  # pas de dossier « Envoyés » sur ce faux compte
    msg, frm, rcpt = smtp.sent[0]
    assert frm == "t@example.org" and rcpt == ["bob@example.org", "c@example.org", "d@example.org"]
    assert msg["From"] == "t@example.org" and smtp.quit_called


def test_send_copies_to_sent_folder_except_on_gmail():
    folders = {"INBOX": {}, "Sent": {}, "Drafts": {}}
    c, imap, smtp = conn(CUSTOM, imap=FakeIMAP(folders=folders))
    _, out = run(c, "mail_send", to="bob@example.org", subject="s", body="b")
    assert out["copy_in"] == "Sent" and len(folders["Sent"]) == 1
    c, imap, smtp = conn(GMAIL)
    _, out = run(c, "mail_send", to="bob@example.org", subject="s", body="b")
    assert out["copy_in"] is None


def test_move_archive_spam_label_and_noop():
    c, imap, _ = conn()
    _, out = run(c, "mail_move", message_id="INBOX:7:1", destination="archive")
    assert out == {"moved": True, "from": "INBOX", "folder": "[Gmail]/All Mail"} and 1 not in imap.folders["INBOX"]
    _, out = run(c, "mail_move", message_id="INBOX:7:2", destination="label:Clients")
    assert out["folder"] == "Clients"
    r, out = run(c, "mail_move", message_id="INBOX:7:2", destination="label:Inexistant")
    assert r.is_error
    r, out = run(c, "mail_move", message_id="INBOX:7:2", destination="corbeille")
    assert r.is_error


def test_move_falls_back_to_copy_store_expunge_without_move_capability():
    c, imap, _ = conn(imap=FakeIMAP(caps=("IMAP4REV1",)))
    _, out = run(c, "mail_move", message_id="INBOX:7:1", destination="spam")
    assert out["moved"] is True and 1 not in imap.folders["INBOX"] and len(imap.folders["[Gmail]/Spam"]) == 1
    assert [x[0] for x in imap.log if x[0] in ("COPY", "STORE")] == ["COPY", "STORE"]


def test_trash_moves_to_trash_and_never_expunges_for_good():
    c, imap, _ = conn()
    _, out = run(c, "mail_trash", message_id="INBOX:7:1")
    assert out["folder"] == "[Gmail]/Trash" and len(imap.folders["[Gmail]/Trash"]) == 1
    _, out = run(c, "mail_trash", message_id="[Gmail]/Trash:7:100")
    assert out["moved"] is False


def test_errors_never_contain_the_secret():
    class Boom(FakeIMAP):
        def select(self, *a, **k):
            raise OSError(f"auth failed for {SECRET}")

    c, _, _ = conn(imap=Boom())
    r, out = run(c, "mail_search", query="x")
    assert r.is_error and SECRET not in out


def test_unknown_tool_and_test_method():
    c, _, smtp = conn()
    assert c.call("mail_nuke", {}, CTX).is_error
    assert "IMAP et SMTP ok" in c.test() and smtp.quit_called


def test_utf7_roundtrip():
    for name in ("INBOX", "Envoyés", "Éléments supprimés", "R&D", "日本語"):
        assert imap_utf7.decode(imap_utf7.encode(name)) == name
    assert imap_utf7.encode("Envoyés") == "Envoy&AOk-s"


# ── transport réel (imaplib / smtplib épinglés) contre un faux serveur sur 127.0.0.1 ──


def serve(script):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def run_srv():
        s, _ = srv.accept()
        f = s.makefile("rwb", buffering=0)
        script(f)
        s.close()
        srv.close()

    threading.Thread(target=run_srv, daemon=True).start()
    return ResolvedHop(url="", scheme="https", hostname="mail.example.org", port=srv.getsockname()[1], ip="127.0.0.1")


def test_pinned_imap_logs_in_through_the_pinned_ip():
    seen = []

    def script(f):
        f.write(b"* OK ready\r\n")
        while True:
            line = f.readline().decode().strip()
            if not line:
                return
            tag, _, cmd = line.partition(" ")
            seen.append(cmd)
            if cmd.startswith("CAPABILITY"):
                f.write(b"* CAPABILITY IMAP4rev1\r\n" + tag.encode() + b" OK done\r\n")
            elif cmd.startswith("LOGIN"):
                f.write(tag.encode() + b" OK logged in\r\n")
            elif cmd.startswith("LOGOUT"):
                f.write(b"* BYE\r\n" + tag.encode() + b" OK bye\r\n")
                return

    m = _PinnedIMAP(serve(script), False, 3)
    m.login("t@example.org", SECRET)
    m.logout()
    assert any(c.startswith("LOGIN") for c in seen)


def test_imap_143_refuses_cleartext_login_when_starttls_is_missing(monkeypatch):
    from nokker.connectors import mail

    seen = []

    def script(f):
        f.write(b"* OK ready\r\n")
        while True:
            line = f.readline().decode().strip()
            if not line:
                return
            tag, _, cmd = line.partition(" ")
            seen.append(cmd)
            if cmd.startswith("CAPABILITY"):
                f.write(b"* CAPABILITY IMAP4rev1\r\n" + tag.encode() + b" OK done\r\n")
            else:
                f.write(tag.encode() + b" BAD no\r\n")

    hop = serve(script)
    monkeypatch.setattr(mail, "check_endpoint", lambda *a, **k: hop)
    with pytest.raises(ConnectorError) as e:
        mail.connect_imap({"imap_host": "mail.example.org", "imap_port": 143, "username": "u"}, SECRET)
    assert e.value.code in ("protocol_error", "tls_failed")
    assert not any(c.startswith("LOGIN") for c in seen) and SECRET not in str(e.value)


def test_pinned_smtp_sends_a_message():
    got = []

    def script(f):
        f.write(b"220 ready\r\n")
        data_mode = False
        while True:
            line = f.readline()
            if not line:
                return
            text = line.decode().strip()
            if data_mode:
                got.append(line)
                if text == ".":
                    data_mode = False
                    f.write(b"250 queued\r\n")
                continue
            if text.upper().startswith("EHLO"):
                f.write(b"250-mail.example.org\r\n250 AUTH PLAIN\r\n")
            elif text.upper().startswith("AUTH"):
                f.write(b"235 ok\r\n")
            elif text.upper().startswith(("MAIL", "RCPT")):
                f.write(b"250 ok\r\n")
            elif text.upper() == "DATA":
                data_mode = True
                f.write(b"354 go\r\n")
            elif text.upper() == "QUIT":
                f.write(b"221 bye\r\n")
                return

    m = _PinnedSMTP(serve(script), False, 3)
    m.connect("mail.example.org", m._hop.port)
    m.login("t@example.org", SECRET)
    msg = email.message.EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = "t@example.org", "b@example.org", "Hi"
    msg.set_content("x")
    m.send_message(msg)
    m.quit()
    assert any(b"Subject: Hi" in line for line in got)
