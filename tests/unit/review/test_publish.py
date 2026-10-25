"""X13 : publication de la revue sur GitHub et Forgejo, contre un faux serveur HTTP local (`nokker/review/publish.py`)."""
from __future__ import annotations

import json

import pytest

from nokker.review.engine import Comment, ReviewReport
from nokker.review.publish import (
    ALLOWED_EVENTS,
    PublishError,
    Target,
    publish,
    resolve_target,
    review_event,
    token_from_env,
)

SHA = "a" * 40


def report(verdict="changes_requested"):
    return ReviewReport(verdict=verdict, summary="Fee sign inverted.", comments=[
        Comment("src/pay.py", 11, "RIGHT", "error", "Added instead of subtracted."),
        Comment("src/pay.py", 11, "LEFT", "info", "Previous behaviour.")], reviewed_files=["src/pay.py"])


def created(_req):
    return 200, {"id": 1, "html_url": "https://example.org/pr/7#review-1"}


def test_never_approve():
    for verdict in ("ok", "changes_requested", "approve", "APPROVE", ""):
        for flag in (False, True):
            assert review_event(verdict, flag) in ALLOWED_EVENTS
    assert review_event("ok", True) == "COMMENT"
    assert review_event("changes_requested", False) == "COMMENT"
    assert review_event("changes_requested", True) == "REQUEST_CHANGES"


def test_github_review_payload(fake_server):
    srv = fake_server(created)
    t = Target("github", srv.url, "acme/shop", 7, SHA)
    res = publish(t, report(), token="ghs_test", post=True, request_changes=True)
    assert res.posted and res.event == "REQUEST_CHANGES" and res.html_url.endswith("review-1")
    (req,) = srv.requests
    assert req["path"] == "/repos/acme/shop/pulls/7/reviews"
    assert req["headers"]["authorization"] == "Bearer ghs_test"
    assert req["headers"]["accept"] == "application/vnd.github+json"
    body = req["body"]
    assert body["event"] == "REQUEST_CHANGES" and body["commit_id"] == SHA
    assert body["comments"] == [
        {"path": "src/pay.py", "line": 11, "side": "RIGHT", "body": "**error**: Added instead of subtracted."},
        {"path": "src/pay.py", "line": 11, "side": "LEFT", "body": "**info**: Previous behaviour."}]
    assert "Fee sign inverted." in body["body"] and "never approves" in body["body"]
    assert "Added instead of subtracted" not in body["body"]  # en commentaire de ligne, pas recopié


def test_ok_verdict_posts_a_comment_review(fake_server):
    srv = fake_server(created)
    publish(Target("github", srv.url, "acme/shop", 7), report("ok"), token="t", post=True, request_changes=True)
    assert srv.requests[0]["body"]["event"] == "COMMENT" and "commit_id" not in srv.requests[0]["body"]


def test_forgejo_review_payload(fake_server):
    srv = fake_server(created)
    t = Target("forgejo", srv.url + "/api/v1", "acme/shop", 7, SHA)
    res = publish(t, report(), token="fj_test", post=True)
    assert res.posted and res.event == "COMMENT"
    (req,) = srv.requests
    assert req["path"] == "/api/v1/repos/acme/shop/pulls/7/reviews"
    assert req["headers"]["authorization"] == "token fj_test"
    assert req["body"]["comments"] == [
        {"path": "src/pay.py", "body": "**error**: Added instead of subtracted.", "new_position": 11,
         "old_position": 0},
        {"path": "src/pay.py", "body": "**info**: Previous behaviour.", "new_position": 0, "old_position": 11}]


def test_dry_run_sends_nothing(fake_server):
    srv = fake_server(created)
    res = publish(Target("github", srv.url, "acme/shop", 7), report(), token="", post=False)
    assert res.dry_run and not res.posted and res.comments == 2 and not srv.requests


def test_422_fallbacks(fake_server):
    def respond(req):
        b = req["body"]
        if b["event"] == "REQUEST_CHANGES":
            return 422, {"message": "Can not request changes on your own pull request"}
        if b.get("comments"):
            return 422, {"message": "Line could not be resolved"}
        return 200, {"id": 2}

    srv = fake_server(respond)
    res = publish(Target("github", srv.url, "acme/shop", 7), report(), token="t", post=True, request_changes=True)
    assert res.posted and res.event == "COMMENT" and res.comments == 0
    assert res.fallbacks == ["request_changes_refused", "inline_refused"]
    last = srv.requests[-1]["body"]
    assert "comments" not in last and "Added instead of subtracted" in last["body"] and last["event"] == "COMMENT"


def test_http_error_is_raised(fake_server):
    srv = fake_server(lambda _r: (403, {"message": "Resource not accessible by integration"}))
    with pytest.raises(PublishError, match="403"):
        publish(Target("github", srv.url, "acme/shop", 7), report(), token="t", post=True)


def test_token_never_sent_in_clear_to_a_remote_host():
    with pytest.raises(PublishError, match="en clair"):
        publish(Target("github", "http://api.example.org", "acme/shop", 7), report(), token="t", post=True)
    with pytest.raises(PublishError, match="jeton"):
        publish(Target("github", "https://api.example.org", "acme/shop", 7), report(), token="", post=True)


def test_target_from_runner_environment(tmp_path):
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"number": 42, "pull_request": {"number": 42, "head": {"sha": "b" * 40}}}))
    env = {"GITHUB_REPOSITORY": "acme/shop", "GITHUB_EVENT_PATH": str(event),
           "GITHUB_API_URL": "https://api.github.com", "GITHUB_SERVER_URL": "https://git.example.org"}
    gh = resolve_target("github", env=env)
    assert (gh.repo, gh.number, gh.commit, gh.api_url) == ("acme/shop", 42, "b" * 40, "https://api.github.com")
    fj = resolve_target("forgejo", env=env)
    assert fj.api_url == "https://git.example.org/api/v1"
    assert fj.reviews_url() == "https://git.example.org/api/v1/repos/acme/shop/pulls/42/reviews"
    with pytest.raises(PublishError):
        resolve_target("github", env={"GITHUB_REPOSITORY": "../../x"})
    with pytest.raises(PublishError):
        resolve_target("github", env={"GITHUB_REPOSITORY": "acme/shop"})  # pas de PR


def test_token_lookup_order():
    assert token_from_env("github", env={"GITHUB_TOKEN": "g"}) == ("GITHUB_TOKEN", "g")
    assert token_from_env("forgejo", env={"GITHUB_TOKEN": "g", "NOKKER_REVIEW_TOKEN": "n"}) == ("NOKKER_REVIEW_TOKEN",
                                                                                                "n")
    assert token_from_env("forgejo", env={"GITHUB_TOKEN": "g"}) == ("GITHUB_TOKEN", "g")
    assert token_from_env("github", "MY_TOKEN", env={}) == ("MY_TOKEN", "")
