"""News reuses connected X credentials without copying or exposing tokens."""
import json
import time
from unittest import mock

import pytest
import requests

import app
from pipeline import news, x as xt
from webapp.backend import news_monitor


@pytest.fixture(autouse=True)
def isolate_credentials(monkeypatch):
    monkeypatch.delenv("X_BEARER_TOKEN", raising=False)
    with mock.patch.object(xt, "_load_token", return_value=None), \
            mock.patch.object(xt, "_save_token"), \
            mock.patch.object(xt.requests, "request", side_effect=AssertionError("unexpected network")), \
            mock.patch.object(xt.requests, "post", side_effect=AssertionError("unexpected refresh")):
        yield


def config(*accounts, selected="", bearer=""):
    return {"x_accounts": [{"id": key, "name": f"User {key}"} for key in accounts],
            "x_client_id": "client-id", "x_client_secret": "client-secret",
            "news": {"x_account": selected, "x_bearer_token": bearer}}


def oauth2(**overrides):
    return {"access_token": "user-secret", "expires_at": time.time() + 3600, **overrides}


def search_response(payload=None):
    response = mock.MagicMock()
    response.__enter__.return_value = response
    response.status_code = 200
    response.iter_content.return_value = [json.dumps(payload or {
        "data": [{"id": "101", "text": "Latest report"}],
    }).encode()]
    return response


def test_single_connected_account_is_detected_without_refresh_or_network():
    cfg = config("alice")
    with mock.patch.object(xt, "_load_token", return_value=oauth2()) as load, \
            mock.patch.object(xt, "_account_auth") as auth:
        result = news.search_connection(cfg)
    assert result == {"configured": True, "auth_source": "account", "account": "alice",
                      "account_name": "User alice", "connection_error": ""}
    load.assert_called_once_with("alice")
    auth.assert_not_called()
    xt.requests.request.assert_not_called()
    xt.requests.post.assert_not_called()
    xt._save_token.assert_not_called()


@pytest.mark.parametrize("bearer", ["manual-secret", ""])
def test_explicit_account_takes_precedence_over_manual_and_environment_tokens(monkeypatch, bearer):
    monkeypatch.setenv("X_BEARER_TOKEN", "env-secret")
    cfg = config("alice", "bob", selected="bob", bearer=bearer)
    with mock.patch.object(xt, "_load_token", return_value=oauth2()) as load:
        result = news.search_connection(cfg)
    assert result["account"] == "bob"
    assert result["auth_source"] == "account"
    load.assert_called_once_with("bob")


def test_existing_bearer_override_does_not_read_account_credentials():
    result = news.search_connection(config("alice", "bob", bearer="manual-secret"))
    assert result["configured"] is True
    assert result["auth_source"] == "bearer"
    assert result["account"] == ""
    xt._load_token.assert_not_called()


def test_multiple_accounts_require_choice_without_silently_using_first():
    result = news.search_connection(config("alice", "bob"))
    assert result["configured"] is False
    assert "Choose an X account" in result["connection_error"]
    xt._load_token.assert_not_called()


def test_removed_explicit_account_does_not_fall_back_to_other_account_or_token():
    cfg = config("alice", selected="removed", bearer="manual-secret")
    result = news.search_connection(cfg)
    assert result["configured"] is False
    assert result["account"] == "removed"
    assert "no longer connected" in result["connection_error"]
    with pytest.raises(news.NewsError, match="no longer connected"):
        news.fetch_recent_posts(cfg, "rail")
    xt._load_token.assert_not_called()


@pytest.mark.parametrize("token", [None, {}, {"auth": "oauth1", "api_key": "key"},
                                  oauth2(expires_at=0), oauth2(expires_at="invalid")])
def test_missing_or_expired_unrefreshable_connection_requires_reconnect(token):
    with mock.patch.object(xt, "_load_token", return_value=token):
        result = news.search_connection(config("alice"))
    assert result["configured"] is False
    assert "Reconnect" in result["connection_error"]


def test_client_id_and_secret_alone_still_require_connecting_account():
    result = news.search_connection(config())
    assert result["configured"] is False
    assert "Connect an X account" in result["connection_error"]
    xt._load_token.assert_not_called()


def test_expired_oauth2_with_refresh_is_configured_but_status_does_not_refresh():
    with mock.patch.object(xt, "_load_token", return_value=oauth2(
            expires_at=0, refresh_token="refresh-secret")):
        result = news.search_connection(config("alice"))
    assert result["configured"] is True
    xt.requests.post.assert_not_called()
    xt._save_token.assert_not_called()


def test_refreshable_account_needs_client_id_for_refresh():
    cfg = config("alice")
    cfg["x_client_id"] = ""
    with mock.patch.object(xt, "_load_token", return_value=oauth2(
            expires_at=0, refresh_token="refresh-secret")):
        assert news.search_connection(cfg)["configured"] is False


