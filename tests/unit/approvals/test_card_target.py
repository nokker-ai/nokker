"""Champ `target` des cartes d'approbation : cible lisible (commande, chemin, URL, destinataire et sujet, entité et service),
caviardée comme l'historique, 200 caractères au plus, NFC, sans contrôle, absente sans cible, figée à la création."""
from __future__ import annotations

import re
import unicodedata

from nokker.api.server import approval_to_json
from nokker.approvals.memory import InMemoryApprovals
from nokker.approvals.target import MAX_LEN, card_target
from nokker.core.turn import Decision, Gate, InputCheck, ToolCall, Turn, Verdict
from tests.unit.test_thread_serialization import _req, _server

FAKE_KEY = "sk-" + "proj-Zq8Lm3Xv9Tn2Bw7Kc4Rd6Yh1Fp5Ja0Ug"  # faux
HANDLE = re.compile(r"\[secret:[^\]]+\]")


def _card(backend, name, **args):
    turn = Turn(thread_id="fil", index=0, input=InputCheck(text="x"))
    return backend.request(turn, ToolCall(id="c1", name=name, arguments=args),
                           Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule="effect:write",
                                   strength="standard"), None, notify=False)


def test_terminal_secret_becomes_a_handle_never_the_value():
    t = card_target("terminal", {"command": f"curl -H 'Authorization: Bearer {FAKE_KEY}' https://x.example/api"})
    assert FAKE_KEY not in t and "Zq8Lm3Xv9Tn2" not in t and HANDLE.search(t) and t.startswith("curl")


def test_card_number_and_avs_are_masked():
    t = card_target("terminal", {"command": "echo 4111 1111 1111 1111 et 756.1234.5678.97"})
    assert "4111 1111 1111 1111" not in t and "4111111111111111" not in t
    assert "756.1234.5678.97" not in t and "[secret:avs-" in t


def test_cap_nfc_and_no_control_characters():
    decomposed = "café " * 80
    t = card_target("terminal", {"command": decomposed})
    assert len(t) <= MAX_LEN and unicodedata.is_normalized("NFC", t) and "café" in t
    t2 = card_target("terminal", {"command": "ls\n\tcd /tmp\r\nrm\x00 -rf​ x\x1b[0m"})
    assert not any(unicodedata.category(c) in ("Cc", "Cf") for c in t2) and t2.startswith("ls cd /tmp rm")


def test_truncation_never_cuts_a_handle_in_two():
    for pad in range(150, 200):
        t = card_target("terminal", {"command": "a" * pad + f" {FAKE_KEY} fin"})
        assert len(t) <= MAX_LEN
        assert "[secret:" not in t or HANDLE.search(t), (pad, t)
        assert t.count("[") == t.count("]")


def test_per_tool_targets():
    assert card_target("write_file", {"path": "src/a.py", "content": "secret body"}) == "src/a.py"
    assert card_target("read_file", {"path": "notes/b.txt"}) == "notes/b.txt"
    assert card_target("patch", {"mode": "replace", "path": "c.py", "old_string": "x", "new_string": "y"}) == "c.py"
    assert card_target("patch", {"mode": "patch", "patch": "*** Begin Patch"}) is None
    assert card_target("web_fetch", {"urls": ["https://example.org/p"]}) == "https://example.org/p"
    assert card_target("web_fetch", {"urls": ["https://example.org/p", "https://b.example"]}) == "https://example.org/p (+1)"
    assert card_target("browser_navigate", {"url": "https://example.org/q"}) == "https://example.org/q"
    assert card_target("mail_send", {"to": "bob@example.org", "subject": "Devis", "body": "corps"}) == "bob@example.org — Devis"
    assert card_target("send_message", {"target": "telegram:123", "message": "Salut"}) == "telegram:123"
    assert card_target("maison_action_forte", {"domain": "lock", "service": "unlock",
                                               "entity_id": "lock.entree"}) == "lock.unlock — lock.entree"


