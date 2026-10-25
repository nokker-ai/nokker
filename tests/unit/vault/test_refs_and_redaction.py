"""Références `secret:NOM` résolues à l'usage, choix explicite du fournisseur, et caviardage : une valeur du coffre
n'entre jamais dans le contexte du modèle, et ressort caviardée d'un outil qui la renverrait."""
from __future__ import annotations

import logging

import pytest

from nokker.api.server import _mcp_header_source
from nokker.connectors.provider import ConnectorProvider
from nokker.core.ports import ToolSpec
from nokker.core.turn import Effect, Message, ToolResult, TurnInput
from nokker.qa.invariants import DeterministicInvariants
from nokker.qa.secrets import KnownSecrets
from nokker.secrets import SecretResolver, SecretsError, Vault, refs_in
from nokker.secrets.vault import VaultKnownSecrets
from tests.unit.qa.test_hostile_model import ToolsFake, make_kernel, tc
from tests.unit.vault.fakes import FAKE_A, FAKE_B, FAKE_C, MemorySecrets, fernet_key


@pytest.fixture
def vault(tmp_path, monkeypatch):
    monkeypatch.setenv("NOKKER_LOCAL_SECRET_KEY", fernet_key())
    monkeypatch.delenv("NOKKER_SECRETS_PROVIDER", raising=False)
    monkeypatch.delenv("NOKKER_OPENBAO_ADDR", raising=False)
    return Vault.from_config({}, tmp_path)


def test_references_resolved_at_use(vault):
    vault.set("GH", FAKE_A)
    r = vault.resolver()
    cfg = {"url": "https://mcp.example/x", "headers": {"X-Key": "secret:GH", "X-Plain": "visible"},
           "args": ["--token", "secret:GH"]}
    assert refs_in(cfg) == ["GH"]
    out = r.resolve(cfg)
    assert out["headers"] == {"X-Key": FAKE_A, "X-Plain": "visible"} and out["args"] == ["--token", FAKE_A]
    assert cfg["headers"]["X-Key"] == "secret:GH"  # la config garde le NOM
    assert r.env({"GITHUB_TOKEN": "secret:GH", "LANG": "C"}) == {"GITHUB_TOKEN": FAKE_A, "LANG": "C"}
    vault.set("GH", FAKE_B)  # relu à chaque usage : la valeur changée est prise sans redémarrage
    assert r.resolve("secret:GH") == FAKE_B


def test_missing_reference_names_the_secret_not_a_value(vault):
    with pytest.raises(SecretsError) as e:
        vault.resolver().resolve({"k": "secret:ABSENT"})
    assert e.value.code == "missing" and "ABSENT" in str(e.value)
    with pytest.raises(SecretsError) as e:
        SecretResolver(None).resolve("secret:X")
    assert e.value.code == "unavailable"
    with pytest.raises(SecretsError) as e:
        vault.resolver().resolve("secret:../etc")
    assert e.value.code == "bad_name"
    assert SecretResolver(None).resolve("pas une référence") == "pas une référence"


def test_sealed_vault_reference_fails_with_clear_message():
    sealed = MemorySecrets()
    sealed.fail = "sealed"
    with pytest.raises(SecretsError) as e:
        SecretResolver(sealed).value("GH")
    assert e.value.code == "sealed" and "secret:GH" in str(e.value) and "scellé" in str(e.value)


def test_mcp_headers_read_from_vault_at_each_session(vault):
    vault.set("MCP_TOK", FAKE_A)
    build = _mcp_header_source({"name": "m", "url": "u", "token": "secret:MCP_TOK",
                                "headers": {"X-Team": "ops"}}, {}, vault)
    assert build() == {"Authorization": f"Bearer {FAKE_A}", "X-Team": "ops"}
    vault.set("MCP_TOK", FAKE_B)
    assert build()["Authorization"] == f"Bearer {FAKE_B}"
    assert _mcp_header_source({"name": "m", "url": "u", "token_env": "X"}, {}, vault) is None  # forme d'avant


def test_connector_secret_reference_resolved_when_building_the_source(vault, monkeypatch):
    seen = {}
    monkeypatch.setattr("nokker.connectors.provider.build_connector",
                        lambda kind, settings, secret: seen.setdefault(kind, secret))

    class Cred:
        settings, secret = {}, "secret:MAIL_PW"

    class Store:
        def get(self, account, kind):
            return Cred() if kind == "mail" else None

    vault.set("MAIL_PW", FAKE_C)
    ConnectorProvider(Store(), lambda t: "acc", resolve=vault.resolver().resolve).sources_for("t")
    assert seen == {"mail": FAKE_C}


