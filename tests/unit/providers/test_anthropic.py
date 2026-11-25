"""`AnthropicProvider` contre un faux amont HTTP local (`anthropic_upstream.py`) qui rejoue des réponses conformes à
la documentation de l'API Messages. Aucun appel vers l'extérieur."""
from __future__ import annotations

import json

import pytest

from nokker.core.loop import accepts_on_delta
from nokker.providers.anthropic import AnthropicProvider, StreamInterrupted, to_messages_body

from .anthropic_upstream import MODEL, FakeAnthropic, error_body, message, stream_of

KEY = "sk-ant-faux-0123456789"
PREFIX = "Tu es Atlas. " * 40
TOOLS = [
    {"type": "function", "function": {"name": "get_weather", "description": "Météo d'un lieu",
                                      "parameters": {"type": "object", "properties": {"location": {"type": "string"}},
                                                     "required": ["location"]}}},
    {"type": "function", "function": {"name": "mail_search", "description": "Cherche un mail",
                                      "parameters": {"type": "object", "properties": {"q": {"type": "string"}}}}},
]
TOOL_MESSAGE = message(
    [{"type": "text", "text": "Je regarde ça."},
     {"type": "tool_use", "id": "toolu_01A", "name": "get_weather", "input": {"location": "Genève"}},
     {"type": "tool_use", "id": "toolu_01B", "name": "mail_search", "input": {"q": "facture"}}],
    stop_reason="tool_use",
    usage={"input_tokens": 40, "cache_creation_input_tokens": 300, "cache_read_input_tokens": 900,
           "output_tokens": 31})


@pytest.fixture
def up():
    with FakeAnthropic() as upstream:
        yield upstream


def _provider(up: FakeAnthropic, **kw) -> AnthropicProvider:
    kw.setdefault("retry_backoff_s", 0.0)
    return AnthropicProvider(base_url=up.url, api_key=KEY, model=MODEL, **kw)


def _ask(p: AnthropicProvider, messages=None, tools=None, **kw):
    return p.complete(messages or [{"role": "user", "content": "Bonjour"}], tools or [], reasoning=False,
                      session_key="fil-1", **kw)


# ───────────────────────────── requête : en-têtes, corps, traduction ─────────────────────────────


def test_request_headers_path_and_minimal_body(up):
    up.reply(message([{"type": "text", "text": "Bonjour."}]))
    comp = _ask(_provider(up), [{"role": "system", "content": "Tu es Atlas."}, {"role": "user", "content": "Salut"}],
                max_tokens=512)
    req = up.requests[0]
    assert req["path"] == "/v1/messages"
    assert req["headers"]["x-api-key"] == KEY
    assert req["headers"]["anthropic-version"] == "2026-10-21"
    assert req["headers"]["content-type"] == "application/json"
    assert "authorization" not in req["headers"] and "x-session-id" not in req["headers"]
    assert req["body"] == {"model": MODEL, "max_tokens": 512,
                           "system": [{"type": "text", "text": "Tu es Atlas."}],
                           "messages": [{"role": "user", "content": [{"type": "text", "text": "Salut"}]}]}
    assert comp.message.content == "Bonjour." and comp.finish_reason == "stop" and comp.model == MODEL
    assert comp.attempts == 1 and comp.message.tool_calls == []


def test_base_url_with_v1_and_bearer_auth_and_no_key(up):
    up.reply(message([{"type": "text", "text": "a"}]))
    up.reply(message([{"type": "text", "text": "b"}]))
    _ask(AnthropicProvider(base_url=up.url + "/v1/", api_key=KEY, model=MODEL, auth="bearer"))
    _ask(AnthropicProvider(base_url=up.url, api_key="", model=MODEL))
    first, second = up.requests
    assert first["path"] == "/v1/messages" and first["headers"]["authorization"] == f"Bearer {KEY}"
    assert "x-api-key" not in first["headers"]
    assert "x-api-key" not in second["headers"] and "authorization" not in second["headers"]


