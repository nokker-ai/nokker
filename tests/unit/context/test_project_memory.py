"""Mémoire de projet (lot X1, docs/LOKKAL.md §7.11) : découverte des fichiers d'instructions et frontière de la racine,
clé de projet, réimport sur changement d'empreinte, noyau sous budget, rappel par pertinence, portée interdite,
oubli, drapeau éteint. Le moteur CortHeXis est remplacé par un faux lexical (pas de Postgres ni d'embeddings)."""
from __future__ import annotations

import json
import re
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from nokker.context.engine import ContextEngineV0, estimate_tokens
from nokker.context.project_memory import (
    GUARD,
    USER_PROJECT,
    ProjectMemory,
    ProjectMemoryTools,
    ProjectRecallMemory,
    code_prefix,
    discover,
    find_git_root,
    from_local_config,
    normalize_remote,
    project_key,
    project_name,
    split_sections,
)
from nokker.core.ports import Recall, ToolContext
from nokker.core.turn import Decision, Effect, InputCheck, Tier, ToolCall, Turn
from nokker.local.api import LocalApi
from nokker.qa.permissions import LocalPermissions, Mode
from nokker.tenancy.resources import TenantMemory
from tests.unit.context.test_engine import FakeRegistry


class FakeEngine:
    """Même port que `CorthexisSemanticIndex` (enqueue_project, search_rows), sur les fichiers de notes eux-mêmes."""

    def __init__(self, root: Path):
        self.root, self.enqueued = Path(root), []

    def enqueue_project(self, project):
        self.enqueued.append(project)

    def search_rows(self, projects, query, k=8):
        q = {w for w in re.findall(r"\w+", query.lower()) if len(w) >= 4}
        rows = []
        for project in projects:
            for p in (self.root / project).glob("*.md"):
                text = p.read_text(encoding="utf-8")
                name = re.search(r"^name: (.+)$", text, re.MULTILINE).group(1)
                desc = json.loads(re.search(r"^description: (.+)$", text, re.MULTILINE).group(1))
                words = set(re.findall(r"\w+", text.lower()))
                score = len(q & words) / max(1, len(q))
                if score:
                    rows.append({"note_name": name, "score": score, "rerank": None, "description": desc,
                                 "snippet": text.split("---", 2)[-1].strip()[:200]})
        return sorted(rows, key=lambda r: -r["score"])[:k]


def make_repo(tmp_path: Path, remote: str | None = None) -> Path:
    repo = tmp_path / "work" / "app"
    (repo / ".git").mkdir(parents=True)
    if remote:
        (repo / ".git" / "config").write_text(f'[core]\n\tbare = false\n[remote "origin"]\n\turl = {remote}\n')
    return repo


def make_memory(tmp_path: Path, **kw) -> tuple[ProjectMemory, FakeEngine]:
    engine = FakeEngine(tmp_path / "pm")
    events = []
    pm = ProjectMemory(root=tmp_path / "pm", engine=engine, emit=lambda name, data: events.append((name, data)), **kw)
    pm.events = events  # type: ignore[attr-defined]
    return pm, engine


def ctx(thread="th_1", turn=0) -> ToolContext:
    return ToolContext(thread_id=thread, turn_index=turn, channel="local")


# ─────────────────────────────── découverte et frontière ───────────────────────────────


def test_discover_from_session_folder_up_to_git_root_outer_first(tmp_path):
    repo = make_repo(tmp_path)
    (tmp_path / "work" / "AGENTS.md").write_text("# au-dessus du dépôt\njamais lu\n")
    (repo / "AGENTS.md").write_text("# Racine\n")
    (repo / "CLAUDE.md").write_text("# Claude\n")
    (repo / "api").mkdir()
    (repo / "api" / "NOKKER.md").write_text("# API\n")
    (repo / "api" / "deep").mkdir()
    found = discover(repo / "api" / "deep", tmp_path)
    assert [str(p.relative_to(repo)) for p in found] == ["AGENTS.md", "CLAUDE.md", "api/NOKKER.md"]
    assert find_git_root(repo / "api" / "deep", tmp_path) == repo.resolve()