def test_explicit_choice_only(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv("NOKKER_SECRETS_PROVIDER", raising=False)
    monkeypatch.setenv("NOKKER_OPENBAO_ADDR", "http://openbao:8200")
    monkeypatch.setenv("NOKKER_OPENBAO_ROLE_ID_FILE", str(tmp_path / "vault-dir" / "role_id"))
    v = Vault.from_config({}, tmp_path)  # compose sans le profil : adresse posée, dossier des identifiants vide
    assert v.provider == "file" and v.notice is None
    (tmp_path / "vault-dir").mkdir()
    (tmp_path / "vault-dir" / "role_id").write_text("r")
    v = Vault.from_config({}, tmp_path)  # OpenBao préparé mais pas choisi : avis, aucune bascule
    assert v.provider == "file" and v.user.kind == "file" and "rien n'a basculé" in v.notice
    with caplog.at_level(logging.WARNING, logger="nokker.secrets"):
        v.startup_report()
    assert "rien n'a basculé" in caplog.text
    assert Vault.from_config({"openbao": {"address": "http://openbao:8200"}}, tmp_path).provider == "file"
    monkeypatch.setenv("NOKKER_SECRETS_PROVIDER", "openbao")  # l'environnement du compose prime sur la config
    v = Vault.from_config({"provider": "file"}, tmp_path)
    assert v.provider == "openbao" and v.user.kind == "openbao" and v.system.kind == "openbao" and v.notice is None
    monkeypatch.setenv("NOKKER_SECRETS_PROVIDER", "nimporte")
    with pytest.raises(SecretsError):
        Vault.from_config({}, tmp_path)


def test_known_secrets_follow_the_vault_and_never_forget(tmp_path, monkeypatch):
    user, system = MemorySecrets("user"), MemorySecrets("system")
    user.set("GH", FAKE_A)
    system.set("router", FAKE_B)
    known = VaultKnownSecrets([user, system], ttl_s=0)
    base = KnownSecrets()
    base.add("NOKKER_SECRET_OLD", FAKE_C)
    known.merge(base)
    assert dict(known.items_by_length_desc()) == {"GH": FAKE_A, "nokker:router": FAKE_B, "NOKKER_SECRET_OLD": FAKE_C}
    user.delete("GH")  # effacée : reste caviardée (elle a pu entrer dans un fil avant)
    user.set("tiny", "abc")  # trop courte pour être caviardée sans abîmer le texte
    assert "GH" in known.names() and "tiny" not in known.names()
    user.fail = "sealed"  # coffre scellé : on garde ce qu'on a, sans exception
    assert "GH" in known.names()
    assert FAKE_A not in repr(known)


def test_value_never_in_model_context_and_redacted_in_tool_output(vault):
    """Le modèle n'a que le NOM ; l'outil résout la référence hors du modèle. S'il renvoie la valeur (bavard ou
    hostile), le noyau la caviarde avant le contexte et le store ; la réponse finale aussi."""
    vault.set("DB_PW", FAKE_A)
    resolver = vault.resolver()

    class LoginTool(ToolsFake):
        def call(self, name, arguments, ctx):
            pw = resolver.resolve(arguments["password"])
            return ToolResult(call_id="", name=name, content=f"connecté (mot de passe utilisé : {pw})")

    tools = LoginTool({"db_login": ToolSpec(name="db_login", description="d", parameters={}, effect=Effect.READ)})
    inv = DeterministicInvariants()
    inv.known = vault.known.merge(inv.known)  # ce que fait build_kernel(…, secrets=vault)
    script = [Message("assistant", tool_calls=[tc("db_login", password="secret:DB_PW")]),
              Message("assistant", f"c'est fait, le mot de passe est {FAKE_A}")]
    k, ship, _ = make_kernel(script, invariants=inv, tools=tools)
    turn = k.run_turn(TurnInput("t1", "connecte-toi à la base"))
    tool_text = " ".join(m.content for m in turn.messages if m.role == "tool")
    assert FAKE_A not in tool_text and "[secret:DB_PW]" in tool_text
    assert FAKE_A not in turn.reply and "[secret:DB_PW]" in turn.reply
    assert all(FAKE_A not in str(msgs) for msgs in ship.seen)  # rien de ce que le modèle a lu


def test_value_set_through_the_vault_is_redacted_immediately(vault):
    known = vault.known
    known.refresh(force=True)
    vault.set("LATE", FAKE_B)  # écrite après le démarrage (route PUT)
    from nokker.qa.secrets import redact

    assert redact(f"voilà {FAKE_B}", known, weak=False).text == "voilà [secret:LATE]"


def test_build_kernel_wires_the_vault_into_redaction_and_mcp_headers(vault):
    """Le câblage réel : `build_kernel(…, secrets=vault)` caviarde les valeurs du coffre dans chaque résultat d'outil,
    et un serveur MCP `token: secret:NOM` lit son jeton au coffre à l'ouverture de session (pas au démarrage)."""
    from nokker.api.server import build_kernel
    from nokker.config import NokkerConfig

    vault.set("MCP_TOK", FAKE_A)
    cfg = NokkerConfig(pg_dsn_env="TEST_NOKKER_ABSENT_DSN_VAR", web_search_enabled=False, previews_enabled=False,
                       approvals_enabled=False, mcp_tool_servers=[{"name": "m", "url": "http://127.0.0.1:9/mcp",
                                                                   "token": "secret:MCP_TOK"}])
    kernel = build_kernel(cfg, secrets=vault)
    spec = ToolSpec(name="x", description="d", parameters={}, effect=Effect.READ)
    out = kernel.invariants.redact_result(ToolResult(call_id="c", name="x", content=f"v={FAKE_A}"), spec)
    assert FAKE_A not in out.content and "[secret:MCP_TOK]" in out.content
    mcp = next(s for s in kernel.tools.internal.sources if getattr(getattr(s, "inner", None), "name", None) == "m")
    io = mcp.inner._io
    assert callable(io.headers) and io.headers() == {"Authorization": f"Bearer {FAKE_A}"}
    vault.set("MCP_TOK", FAKE_B)
    assert io.headers() == {"Authorization": f"Bearer {FAKE_B}"}
