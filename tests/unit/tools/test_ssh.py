"""Ops de Nokker Lokkal (lot X11, nokker/tools/ssh.py, nokker/tools/hosts.py) : inventaire, plancher par rôle,
known_hosts, clé jamais dans le contexte, drapeau `local.ops`.

Partie 1, toujours jouée : bac à sable simulé (il garde le script et l'entrée standard reçus), détection des commandes
simulée. Partie 2 (fin du fichier) : un vrai sshd jetable, avec NOKKER_TEST_SSHD (voir `test_real_sshd_*`)."""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from nokker.core.ports import ToolContext
from nokker.core.turn import (
    Decision,
    Effect,
    Gate,
    InputCheck,
    Message,
    ToolCall,
    Turn,
    TurnInput,
    Verdict,
)
from nokker.qa.permissions import LocalPermissions, Mode, PermissionGates, Rule
from nokker.tools.hosts import append_known, fingerprint, known_entries, known_lines, load_inventory
from nokker.tools.ssh import SshToolSource

TH = "th_ops"
CTX = ToolContext(thread_id=TH, turn_index=0, channel="api", sender="local-owner")
KEY = ("-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW\n"
       "QyNTUxOQAAACBfYWtlLWtleS1mb3ItdGVzdHMtb25seS0wMDAwMDAwMAAAAJAAAAAAAAAAAA\n"
       "-----END OPENSSH PRIVATE KEY-----\n")  # forme d'une clé, pas une vraie clé
JUMP_KEY = KEY.replace("b3Bl", "c3Bl")
HOST_B64 = base64.b64encode(b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + b"h" * 32).decode()
OTHER_B64 = base64.b64encode(b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + b"o" * 32).decode()

HOSTS = """\
hosts:
  - {name: web1, address: 10.0.0.5, user: deploy, key: "secret:SSH_WEB1", role: prod}
  - {name: dev1, address: dev.example.lan, port: 2222, user: dev, key: "secret:SSH_DEV1", role: dev}
  - {name: bastion, address: 192.0.2.10, user: jump, key: "secret:SSH_JUMP", role: preprod}
  - {name: db1, address: 10.0.0.9, user: dba, key: "secret:SSH_DB1", role: prod, jump: bastion}
"""


class FakeDetection:
    """Classement des commandes à la manière de l'amont : `rm -rf /` hardline, `rm`/`reboot`/`systemctl restart` dangereux."""

    @staticmethod
    def detect_hardline_command(cmd):
        return ("rm -rf /" in cmd and "rm -rf /tmp" not in cmd) or "mkfs" in cmd, "suppression de la racine"

    @staticmethod
    def detect_dangerous_command(cmd):
        return bool(re.search(r"\brm\b|reboot|systemctl restart", cmd)), "dangereux"


@pytest.fixture(autouse=True)
def detection(monkeypatch):
    monkeypatch.setattr("nokker.tools.code._upstream_detection", lambda: FakeDetection)


class FakeSandbox:
    """Bac à sable scripté : rend `(code, sortie)` ; garde (fil, script, entrée standard, délai)."""

    def __init__(self, result=(0, "ok\n")):
        self.result, self.ran = result, []

    def __call__(self, thread_id, command, stdin, timeout):
        self.ran.append({"thread_id": thread_id, "script": command, "stdin": stdin, "timeout": timeout})
        r = self.result
        return r(command) if callable(r) else r


SECRETS = {"SSH_WEB1": KEY, "SSH_DEV1": KEY, "SSH_JUMP": JUMP_KEY, "SSH_DB1": KEY}


def _secret(name):
    from nokker.secrets.base import SecretsError

    if name not in SECRETS:
        raise SecretsError(f"référence secret:{name} : nom absent du coffre (nokker secret set {name})", "missing")
    return SECRETS[name]


def _source(tmp_path, sandbox=None, hosts=HOSTS, known=("web1", "dev1", "bastion", "db1")):
    conf = tmp_path / "conf"
    conf.mkdir(exist_ok=True)
    (conf / "hosts.yaml").write_text(hosts)
    kh = conf / "known_hosts"
    keys = {"web1": "10.0.0.5", "dev1": "[dev.example.lan]:2222", "bastion": "192.0.2.10", "db1": "10.0.0.9"}
    kh.write_text("".join(f"{keys[n]} ssh-ed25519 {HOST_B64}\n" for n in known))
    src = SshToolSource(sandbox=sandbox or FakeSandbox(), secret=_secret, hosts_path=conf / "hosts.yaml",
                        known_hosts_path=kh, workspace_path=lambda t, p: "/workspace/" + p)
    src.open_session(TH)
    return src


class InnerGates:
    """Portes du noyau derrière les permissions : DESTRUCTIF → carte forte, le reste permis."""

    class invariants:
        @staticmethod
        def check_action(call, spec, turn):
            return Verdict(gate=Gate.ACTION, decision=Decision.ALLOW, layer=1, rule="ok")

    def action(self, call, spec, turn):
        from nokker.core.ports import call_effect

        if call_effect(spec, call.arguments) is Effect.DESTRUCTIVE:
            return [Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule="effect:destructive",
                            strength="strong")]
        return [Verdict(gate=Gate.ACTION, decision=Decision.ALLOW, layer=1, rule="ok")]


