import pytest

from nokker.billing.stripe import StripeClient, StripeError, encode, sign, verify_signature

SECRET = "whsec_test"
BODY = b'{"id":"evt_1"}'


def test_signature_round_trip_and_tampering():
    verify_signature(BODY, sign(BODY, SECRET, 1000), SECRET, now=1100)
    for header, body in [(sign(BODY, "autre", 1000), BODY), (sign(BODY, SECRET, 1000), BODY + b" "), ("", BODY),
                         ("t=abc,v1=00", BODY), ("v1=00", BODY)]:
        with pytest.raises(StripeError) as e:
            verify_signature(body, header, SECRET, now=1100)
        assert e.value.code == "signature"


def test_signature_tolerance_is_five_minutes_both_ways():
    verify_signature(BODY, sign(BODY, SECRET, 1000), SECRET, now=1300)
    for now in (1301, 699):
        with pytest.raises(StripeError):
            verify_signature(BODY, sign(BODY, SECRET, 1000), SECRET, now=now)


def test_signature_accepts_any_of_several_v1_and_refuses_empty_secret():
    good = sign(BODY, SECRET, 1000)
    verify_signature(BODY, good + ",v1=" + "0" * 64, SECRET, now=1000)
    with pytest.raises(StripeError):
        verify_signature(BODY, good, "", now=1000)


def test_form_encoding_of_nested_fields():
    assert encode({"mode": "subscription", "line_items": [{"price": "p", "quantity": 1}],
                   "metadata": {"a": "b"}, "flag": True, "none": None}) == [
        ("mode", "subscription"), ("line_items[0][price]", "p"), ("line_items[0][quantity]", "1"),
        ("metadata[a]", "b"), ("flag", "true")]


def test_client_sends_bearer_user_agent_and_idempotency_key():
    seen = []

    def transport(method, url, body, headers, timeout):
        seen.append((method, url, body.decode(), headers))
        return 200, {"id": "cus_1"}

    assert StripeClient("sk_test_x", transport=transport).create_customer("a@b.ch", "tn_a", "us_a") == "cus_1"
    method, url, body, headers = seen[0]
    assert (method, url) == ("POST", "https://api.stripe.com/v1/customers")
    assert headers["Authorization"] == "Bearer sk_test_x" and headers["User-Agent"].startswith("nokker-core")
    assert headers["Idempotency-Key"] == "nokker-customer-tn_a" and "metadata%5Btenant_id%5D=tn_a" in body


def test_client_maps_errors():
    with pytest.raises(StripeError) as e:
        StripeClient("sk", transport=lambda *a: (402, {"error": {"code": "card_declined"}})).portal_session("c", "u")
    assert (e.value.code, e.value.status) == ("card_declined", 402)

    def boom(*a):
        raise OSError

    with pytest.raises(StripeError) as e:
        StripeClient("sk", transport=boom).portal_session("c", "u")
    assert e.value.code == "unavailable"
    with pytest.raises(ValueError):
        StripeClient("")


def test_cancel_subscription_is_idempotent_and_list_is_a_get():
    seen = []

    def transport(method, url, body, headers, timeout):
        seen.append((method, url, body, headers.get("Idempotency-Key")))
        return 200, {"data": [{"id": "sub_1", "status": "active"}]} if method == "GET" else {"id": "sub_1", "status": "canceled"}

    c = StripeClient("sk_test_x", transport=transport)
    assert c.list_subscriptions("cus 1")[0]["id"] == "sub_1"
    assert c.cancel_subscription("sub_1")["status"] == "canceled"
    assert seen[0][:3] == ("GET", "https://api.stripe.com/v1/subscriptions?customer=cus%201&status=all&limit=100", b"")
    assert seen[1] == ("DELETE", "https://api.stripe.com/v1/subscriptions/sub_1", b"", "nokker-cancel-sub_1")
