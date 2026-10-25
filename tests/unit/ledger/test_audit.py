"""Journal d'audit chaîné (lot X11, nokker/ledger/audit.py) : chaîne intacte, entrée modifiée, retirée, journal
tronqué, ancre, export JSONL/CSV, enveloppe du registre (effets, ssh, caviardage) et `nokker audit`."""
from __future__ import annotations

import csv
import io
import itertools
import json
import os

import pytest

from nokker.core.ports import ToolContext, ToolSpec
from nokker.core.turn import Effect, ToolResult
from nokker.ledger.audit import GENESIS, AuditedTools, AuditLog, export, verify

CTX = ToolContext(thread_id="th_a", turn_index=2, channel="api", sender="local-owner")


def _log(tmp_path, n=5) -> AuditLog:
    log = AuditLog(tmp_path / "audit" / "audit.jsonl")
    for i in range(n):
        log.append({"thread_id": "th_a", "turn_index": i, "tool": "terminal", "effect": "write",
                    "args": {"command": f"echo {i}"}})
    return log


def _lines(log):
    return log.path.read_text().splitlines()


def test_chain_is_intact_and_files_are_private(tmp_path):
    log = _log(tmp_path)
    entries = list(log.entries())
    assert [e["seq"] for e in entries] == [1, 2, 3, 4, 5] and entries[0]["prev"] == GENESIS
    assert all(b["prev"] == a["hash"] for a, b in itertools.pairwise(entries))
    v = verify(log.entries(), log.head())
    assert v.ok and v.count == 5 and v.last_seq == 5 and v.last_hash == entries[-1]["hash"]
    assert log.head() == {"seq": 5, "hash": entries[-1]["hash"]}
    assert os.stat(log.path).st_mode & 0o777 == 0o600 and os.stat(log.path.parent).st_mode & 0o777 == 0o700


def test_modified_entry_is_detected(tmp_path):
    log = _log(tmp_path)
    lines = _lines(log)
    e = json.loads(lines[2])
    e["args"]["command"] = "echo rien"  # réécrite sans recalculer
    lines[2] = json.dumps(e)
    log.path.write_text("\n".join(lines) + "\n")
    v = verify(log.entries(), log.head())
    assert not v.ok and any("entrée 3 : modifiée" in p for p in v.problems)
    # recalculer l'empreinte de l'entrée ne suffit pas : la suivante pointe sur l'ancienne
    from nokker.ledger.audit import entry_hash

    e["hash"] = entry_hash(e)
    lines[2] = json.dumps(e)
    log.path.write_text("\n".join(lines) + "\n")
    v = verify(log.entries(), log.head())
    assert not v.ok and any("entrée 4 : « prev »" in p for p in v.problems)


def test_removed_entry_in_the_middle_or_at_the_start_is_detected(tmp_path):
    log = _log(tmp_path)
    lines = _lines(log)
    log.path.write_text("\n".join(lines[:2] + lines[3:]) + "\n")
    v = verify(log.entries(), log.head())
    assert not v.ok and any("entrée 4 : suit l'entrée 2" in p for p in v.problems)
    log.path.write_text("\n".join(lines[1:]) + "\n")
    v = verify(log.entries(), log.head())
    assert not v.ok and any("entrée 2 : suit l'entrée 0" in p for p in v.problems)


def test_truncated_journal_is_detected_by_the_head_and_by_an_anchor(tmp_path):
    log = _log(tmp_path)
    lines = _lines(log)
    anchor = (5, json.loads(lines[4])["hash"])
    log.path.write_text("\n".join(lines[:3]) + "\n")
    v = verify(log.entries(), log.head())
    assert not v.ok and any("tronqué" in p for p in v.problems)
    log.head_path.unlink()  # tête effacée aussi : seule une ancre gardée ailleurs le voit
    assert verify(log.entries(), log.head()).ok
    v = verify(log.entries(), log.head(), anchor)
    assert not v.ok and any("ancre 5 introuvable" in p for p in v.problems)
    # coupé au milieu d'une ligne : illisible
    log.path.write_text("\n".join(lines[:2]) + "\n" + lines[2][:40] + "\n")
    assert any("illisible" in p for p in verify(log.entries()).problems)


def test_append_continues_the_chain_after_a_restart_and_without_head(tmp_path):
    log = _log(tmp_path, 2)
    again = AuditLog(log.path)
    assert again.append({"tool": "patch"})["seq"] == 3
    again.head_path.unlink()
    assert AuditLog(log.path).append({"tool": "patch"})["seq"] == 4
    assert verify(again.entries(), again.head()).ok