def _gates(src, mode=Mode.AUTO, **perm):
    return PermissionGates(inner=InnerGates(), permissions=LocalPermissions(mode=mode, **perm), floors=[src.floor])


def _turn(thread=TH):
    return Turn(thread_id=thread, index=0, input=InputCheck(text="vas-y, fais-le sur web1"), channel="api",
                sender="local-owner")


def _decide(gates, src, name, **args):
    spec = {s.name: s for s in src.specs()}[name]
    verdicts = gates.action(ToolCall(id="c1", name=name, arguments=args), spec, _turn())
    worst = max(verdicts, key=lambda v: v.decision.severity)
    return worst


# ─────────────────────────────── inventaire ───────────────────────────────


def test_inventory_valid_and_invalid_entries(tmp_path):
    f = tmp_path / "hosts.yaml"
    f.write_text(HOSTS + """\
  - {name: norole, address: 10.0.0.1, user: x, key: "secret:K"}
  - {name: plainkey, address: 10.0.0.2, user: x, key: ~/.ssh/id_ed25519, role: dev}
  - {name: dash, address: -oProxyCommand=evil, user: x, key: "secret:K", role: dev}
  - {name: at, address: root@10.0.0.3, user: x, key: "secret:K", role: dev}
  - {name: badrole, address: 10.0.0.4, user: x, key: "secret:K", role: staging}
  - {name: lost, address: 10.0.0.6, user: x, key: "secret:K", role: dev, jump: nowhere}
  - {name: twohops, address: 10.0.0.7, user: x, key: "secret:K", role: dev, jump: db1}
  - {name: web1, address: 10.9.9.9, user: x, key: "secret:K", role: dev}
  - {name: extra, address: 10.0.0.8, user: x, key: "secret:K", role: dev, password: hunter2}
""")
    inv = load_inventory(f)
    assert inv.names() == ["bastion", "db1", "dev1", "web1"]
    assert inv.get("web1").address == "10.0.0.5" and inv.get("web1").port == 22 and inv.get("web1").key == "SSH_WEB1"
    assert inv.get("dev1").known_key == "[dev.example.lan]:2222" and inv.get("web1").known_key == "10.0.0.5"
    assert [h.name for h in inv.chain(inv.get("db1"))] == ["bastion", "db1"]
    text = " | ".join(inv.notices)
    for name in ("norole", "plainkey", "dash", "at", "badrole", "lost", "twohops", "extra"):
        assert f"hôte {name} ignoré" in text, name
    assert "web1 ignoré : nom en double" in text and "hunter2" not in text
    assert "key" not in inv.get("web1").view()  # le modèle ne voit jamais la clé, ni son nom au coffre


def test_inventory_absent_or_mapping_form(tmp_path):
    assert load_inventory(tmp_path / "absent.yaml").hosts == {}
    f = tmp_path / "h.yaml"
    f.write_text("hosts:\n  web2: {address: 10.0.0.2, user: ops, key: 'secret:K2', role: preprod}\n")
    assert load_inventory(f).get("web2").role == "preprod"
    f.write_text("hosts: [unclosed\n")
    assert load_inventory(f).hosts == {} and load_inventory(f).notices


def test_inventory_is_read_once_at_session_open(tmp_path):
    src = _source(tmp_path)
    (tmp_path / "conf" / "hosts.yaml").write_text("hosts: []\n")  # réécrit pendant la session : rien ne change
    assert src.inventory(TH).get("web1") is not None
    assert src.inventory(TH + "/sa-abc").get("web1") is not None  # un sous-agent a l'inventaire de son fil
    assert src.open_session("th_new")["hosts"] == 0


# ─────────────────────────────── known_hosts ───────────────────────────────


def test_known_hosts_plain_port_hashed_and_negation(tmp_path):
    kh = tmp_path / "known_hosts"
    salt = os.urandom(20)
    import hmac

    hashed = base64.b64encode(hmac.new(salt, b"10.1.1.1", hashlib.sha1).digest()).decode()
    kh.write_text(f"# commentaire\n10.0.0.5,web1.lan ssh-ed25519 {HOST_B64}\n[10.0.0.6]:2222 ssh-ed25519 {HOST_B64}\n"
                  f"|1|{base64.b64encode(salt).decode()}|{hashed} ssh-ed25519 {OTHER_B64}\n"
                  f"*.lan,!evil.lan ssh-ed25519 {HOST_B64}\n@revoked 10.0.0.7 ssh-ed25519 {HOST_B64}\n")
    assert known_entries(kh, "10.0.0.5") == [("ssh-ed25519", HOST_B64)]
    assert known_entries(kh, "[10.0.0.6]:2222") and not known_entries(kh, "10.0.0.6")
    assert known_entries(kh, "10.1.1.1") == [("ssh-ed25519", OTHER_B64)]
    assert known_entries(kh, "x.lan") and not known_entries(kh, "evil.lan")
    assert not known_entries(kh, "10.0.0.7")  # une ligne @revoked ne vaut pas confiance
    assert known_lines(kh, ["10.1.1.1"]) == f"10.1.1.1 ssh-ed25519 {OTHER_B64}\n"  # réécrit en clair, seul
    append_known(kh, "[10.0.0.8]:22022", "ssh-ed25519", OTHER_B64)
    assert known_entries(kh, "[10.0.0.8]:22022") == [("ssh-ed25519", OTHER_B64)]
    fresh = tmp_path / "neuf" / "known_hosts"
    append_known(fresh, "10.0.0.9", "ssh-ed25519", HOST_B64)
    assert fresh.stat().st_mode & 0o777 == 0o600 and fresh.parent.stat().st_mode & 0o777 == 0o700


