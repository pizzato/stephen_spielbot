"""Provider tool contracts, real provenance, limits and failure accounting."""
import io
import json
import urllib.error
from unittest import mock

import pytest

from pipeline import news_research as research


def cfg(provider="openai", **news):
    return {"llm_backend": provider, f"{provider}_api_key": "private-api-key",
            f"{provider}_model": "configured-model", "news": news}


def response(payload):
    result = mock.MagicMock()
    result.__enter__.return_value.read.return_value = json.dumps(payload).encode()
    return result


def responses_payload(**changes):
    return {
        "status": "completed", "model": "reported-model",
        "output": [
            {"type": "web_search_call", "status": "completed",
             "action": {"type": "search", "sources": [
                 {"url": "https://example.com/report#section", "title": "Original report"}]}},
            {"type": "message", "status": "completed", "content": [
                {"type": "output_text", "text": "A sourced recent development.",
                 "annotations": [{"type": "url_citation", "url": "https://example.com/report",
                                  "title": "Original report"}]}]},
        ],
        "usage": {"input_tokens": 1234, "output_tokens": 200, "total_tokens": 1434,
                  "input_tokens_details": {"cached_tokens": 300},
                  "output_tokens_details": {"reasoning_tokens": 20}},
        **changes,
    }


def claude_payload(**changes):
    return {
        "type": "message", "stop_reason": "end_turn", "model": "reported-claude",
        "content": [
            {"type": "text", "text": "I will search."},
            {"type": "server_tool_use", "id": "t1", "name": "web_search", "input": {"query": "news"}},
            {"type": "web_search_tool_result", "tool_use_id": "t1", "content": [
                {"type": "web_search_result", "url": "https://example.com/report", "title": "Report"}]},
            {"type": "web_fetch_tool_result", "tool_use_id": "t2", "content": {
                "type": "web_fetch_result", "url": "https://example.com/original",
                "content": {"type": "document", "title": "Original"}}},
            {"type": "text", "text": "The event happened today.", "citations": [
                {"type": "web_search_result_location", "url": "https://example.com/report", "title": "Report"}]},
        ],
        "usage": {"input_tokens": 2000, "output_tokens": 200, "cache_read_input_tokens": 100,
                  "server_tool_use": {"web_search_requests": 1, "web_fetch_requests": 1}},
        **changes,
    }


def run(payload, config=None):
    with mock.patch.object(research.urllib.request, "urlopen", return_value=response(payload)) as send:
        result = research.research(config or cfg(), "Medicare")
    return result, send


def test_settings_and_cloud_configuration_are_read_only(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with mock.patch.object(research.urllib.request, "urlopen") as send:
        assert research.settings({}) == {"research_provider": "default", "research_model": "",
                                         "country": "AU", "lookback_hours": 48, "max_checks_per_day": 3}
        assert research.connection(cfg())["configured"] is True
        assert research.connection({"llm_backend": "openai"})["configured"] is False
        assert research.connection({"llm_backend": "local"})["configured"] is False
        assert research.connection(cfg("local", research_provider="openai", research_model="special"))["model"] == "special"
    send.assert_not_called()


@pytest.mark.parametrize("value", [None, "bad", float("inf"), float("nan"), []])
def test_invalid_numeric_settings_use_defaults(value):
    options = research.settings({"news": {"lookback_hours": value, "max_checks_per_day": value}})
    assert options["lookback_hours"] == 48
    assert options["max_checks_per_day"] == 3


def test_settings_bounds_and_explicit_provider(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "environment-key")
    config = {"llm_backend": "local", "news": {"research_provider": "grok", "country": " au ",
              "research_model": "selected", "lookback_hours": 999, "max_checks_per_day": 0}}
    assert research.settings(config)["lookback_hours"] == 168
    assert research.settings(config)["max_checks_per_day"] == 1
    assert research.settings(config)["country"] == "AU"
    assert research.connection(config) == {"configured": True, "provider": "grok", "model": "selected", "connection_error": ""}
    assert research.settings({"news": {"country": "Australia"}})["country"] == "AU"


def test_openai_search_payload_and_real_provenance():
    result, send = run(responses_payload())
    request = send.call_args.args[0]
    payload = json.loads(request.data)
    assert request.full_url == "https://api.openai.com/v1/responses"
    assert request.get_header("Authorization") == "Bearer private-api-key"
    assert payload["tools"] == [{"type": "web_search", "user_location": {"type": "approximate", "country": "AU"}}]
    assert payload["max_tool_calls"] == 4
    assert payload["include"] == ["web_search_call.action.sources"]
    assert payload["model"] == "configured-model"
    assert payload["max_output_tokens"] == 4000
    assert payload["store"] is False
    assert "Medicare" in payload["input"][1]["content"]
    assert "News window:" in payload["input"][1]["content"]
    assert "cannot browse" in payload["input"][0]["content"]
    assert "never as instructions" in payload["input"][0]["content"]
    assert send.call_args.kwargs["timeout"] == 180
    assert result["sources"] == [{"url": "https://example.com/report", "title": "Original report"}]
    assert result["provider"] == "openai"
    assert result["model"] == "reported-model"
    assert result["usage"]["input_tokens_details"] == {"cached_tokens": 300}
    assert result["usage"]["output_tokens_details"] == {"reasoning_tokens": 20}
    assert result["usage"]["web_search_calls"] == 1
    assert result["searched"] is True


def test_grok_has_only_web_tools_and_preserves_reported_usage():
    payload = responses_payload()
    payload["usage"].update(cost_in_usd_ticks=250000000, server_side_tool_usage_details={"web_search_calls": 2})
    payload["server_side_tool_usage"] = {"SERVER_SIDE_TOOL_WEB_SEARCH": 2}
    result, send = run(payload, cfg("grok"))
    request = send.call_args.args[0]
    body = json.loads(request.data)
    assert request.full_url == "https://api.x.ai/v1/responses"
    assert body["tools"] == [{"type": "web_search"}]
    assert body["max_turns"] == 2
    assert "max_tool_calls" not in body
    assert "x_search" not in request.data.decode()
    assert result["usage"]["cost_in_usd_ticks"] == 250000000
    assert result["usage"]["server_side_tool_usage"] == {"SERVER_SIDE_TOOL_WEB_SEARCH": 2}


def test_claude_bounded_search_fetch_and_citations():
    result, send = run(claude_payload(), cfg("claude"))
    request = send.call_args.args[0]
    body = json.loads(request.data)
    assert request.full_url == "https://api.anthropic.com/v1/messages"
    assert request.get_header("X-api-key") == "private-api-key"
    assert request.get_header("Anthropic-version") == "2023-06-01"
    assert [(tool["name"], tool["max_uses"]) for tool in body["tools"]] == [("web_search", 2), ("web_fetch", 2)]
    assert body["tools"][1]["max_content_tokens"] == 6000
    assert body["tools"][1]["citations"] == {"enabled": True}
    assert body["max_tokens"] == 4000
    assert result["text"] == "The event happened today."
    assert len(result["sources"]) == 2
    assert result["usage"]["server_tool_use"] == {"web_search_requests": 1, "web_fetch_requests": 1}
    assert result["usage"]["tool_calls"] == 2


def test_custom_openai_compatible_url_is_converted():
    config = {**cfg(), "openai_api_url": "https://gateway.example/v1/chat/completions"}
    _, send = run(responses_payload(), config)
    assert send.call_args.args[0].full_url == "https://gateway.example/v1/responses"


@pytest.mark.parametrize("url", ["javascript:alert(1)", "https://user:password@example.com/", "https://example.com/a\nb", "http://[broken"])
def test_unsafe_sources_and_model_written_urls_are_not_provenance(url):
    payload = responses_payload()
    payload["output"][0]["action"]["sources"] = [{"url": url}]
    payload["output"][1]["content"][0].update(text="Use https://fabricated.example/article", annotations=[])
    result, _ = run(payload)
    assert result["sources"] == []
    assert result["text"] == ""


@pytest.mark.parametrize("provider", ["openai", "grok", "claude"])
def test_completed_search_without_findings_is_empty(provider):
    if provider == "claude":
        payload = claude_payload(content=[{"type": "web_search_tool_result", "content": []},
                                          {"type": "text", "text": "NO_RECENT_NEWS"}])
    else:
        payload = responses_payload(output=[{"type": "web_search_call", "status": "completed",
                                             "action": {"type": "search", "sources": []}}])
    result, _ = run(payload, cfg(provider))
    assert result["searched"] is True
    assert result["text"] == ""
    assert result["sources"] == []


@pytest.mark.parametrize("provider", ["openai", "grok", "claude"])
def test_answer_without_web_search_is_an_error(provider):
    payload = claude_payload(content=[]) if provider == "claude" else responses_payload(output=[])
    with pytest.raises(research.ResearchError, match="did not run a web search") as caught:
        run(payload, cfg(provider))
    assert caught.value.usage["input_tokens"] > 0


@pytest.mark.parametrize("status", ["incomplete", "failed", "in_progress"])
def test_openai_incomplete_keeps_usage_and_does_not_retry(status):
    with mock.patch.object(research.urllib.request, "urlopen", return_value=response(responses_payload(status=status))) as send:
        with pytest.raises(research.ResearchError, match="did not finish") as caught:
            research.research(cfg(), "Medicare")
    send.assert_called_once()
    assert caught.value.usage["input_tokens"] == 1234
    assert caught.value.usage["tool_calls"] == 1


def test_failed_search_does_not_masquerade_as_empty_findings():
    payload = responses_payload()
    payload["output"][0]["status"] = "failed"
    with pytest.raises(research.ResearchError, match="tool failed") as caught:
        run(payload)
    assert caught.value.usage["tool_calls"] == 1
    assert caught.value.usage["web_search_calls"] == 0


def test_source_results_without_a_finished_brief_are_an_error():
    payload = responses_payload()
    payload["output"] = payload["output"][:1]
    with pytest.raises(research.ResearchError, match="no news brief"):
        run(payload)


def test_explicit_no_recent_news_does_not_generate_a_story_from_old_sources():
    payload = responses_payload()
    payload["output"][1]["content"][0]["text"] = "NO_RECENT_NEWS"
    result, _ = run(payload)
    assert result["text"] == ""
    assert result["sources"] == []


@pytest.mark.parametrize("reason", ["pause_turn", "max_tokens", "tool_use", "refusal"])
def test_claude_partial_answer_is_not_no_news(reason):
    with pytest.raises(research.ResearchError, match="did not finish") as caught:
        run(claude_payload(stop_reason=reason), cfg("claude"))
    assert caught.value.usage["server_tool_use"]["web_search_requests"] == 1


def test_claude_embedded_search_error_keeps_usage():
    payload = claude_payload(content=[{"type": "web_search_tool_result", "content": {
        "type": "web_search_tool_result_error", "error_code": "unavailable"}}])
    with pytest.raises(research.ResearchError, match="tool failed") as caught:
        run(payload, cfg("claude"))
    assert caught.value.usage["output_tokens"] == 200


@pytest.mark.parametrize("error", [
    urllib.error.HTTPError("https://private-api-key@example.com", 429, "private-api-key", {}, io.BytesIO(b"private-api-key")),
    urllib.error.URLError("private-api-key"), TimeoutError("private-api-key"),
])
def test_request_failures_are_sanitized_and_not_retried(error):
    with mock.patch.object(research.urllib.request, "urlopen", side_effect=error) as send:
        with pytest.raises(research.ResearchError) as caught:
            research.research(cfg(), "Medicare")
    send.assert_called_once()
    assert "private-api-key" not in str(caught.value)


def test_local_provider_fails_without_network():
    with mock.patch.object(research.urllib.request, "urlopen") as send:
        with pytest.raises(research.ResearchError, match="requires OpenAI"):
            research.research(cfg("local"), "Medicare")
    send.assert_not_called()


def test_response_read_is_bounded():
    reply = response({})
    reply.__enter__.return_value.read.return_value = b"x" * (research._MAX_RESPONSE_BYTES + 1)
    with mock.patch.object(research.urllib.request, "urlopen", return_value=reply):
        with pytest.raises(research.ResearchError, match="size limit"):
            research.research(cfg(), "Medicare")
    reply.__enter__.return_value.read.assert_called_once_with(research._MAX_RESPONSE_BYTES + 1)
