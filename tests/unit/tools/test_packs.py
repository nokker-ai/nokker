"""Packs d'agents (lot X16, nokker/tools/packs.py, docs/en/packs.md).

Toujours joués (CI unitaire, sans l'amont) : chaque pack se charge et se lit par `SkillToolSource` ; aucun pack ne
contient de nom, d'adresse, de chemin personnel ni de secret ; les `auto_approve` sont prudents ; installer copie
les compétences et PROPOSE les agents sans en créer ni en armer un ; garde en échec fermé (faux scanner).
`needs_upstream` : la VRAIE garde (scan amont, source « community ») accepte tous les packs."""
from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from nokker.core.ports import ToolContext
from nokker.core.turn import Effect, ToolResult
from nokker.tools import packs as pk
from nokker.tools import skills as sk
from nokker.tools.packs import (
    PACKS_ROOT,
    PRUDENT_WRITES,
    PackError,
    PackToolSource,
    auto_approve_problems,
    builtin_effects,
    installed_effect_overrides,
    load_all,
    load_pack,
    resolve_agent,
)
from nokker.tools.skills import SkillToolSource

UPSTREAM = importlib.util.find_spec("tools") is not None and importlib.util.find_spec("tools.skills_guard") is not None
needs_upstream = pytest.mark.skipif(not UPSTREAM, reason="amont (tools.skills_guard) non importable")
EXPECTED = {"mail", "social", "jobs", "brand", "video", "mcp-builder", "ops"}
DOC = Path(__file__).resolve().parents[3] / "docs" / "en" / "packs.md"
CTX = ToolContext(thread_id="t-packs", turn_index=0, channel="api", sender="alice")


def safe_guard(verdict="safe"):
    def scan(path, **kw):
        return SimpleNamespace(verdict=verdict, summary=verdict, trust_level="community", findings=[]), {}

    return SimpleNamespace(
        scan_skill_cached=scan, scan_skill=lambda p, **kw: scan(p)[0],
        should_allow_install=lambda r, force=False: (r.verdict == "safe", r.verdict),
        format_scan_report=lambda r: f"Verdict: {r.verdict.upper()}")


@pytest.fixture
def guard(monkeypatch):
    monkeypatch.setattr(sk, "_guard_module", lambda: safe_guard())


# ─────────────────────────────── chargement ───────────────────────────────


def test_every_pack_loads():
    packs, errors = load_all()
    assert errors == {}
    assert {p.name for p in packs} == EXPECTED
    for p in packs:
        assert len(p.skills) >= 3, p.name
        assert p.agents, p.name
        assert all(f == p.skills[0][0] for f, *_ in p.skills), "une famille par pack"


def test_pack_skills_read_by_the_existing_skill_source(guard):
    """Le format est celui que lit déjà `SkillToolSource` : index, description, corps, fichiers liés."""
    for p in load_all()[0]:
        src = SkillToolSource(roots=[p.path / "skills"], home=p.path / "_unused_home")
        listed = {s["name"]: s for s in src.list_skills()["skills"]}
        assert set(listed) == {n for _f, n, _d, _p in p.skills}
        for _f, name, desc, _d in p.skills:
            assert listed[name]["description"] == desc and len(desc) <= 60
            view, err = src.view(name)
            assert not err and len(view["content"]) > 200


def test_video_pack_declares_gpu_for_placement():
    video = load_pack(PACKS_ROOT / "video")
    assert video.needs == {"gpu": True}
    assert all({**video.needs, **t.needs}.get("gpu") for t in video.agents)


def test_mcp_builder_templates_are_linked_files(guard):
    p = load_pack(PACKS_ROOT / "mcp-builder")
    src = SkillToolSource(roots=[p.path / "skills"], home=p.path / "_unused_home")
    view, _ = src.view("mcp-scaffold")
    assert {"templates/server.py", "templates/test_server.py", "templates/smoke_client.py",
            "templates/requirements.txt"} <= set(view["linked_files"])


# ─────────────────────────────── garde ───────────────────────────────