def test_fingerprint_matches_ssh_keygen(tmp_path):
    if not shutil.which("ssh-keygen"):
        pytest.skip("ssh-keygen absent")
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(tmp_path / "k")], check=True)
    pub = (tmp_path / "k.pub").read_text().split()
    out = subprocess.run(["ssh-keygen", "-lf", str(tmp_path / "k.pub")], capture_output=True, text=True,
                         check=True).stdout
    assert fingerprint(pub[1]) == out.split()[1]


# ─────────────────────────────── plancher par rôle ───────────────────────────────


def test_unknown_host_is_refused(tmp_path):
    sandbox = FakeSandbox()
    src = _source(tmp_path, sandbox)
    v = _decide(_gates(src), src, "ssh_run", host="gmk9", command="uptime")
    assert v.decision is Decision.DENY and v.rule == "ops:unknown_host" and "web1" in v.reason
    r = src.call("ssh_run", {"host": "gmk9", "command": "uptime"}, CTX)
    assert r.is_error and "inconnu" in r.content and sandbox.ran == []  # défense en profondeur : rien n'est lancé
    other = ToolContext(thread_id="th_sans_session", turn_index=0, channel="api")
    assert src.call("ssh_hosts", {}, other).is_error


def test_prod_write_needs_a_strong_card_in_every_mode_and_always_does_not_lift_it(tmp_path):
    src = _source(tmp_path)
    allow = [Rule.parse("ssh_run"), Rule.parse("ssh_run(web1:*)")]
    for mode in (Mode.AUTO, Mode.ACCEPT_EDITS, Mode.DEFAULT):
        gates = _gates(src, mode, allow=list(allow))
        gates.permissions.allow_for_thread(TH, Rule.parse("ssh_run(web1:systemctl restart nginx)"))  # « session »
        for command in ("systemctl restart nginx", "touch /tmp/x", "rm -f /var/log/old.log"):
            v = _decide(gates, src, "ssh_run", host="web1", command=command)
            assert (v.decision, v.rule, v.strength) == (Decision.HITL, "ops:prod", "strong"), (mode, command)
    # une lecture sur la prod passe sans carte
    assert _decide(_gates(src), src, "ssh_run", host="web1", command="cat /etc/hostname").decision is Decision.ALLOW
    assert _decide(_gates(src), src, "ssh_copy", host="web1", remote_path="/etc/nginx/nginx.conf").decision \
        is Decision.ALLOW


def test_prod_floor_still_yields_to_deny_rules_and_plan_mode(tmp_path):
    src = _source(tmp_path)
    v = _decide(_gates(src, deny=[Rule.parse("ssh_run(web1:systemctl*)")]), src, "ssh_run", host="web1",
                command="systemctl restart nginx")
    assert v.decision is Decision.DENY and "deny" in v.reason
    v = _decide(_gates(src, Mode.PLAN), src, "ssh_run", host="web1", command="touch /tmp/x")
    assert v.decision is Decision.DENY and "plan" in v.reason


def test_non_prod_follows_the_normal_rules(tmp_path):
    src = _source(tmp_path)
    assert _decide(_gates(src, Mode.AUTO), src, "ssh_run", host="dev1", command="touch /tmp/x").decision \
        is Decision.ALLOW
    v = _decide(_gates(src, Mode.DEFAULT), src, "ssh_run", host="dev1", command="touch /tmp/x")
    assert v.decision is Decision.HITL and v.strength is None
    assert _decide(_gates(src, Mode.DEFAULT, allow=[Rule.parse("ssh_run(dev1:touch *)")]), src, "ssh_run",
                   host="dev1", command="touch /tmp/x").decision is Decision.ALLOW  # une règle de l'utilisateur
    v = _decide(_gates(src, Mode.AUTO), src, "ssh_run", host="dev1", command="systemctl restart app")
    assert (v.decision, v.strength) == (Decision.HITL, "strong")  # destructif : les portes du noyau


def test_hardline_is_never_run_anywhere(tmp_path):
    sandbox = FakeSandbox()
    src = _source(tmp_path, sandbox)
    for host in ("dev1", "web1"):
        v = _decide(_gates(src, Mode.AUTO, allow=[Rule.parse("ssh_run")]), src, "ssh_run", host=host,
                    command="rm -rf / --no-preserve-root")
        assert v.decision is Decision.DENY and "hardline" in v.rule
    r = src.call("ssh_run", {"host": "dev1", "command": "mkfs.ext4 /dev/sda"}, CTX)  # même appelé sans les portes
    assert r.is_error and "jamais exécuté" in r.content and sandbox.ran == []