def test_unknown_tool_or_no_value_has_no_target():
    assert card_target("infra_lvextend", {"path": "/srv", "size": "5G"}) is None
    assert card_target("terminal", {}) is None and card_target("terminal", {"command": "  \n"}) is None
    assert card_target("terminal", {"command": 5}) is None and card_target("terminal", None) is None


def test_target_is_frozen_at_creation_and_stored():
    backend = InMemoryApprovals()
    card = _card(backend, "terminal", command="make test")
    assert card.target == "make test"
    card.arguments["command"] = "rm -rf /"
    backend._store_approval(card)
    assert backend.get(card.id).target == "make test"
    assert approval_to_json(backend.get(card.id))["target"] == "make test"


def test_json_without_target_is_unchanged():
    backend = InMemoryApprovals()
    card = _card(backend, "infra_lvextend", size="5G")
    assert card.target is None and "target" not in approval_to_json(card)


def test_requested_event_carries_target():
    backend = InMemoryApprovals()
    events: list = []
    backend.on_event = lambda k, d: events.append((k, d))
    _card(backend, "terminal", command="git status")
    [data] = [d for k, d in events if k == "approval.requested"]
    assert approval_to_json(data["approval"])["target"] == "git status"


def test_http_routes_expose_target(tmp_path):
    httpd, port, api, kernel, approvals, tools, model, events = _server(tmp_path)
    try:
        c1 = _card(approvals, "terminal", command=f"deploy --key {FAKE_KEY}")
        c2 = _card(approvals, "infra_lvextend", size="5G")
        for path in (f"/v1/approvals/{c1.id}", f"/v1/local/approvals/{c1.id}"):
            status, body = _req(port, path, token=api.token)
            if status != 404:  # la route locale n'existe peut-être pas en lecture unitaire
                assert status == 200 and body["target"].startswith("deploy --key [secret:") and FAKE_KEY not in body["target"], path
        status, body = _req(port, f"/v1/approvals/{c1.id}", token=api.token)
        assert status == 200 and "target" in body
        status, body = _req(port, f"/v1/approvals/{c2.id}", token=api.token)
        assert status == 200 and "target" not in body
        status, lst = _req(port, "/v1/local/approvals", token=api.token)
        assert status == 200
        got = {x["id"]: x for x in lst["approvals"]}
        assert got[c1.id]["target"].startswith("deploy --key [secret:") and FAKE_KEY not in got[c1.id]["target"] and "target" not in got[c2.id]
    finally:
        httpd.shutdown()


# --- arguments réels : schémas chargés depuis la source de l'outil, pas recopiés -------------------------------------

def _real_specs(*names):
    from nokker.connectors.mail import _SPECS

    specs = {sp.name: sp for sp in _SPECS}
    return [specs[n] for n in names]


def _valid_args(schema, **fixed):
    """Arguments valides construits depuis le schéma : chaque `required` rempli (valeur de `fixed` sinon texte)."""
    return {k: fixed.get(k, f"v_{k}") for k in schema["required"]}


def _keys_read(name, args):
    """Clés réellement lues par l'extracteur de `name`."""
    from nokker.approvals.target import EXTRACTORS

    seen = set()

    class Spy(dict):
        def get(self, key, default=None):
            seen.add(key)
            return super().get(key, default)

    EXTRACTORS[name](Spy(args))
    return seen


def _mail_target_reads_real_properties(name):
    (spec,) = _real_specs(name)
    props = spec.parameters["properties"]
    assert {"to", "subject"} <= set(props) and {"to", "subject"} <= set(spec.parameters["required"])
    # (a) tout argument lu par l'extracteur existe dans le schéma
    assert _keys_read(name, _valid_args(spec.parameters)) <= set(props)
    # (b) cible attendue depuis des arguments valides
    args = _valid_args(spec.parameters, to="bob@example.org", subject="Devis")
    assert card_target(name, args) == "bob@example.org — Devis"
    # (c) un secret dans destinataire ou sujet est masqué
    t = card_target(name, {**args, "subject": f"cle {FAKE_KEY}"})
    assert FAKE_KEY not in t and HANDLE.search(t)
    t = card_target(name, {**args, "to": f"{FAKE_KEY}@example.org"})
    assert FAKE_KEY not in t


