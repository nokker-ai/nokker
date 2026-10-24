"""`InMemoryApprovals` (backend `Approvals` de test) — cycle de vie complet, docs/APPROVALS.md §2-§4.
Toute la logique de preuve testée ici vit dans `base.BaseApprovals` : ces tests couvrent donc aussi
`PgApprovals` par construction (même code), cf. `tests/unit/approvals/test_pg_approvals.py` (marqué
`integration`, un Postgres jetable) pour le filet de sécurité « le SQL marche vraiment »."""
from __future__ import annotations

import json

import pytest

from nokker.approvals.errors import ApprovalExpired, ApprovalNotFound, ApprovalNotPending, ProofInvalid
from nokker.approvals.memory import InMemoryApprovals
from nokker.core.ports import Proof
from nokker.core.turn import Decision, Gate, ToolCall, Turn, Verdict
from tests.unit.approvals.conftest import gen_keypair, sign_p1363, telegram_init_data

ALICE = "alice"
MALLORY = "mallory@media-exemple.com"
TELEGRAM_BOT_TOKEN = "123456:FAKE-BOT-TOKEN"
TELEGRAM_USER_ID = 1000000001


def make_turn(sender=ALICE, channel="telegram") -> Turn:
    from nokker.core.turn import InputCheck

    return Turn(thread_id="t1", index=0, input=InputCheck(text="vas-y"), channel=channel, sender=sender)


def make_call(**args) -> ToolCall:
    return ToolCall(id="c1", name="infra_lvextend", arguments=args or {"path": "/srv/share", "size": "50G"})


def make_verdict(strength="strong") -> Verdict:
    return Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule="effect:destructive",
                  reason="sans GO", strength=strength)


def enroll_approved_device(backend: InMemoryApprovals, user=ALICE, telegram_user_id=None):
    priv, spki = gen_keypair()
    device = backend.enroll_device(user=user, name="iPhone de Alice", public_key_spki=spki,
                                   telegram_user_id=telegram_user_id)
    confirmed = backend.confirm_device_cli(device.code)
    assert confirmed.status == "approved"
    return priv, confirmed


def enroll_approved_telegram_device(backend: InMemoryApprovals, user=ALICE):
    """Appareil enrôlé ET lié à un `telegram_user_id` — seule combinaison acceptée au niveau `strong`
    depuis que `_verify_proof` exige la preuve « signature+telegram » (Mini App) à ce niveau."""
    return enroll_approved_device(backend, user=user, telegram_user_id=str(TELEGRAM_USER_ID))


def sign_decision(priv, challenge_text: str, decision: str) -> str:
    return sign_p1363(priv, f"{challenge_text}.{decision}".encode("utf-8"))


# ═══════════════════════════ chemin heureux ═══════════════════════════


def test_happy_path_request_challenge_sign_execute():
    """Niveau `strong` : clé d'appareil + preuve Telegram (Mini App) — une signature logicielle seule
    ne suffit plus (cf `test_strong_action_rejects_software_key_signature_alone` plus bas)."""
    backend = InMemoryApprovals(telegram_bot_token=TELEGRAM_BOT_TOKEN)
    priv, device = enroll_approved_telegram_device(backend)
    turn, call, verdict = make_turn(), make_call(), make_verdict("strong")

    approval = backend.request(turn, call, verdict, advice="sauvegarde récente, retour arrière possible")
    assert approval.status == "pending"
    assert approval.strength == "strong"
    assert approval in backend.pending()

    ch = backend.challenge(approval.id)
    sig = sign_decision(priv, ch.canonical(), "approve")
    init_data = telegram_init_data(TELEGRAM_BOT_TOKEN, TELEGRAM_USER_ID)
    decided = backend.decide(approval.id, "approve",
                             Proof(challenge=ch.canonical(), signature=sig, device_id=device.id,
                                   telegram_init_data=init_data))
    assert decided.status == "approved"
    assert decided.decided_by == ALICE
    assert decided.decided_via == "signature+telegram"
    assert backend.get(approval.id).status == "approved"
    assert approval not in backend.pending()


def test_happy_path_reject():
    backend = InMemoryApprovals(telegram_bot_token=TELEGRAM_BOT_TOKEN)
    priv, device = enroll_approved_telegram_device(backend)
    approval = backend.request(make_turn(), make_call(), make_verdict("strong"), None)
    ch = backend.challenge(approval.id)
    sig = sign_decision(priv, ch.canonical(), "reject")
    init_data = telegram_init_data(TELEGRAM_BOT_TOKEN, TELEGRAM_USER_ID)
    decided = backend.decide(approval.id, "reject", Proof(challenge=ch.canonical(), signature=sig,
                                                          device_id=device.id, telegram_init_data=init_data))
    assert decided.status == "rejected"
    assert decided.decided_by == ALICE


