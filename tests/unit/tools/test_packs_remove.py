"""Retirer un pack en un appel (`pack_remove`, nokker/tools/packs.py, docs/en/packs.md « Removing a pack »).

Retire les compétences du pack et ferme les cartes `agent_draft` encore ouvertes ; les agents DÉJÀ créés depuis le
pack ne sont jamais supprimés ni désarmés en silence : une carte `pack_agents` propose de les désarmer ou de les
supprimer, et seule l'action de l'humain le fait. Idempotent ; les effets qu'un pack a relevés restent relevés
(retirer ne baisse jamais un effet). L'agent créé depuis une carte de pack garde son origine (`origin`)."""
from __future__ import annotations

import json

import pytest

from nokker.core.ports import ToolContext
from nokker.core.turn import Effect
from nokker.tools import skills as sk
from nokker.tools.packs import PackToolSource, installed_effect_overrides
from nokker.tools.skills import SkillToolSource
from tests.unit.tools.test_packs import _bubbles_and_agents, safe_guard


@pytest.fixture(autouse=True)
def guard(monkeypatch):
    monkeypatch.setattr(sk, "_guard_module", lambda: safe_guard())


def _setup(tmp_path):
    from nokker.bubbles.tools import pack_removed, propose_agent_draft

    hub, agents = _bubbles_and_agents()
    thread = hub.create_thread("Packs", "alice")
    ctx = ToolContext(thread_id=thread.id, turn_index=0, channel="api", sender="alice")
    src = PackToolSource(home=tmp_path, propose=lambda a, c, extra: propose_agent_draft(hub, c, a, extra),
                         on_remove=lambda pack, ids, c: pack_removed(hub, c, pack, ids))
    return hub, agents, thread, ctx, src


def _drafts(hub, thread, include_closed=False):
    return [b for b in hub.bubbles.list_thread(thread.id, include_closed=include_closed) if b.kind == "agent_draft"]


def test_pack_remove_is_destructive():
    specs = {s.name: s.effect for s in PackToolSource(home=None).specs()}
    assert specs["pack_remove"] is Effect.DESTRUCTIVE


def test_remove_takes_skills_and_open_cards_in_one_call(tmp_path):
    hub, agents, thread, ctx, src = _setup(tmp_path)
    assert not src.call("pack_install", {"name": "mail"}, ctx).is_error
    assert len(_drafts(hub, thread)) == 2
    out = src.call("pack_remove", {"name": "mail"}, ctx)
    assert not out.is_error, out.content
    payload = json.loads(out.content)
    assert payload["removed"] == "mail" and sorted(payload["skills"]) == [
        "mail/calendar-scheduling", "mail/mail-morning-brief", "mail/mail-reply-drafts", "mail/mail-triage"]
    assert len(payload["drafts_closed"]) == 2 and payload["agents"] == [] and payload["card"] is None
    assert _drafts(hub, thread) == []  # cartes non activées : fermées
    assert SkillToolSource(home=tmp_path).list_skills()["skills"] == []
    listed = json.loads(src.call("pack_list", {}, ctx).content)
    assert not next(p for p in listed["packs"] if p["name"] == "mail")["installed"]


def test_remove_is_idempotent(tmp_path):
    _hub, _agents, _thread, ctx, src = _setup(tmp_path)
    assert not src.call("pack_install", {"name": "ops"}, ctx).is_error
    assert not src.call("pack_remove", {"name": "ops"}, ctx).is_error
    again = src.call("pack_remove", {"name": "ops"}, ctx)
    assert not again.is_error
    payload = json.loads(again.content)
    assert payload["skills"] == [] and payload["already_removed"] is True
    never = json.loads(src.call("pack_remove", {"name": "brand"}, ctx).content)  # jamais installé : rien à faire
    assert never["skills"] == [] and never["already_removed"] is True
    assert src.call("pack_remove", {"name": "../etc"}, ctx).is_error


def test_remove_leaves_the_users_own_skills(tmp_path):
    _hub, _agents, _thread, ctx, src = _setup(tmp_path)
    assert not src.call("pack_install", {"name": "mail"}, ctx).is_error
    mine = tmp_path / "skills" / "mail" / "my-signature"
    mine.mkdir(parents=True)
    (mine / "SKILL.md").write_text("---\nname: my-signature\ndescription: mine\n---\nmy own\n", encoding="utf-8")
    assert not src.call("pack_remove", {"name": "mail"}, ctx).is_error
    assert (mine / "SKILL.md").is_file()


def test_raised_effects_stay_after_removal(tmp_path):
    _hub, _agents, _thread, ctx, src = _setup(tmp_path)
    assert not src.call("pack_install", {"name": "social"}, ctx).is_error
    assert not src.call("pack_remove", {"name": "social"}, ctx).is_error
    raised = installed_effect_overrides(tmp_path, lambda n: Effect.WRITE)
    assert raised["integrationSchedulePostTool"] == "send_external"  # retirer ne baisse jamais un effet
    assert not src.call("pack_install", {"name": "social"}, ctx).is_error  # et se réinstalle