@needs_upstream
def test_real_guard_accepts_every_pack():
    from tools import skills_guard as g

    for p in load_all()[0]:
        targets = [d for _f, _n, _d, d in p.skills] + [p.path / "pack.yaml"] + [t.path for t in p.agents]
        for path in targets:
            result = g.scan_skill(path, source="community")
            allowed, _reason = g.should_allow_install(result)
            assert result.verdict == "safe" and allowed is True, (path, g.format_scan_report(result))


def test_install_refused_without_scanner(monkeypatch, tmp_path):
    monkeypatch.setattr(sk, "_guard_module", lambda: None)
    out = PackToolSource(home=tmp_path).call("pack_install", {"name": "mail"}, CTX)
    assert out.is_error and "scanner" in out.content
    assert not (tmp_path / "skills").exists()


def test_install_refused_when_a_skill_is_flagged(monkeypatch, tmp_path):
    monkeypatch.setattr(sk, "_guard_module", lambda: safe_guard("caution"))
    proposed = []
    out = PackToolSource(home=tmp_path, propose=lambda *a: proposed.append(a)).call(
        "pack_install", {"name": "ops"}, CTX)
    assert out.is_error and "REFUSÉE" in out.content
    assert not (tmp_path / "skills").exists() and proposed == []


def test_pack_tools_effects():
    specs = {s.name: s.effect for s in PackToolSource(home=Path("/nonexistent-home")).specs()}
    assert specs == {"pack_list": Effect.READ, "pack_install": Effect.DESTRUCTIVE, "pack_remove": Effect.DESTRUCTIVE}


# ─────────────────────────────── contenu neutre ───────────────────────────────

# Noms interdits dans les packs, au mot et sans casse. Les noms publics (produits, dépendances, hébergeurs) sont en
# clair ici ; les noms internes vivent dans tests/guard-words.txt, que l'export public retire (scripts/public-tree.sh
# refuse alors tout export qui en contient un) : la garde publique ne publie pas ce qu'elle protège.
PUBLIC_WORDS = ("hermes", "sokkan", "ninabot", "nina", "corthexis", "openclaw", "tailscale", "exoscale", "ovh",
                "openbao")
PRIVATE_WORDS_FILE = Path(__file__).resolve().parents[2] / "guard-words.txt"


def _private_words() -> tuple[str, ...]:
    if not PRIVATE_WORDS_FILE.is_file():
        return ()
    lines = PRIVATE_WORDS_FILE.read_text(encoding="utf-8").splitlines()
    return tuple(w.strip() for w in lines if w.strip() and not w.lstrip().startswith("#"))


def words_re(words) -> re.Pattern:
    return re.compile(r"(?i)\b(" + "|".join(re.escape(w) for w in words) + r")\b")


FORBIDDEN_WORDS = PUBLIC_WORDS + _private_words()
_WORDS = words_re(FORBIDDEN_WORDS)
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z0-9.-]+")
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_HOME = re.compile(r"(?i)(?:/root\b|/home/|/Users/|[A-Z]:\\|~/[A-Za-z])")
_ABS = re.compile(r"(?<![\w.:/~<>{}-])/[A-Za-z][\w.-]*(?:/[\w.<>-]+)*")
# `/v1/…` : routes publiques de l'API du noyau (contrat documenté), pas un chemin de machine.
ALLOWED_ABS = ("/var/log", "/var/cache", "/var/lib/docker", "/etc/letsencrypt", "/dev/null", "/mcp", "/v1/")
_URL = re.compile(r"https?://([^\s\"'`)]+)")
_KEYLIKE = re.compile(r"(?i)\b(?:sk-[a-z0-9]{8,}|ghp_[a-z0-9]{8,}|xox[bap]-|akia[0-9a-z]{12,}|whsec_[a-z0-9]{6,}|"
                      r"-----BEGIN [A-Z ]*PRIVATE KEY)")
_SECRET_REF = re.compile(r"secret:([^\s\"'`,)}\]]*)")


