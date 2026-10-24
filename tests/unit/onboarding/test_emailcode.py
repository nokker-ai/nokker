"""Connexion par code e-mail (nokker/onboarding/emailcode.py) — scope §16 : réponse identique compte connu ou non,
10 minutes, usage unique, 5 essais puis code brûlé, limites, appareil de simple connexion."""
from datetime import UTC, datetime, timedelta

import pytest

from nokker.approvals.memory import InMemoryApprovals
from nokker.onboarding import emailcode as ec
from nokker.onboarding.emailcode import CodeError, EmailCodes, MemoryCodes
from nokker.onboarding.front import MemoryFront
from nokker.tenancy.store import MemoryTenancy
from tests.unit.approvals.conftest import gen_keypair


class Clock:
    def __init__(self):
        self.t = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)

    def __call__(self):
        return self.t


@pytest.fixture
def rig():
    tenancy, front, clock = MemoryTenancy(), MemoryFront(), Clock()
    acc = tenancy.create_account("camille@example.org")
    codes = EmailCodes(front=front, tenancy=tenancy, approvals=InMemoryApprovals(), store=MemoryCodes(), now=clock,
                       dispatch=lambda fn: fn())
    codes.approvals.user_tenant = lambda u: acc.tenant_id if u == acc.account_id else None
    return codes, front, clock, acc


def spki():
    return gen_keypair()[1]


def test_start_answers_the_same_whether_the_account_exists_or_not(rig):
    codes, front, _, _ = rig
    assert codes.start("Camille@Example.org ", "fr", "1.1.1.1") == codes.start("inconnu@example.org", "fr", "1.1.1.2")
    assert [(e, len(c), lang) for e, c, lang in front.codes] == [("camille@example.org", 6, "fr")]


def test_verify_creates_a_login_only_device_once(rig):
    codes, front, _, acc = rig
    codes.start("camille@example.org", "en", "ip")
    code = front.codes[-1][1]
    out = codes.verify("camille@example.org", code, "portable", spki(), "ip")
    assert out["user"] == acc.account_id
    dev = codes.approvals.get_device(out["device_id"])
    assert dev.status == "approved" and dev.approved_via == "email-code"
    assert codes.tenancy.account_for_identity("device", dev.id).account_id == acc.account_id
    with pytest.raises(CodeError, match="invalid_code"):  # usage unique
        codes.verify("camille@example.org", code, "autre", spki(), "ip")


def test_every_failure_is_the_same_invalid_code(rig):
    codes, front, clock, _ = rig
    codes.start("camille@example.org", "fr", "ip")
    code = front.codes[-1][1]
    wrong = "000000" if code != "000000" else "111111"
    for email, c in (("inconnu@example.org", code), ("camille@example.org", wrong)):
        with pytest.raises(CodeError) as e:
            codes.verify(email, c, "x", spki(), "ip")
        assert (e.value.code, e.value.status) == ("invalid_code", 401)
    clock.t += ec.TTL + timedelta(seconds=1)
    with pytest.raises(CodeError, match="invalid_code"):  # expiré, même avec le bon code
        codes.verify("camille@example.org", code, "x", spki(), "ip")


def test_five_wrong_attempts_burn_the_code(rig):
    codes, front, _, _ = rig
    codes.start("camille@example.org", "fr", "ip")
    code = front.codes[-1][1]
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(ec.MAX_ATTEMPTS):
        with pytest.raises(CodeError):
            codes.verify("camille@example.org", wrong, "x", spki(), "ip")
    with pytest.raises(CodeError, match="invalid_code"):  # le bon code ne sert plus
        codes.verify("camille@example.org", code, "x", spki(), "ip")


def test_a_new_code_replaces_the_previous_one(rig):
    codes, front, _, _ = rig
    codes.start("camille@example.org", "fr", "ip")
    first = front.codes[-1][1]
    codes.start("camille@example.org", "fr", "ip")
    second = front.codes[-1][1]
    if first != second:
        with pytest.raises(CodeError):
            codes.verify("camille@example.org", first, "x", spki(), "ip")
    assert codes.verify("camille@example.org", second, "x", spki(), "ip")["device_id"]


def test_bad_key_is_refused_before_spending_an_attempt(rig):
    codes, front, _, _ = rig
    codes.start("camille@example.org", "fr", "ip")
    with pytest.raises(CodeError, match="invalid_key"):
        codes.verify("camille@example.org", front.codes[-1][1], "x", "pas-une-cle", "ip")
    assert codes.verify("camille@example.org", front.codes[-1][1], "x", spki(), "ip")


def test_rate_limits_are_identical_for_known_and_unknown_addresses(rig):
    codes, _, _, _ = rig
    for email in ("camille@example.org", "inconnu@example.org"):
        for i in range(ec.START_PER_EMAIL):
            codes.start(email, "fr", f"ip-{email}-{i}")
        with pytest.raises(CodeError) as e:
            codes.start(email, "fr", "ip-autre")
        assert (e.value.code, e.value.status) == ("rate", 429)


def test_suspended_account_gets_no_code(rig):
    codes, front, _, acc = rig
    import dataclasses

    suspended = dataclasses.replace(acc, status="suspended")
    codes.tenancy.account_by_email = lambda email: suspended
    assert codes.start("camille@example.org", "fr", "ip") == {"sent": True}
    assert front.codes == []


def test_a_failed_mail_never_leaks_to_the_caller(rig):
    codes, front, _, _ = rig
    front.fail_send = True
    assert codes.start("camille@example.org", "fr", "ip") == {"sent": True}
