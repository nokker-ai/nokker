"""Suppression d'un compte par la personne (nokker/account/delete.py) : carte FORTE, action hors registre d'outils,
compte suspendu dès l'accord, file d'effacement ; seul le compte lui-même peut l'accorder."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from nokker.account import delete
from nokker.approvals.executor import ApprovalExecutor
from nokker.approvals.memory import InMemoryApprovals
from nokker.tenancy.context import tenant_scope
from nokker.tenancy.resolve import UnknownSender, resolve_tenant
from nokker.tenancy.store import MemoryTenancy


def _world():
    tenancy = MemoryTenancy()
    acc = tenancy.create_account("a@example.org")
    approvals = InMemoryApprovals()
    approvals.user_tenant = lambda user: tenancy.account(user).tenant_id if tenancy.account(user) else None
    ex = ApprovalExecutor(kernel=SimpleNamespace(), approvals=approvals,
                          account_actions={delete.ACTION: delete.executor_action(tenancy)})
    return tenancy, acc, approvals, ex


def _decide(approvals, card, status, by):
    card = approvals._load_approval(card.id)
    card.status, card.decided_by = status, by
    approvals._store_approval(card)
    return card


def test_card_is_strong_idempotent_and_not_a_tool():
    tenancy, acc, approvals, _ = _world()
    with tenant_scope(acc.tenant_id):
        card = delete.request(approvals, acc, acc.tenant_id)
        again = delete.request(approvals, acc, acc.tenant_id)
    assert card.strength == "strong" and card.channel == delete.ACCOUNT_CHANNEL and card.tool_name == delete.ACTION
    assert again.id == card.id and card.thread_id == f"{acc.tenant_id}:account"


def test_approved_by_the_account_suspends_and_queues_the_erasure():
    tenancy, acc, approvals, ex = _world()
    with tenant_scope(acc.tenant_id):
        card = delete.request(approvals, acc, acc.tenant_id)
    assert ex.run(_decide(approvals, card, "approved", acc.account_id)) is None  # aucun tour modèle
    assert tenancy.account(acc.account_id).status == "suspended" and tenancy.deletions == {acc.account_id: card.id}
    assert approvals._load_approval(card.id).status == "executed"
    with pytest.raises(UnknownSender):
        resolve_tenant(acc.account_id, tenancy, set())  # plus aucune requête du compte


@pytest.mark.parametrize("status,by", [("approved", "us_quelquun_dautre"), ("rejected", None)])
def test_someone_else_or_a_refusal_changes_nothing(status, by):
    tenancy, acc, approvals, ex = _world()
    with tenant_scope(acc.tenant_id):
        card = delete.request(approvals, acc, acc.tenant_id)
    assert ex.run(_decide(approvals, card, status, by)) is None
    assert tenancy.account(acc.account_id).status == "active" and not tenancy.deletions


def _pending_world(monkeypatch, queue, *, refuse=()):
    """`nokker erase --pending` sans base : file, effacement et verrou remplacés ; la config porte les trois stockages
    hors base (images, sessions de code, index de session CortHeXis)."""
    from nokker import cli
    from nokker.admin import erase as erase_mod

    calls: list[dict] = []
    cfg = SimpleNamespace(images_root=lambda: "/var/lib/nokker/images", code={"workspace_root": "/workspaces"},
                          context_corthexis={"enabled": True, "root_dir": "/var/lib/nokker/session-memory"},
                          pg_dsn=lambda: "postgresql://invalid")

    def fake_erase(dsn, **kw):
        calls.append(kw)
        if kw["account_id"] in refuse:
            raise erase_mod.EraseError(f"refus pour {kw['account_id']}")
        report = erase_mod.Report(account_id=kw["account_id"], tenant_id="tn_" + kw["account_id"][3:],
                                  email=f"{kw['account_id']}@example.org", devices=[], deleted={"turns": 3})
        if kw["apply"] and kw.get("before_commit") is not None:
            try:
                kw["before_commit"](report)  # comme _erase_db : juste avant la validation
            except OSError as e:
                raise erase_mod.EraseError(f"journal : {type(e).__name__}") from None
        report.applied = kw["apply"]
        return report

    monkeypatch.setattr(cli, "load_config", lambda path=None: cfg)
    monkeypatch.setattr(erase_mod, "pending_deletions", lambda dsn: list(queue))
    monkeypatch.setattr(erase_mod, "erase_account", fake_erase)
    return cli, erase_mod, calls


def test_erase_pending_passes_the_full_scope(monkeypatch, capsys):
    """La file des suppressions demandées efface AUSSI hors base : images jointes, sessions de code, index de session
    CortHeXis (`erase_scope(config)`), comme l'effacement sur demande — sinon les notes d'épisodes et les espaces de
    travail d'un compte supprimé survivent."""
    from contextlib import contextmanager

    cli, erase_mod, calls = _pending_world(monkeypatch, ["us_x", "us_y"])

    @contextmanager
    def free_lock(dsn):
        yield True

    monkeypatch.setattr(erase_mod, "_pending_lock", free_lock, raising=False)
    assert cli.main(["erase", "--pending", "--apply"]) == 0
    assert [c["account_id"] for c in calls] == ["us_x", "us_y"]
    for c in calls:
        assert c["confirm"] == c["account_id"] and c["apply"] is True
        assert c["images_root"] == "/var/lib/nokker/images" and c["code"] == {"workspace_root": "/workspaces"}
        assert c["session_memory"] == {"enabled": True, "root_dir": "/var/lib/nokker/session-memory"}