def problems_in(text: str, words: re.Pattern = _WORDS) -> list[str]:
    out = [f"mot interdit {m.group(0)!r}" for m in words.finditer(text)]
    out += [f"adresse e-mail {m.group(0)!r}" for m in _EMAIL.finditer(text)]
    out += [f"IP {m.group(0)!r}" for m in _IPV4.finditer(text) if m.group(0) != "127.0.0.1"]
    out += [f"chemin personnel {m.group(0)!r}" for m in _HOME.finditer(text)]
    out += [f"chemin absolu {m.group(0)!r}" for m in _ABS.finditer(text)
            if not m.group(0).startswith(ALLOWED_ABS)]
    for m in _URL.finditer(text):
        host = m.group(1)
        if not (host.startswith(("<", "127.0.0.1", "localhost")) or re.match(r"([\w-]+\.)*example\.com\b", host)):
            out.append(f"adresse {m.group(0)!r}")
    out += [f"clé {m.group(0)[:12]!r}" for m in _KEYLIKE.finditer(text)]
    for m in _SECRET_REF.finditer(text):
        if not re.fullmatch(r"(?:<NAME>|[A-Z][A-Z0-9_]{2,63})", m.group(1)):
            out.append(f"référence de secret qui n'est pas un NOM {m.group(0)!r}")
    return out


def pack_files() -> list[Path]:
    return sorted(p for p in PACKS_ROOT.rglob("*") if p.is_file())


def test_checker_catches_planted_content():
    """Le grep n'est pas vide de sens : chaque famille de fuite est attrapée. Le mot planté est NEUTRE et construit
    à l'exécution : ajouté à la liste, il est attrapé au mot et quelle que soit la casse."""
    word = "Zorg" + "lub"
    words = words_re(FORBIDDEN_WORDS + (word,))
    planted = (f"Ask {word} at jane.doe@corp.test, see /root/notes, ssh 100.64.1.2, token secret:sk-live-abc,"
               f" https://app.realcompany.io/x, key ghp_abcdefgh1234, {word.upper()}, Sokkan")
    found = " | ".join(problems_in(planted, words))
    for needle in (word, word.upper(), "Sokkan", "jane.doe@", "/root", "100.64.1.2", "secret:sk-live", "realcompany",
                   "ghp_"):
        assert needle in found, needle
    assert not problems_in(f"{word}s x{word.lower()}", words)  # bornes de mot, comme \b…\b
    assert not any(word in p for p in problems_in(planted))  # vu grâce à l'ajout seul


def test_private_words_stay_out_of_the_public_tree():
    """La liste des noms internes est un fichier à part (aucun de ses mots n'est écrit dans ce test), que l'export
    public retire puis utilise pour refuser tout arbre qui en contient un."""
    tree = Path(__file__).resolve().parents[3] / "scripts" / "public-tree.sh"
    if tree.is_file():  # dépôt privé : scripts/ ne part pas dans l'export
        assert "rm -f tests/guard-words.txt" in tree.read_text(encoding="utf-8")
    if PRIVATE_WORDS_FILE.is_file():
        assert len(_private_words()) >= 10


def test_no_forbidden_name_address_path_or_secret_in_packs():
    files = pack_files()
    assert len(files) > 30
    bad = {}
    for f in files:
        text = f.read_text(encoding="utf-8")
        found = problems_in(text)
        if found:
            bad[str(f.relative_to(PACKS_ROOT))] = found
    assert bad == {}


def test_public_doc_is_neutral_too():
    assert DOC.is_file()
    assert problems_in(DOC.read_text(encoding="utf-8")) == []


def test_secrets_are_names_everywhere():
    for p in load_all()[0]:
        for role in p.mcp.values():
            assert all(re.fullmatch(r"[A-Z][A-Z0-9_]+", s) for s in role.secrets)
        for t in p.agents:
            args, _ = resolve_agent(p, t)
            assert all(re.fullmatch(r"[A-Z][A-Z0-9_]+", s) for s in args["secrets"])


# ─────────────────────────────── prudence ───────────────────────────────

NEVER_LIFTED = {Effect.SEND_EXTERNAL, Effect.SEND_BULK, Effect.DESTRUCTIVE, Effect.SPEND, Effect.SECRET}