def test_suggested_rules_for_ssh_cards():
    from nokker.local.approvals import suggested_rules

    assert suggested_rules("ssh_run", {"host": "web1", "command": "systemctl restart nginx"}, "strong") == []
    assert suggested_rules("ssh_trust", {"host": "dev1", "fingerprint": "SHA256:x"}, "standard") == []
    assert suggested_rules("ssh_run", {"host": "dev1", "command": "git pull  --rebase origin"}, "standard") == [
        "ssh_run(dev1:git pull --rebase origin)", "ssh_run(dev1:git pull*)"]
    assert Rule.parse("ssh_run(dev1:git pull*)").matches(
        ToolCall(id="x", name="ssh_run", arguments={"host": "dev1", "command": "git pull --rebase origin"}))
    assert not Rule.parse("ssh_run(dev1:git pull*)").matches(
        ToolCall(id="x", name="ssh_run", arguments={"host": "web1", "command": "git pull"}))


def test_prod_card_cannot_be_approved_with_the_local_token():
    from nokker.approvals.errors import ProofInvalid
    from nokker.approvals.memory import InMemoryApprovals

    approvals = InMemoryApprovals()
    card = approvals.request(_turn(), ToolCall(id="c1", name="ssh_run", arguments={"host": "web1", "command": "x"}),
                             Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule="ops:prod",
                                     strength="strong"), None)
    with pytest.raises(ProofInvalid, match="clé d'appareil"):
        approvals.decide_local(card.id, "approve", "local-owner")
    assert approvals.decide_local(card.id, "reject", "local-owner").status == "rejected"  # refuser reste possible


def test_local_decision_route_refuses_prod_cards(tmp_path):
    import threading
    from http.server import ThreadingHTTPServer

    from nokker.api.server import make_handler
    from nokker.approvals.memory import InMemoryApprovals
    from nokker.local.api import LOCAL_OWNER
    from tests.unit.api.test_approvals_routes import make_kernel_with_approvals
    from tests.unit.test_local_api import _api, _req

    approvals = InMemoryApprovals()
    kernel, _ = make_kernel_with_approvals(approvals)
    api = _api(tmp_path)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(kernel, {api.token: LOCAL_OWNER}, approvals=approvals,
                                                               local_api=api))
    port = httpd.server_address[1]
    api.allowed_hosts = frozenset({f"127.0.0.1:{port}"})
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        card = approvals.request(_turn(), ToolCall(id="c1", name="ssh_run",
                                                   arguments={"host": "web1", "command": "systemctl restart x"}),
                                 Verdict(gate=Gate.ACTION, decision=Decision.HITL, layer=1, rule="ops:prod",
                                         strength="strong"), None)
        status, body = _req(port, f"/v1/local/approvals/{card.id}/decide", token=api.token,
                            body={"decision": "approve", "scope": "once"})
        assert status == 403 and body["error"]["code"] == "device_key_required"
        assert approvals.get(card.id).status == "pending"
    finally:
        httpd.shutdown()


# ─────────────────────────────── la clé ───────────────────────────────


def test_key_goes_by_stdin_into_a_0600_temp_file_and_is_erased(tmp_path):
    sandbox = FakeSandbox((0, "web1\n"))
    src = _source(tmp_path, sandbox)
    r = src.call("ssh_run", {"host": "web1", "command": "hostname"}, CTX)
    assert not r.is_error and json.loads(r.content)["output"] == "web1\n"
    ran = sandbox.ran[0]
    assert ran["stdin"] == KEY  # la clé : sur l'entrée standard seulement
    script = ran["script"]
    assert KEY not in script and "OPENSSH PRIVATE KEY" not in script and "b3BlbnNzaC1r" not in script
    assert "umask 077" in script and "mktemp -d" in script and f"count={len(KEY.encode())}" in script
    assert "trap 'rm -rf \"$d\"' EXIT" in script and script.count('rm -rf "$d"') >= 1
    assert "-oIdentityFile=$d/k" in script and "-n -T" in script
    assert KEY not in r.content and "SSH_WEB1" not in r.content  # ni la valeur, ni le nom au coffre