# ═══════════════════════════ preuves refusées ═══════════════════════════


def test_valid_signature_from_wrong_user_is_rejected():
    backend = InMemoryApprovals()
    priv, device = enroll_approved_device(backend, user=MALLORY)
    approval = backend.request(make_turn(), make_call(), make_verdict("strong"), None)
    ch = backend.challenge(approval.id)
    sig = sign_decision(priv, ch.canonical(), "approve")
    with pytest.raises(ProofInvalid):
        backend.decide(approval.id, "approve", Proof(challenge=ch.canonical(), signature=sig, device_id=device.id))
    assert backend.get(approval.id).status == "pending"


def test_replayed_nonce_is_rejected_on_second_decide():
    """Rejeu de nonce — indépendant du niveau de preuve ; `standard` suffit (plus simple, pas de
    Telegram à poser) pour une clé logicielle seule."""
    backend = InMemoryApprovals()
    priv, device = enroll_approved_device(backend)
    approval = backend.request(make_turn(), make_call(), make_verdict("standard"), None)
    ch = backend.challenge(approval.id)
    sig = sign_decision(priv, ch.canonical(), "approve")
    proof = Proof(challenge=ch.canonical(), signature=sig, device_id=device.id)
    backend.decide(approval.id, "approve", proof)  # 1ère décision : consomme le nonce

    approval2 = backend.request(make_turn(), make_call(), make_verdict("standard"), None)
    # On REJOUE le nonce DÉJÀ CONSOMMÉ de `ch` sur une AUTRE approbation (même utilisateur, même
    # appareil, défi forgé avec ce nonce) : refusé (déjà utilisé, et/ou lié à une autre approbation).
    forged = json.loads(ch.canonical())
    forged["approval_id"] = approval2.id
    forged_text = json.dumps(forged, sort_keys=True, separators=(",", ":"))
    sig2 = sign_decision(priv, forged_text, "approve")
    with pytest.raises(ProofInvalid):
        backend.decide(approval2.id, "approve", Proof(challenge=forged_text, signature=sig2, device_id=device.id))


def test_args_modified_after_request_is_rejected():
    """Les arguments de l'`Approval` stockée changent ENTRE la demande et la décision (simulé ici en
    mutant directement l'objet en mémoire — en prod, `arguments` ne devrait jamais changer après
    `request()` ; c'est précisément ce que ce test prouve que `decide()` détecte)."""
    backend = InMemoryApprovals()
    priv, device = enroll_approved_device(backend)
    approval = backend.request(make_turn(), make_call(size="50G"), make_verdict("strong"), None)
    ch = backend.challenge(approval.id)
    sig = sign_decision(priv, ch.canonical(), "approve")

    tampered = backend._load_approval(approval.id)
    tampered.arguments["size"] = "500G"
    backend._store_approval(tampered)

    with pytest.raises(ProofInvalid):
        backend.decide(approval.id, "approve", Proof(challenge=ch.canonical(), signature=sig, device_id=device.id))


def test_expired_challenge_is_rejected():
    clock = {"t": 1_700_000_000.0}
    backend = InMemoryApprovals(clock=lambda: clock["t"])
    priv, device = enroll_approved_device(backend)
    approval = backend.request(make_turn(), make_call(), make_verdict("strong"), None)
    ch = backend.challenge(approval.id)
    sig = sign_decision(priv, ch.canonical(), "approve")
    clock["t"] += 121  # TTL défi = 120 s (docs/APPROVALS.md §3)
    with pytest.raises(ProofInvalid):
        backend.decide(approval.id, "approve", Proof(challenge=ch.canonical(), signature=sig, device_id=device.id))


def test_expired_approval_is_rejected_even_with_valid_proof():
    clock = {"t": 1_700_000_000.0}
    backend = InMemoryApprovals(clock=lambda: clock["t"])
    priv, device = enroll_approved_device(backend)
    approval = backend.request(make_turn(), make_call(), make_verdict("strong"), None)
    clock["t"] += 25 * 3600  # TTL par défaut 24h
    with pytest.raises(ApprovalExpired):
        backend.challenge(approval.id)
    assert backend.get(approval.id).status == "expired"