def test_auto_approve_of_every_template_is_prudent():
    from nokker.qa.permissions import Rule

    known = builtin_effects()
    for p in load_all()[0]:
        for t in p.agents:
            args, effects = resolve_agent(p, t)
            assert args["budget"] and (args["budget"]["max_calls"] or args["budget"]["max_cost"])
            assert {Rule.parse(r).tool for r in args["auto_approve"]} <= set(args["tools"])
            for rule in args["auto_approve"]:
                tool = Rule.parse(rule).tool
                eff = effects[tool]
                assert eff not in NEVER_LIFTED, (p.name, t.id, rule)
                assert eff is Effect.READ or tool in PRUDENT_WRITES, (p.name, t.id, rule)
                assert not tool.startswith(("ssh_", "terminal", "process_", "mcp_server_add", "mcp_server_remove",
                                            "pack_install", "skill_install", "skill_remove", "mail_send")), rule
                # jamais plus bas que le noyau
                if tool in known:
                    assert pk.RANK[eff] >= pk.RANK[known[tool]]
            if args["trigger"]["kind"] == "event":
                assert all(effects[Rule.parse(r).tool] is Effect.READ for r in args["auto_approve"])
            assert "placement" not in args


def _with_template(name: str, **changes):
    p = load_pack(PACKS_ROOT / name)
    t = p.agents[0]
    t.fields = {**t.fields, **changes}
    return p, t


@pytest.mark.parametrize("pack,changes,needle", [
    ("mail", {"tools": ["skill_view", "mail_send"], "auto_approve": ["mail_send"]}, "send_external"),
    ("mail", {"tools": ["skill_view", "mail_move"], "auto_approve": ["mail_move"]}, "write"),
    ("ops", {"trigger": {"kind": "manual"}, "tools": ["skill_view", "ssh_run"], "auto_approve": ["ssh_run(*:df -h)"]},
     "destructive"),
    ("ops", {"tools": ["skill_view", "ssh_run"], "auto_approve": ["ssh_run(*:df -h)"]}, "événement"),
    ("mcp-builder", {"tools": ["mcp_server_add"], "auto_approve": ["mcp_server_add"]}, "destructive"),
    ("video", {"tools": ["skill_view", "terminal"], "auto_approve": ["terminal(ffmpeg *)"]}, "destructive"),
    ("social", {"auto_approve": ["scheduler.schedule_post"]}, "hors de la liste blanche"),
    ("social", {"tools": ["scheduler.schedule_post"], "auto_approve": ["scheduler.schedule_post"]}, "send_external"),
    ("brand", {"tools": ["images.generate_image"], "auto_approve": ["images.generate_image"]}, "spend"),
    ("mail", {"trigger": {"kind": "event", "event": "webhook:new-mail"}, "tools": ["mail_draft"],
              "auto_approve": ["mail_draft"]}, "événement"),
    ("mail", {"tools": ["calendar_delete"]}, "non déclaré"),
    ("mail", {"budget": None}, "budget requis"),
    ("mail", {"secrets": ["sk-abcdefabcdefabcdefabcdef"]}, "valeur"),
    ("mail", {"trigger": {"kind": "event", "event": "mail:is:unread"}, "tools": ["mail_draft"],
              "auto_approve": ["mail_draft"]}, "événement"),
    ("mail", {"trigger": {"kind": "event", "event": "mail:after:2026/01/01"}}, "after:"),
])
def test_imprudent_templates_are_refused(pack, changes, needle):
    p, t = _with_template(pack, **changes)
    with pytest.raises(PackError, match=re.escape(needle)):
        resolve_agent(p, t)


def test_a_mail_trigger_template_is_checked_here_and_gated_at_the_card():
    """Un modèle `mail:<filtre>` : la syntaxe du filtre est vérifiée ici (le pack ne sait pas si l'instance a la relève
    du courrier) ; c'est la carte `agent_draft` et la création qui refusent quand la relève n'est pas branchée."""
    p, t = _with_template("mail", trigger={"kind": "event", "event": "mail:from:billing@example.com   is:unread"},
                          auto_approve=[])
    args, _ = resolve_agent(p, t)
    assert args["trigger"] == {"kind": "event", "event": "mail:from:billing@example.com is:unread"}


def test_binding_cannot_lower_an_effect():
    """Lier un outil MCP « lecture » à un outil du noyau qui envoie : l'effet retenu est le plus prudent."""
    p = load_pack(PACKS_ROOT / "social")
    t = next(a for a in p.agents if a.id == "linkedin-watch")
    with pytest.raises(PackError, match="send_external"):
        resolve_agent(p, t, {"linkedin.list_comments": "mail_send"})