def test_erase_pending_prints_one_line_of_numbers_and_a_date(monkeypatch, capsys):
    """`nokker erase --pending --apply` : stdout = UNE ligne par passage, des nombres et une date, rien d'autre —
    jamais l'adresse, ni l'identifiant du compte, ni celui du locataire (ces sorties sont gardées sans fin) ; un refus
    sort en code 1, sans identité non plus."""
    import re
    from contextlib import contextmanager

    cli, erase_mod, _ = _pending_world(monkeypatch, ["us_x", "us_y", "us_z"], refuse=("us_z",))

    @contextmanager
    def free_lock(dsn):
        yield True

    monkeypatch.setattr(erase_mod, "_pending_lock", free_lock, raising=False)
    assert cli.main(["erase", "--pending", "--apply"]) == 1
    out, err = capsys.readouterr()
    lines = out.strip().splitlines()
    assert len(lines) == 1
    line = json.loads(lines[0])
    assert set(line) == {"erased_at", "erased", "refused"} and line["erased"] == 2 and line["refused"] == 1
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", line["erased_at"])
    for leak in ("us_x", "us_y", "us_z", "tn_x", "tn_y", "example.org"):
        assert leak not in out + err

    # À blanc : rien sur stdout (rien n'est effacé), le résumé sur stderr seulement.
    cli, erase_mod, calls = _pending_world(monkeypatch, ["us_x"])
    monkeypatch.setattr(erase_mod, "_pending_lock", free_lock, raising=False)
    assert cli.main(["erase", "--pending"]) == 0
    out, err = capsys.readouterr()
    assert out == "" and "1 compte(s) à blanc" in err and calls[0]["apply"] is False and "us_x" not in err


def test_erase_pending_does_nothing_while_another_run_holds_the_lock(monkeypatch, capsys):
    """Deux passages en même temps (CronJob relancé, opérateur à la main) : le second ne touche à rien et sort en 0 ;
    la ligne du journal est celle du passage qui tient le verrou."""
    from contextlib import contextmanager

    cli, erase_mod, calls = _pending_world(monkeypatch, ["us_x"])

    @contextmanager
    def busy_lock(dsn):
        yield False

    monkeypatch.setattr(erase_mod, "_pending_lock", busy_lock, raising=False)
    assert cli.main(["erase", "--pending", "--apply"]) == 0
    out, err = capsys.readouterr()
    assert calls == [] and out == "" and "déjà en cours" in err


def test_subscriptions_are_canceled_first_and_a_stripe_failure_suspends_nothing():
    tenancy, acc, approvals, ex = _world()
    canceled = []
    ex.account_actions[delete.ACTION] = delete.executor_action(tenancy, canceled.append)
    with tenant_scope(acc.tenant_id):
        card = delete.request(approvals, acc, acc.tenant_id)
    ex.run(_decide(approvals, card, "approved", acc.account_id))
    assert canceled == [acc.tenant_id] and tenancy.account(acc.account_id).status == "suspended"

    tenancy2, acc2, approvals2, ex2 = _world()

    def stripe_down(tenant_id):
        raise RuntimeError("stripe_unavailable")

    ex2.account_actions[delete.ACTION] = delete.executor_action(tenancy2, stripe_down)
    with tenant_scope(acc2.tenant_id):
        card2 = delete.request(approvals2, acc2, acc2.tenant_id)
    ex2.run(_decide(approvals2, card2, "approved", acc2.account_id))
    assert tenancy2.account(acc2.account_id).status == "active" and not tenancy2.deletions
    assert approvals2._load_approval(card2.id).status == "failed"


