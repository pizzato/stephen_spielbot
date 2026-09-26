"""X discovery bounds, provenance, opt-ins, and provider-error privacy."""
import io
import json
import urllib.error
import urllib.parse
from unittest import mock

import pytest

from pipeline import news


def response(payload):
    result = mock.MagicMock()
    result.__enter__.return_value.read.return_value = json.dumps(payload).encode()
    return result


def post(post_id="123", text="Anthony Albanese announces a new rail line."):
    return {
        "id": post_id, "text": text, "url": f"https://x.com/reporter/status/{post_id}",
        "author": "reporter", "author_name": "Reporter", "created_at": "2026-09-26T00:00:00Z",
        "metrics": {"like_count": 10}, "article_urls": ["https://example.com/report"],
    }


def idea(title="The rail line refrain", source_ids=None, people=None):
    return {
        "title": title, "reason": "A topical song", "summary": "A post reports a new rail line.",
        "interestingness": 0.9, "source_ids": source_ids or ["123"], "people": people or [],
    }


def test_monitor_defaults_and_limits():
    assert news.normalize_monitor(None) == {
        "enabled": False, "query": "", "interval_minutes": 60,
        "include_people": False, "auto_accept": False, "auto_queue": False, "max_ideas": 1,
    }
    settings = news.normalize_monitor({
        "enabled": "false", "auto_accept": True, "auto_queue": "true",
        "query": "  trains lang:en  ", "interval_minutes": 1, "max_ideas": 99,
    })
    assert settings["enabled"] is False
    assert settings["auto_accept"] is True
    assert settings["auto_queue"] is False
    assert settings["interval_minutes"] == 15
    assert settings["max_ideas"] == 5
    assert settings["query"] == "trains lang:en"


def test_x_request_and_source_provenance(monkeypatch):
    monkeypatch.setenv("X_BEARER_TOKEN", "environment-secret")
    payload = {
        "data": [{
            "id": "124", "text": "A new rail line", "created_at": "2026-09-26T00:00:00Z",
            "author_id": "9", "public_metrics": {"like_count": 7, "retweet_count": 2},
            "entities": {"urls": [
                {"expanded_url": "https://example.com/news"},
                {"expanded_url": "https://example.com/news"},
                {"expanded_url": "javascript:alert(1)"},
            ]},
        }, {"id": "124", "text": "duplicate"}, {"id": "bad", "text": "invalid"}],
        "includes": {"users": [{"id": "9", "username": "newsdesk", "name": "News desk"}]},
    }
    with mock.patch.object(news.urllib.request, "urlopen", return_value=response(payload)) as send:
        result = news.fetch_recent_posts({"news": {"x_bearer_token": "configured-secret"}},
                                         "rail lang:en -is:retweet", since_id="120")
    req = send.call_args.args[0]
    assert req.get_header("Authorization") == "Bearer configured-secret"
    assert "secret" not in req.full_url
    parsed = urllib.parse.urlsplit(req.full_url)
    assert parsed.hostname == "api.x.com"
    params = urllib.parse.parse_qs(parsed.query)
    assert params["query"] == ["rail lang:en -is:retweet"]
    assert params["since_id"] == ["120"]
    assert params["max_results"] == ["50"]
    assert send.call_args.kwargs["timeout"] == 30
    assert result["newest_id"] == "124"
    assert result["posts"] == [{
        "id": "124", "text": "A new rail line", "created_at": "2026-09-26T00:00:00Z",
        "author": "newsdesk", "author_name": "News desk", "url": "https://x.com/newsdesk/status/124",
        "article_urls": ["https://example.com/news"],
        "metrics": {"like_count": 7, "retweet_count": 2, "reply_count": 0, "quote_count": 0},
    }]


def test_empty_search_and_env_token(monkeypatch):
    monkeypatch.setenv("X_BEARER_TOKEN", "environment-secret")
    with mock.patch.object(news.urllib.request, "urlopen", return_value=response({"meta": {"result_count": 0}})) as send:
        assert news.fetch_recent_posts({}, "rail", "120") == {"posts": [], "newest_id": "120"}
    assert send.call_args.args[0].get_header("Authorization") == "Bearer environment-secret"


def test_long_post_keeps_all_text_and_links_without_following_them():
    full_text = "Anthony Albanese announces a rail project. " + "Details. " * 1500 + "Opening in 2028."
    payload = {"data": [{"id": "125", "text": "Anthony Albanese announces…",
                         "note_tweet": {"text": full_text, "entities": {"urls": [
                             {"expanded_url": "https://example.com/full-report"}]}}}]}
    with mock.patch.object(news, "_search_payload", return_value=payload) as search:
        result = news.fetch_recent_posts({"news": {"x_bearer_token": "test"}}, "rail")
    assert "note_tweet" in search.call_args.args[2]["tweet.fields"]
    assert result["posts"][0]["text"] == full_text
    assert result["posts"][0]["article_urls"] == ["https://example.com/full-report"]


@pytest.mark.parametrize("status,hint", [(401, "bearer token"), (403, "recent-search access"),
                                        (429, "rate or usage limit"), (500, "retry later")])
