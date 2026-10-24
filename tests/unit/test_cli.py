"""`nokker/cli.py` — parsing + `chat`/`costs` branchés sur le noyau, aucun réseau."""
from __future__ import annotations

from nokker.cli import build_parser, main
from nokker.config import NokkerConfig


def test_parser_requires_a_command():
    import pytest

    p = build_parser()
    with pytest.raises(SystemExit):
        p.parse_args([])


def test_chat_subcommand_defaults():
    p = build_parser()
    args = p.parse_args(["chat", "bonjour"])
    assert args.message == "bonjour" and args.thread is None and args.sender is None


def test_chat_subcommand_explicit_sender():
    p = build_parser()
    args = p.parse_args(["chat", "bonjour", "--sender", "alice", "--thread", "t1"])
    assert args.sender == "alice" and args.thread == "t1"


def test_costs_subcommand_wired_to_ledger_cli():
    p = build_parser()
    args = p.parse_args(["costs", "--tier", "deep"])
    assert args.tier == "deep"
    assert callable(args._nokker_ledger_cmd)
    assert callable(args.func)


def test_chat_prints_reply_from_kernel_without_any_network(monkeypatch, capsys):
    # `build_kernel` est monkeypatché : AUCUN réseau (le vrai appelle le Router/CortHeXis par défaut).
    import nokker.cli as cli_mod
    from nokker.core.turn import InputCheck, Turn

    class FakeKernel:
        def run_turn(self, inp):
            assert inp.sender == "alice"
            return Turn(thread_id=inp.thread_id, index=0, input=InputCheck(text=inp.user_text),
                       reply="2 rendez-vous aujourd'hui.")

    monkeypatch.setattr(cli_mod, "build_kernel", lambda config: FakeKernel())
    rc = main(["chat", "mon agenda ?", "--sender", "alice"])
    out = capsys.readouterr()
    assert rc == 0
    assert out.out.strip() == "2 rendez-vous aujourd'hui."


def test_chat_reports_turn_error_on_stderr(monkeypatch, capsys):
    import nokker.cli as cli_mod
    from nokker.core.turn import InputCheck, Turn

    class FakeKernel:
        def run_turn(self, inp):
            return Turn(thread_id=inp.thread_id, index=0, input=InputCheck(text=inp.user_text),
                       reply="", error="RuntimeError: boum")

    monkeypatch.setattr(cli_mod, "build_kernel", lambda config: FakeKernel())
    rc = main(["chat", "x"])
    out = capsys.readouterr()
    assert rc == 1
    assert "boum" in out.err


def test_build_parser_config_flag_passes_through():
    p = build_parser()
    args = p.parse_args(["--config", "/tmp/x.yaml", "chat", "m"])
    assert args.config == "/tmp/x.yaml"


def test_nokker_config_default_tiers_point_to_router():
    cfg = NokkerConfig()
    assert cfg.tiers["ship"].base_url == "https://router.sokkan.ch/v1"
    assert cfg.tiers["deep"].base_url == "https://router.sokkan.ch/v1"


# ───────────────────────── devices approve (P1, docs/APPROVALS.md §3.1) ─────────────────────────


def test_devices_approve_subcommand_requires_a_code():
    p = build_parser()
    args = p.parse_args(["devices", "approve", "a1b2c3"])
    assert args.code == "a1b2c3"
    assert callable(args.func)


def test_devices_approve_confirms_a_pending_device_via_the_real_backend(monkeypatch, capsys):
    import nokker.cli as cli_mod
    from nokker.approvals.memory import InMemoryApprovals

    backend = InMemoryApprovals()
    _, spki = _gen_test_keypair()
    device = backend.enroll_device(user="alice", name="laptop de test", public_key_spki=spki)

    monkeypatch.setattr(cli_mod, "build_approvals", lambda config: backend)
    rc = main(["devices", "approve", device.code])
    out = capsys.readouterr()
    assert rc == 0
    assert device.id in out.out and "approved" in out.out
    assert backend.get_device(device.id).status == "approved"


def test_devices_approve_unknown_code_reports_error_on_stderr(monkeypatch, capsys):
    import nokker.cli as cli_mod
    from nokker.approvals.memory import InMemoryApprovals

    monkeypatch.setattr(cli_mod, "build_approvals", lambda config: InMemoryApprovals())
    rc = main(["devices", "approve", "ffffff"])
    out = capsys.readouterr()
    assert rc == 1
    assert "erreur" in out.err


def _gen_test_keypair():
    import base64

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    priv = ec.generate_private_key(ec.SECP256R1())
    spki_der = priv.public_key().public_bytes(serialization.Encoding.DER,
                                              serialization.PublicFormat.SubjectPublicKeyInfo)
    return priv, base64.urlsafe_b64encode(spki_der).decode("ascii").rstrip("=")
