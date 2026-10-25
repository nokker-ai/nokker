"""Skills du dépôt dans une session de code multi-utilisateur (docs/LOKKAL.md §7.28, §7.21) : lus dans le dépôt cloné
de LA session, invisibles tant que SON propriétaire n'a pas fait confiance à cet arbre (empreinte), confiance rangée
chez l'utilisateur sous `code.state_dir` (jamais dans le dépôt), jamais en mode plan ; un arbre qui contient un lien
symbolique est refusé (le noyau lirait un fichier hors du dépôt) ; hors session, `skill_list`/`skill_view` restent ceux
du compte (packs du web)."""
from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from nokker.code_sessions.config import CodeConfig
from nokker.code_sessions.service import NO_SESSION, CodeError, CodeSessions
from nokker.code_sessions.store import MemoryCodeStore
from nokker.core.ports import ToolContext
from nokker.core.turn import ToolResult
from nokker.tenancy.context import tenant_scope
from nokker.tools import skills as sk
from tests.unit.code_sessions.conftest import code_block

TA, TB = "tn_aaaaaaaaaaaa", "tn_bbbbbbbbbbbb"
ANA, BEN = "us_aaaaaaaaaaa1", "us_aaaaaaaaaaa2"
REPO = "https://git.example.org/team/app.git"