def test_tool_round_trip_is_translated_to_tool_use_and_tool_result():
    history = [
        {"role": "system", "content": "Tu es Atlas."},
        {"role": "user", "content": "Quel temps à Genève, et ma facture ?"},
        {"role": "assistant", "content": "Je regarde.", "tool_calls": [
            {"id": "call_a", "type": "function", "function": {"name": "get_weather",
                                                              "arguments": "{\"location\": \"Genève\"}"}},
            {"id": "functions.mail:1", "type": "function", "function": {"name": "mail_search",
                                                                       "arguments": "{pas du json"}}]},
        {"role": "tool", "tool_call_id": "call_a", "name": "get_weather", "content": "12 °C, pluie"},
        {"role": "tool", "tool_call_id": "functions.mail:1", "name": "mail_search", "content": ""},
        {"role": "user", "content": "Merci, et demain ?"},
    ]
    body = to_messages_body(history, TOOLS, model=MODEL, max_tokens=100)
    assert body["tools"] == [
        {"name": "get_weather", "description": "Météo d'un lieu",
         "input_schema": {"type": "object", "properties": {"location": {"type": "string"}}, "required": ["location"]}},
        {"name": "mail_search", "description": "Cherche un mail",
         "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}}}]
    assert "tool_choice" not in body  # défaut de l'API : auto
    assert [m["role"] for m in body["messages"]] == ["user", "assistant", "user"]  # alternance stricte
    assert body["messages"][1]["content"] == [
        {"type": "text", "text": "Je regarde."},
        {"type": "tool_use", "id": "call_a", "name": "get_weather", "input": {"location": "Genève"}},
        {"type": "tool_use", "id": "functions_mail_1", "name": "mail_search", "input": {}}]
    # Tous les résultats dans UN message user, avant le texte ; même identifiant que l'appel.
    assert body["messages"][2]["content"] == [
        {"type": "tool_result", "tool_use_id": "call_a", "content": "12 °C, pluie"},
        {"type": "tool_result", "tool_use_id": "functions_mail_1"},
        {"type": "text", "text": "Merci, et demain ?"}]


def test_images_become_image_blocks_and_empty_parts_are_dropped():
    body = to_messages_body([{"role": "user", "content": [
        {"type": "text", "text": ""},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}},
        {"type": "image_url", "image_url": {"url": "https://example.com/a.jpg"}},
        {"type": "text", "text": "Que vois-tu ?"}]}], [], model=MODEL, max_tokens=10)
    assert body["messages"] == [{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="}},
        {"type": "image", "source": {"type": "url", "url": "https://example.com/a.jpg"}},
        {"type": "text", "text": "Que vois-tu ?"}]}]
    assert "system" not in body and "tools" not in body


def test_empty_assistant_is_dropped_and_same_role_messages_merge():
    body = to_messages_body([{"role": "user", "content": "a"}, {"role": "assistant", "content": ""},
                             {"role": "user", "content": "b"}], [], model=MODEL, max_tokens=10)
    assert body["messages"] == [{"role": "user", "content": [{"type": "text", "text": "a"},
                                                             {"type": "text", "text": "b"}]}]


# ───────────────────────────── réflexion, échantillonnage ─────────────────────────────


def test_reasoning_setting_is_translated_per_call(up):
    up.reply(message([{"type": "text", "text": "a"}]))
    up.reply(message([{"type": "text", "text": "b"}]))
    up.reply(message([{"type": "text", "text": "c"}]))
    p = _provider(up)
    p.complete([{"role": "user", "content": "x"}], [], reasoning=True, session_key="f")
    p.complete([{"role": "user", "content": "x"}], [], reasoning=False, session_key="f")
    off = {"thinking": {"type": "disabled"}}
    _provider(up, reasoning_off_body=off).complete([{"role": "user", "content": "x"}], [], reasoning=False,
                                                   session_key="f")
    on, default_off, configured_off = (r["body"] for r in up.requests)
    assert on["thinking"] == {"type": "adaptive"}
    assert "thinking" not in default_off and "output_config" not in default_off
    assert configured_off["thinking"] == {"type": "disabled"}


def test_thinking_blocks_are_ignored_cleanly(up):
    up.reply(message([{"type": "thinking", "thinking": "", "signature": "EqQBCgIYAhIM"},
                      {"type": "redacted_thinking", "data": "abc"},
                      {"type": "text", "text": "\n\nRéponse."}]))
    comp = _ask(_provider(up))
    assert comp.message.content == "Réponse." and comp.message.tool_calls == []
    assert comp.usage.reasoning == 0 and comp.usage.reasoning_reported is False


def test_sampling_is_never_sent_on_the_current_api_version(up):
    up.reply(message([{"type": "text", "text": "a"}]))
    up.reply(message([{"type": "text", "text": "b"}]))
    sampling = {"temperature": 0.3, "top_p": 0.9, "seed": 7, "min_p": 0.05}
    _ask(_provider(up, sampling_body=sampling))
    _ask(_provider(up, sampling_body=sampling, anthropic_version="2023-06-01"))
    new, old = (r["body"] for r in up.requests)
    assert not {"temperature", "top_p", "top_k", "seed", "min_p"} & set(new)
    assert old["temperature"] == 0.3 and old["top_p"] == 0.9 and "seed" not in old and "min_p" not in old
    assert up.requests[1]["headers"]["anthropic-version"] == "2023-06-01"


# ───────────────────────────── cache explicite ─────────────────────────────


def _cache_points(body: dict) -> list[str]:
    found = []
    for i, block in enumerate(body.get("system") or []):
        if "cache_control" in block:
            found.append(f"system[{i}]")
    for i, tool in enumerate(body.get("tools") or []):
        if "cache_control" in tool:
            found.append(f"tools[{i}]")
    for m in body["messages"]:
        for block in m["content"]:
            if "cache_control" in block:
                found.append("messages")
    if "cache_control" in body:
        found.append("top")
    return found


SYSTEM = PREFIX + "\n\n[Outils]\nweb, mail\n\n[Profil]\nTutoiement."
HISTORY = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": "Bonjour"},
           {"role": "assistant", "content": "Salut."}, {"role": "user", "content": "Et ensuite ?"}]


def test_cache_points_on_the_stable_prefix_and_the_tool_block_only():
    body = to_messages_body(HISTORY, TOOLS, model=MODEL, max_tokens=10, cache=True, stable_prefix=PREFIX)
    assert _cache_points(body) == ["system[0]", "tools[1]"]  # 2 points sur les 4 permis, aucun dans les messages
    assert body["system"][0] == {"type": "text", "text": PREFIX, "cache_control": {"type": "ephemeral"}}
    assert body["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    # Le texte système est intact, à l'octet près, une fois les blocs recollés.
    assert "".join(b["text"] for b in body["system"]) == SYSTEM


def test_cache_none_poses_no_point_and_unknown_prefix_marks_tools_only():
    assert _cache_points(to_messages_body(HISTORY, TOOLS, model=MODEL, max_tokens=10, cache=False,
                                          stable_prefix=PREFIX)) == []
    for hint in (None, "", "Un autre préfixe."):
        body = to_messages_body(HISTORY, TOOLS, model=MODEL, max_tokens=10, cache=True, stable_prefix=hint)
        assert _cache_points(body) == ["tools[1]"]
        assert body["system"] == [{"type": "text", "text": SYSTEM}]


def test_cached_prefix_is_byte_stable_across_calls_of_a_growing_thread():
    first = to_messages_body(HISTORY[:2], TOOLS, model=MODEL, max_tokens=10, cache=True, stable_prefix=PREFIX)
    later = to_messages_body(HISTORY, TOOLS, model=MODEL, max_tokens=10, cache=True, stable_prefix=PREFIX)
    dump = lambda b: json.dumps([b["tools"], b["system"]], sort_keys=True)  # noqa: E731 — ordre de rendu : outils, système
    assert dump(first) == dump(later)


def test_provider_sends_cache_points_only_with_cache_prefix(up):
    up.reply(message([{"type": "text", "text": "a"}]))
    up.reply(message([{"type": "text", "text": "b"}]))
    _provider(up, cache="prefix").complete(HISTORY, TOOLS, reasoning=False, session_key="f", stable_prefix=PREFIX)
    _provider(up).complete(HISTORY, TOOLS, reasoning=False, session_key="f", stable_prefix=PREFIX)
    assert _cache_points(up.requests[0]["body"]) == ["system[0]", "tools[1]"]
    assert _cache_points(up.requests[1]["body"]) == []


# ───────────────────────────── réponse : outils, raisons d'arrêt, usage ─────────────────────────────


def test_tool_use_response_and_cache_usage(up):
    up.reply(TOOL_MESSAGE)
    comp = _ask(_provider(up), tools=TOOLS)
    assert comp.finish_reason == "tool_calls" and comp.message.content == "Je regarde ça."
    assert [(c.id, c.name, c.arguments) for c in comp.message.tool_calls] == [
        ("toolu_01A", "get_weather", {"location": "Genève"}), ("toolu_01B", "mail_search", {"q": "facture"})]
    assert json.loads(comp.message.tool_calls[0].raw_arguments) == {"location": "Genève"}
    # `input_tokens` = non caché seulement : prompt = 40 + 300 écrits + 900 lus.
    u = comp.usage
    assert (u.prompt, u.completion, u.cached, u.cache_write, u.cached_reported) == (1240, 31, 900, 300, True)


def test_usage_without_cache_fields_is_not_reported_as_zero_cache(up):
    up.reply(message([{"type": "text", "text": "a"}], usage={"input_tokens": 7, "output_tokens": 2}))
    u = _ask(_provider(up)).usage
    assert (u.prompt, u.completion, u.cached, u.cache_write, u.cached_reported) == (7, 2, 0, 0, False)


@pytest.mark.parametrize("stop,finish", [("end_turn", "stop"), ("stop_sequence", "stop"), ("max_tokens", "length"),
                                         ("pause_turn", "stop"), ("refusal", "content_filter")])
def test_stop_reasons(up, stop, finish):
    up.reply(message([{"type": "text", "text": "x"}], stop_reason=stop))
    assert _ask(_provider(up)).finish_reason == finish


def test_refusal_and_truncated_tool_call_are_never_handed_to_the_kernel(up):
    call = {"type": "tool_use", "id": "toolu_01A", "name": "get_weather", "input": {"location": "Gen"}}
    done = {"type": "tool_use", "id": "toolu_01Z", "name": "mail_search", "input": {"q": "facture"}}
    up.reply(message([{"type": "text", "text": "Je"}, call], stop_reason="refusal"))
    up.reply(message([done, call], stop_reason="max_tokens"))
    p = _provider(up)
    refused, cut = _ask(p, tools=TOOLS), _ask(p, tools=TOOLS)
    assert refused.finish_reason == "content_filter" and refused.message.tool_calls == []
    assert cut.finish_reason == "length" and [c.id for c in cut.message.tool_calls] == ["toolu_01Z"]


# ───────────────────────────── flux SSE ─────────────────────────────


def test_stream_rebuilds_the_same_completion_as_non_stream(up):
    up.reply(TOOL_MESSAGE)
    up.reply_stream(stream_of(TOOL_MESSAGE))
    p = _provider(up)
    plain = _ask(p, tools=TOOLS)
    pieces, tool_pieces = [], []
    streamed = _ask(p, tools=TOOLS, on_delta=pieces.append, on_tool_delta=lambda *a: tool_pieces.append(a))
    assert "stream" not in up.requests[0]["body"] and up.requests[1]["body"]["stream"] is True
    assert "".join(pieces) == "Je regarde ça." and len(pieces) > 1
    assert streamed.message == plain.message and streamed.usage == plain.usage
    assert (streamed.finish_reason, streamed.model) == (plain.finish_reason, plain.model)
    # Arguments d'outils remis au fil de l'eau : rang de l'appel (0, 1), identifiant, nom, morceau brut.
    assert {(i, cid, name) for i, cid, name, _ in tool_pieces} == {(0, "toolu_01A", "get_weather"),
                                                                    (1, "toolu_01B", "mail_search")}
    assert json.loads("".join(p for i, *_, p in tool_pieces if i == 0)) == {"location": "Genève"}


def test_stream_ignores_thinking_ping_and_unknown_events(up):
    msg = message([{"type": "thinking", "thinking": "je pèse", "signature": "sig"},
                   {"type": "text", "text": "\nVoilà."}])
    events = stream_of(msg)
    events.insert(1, {"type": "evenement_a_venir", "x": 1})
    up.reply_stream(events)
    pieces = []
    comp = _ask(_provider(up), on_delta=pieces.append)
    assert comp.message.content == "Voilà." and "".join(pieces) == "Voilà."
    assert comp.usage.prompt == 12 and comp.usage.completion == 5


def test_stream_tool_call_without_arguments_and_invalid_partial_json(up):
    events = stream_of(message([{"type": "tool_use", "id": "toolu_01A", "name": "now", "input": {}}],
                               stop_reason="tool_use"))
    events = [e for e in events if e["type"] != "content_block_delta"]  # outil sans argument : aucun delta
    bad = stream_of(message([{"type": "tool_use", "id": "toolu_01B", "name": "get_weather", "input": {"a": 1}}],
                            stop_reason="tool_use"))
    for e in bad:
        if e["type"] == "content_block_delta":
            e["delta"]["partial_json"] = "{\"location\": \"Gen"
    bad = [e for i, e in enumerate(bad) if e["type"] != "content_block_delta" or i == 2]
    up.reply_stream(events)
    up.reply_stream(bad)
    p = _provider(up)
    empty = _ask(p, tools=TOOLS, on_delta=lambda s: None)
    broken = _ask(p, tools=TOOLS, on_delta=lambda s: None)
    assert empty.message.tool_calls[0].arguments == {} and empty.message.tool_calls[0].name == "now"
    assert broken.message.tool_calls[0].arguments == {}
    assert broken.message.tool_calls[0].raw_arguments == "{\"location\": \"Gen"  # gardé pour le journal du noyau


# ───────────────────────────── erreurs et réessais ─────────────────────────────


def test_invalid_request_is_not_retried_and_the_key_never_leaks(up):
    up.reply(error_body("invalid_request_error", "messages: roles must alternate"), status=400)
    p = _provider(up)
    with pytest.raises(RuntimeError) as err:
        _ask(p)
    assert "HTTP 400" in str(err.value) and "invalid_request_error: messages: roles must alternate" in str(err.value)
    assert len(up.requests) == 1
    assert KEY not in str(err.value) and KEY not in repr(p)


@pytest.mark.parametrize("status,kind", [(401, "authentication_error"), (403, "permission_error"),
                                         (404, "not_found_error"), (413, "request_too_large")])
def test_client_errors_are_final(up, status, kind):
    up.reply(error_body(kind, "refusé"), status=status)
    with pytest.raises(RuntimeError, match=f"HTTP {status}"):
        _ask(_provider(up))
    assert len(up.requests) == 1


@pytest.mark.parametrize("status,kind", [(500, "api_error"), (529, "overloaded_error")])
def test_server_errors_are_retried(up, status, kind):
    up.reply(error_body(kind, "plus tard"), status=status)
    up.reply(message([{"type": "text", "text": "ok"}]))
    comp = _ask(_provider(up))
    assert comp.message.content == "ok" and comp.attempts == 2 and len(up.requests) == 2


def test_retries_are_bounded(up):
    for _ in range(5):
        up.reply(error_body("overloaded_error", "Overloaded"), status=529)
    with pytest.raises(RuntimeError, match="HTTP 529"):
        _ask(_provider(up))
    assert len(up.requests) == 3  # 1 appel + max_retries (2)


def test_rate_limit_waits_retry_after_without_spending_a_retry(up, monkeypatch):
    slept = []
    monkeypatch.setattr("nokker.providers.anthropic.time.sleep", slept.append)
    up.reply(error_body("rate_limit_error", "trop de requêtes"), status=429, headers={"retry-after": "3"})
    up.reply(message([{"type": "text", "text": "ok"}]))
    comp = _ask(_provider(up))
    assert comp.message.content == "ok" and comp.attempts == 2 and slept == [3.0]


def test_rate_limit_longer_than_the_budget_gives_up(up, monkeypatch):
    monkeypatch.setattr("nokker.providers.anthropic.time.sleep", lambda s: None)
    for _ in range(4):
        up.reply(error_body("rate_limit_error", "trop de requêtes"), status=429, headers={"retry-after": "600"})
    with pytest.raises(RuntimeError, match="HTTP 429"):
        _ask(_provider(up))
    assert len(up.requests) == 3  # jamais d'attente de 600 s : les réessais ordinaires, puis l'abandon


def test_non_json_error_body_is_reported(up):
    up.reply_raw(502, b"<html>Bad Gateway</html>")
    up.reply_raw(502, b"<html>Bad Gateway</html>")
    up.reply_raw(502, b"<html>Bad Gateway</html>")
    with pytest.raises(RuntimeError, match="HTTP 502"):
        _ask(_provider(up))


def test_stream_error_event_before_any_piece_is_retried(up):
    ok = message([{"type": "text", "text": "Bonjour."}])
    up.reply_stream([stream_of(ok)[0], {"type": "error", "error": {"type": "overloaded_error",
                                                                  "message": "Overloaded"}}])
    up.reply_stream(stream_of(ok))
    pieces = []
    comp = _ask(_provider(up), on_delta=pieces.append)
    assert comp.message.content == "Bonjour." and comp.attempts == 2 and "".join(pieces) == "Bonjour."


def test_stream_error_after_a_piece_is_never_replayed(up):
    events = stream_of(message([{"type": "text", "text": "Bonjour à tous."}]))
    cut_at = next(i for i, e in enumerate(events) if e["type"] == "content_block_delta") + 1
    up.reply_stream(events[:cut_at] + [{"type": "error", "error": {"type": "overloaded_error", "message": "x"}}])
    up.reply_stream(events[:cut_at], cut=True)
    p = _provider(up)
    for _ in range(2):  # événement `error`, puis connexion fermée sans `message_stop`
        with pytest.raises(StreamInterrupted):
            _ask(p, on_delta=lambda s: None)
    assert len(up.requests) == 2


def test_stream_http_error_and_cut_before_any_piece(up):
    ok = message([{"type": "text", "text": "ok"}])
    up.reply(error_body("overloaded_error", "Overloaded"), status=529)
    up.reply_stream(stream_of(ok)[:1], cut=True)  # `message_start` puis plus rien
    up.reply_stream(stream_of(ok))
    comp = _ask(_provider(up), on_delta=lambda s: None)
    assert comp.message.content == "ok" and comp.attempts == 3


def test_connection_refused_is_retried_then_reported():
    p = AnthropicProvider(base_url="http://127.0.0.1:9", api_key=KEY, model=MODEL, retry_backoff_s=0.0)
    with pytest.raises(RuntimeError, match="échec réseau après 3 tentative"):
        _ask(p)


# ───────────────────────────── port du noyau ─────────────────────────────


def test_provider_declares_the_optional_keywords_of_the_port(up):
    p = _provider(up)
    assert accepts_on_delta(p) and accepts_on_delta(p, "on_tool_delta") and accepts_on_delta(p, "stable_prefix")
    twin = p.detached()
    assert isinstance(twin, AnthropicProvider) and twin._client is not p._client
    twin.close()
    with pytest.raises(RuntimeError, match="coupé"):
        _ask(twin)
    up.reply(message([{"type": "text", "text": "ok"}]))
    assert _ask(p).message.content == "ok"  # le jumeau fermé ne coupe pas l'original


def test_a_tool_loaded_after_the_base_keeps_a_cache_point_on_the_previous_last_tool(up):
    """`tools` précède `system` côté Anthropic : quand le jeu grandit d'un outil chargé à la suite, le dernier outil de
    l'appel précédent garde son point de cache (le bloc d'avant reste lu) ; sans ajout, un seul point, comme avant."""
    p = _provider(up, cache="prefix")
    body1 = {}
    p._mark_base_tools(body1, "s")  # sans outils : rien
    assert body1 == {}
    specs1 = [{"name": "a"}, {"name": "b", "cache_control": {"type": "ephemeral"}}]
    p._mark_base_tools({"tools": specs1}, "s")
    specs2 = [{"name": "a"}, {"name": "b"}, {"name": "c", "cache_control": {"type": "ephemeral"}}]
    p._mark_base_tools({"tools": specs2}, "s")
    assert "cache_control" in specs2[1] and "cache_control" in specs2[2] and "cache_control" not in specs2[0]
    specs3 = [{"name": "a"}, {"name": "b"}, {"name": "c", "cache_control": {"type": "ephemeral"}}]
    p._mark_base_tools({"tools": specs3}, "s")  # même jeu : un seul point
    assert "cache_control" not in specs3[1]
    other = [{"name": "z"}, {"name": "y"}]  # un autre fil n'est pas touché par l'historique du premier
    p._mark_base_tools({"tools": other}, "autre")
    assert all("cache_control" not in s for s in other)
