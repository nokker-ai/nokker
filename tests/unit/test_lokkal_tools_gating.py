"""`lokkal_session` et `lokkal_handoff` n'existent que pour un compte qui a au moins une machine liée et non révoquée
(revue du train du 2.11, défaut 2 : tous les testeurs voyaient ces deux outils, même sans machine ; l'index des outils
du message système changeait pour tout le monde et `lokkal_session` faisait signer une carte forte pour rien)."""
from __future__ import annotations

from nokker.cloud.handoff import HandoffService, HandoffTools
from nokker.cloud.link import LinkService, Signer
from nokker.cloud.lokkal_session import LokkalSessionTools
from nokker.core.ports import ToolContext
from nokker.tenancy.context import tenant_scope
from nokker.tenancy.resources import TenantRegistry
from nokker.tools.registry import Registry, builtin_core_source

LOKKAL = {"lokkal_session", "lokkal_handoff"}


def _setup():
    links, signer = LinkService(), Signer.generate()
    tools = TenantRegistry(internal=Registry(sources=[builtin_core_source()]), tester_sources=[
        builtin_core_source(),
        LokkalSessionTools(links=links, signer=signer),
        HandoffTools(service=HandoffService(links=links, signer=signer),
                     tenant_of=lambda acc: {"us_pierre": "tn_pierre", "us_jean": "tn_jean"}.get(acc))])
    links.on_change = tools.forget  # comme `serve` : un lien ou une révocation reconstruit le jeu du locataire
    return links, tools


def _names(tools, tenant):
    with tenant_scope(tenant):
        return {n for n, _ in tools.index()} | {s.name for s in tools.core()}


def _link(links, account="us_pierre", tenant="tn_pierre"):
    d = links.device("atelier")
    links.approve(d["user_code"], account, tenant)
    return links.token(d["device_code"])["machine_token"]


def test_no_lokkal_tool_without_a_linked_machine():
    links, tools = _setup()
    assert not _names(tools, "tn_pierre") & LOKKAL
    with tenant_scope("tn_pierre"):
        r = tools.call("lokkal_session", {"cwd": "/x", "task": "t"},
                       ToolContext(thread_id="t", turn_index=0, channel="api", sender="us_pierre"))
    assert r.is_error and "outil inconnu" in r.content  # pas de carte forte à signer pour rien


def test_tools_appear_once_linked_and_vanish_once_revoked():
    links, tools = _setup()
    assert not _names(tools, "tn_pierre") & LOKKAL  # mis en cache sans les outils…
    d = links.device("atelier")
    links.approve(d["user_code"], "us_pierre", "tn_pierre")
    assert not _names(tools, "tn_pierre") & LOKKAL  # approuvée mais jeton pas encore relevé : pas encore liée
    token = links.token(d["device_code"])["machine_token"]
    assert _names(tools, "tn_pierre") >= LOKKAL  # … puis reconstruit au lien
    assert not _names(tools, "tn_jean") & LOKKAL  # un autre compte ne les voit pas
    assert links.revoke(token)
    assert not _names(tools, "tn_pierre") & LOKKAL


def test_revoking_one_of_two_machines_keeps_the_tools():
    links, tools = _setup()
    _link(links)
    second = _link(links)
    links.revoke(second)
    assert _names(tools, "tn_pierre") >= LOKKAL
    m = links.machines_of("us_pierre")[0]
    assert links.revoke_machine("us_pierre", m.id)
    assert not _names(tools, "tn_pierre") & LOKKAL


def test_internal_tenant_registry_is_untouched():
    links, tools = _setup()
    _link(links, "us_alice", "tn_ninabot")
    assert not _names(tools, "tn_ninabot") & LOKKAL  # l'interne a son propre registre, sans ces sources
