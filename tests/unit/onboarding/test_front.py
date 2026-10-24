import pytest

from nokker.onboarding.front import FrontError, FrontInvitations, MemoryFront


class Wire:
    def __init__(self, status=200, data=None, boom=None):
        self.status, self.data, self.boom, self.calls = status, data if data is not None else {"ok": True}, boom, []

    def __call__(self, url, body, headers, timeout):
        self.calls.append((url, body, headers.get("Authorization")))
        if self.boom:
            raise self.boom
        return self.status, self.data


def client(wire):
    return FrontInvitations("http://192.0.2.10:8122/", "tok", transport=wire)


def test_send_posts_with_bearer_and_returns_only_the_receipt():
    w = Wire(data={"ok": True, "invitation_id": 3, "sent_at": "a", "expires_at": "b", "lang": "fr", "extra": 1})
    r = client(w).send(12, None, "alice@example.ch")
    assert r == {"invitation_id": 3, "sent_at": "a", "expires_at": "b", "lang": "fr"}
    assert w.calls == [("http://192.0.2.10:8122/internal/invitations/send",
                        {"candidature_id": 12, "lang": None, "actor": "alice@example.ch"}, "Bearer tok")]


def test_check_valid_and_invalid():
    assert client(Wire(data={"ok": True, "valid": True, "lang": "en", "prenom": "Sam"})).check("t") == {
        "valid": True, "lang": "en", "prenom": "Sam"}
    assert client(Wire(data={"ok": True, "valid": False})).check("t") == {"valid": False}


@pytest.mark.parametrize("status,data,code", [
    (409, {"ok": False, "error": "template_not_approved"}, "template_not_approved"),
    (410, {"ok": False, "error": "invalid_token"}, "invalid_token"),
    (401, {"ok": False}, "unauthorized"),
    (500, {}, "unavailable"),
])
def test_refusals_become_front_errors(status, data, code):
    with pytest.raises(FrontError) as e:
        client(Wire(status=status, data=data)).redeem("t")
    assert e.value.code == code and e.value.status == status


def test_network_failure_is_unavailable():
    with pytest.raises(FrontError) as e:
        client(Wire(boom=TimeoutError())).release(1)
    assert e.value.code == "unavailable"


def test_config_required():
    with pytest.raises(ValueError):
        FrontInvitations("", "tok")


def test_memory_front_mirrors_the_contract():
    f = MemoryFront({1: {"email": "c@example.org", "prenom": "Camille", "lang": "fr"}})
    f.send(1, None, "n")
    tok = next(iter(f.tokens))
    assert f.check(tok)["valid"]
    r = f.redeem(tok)
    assert r["email"] == "c@example.org"
    with pytest.raises(FrontError):
        f.redeem(tok)
    f.release(r["invitation_id"])
    assert f.check(tok)["valid"]
    f.send(1, None, "n")
    assert not f.check(tok)["valid"]
    with pytest.raises(FrontError):
        f.send(9, None, "n")


def test_extra_headers_travel_but_never_override_authorization():
    seen = {}

    def wire(url, body, headers, timeout):
        seen.update(headers)
        return 200, {"ok": True, "invitations": []}

    FrontInvitations("https://intake.nokker.ai", "tok", transport=wire,
                     extra_headers={"CF-Access-Client-Id": "id", "Authorization": "evil"}).invitations(1)
    assert seen["CF-Access-Client-Id"] == "id" and seen["Authorization"] == "Bearer tok"


def test_explicit_user_agent_for_cloudflare():
    """Cloudflare (intake.nokker.ai) refuse le User-Agent de urllib : error code 1010 → front_unavailable."""
    seen = {}

    def wire(url, body, headers, timeout):
        seen.update(headers)
        return 200, {"ok": True, "valid": False}

    FrontInvitations("https://intake.nokker.ai", "tok", transport=wire).check("t")
    assert seen["User-Agent"].startswith("nokker-core/")
