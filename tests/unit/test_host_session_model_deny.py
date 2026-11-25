"""`model` d'une ouverture de session (palier de la config), noms de `deny_tools` (`<serveur>__<outil>` / nom nu,
`unknown_denied`), `delegate_tiers` et `read_tools` de la config refusés au démarrage s'ils sont inconnus."""
from __future__ import annotations

import pytest

from nokker.api.server import config_tier_provider, exposed_tool_names
from nokker.byok.context import ByokRoute
from nokker.config import NokkerConfig, TierConfig, load_config
from nokker.core.ports import ToolSpec
from nokker.core.turn import Effect, Tier, ToolResult
from nokker.tenancy.resources import TenantRegistry
from nokker.tools.mcp_config import McpConfigError, config_read_tools
from nokker.tools.registry import Registry, StaticToolSource
from tests.unit.test_local_api import _api


def _open(api, tmp_path, **extra):
    return api.handle("POST", "/v1/local/sessions", f"Bearer {api.token}",
                      {"cwd": str(tmp_path / "projets" / "app"), **extra})


def _with_tiers(api, names=("ship", "deep", "atlas")):
    built = []

    def resolve(name):
        built.append(name)
        return ByokRoute(name=name, provider=object(), own_key=False)

    api.tier_names = lambda: list(names)
    api.resolve_tier = resolve
    return built


def test_model_picks_a_config_tier_for_the_session(tmp_path):
    api = _api(tmp_path)
    _with_tiers(api)
    status, body = _open(api, tmp_path, model="atlas")
    assert status == 201 and body["model"] == "atlas"
    tid = body["thread_id"]
    assert api.route_for_thread(tid).name == "atlas" and api.route_for_thread(tid + "/sa-1").name == "atlas"
    row = api.handle("GET", "/v1/local/sessions", f"Bearer {api.token}", {})[1]["sessions"][0]
    assert row["model"] == "atlas"


def test_unknown_model_is_400_with_the_valid_names_and_opens_nothing(tmp_path):
    api = _api(tmp_path)
    _with_tiers(api)
    for bad in ("inconnu", 3, ["ship"], ""):
        status, body = _open(api, tmp_path, model=bad)
        assert status == 400 and body["error"]["code"] == "unknown_model", bad
        assert body["error"]["valid"] == ["ship", "deep", "atlas"] and "ship, deep, atlas" in body["error"]["message"]
    assert api.code.sessions() == {}
    api.tier_names = api.resolve_tier = None  # noyau sans résolveur : refusé aussi
    assert _open(api, tmp_path, model="ship")[0] == 400


def test_default_is_unchanged_without_model(tmp_path):
    api = _api(tmp_path)
    built = _with_tiers(api)
    status, body = _open(api, tmp_path)
    assert status == 201 and "model" not in body and "unknown_denied" not in body
    assert api.route_for_thread(body["thread_id"]) is None and built == []


def _registry():
    def src(server, names):
        def spec(n):
            return ToolSpec(name=n, description=n, parameters={"type": "object", "properties": {}},
                            effect=Effect.READ, source=f"mcp:{server}" if server else "builtin")

        return StaticToolSource(entries=[(spec(n), (lambda a, c, n=n: ToolResult(call_id="", name=n, content="ok")))
                                         for n in names])

    return TenantRegistry(internal=Registry(sources=[src(None, ["read_file", "terminal"]), src("docs", ["lookup"])]),
                          tester_sources=[])


class _K:
    def __init__(self):
        self.tools = _registry()


def test_exposed_names_follow_the_convention():
    names = exposed_tool_names(_K(), "th_x")
    assert names == {"read_file": "read_file", "terminal": "terminal", "docs__lookup": "lookup"}


def test_deny_tools_naming_and_unknown_denied(tmp_path):
    api = _api(tmp_path)
    kernel = _K()
    api.tool_catalog = lambda tid: exposed_tool_names(kernel, tid)
    seen = []
    api.on_session_deny = lambda tid, names: seen.append(list(names))
    status, body = _open(api, tmp_path, deny_tools=["docs__lookup", "terminal", "lookup", "nouveau_outil"])
    assert status == 201
    # appliqué : le nom du registre pour un outil MCP ; un nom inconnu est ACCEPTÉ (appliqué tel quel) et listé
    assert seen == [["lookup", "terminal", "nouveau_outil"]]
    assert body["unknown_denied"] == ["lookup", "nouveau_outil"]  # le nom nu d'un outil MCP ne suit pas la convention
    status, body = _open(api, tmp_path, deny_tools=["read_file", "docs__lookup"])
    assert status == 201 and body["unknown_denied"] == []
    status, body = _open(api, tmp_path, deny_tools=[])
    assert status == 201 and "unknown_denied" not in body