def test_discover_never_goes_above_the_mounted_root(tmp_path):
    repo = make_repo(tmp_path)  # le .git est AU-DESSUS de la racine montée
    (repo / "AGENTS.md").write_text("# hors montage\n")
    mounted = repo / "pkg"
    (mounted / "sub").mkdir(parents=True)
    (mounted / "AGENTS.md").write_text("# monté\n")
    outside = tmp_path / "secret.md"
    outside.write_text("# dehors\n")
    (mounted / "sub" / "CLAUDE.md").symlink_to(outside)  # un lien qui sort de la racine montée est ignoré
    assert [p.name for p in discover(mounted / "sub", mounted)] == ["AGENTS.md"]
    assert find_git_root(mounted / "sub", mounted) is None
    assert discover(tmp_path, mounted) == []  # dossier hors de la racine : rien


def test_project_key_is_the_remote_without_credentials_or_the_git_root(tmp_path):
    https = make_repo(tmp_path / "a", "https://nick:ghp_jeton1234567890@GitHub.com/acme/app.git")
    ssh = make_repo(tmp_path / "b", "git@github.com:acme/app")
    key_a, label, base = project_key(https, tmp_path)
    assert key_a == "remote:github.com/acme/app" and label == "github.com/acme/app" and base == https.resolve()
    assert "jeton" not in key_a and project_key(ssh, tmp_path)[0] == key_a  # même dépôt, même projet
    bare = make_repo(tmp_path / "c")
    assert project_key(bare / ".", tmp_path)[0] == f"path:{bare.resolve()}"
    assert re.fullmatch(r"p-[0-9a-f]{16}", project_name(key_a))
    assert normalize_remote("ssh://git@git.example.org:2222/org/x.git/") == "git.example.org/org/x"


def test_worktree_git_file_leads_to_the_common_config(tmp_path):
    main = make_repo(tmp_path, "https://git.example.org/org/x.git")
    wt = tmp_path / "work" / "wt"
    (main / ".git" / "worktrees" / "wt").mkdir(parents=True)
    (main / ".git" / "worktrees" / "wt" / "commondir").write_text("../..\n")
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {main / '.git' / 'worktrees' / 'wt'}\n")
    assert project_key(wt, tmp_path)[0] == "remote:git.example.org/org/x"


def test_split_sections_by_headings_outside_code_fences():
    text = "Intro\n\n# Tests\nlance pytest\n```sh\n# pas un titre\n```\n## Style\nfrançais\n### Vide\n"
    assert split_sections(text) == [("(préambule)", "Intro"), ("Tests", "lance pytest\n```sh\n# pas un titre\n```"),
                                    ("Style", "français")]


# ─────────────────────────────── import, réimport, noyau ───────────────────────────────


def test_import_as_project_sources_and_reimport_when_the_fingerprint_changes(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "AGENTS.md").write_text("# Tests\nlance `pytest -q`\n# Style\nfrançais\n")
    pm, engine = make_memory(tmp_path)
    pm.open_session("th_1", repo, tmp_path)
    project = project_name(project_key(repo, tmp_path)[0])
    notes = sorted(p.name for p in (tmp_path / "pm" / project).glob("src_*.md"))
    assert len(notes) == 2 and engine.enqueued == [project]
    assert "pytest -q" in pm.core_block("th_1", 0, "bonjour")
    pm.core_block("th_1", 1, "encore")  # rien n'a changé : pas de réindexation
    assert engine.enqueued == [project]
    (repo / "AGENTS.md").write_text("# Tests\nlance `make check`\n")
    assert "pytest -q" in pm.core_block("th_1", 1, "même tour")  # figé pendant le tour
    block = pm.core_block("th_1", 2, "tour suivant")
    assert "make check" in block and "pytest -q" not in block and engine.enqueued == [project, project]
    assert len(list((tmp_path / "pm" / project).glob("src_*.md"))) == 1
    restarted, engine2 = make_memory(tmp_path)  # redémarrage : réindexé une fois même sans changement
    restarted.open_session("th_9", repo, tmp_path)
    restarted.open_session("th_10", repo, tmp_path)
    assert engine2.enqueued == [project]
    (repo / "AGENTS.md").unlink()
    assert pm.core_block("th_1", 3, "x") == ""  # fichier disparu, rien d'appris : rien d'injecté
    assert list((tmp_path / "pm" / project).glob("src_*.md")) == []