def _free_lock(erase_mod, monkeypatch):
    from contextlib import contextmanager

    @contextmanager
    def free_lock(dsn):
        yield True

    monkeypatch.setattr(erase_mod, "_pending_lock", free_lock)


def test_erase_pending_journal_one_line_per_account_mode_0600(monkeypatch, capsys, tmp_path):
    """`--journal PATH` : une ligne `{tenant_id, erased_at}` par compte effacé, en ajout, fichier créé en 0600 ; ni
    l'adresse ni le compte `us_…` (ni dans le fichier, ni sur stdout, qui garde sa ligne de nombres)."""
    import os
    import stat

    cli, erase_mod, _ = _pending_world(monkeypatch, ["us_x", "us_y"])
    _free_lock(erase_mod, monkeypatch)
    journal = tmp_path / "beta-erasures.jsonl"
    assert cli.main(["erase", "--pending", "--apply", "--journal", str(journal)]) == 0
    assert stat.S_IMODE(os.stat(journal).st_mode) == 0o600
    lines = [json.loads(x) for x in journal.read_text().splitlines()]
    assert [x["tenant_id"] for x in lines] == ["tn_x", "tn_y"] and all(set(x) == {"tenant_id", "erased_at"} for x in lines)
    assert "us_" not in journal.read_text() and "example.org" not in journal.read_text()
    out, err = capsys.readouterr()
    assert set(json.loads(out.strip())) == {"erased_at", "erased", "refused"}
    for leak in ("us_x", "us_y", "tn_x", "tn_y", "example.org"):
        assert leak not in out + err
    assert "rejouable" not in err  # avec journal : pas d'avertissement

    cli.main(["erase", "--pending", "--apply", "--journal", str(journal)])  # en AJOUT, jamais réécrit
    assert len(journal.read_text().splitlines()) == 4


def test_erase_pending_journal_write_error_refuses_the_account(monkeypatch, capsys, tmp_path):
    """Écriture du journal impossible (disque plein…) : le compte est refusé AVANT la validation, il reste dans la
    file ; code 1."""
    cli, erase_mod, calls = _pending_world(monkeypatch, ["us_x"])
    _free_lock(erase_mod, monkeypatch)

    def full(self, tenant_id):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(erase_mod.ErasureJournal, "append", full)
    assert cli.main(["erase", "--pending", "--apply", "--journal", str(tmp_path / "j.jsonl")]) == 1
    out, _ = capsys.readouterr()
    assert json.loads(out.strip())["erased"] == 0 and json.loads(out.strip())["refused"] == 1


@pytest.mark.parametrize("where", ["missing-dir", "read-only-dir"])
def test_erase_pending_journal_unusable_path_refuses_before_anything(monkeypatch, capsys, tmp_path, where):
    """Chemin absent ou répertoire non inscriptible : code 2, rien n'est lu ni effacé, le verrou n'est pas pris."""
    import os

    cli, erase_mod, calls = _pending_world(monkeypatch, ["us_x"])
    taken = []

    from contextlib import contextmanager

    @contextmanager
    def lock(dsn):
        taken.append(1)
        yield True

    monkeypatch.setattr(erase_mod, "_pending_lock", lock)
    if where == "missing-dir":
        path = tmp_path / "absent" / "j.jsonl"
    else:
        ro = tmp_path / "ro"
        ro.mkdir()
        path = ro / "j.jsonl"
        monkeypatch.setattr(os, "access", lambda p, mode, **kw: False if str(p) == str(ro) else True)
    assert cli.main(["erase", "--pending", "--apply", "--journal", str(path)]) == 2
    assert calls == [] and taken == [] and not path.exists()
    out, err = capsys.readouterr()
    assert out == "" and "journal" in err


def test_erase_pending_without_journal_warns(monkeypatch, capsys):
    cli, erase_mod, _ = _pending_world(monkeypatch, ["us_x"])
    _free_lock(erase_mod, monkeypatch)
    assert cli.main(["erase", "--pending", "--apply"]) == 0
    out, err = capsys.readouterr()
    assert "sans journal" in err and "rejouable" in err and "tn_x" not in out + err
