"""Official Suno MCP client and local, browser-based account connection.

The MCP SDK owns discovery, registration, PKCE and refresh. Credentials belong
to Spielbot and stay in its private config directory, independently of Codex.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import tempfile
import threading
import time
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


MCP_URL = "https://api.suno.com/mcp"
_TOKEN_PATH = Path.home() / ".config" / "video-generator" / "suno_token.json"
_AUTH_TIMEOUT = 300
_state_lock = threading.RLock()
_call_lock = threading.Lock()
_generation = 0
_flow = None


class SunoError(RuntimeError):
    """An actionable message safe to display without OAuth credentials."""


class _OAuthLogFilter(logging.Filter):
    def filter(self, record):
        # SDK exception traces can include a token endpoint response body.
        if record.exc_info:
            record.exc_info = None
            record.exc_text = None
        return True


def _read_state() -> dict:
    try:
        data = json.loads(_TOKEN_PATH.read_text())
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def _write_state(data: dict) -> None:
    _TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".suno-", dir=_TOKEN_PATH.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, _TOKEN_PATH)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class _Storage:
    """MCP TokenStorage, with absolute expiry and discovery across restarts."""

    def __init__(self, generation: int, interactive: bool = False):
        self.generation = generation
        self.interactive = interactive
        self.context = None

    def _save(self, updates: dict) -> None:
        with _state_lock:
            if self.generation != _generation:
                raise SunoError("Suno connection was cancelled.")
            state = _read_state()
            state.update(updates)
            if self.context is not None:
                for key in ("oauth_metadata", "protected_resource_metadata"):
                    value = getattr(self.context, key)
                    if value is not None:
                        state[key] = value.model_dump(mode="json")
            _write_state(state)

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken

        data = None if self.interactive else _read_state().get("tokens")
        return OAuthToken.model_validate(data) if data else None

    async def set_tokens(self, tokens):
        data = tokens.model_dump(mode="json")
        previous = _read_state().get("tokens", {})
        # RFC 6749 allows a refresh response to omit the unchanged refresh token.
        if not data.get("refresh_token") and not self.interactive:
            data["refresh_token"] = previous.get("refresh_token")
            tokens.refresh_token = data["refresh_token"]
        expires_at = time.time() + tokens.expires_in if tokens.expires_in is not None else None
        self._save({"tokens": data, "expires_at": expires_at})

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull

        data = None if self.interactive else _read_state().get("client_info")
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def set_client_info(self, client_info):
        self._save({"client_info": client_info.model_dump(mode="json")})


def is_connected() -> bool:
    """Whether local credentials exist; account_usage verifies them remotely."""
    with _state_lock:
        data = _read_state()
        return bool(data.get("tokens", {}).get("access_token") and data.get("client_info"))


def _provider(storage, redirect_handler, callback_handler, redirect_uri):
    from mcp.client.auth import OAuthClientProvider
    from mcp.shared.auth import OAuthClientMetadata, OAuthMetadata, ProtectedResourceMetadata

    provider = OAuthClientProvider(
        server_url=MCP_URL,
        client_metadata=OAuthClientMetadata(
            client_name="Stephen Spielbot",
            redirect_uris=[redirect_uri],
            token_endpoint_auth_method="none",
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
        ),
        storage=storage,
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
        timeout=_AUTH_TIMEOUT,
    )
    storage.context = provider.context
    if not storage.interactive:
        state = _read_state()
        # SDK 1.x doesn't restore these from TokenStorage. In particular, its
        # default refresh endpoint is on the resource host, not Suno's issuer.
        provider.context.token_expiry_time = state.get("expires_at")
        if state.get("oauth_metadata"):
            provider.context.oauth_metadata = OAuthMetadata.model_validate(state["oauth_metadata"])
        if state.get("protected_resource_metadata"):
            provider.context.protected_resource_metadata = ProtectedResourceMetadata.model_validate(
                state["protected_resource_metadata"]
            )
    return provider


def _result_data(result) -> dict:
    if result.isError:
        raise SunoError("Suno could not complete this request. Check your available credits and selected model.")
    if isinstance(result.structuredContent, dict):
        return result.structuredContent
    for block in result.content:
        if getattr(block, "type", "") == "text":
            try:
                value = json.loads(block.text)
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
    raise SunoError("Suno returned an unexpected response. Please try again.")


async def _execute(name: str, arguments: dict, flow=None) -> dict:
    try:
        import httpx
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
    except ImportError:
        raise SunoError("Install Spielbot's updated Python requirements to connect Suno.") from None

    logger = logging.getLogger("mcp.client.auth.oauth2")
    if not any(isinstance(item, _OAuthLogFilter) for item in logger.filters):
        logger.addFilter(_OAuthLogFilter())

    async def no_interaction(*args):
        storage._save({"tokens": {}, "expires_at": None})
        raise SunoError("Sign in to Suno again before generating music.")

    storage = _Storage(flow.generation if flow else _generation, interactive=flow is not None)
    provider = _provider(
        storage,
        flow.redirect if flow else no_interaction,
        flow.callback if flow else no_interaction,
        flow.redirect_uri if flow else "http://127.0.0.1/suno/callback",
    )
    async with httpx.AsyncClient(auth=provider, timeout=httpx.Timeout(90, connect=20)) as client:
        async with streamable_http_client(MCP_URL, http_client=client) as (read, write, _):
            timeout = _AUTH_TIMEOUT + 60 if flow else 120
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=timeout)) as session:
                await session.initialize()
                return _result_data(await session.call_tool(name, arguments))


def _public_error(error: Exception) -> SunoError:
    # Transport task groups wrap errors; never expose raw HTTP bodies, OAuth
    # callback parameters or validation errors (which may contain tokens).
    pending = [error]
    while pending:
        cause = pending.pop()
        if isinstance(cause, SunoError):
            return cause
        if type(cause).__name__ in ("OAuthFlowError", "OAuthTokenError"):
            return SunoError("Suno needs you to sign in again.")
        pending.extend(reversed(getattr(cause, "exceptions", ())))
    return SunoError("Could not reach Suno. Please try again.")


def call_tool(name: str, arguments: dict) -> dict:
    """Call an official MCP tool, refreshing saved credentials if needed.

    Invoke from a synchronous worker (or asyncio.to_thread in an async route).
    Background jobs never open a browser or initiate an interactive sign-in.
    """
    with _call_lock:
        if not is_connected():
            raise SunoError("Connect Suno before generating music.")
        try:
            return asyncio.run(_execute(name, arguments))
        except Exception as exc:
            raise _public_error(exc) from None


def account_usage() -> dict:
    return call_tool("account-usage", {})


class _AuthFlow:
    def __init__(self, generation):
        self.generation = generation
        self.status = "pending"
        self.authorize_url = None
        self.error = None
        self.ready = threading.Event()
        self.cancelled = threading.Event()
        self.expected_state = None
        self.code = None
        self.callback_error = None
        flow = self

        class CallbackHandler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass  # callback URLs contain the short-lived authorization code

            def do_GET(self):
                parsed = urlparse(self.path)
                params = parse_qs(parsed.query)
                state = params.get("state", [""])[0]
                valid = (parsed.path == "/suno/callback" and flow.expected_state
                         and secrets.compare_digest(state, flow.expected_state))
                if not valid:
                    self.send_error(400, "Invalid Suno sign-in callback")
                    return
                if params.get("error"):
                    flow.callback_error = "Suno sign-in was declined. Please try again."
                elif params.get("code"):
                    flow.code = params["code"][0]
                else:
                    self.send_error(400, "Missing authorization code")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(b"Return to Stephen Spielbot to check your Suno connection.")

        self.server = HTTPServer(("127.0.0.1", 0), CallbackHandler)
        self.server.timeout = 0.5
        self.redirect_uri = f"http://127.0.0.1:{self.server.server_port}/suno/callback"

    async def redirect(self, url):
        self.expected_state = parse_qs(urlparse(url).query)["state"][0]
        with _state_lock:
            self.authorize_url = url
            self.ready.set()

    async def callback(self):
        deadline = time.monotonic() + _AUTH_TIMEOUT
        while not self.cancelled.is_set() and time.monotonic() < deadline:
            await asyncio.to_thread(self.server.handle_request)
            if self.callback_error:
                raise SunoError(self.callback_error)
            if self.code:
                return self.code, self.expected_state
        raise SunoError("Suno sign-in was cancelled or timed out. Please try again.")

    def run(self):
        try:
            with _call_lock:
                asyncio.run(_execute("account-usage", {}, self))
            with _state_lock:
                if not self.cancelled.is_set():
                    self.status = "connected"
                    self.authorize_url = None
        except Exception as exc:
            with _state_lock:
                self.status = "error"
                self.error = str(_public_error(exc))
                self.authorize_url = None
        finally:
            self.server.server_close()
            self.ready.set()


def start_auth_flow() -> dict:
    global _flow, _generation
    with _state_lock:
        if _flow is not None and _flow.status == "pending":
            return poll_auth_flow()
        try:
            flow = _AuthFlow(_generation + 1)
        except OSError:
            raise SunoError("Could not open the local Suno sign-in callback. Please try again.") from None
        # An explicit Connect action also replaces stale/revoked credentials.
        # Local token presence cannot prove that refresh still works remotely.
        _generation += 1
        _TOKEN_PATH.unlink(missing_ok=True)
        _flow = flow
        threading.Thread(target=_flow.run, name="suno-sign-in", daemon=True).start()
    flow.ready.wait(timeout=10)
    return poll_auth_flow()


def poll_auth_flow() -> dict:
    with _state_lock:
        if _flow is not None:
            if _flow.status == "connected" and not is_connected():
                return {"status": "disconnected"}
            result = {"status": _flow.status}
            if _flow.authorize_url:
                result["authorize_url"] = _flow.authorize_url
            if _flow.error:
                result["error"] = _flow.error
            return result
        return {"status": "connected" if is_connected() else "disconnected"}


def disconnect() -> None:
    """Forget this app's local credentials and cancel any pending connection."""
    global _flow, _generation
    with _state_lock:
        _generation += 1
        if _flow is not None:
            _flow.cancelled.set()
            _flow = None
        _TOKEN_PATH.unlink(missing_ok=True)