def test_auto_approve_problems_direct():
    args = {"trigger": {"kind": "schedule"}, "auto_approve": ["a", "b(x*)", "c", "mail_draft"]}
    eff = {"a": Effect.READ, "b": Effect.WRITE, "c": Effect.SEND_EXTERNAL, "mail_draft": Effect.WRITE}
    out = auto_approve_problems(args, eff)
    assert len(out) == 2 and "b(x*)" in out[0] and "c" in out[1]


def test_bind_renames_tools_and_secrets():
    p = load_pack(PACKS_ROOT / "social")
    t = next(a for a in p.agents if a.id == "weekly-post-plan")
    args, effects = resolve_agent(p, t, {"scheduler.schedule_post": "plan_post", "secret:SCHEDULER_API_KEY": "MY_KEY"})
    assert "plan_post" in args["tools"] and effects["plan_post"] is Effect.SEND_EXTERNAL
    assert "plan_post" not in args["auto_approve"] and args["secrets"] == ["MY_KEY"]
    args, _ = resolve_agent(p, t, {"secret:SCHEDULER_API_KEY": ""})
    assert args["secrets"] == []


# ─────────────────────────────── installation : proposer sans armer ───────────────────────────────


def _bubbles_and_agents():
    from nokker.agents.hub import AgentsHub
    from nokker.agents.memory import InMemoryAgents
    from nokker.bubbles.hub import BubbleHub
    from nokker.bubbles.memory import InMemoryBubbles
    from nokker.threads.memory import InMemoryThreads

    known = builtin_effects()
    hub = BubbleHub(threads=InMemoryThreads(), bubbles=InMemoryBubbles(), publish=lambda *a: None,
                    known_users=frozenset({"alice"}), spawn=lambda fn: fn())
    agents = AgentsHub(store=InMemoryAgents(), publish=lambda *a: None, rule_effect=known.get,
                       engine=_InertEngine())  # Lokkal a un moteur ; sans lui, les agents programmés sont refusés
    hub.agents_hub = agents
    return hub, agents


class _InertEngine:
    def drop_card(self, approval_id, why):
        pass


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_install_copies_skills_and_proposes_agents_without_arming(guard, tmp_path, name):
    from nokker.bubbles.tools import propose_agent_draft

    hub, agents = _bubbles_and_agents()
    thread = hub.create_thread("Packs", "alice")
    ctx = ToolContext(thread_id=thread.id, turn_index=0, channel="api", sender="alice")
    src = PackToolSource(home=tmp_path, propose=lambda a, c, extra: propose_agent_draft(hub, c, a, extra))
    out = src.call("pack_install", {"name": name}, ctx)
    assert not out.is_error, out.content
    payload = json.loads(out.content)
    pack = load_pack(PACKS_ROOT / name)
    # compétences posées dans la racine utilisateur, lues par la source des compétences (rescannées)
    user = SkillToolSource(home=tmp_path)
    assert {s["name"] for s in user.list_skills()["skills"]} == {n for _f, n, _d, _p in pack.skills}
    # des cartes, aucun agent
    drafts = [b for b in hub.bubbles.list_thread(thread.id) if b.kind == "agent_draft"]
    assert len(drafts) == len(pack.agents) == len(payload["proposed"])
    assert payload["armed"] == [] and agents.list_agents() == []
    for b in drafts:
        assert b.state["pack"] == name and "needs" in b.state
        assert b.actions == [{"id": "activate", "label": "Activer", "primary": True}]
    # l'humain active : rien de planifié ni d'écrivant ne s'arme sans un second geste (carte forte)
    for b in drafts:
        agent = hub.action(b.id, "alice", "activate")["agent"]
        writes = agents.write_rules(agent["auto_approve"])
        if agent["trigger"]["kind"] != "manual" or writes:
            assert agent["status"] == "paused", (b.state["template"], agent["status"])
        else:
            assert writes == []


