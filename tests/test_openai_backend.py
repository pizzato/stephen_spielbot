"""Provider request compatibility and actionable OpenAI failures."""
import io
import json
import urllib.error
from unittest import mock

import pytest

from pipeline import llm


def response():
    result = mock.MagicMock()
    result.__enter__.return_value.read.return_value = json.dumps({
        "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
    }).encode()
    return result


@pytest.mark.parametrize("backend,model,token_field,temperature,reasoning", [
    ("openai", "gpt-5.6-luna", "max_completion_tokens", None, "none"),
    ("openai", "gpt-5-mini", "max_completion_tokens", None, "none"),
    ("openai", "o3", "max_completion_tokens", None, "none"),
    ("openai", "gpt-4o", "max_completion_tokens", 0.7, None),
    ("grok", "grok-4.5", "max_tokens", 0.7, None),
])
@pytest.mark.parametrize("script", [False, True])
def test_provider_payloads(backend, model, token_field, temperature, reasoning, script):
    # Exercise both one-shot calls (including the critic) and script batches.
    def batch(title, n_scenes, style_hint, call_fn, **kwargs):
        return call_fn("system", "user", 100, "test", retries=1)

    cfg = {"llm_backend": backend, f"{backend}_api_key": "test-key",
           f"{backend}_model": model}
    with mock.patch.object(llm.urllib.request, "urlopen", return_value=response()) as send, \
            mock.patch.object(llm, "_load_cfg", return_value=cfg), \
            mock.patch.object(llm, "_json_script_generate", side_effect=batch):
        if script:
            result = llm.generate_script("Topic", 3)
        else:
            result = llm._chat_complete(cfg, "system", "user", 100, "test", retries=1)
    assert result == "OK"
    payload = json.loads(send.call_args.args[0].data)
    expected = {"model": model, token_field: 100, "messages": [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "user"},
    ]}
    if temperature is not None:
        expected["temperature"] = temperature
    if reasoning is not None:
        expected["reasoning_effort"] = reasoning
    assert payload == expected


@pytest.mark.parametrize("configured,expected", [
    (None, "none"),        # unset — the shipped default turns thinking off
    ("high", "high"),      # an explicit level is sent through
    ("", None),            # emptied — the parameter is left off the request
])
def test_reasoning_effort_setting(configured, expected):
    cfg = {"llm_backend": "openai", "openai_api_key": "test-key",
           "openai_model": "gpt-5.6-luna"}
    if configured is not None:
        cfg["openai_reasoning_effort"] = configured
    with mock.patch.object(llm.urllib.request, "urlopen", return_value=response()):
        llm._chat_complete(cfg, "system", "user", 100, "test", retries=1)
        payload = json.loads(llm.urllib.request.urlopen.call_args.args[0].data)
    assert payload.get("reasoning_effort") == expected


def test_reasoning_effort_left_off_non_reasoning_models():
    """gpt-4o and the Grok path reject the parameter — they never see it."""
    for backend, model in (("openai", "gpt-4o"), ("grok", "grok-4.5")):
        cfg = {"llm_backend": backend, f"{backend}_api_key": "test-key",
               f"{backend}_model": model, "openai_reasoning_effort": "high"}
        with mock.patch.object(llm.urllib.request, "urlopen", return_value=response()):
            llm._chat_complete(cfg, "system", "user", 100, "test", retries=1)
            payload = json.loads(llm.urllib.request.urlopen.call_args.args[0].data)
        assert "reasoning_effort" not in payload


def test_claude_still_streams_with_max_tokens():
    client = mock.MagicMock()
    stream = client.messages.stream.return_value.__enter__.return_value
    stream.text_stream = iter([" O", "K "])
    stream.get_final_message.return_value.stop_reason = "end_turn"
    with mock.patch("anthropic.Anthropic", return_value=client):
        assert llm._chat_complete({
            "llm_backend": "claude", "claude_api_key": "test-key",
            "claude_model": "claude-sonnet-4-6",
        }, "system", "user", 100, "test", retries=1) == "OK"
    client.messages.stream.assert_called_once_with(
        model="claude-sonnet-4-6", max_tokens=100, system="system",
        messages=[{"role": "user", "content": "user"}],
    )


@pytest.mark.parametrize("body,expected", [
    (json.dumps({"error": {"message": "Unsupported parameter: max_tokens"}}).encode(),
     "Unsupported parameter: max_tokens"),
    (b"<html>Bad gateway</html>", "Bad Request"),
    (b'{"error":{"message":"Invalid test-key"}}', "Invalid [redacted]"),
])
def test_openai_bad_request_is_actionable_and_not_retried(body, expected):
    error = urllib.error.HTTPError("https://api.openai.com", 400, "Bad Request", {}, io.BytesIO(body))
    with mock.patch.object(llm.urllib.request, "urlopen", side_effect=error) as send, \
            mock.patch("time.sleep") as sleep:
        with pytest.raises(RuntimeError, match="OpenAI HTTP 400") as caught:
            llm._chat_complete({"llm_backend": "openai", "openai_api_key": "test-key"},
                               "system", "user", 100, "test")
    assert expected in str(caught.value)
    send.assert_called_once()
    sleep.assert_not_called()


@pytest.mark.parametrize("status", [429, 500])
def test_openai_transient_error_still_retries(status):
    error = urllib.error.HTTPError("https://api.openai.com", status, "Try again", {},
                                   io.BytesIO(b'{"error":{"message":"Try again"}}'))
    with mock.patch.object(llm.urllib.request, "urlopen", side_effect=[error, response()]) as send, \
            mock.patch("time.sleep") as sleep:
        assert llm._chat_complete({"llm_backend": "openai", "openai_api_key": "test-key"},
                                  "system", "user", 100, "test", retries=2) == "OK"
    assert send.call_count == 2
    sleep.assert_called_once_with(10)