def test_reopen_with_the_same_deny_list_is_not_unknown(tmp_path):
    api = _api(tmp_path)
    kernel = _K()
    api.tool_catalog = lambda tid: exposed_tool_names(kernel, tid)
    api.on_session_deny = lambda tid, names: kernel.tools.hide_for_thread(tid, names)
    tid = _open(api, tmp_path, deny_tools=["terminal"])[1]["thread_id"]
    body = _open(api, tmp_path, thread_id=tid, deny_tools=["terminal"])[1]
    assert body["unknown_denied"] == []


def test_config_read_tools_names_are_checked():
    known = {"lookup", "search"}
    assert config_read_tools("docs", ["lookup", "docs__search"], known) == ["lookup", "search"]
    with pytest.raises(McpConfigError, match="inconnu de ce serveur : docs__nope"):
        config_read_tools("docs", ["lookup", "docs__nope"], known)
    with pytest.raises(McpConfigError, match="autre__lookup"):
        config_read_tools("docs", ["autre__lookup"], known)
    # catalogue inconnu (serveur injoignable) : rien à vérifier, le préfixe est retiré
    assert config_read_tools("docs", ["docs__lookup", "search"], None) == ["lookup", "search"]


def test_delegate_tiers_refused_at_startup_when_unknown_or_misplaced(tmp_path):
    p = tmp_path / "c.yaml"
    base = ("delegate_tier: true\ntiers:\n  ship: {base_url: 'https://a/v1', model: m}\n"
            "  outils: {base_url: 'https://b/v1', model: m2}\n  defi: {base_url: 'https://b/v1', model: m3}\n")
    p.write_text(base + "delegate_tiers: [outils, defi]\n")
    assert load_config(str(p)).delegate_tiers == ["outils", "defi"]
    p.write_text(base + "delegate_tiers: [outils, fantome]\n")
    with pytest.raises(ValueError, match="fantome"):
        load_config(str(p))
    p.write_text(base + "delegate_tiers: [outils, outils]\n")
    with pytest.raises(ValueError, match="doublon"):
        load_config(str(p))
    p.write_text(base.replace("delegate_tier: true\n", "") + "delegate_tiers: [outils]\n")
    with pytest.raises(ValueError, match="delegate_tier: true"):
        load_config(str(p))
    assert NokkerConfig().delegate_tiers == []  # défaut : ship et deep, comme avant


def test_config_tier_provider_carries_the_tier_settings():
    cfg = NokkerConfig(tiers={"atlas": TierConfig(base_url="https://b/v1", model="m2", gate=True,
                                                  sampling={"temperature": 0.3}, vision=True)})
    p = config_tier_provider(cfg, "atlas")
    assert (p.model, p.gate, p.vision, p.sampling_body) == ("m2", True, True, {"temperature": 0.3})


# ───────────────────── `delegate_tiers` : des noms de paliers choisis par l'hôte ─────────────────────


def test_delegate_schema_lists_the_host_tiers_only_when_configured():
    from nokker.tools import delegate as dg
    from tests.unit.tools.test_delegate_tier import setup

    names = ["outils", "defi", "redaction", "petit"]
    src, *_ = setup(tier_choices=names, tier_provider=lambda n: None)
    spec = src.specs()[0]
    assert spec.parameters["properties"]["tier"] == dg.tier_param(names) and spec.parameters["properties"]["tier"]["enum"] == names
    assert spec.description == dg.CONV_DESCRIPTION_NAMED and "ship" not in spec.description
    # sans delegate_tiers : schéma et description d'avant, à l'octet
    plain, *_ = setup()
    assert plain.specs()[0].parameters["properties"]["tier"] == dg._TIER_PARAM and plain.specs()[0].description == dg.CONV_DESCRIPTION


def test_delegate_host_tier_names_are_validated_and_routed():
    from tests.unit.tools.test_delegate_tier import call, setup

    asked = []
    src, _p, kids, ctx = setup(tier_choices=["outils", "deep"], tier_provider=lambda n: asked.append(n))
    r = call(src, ctx, tier="ship")  # n'est plus un choix de l'hôte
    assert r.is_error and r.content == "tier invalide : choisis parmi outils, deep" and not kids
    assert not call(src, ctx, tier="outils").is_error
    assert asked == ["outils"] and set(kids[0].decisions) == {Tier.SHIP}  # son fournisseur prend la place du palier ship
    assert not call(src, ctx, tier="deep").is_error  # un nom qui est un palier servi du noyau le force
    assert asked == ["outils"] and set(kids[1].decisions) == {Tier.DEEP}