def test_created_agents_get_a_card_never_a_silent_deletion(tmp_path):
    hub, agents, thread, ctx, src = _setup(tmp_path)
    assert not src.call("pack_install", {"name": "video"}, ctx).is_error
    [draft] = _drafts(hub, thread)
    created = hub.action(draft.id, "alice", "activate")["agent"]
    assert created["origin"] == {"pack": "video", "template": "video-storyboard"}
    assert created["status"] == "armed"  # manuel, lecture seule : armé à l'activation
    payload = json.loads(src.call("pack_remove", {"name": "video"}, ctx).content)
    assert [a["id"] for a in payload["agents"]] == [created["id"]] and payload["agents"][0]["armed"] is True
    # l'agent est toujours là, toujours armé : seule la carte propose
    assert agents.get_agent(created["id"]).armed is True
    card = hub.bubbles.require(payload["card"])
    assert card.kind == "pack_agents" and card.state["pack"] == "video"
    assert [a["id"] for a in card.actions] == ["disarm", "delete", "keep"]
    # idempotent : la même carte, pas une seconde
    again = json.loads(src.call("pack_remove", {"name": "video"}, ctx).content)
    assert again["card"] == payload["card"]
    out = hub.action(card.id, "alice", "disarm")
    assert out["disarmed"] == [created["id"]] and agents.get_agent(created["id"]).armed is False
    assert hub.bubbles.require(card.id).closed_at is not None


def test_card_delete_and_keep(tmp_path):
    from nokker.agents.types import AgentNotFound

    hub, agents, thread, ctx, src = _setup(tmp_path)
    src.call("pack_install", {"name": "video"}, ctx)
    [draft] = _drafts(hub, thread)
    agent_id = hub.action(draft.id, "alice", "activate")["agent"]["id"]
    keep = json.loads(src.call("pack_remove", {"name": "video"}, ctx).content)["card"]
    assert hub.action(keep, "alice", "keep") == {"closed": True, "kept": [agent_id]}
    assert agents.get_agent(agent_id).armed is True
    card = json.loads(src.call("pack_remove", {"name": "video"}, ctx).content)["card"]
    assert card != keep  # l'ancienne est fermée : une nouvelle carte
    assert hub.action(card, "alice", "delete")["deleted"] == [agent_id]
    with pytest.raises(AgentNotFound):
        agents.get_agent(agent_id)


def test_card_only_touches_agents_still_from_that_pack(tmp_path):
    """Entre la carte et le clic, un agent supprimé à la main est sauté ; un agent d'un autre pack n'est jamais
    touché, même si l'état de la carte le nommait."""
    hub, agents, thread, ctx, src = _setup(tmp_path)
    src.call("pack_install", {"name": "video"}, ctx)
    [draft] = _drafts(hub, thread)
    agent_id = hub.action(draft.id, "alice", "activate")["agent"]["id"]
    other = agents.create_agent(name="mine", role="r", tools=["skill_view"])
    card = json.loads(src.call("pack_remove", {"name": "video"}, ctx).content)["card"]
    b = hub.bubbles.require(card)
    hub.bubbles.update(card, state={**b.state, "agents": [*b.state["agents"], {"id": other.id, "name": "mine"}]})
    agents.delete_agent(agent_id, "alice")
    out = hub.action(card, "alice", "delete")
    assert out["deleted"] == [] and agents.get_agent(other.id) is not None


def test_pack_agents_card_is_kernel_only(tmp_path):
    from nokker.bubbles.types import BubbleError

    hub, _agents, thread, _ctx, _src = _setup(tmp_path)
    with pytest.raises(BubbleError):
        hub.create_bubble(thread.id, "alice", kind="pack_agents", state={"pack": "video", "agents": []})


def test_remove_without_hub_lists_agents_for_the_model(tmp_path):
    """Sans bulles (pas de `on_remove`) : compétences retirées, et le résultat dit que les agents créés restent."""
    src = PackToolSource(home=tmp_path)
    ctx = ToolContext(thread_id="t", turn_index=0, channel="api", sender="alice")
    src.call("pack_install", {"name": "ops"}, ctx)
    payload = json.loads(src.call("pack_remove", {"name": "ops"}, ctx).content)
    assert payload["skills"] and payload["agents"] is None and "agents" in payload["note"]


# ─────────────────────────────── route locale ───────────────────────────────


def test_local_route_removes_and_posts_the_same_card(tmp_path):
    """`POST /v1/local/packs/{nom}/remove {thread_id?}` (jeton local) : même retrait et même carte que l'outil ; jamais par la
    passerelle ; fil inconnu → 404."""
    from nokker.local.api import LocalApi

    hub, agents, thread, ctx, src = _setup(tmp_path)
    root = tmp_path / "projets"
    root.mkdir()
    api = LocalApi.from_config({"token_path": str(tmp_path / "tok"), "code": {"roots": [str(root)],
                                                                             "backend": "local"}}, port=8742)
    api.packs = src
    api.packs_thread = lambda tid: hub.threads.get(tid) is not None
    auth = f"Bearer {api.token}"
    src.call("pack_install", {"name": "video"}, ctx)
    [draft] = _drafts(hub, thread)
    agent_id = hub.action(draft.id, "alice", "activate")["agent"]["id"]
    status, body = api.handle("GET", "/v1/local/packs", auth, {})
    assert status == 200 and next(p for p in body["packs"] if p["name"] == "video")["installed"] is True
    assert api.handle("POST", "/v1/local/packs/video/remove", auth, {"thread_id": "th_nope"})[0] == 404
    status, body = api.handle("POST", "/v1/local/packs/video/remove", auth, {"thread_id": thread.id})
    assert status == 200 and body["removed"] == "video" and [a["id"] for a in body["agents"]] == [agent_id]
    assert hub.bubbles.require(body["card"]).kind == "pack_agents" and agents.get_agent(agent_id).armed
    assert api.handle("POST", "/v1/local/packs/video/remove", auth, {})[1]["already_removed"] is True
    assert api.handle("POST", "/v1/local/packs/nope/remove", auth, {})[0] == 404
    assert api.handle("POST", "/v1/local/packs/video/remove", auth, {"thread_id": 3})[0] == 400
    assert api.handle("POST", "/v1/local/packs/video/remove", "Bearer faux", {})[0] == 401