def test_install_without_proposer_returns_the_cards_to_propose(guard, tmp_path):
    out = PackToolSource(home=tmp_path).call("pack_install", {"name": "mail"}, CTX)
    payload = json.loads(out.content)
    assert payload["proposed"] == [] and len(payload["to_propose"]) == 2
    assert all(set(d["agent_draft"]) >= {"name", "role", "tools", "auto_approve", "trigger", "budget"}
               for d in payload["to_propose"])


def test_proposer_failure_keeps_install_and_reports(guard, tmp_path):
    def boom(*a):
        raise RuntimeError("fil inconnu")

    out = PackToolSource(home=tmp_path, propose=boom).call("pack_install", {"name": "ops"}, CTX)
    payload = json.loads(out.content)
    assert payload["installed"] == "ops" and payload["to_propose"][0]["error"].startswith("RuntimeError")


def test_install_never_overwrites_a_user_skill(guard, tmp_path):
    mine = tmp_path / "skills" / "mail" / "mail-triage"
    mine.mkdir(parents=True)
    (mine / "SKILL.md").write_text("---\nname: mail-triage\ndescription: mine\n---\nmy own\n", encoding="utf-8")
    out = PackToolSource(home=tmp_path).call("pack_install", {"name": "mail"}, CTX)
    assert out.is_error and "existe déjà" in out.content
    assert (mine / "SKILL.md").read_text(encoding="utf-8").endswith("my own\n")
    assert not (tmp_path / "skills" / "mail" / "mail-reply-drafts").exists()  # rien copié avant le refus


def test_reinstall_updates_its_own_skills(guard, tmp_path):
    src = PackToolSource(home=tmp_path)
    assert not src.call("pack_install", {"name": "ops"}, CTX).is_error
    stale = tmp_path / "skills" / "ops" / "ops-basics" / "SKILL.md"
    stale.write_text("old", encoding="utf-8")
    assert not src.call("pack_install", {"name": "ops"}, CTX).is_error
    assert stale.read_text(encoding="utf-8").startswith("---\nname: ops-basics")
    assert not any(p.name.startswith(".") and p.is_dir() for p in (tmp_path / "skills" / "ops").iterdir())


def test_pack_list_and_detail(guard, tmp_path):
    src = PackToolSource(home=tmp_path)
    listed = json.loads(src.call("pack_list", {}, CTX).content)
    assert {p["name"] for p in listed["packs"]} == EXPECTED and not any(p["installed"] for p in listed["packs"])
    detail = json.loads(src.call("pack_list", {"name": "social"}, CTX).content)
    roles = {r["role"]: r for r in detail["mcp"]}
    assert roles["scheduler"]["tools"]["schedule_post"] == {"default": "integrationSchedulePostTool",
                                                            "effect": "send_external"}
    assert "Postiz" in " ".join(roles["scheduler"]["examples"])
    assert src.call("pack_list", {"name": "../etc"}, CTX).is_error


def test_bad_bind_keys_refused(guard, tmp_path):
    out = PackToolSource(home=tmp_path).call("pack_install", {"name": "social", "bind": {"rm": "x"}}, CTX)
    assert out.is_error and "bind" in out.content


def test_installed_effects_only_raise(guard, tmp_path):
    src = PackToolSource(home=tmp_path)
    bad = src.call("pack_install", {"name": "social", "bind": {"linkedin.list_posts": "mail_send"}}, CTX)
    assert bad.is_error and "send_external" in bad.content  # linkedin-watch le lèverait : pack refusé entier
    assert not (tmp_path / "skills").exists()
    assert not src.call("pack_install", {"name": "social"}, CTX).is_error
    current = {"integrationSchedulePostTool": Effect.WRITE, "postsListTool": Effect.WRITE}
    raised = installed_effect_overrides(tmp_path, lambda n: current.get(n, Effect.WRITE))
    assert raised["integrationSchedulePostTool"] == "send_external"
    assert raised["linkedin_reply_comment"] == "send_external"
    assert "postsListTool" not in raised  # déclaré « read » : jamais plus bas que WRITE du noyau
    assert installed_effect_overrides(tmp_path, lambda n: Effect.DESTRUCTIVE) == {}


