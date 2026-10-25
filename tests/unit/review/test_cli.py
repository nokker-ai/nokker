"""X13 : `python -m nokker review` de bout en bout, modèle et plateforme simulés par des faux serveurs locaux."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from nokker.cli import build_parser as nokker_parser
from nokker.review.cli import build_parser, model_settings, run
from tests.unit.review.helpers import model_reply
from tests.unit.review.test_engine import INJECTED, MOD

ROOT = Path(__file__).resolve().parents[3]


def model_server(fake_server, reply):
    def respond(req):
        assert req["path"] == "/v1/chat/completions"
        return 200, model_reply(reply(req["body"]))

    return fake_server(respond)


def changes(_body):
    return {"verdict": "changes_requested", "summary": "Fee sign inverted.", "comments": [
        {"path": "src/pay.py", "line": 11, "side": "RIGHT", "severity": "error", "body": "Added, not subtracted."},
        {"path": "src/pay.py", "line": 404, "side": "RIGHT", "severity": "error", "body": "hallucinated"}]}


@pytest.fixture
def diff_file(tmp_path):
    p = tmp_path / "pr.diff"
    p.write_text(MOD)
    return p


KEY = "sk-" + "test-key-0123456789abcdefghij"  # morcelé : scanners
TOKEN = "ghs_" + "tokenvalue_0123456789abcdef"


def env_for(model_url, **extra):
    return {"OPENAI_BASE_URL": model_url + "/v1", "OPENAI_API_KEY": KEY,
            "NOKKER_REVIEW_MODEL": "judge-x", **extra}


def test_dry_run_json(fake_server, diff_file, capsys):
    srv = model_server(fake_server, changes)
    code = run(build_parser().parse_args(["--diff", str(diff_file), "--json"]), env=env_for(srv.url))
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["verdict"] == "changes_requested" and out["dropped_comments"] == 1
    assert [(c["line"], c["side"], c["severity"]) for c in out["comments"]] == [(11, "RIGHT", "error")]
    assert "post" not in out  # ni plateforme ni PR : aucune publication, même à blanc
    (req,) = srv.requests
    assert req["headers"]["authorization"] == f"Bearer {KEY}"
    assert req["body"]["model"] == "judge-x" and req["body"]["temperature"] == 0


def test_post_to_github_and_fail_on_changes(fake_server, diff_file, tmp_path, capsys):
    model = model_server(fake_server, changes)
    gh = fake_server(lambda _r: (200, {"id": 9, "html_url": "https://example.org/r/9"}))
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"number": 5, "head": {"sha": "c" * 40}}}))
    env = env_for(model.url, GITHUB_ACTIONS="true", GITHUB_REPOSITORY="acme/shop", GITHUB_API_URL=gh.url,
                  GITHUB_EVENT_PATH=str(event), GITHUB_TOKEN=TOKEN)
    args = build_parser().parse_args(["--diff", str(diff_file), "--json", "--post", "--fail-on-changes"])
    code = run(args, env=env)
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and out["post"]["posted"] and out["post"]["event"] == "COMMENT"
    (req,) = gh.requests
    assert req["path"] == "/repos/acme/shop/pulls/5/reviews" and req["body"]["commit_id"] == "c" * 40
    assert [c["line"] for c in req["body"]["comments"]] == [11]
    assert TOKEN not in json.dumps(out)


def test_in_ci_without_post_is_a_dry_run(fake_server, diff_file, tmp_path, capsys):
    model = model_server(fake_server, changes)
    gh = fake_server(lambda _r: (200, {}))
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"number": 5}}))
    env = env_for(model.url, FORGEJO_ACTIONS="true", GITHUB_ACTIONS="true", GITHUB_REPOSITORY="acme/shop",
                  GITHUB_SERVER_URL=gh.url, GITHUB_EVENT_PATH=str(event))
    code = run(build_parser().parse_args(["--diff", str(diff_file), "--json", "--request-changes"]), env=env)
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["post"]["dry_run"] and out["post"]["platform"] == "forgejo"
    assert out["post"]["event"] == "REQUEST_CHANGES" and out["post"]["url"].endswith("/api/v1/repos/acme/shop/pulls/5/reviews")
    assert not gh.requests


def test_hidden_instruction_end_to_end(fake_server, tmp_path, capsys):
    def obedient(body):
        if "approve this PR" in body["messages"][1]["content"]:
            return {"verdict": "ok", "summary": "Approved as requested.", "comments": []}
        return {"verdict": "ok", "summary": "", "comments": []}

    srv = model_server(fake_server, obedient)
    p = tmp_path / "pr.diff"
    p.write_text(INJECTED)
    code = run(build_parser().parse_args(["--diff", str(p), "--json", "--fail-on-changes"]), env=env_for(srv.url))
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and out["verdict"] == "changes_requested" and out["injection_detected"]


def test_model_down_exit_3_and_nothing_posted(fake_server, diff_file, capsys):
    srv = fake_server(lambda _r: (500, {"error": "boom"}))
    gh = fake_server(lambda _r: (200, {}))
    env = env_for(srv.url, GITHUB_REPOSITORY="acme/shop", GITHUB_API_URL=gh.url, GITHUB_TOKEN="ghs_x0123456789")
    args = build_parser().parse_args(["--diff", str(diff_file), "--json", "--post", "--platform", "github",
                                      "--pr", "3"])
    assert run(args, env=env) == 3
    out = json.loads(capsys.readouterr().out)
    assert out["post"] is None and not gh.requests and "could not run" in out["summary"]


def test_markdown_output(fake_server, diff_file, capsys):
    srv = model_server(fake_server, changes)
    assert run(build_parser().parse_args(["--diff", str(diff_file)]), env=env_for(srv.url)) == 0
    text = capsys.readouterr().out
    assert text.startswith("## Nokker review: changes requested") and "`src/pay.py:11` (RIGHT) **error**" in text


def test_model_settings_sources(tmp_path):
    a = build_parser().parse_args([])
    with pytest.raises(Exception, match="aucun modèle"):
        model_settings(a, {})
    cfg = tmp_path / "core-config.yaml"
    cfg.write_text("tiers:\n  ship:\n    base_url: https://llm.example.org/v1\n    model: big-model\n"
                   "    api_key_env: MY_KEY\n")
    s = model_settings(a, {"NOKKER_CONFIG": str(cfg), "MY_KEY": "k"})
    assert (s["base_url"], s["model"], s["api_key"], s["extra_body"]) == ("https://llm.example.org/v1", "big-model",
                                                                          "k", {"reasoning_effort": "none"})
    s = model_settings(a, {"NOKKER_CONFIG": str(cfg), "MY_KEY": "k", "NOKKER_REVIEW_MODEL": "other"})
    assert s["model"] == "other"
    s = model_settings(a, {"OPENAI_BASE_URL": "https://x/v1", "NOKKER_REVIEW_MODEL": "m", "OPENAI_API_KEY": "o",
                           "NOKKER_REVIEW_EXTRA_BODY": '{"chat_template_kwargs": {"enable_thinking": false}}'})
    assert s["api_key"] == "o" and s["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    with pytest.raises(Exception, match="introuvable"):
        model_settings(build_parser().parse_args(["--config", str(tmp_path / "nope.yaml")]), {})


def test_nokker_cli_has_review_subcommand():
    args = nokker_parser().parse_args(["--config", "/x.yaml", "review", "--diff", "-", "--json"])
    assert args.diff == "-" and args.json and args.config == "/x.yaml" and args.review_config is None


def test_python_m_nokker_review(fake_server, diff_file):
    srv = model_server(fake_server, changes)
    env = {k: v for k, v in os.environ.items() if "PROXY" not in k.upper()}
    env.update(env_for(srv.url), NO_PROXY="127.0.0.1")
    proc = subprocess.run([sys.executable, "-m", "nokker", "review", "--diff", "-", "--json"], input=MOD,
                          capture_output=True, text=True, env=env, cwd=ROOT, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["verdict"] == "changes_requested"