def test_export_jsonl_is_verifiable_and_csv_is_flat(tmp_path):
    log = _log(tmp_path, 3)
    out = io.StringIO()
    assert export(log.entries(), "jsonl", out) == 3
    assert verify(json.loads(x) for x in out.getvalue().splitlines()).ok
    out = io.StringIO()
    assert export(log.entries(), "csv", out) == 3
    rows = list(csv.DictReader(io.StringIO(out.getvalue())))
    assert rows[0]["seq"] == "1" and json.loads(rows[0]["args"]) == {"command": "echo 0"} and rows[2]["hash"]
    assert export(log.entries(), "jsonl", io.StringIO(), since="2999-01-01") == 0
    assert export(log.entries(), "jsonl", io.StringIO(), since="2000-01-01T00:00:00Z") == 3
    with pytest.raises(ValueError):
        export(log.entries(), "xml", io.StringIO())


class Registry:
    def __init__(self):
        self.specs = {"read_file": ToolSpec("read_file", "", {}, Effect.READ),
                      "write_file": ToolSpec("write_file", "", {}, Effect.WRITE),
                      "ssh_copy": ToolSpec("ssh_copy", "", {}, Effect.READ),
                      "terminal": ToolSpec("terminal", "", {}, Effect.DESTRUCTIVE,
                                           effect_of=lambda a: Effect.READ if a.get("command") == "ls" else Effect.WRITE)}
        self.called = []
        self.extra = "attribut du registre"

    def spec(self, name):
        return self.specs.get(name)

    def call(self, name, arguments, ctx):
        self.called.append(name)
        return ToolResult(call_id="", name=name, content=f"résultat {name} hunter2", is_error=name == "write_file")


def test_audited_tools_record_effects_and_remote_reads_only(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    reg = Registry()
    tools = AuditedTools(inner=reg, log=log, scrub=lambda s: s.replace("hunter2", "[secret:pw]"),
                         describe=[lambda n, a, t: {"target": "ssh:web1", "role": "prod"} if n == "ssh_copy" else None],
                         remote=frozenset({"ssh_copy"}))
    tools.call("read_file", {"path": "a"}, CTX)
    tools.call("terminal", {"command": "ls"}, CTX)  # lecture locale : pas au journal
    tools.call("terminal", {"command": "make install"}, CTX)
    tools.call("write_file", {"path": "a.txt", "content": "mdp hunter2 " + "x" * 5000}, CTX)
    tools.call("ssh_copy", {"host": "web1", "remote_path": "/etc/motd"}, CTX)  # lecture distante : au journal
    entries = list(log.entries())
    assert [e["tool"] for e in entries] == ["terminal", "write_file", "ssh_copy"] and len(reg.called) == 5
    t, w, s = entries
    assert t["target"] == "local" and t["effect"] == "write" and t["actor"] == "local-owner" and t["turn_index"] == 2
    assert w["is_error"] and "hunter2" not in json.dumps(w) and "[secret:pw]" in w["args"]["content"]
    assert len(w["args"]["content"]) < 2100 and len(w["args_sha256"]) == 64
    assert s["target"] == "ssh:web1" and s["role"] == "prod" and s["effect"] == "read"
    assert verify(log.entries(), log.head()).ok
    assert tools.extra == "attribut du registre"  # le reste est celui du registre
    child = tools.wrap(Registry())
    child.call("write_file", {"path": "b", "content": ""}, CTX)
    assert list(log.entries())[-1]["seq"] == 4  # même journal pour un sous-agent


def test_a_broken_journal_never_breaks_a_call(tmp_path):
    (tmp_path / "ro").mkdir()
    log = AuditLog(tmp_path / "ro" / "audit.jsonl")
    tools = AuditedTools(inner=Registry(), log=log)
    os.chmod(tmp_path / "ro", 0o500)
    try:
        if os.geteuid() == 0:
            pytest.skip("root écrit partout")
        assert tools.call("terminal", {"command": "make"}, CTX).content.startswith("résultat")
    finally:
        os.chmod(tmp_path / "ro", 0o700)


def test_cli_verify_and_export(tmp_path, capsys):
    from nokker.cli import main

    log = _log(tmp_path, 3)
    assert main(["audit", "verify", "--file", str(log.path)]) == 0
    out = capsys.readouterr().out
    assert "journal intact : 3 entrée(s)" in out
    anchor = out.strip().split("ancre ")[1]
    assert main(["audit", "verify", "--file", str(log.path), "--anchor", anchor]) == 0
    capsys.readouterr()
    assert main(["audit", "export", "--file", str(log.path), "--format", "csv"]) == 0
    assert capsys.readouterr().out.startswith("seq,at,")
    log.path.write_text("\n".join(_lines(log)[:2]) + "\n")
    assert main(["audit", "verify", "--file", str(log.path)]) == 1
    assert "[ROMPU]" in capsys.readouterr().out
    assert main(["audit", "verify", "--file", str(tmp_path / "absent.jsonl")]) == 1