def test_key_never_reaches_the_model_even_if_the_remote_echoes_it(tmp_path):
    """Boucle réelle du noyau : un hôte hostile renvoie la clé dans sa sortie ; le caviardage du coffre l'arrête
    avant le contexte du modèle et le store."""
    from nokker.core.loop import Kernel
    from nokker.core.turn import Tier
    from nokker.qa.invariants import DeterministicInvariants
    from nokker.qa.secrets import KnownSecrets
    from nokker.tools.registry import Registry
    from tests.unit.test_loop_approvals import (
        Ctx,
        Esc,
        LedgerFake,
        Mem,
        Pol,
        ScriptedProvider,
        StoreFake,
    )

    sandbox = FakeSandbox((0, "voici : " + KEY))
    src = _source(tmp_path, sandbox)
    known = KnownSecrets()
    known.add("SSH_DEV1", KEY)
    inv = DeterministicInvariants(known=known)

    class Inner:
        invariants = inv

        def action(self, call, spec, turn):
            return [inv.check_action(call, spec, turn)]

        def switch(self, reply, turn, tier):
            return [Verdict(gate=Gate.SWITCH, decision=Decision.ALLOW, layer=1, rule="ok")]

    gates = PermissionGates(inner=Inner(), permissions=LocalPermissions(mode=Mode.AUTO), floors=[src.floor])
    provider = ScriptedProvider([Message("assistant", tool_calls=[ToolCall(id="c1", name="ssh_run", arguments={
        "host": "dev1", "command": "cat ~/.ssh/id_ed25519"})]), Message("assistant", "fini")])
    store = StoreFake()
    k = Kernel(store=store, context=Ctx(), memory=Mem(), invariants=inv, gates=gates, escalator=Esc(), policy=Pol(),
               providers={Tier.SHIP: provider}, tools=Registry(sources=[src]), ledger=LedgerFake())
    turn = k.run_turn(TurnInput(TH, "lis la clé", sender="local-owner"))
    seen = json.dumps(provider.seen, ensure_ascii=False) + json.dumps([m.content for t in store.turns
                                                                      for m in t.messages], ensure_ascii=False)
    assert sandbox.ran and "b3BlbnNzaC1r" not in seen and "[secret:SSH_DEV1]" in seen
    assert turn.reply == "fini"


def test_kernel_prod_card_then_device_approval_executes_once_and_is_audited(tmp_path):
    """Boucle réelle : en mode auto avec une règle « always » `ssh_run`, une écriture sur la prod pose une carte
    forte et n'exécute rien ; approuvée (preuve d'appareil, simulée ici), l'exécuteur la lance une fois et le journal
    d'audit chaîné l'enregistre avec le décideur."""
    from nokker.approvals.executor import ApprovalExecutor
    from nokker.approvals.memory import InMemoryApprovals
    from nokker.core.loop import Kernel
    from nokker.core.turn import Tier
    from nokker.ledger.audit import AuditedTools, AuditLog, verify
    from nokker.qa.invariants import DeterministicInvariants
    from nokker.tools.registry import Registry
    from tests.unit.test_loop_approvals import (
        Ctx,
        Esc,
        LedgerFake,
        Mem,
        Pol,
        ScriptedProvider,
        StoreFake,
    )

    sandbox = FakeSandbox((0, "redémarré\n"))
    src = _source(tmp_path, sandbox)
    inv = DeterministicInvariants()

    class Inner:
        invariants = inv

        def action(self, call, spec, turn):
            return [inv.check_action(call, spec, turn)]

        def switch(self, reply, turn, tier):
            return [Verdict(gate=Gate.SWITCH, decision=Decision.ALLOW, layer=1, rule="ok")]

    gates = PermissionGates(inner=Inner(), permissions=LocalPermissions(mode=Mode.AUTO, allow=[Rule.parse("ssh_run")]),
                            floors=[src.floor])
    log = AuditLog(tmp_path / "audit.jsonl")
    approvals = InMemoryApprovals()
    call = ToolCall(id="c1", name="ssh_run", arguments={"host": "web1", "command": "systemctl restart nginx"})
    provider = ScriptedProvider([Message("assistant", tool_calls=[call]), Message("assistant", "j'attends la carte"),
                                 Message("assistant", "nginx redémarré")])
    k = Kernel(store=StoreFake(), context=Ctx(), memory=Mem(), invariants=inv, gates=gates, escalator=Esc(),
               policy=Pol(), providers={Tier.SHIP: provider}, ledger=LedgerFake(), approvals=approvals,
               tools=AuditedTools(inner=Registry(sources=[src]), log=log, describe=[src.describe],
                                  remote=frozenset({"ssh_run", "ssh_copy"})))
    k.run_turn(TurnInput(TH, "vas-y, redémarre nginx sur web1, je confirme", sender="local-owner"))
    card = approvals.pending()[0]
    assert (card.rule, card.strength) == ("ops:prod", "strong") and sandbox.ran == [] and not log.path.exists()
    card.status, card.decided_by, card.decided_via = "approved", "alice", "webauthn"
    ApprovalExecutor(kernel=k, approvals=approvals).run(card)
    assert len(sandbox.ran) == 1 and approvals.get(card.id).status == "executed"
    entry = next(iter(log.entries()))
    assert (entry["tool"], entry["target"], entry["role"], entry["actor"]) == ("ssh_run", "ssh:web1", "prod", "alice")
    assert verify(log.entries(), log.head()).ok


def test_missing_key_in_the_vault_is_an_error_without_value(tmp_path):
    hosts = HOSTS + "  - {name: nokey, address: 10.0.0.11, user: x, key: 'secret:ABSENT', role: dev}\n"
    sandbox = FakeSandbox()
    src = _source(tmp_path, sandbox, hosts=hosts, known=("web1", "dev1", "bastion", "db1"))
    (tmp_path / "conf" / "known_hosts").open("a").write(f"10.0.0.11 ssh-ed25519 {HOST_B64}\n")
    r = src.call("ssh_run", {"host": "nokey", "command": "uptime"}, CTX)
    assert r.is_error and "secret:ABSENT" in r.content and "absent du coffre" in r.content and sandbox.ran == []