# ───────────────────── une description par nom de palier, textes figés ─────────────────────


def test_tier_param_cites_each_description_in_parentheses():
    from nokker.tools import delegate as dg

    names = ["outils", "defi", "petit"]
    p = dg.tier_param(names, {"outils": "rapide, pour des tâches simples", "defi": "approfondi, pour un raisonnement long"})
    assert p["enum"] == names and p["description"] == (
        "palier du modèle des sous-agents : outils (rapide, pour des tâches simples), defi (approfondi, pour un "
        "raisonnement long), petit ; choisis celui qui convient à la tâche ; absent : le palier habituel")
    # sans aucune description : le texte de la version précédente, nom seul
    assert dg.tier_param(names)["description"] == (
        "palier du modèle des sous-agents : outils, defi, petit ; choisis celui qui convient à la tâche ; absent : le palier habituel")


def test_named_tier_texts_are_frozen():
    from nokker.tools import delegate as dg

    assert dg.CONV_DESCRIPTION_NAMED == (
        "Confie un travail à un sous-agent et attends son compte rendu : choisis le palier qui convient à la nature du travail "
        "(ne le nomme pas à la personne). Tu restes la voix : transmets le compte rendu fidèlement, sans rien ajouter.")
    assert dg.TIER_INVALID_NAMED == "tier invalide : choisis parmi {names}"


def test_without_delegate_tiers_the_ship_deep_texts_are_byte_identical():
    from nokker.tools import delegate as dg
    from tests.unit.tools.test_delegate_tier import setup

    src, *_ = setup()
    assert src.specs()[0].parameters["properties"]["tier"] == {
        "type": "string", "enum": ["ship", "deep"],
        "description": "palier du modèle des sous-agents : ship (code, DevOps, build, opérations ; rapide) ou deep "
                       "(architecture, analyse longue, problème difficile ; plus coûteux, réservé aux comptes qui y ont "
                       "droit) ; absent : le palier habituel"}
    assert dg.TIER_INVALID == "tier : ship ou deep attendu"
    assert dg.CONV_DESCRIPTION == ("Confie un travail à un sous-agent et attends son compte rendu : tier ship pour une exécution "
                                   "technique bornée (code, build, opérations), tier deep pour une conception ou un diagnostic "
                                   "difficile. Tu restes la voix : transmets le compte rendu fidèlement, sans rien ajouter.")


def test_schema_uses_the_configured_descriptions():
    from tests.unit.tools.test_delegate_tier import setup

    src, *_ = setup(tier_choices=["outils", "defi"], tier_descriptions={"outils": "rapide"}, tier_provider=lambda n: None)
    assert "outils (rapide), defi ;" in src.specs()[0].parameters["properties"]["tier"]["description"]


def test_delegate_tiers_accepts_names_or_objects_and_validates_descriptions(tmp_path):
    p = tmp_path / "c.yaml"
    base = ("delegate_tier: true\ntiers:\n  ship: {base_url: 'https://a/v1', model: m}\n"
            "  outils: {base_url: 'https://b/v1', model: m2}\n  defi: {base_url: 'https://b/v1', model: m3}\n")
    p.write_text(base + "delegate_tiers:\n  - {name: outils, description: 'rapide, pour des tâches simples'}\n  - defi\n")
    cfg = load_config(str(p))
    assert cfg.delegate_tier_entries() == [("outils", "rapide, pour des tâches simples"), ("defi", "")]
    p.write_text(base + "delegate_tiers: [outils, defi]\n")  # la forme « liste de noms » reste valide
    assert load_config(str(p)).delegate_tier_entries() == [("outils", ""), ("defi", "")]
    for bad in ("{name: outils, description: ''}", "{name: outils, description: 3}", "{name: outils, extra: 1}",
                "{description: 'x'}", "{name: fantome, description: 'x'}", "{name: outils, description: \"a\\nb\"}",
                "{name: outils, description: '" + "x" * 121 + "'}"):
        p.write_text(base + f"delegate_tiers: [{bad}]\n")
        with pytest.raises(ValueError, match="delegate_tiers"):
            load_config(str(p))
    p.write_text(base + "delegate_tiers: [{name: outils, description: '" + "x" * 120 + "'}]\n")
    assert load_config(str(p)).delegate_tier_entries()[0][1] == "x" * 120