def test_totp_on_strong_action_is_rejected():
    from nokker.approvals.crypto import new_totp_secret, totp_code

    backend = InMemoryApprovals()
    secret = new_totp_secret()
    backend.enroll_totp_secret(ALICE, secret)
    approval = backend.request(make_turn(), make_call(), make_verdict("strong"), None)
    ch = backend.challenge(approval.id)
    code = totp_code(secret)
    with pytest.raises(ProofInvalid):
        backend.decide(approval.id, "approve", Proof(challenge=ch.canonical(), totp=code, totp_user=ALICE))


def test_totp_on_standard_action_is_accepted():
    from nokker.approvals.crypto import new_totp_secret, totp_code

    backend = InMemoryApprovals()
    secret = new_totp_secret()
    backend.enroll_totp_secret(ALICE, secret)
    approval = backend.request(make_turn(), make_call(), make_verdict("standard"), None)
    ch = backend.challenge(approval.id)
    code = totp_code(secret)
    decided = backend.decide(approval.id, "approve", Proof(challenge=ch.canonical(), totp=code, totp_user=ALICE))
    assert decided.status == "approved" and decided.decided_via == "totp"


def test_totp_code_is_not_reusable():
    from nokker.approvals.crypto import new_totp_secret, totp_code

    backend = InMemoryApprovals()
    secret = new_totp_secret()
    backend.enroll_totp_secret(ALICE, secret)
    code = totp_code(secret)

    a1 = backend.request(make_turn(), make_call(), make_verdict("standard"), None)
    ch1 = backend.challenge(a1.id)
    backend.decide(a1.id, "approve", Proof(challenge=ch1.canonical(), totp=code, totp_user=ALICE))

    a2 = backend.request(make_turn(), make_call(), make_verdict("standard"), None)
    ch2 = backend.challenge(a2.id)
    with pytest.raises(ProofInvalid):
        backend.decide(a2.id, "approve", Proof(challenge=ch2.canonical(), totp=code, totp_user=ALICE))


def test_totp_from_unauthorized_user_is_rejected():
    from nokker.approvals.crypto import new_totp_secret, totp_code

    backend = InMemoryApprovals()
    secret = new_totp_secret()
    backend.enroll_totp_secret(MALLORY, secret)
    approval = backend.request(make_turn(), make_call(), make_verdict("standard"), None)
    ch = backend.challenge(approval.id)
    code = totp_code(secret)
    with pytest.raises(ProofInvalid):
        backend.decide(approval.id, "approve", Proof(challenge=ch.canonical(), totp=code, totp_user=MALLORY))


def test_no_proof_at_all_is_rejected():
    backend = InMemoryApprovals()
    approval = backend.request(make_turn(), make_call(), make_verdict("standard"), None)
    ch = backend.challenge(approval.id)
    with pytest.raises(ProofInvalid):
        backend.decide(approval.id, "approve", Proof(challenge=ch.canonical()))


def test_pending_device_cannot_sign_a_decision():
    backend = InMemoryApprovals()
    priv, spki = gen_keypair()
    device = backend.enroll_device(user=ALICE, name="appareil pas encore confirmé", public_key_spki=spki)
    approval = backend.request(make_turn(), make_call(), make_verdict("strong"), None)
    ch = backend.challenge(approval.id)
    sig = sign_decision(priv, ch.canonical(), "approve")
    with pytest.raises(ProofInvalid):
        backend.decide(approval.id, "approve", Proof(challenge=ch.canonical(), signature=sig, device_id=device.id))


def test_decide_on_already_decided_approval_raises():
    """Refus d'une 2e décision sur une approbation déjà tranchée — une question d'état, pas de niveau
    de preuve ; `standard` suffit ici."""
    backend = InMemoryApprovals()
    priv, device = enroll_approved_device(backend)
    approval = backend.request(make_turn(), make_call(), make_verdict("standard"), None)
    ch = backend.challenge(approval.id)
    sig = sign_decision(priv, ch.canonical(), "approve")
    backend.decide(approval.id, "approve", Proof(challenge=ch.canonical(), signature=sig, device_id=device.id))

    # Plus `pending` : même `challenge()` (étape qui précède normalement une 2e décision) doit refuser.
    with pytest.raises(ApprovalNotPending):
        backend.challenge(approval.id)


def test_unknown_approval_id_raises_not_found():
    backend = InMemoryApprovals()
    with pytest.raises(ApprovalNotFound):
        backend.challenge("inexistant")


# ═══════════════════════════ appareils + sessions ═══════════════════════════