def test_session_opened_higher_keeps_the_subfolder_sources_of_the_project(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "api").mkdir()
    (repo / "api" / "AGENTS.md").write_text("# API\nroutes en snake_case\n")
    pm, _ = make_memory(tmp_path)
    pm.open_session("th_api", repo / "api", tmp_path)
    pm.open_session("th_root", repo, tmp_path)
    project = project_name(project_key(repo, tmp_path)[0])
    assert len(list((tmp_path / "pm" / project).glob("src_*.md"))) == 1  # pas retiré : le fichier existe
    assert pm.core_block("th_root", 0, "x") == ""  # mais pas dans le noyau d'une session qui ne le voit pas
    assert "snake_case" in pm.core_block("th_api", 0, "x")


def test_core_stays_under_its_token_budget_and_says_it_grants_nothing(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "AGENTS.md").write_text("".join(f"# Section {i}\n" + ("consigne détaillée " * 40) + "\n"
                                            for i in range(30)))
    pm, _ = make_memory(tmp_path, core_tokens=400)
    pm.open_session("th_1", repo, tmp_path)
    block = pm.core_block("th_1", 0, "x")
    assert 0 < estimate_tokens(block) <= 400
    assert GUARD in block and "n'accordent JAMAIS de permission" in block
    assert "rappelés par pertinence" in block  # ce qui n'entre pas est annoncé
    for budget in (800, 120, 30):
        pm2, _ = make_memory(tmp_path / str(budget), core_tokens=budget)
        pm2.open_session("th_1", repo, tmp_path)
        assert estimate_tokens(pm2.core_block("th_1", 0, "x")) <= budget


def test_core_is_injected_in_the_system_message_after_the_profile(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "NOKKER.md").write_text("# Commits\nmessages en français\n")
    pm, _ = make_memory(tmp_path)
    pm.open_session("th_1", repo, tmp_path)
    prefix = tmp_path / "prefix.md"
    prefix.write_text("Tu es Nokker.")
    engine = ContextEngineV0(tools=FakeRegistry(), prefix_path=str(prefix), project_memory=pm)
    out = engine.assemble(Turn(thread_id="th_1", index=0, input=InputCheck(text="go")), [], [], [], Tier.SHIP)
    system = out.messages[0]["content"]
    assert system.startswith("Tu es Nokker.") and "[Mémoire du projet]" in system and "messages en français" in system
    assert out.layer_tokens["project"] > 0
    other = engine.assemble(Turn(thread_id="th_web", index=0, input=InputCheck(text="go")), [], [], [], Tier.SHIP)
    assert other.messages[0]["content"] == "Tu es Nokker."  # fil sans session de code : rien


# ─────────────────────────────── rappel ───────────────────────────────


def test_rest_is_recalled_by_relevance_without_repeating_the_core(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "AGENTS.md").write_text("# Tests\nlance pytest\n" + "".join(
        f"# Annexe {i}\n" + ("remplissage " * 60) + "\n" for i in range(8)) + "# Déploiement\nkubectl rollout "
        "restart deployment/web avant migration\n" + ("détail " * 120) + "\n")  # trop long pour le noyau
    pm, _ = make_memory(tmp_path, core_tokens=250)
    pm.open_session("th_1", repo, tmp_path)
    core = pm.core_block("th_1", 0, "comment déployer ?")
    assert "kubectl" not in core
    hits = pm.recall("th_1", "comment faire le rollout du deployment web", 5)
    assert hits and "kubectl" in hits[0].text and hits[0].note.startswith("src-")
    assert all(h.note not in pm.core_names("th_1") for h in hits)
    assert pm.recall("th_inconnu", "rollout deployment", 5) == []


