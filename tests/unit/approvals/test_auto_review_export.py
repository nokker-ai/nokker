"""Export d'opérateur `auto-review export` (`approvals/auto_review_export.py`) : lignes, résumé, trajectoire réduite
aux noms d'outils, bornes, CLI. Aucun Postgres : la lecture est branchée sur des cartes en mémoire."""
from __future__ import annotations

import io
import json

from nokker.approvals import auto_review_export as ex
from nokker.cli import build_parser

TURN = {"messages": [
    {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "name": "read_file", "arguments": {"p": "SECRET-CONTENU"}}]},
    {"role": "tool", "content": "résultat confidentiel", "tool_call_id": "c1"},
    {"role": "assistant", "content": "", "tool_calls": [{"id": "c2", "name": "grep", "arguments": {}},
                                                       {"id": "c3", "name": "write_file", "arguments": {}}]},
]}


def card(**kw):
    review = {"decision": "approve", "reason": "lecture seule", "model": "qwen/qwen3.5-397b-a17b", "at": 1793577600.0,
              "by_rule": False}
    review.update(kw.pop("review", {}))
    base = ("a1", "t1", 0, "write_file", "effect:write", {"path": "a.txt", "content": "x" * 500}, "c3", review)
    return base


def test_row_fields_summary_model_and_trajectory_without_content():
    row = ex.build_row("us_1", card(), TURN)
    assert row["date"] == "2026-11-02T00:00:00+00:00" and row["locataire"] == "us_1" and row["carte"] == "a1"
    assert (row["outil"], row["regle"], row["effet"]) == ("write_file", "effect:write", "write")
    assert len(row["arguments"]) <= 201 and row["arguments"].endswith("…")
    assert (row["raison"], row["modele"]) == ("lecture seule", "qwen/qwen3.5-397b-a17b")
    assert row["trajectoire"] == ["read_file", "grep"]  # appels AVANT la carte (c3 exclu), noms seuls
    assert "SECRET" not in json.dumps(row) and "confidentiel" not in json.dumps(row)


def test_trajectory_none_without_turn_effect_none_for_other_rules_and_limit():
    row = ex.build_row("us_1", (*card()[:4], "permissions:default", *card()[5:]), None)
    assert row["trajectoire"] is None and row["effet"] is None
    many = {"messages": [{"role": "assistant", "tool_calls": [{"id": f"i{i}", "name": f"t{i}"} for i in range(12)]}]}
    assert ex.trajectory_names(many, "zz") == [f"t{i}" for i in range(4, 12)]


def test_parse_instant_naive_is_utc():
    assert ex.parse_instant("2026-11-02T00:00:00") == ex.parse_instant("2026-11-02T00:00:00Z") == 1793577600.0


def test_main_writes_json_lines_and_bounds(monkeypatch):
    seen = {}

    def fake(dsn, since, until):
        seen.update(dsn=dsn, since=since, until=until)
        yield ex.build_row("us_1", card(), TURN)
        yield ex.build_row("us_2", card(), None)

    monkeypatch.setattr(ex, "export_rows", fake)
    out = io.StringIO()
    n = ex.main("postgresql://x", "2026-11-02T00:00:00Z", "2026-11-09T00:00:00Z", out)
    lines = out.getvalue().splitlines()
    assert n == 2 and [json.loads(x)["locataire"] for x in lines] == ["us_1", "us_2"]
    assert seen["until"] - seen["since"] == 7 * 86400 and seen["dsn"] == "postgresql://x"


def test_cli_parses_and_runs(monkeypatch, capsys, tmp_path):
    a = build_parser().parse_args(["auto-review", "export", "--since", "2026-11-02"])
    assert (a.since, a.until, a.unscoped) == ("2026-11-02", None, True)
    monkeypatch.setattr(ex, "export_rows", lambda dsn, s, u: iter([ex.build_row("us_1", card(), TURN)]))
    monkeypatch.setenv("NOKKER_PG_DSN", "postgresql://x")
    from nokker.cli import main
    assert main(["auto-review", "export", "--since", "2026-11-02T00:00:00Z"]) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[0])["outil"] == "write_file"
    assert main(["auto-review", "export", "--since", "pas-une-date"]) == 2


def test_sql_is_read_only_select():
    assert ex._CARDS_SQL.lstrip().upper().startswith("SELECT") and "auto_review->>'decision' = 'approve'" in ex._CARDS_SQL
