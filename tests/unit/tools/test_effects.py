"""Tests de la table d'effets et de l'enveloppe secrète (F3, nokker/tools/effects.py)."""
from __future__ import annotations

from nokker.core.ports import ToolContext, ToolSpec
from nokker.core.turn import Effect, ToolResult
from nokker.qa.secrets import KnownSecrets
from nokker.tools.effects import (
    DEFAULT_EFFECTS, EffectsTable, SecretScrubbingToolSource, redact_tool_result,
)


FAKE_PASSWORD = "Xk9!qpzT42Lm"
CTX = ToolContext(thread_id="t1", turn_index=0, channel="api")


# ───────────────────────── table par défaut ─────────────────────────


def test_default_effects_cover_banc_tool_names():
    # ids réels du banc openclaw-eval/tasks.py : ne doivent pas tomber sur le défaut WRITE pour
    # rien (sinon credentiels-pas-de-fuite ne serait jamais détecté comme SECRET).
    assert DEFAULT_EFFECTS["grafana_get_credentials"] is Effect.SECRET
    assert DEFAULT_EFFECTS["send_bulk_email"] is Effect.SEND_BULK
    assert DEFAULT_EFFECTS["infra_lvextend"] is Effect.DESTRUCTIVE
    assert DEFAULT_EFFECTS["infra_disk_usage"] is Effect.READ
    assert DEFAULT_EFFECTS["memory_search"] is Effect.READ
    assert DEFAULT_EFFECTS["calendar_create"] is Effect.WRITE
    assert DEFAULT_EFFECTS["send_email_forward"] is Effect.SEND_EXTERNAL


def test_unknown_tool_defaults_to_write():
    table = EffectsTable.load()
    assert table.effect_for("un_outil_jamais_vu") is Effect.WRITE


def test_override_wins_over_default():
    table = EffectsTable.load({"calendar_create": "destructive"})
    assert table.effect_for("calendar_create") is Effect.DESTRUCTIVE
    assert table.effect_for("calendar_list") is Effect.READ  # inchangé


def test_annotate_sets_effect_on_spec():
    table = EffectsTable.load()
    spec = ToolSpec(name="infra_lvextend", description="d", parameters={})
    annotated = table.annotate(spec)
    assert annotated.effect is Effect.DESTRUCTIVE
    assert spec.effect is Effect.WRITE  # l'original n'est pas muté


# ───────────────────────── transformation SECRET → poignée ─────────────────────────


def test_redact_tool_result_removes_raw_secret():
    known = KnownSecrets()
    known.add("VAULT_GRAFANA", FAKE_PASSWORD)
    result = ToolResult(call_id="c1", name="grafana_get_credentials",
                        content=f"user admin / mot de passe {FAKE_PASSWORD}")
    scrubbed = redact_tool_result(result, known)
    assert FAKE_PASSWORD not in scrubbed.content
    assert "VAULT_GRAFANA" in scrubbed.secret_handles


class _FakeSource:
    def __init__(self, content: str):
        self.content = content
        self.calls: list[str] = []

    def specs(self):
        return [ToolSpec(name="grafana_get_credentials", description="d", parameters={})]

    def call(self, name, arguments, ctx):
        self.calls.append(name)
        return ToolResult(call_id="c1", name=name, content=self.content)


def test_secret_scrubbing_tool_source_redacts_secret_effect_results():
    known = KnownSecrets()
    known.add("VAULT_GRAFANA", FAKE_PASSWORD)
    inner = _FakeSource(f"user admin / mot de passe {FAKE_PASSWORD}")
    wrapped = SecretScrubbingToolSource(inner=inner, known=known)
    result = wrapped.call("grafana_get_credentials", {}, CTX)
    assert FAKE_PASSWORD not in result.content
    assert inner.calls == ["grafana_get_credentials"]  # l'outil réel a bien été exécuté


def test_secret_scrubbing_tool_source_leaves_non_secret_results_untouched():
    inner = _FakeSource(f"ceci n'est pas un secret : {FAKE_PASSWORD}")
    table = EffectsTable.load({"grafana_get_credentials": "read"})
    wrapped = SecretScrubbingToolSource(inner=inner, effects=table)
    result = wrapped.call("grafana_get_credentials", {}, CTX)
    assert FAKE_PASSWORD in result.content  # effet READ dans cette enveloppe : pas transformé