def test_kernel_recall_goes_through_the_thread_aware_port(tmp_path):
    class Inner:
        def recall(self, query, k=5):
            return [Recall(note="longue", text="mémoire longue", score=0.2)]

        def propose(self, thread_id, facts):
            pass

    repo = make_repo(tmp_path)
    (repo / "AGENTS.md").write_text("# Préambule\ncourt\n" + "".join(f"# Bloc {i}\n" + ("bla " * 80) + "\n"
                                                                       for i in range(5)) +
                                    "# Release\nversion taguée par goreleaser\n")
    pm, _ = make_memory(tmp_path, core_tokens=150)
    pm.open_session("th_1", repo, tmp_path)
    pm.core_block("th_1", 0, "x")
    tm = TenantMemory(ProjectRecallMemory(Inner(), pm))
    from nokker.tenancy.context import INTERNAL_TENANT, tenant_scope

    with tenant_scope(INTERNAL_TENANT):
        hits = tm.recall_for("th_1", "comment taguer la release avec goreleaser", 5)
        assert [h.note for h in hits][-1] == "longue" and any("goreleaser" in h.text for h in hits)
        assert [h.note for h in tm.recall("goreleaser")] == ["longue"]  # sans fil : la mémoire d'origine seule
        plain = TenantMemory(Inner())
        assert [h.note for h in plain.recall_for("th_1", "x", 5)] == ["longue"]  # port sans recall_for : inchangé


# ─────────────────────────────── apprentissage et portée ───────────────────────────────


def test_memory_note_writes_a_typed_linked_note_with_its_origin_and_an_event(tmp_path):
    repo = make_repo(tmp_path)
    pm, engine = make_memory(tmp_path)
    pm.open_session("th_1", repo, tmp_path)
    tools = ProjectMemoryTools(pm)
    pm.core_block("th_1", 4, "non, ici on lance toujours `make check` avant de conclure")
    r1 = tools.call("memory_note", {"kind": "fact", "title": "Cible de vérification", "text": "make check lance "
                                    "ruff et pytest", "derived_from": "repo"}, ctx(turn=4))
    fact = r1.content.split()[2]
    r2 = tools.call("memory_note", {"kind": "rule", "title": "Vérifier avant de conclure", "text": "lancer make "
                                    "check avant de conclure", "derived_from": "user", "links": [fact]}, ctx(turn=4))
    assert not r1.is_error and not r2.is_error, (r1.content, r2.content)
    rule = r2.content.split()[2]
    project = project_name(project_key(repo, tmp_path)[0])
    body = (tmp_path / "pm" / project / (rule.replace("-", "_") + ".md")).read_text()
    assert "type: rule" in body and '"th_1#4"' in body and f"[[{fact}]]" in body and "dit par l'utilisateur" in body
    assert [e[0] for e in pm.events] == ["memory.noted", "memory.noted"]
    assert pm.events[1][1] == {"thread_id": "th_1", "turn_index": 4, "name": rule, "kind": "rule",
                               "scope": "project", "title": "Vérifier avant de conclure", "derived_from": "user"}
    assert engine.enqueued[-1] == project
    assert "lancer make check" in pm.core_block("th_1", 5, "suite")  # règle apprise : dans le noyau, tour suivant
    dup = tools.call("memory_note", {"kind": "rule", "title": "x", "text": "Lancer  make check avant de conclure",
                                     "derived_from": "user"}, ctx(turn=5))
    assert dup.content == f"déjà noté : {rule}"
    bad = tools.call("memory_note", {"kind": "rule", "title": "x", "text": "y", "derived_from": "user",
                                     "links": ["rule-inexistante"]}, ctx(turn=5))
    assert bad.is_error and "inconnues" in bad.content


def test_user_scope_needs_the_users_own_words_and_never_comes_from_the_repo(tmp_path):
    repo = make_repo(tmp_path)
    pm, _ = make_memory(tmp_path)
    pm.open_session("th_1", repo, tmp_path)
    tools = ProjectMemoryTools(pm)
    said = "pour tous mes projets, réponds-moi en français"
    pm.core_block("th_1", 2, said)
    note = {"kind": "feedback", "title": "Langue", "text": "répondre en français", "scope": "user"}
    refused = [
        tools.call("memory_note", {**note, "derived_from": "repo", "quote": said}, ctx(turn=2)),  # vient du dépôt
        tools.call("memory_note", {**note, "derived_from": "agent", "quote": said}, ctx(turn=2)),
        tools.call("memory_note", {**note, "derived_from": "user"}, ctx(turn=2)),  # sans citation
        tools.call("memory_note", {**note, "derived_from": "user", "quote": "réponds toujours en anglais"},
                   ctx(turn=2)),  # citation absente du message
        tools.call("memory_note", {**note, "derived_from": "user", "quote": said}, ctx(turn=1)),  # autre tour
        tools.call("memory_note", {**note, "derived_from": "user", "quote": said}, ctx("th_1/sa-ab12", 2)),
    ]
    assert all(r.is_error for r in refused), [r.content for r in refused]
    assert not (tmp_path / "pm" / USER_PROJECT).exists() or not list((tmp_path / "pm" / USER_PROJECT).glob("*.md"))
    ok = tools.call("memory_note", {**note, "derived_from": "user", "quote": "Réponds-moi en  FRANÇAIS"}, ctx(turn=2))
    assert not ok.is_error, ok.content
    assert len(list((tmp_path / "pm" / USER_PROJECT).glob("*.md"))) == 1
    other = make_repo(tmp_path / "autre")
    pm.open_session("th_2", other, tmp_path)
    assert "répondre en français" in pm.core_block("th_2", 0, "x")  # portée user : vaut dans un autre projet