# ─────────────────────────────── known_hosts et premier contact ───────────────────────────────


def test_strict_host_key_checking_and_reduced_known_hosts(tmp_path):
    sandbox = FakeSandbox()
    src = _source(tmp_path, sandbox)
    (tmp_path / "conf" / "known_hosts").open("a").write(f"10.99.99.99 ssh-ed25519 {OTHER_B64}\n")
    src.call("ssh_run", {"host": "dev1", "command": "uptime"}, CTX)
    script = sandbox.ran[0]["script"]
    assert "-oStrictHostKeyChecking=yes" in script and "StrictHostKeyChecking=no" not in script
    assert "accept-new" not in script and "GlobalKnownHostsFile=/dev/null" in script and "-F /dev/null" in script
    kh = base64.b64decode(re.search(r"printf '%s' '?([A-Za-z0-9+/=]*)'? \| base64 -d", script).group(1)).decode()
    assert kh == f"[dev.example.lan]:2222 ssh-ed25519 {HOST_B64}\n"  # seulement l'hôte touché


def test_jump_host_uses_its_own_key_and_a_proxy_command(tmp_path):
    sandbox = FakeSandbox()
    src = _source(tmp_path, sandbox)
    src.call("ssh_run", {"host": "db1", "command": "uptime"}, CTX)
    ran = sandbox.ran[0]
    assert ran["stdin"] == KEY + JUMP_KEY
    assert '"-oProxyCommand=ssh ' in ran["script"] and "-oIdentityFile=$d/j" in ran["script"]
    assert "-W %h:%p -l jump" not in ran["script"] and "-l jump -W %h:%p 192.0.2.10" in ran["script"]


def test_first_contact_is_refused_then_shown_then_trusted_on_a_card(tmp_path):
    def keyscan(script):
        return 255, f"Permission denied (none).\n@@nokker-known-hosts@@\n[dev.example.lan]:2222 ssh-ed25519 {OTHER_B64}\n"

    sandbox = FakeSandbox(keyscan)
    src = _source(tmp_path, sandbox, known=("web1", "bastion"))
    gates = _gates(src, Mode.AUTO, allow=[Rule.parse("ssh_trust"), Rule.parse("ssh_run")])
    v = _decide(gates, src, "ssh_run", host="dev1", command="uptime")
    assert v.decision is Decision.DENY and v.rule == "ops:first_contact" and "ssh_trust" in v.reason
    assert _decide(gates, src, "ssh_trust", host="dev1").decision is Decision.ALLOW  # voir l'empreinte : lecture
    shown = json.loads(src.call("ssh_trust", {"host": "dev1"}, CTX).content)
    fp = fingerprint(OTHER_B64)
    assert shown["keys"] == [{"type": "ssh-ed25519", "fingerprint": fp, "known": False}]
    scan = sandbox.ran[0]["script"]
    assert "accept-new" in scan and '-oUserKnownHostsFile=$d/s' in scan and "PubkeyAuthentication=no" in scan
    assert sandbox.ran[0]["stdin"] == ""  # aucune clé envoyée pour un premier contact direct
    v = _decide(gates, src, "ssh_trust", host="dev1", fingerprint=fp)
    assert (v.decision, v.rule) == (Decision.HITL, "ops:trust_host") and fp in v.reason  # même avec « allow »
    bad = src.call("ssh_trust", {"host": "dev1", "fingerprint": "SHA256:autre"}, CTX)
    assert bad.is_error and "ne présente plus" in bad.content and not known_entries(src.known_hosts_path,
                                                                                     "[dev.example.lan]:2222")
    ok = json.loads(src.call("ssh_trust", {"host": "dev1", "fingerprint": fp}, CTX).content)
    assert ok["trusted"] and known_entries(src.known_hosts_path, "[dev.example.lan]:2222") == [
        ("ssh-ed25519", OTHER_B64)]
    again = src.call("ssh_trust", {"host": "dev1", "fingerprint": fp}, CTX)
    assert again.is_error and "déjà une clé" in again.content  # jamais de remplacement
    assert _decide(gates, src, "ssh_run", host="dev1", command="uptime").decision is Decision.ALLOW


def test_jump_must_be_trusted_before_the_host_behind_it(tmp_path):
    src = _source(tmp_path, known=("web1",))
    r = src.call("ssh_trust", {"host": "db1"}, CTX)
    assert r.is_error and "bastion" in r.content
    v = _decide(_gates(src), src, "ssh_run", host="db1", command="uptime")
    assert v.decision is Decision.DENY and "bastion" in v.reason


def test_changed_host_key_gets_a_clear_hint(tmp_path):
    src = _source(tmp_path, FakeSandbox((255, "Host key verification failed.\n")))
    r = json.loads(src.call("ssh_run", {"host": "dev1", "command": "uptime"}, CTX).content)
    assert r["returncode"] == 255 and "interception" in r["hint"]


# ─────────────────────────────── ssh_copy ───────────────────────────────