def test_secret_scrubbing_tool_source_annotates_specs():
    inner = _FakeSource("peu importe")
    wrapped = SecretScrubbingToolSource(inner=inner)
    specs = wrapped.specs()
    assert specs[0].effect is Effect.SECRET


# ── filet fail-closed : trois noms sans outil local, mais qu'un outil MCP ou de skill homonyme peut porter ──
_FAILCLOSED_NAMES = ("send_email_reply", "send_email_forward", "send_telegram_message")


class _McpHomonym:
    """Source MCP minimale qui expose les trois noms (comme `McpToolSource` après `list_tools`) : tout en WRITE au départ."""

    def specs(self):
        from nokker.core.ports import ToolSpec

        return [ToolSpec(name=n, description="Lit le dossier", parameters={}, source="mcp:tiers") for n in _FAILCLOSED_NAMES]

    def call(self, name, arguments, ctx):
        return "ok"

    def turn_done(self, thread_id):
        pass


def test_mcp_homonyms_stay_send_external_and_are_not_liftable_to_read():
    from nokker.tools.mcp_config import ReadDeclaredSource

    table = EffectsTable()
    # l'opérateur déclare ces outils « en lecture » (read_tools) : la déclaration ne baisse jamais un effet connu
    wrapped = ReadDeclaredSource(inner=_McpHomonym(), read_tools=list(_FAILCLOSED_NAMES), effects=table)
    assert {s.name: s.effect for s in wrapped.specs()} == {n: Effect.SEND_EXTERNAL for n in _FAILCLOSED_NAMES}
    for spec in _McpHomonym().specs():
        assert wrapped._liftable(table.annotate(spec)) is False


def test_failclosed_names_have_no_card_target():
    from nokker.approvals.target import card_target

    for name in _FAILCLOSED_NAMES:
        assert card_target(name, {"to": "bob@example.org", "subject": "Devis", "target": "telegram:1"}) is None


# ───────────────────────── outil sans effet déclaré (G4) ─────────────────────────


def _spec(name: str, description: str = "d", props: dict | None = None) -> ToolSpec:
    return ToolSpec(name=name, description=description, source="mcp:x",
                    parameters={"type": "object", "properties": props or {}})


def test_g4_outil_inconnu_ressemblant_a_un_envoi_est_send_external():
    table = EffectsTable.load()
    for name in ("send_sms", "sendEmail", "slack_post_message", "notify_team", "publish_article", "reply_to_thread",
                 "forward_mail", "gmail_send", "outlook_mail_create", "tweet_post"):
        assert table.annotate(_spec(name)).effect is Effect.SEND_EXTERNAL, name


def test_g4_schema_de_destinataire_vaut_envoi():
    table = EffectsTable.load()
    s = _spec("deliver", props={"recipients": {"type": "array"}, "body": {"type": "string"}})
    assert table.annotate(s).effect is Effect.SEND_EXTERNAL
    s = _spec("deliver", props={"to": {"type": "string"}, "subject": {"type": "string"}})
    assert table.annotate(s).effect is Effect.SEND_EXTERNAL
    s = _spec("deliver", props={"chat_id": {"type": "string"}, "text": {"type": "string"}})
    assert table.annotate(s).effect is Effect.SEND_EXTERNAL


def test_g4_lectures_et_outils_neutres_restent_write_marques_inconnus():
    table = EffectsTable.load()
    for name in ("get_message", "list_messages", "search_mail", "read_email", "convert_units", "resize_image"):
        spec = table.annotate(_spec(name, props={"from": {"type": "string"}, "to": {"type": "string"}}))
        assert spec.effect is Effect.WRITE, name
        assert spec.effect_inferred == "unknown", name


def test_g4_outils_connus_et_surcharges_inchanges():
    table = EffectsTable.load({"slack_post_message": "read", "notify_team": "write"})
    assert table.annotate(_spec("slack_post_message")).effect is Effect.READ
    assert table.annotate(_spec("notify_team")).effect is Effect.WRITE
    assert table.annotate(_spec("notify_team")).effect_inferred is None
    assert table.annotate(_spec("mail_search")).effect is Effect.READ
    assert table.annotate(_spec("mail_draft")).effect is Effect.WRITE
    assert table.annotate(_spec("mail_draft")).effect_inferred is None
    assert table.annotate(_spec("mail_send")).effect is Effect.SEND_EXTERNAL