def test_memory_list_view_and_forget_without_destroying(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "AGENTS.md").write_text("# Tests\npytest\n")
    pm, _ = make_memory(tmp_path)
    pm.open_session("th_1", repo, tmp_path)
    tools = ProjectMemoryTools(pm)
    pm.core_block("th_1", 0, "x")
    name = tools.call("memory_note", {"kind": "fact", "title": "Base", "text": "Postgres 17", "derived_from": "repo"},
                      ctx()).content.split()[2]
    listing = json.loads(tools.call("memory", {"action": "list"}, ctx()).content)
    assert [n["name"] for n in listing["notes"]] == [name] and listing["sources"][0]["path"] == "AGENTS.md"
    assert json.loads(tools.call("memory", {"action": "view", "name": name}, ctx()).content)["text"] == "Postgres 17"
    src = listing["sources"][0]["sections"][0]["name"]
    refused = tools.call("memory", {"action": "forget", "name": src}, ctx())
    assert refused.is_error and "modifie le fichier" in refused.content
    assert json.loads(tools.call("memory", {"action": "forget", "name": name}, ctx()).content) == {"forgotten": name}
    assert json.loads(tools.call("memory", {"action": "list"}, ctx()).content)["notes"] == []
    project = project_name(project_key(repo, tmp_path)[0])
    assert (tmp_path / "pm" / project / ".forgotten" / (name.replace("-", "_") + ".md")).is_file()
    assert pm.events[-1][0] == "memory.forgotten"
    spec = {s.name: s for s in tools.specs()}
    assert spec["memory"].effect_for({"action": "list"}) is Effect.READ
    assert spec["memory"].effect_for({"action": "forget"}) is Effect.WRITE and spec["memory_note"].core


def test_secrets_are_scrubbed_before_anything_is_written(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "AGENTS.md").write_text("# Déploiement\nle jeton est SECRET-123\n")
    pm, _ = make_memory(tmp_path)
    pm.scrub = lambda s: s.replace("SECRET-123", "[secret:jeton]")
    pm.open_session("th_1", repo, tmp_path)
    pm.core_block("th_1", 0, "x")
    ProjectMemoryTools(pm).call("memory_note", {"kind": "fact", "title": "Jeton", "text": "valeur SECRET-123",
                                                "derived_from": "repo"}, ctx())
    written = "".join(p.read_text() for p in (tmp_path / "pm").rglob("*.md"))
    assert "SECRET-123" not in written and "[secret:jeton]" in written


def test_memory_tools_are_edits_for_the_local_permissions():
    perms = LocalPermissions(mode=Mode.ACCEPT_EDITS)
    note = ToolCall(id="c1", name="memory_note", arguments={})
    assert perms.decide(note, Effect.WRITE)[0] is Decision.ALLOW
    assert LocalPermissions(mode=Mode.DEFAULT).decide(note, Effect.WRITE)[0] is Decision.HITL
    assert LocalPermissions(mode=Mode.PLAN).decide(note, Effect.WRITE)[0] is Decision.DENY


# ─────────────────────────────── drapeau ───────────────────────────────