def test_invalid_pack_is_refused_whole(tmp_path):
    bad = tmp_path / "bad"
    (bad / "skills" / "x" / "one").mkdir(parents=True)
    (bad / "agents").mkdir()
    (bad / "pack.yaml").write_text("name: bad\nversion: 1\ntitle: Bad\ndescription: d\ntools: [skill_view]\n",
                                   encoding="utf-8")
    (bad / "skills" / "x" / "one" / "SKILL.md").write_text(
        "---\nname: one\ndescription: d\n---\n" + "x" * 300, encoding="utf-8")
    (bad / "agents" / "a.yaml").write_text(
        "id: a\nname: A\nrole: r\ninstructions: " + "y" * 100 + "\ntools: [skill_view]\nauto_approve: [skill_view]\n",
        encoding="utf-8")
    packs, errors = load_all(tmp_path)
    assert packs == [] and "budget requis" in errors["bad"]


def test_tool_result_shape_of_proposer_is_used(guard, tmp_path):
    seen = []

    def propose(args, ctx, extra):
        seen.append((args["name"], extra["template"]))
        return ToolResult(call_id="", name="agent_draft", content=json.dumps({"bubble_id": "bu_1"}))

    out = json.loads(PackToolSource(home=tmp_path, propose=propose).call("pack_install", {"name": "video"},
                                                                          CTX).content)
    assert out["proposed"] == [{"template": "video-storyboard", "bubble_id": "bu_1"}]
    assert seen == [("Video storyboard", "video-storyboard")]


def test_startup_merges_added_servers_and_raises_effects(guard, tmp_path):
    """Démarrage de Lokkal (`serve`) : serveurs ajoutés + effets des packs installés, jamais plus bas."""
    from nokker.config import NokkerConfig
    from nokker.core.ports import ToolSpec
    from nokker.tools.effects import EffectsTable
    from nokker.tools.mcp_config import FILE_NAME
    from nokker.tools.packs import with_user_extensions

    assert not PackToolSource(home=tmp_path).call("pack_install", {"name": "social"}, CTX).is_error
    (tmp_path / FILE_NAME).write_text(
        "servers:\n  - {name: scheduler, url: 'https://s.example.com/mcp', token: 'secret:SCHEDULER_API_KEY'}\n",
        encoding="utf-8")
    cfg = NokkerConfig(mcp_tool_servers=[{"name": "memory", "url": "https://m.example.com/mcp"}],
                       effect_overrides={"linkedin_reply_comment": "destructive"})
    out = with_user_extensions(cfg, tmp_path)
    assert [s["name"] for s in out.mcp_tool_servers] == ["memory", "scheduler"]
    table = EffectsTable.load(out.effect_overrides)
    assert table.effect_for("integrationSchedulePostTool") is Effect.SEND_EXTERNAL
    assert table.effect_for("linkedin_reply_comment") is Effect.DESTRUCTIVE  # la config, plus stricte, reste
    assert table.effect_for("postsListTool") is Effect.WRITE  # « read » du pack ne baisse rien
    spec = table.annotate(ToolSpec(name="integrationSchedulePostTool", description="", parameters={}))
    assert spec.effect is Effect.SEND_EXTERNAL
    # rien d'installé : configuration inchangée
    assert with_user_extensions(cfg, tmp_path / "empty").mcp_tool_servers == cfg.mcp_tool_servers


def test_pack_tools_are_indexed_in_their_family():
    from nokker.tools.select import compact_index

    index = compact_index(["pack_list", "pack_install", "mcp_server_add", "skill_view"])
    assert "Packs : pack_list, pack_install" in index and "Serveurs MCP : mcp_server_add" in index


@needs_upstream
@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_real_guard_install_then_skills_visible_after_rescan(tmp_path, name):
    """Chemin complet avec la vraie garde : scan à l'installation, puis rescan par `SkillToolSource` (racine
    utilisateur, non fournie) : toutes les compétences restent visibles, sans avertissement."""
    out = PackToolSource(home=tmp_path).call("pack_install", {"name": name}, CTX)
    assert not out.is_error, out.content
    listed = SkillToolSource(home=tmp_path).list_skills()
    pack = load_pack(PACKS_ROOT / name)
    assert {s["name"] for s in listed["skills"]} == {n for _f, n, _d, _p in pack.skills}
    assert not any("warning" in s for s in listed["skills"]) and "note" not in listed