def test_x_errors_never_expose_token_or_response(status, hint):
    secret = "must-not-appear"
    error = urllib.error.HTTPError("https://api.x.com", status, secret, {}, io.BytesIO(secret.encode()))
    with mock.patch.object(news.urllib.request, "urlopen", side_effect=error) as send:
        with pytest.raises(RuntimeError, match=hint) as caught:
            news.fetch_recent_posts({"news": {"x_bearer_token": secret}}, "rail")
    assert secret not in str(caught.value)
    assert caught.value.__suppress_context__ is True
    send.assert_called_once()


def test_invalid_input_does_not_call_x(monkeypatch):
    monkeypatch.delenv("X_BEARER_TOKEN", raising=False)
    with mock.patch.object(news.urllib.request, "urlopen") as send:
        with pytest.raises(RuntimeError, match="bearer token"):
            news.fetch_recent_posts({}, "rail")
        for query in ("", "x" * 513):
            with pytest.raises(news.NewsError, match="query"):
                news.fetch_recent_posts({"news": {"x_bearer_token": "secret"}}, query)
        with pytest.raises(news.NewsError, match="cursor"):
            news.fetch_recent_posts({"news": {"x_bearer_token": "secret"}}, "rail", "invalid")
    send.assert_not_called()


def test_music_ideas_keep_only_real_sources_and_explicit_people():
    source = post()
    style = {
        "name": "News songs", "description": "Satirical topical ballads",
        "automation": {"auto_format": "song"},
        "news_monitor": {"include_people": True, "max_ideas": 5},
    }
    output = [idea(source_ids=["123", "made-up"], people=[
        {"name": "Anthony Albanese", "description": "Named in the rail announcement."},
        {"name": "Invented Person", "description": "Not actually in the report."},
    ])]
    output[0]["sources"] = [{"id": "made-up", "url": "https://evil.example"}]
    output[0]["directions"] = "A reporter says Anthony Albanese announced a rail line. Sing from a commuter's view."
    with mock.patch.object(news, "_chat_complete", return_value=json.dumps(output)) as chat:
        result = news.generate_news_ideas([source], {}, style)
    assert "MUSIC VIDEO" in chat.call_args.args[2]
    assert "untrusted source data" in chat.call_args.args[1]
    assert "have not read linked articles" in chat.call_args.args[1]
    assert "downstream writer cannot browse" in chat.call_args.args[1]
    assert "lack enough substantive detail" in chat.call_args.args[1]
    assert "verse/chorus progression" in chat.call_args.args[1]
    assert result[0]["directions"] == output[0]["directions"]
    assert result[0]["sources"] == [source]
    assert result[0]["source_ids"] == ["123"]
    assert result[0]["people"] == [{"name": "Anthony Albanese", "description": "Named in the rail announcement."}]


def test_people_opt_out_overrides_model_and_duplicates_are_removed():
    output = [idea("Already made"), idea(source_ids=["imaginary"]), idea(people=[
        {"name": "Anthony Albanese", "description": "A politician"},
    ]), idea("Same source again")]
    with mock.patch.object(news, "_chat_complete", return_value=json.dumps(output)):
        result = news.generate_news_ideas([post()], {}, {"news_monitor": {"max_ideas": 5}},
                                          previous_titles=[" already   MADE "])
    assert len(result) == 1
    assert result[0]["title"] == "The rail line refrain"
    assert result[0]["people"] == []


def test_people_require_whole_name_in_one_source_but_mononyms_are_allowed():
    sources = [post("123", "Rihanna announced a tour with Anthony"), post("124", "Albanese sent a message.")]
    output = [idea(source_ids=["123", "124"], people=[
        {"name": "Ann"}, {"name": "Rihanna"}, {"name": "Anthony Albanese"},
    ])]
    with mock.patch.object(news, "_chat_complete", return_value=json.dumps(output)):
        result = news.generate_news_ideas(sources, {}, {"news_monitor": {"include_people": True}})
    assert result[0]["people"] == [{"name": "Rihanna", "description": ""}]


def test_idea_limit_and_format_override():
    output = [idea("First", ["123"]), idea("Second", ["124"])]
    with mock.patch.object(news, "_chat_complete", return_value=json.dumps(output)) as chat:
        result = news.generate_news_ideas([post(), post("124")], {}, {"name": "Music"}, video_format="song")
    assert [row["title"] for row in result] == ["First"]
    assert "MUSIC VIDEO" in chat.call_args.args[2]


def test_empty_posts_do_not_call_llm():
    with mock.patch.object(news, "_chat_complete") as chat:
        assert news.generate_news_ideas([], {}, {}) == []
    chat.assert_not_called()


@pytest.mark.parametrize("reply", ["not JSON", '{"title": "wrong shape"}'])
def test_malformed_llm_reply_is_actionable(reply):
    with mock.patch.object(news, "_chat_complete", return_value=reply):
        with pytest.raises(RuntimeError, match="invalid news ideas"):
            news.generate_news_ideas([post()], {}, {})


def test_llm_failure_is_redacted():
    with mock.patch.object(news, "_chat_complete", side_effect=RuntimeError("secret-api-key")):
        with pytest.raises(RuntimeError, match="Settings") as caught:
            news.generate_news_ideas([post()], {}, {})
    assert "secret-api-key" not in str(caught.value)