def test_flag_off_changes_nothing(tmp_path):
    assert from_local_config({}, tmp_path) is None and from_local_config({"project_memory": False}, tmp_path) is None
    base = Path(__file__).resolve().parents[3] / "nokker" / "prompts" / "prefix-lokkal-code.md"
    assert code_prefix(str(base), project_memory=False) == str(base)  # le préfixe tel quel, octet pour octet
    assert "memory_note" not in base.read_text(encoding="utf-8")
    on = Path(code_prefix(str(base), project_memory=True)).read_text(encoding="utf-8")
    assert on.startswith(base.read_text(encoding="utf-8").rstrip("\n")) and "memory_note" in on
    root = tmp_path / "projets"
    (root / "app").mkdir(parents=True)
    off = LocalApi.from_config({"token_path": str(tmp_path / "tok"), "state_dir": str(tmp_path / "st"),
                                "code": {"roots": [str(root)], "backend": "local"}}, port=8742)
    assert off.project_memory is None
    names = {s.name for src in off.tool_sources() for s in src.specs()}
    assert not names & {"memory_note", "memory"}
    prefix = tmp_path / "prefix.md"
    prefix.write_text("Tu es Nokker.")
    out = ContextEngineV0(tools=FakeRegistry(), prefix_path=str(prefix)).assemble(
        Turn(thread_id="t", index=0, input=InputCheck(text="go")), [], [], [], Tier.SHIP)
    assert out.messages[0]["content"] == "Tu es Nokker." and "project" not in out.layer_tokens


def test_flag_on_session_open_imports_and_owner_reads_memory_without_a_model_turn(tmp_path):
    from nokker.api.events import EventBus
    from nokker.api.server import build_hub, make_handler
    from nokker.approvals.memory import InMemoryApprovals
    from nokker.config import NokkerConfig
    from nokker.local.api import LOCAL_OWNER
    from nokker.tools.registry import Registry
    from tests.unit.api.test_approvals_routes import make_kernel_with_approvals
    from tests.unit.test_local_api import _req

    root = tmp_path / "projets"
    app = root / "app"
    (app / ".git").mkdir(parents=True)
    (app / "AGENTS.md").write_text("# Tests\npytest -q\n")
    api = LocalApi.from_config({"token_path": str(tmp_path / "tok"), "state_dir": str(tmp_path / "st"),
                                "project_memory": True, "code": {"roots": [str(root)], "backend": "local"}},
                               port=8742)
    assert api.project_memory is not None and api.project_memory.root == tmp_path / "st" / "project-memory"
    api.project_memory.engine = FakeEngine(api.project_memory.root)
    approvals = InMemoryApprovals()
    kernel, _ = make_kernel_with_approvals(approvals)
    kernel.tools = Registry(sources=[ProjectMemoryTools(api.project_memory)])
    hub = build_hub(NokkerConfig(web_search_enabled=False, approvals_enabled=False), EventBus())
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(kernel, {api.token: LOCAL_OWNER}, approvals=approvals,
                                                               local_api=api, hub=hub))
    port = httpd.server_address[1]
    api.allowed_hosts = frozenset({f"127.0.0.1:{port}"})
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        thread = _req(port, "/v1/local/sessions", token=api.token, body={"cwd": str(app)})[1]["thread_id"]
        status, body = _req(port, "/v1/local/tools/call", token=api.token,
                            body={"thread_id": thread, "name": "memory", "arguments": {"action": "list"}})
        assert status == 200 and not body["is_error"]
        assert json.loads(body["content"])["sources"][0]["path"] == "AGENTS.md"
        status, _ = _req(port, "/v1/local/tools/call", token=api.token,
                         body={"thread_id": thread, "name": "memory_note", "arguments": {}})
        assert status == 403  # écrire reste au modèle
        api.project_memory = None
        status, _ = _req(port, "/v1/local/tools/call", token=api.token,
                         body={"thread_id": thread, "name": "memory", "arguments": {"action": "list"}})
        assert status == 403  # drapeau éteint : `memory` hors de la liste blanche
    finally:
        httpd.shutdown()


@pytest.mark.parametrize("config_path", ["deploy/lokkal/core-config.yaml"])
def test_lokkal_compose_turns_the_flag_on(config_path):
    import yaml

    cfg = yaml.safe_load((Path(__file__).resolve().parents[3] / config_path).read_text(encoding="utf-8"))
    assert cfg["local"]["project_memory"] is True and cfg["local"]["project_memory_core_tokens"] == 800