def test_mail_send_target_from_real_spec():
    _mail_target_reads_real_properties("mail_send")


def test_mail_draft_target_from_real_spec():
    _mail_target_reads_real_properties("mail_draft")


def _upstream_send_message_params():
    """Paramètres de `messages_send` (serveur MCP amont) lus dans la source au tag de HERMES.lock ; None si absente."""
    import ast
    import os
    import pathlib
    import subprocess

    root = pathlib.Path(__file__).resolve().parents[3]
    tag = dict(ln.split("=", 1) for ln in (root / "HERMES.lock").read_text().splitlines()
               if "=" in ln and not ln.startswith("#"))["tag"]
    repo = os.environ.get("HERMES_SRC", "/root/hermes-nokker")
    try:
        src = subprocess.run(["git", "-C", repo, "show", f"{tag}:mcp_serve.py"], capture_output=True, text=True,
                             check=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.FunctionDef) and n.name == "messages_send":
            return [a.arg for a in n.args.args if a.arg != "self"]
    return None


def test_send_message_target_reads_real_upstream_argument():
    import pytest

    params = _upstream_send_message_params()
    if params is None:
        pytest.skip("source de l'amont absente (HERMES_SRC / tag de HERMES.lock)")
    assert params == ["target", "message"]
    args = dict.fromkeys(params, "x") | {"target": "telegram:123"}
    assert _keys_read("send_message", args) <= set(params)
    assert card_target("send_message", args) == "telegram:123"
    t = card_target("send_message", {**args, "target": f"telegram:{FAKE_KEY}"})
    assert FAKE_KEY not in t and HANDLE.search(t)


def test_tools_without_any_known_schema_have_no_target():
    for name in ("send_email_reply", "send_email_forward", "send_telegram_message"):
        assert card_target(name, {"to": "bob@example.org", "subject": "S", "chat_id": "1"}) is None


# ── coffre de valeurs connues : une valeur du coffre est masquée dans `target` comme dans l'historique ──
VAULT_VALUE = "calme-plat-Orchidee-7"  # sans motif reconnaissable (faux)


def _vault():
    from nokker.qa.secrets import KnownSecrets

    ks = KnownSecrets()
    ks.add("NOKKER_SECRET_TEST", VAULT_VALUE)
    return ks


def test_vault_value_is_masked_in_target_when_known_is_given():
    args = {"command": f"deploy --token {VAULT_VALUE} --now"}
    assert VAULT_VALUE in card_target("terminal", args)  # sans coffre : aucun motif, rien à masquer (point de départ)
    masked = card_target("terminal", args, _vault())
    assert VAULT_VALUE not in masked and "Orchidee" not in masked and masked.startswith("deploy")


def test_base_approvals_masks_vault_values_in_the_frozen_target():
    backend = InMemoryApprovals(known=_vault())
    card = _card(backend, "terminal", command=f"echo {VAULT_VALUE}")
    assert VAULT_VALUE not in card.target and VAULT_VALUE not in approval_to_json(card)["target"]
    assert backend.get(card.id).target == card.target  # figé


def test_build_approvals_wires_the_vault(monkeypatch):
    from nokker.api.server import build_approvals
    from nokker.config import NokkerConfig

    monkeypatch.setenv("NOKKER_SECRET_TEST", VAULT_VALUE)
    cfg = NokkerConfig()
    backend = build_approvals(cfg)
    assert "NOKKER_SECRET_TEST" in backend.known.names()
    assert VAULT_VALUE not in _card(backend, "terminal", command=f"echo {VAULT_VALUE}").target