def _skill(dest, fam, name, desc, body="# Mode d'emploi\nlance make lint\n"):
    d = os.path.join(dest, ".nokker", "skills", fam, name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write(f"---\nname: {name}\ndescription: {desc}\n---\n{body}")
    return d


def repo_cloner(url, dest, token, **_):
    os.makedirs(os.path.join(dest, ".git"))
    _skill(dest, "dev", "lint", "Linter du dépôt")
    os.makedirs(os.path.join(dest, ".nokker", "skills", "dev", "lint", "references"))
    with open(os.path.join(dest, ".nokker", "skills", "dev", "lint", "references", "regles.md"), "w") as f:
        f.write("règle 1\n")


@pytest.fixture(autouse=True)
def safe_guard(monkeypatch):
    monkeypatch.setattr(sk, "_guard_module", lambda: SimpleNamespace(
        scan_skill_cached=lambda p, **kw: (SimpleNamespace(verdict="safe", summary="vu"), {})))


class Packs:
    """Les skills du compte (packs du web, `WebPackTools`) : le repli hors session, et après le dépôt dans une session."""

    def __init__(self):
        self.calls = []

    def specs(self):
        return []

    def call(self, name, arguments, ctx):
        self.calls.append((name, ctx.thread_id, ctx.sender))
        if name == "skill_list":
            return ToolResult(call_id="", name=name, content=json.dumps({"skills": [
                {"name": "lint", "category": "dev", "description": "pack", "source": "pack:dev"},
                {"name": "rédaction", "category": "écrit", "description": "pack", "source": "pack:écrit"}]}))
        if arguments.get("name") == "rédaction":
            return ToolResult(call_id="", name=name, content=json.dumps({"name": "rédaction", "source": "pack:écrit"}))
        return ToolResult(call_id="", name=name, content="compétence introuvable", is_error=True)


@pytest.fixture
def make(cluster, fake_upstream):
    made = []

    def _make(cloner=repo_cloner, **over):
        over.setdefault("modes", ["default", "accept_edits", "plan", "auto"])
        cfg = CodeConfig.from_config(code_block(cluster.workspace_root, **over), lokkal=False)
        s = CodeSessions(cfg=cfg, store=MemoryCodeStore(), cloner=cloner)
        made.append(s)
        return s
    yield _make
    for s in made:
        s.close_all()


def _open(svc, user=ANA, tenant=TA, **body):
    body.setdefault("repo", {"url": REPO})
    with tenant_scope(tenant):
        s = svc.create(user, body)
    return s, f"{tenant}:{s['id']}"


def _call(svc, name, args, thread, sender=ANA, tenant=TA):
    ctx = ToolContext(thread_id=thread, turn_index=1, channel="api", sender=sender)
    with tenant_scope(tenant):
        r = svc.skill_tools().call(name, args, ctx)
    return r, (json.loads(r.content) if not r.is_error else r.content)


def test_listed_and_read_from_the_session_repo_after_trust(make):
    svc = make()
    s, thread = _open(svc)
    st = s["skills"]
    assert st["enabled"] is True and st["project"] == "untrusted" and st["count"] == 1 and st["sha256"]
    assert st["root"] == "/workspace"  # jamais le chemin du volume côté noyau
    assert "POST /v1/code/sessions/" in st["notice"] and "/v1/local/" not in st["notice"]
    assert _call(svc, "skill_list", {}, thread)[1]["skills"] == []  # pas de confiance : invisible
    assert _call(svc, "skill_view", {"name": "lint"}, thread)[0].is_error
    with tenant_scope(TA):
        out = svc.trust_skills(ANA, s["id"], st["sha256"])
    assert out == {"thread_id": s["id"], "root": "/workspace", "sha256": st["sha256"], "trusted": True}
    lst = _call(svc, "skill_list", {}, thread)[1]["skills"]
    assert [(x["name"], x["source"]) for x in lst] == [("lint", "project")]
    view = _call(svc, "skill_view", {"name": "lint"}, thread)[1]
    assert "make lint" in view["content"] and view["linked_files"] == ["references/regles.md"]
    assert "règle 1" in _call(svc, "skill_view", {"name": "lint", "file": "references/regles.md"}, thread)[1]["content"]
    # un sous-agent de la session les voit aussi
    assert _call(svc, "skill_list", {}, f"{thread}/sa-0123abcd")[1]["skills"][0]["name"] == "lint"
    with tenant_scope(TA):
        assert svc.get(ANA, s["id"])["skills"]["project"] == "trusted"


def test_trust_is_per_user_and_stored_under_state_dir(make, cluster):
    svc = make()
    s, thread = _open(svc)
    sha = s["skills"]["sha256"]
    with tenant_scope(TA):
        with pytest.raises(CodeError) as e:
            svc.trust_skills(BEN, s["id"], sha)  # pas sa session : elle n'existe pas pour lui
        assert e.value.status == 404
        with pytest.raises(CodeError) as e:
            svc.trust_skills(ANA, s["id"], "0" * 64)
        assert e.value.status == 409 and e.value.code == "skills_changed"
        svc.trust_skills(ANA, s["id"], sha)
    trust = os.path.join(svc.cfg.state_dir, TA, ANA, "settings.json")
    assert sha in open(trust, encoding="utf-8").read()
    assert not trust.startswith(os.path.join(cluster.workspace_root, TA) + os.sep)
    assert not os.path.exists(os.path.join(cluster.workspace_root, TA, ANA, s["id"], ".nokker", "settings.json"))
    # ben ouvre le même dépôt : sa session n'a pas la confiance d'ana
    s2, t2 = _open(svc, user=BEN)
    assert s2["skills"]["project"] == "untrusted"
    assert _call(svc, "skill_list", {}, t2, sender=BEN)[1]["skills"] == []
    assert not os.path.exists(os.path.join(svc.cfg.state_dir, TA, BEN, "settings.json"))
    # ben appelle l'outil sur le fil d'ana : refus
    r, text = _call(svc, "skill_list", {}, thread, sender=BEN)
    assert r.is_error and "autre utilisateur" in text


def test_tenant_scope_and_fallback_outside_a_session(make):
    svc = make()
    s, thread = _open(svc)
    with tenant_scope(TA):
        svc.trust_skills(ANA, s["id"], s["skills"]["sha256"])
    r, text = _call(svc, "skill_list", {}, thread, sender="us_bbbbbbbbbbb1", tenant=TB)
    assert r.is_error and text == NO_SESSION  # le fil d'un autre locataire n'est pas une session ici
    packs = Packs()
    svc.skills_fallback = lambda: packs
    r, out = _call(svc, "skill_list", {}, f"{TA}:th_web")  # hors session : les skills du compte, tels quels
    assert [x["name"] for x in out["skills"]] == ["lint", "rédaction"] and packs.calls[-1][1] == f"{TA}:th_web"
    # dans la session : le dépôt d'abord, puis ceux du compte qui ne sont pas masqués
    lst = _call(svc, "skill_list", {}, thread)[1]["skills"]
    assert [(x["name"], x["source"]) for x in lst] == [("lint", "project"), ("rédaction", "pack:écrit")]
    assert _call(svc, "skill_view", {"name": "rédaction"}, thread)[1]["source"] == "pack:écrit"
    assert "make lint" in _call(svc, "skill_view", {"name": "lint"}, thread)[1]["content"]


def test_never_in_plan_mode_and_changed_tree_untrusted(make, cluster):
    svc = make()
    s, thread = _open(svc, mode="plan")
    with tenant_scope(TA):
        svc.trust_skills(ANA, s["id"], s["skills"]["sha256"])
    assert _call(svc, "skill_list", {}, thread)[1]["skills"] == []  # mode plan : aucun skill du dépôt
    s, thread = _open(svc)
    with tenant_scope(TA):
        svc.trust_skills(ANA, s["id"], s["skills"]["sha256"])
    _skill(os.path.join(cluster.workspace_root, TA, ANA, s["id"]), "dev", "lint", "réécrit par le modèle")
    assert _call(svc, "skill_list", {}, thread)[1]["skills"] == []
    with tenant_scope(TA):
        st = svc.get(ANA, s["id"])["skills"]
    assert st["project"] == "untrusted" and st["sha256"] != s["skills"]["sha256"]


def test_symlink_in_the_skills_tree_is_refused(make, tmp_path):
    outside = tmp_path / "noyau-secret.txt"
    outside.write_text("SECRET-DU-NOYAU")

    def cloner(url, dest, token, **_):
        d = _skill(dest, "dev", "lint", "x")
        os.remove(os.path.join(d, "SKILL.md"))
        os.symlink(outside, os.path.join(d, "SKILL.md"))

    svc = make(cloner=cloner)
    s, thread = _open(svc)
    assert s["skills"]["project"] == "invalid" and "lien symbolique" in s["skills"]["notice"]
    with tenant_scope(TA), pytest.raises(CodeError) as e:
        svc.trust_skills(ANA, s["id"], s["skills"]["sha256"] or "x")
    assert e.value.status == 404
    assert "SECRET" not in json.dumps(_call(svc, "skill_list", {}, thread)[1])


def test_flag_off(make):
    svc = make(project_skills=False)
    s, thread = _open(svc)
    assert "skills" not in s
    with tenant_scope(TA), pytest.raises(CodeError) as e:
        svc.trust_skills(ANA, s["id"], "x")
    assert e.value.status == 404
    assert _call(svc, "skill_list", {}, thread)[1]["skills"] == []  # sans repli ni dépôt : rien
