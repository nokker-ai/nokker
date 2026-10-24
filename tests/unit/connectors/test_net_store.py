import socket
import threading

import pytest
from cryptography.fernet import Fernet

from nokker.connectors.crypto import SecretBox
from nokker.connectors.errors import ConnectorError, scrub
from nokker.connectors.net import check_endpoint, open_pinned
from nokker.connectors.store import MemoryConnectorStore
from nokker.previews.ssrf import ResolvedHop


def resolver_for(*ips):
    def resolve(host, port, proto=0):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ips]

    return resolve


def test_check_endpoint_accepts_public_host_and_port():
    hop = check_endpoint("IMAP.Example.org.", 993, "imap", resolver=resolver_for("93.184.216.34"))
    assert (hop.hostname, hop.port, hop.ip) == ("imap.example.org", 993, "93.184.216.34")


@pytest.mark.parametrize("ip", ["10.0.0.5", "127.0.0.1", "100.64.1.2", "169.254.169.254", "192.168.1.1", "::1", "fd00::1"])
def test_private_addresses_are_blocked(ip):
    with pytest.raises(ConnectorError) as e:
        check_endpoint("mail.example.org", 993, "imap", resolver=resolver_for(ip))
    assert e.value.code == "host_blocked"


def test_mixed_public_and_private_answers_are_blocked():
    with pytest.raises(ConnectorError) as e:
        check_endpoint("mail.example.org", 993, "imap", resolver=resolver_for("93.184.216.34", "10.0.0.5"))
    assert e.value.code == "host_blocked"


@pytest.mark.parametrize("host", ["127.0.0.1", "10.1.2.3", "localhost", "intranet", "a@b.org", "x.org/evil", "", "mail.example.org:25"])
def test_literal_ips_single_labels_and_junk_are_bad_settings(host):
    with pytest.raises(ConnectorError) as e:
        check_endpoint(host, 993, "imap", resolver=resolver_for("93.184.216.34"))
    assert e.value.code == "bad_settings"


@pytest.mark.parametrize("proto,port", [("imap", 25), ("imap", 22), ("smtp", 25), ("smtp", 993), ("https", 80), ("https", 6379)])
def test_ports_are_allowlisted_per_protocol(proto, port):
    with pytest.raises(ConnectorError) as e:
        check_endpoint("mail.example.org", port, proto, resolver=resolver_for("93.184.216.34"))
    assert e.value.code == "port_not_allowed"


def test_open_pinned_connects_to_the_verified_ip_not_the_name():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    got = []
    t = threading.Thread(target=lambda: got.append(srv.accept()[0]))
    t.start()
    hop = ResolvedHop(url="", scheme="https", hostname="does-not-resolve.invalid", port=srv.getsockname()[1], ip="127.0.0.1")
    s = open_pinned(hop, tls=False, timeout_s=2)
    t.join(2)
    assert got
    for sock in (s, got[0], srv):
        sock.close()


def test_open_pinned_unreachable_is_a_connector_error():
    hop = ResolvedHop(url="", scheme="https", hostname="x.example.org", port=9, ip="127.0.0.1")
    with pytest.raises(ConnectorError) as e:
        open_pinned(hop, tls=False, timeout_s=1)
    assert e.value.code == "unreachable"


def test_scrub_removes_secrets_but_not_short_noise():
    assert scrub("login failed for hunter2hunter2 x", "hunter2hunter2") == "login failed for *** x"
    assert scrub("abc", "a") == "abc"


def test_secretbox_roundtrip_and_wrong_key():
    a, b = SecretBox(Fernet.generate_key()), SecretBox(Fernet.generate_key())
    sealed = a.seal("app-password")
    assert b"app-password" not in sealed and a.open(sealed) == "app-password"
    with pytest.raises(ConnectorError) as e:
        b.open(sealed)
    assert e.value.code == "key_missing"


def test_secretbox_from_env_requires_a_valid_key():
    with pytest.raises(ConnectorError):
        SecretBox.from_env({})
    with pytest.raises(ConnectorError):
        SecretBox.from_env({"NOKKER_CONNECTOR_KEY": "pas-une-cle"})
    assert SecretBox.from_env({"NOKKER_CONNECTOR_KEY": SecretBox.generate_key()})


def test_store_roundtrip_never_leaks_secret_in_status_or_repr():
    st = MemoryConnectorStore(SecretBox(Fernet.generate_key()))
    st.put("us_1", "mail", {"address": "a@b.org"}, "s3cret-pass")
    assert st.get("us_1", "mail").secret == "s3cret-pass"
    assert "s3cret-pass" not in repr(st.get("us_1", "mail"))
    assert [s.kind for s in st.statuses("us_1")] == ["mail"] and not hasattr(st.statuses("us_1")[0], "secret")
    assert st.statuses("us_2") == [] and st.get("us_2", "mail") is None
    assert st._rows[("us_1", "mail")]["sealed"] != b"s3cret-pass"
    st.mark_ok("us_1", "mail")
    assert st.get("us_1", "mail").last_ok_at is not None
    st.put("us_1", "mail", {"address": "new@b.org"}, "other-pass")  # remplacement : last_ok_at repart à zéro
    assert st.get("us_1", "mail").last_ok_at is None
    assert st.delete("us_1", "mail") is True and st.delete("us_1", "mail") is False


@pytest.mark.parametrize("kind,settings,secret", [
    ("sms", {}, "x"), ("mail", {"password": "x"}, "x"), ("mail", {"address": "a@b.org"}, ""), ("mail", "texte", "x")])
def test_store_rejects_bad_input(kind, settings, secret):
    with pytest.raises(ConnectorError):
        MemoryConnectorStore(SecretBox(Fernet.generate_key())).put("us_1", kind, settings, secret)


def test_httpx_request_logs_cannot_leak_ical_urls():
    import logging

    from nokker.connectors import net  # noqa: F401 — l'import pose les niveaux

    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING
