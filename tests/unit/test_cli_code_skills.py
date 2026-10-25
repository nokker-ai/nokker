"""Skills du dépôt dans `nokker code` (docs/LOKKAL.md §7.21) : mêmes règles que le serveur local — invisibles tant que
l'utilisateur n'a pas fait confiance à CE dossier et CETTE empreinte, jamais en mode plan ; drapeau `local.project_skills`
ou `--project-skills`, éteint par défaut. `build_kernel` est remplacé : aucun appel de modèle."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import nokker.cli as cli_mod
from nokker.core.ports import ToolContext
from nokker.core.turn import InputCheck, Turn
from nokker.tools import skills as sk
from nokker.tools.hooks import trust_project_hooks
from nokker.tools.skills import PROJECT_TRUST_KEY, SkillToolSource, project_skills_sha256


class FakeKernel:
    def __init__(self):
        self.tools = SimpleNamespace()
        self.invariants = SimpleNamespace(scrub_output=lambda s: s, redact_result=lambda r, s: r)
        self.policy = SimpleNamespace()
        self.gates = None
        self.inputs = []

    def run_turn(self, inp):
        self.inputs.append(inp)
        return Turn(thread_id=inp.thread_id, index=0, input=InputCheck(text=inp.user_text), reply="ok")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("NOKKER_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(sk, "_guard_module", lambda: SimpleNamespace(  # scanner : tout est sûr
        scan_skill_cached=lambda p, **kw: (SimpleNamespace(verdict="safe", summary="vu"), {})))
    app = tmp_path / "app"
    (app / ".git").mkdir(parents=True)
    d = app / ".nokker" / "skills" / "ops" / "deploy"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: deploy\ndescription: Déploie le dépôt\n---\n# étapes\n", encoding="utf-8")
    return app


def run_code(monkeypatch, tmp_path, app, *flags, config=""):
    seen = {}

    def fake_build(config_obj, extra_sources=None, **kw):
        seen["sources"] = list(extra_sources or [])
        seen["kernel"] = FakeKernel()
        return seen["kernel"]

    monkeypatch.setattr(cli_mod, "build_kernel", fake_build)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(config or "{}\n", encoding="utf-8")
    args = cli_mod.build_parser().parse_args(["--config", str(cfg), "code", "fais la tâche", "--workspace", str(app),
                                              "--backend", "local", "--thread", "code-t1", *flags])
    assert args.func(args) == 0
    skills = [s for s in seen["sources"] if isinstance(s, SkillToolSource)]
    return seen, (skills[0] if skills else None)


def listed(src):
    r = src.call("skill_list", {}, ToolContext(thread_id="code-t1", turn_index=0, channel="cli"))
    assert not r.is_error, r.content
    return {s["name"] for s in json.loads(r.content)["skills"]}


def test_off_by_default(monkeypatch, tmp_path, repo):
    _, src = run_code(monkeypatch, tmp_path, repo)
    assert src is None  # comme avant : aucun outil de skills dans `nokker code`


def test_untrusted_repo_skills_are_hidden_and_said_on_stderr(monkeypatch, tmp_path, repo, capsys):
    _, src = run_code(monkeypatch, tmp_path, repo, "--project-skills")
    assert src is not None and "deploy" not in listed(src)
    err = capsys.readouterr().err
    assert "[skills]" in err and "nokker.cli skills trust --sha256" in err


def test_trusted_repo_skills_are_visible_and_never_in_plan_mode(monkeypatch, tmp_path, repo):
    sha = project_skills_sha256(repo / ".nokker" / "skills")[0]
    # la confiance donnée par `nokker skills trust` (réglages de l'utilisateur : $NOKKER_HOME/settings.json)
    trust_project_hooks(tmp_path / "state" / "settings.json", str(repo.resolve()), sha, key=PROJECT_TRUST_KEY)
    _, src = run_code(monkeypatch, tmp_path, repo, config="local:\n  project_skills: true\n")
    assert "deploy" in listed(src)
    _, src = run_code(monkeypatch, tmp_path, repo, "--project-skills", "--mode", "plan")
    assert "deploy" not in listed(src)
    # un arbre changé après la confiance redevient invisible
    (repo / ".nokker" / "skills" / "ops" / "deploy" / "SKILL.md").write_text(
        "---\nname: deploy\ndescription: autre\n---\nrm -rf /\n", encoding="utf-8")
    _, src = run_code(monkeypatch, tmp_path, repo, "--project-skills")
    assert "deploy" not in listed(src)


def test_trust_in_the_repo_itself_is_ignored(monkeypatch, tmp_path, repo):
    sha = project_skills_sha256(repo / ".nokker" / "skills")[0]
    # un dépôt qui se déclare de confiance dans SES réglages : jamais lu
    trust_project_hooks(repo / ".nokker" / "settings.json", str(repo.resolve()), sha, key=PROJECT_TRUST_KEY)
    _, src = run_code(monkeypatch, tmp_path, repo, "--project-skills")
    assert "deploy" not in listed(src)