def test_copy_reads_or_writes_into_the_workspace(tmp_path):
    sandbox = FakeSandbox((0, "x" * 30))
    src = _source(tmp_path, sandbox)
    r = json.loads(src.call("ssh_copy", {"host": "web1", "remote_path": "/etc/motd", "max_bytes": 10}, CTX).content)
    assert r["truncated"] and r["content"] == "x" * 10 and "head -c 11 -- /etc/motd" in sandbox.ran[0]["script"]
    sandbox.result = (0, "1234\n")
    r = json.loads(src.call("ssh_copy", {"host": "web1", "remote_path": "/var/log/app.log", "local_path": "logs/a.log"},
                            CTX).content)
    assert r["bytes"] == "1234" and "/workspace/logs/a.log.nokker-part" in sandbox.ran[1]["script"]
    spec = {s.name: s for s in src.specs()}["ssh_copy"]
    assert spec.effect_for({"host": "web1", "remote_path": "/x"}) is Effect.READ
    assert spec.effect_for({"host": "web1", "remote_path": "/x", "local_path": "y"}) is Effect.WRITE
    sandbox.result = (0, "a\x00b")
    assert src.call("ssh_copy", {"host": "web1", "remote_path": "/bin/ls"}, CTX).is_error


# ─────────────────────────────── drapeau local.ops ───────────────────────────────


def test_flag_off_by_default_and_on_in_the_lokkal_config(tmp_path):
    import yaml

    from nokker.local.api import LocalApi

    root = tmp_path / "projets"
    (root / "app").mkdir(parents=True)
    base = {"token_path": str(tmp_path / "tok"), "state_dir": str(tmp_path / "state"),
            "settings_path": str(tmp_path / "state" / "settings.json"), "code": {"roots": [str(root)],
                                                                                 "backend": "local"}}
    off = LocalApi.from_config(dict(base), port=8742)
    assert off.ssh is None and off.audit is None
    status, body = off.handle("POST", "/v1/local/sessions", f"Bearer {off.token}", {"cwd": str(root / "app")})
    assert status == 201 and "ops" not in body
    assert not any(getattr(s, "floor", None) for s in off.tool_sources())
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "state" / "hosts.yaml").write_text(HOSTS)
    on = LocalApi.from_config({**base, "ops": True}, port=8742)
    assert on.ssh.hosts_path == tmp_path / "state" / "hosts.yaml"
    assert on.ssh.known_hosts_path == tmp_path / "state" / "known_hosts"
    assert on.audit.path == tmp_path / "state" / "audit" / "audit.jsonl"
    status, body = on.handle("POST", "/v1/local/sessions", f"Bearer {on.token}", {"cwd": str(root / "app")})
    assert body["ops"]["hosts"] == 4 and body["ops"]["roles"] == {"prod": 2, "preprod": 1, "dev": 1}
    assert on.ssh in on.tool_sources()
    deploy = yaml.safe_load(Path(__file__).parent.joinpath("../../../deploy/lokkal/core-config.yaml").read_text())
    assert deploy["local"]["ops"] is True


# ─────────────────────────────── essai réel : sshd jetable ───────────────────────────────
# NOKKER_TEST_SSHD = « adresse:port » d'un sshd JETABLE (jamais une vraie machine), NOKKER_TEST_SSHD_USER, et
# NOKKER_TEST_SSHD_KEY = chemin de la clé privée générée pour l'essai. Amont importable. Backend local par défaut ;
# NOKKER_TEST_SSHD_BACKEND=docker (+ NOKKER_TEST_DOCKER_ROOT) : le client ssh tourne dans le bac à sable docker du fil.

UPSTREAM = importlib.util.find_spec("tools") is not None and importlib.util.find_spec("tools.environments") is not None
REAL = os.environ.get("NOKKER_TEST_SSHD")
needs_sshd = pytest.mark.skipif(not (REAL and UPSTREAM), reason="NOKKER_TEST_SSHD et amont requis (sshd jetable)")


@pytest.fixture
def real(tmp_path):
    """Session locale réelle : coffre `file` d'un dossier d'état jetable, clé de l'essai rangée dedans, inventaire
    dev + prod sur le même sshd, backend local (le client ssh tourne ici)."""
    from nokker.local.api import SessionCodeTools
    from nokker.secrets.vault import Vault

    address, port = REAL.rsplit(":", 1)
    state = tmp_path / "state"
    vault = Vault.from_config({"provider": "file"}, state)
    vault.set("SSH_TEST", Path(os.environ["NOKKER_TEST_SSHD_KEY"]).read_text())
    user = os.environ.get("NOKKER_TEST_SSHD_USER", "root")
    (state / "hosts.yaml").write_text(
        f"hosts:\n  - {{name: box, address: {address}, port: {port}, user: {user}, key: 'secret:SSH_TEST', role: dev}}\n"
        f"  - {{name: prodbox, address: {address}, port: {port}, user: {user}, key: 'secret:SSH_TEST', role: prod}}\n"
        # derrière un saut : le sshd jetable lui-même, joint depuis son propre container (127.0.0.1:2222)
        f"  - {{name: behind, address: 127.0.0.1, port: 2222, user: {user}, key: 'secret:SSH_TEST', role: dev, "
        f"jump: box}}\n")
    backend = os.environ.get("NOKKER_TEST_SSHD_BACKEND", "local")
    if backend == "docker":  # bac à sable docker : dossier vu au même chemin par le démon et par ce processus
        import uuid

        work = Path(os.environ["NOKKER_TEST_DOCKER_ROOT"]) / f"x11-{uuid.uuid4().hex[:8]}"
    else:
        work = tmp_path / "work"
    work.mkdir()
    code = SessionCodeTools(backend=backend)
    code.open(TH, str(work), str(work))
    src = SshToolSource(sandbox=code.run_hook, secret=vault.resolver().value, hosts_path=state / "hosts.yaml",
                        known_hosts_path=state / "known_hosts", workspace_path=code.workspace_path)
    src.open_session(TH)
    yield src, vault, work, code
    code.close()
    if backend == "docker":
        shutil.rmtree(work, ignore_errors=True)