def test_connected_oauth2_search_uses_existing_refresh_and_persists_rotation():
    token = oauth2(expires_at=0, refresh_token="old-refresh", account_id="alice")
    refreshed = mock.Mock()
    refreshed.json.return_value = {"access_token": "new-secret", "refresh_token": "new-refresh",
                                  "expires_in": 7200}
    with mock.patch.object(xt, "_load_token", return_value=token), \
            mock.patch.object(xt.requests, "post", return_value=refreshed) as refresh, \
            mock.patch.object(xt.requests, "request", return_value=search_response()) as send:
        result = news.fetch_recent_posts(config("alice"), "rail lang:en", since_id="99")
    assert result["newest_id"] == "101"
    assert refresh.call_args.args == (xt.TOKEN_URL,)
    assert refresh.call_args.kwargs["data"]["refresh_token"] == "old-refresh"
    assert send.call_args.args == ("GET", "https://api.x.com/2/tweets/search/recent")
    assert send.call_args.kwargs["headers"]["Authorization"] == "Bearer new-secret"
    assert send.call_args.kwargs["params"]["since_id"] == "99"
    assert send.call_args.kwargs["stream"] is True
    assert send.call_args.kwargs["allow_redirects"] is False
    saved_account, saved_token = xt._save_token.call_args.args
    assert saved_account == "alice"
    assert saved_token["refresh_token"] == "new-refresh"
    assert saved_token["account_id"] == "alice"


def test_oauth1_connection_uses_existing_request_signer():
    from requests_oauthlib import OAuth1

    token = {"auth": "oauth1", "api_key": "key", "api_secret": "secret",
             "access_token": "access", "access_secret": "access-secret"}
    with mock.patch.object(xt, "_load_token", return_value=token), \
            mock.patch.object(xt.requests, "request", return_value=search_response()) as send:
        assert news.fetch_recent_posts(config("alice"), "rail")["posts"]
    assert isinstance(send.call_args.kwargs["auth"], OAuth1)
    assert "Authorization" not in send.call_args.kwargs["headers"]
    xt.requests.post.assert_not_called()


def test_failed_auth_refresh_is_actionable_and_does_not_expose_exception():
    with mock.patch.object(xt, "_load_token", return_value=oauth2()), \
            mock.patch.object(xt, "_account_auth", side_effect=RuntimeError("private-secret")):
        with pytest.raises(news.NewsError, match="Reconnect") as caught:
            news.fetch_recent_posts(config("alice"), "rail")
    assert "private-secret" not in str(caught.value)
    xt.requests.request.assert_not_called()


@pytest.mark.parametrize("status,hint", [(401, "Reconnect"), (403, "recent-search access"),
                                        (429, "rate or usage limit"), (503, "retry later")])
def test_connected_search_errors_are_safe(status, hint):
    response = search_response()
    response.status_code = status
    response.raise_for_status.side_effect = requests.HTTPError("private-secret", response=response)
    with mock.patch.object(xt, "_load_token", return_value=oauth2()), \
            mock.patch.object(xt.requests, "request", return_value=response):
        with pytest.raises(news.NewsError, match=hint) as caught:
            news.fetch_recent_posts(config("alice"), "rail")
    assert "private-secret" not in str(caught.value)
    assert caught.value.__suppress_context__ is True


def test_connected_search_keeps_response_size_bound():
    response = search_response()
    response.iter_content.return_value = iter([b"x" * 1_000_000] * 4)
    with mock.patch.object(xt, "_load_token", return_value=oauth2()), \
            mock.patch.object(xt.requests, "request", return_value=response):
        with pytest.raises(news.NewsError, match="invalid response"):
            news.fetch_recent_posts(config("alice"), "rail")
    assert next(response.iter_content.return_value) == b"x" * 1_000_000
    response.__exit__.assert_called_once()


def test_connected_search_http_error_without_response_is_safe():
    with mock.patch.object(xt, "_load_token", return_value=oauth2()), \
            mock.patch.object(xt.requests, "request", side_effect=requests.HTTPError("private-secret")):
        with pytest.raises(news.NewsError, match="retry later") as caught:
            news.fetch_recent_posts(config("alice"), "rail")
    assert "private-secret" not in str(caught.value)


def test_status_exposes_connection_metadata_without_credentials_or_refresh():
    cfg = {**config("alice"), "styles": [{"name": "News"}], "default_style": "News"}
    with mock.patch.object(xt, "_load_token", return_value=oauth2(refresh_token="refresh-secret")), \
            mock.patch.object(news_monitor, "_read_state", return_value={}):
        result = news_monitor.status(cfg)
    assert result["configured"] is True
    assert result["account"] == "alice"
    assert result["auth_source"] == "account"
    assert "secret" not in json.dumps(result)
    xt.requests.request.assert_not_called()
    xt.requests.post.assert_not_called()


def test_news_account_config_updates_preserve_secret_and_publishing_mapping():
    current = {**config("alice", selected="alice", bearer="manual-secret"),
               "x_account": "publisher", "styles": [{"name": "News", "x_account": "publisher"}]}
    merged = app.merge_config_update(current, {"news": {
        "x_account": " bob ", "x_bearer_token": "", "x_bearer_token_set": True,
        "configured": True, "account": "injected", "connection_error": "injected"}})
    assert merged["news"] == {"x_account": "bob", "x_bearer_token": "manual-secret"}
    assert current["news"]["x_account"] == "alice"
    assert merged["x_account"] == "publisher"
    assert merged["styles"][0]["x_account"] == "publisher"
    cleared = app.merge_config_update(merged, {"news": {"x_account": ""}})
    assert cleared["news"] == {"x_account": "", "x_bearer_token": "manual-secret"}