def test_device_enrollment_starts_pending_and_cli_confirms():
    backend = InMemoryApprovals()
    _, spki = gen_keypair()
    device = backend.enroll_device(user=ALICE, name="laptop", public_key_spki=spki)
    assert device.status == "pending"
    confirmed = backend.confirm_device_cli(device.code)
    assert confirmed.status == "approved" and confirmed.approved_via == "cli"


def test_device_enrollment_confirmed_by_totp():
    from nokker.approvals.crypto import new_totp_secret, totp_code

    backend = InMemoryApprovals()
    secret = new_totp_secret()
    backend.enroll_totp_secret(ALICE, secret)
    _, spki = gen_keypair()
    device = backend.enroll_device(user=ALICE, name="tablette", public_key_spki=spki)
    confirmed = backend.confirm_device_totp(device.id, totp_code(secret), ALICE)
    assert confirmed.status == "approved" and confirmed.approved_via == "totp"


def test_device_totp_of_another_user_cannot_confirm():
    """§3.1bis : un appareil web DÉCLARE son utilisateur — seul le TOTP de cet utilisateur le confirme."""
    from nokker.approvals.crypto import new_totp_secret, totp_code

    backend = InMemoryApprovals(is_authorized=lambda u: u in {"alice", "bruno"})
    secret = new_totp_secret()
    backend.enroll_totp_secret("bruno", secret)
    _, spki = gen_keypair()
    device = backend.enroll_device(user=ALICE, name="navigateur", public_key_spki=spki)
    with pytest.raises(ProofInvalid):
        backend.confirm_device_totp(device.id, totp_code(secret), "bruno")
    assert backend.get_device(device.id).status == "pending"


def test_login_session_round_trip():
    backend = InMemoryApprovals()
    priv, device = enroll_approved_device(backend)
    ch = backend.login_challenge(device.id)
    sig = sign_p1363(priv, f"{ch.canonical()}.login".encode("utf-8"))
    session = backend.create_session(device.id, ch.canonical(), sig)
    assert session.user == ALICE
    assert backend.verify_session(session.token).user == ALICE


def test_login_session_nonce_not_reusable():
    backend = InMemoryApprovals()
    priv, device = enroll_approved_device(backend)
    ch = backend.login_challenge(device.id)
    sig = sign_p1363(priv, f"{ch.canonical()}.login".encode("utf-8"))
    backend.create_session(device.id, ch.canonical(), sig)
    with pytest.raises(ProofInvalid):
        backend.create_session(device.id, ch.canonical(), sig)


def test_device_confirmed_by_invitation_traces_it():
    backend = InMemoryApprovals()
    _, spki = gen_keypair()
    device = backend.enroll_device(user="us_1", name="portable", public_key_spki=spki)
    confirmed = backend.confirm_device_by_invitation(device.id, 7)
    assert confirmed.status == "approved" and confirmed.approved_via == "invitation:7"
    assert backend.get_device(device.id).approved_via == "invitation:7"


def test_invitation_confirms_only_a_pending_device():
    from nokker.approvals.errors import DeviceNotFound, ProofInvalid

    backend = InMemoryApprovals()
    _, spki = gen_keypair()
    device = backend.enroll_device(user="us_1", name="portable", public_key_spki=spki)
    backend.confirm_device_by_invitation(device.id, 7)
    with pytest.raises(ProofInvalid):  # une invitation = un appareil
        backend.confirm_device_by_invitation(device.id, 8)
    with pytest.raises(DeviceNotFound):
        backend.confirm_device_by_invitation("inconnu", 7)


def test_device_from_email_code_signs_in_but_never_decides():
    """Scope §16 (Alice, 21.10) : une connexion par code e-mail n'est jamais une approbation."""
    from nokker.approvals.errors import StrongDeviceRequired

    backend = InMemoryApprovals()
    priv, spki = gen_keypair()
    device = backend.enroll_device(user=ALICE, name="portable", public_key_spki=spki)
    confirmed = backend.confirm_device_by_email_code(device.id)
    assert confirmed.status == "approved" and confirmed.approved_via == "email-code"
    ch = backend.login_challenge(device.id)  # se connecter : oui
    backend.create_session(device.id, ch.canonical(), sign_p1363(priv, f"{ch.canonical()}.login".encode()))
    approval = backend.request(make_turn(), make_call(), make_verdict("standard"), None)
    ch = backend.challenge(approval.id)
    with pytest.raises(StrongDeviceRequired, match="strong_device_required"):
        backend.decide(approval.id, "approve", Proof(challenge=ch.canonical(),
                                                     signature=sign_decision(priv, ch.canonical(), "approve"),
                                                     device_id=device.id))
    assert backend.get(approval.id).status == "pending"
