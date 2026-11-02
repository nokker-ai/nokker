"""Déclencheurs de forge (docs/LOKKAL.md §7.28.9, `nokker/code_sessions/forge_triggers.py`) : charges utiles
enregistrées (GitHub et Forgejo, `forge_payloads/`), signature fausse refusée, rejeu idempotent, une session par PR,
cloisonnement par locataire, poussée sur la branche de la PR seulement, jamais forcée."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from nokker.code_sessions import forge
from nokker.code_sessions import http as code_http
from nokker.code_sessions.config import CodeConfig
from nokker.code_sessions.forge_triggers import (
    CI_MESSAGE,
    COMMENT_MESSAGE,
    FORGE_SENDER,
    PUSH_TOOL,
    ForgeHookError,
    ForgeTriggers,
    MemoryForgeStore,
    push,
    push_argv,
    sign,
)
from nokker.code_sessions.secrets import TenantSecrets
from nokker.code_sessions.service import CodeSessions
from nokker.code_sessions.store import MemoryCodeStore
from nokker.core.ports import ToolContext
from nokker.tenancy.context import tenant_scope
from tests.unit.code_sessions.conftest import MemorySecrets, code_block

TA, TB = "tn_aaaaaaaaaaaa", "tn_bbbbbbbbbbbb"
ANA, CAM = "us_aaaaaaaaaaa1", "us_bbbbbbbbbbb1"
PAYLOADS = Path(__file__).parent / "forge_payloads"
REPO = "https://git.example.org/team/app.git"


def load(name: str) -> bytes:
    return (PAYLOADS / name).read_bytes()


@pytest.fixture
def ft(cluster, fake_upstream):
    clones, turns = [], []

    def cloner(url, dest, token, *, ref=None, depth=None, timeout=300):
        clones.append({"url": url, "ref": ref, "token": token})
        Path(dest, "README.md").write_text("x\n")

    vaults: dict[str, MemorySecrets] = {}
    cfg = CodeConfig.from_config({**code_block(cluster.workspace_root), "forge_triggers": True}, lokkal=False)
    svc = CodeSessions(cfg=cfg, store=MemoryCodeStore(), secrets=TenantSecrets(lambda t: vaults.setdefault(
        t, MemorySecrets())), cloner=cloner)
    from nokker.tenancy.context import current_tenant

    def run_turn(thread, text):
        turns.append({"tenant": current_tenant(), "thread": thread, "text": text})

    t = ForgeTriggers(sessions=svc, store=MemoryForgeStore(), run_turn=run_turn)
    t.clones, t.turns = clones, turns  # type: ignore[attr-defined]
    yield t
    svc.close_all()


def hook(ft, tenant, user, provider="github", **kw):
    with tenant_scope(tenant):
        return ft.create_hook(user, {"provider": provider, "repo": {"url": REPO}, **kw})


def headers(provider, secret, body, event, delivery="d-1", bad=False):
    sig = sign(secret, body)
    if bad:
        sig = sign("autre-secret", body)
    if provider == "github":
        return {"X-GitHub-Event": event, "X-GitHub-Delivery": delivery, "X-Hub-Signature-256": "sha256=" + sig}
    return {"X-Forgejo-Event": event, "X-Forgejo-Delivery": delivery, "X-Forgejo-Signature": sig}


def send(ft, h, name, event, delivery="d-1", provider=None, secret=None, bad=False):
    body = load(name)
    p = provider or h["provider"]
    return ft.receive(h["id"], headers(p, secret or h["secret"], body, event, delivery, bad=bad), body)


def test_flag_off_no_routes(cluster):
    cfg = CodeConfig.from_config(code_block(cluster.workspace_root), lokkal=False)
    assert cfg.forge_triggers is False
    code_http.configure(object())  # service sans déclencheurs
    try:
        assert code_http.forge_triggers() is None
        assert code_http.handle(None, "POST", "/v1/code/forge-hooks/fh_0123456789abcdef/events") is False
    finally:
        code_http.configure(None)


def test_bad_or_missing_signature_refused_nothing_opened(ft):
    h = hook(ft, TA, ANA)
    assert send(ft, h, "github_pull_request_opened.json", "pull_request", bad=True)[0] == 401
    body = load("github_pull_request_opened.json")
    assert ft.receive(h["id"], {"X-GitHub-Event": "pull_request"}, body)[0] == 401
    # signature au format Forgejo sur un hook GitHub : refusée aussi
    assert send(ft, h, "github_pull_request_opened.json", "pull_request", provider="forgejo")[0] == 401
    # corps modifié après signature
    hd = headers("github", h["secret"], body, "pull_request")
    assert ft.receive(h["id"], hd, body.replace(b"opened", b"closed"))[0] == 401
    assert ft.clones == [] and ft.turns == []
    assert ft.receive("fh_0123456789abcdef", hd, body)[0] == 404


def test_one_session_per_pr_replay_idempotent_and_close(ft):
    h = hook(ft, TA, ANA)
    st, out = send(ft, h, "github_pull_request_opened.json", "pull_request", "d-1")
    assert st == 200 and out["status"] == "opened"
    assert ft.clones == [{"url": REPO, "ref": "fix/remises", "token": None}]
    # rejeu de la même livraison : rien n'est refait
    assert send(ft, h, "github_pull_request_opened.json", "pull_request", "d-1") == (200, {"status": "duplicate"})
    # nouvelle livraison de la même PR : reprise de LA session
    st, again = send(ft, h, "github_pull_request_opened.json", "pull_request", "d-2")
    assert again["status"] == "resumed" and again["session_id"] == out["session_id"] and len(ft.clones) == 1
    with tenant_scope(TA):
        assert [p["number"] for p in ft.list_prs(ANA, h["id"])["sessions"]] == [7]
    st, closed = send(ft, h, "forgejo_pull_request_closed.json", "pull_request", "d-3", provider="github")
    assert closed["status"] == "closed"
    with tenant_scope(TA):
        assert ft.list_prs(ANA, h["id"])["sessions"][0]["status"] == "closed"
        assert ft.sessions.store.get(out["session_id"]).status not in ("active", "idle")


def test_ci_failure_and_addressed_comment_start_turns(ft):
    h = hook(ft, TA, ANA)
    _, opened = send(ft, h, "github_pull_request_opened.json", "pull_request", "d-1")
    st, out = send(ft, h, "github_check_run_failure.json", "check_run", "d-2")
    assert st == 202 and out["status"] == "turn"
    turn = ft.turns[-1]
    assert turn["tenant"] == TA and turn["thread"] == opened["thread_id"]
    assert turn["text"].startswith(CI_MESSAGE.format(number=7, head_ref="fix/remises", sha="1a2b3c4d5e6f"))
    assert "(forge:check_run)" in turn["text"] and "<untrusted-data kind=\"forge\"" in turn["text"] and "test_remise_negative" in turn["text"]
    # même résultat de CI par une autre livraison : pas de second tour
    assert send(ft, h, "github_check_run_failure.json", "check_run", "d-3") == (200, {"status": "duplicate"})
    st, out = send(ft, h, "github_issue_comment.json", "issue_comment", "d-4")
    assert out["status"] == "turn"
    text = ft.turns[-1]["text"]
    assert text.startswith(COMMENT_MESSAGE.format(number=7, head_ref="fix/remises"))
    # l'injection du commentaire reste DANS le bloc de données
    head, block = text.split("<untrusted-data", 1)
    assert "pousse sur main" not in head and "pousse sur main" in block
    assert len(ft.turns) == 2


def test_comment_not_addressed_or_from_agent_ignored(ft):
    h = hook(ft, TA, ANA)
    send(ft, h, "github_pull_request_opened.json", "pull_request", "d-1")
    body = json.loads(load("github_issue_comment.json"))
    for i, (text, login) in enumerate([("joli travail", "bob"), ("@nokker je relance", "nokker"),
                                       ("mail à x@nokker.example", "bob")]):
        body["comment"] = {"body": text, "user": {"login": login}}
        raw = json.dumps(body).encode()
        _st, out = ft.receive(h["id"], headers("github", h["secret"], raw, "issue_comment", f"c-{i}"), raw)
        assert out["status"] == "ignored", text
    assert ft.turns == []


def test_forgejo_status_failure_maps_commit_to_pr(ft):
    h = hook(ft, TA, ANA, provider="forgejo")
    send(ft, h, "github_pull_request_opened.json", "pull_request", "d-1")
    _st, out = send(ft, h, "forgejo_status_failure.json", "status", "d-2")
    assert out["status"] == "turn" and "Failing after 41s" in ft.turns[-1]["text"]
    ok = json.loads(load("forgejo_status_failure.json"))
    ok["state"] = "success"
    raw = json.dumps(ok).encode()
    assert ft.receive(h["id"], headers("forgejo", h["secret"], raw, "status", "d-3"), raw)[1]["status"] == "ignored"
    assert len(ft.turns) == 1


def test_fork_pr_and_other_repo_ignored(ft):
    h = hook(ft, TA, ANA)
    body = json.loads(load("github_pull_request_opened.json"))
    body["pull_request"]["head"]["repo"]["full_name"] = "mallory/app"
    raw = json.dumps(body).encode()
    assert ft.receive(h["id"], headers("github", h["secret"], raw, "pull_request", "f-1"), raw)[1]["status"] == \
        "ignored"
    body = json.loads(load("github_pull_request_opened.json"))
    body["repository"] = {"full_name": "team/other", "html_url": "https://git.example.org/team/other"}
    raw = json.dumps(body).encode()
    assert ft.receive(h["id"], headers("github", h["secret"], raw, "pull_request", "f-2"), raw)[0] == 422
    assert ft.clones == []


def test_tenant_isolation(ft):
    a = hook(ft, TA, ANA)
    b = hook(ft, TB, CAM)
    # le secret de A ne vaut pas sur le hook de B (et inversement)
    assert send(ft, b, "github_pull_request_opened.json", "pull_request", secret=a["secret"])[0] == 401
    _, ob = send(ft, b, "github_pull_request_opened.json", "pull_request", "d-1")
    _, oa = send(ft, a, "github_pull_request_opened.json", "pull_request", "d-1")  # même id de livraison, autre hook
    assert oa["status"] == ob["status"] == "opened" and oa["session_id"] != ob["session_id"]
    assert oa["thread_id"].startswith(TA + ":") and ob["thread_id"].startswith(TB + ":")
    with tenant_scope(TA):
        assert [x["id"] for x in ft.list_hooks(ANA)["hooks"]] == [a["id"]]
        assert ft.store.get_hook(b["id"]) is None and ft.store.get_pr(b["id"], 7) is None
        assert ft.sessions.store.get(ob["session_id"]) is None
        with pytest.raises(ForgeHookError):  # le hook de B n'existe pas pour A
            ft.list_prs(ANA, b["id"])
    send(ft, b, "github_check_run_failure.json", "check_run", "d-2")
    assert [t["tenant"] for t in ft.turns] == [TB] and ft.turns[0]["thread"] == ob["thread_id"]
    # la PR de B ne pousse jamais depuis un fil de A
    with tenant_scope(TA):
        r = ft.tools().call(PUSH_TOOL, {}, ToolContext(thread_id=ob["thread_id"], turn_index=0, channel="forge"))
    assert r.is_error


def test_secret_shown_once_never_listed(ft):
    h = hook(ft, TA, ANA)
    assert h["secret"].startswith("whsec_")
    with tenant_scope(TA):
        assert h["secret"] not in json.dumps(ft.list_hooks(ANA))


# ─────────────────────────── poussée ───────────────────────────

def test_push_argv_never_forces_or_merges():
    argv = push_argv("/tmp/b", "https://git.example.org/team/app.git", "fix/remises")
    joined = " ".join(argv)
    assert "--force" not in joined and " -f" not in joined and "merge" not in joined and "--delete" not in joined
    assert argv[-1] == "refs/heads/nokker-push:refs/heads/fix/remises" and not argv[-1].startswith("+")
    for bad in ("-f", "--force", "a..b", "x;rm"):
        with pytest.raises(forge.ForgeError):
            push("/tmp/x", "https://git.example.org/a.git", None, bad, run=lambda *a: (0, ""))


def _git(cwd, *args):
    subprocess.run(["git", "-c", "user.email=a@example.org", "-c", "user.name=alice", *args], cwd=cwd, check=True,
                   capture_output=True)


def test_push_real_git_fast_forward_only(tmp_path):
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "-q", str(remote))
    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(remote), str(work))
    (work / "a.txt").write_text("1\n")
    _git(work, "add", "a.txt")
    _git(work, "commit", "-qm", "un")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/fix/remises")
    (work / "a.txt").write_text("2\n")
    _git(work, "commit", "-qam", "deux")
    # un hook et une config hostiles dans le dépôt de travail ne servent pas
    hooks = work / ".git" / "hooks" / "pre-push"
    hooks.write_text("#!/bin/sh\ntouch " + str(tmp_path / "PWNED") + "\n")
    os.chmod(hooks, 0o755)
    _git(work, "config", "url.file:///nowhere/.insteadOf", str(remote))
    push(str(work), str(remote), None, "fix/remises", allow_file_remote=True)
    head = subprocess.run(["git", "--git-dir", str(remote), "rev-parse", "refs/heads/fix/remises"],
                          capture_output=True, text=True, check=False).stdout.strip()
    assert head == subprocess.run(["git", "rev-parse", "HEAD"], cwd=work, capture_output=True, text=True, check=False).stdout.strip()
    assert not (tmp_path / "PWNED").exists()
    # histoire réécrite : la forge refuse, et on ne force jamais
    _git(work, "reset", "-q", "--hard", "HEAD~1")
    (work / "a.txt").write_text("3\n")
    _git(work, "commit", "-qam", "trois")
    with pytest.raises(forge.ForgeError) as e:
        push(str(work), str(remote), None, "fix/remises", allow_file_remote=True)
    assert e.value.code == "push_failed"
    assert subprocess.run(["git", "--git-dir", str(remote), "rev-parse", "refs/heads/fix/remises"],
                          capture_output=True, text=True, check=False).stdout.strip() == head
    branches = subprocess.run(["git", "--git-dir", str(remote), "branch", "--list"], capture_output=True,
                              text=True, check=False).stdout.split()
    assert branches == ["fix/remises"]  # rien d'autre n'a été poussé (ni main, ni nokker-push)


def test_push_tool_only_on_pr_thread_branch_from_pr(ft):
    h = hook(ft, TA, ANA)
    _, opened = send(ft, h, "github_pull_request_opened.json", "pull_request", "d-1")
    calls = []
    ft.pusher = lambda root, url, token, ref: calls.append((url, ref)) or "ok"
    with tenant_scope(TA):
        tools = ft.tools()
        assert tools.specs()[0].effect.value == "write"
        r = tools.call(PUSH_TOOL, {"branch": "main"}, ToolContext(thread_id=opened["thread_id"], turn_index=0,
                                                                   channel="forge"))
        assert r.is_error and calls == []  # aucun argument : la branche est celle de la PR
        r = tools.call(PUSH_TOOL, {}, ToolContext(thread_id=opened["thread_id"], turn_index=0, channel="forge",
                                                  sender=FORGE_SENDER))
        assert not r.is_error and calls == [(REPO, "fix/remises")]
        r = tools.call(PUSH_TOOL, {}, ToolContext(thread_id=opened["thread_id"], turn_index=0, channel="api",
                                                  sender="us_aaaaaaaaaaa9"))
        assert r.is_error  # un autre compte
        r = tools.call(PUSH_TOOL, {}, ToolContext(thread_id=TA + ":autre", turn_index=0, channel="api"))
        assert r.is_error


def test_forge_sender_is_not_a_go_authority():
    from nokker.qa.invariants import DeterministicInvariants

    inv = DeterministicInvariants()
    inv.account_tenant = lambda u: TA if u == ANA else None
    with tenant_scope(TA):
        assert inv.go_authorized(ANA) is True
        assert inv.go_authorized(FORGE_SENDER) is False


def test_http_webhook_route_without_identity(ft):
    import io

    h = hook(ft, TA, ANA)
    body = load("github_pull_request_opened.json")

    class H:
        def __init__(self, hd):
            self.headers = {**hd, "Content-Length": str(len(body))}
            self.rfile = io.BytesIO(body)
            self.out = None

        def _json(self, status, payload):
            self.out = (status, payload)

        def _resolve_identity(self, body):  # jamais appelé pour la route de la forge
            raise AssertionError("identité demandée")

    code_http.configure(ft.sessions, ft)
    try:
        good = H(headers("github", h["secret"], body, "pull_request", "w-1"))
        assert code_http.handle(good, "POST", f"/v1/code/forge-hooks/{h['id']}/events") is True
        assert good.out[0] == 200 and good.out[1]["status"] == "opened"
        bad = H(headers("github", h["secret"], body, "pull_request", "w-2", bad=True))
        code_http.handle(bad, "POST", f"/v1/code/forge-hooks/{h['id']}/events")
        assert bad.out[0] == 401
    finally:
        code_http.configure(None)


# ─────────── seuls les collaborateurs du dépôt déclenchent une session (relecture du format, 02.11) ───────────


@pytest.mark.parametrize("assoc, launched", [("OWNER", True), ("MEMBER", True), ("COLLABORATOR", True),
                                             ("CONTRIBUTOR", False), ("FIRST_TIME_CONTRIBUTOR", False),
                                             ("FIRST_TIMER", False), ("NONE", False), ("MANNEQUIN", False),
                                             (None, False), ("owner", True)])
def test_github_comment_needs_a_collaborator_association(ft, assoc, launched):
    h = hook(ft, TA, ANA)
    send(ft, h, "github_pull_request_opened.json", "pull_request", "d-1")
    body = json.loads(load("github_issue_comment.json"))
    body["comment"] = {"body": "@nokker corrige le test", "user": {"login": "mallory"},
                       **({"author_association": assoc} if assoc is not None else {})}
    raw = json.dumps(body).encode()
    _st, out = ft.receive(h["id"], headers("github", h["secret"], raw, "issue_comment", "c-x"), raw)
    assert (out["status"] == "turn") is launched, assoc
    if not launched:
        assert out == {"status": "ignored", "reason": "auteur sans droit sur le dépôt"} and ft.turns == []
    # relecture et commentaire de relecture : même règle
    for i, (event, key) in enumerate((("pull_request_review", "review"),
                                      ("pull_request_review_comment", "comment"))):
        b = {"action": "submitted" if key == "review" else "created", "pull_request": {"number": 7},
             key: {"body": "@nokker regarde", "user": {"login": "mallory"}, "state": "commented",
                   **({"author_association": assoc} if assoc is not None else {})},
             "repository": body["repository"]}
        raw = json.dumps(b).encode()
        _st, out = ft.receive(h["id"], headers("github", h["secret"], raw, event, f"r-{i}"), raw)
        assert (out["status"] == "turn") is launched, (event, assoc)


def test_forgejo_comment_only_from_the_repository_owner(ft):
    h = hook(ft, TA, ANA, provider="forgejo")
    send(ft, h, "github_pull_request_opened.json", "pull_request", "d-1")
    base = {"action": "created", "is_pull": True, "issue": {"number": 7, "pull_request": {"merged": False}},
            "repository": {"full_name": "team/app", "html_url": "https://git.example.org/team/app",
                           "owner": {"login": "team", "username": "team"}}}
    for i, (login, launched) in enumerate((("mallory", False), ("bob", False), ("team", True), ("Team", True))):
        body = {**base, "comment": {"body": "@nokker corrige", "user": {"login": login, "username": login}}}
        raw = json.dumps(body).encode()
        _st, out = ft.receive(h["id"], headers("forgejo", h["secret"], raw, "issue_comment", f"f-{i}"), raw)
        assert (out["status"] == "turn") is launched, (login, out)
    no_owner = {**base, "repository": {"full_name": "team/app", "html_url": "https://git.example.org/team/app"},
                "comment": {"body": "@nokker corrige", "user": {"login": "team"}}}
    raw = json.dumps(no_owner).encode()
    assert ft.receive(h["id"], headers("forgejo", h["secret"], raw, "issue_comment", "f-9"), raw)[1]["status"] == \
        "ignored"