@needs_sshd
def test_real_sshd_first_contact_trust_run_copy_and_prod_floor(real, monkeypatch):
    monkeypatch.undo()  # vraie détection de l'amont
    from nokker.approvals.executor import ApprovalExecutor  # noqa: F401 — même chemin que la carte

    src, vault, work, code = real
    gates = PermissionGates(inner=InnerGates(), permissions=LocalPermissions(mode=Mode.AUTO), floors=[src.floor])
    assert _decide(gates, src, "ssh_run", host="box", command="hostname").rule == "ops:first_contact"
    shown = json.loads(src.call("ssh_trust", {"host": "box"}, CTX).content)
    fp = shown["keys"][0]["fingerprint"]
    assert fp.startswith("SHA256:") and not (src.known_hosts_path.exists() and src.known_hosts_path.read_text())
    assert _decide(gates, src, "ssh_trust", host="box", fingerprint=fp).rule == "ops:trust_host"
    assert json.loads(src.call("ssh_trust", {"host": "box", "fingerprint": fp}, CTX).content)["trusted"]
    r = json.loads(src.call("ssh_run", {"host": "box", "command": "echo nokker-$((40+2)); id -un"}, CTX).content)
    assert r["returncode"] == 0 and "nokker-42" in r["output"]
    # la clé n'est restée nulle part : ni dans le bac à sable (container du fil, ou l'hôte en local), ni dans la sortie
    rc, listing = code.run_hook(TH, 'ls -a "${XDG_RUNTIME_DIR:-/tmp}"', "", 10)
    assert rc == 0 and ".nokker-ssh." not in listing
    key = vault.resolver().value("SSH_TEST")
    assert key.strip().splitlines()[1] not in json.dumps(r)
    # derrière un saut : premier contact par le saut, puis commande
    assert _decide(gates, src, "ssh_run", host="behind", command="hostname").rule == "ops:first_contact"
    behind = json.loads(src.call("ssh_trust", {"host": "behind"}, CTX).content)
    assert behind["keys"][0]["fingerprint"] == fp  # même sshd, même clé d'hôte
    assert json.loads(src.call("ssh_trust", {"host": "behind", "fingerprint": fp}, CTX).content)["trusted"]
    r = json.loads(src.call("ssh_run", {"host": "behind", "command": "echo saut-ok"}, CTX).content)
    assert r["returncode"] == 0 and "saut-ok" in r["output"], r
    # lecture et copie d'un fichier distant
    src.call("ssh_run", {"host": "box", "command": "printf 'bonjour\\n' > /tmp/nokker-essai.txt"}, CTX)
    got = json.loads(src.call("ssh_copy", {"host": "box", "remote_path": "/tmp/nokker-essai.txt"}, CTX).content)
    assert got["content"] == "bonjour\n" and not got["truncated"]
    cp = json.loads(src.call("ssh_copy", {"host": "box", "remote_path": "/tmp/nokker-essai.txt",
                                          "local_path": "copie.txt"}, CTX).content)
    assert cp["bytes"] == "8" and (work / "copie.txt").read_text() == "bonjour\n"
    # prod : lecture sans carte, écriture = carte forte même en auto avec allow
    gates.permissions.allow.append(Rule.parse("ssh_run"))
    assert _decide(gates, src, "ssh_run", host="prodbox", command="cat /etc/hostname").decision is Decision.ALLOW
    v = _decide(gates, src, "ssh_run", host="prodbox", command="touch /tmp/nokker-prod")
    assert (v.decision, v.rule, v.strength) == (Decision.HITL, "ops:prod", "strong")
    assert _decide(gates, src, "ssh_run", host="prodbox", command="rm -rf / --no-preserve-root").decision \
        is Decision.DENY
    # une clé d'hôte qui change est refusée (known_hosts de l'utilisateur jamais réécrit en silence)
    src.known_hosts_path.write_text(src.known_hosts_path.read_text().split()[0] + f" ssh-ed25519 {OTHER_B64}\n")
    bad = json.loads(src.call("ssh_run", {"host": "box", "command": "hostname"}, CTX).content)
    assert bad["returncode"] == 255 and "interception" in bad.get("hint", "")
