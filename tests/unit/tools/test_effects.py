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
