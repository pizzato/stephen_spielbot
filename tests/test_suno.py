"""Suno OAuth persistence, callback validation and safe MCP responses."""
import asyncio
from builtins import ExceptionGroup
import json
import logging
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.error import HTTPError
from urllib.request import urlopen

from pipeline import suno

try:
    from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
    SDK_AVAILABLE = True
except ImportError:
    SDK_AVAILABLE = False


class SunoTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "config" / "suno_token.json"
        self.patch = mock.patch.object(suno, "_TOKEN_PATH", self.path)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        suno.disconnect()
        self.addCleanup(suno.disconnect)

    def test_atomic_store_is_private_and_disconnect_removes_it(self):
        suno._write_state({"tokens": {"access_token": "secret"}, "client_info": {"client_id": "app"}})
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertTrue(suno.is_connected())
        suno._write_state({"tokens": {"access_token": "replacement"}, "client_info": {"client_id": "app"}})
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])
        suno.disconnect()
        self.assertFalse(self.path.exists())
        self.assertEqual(suno.poll_auth_flow(), {"status": "disconnected"})

    def test_cancelled_flow_cannot_restore_disconnected_tokens(self):
        storage = suno._Storage(suno._generation)
        suno.disconnect()
        with self.assertRaisesRegex(suno.SunoError, "cancelled"):
            storage._save({"tokens": {"access_token": "stale"}})
        self.assertFalse(self.path.exists())

    def test_bad_token_file_is_disconnected(self):
        self.path.parent.mkdir()
        self.path.write_text("bad json")
        self.assertFalse(suno.is_connected())

    def test_background_request_cannot_start_sign_in(self):
        with mock.patch.object(suno, "start_auth_flow") as start:
            with self.assertRaisesRegex(suno.SunoError, "Connect Suno"):
                suno.call_tool("audio-create", {})
        start.assert_not_called()

    def test_connect_replaces_stale_local_credentials(self):
        suno._write_state({"tokens": {"access_token": "revoked"}, "client_info": {"client_id": "old"}})
        ready = threading.Event()
        ready.set()
        flow = SimpleNamespace(status="pending", authorize_url="https://example.test/sign-in",
                               error=None, ready=ready, cancelled=threading.Event(), run=lambda: None)
        with mock.patch.object(suno, "_AuthFlow", return_value=flow):
            self.assertEqual(suno.start_auth_flow()["status"], "pending")
        self.assertFalse(suno.is_connected())
        self.assertFalse(self.path.exists())

    def test_structured_content_and_json_text_are_supported(self):
        self.assertEqual(suno._result_data(SimpleNamespace(
            isError=False, structuredContent={"clips": []}, content=[])), {"clips": []})
        self.assertEqual(suno._result_data(SimpleNamespace(
            isError=False, structuredContent=None,
            content=[SimpleNamespace(type="text", text='{"clip_ids": ["one", "two"]}'),
                     SimpleNamespace(type="text", text="Next, wait for the songs.")])),
            {"clip_ids": ["one", "two"]})

    def test_errors_do_not_expose_transport_or_tool_secrets(self):
        raw_error = RuntimeError("Authorization: Bearer private-access-token")
        self.assertNotIn("private-access-token", str(suno._public_error(raw_error)))
        with self.assertRaises(suno.SunoError) as caught:
            suno._result_data(SimpleNamespace(isError=True, structuredContent=None,
                content=[SimpleNamespace(type="text", text="refresh_token=private-token")]))
        self.assertNotIn("private-token", str(caught.exception))
        nested = ExceptionGroup("HTTP secret", [ExceptionGroup("token", [suno.SunoError("Reconnect Suno.")])])
        self.assertEqual(str(suno._public_error(nested)), "Reconnect Suno.")

    def test_sdk_oauth_logs_strip_secret_bearing_exception_trace(self):
        record = logging.LogRecord("mcp.client.auth.oauth2", logging.ERROR, "", 0,
                                   "OAuth flow error", (), (RuntimeError, RuntimeError("token-secret"), None))
        suno._OAuthLogFilter().filter(record)
        self.assertEqual(logging.Formatter().format(record), "OAuth flow error")

    def test_callback_rejects_wrong_state_then_accepts_valid_code(self):
        flow = suno._AuthFlow(suno._generation)
        self.addCleanup(flow.server.server_close)
        asyncio.run(flow.redirect("https://example.test/authorize?state=correct"))

        def request(query):
            thread = threading.Thread(target=flow.server.handle_request)
            thread.start()
            try:
                return urlopen(flow.redirect_uri + query, timeout=2)
            finally:
                thread.join(timeout=3)

        with self.assertRaises(HTTPError) as caught:
            request("?state=wrong&code=attacker-code")
        self.assertEqual(caught.exception.code, 400)
        self.assertIsNone(flow.code)
        with request("?state=correct&code=real-code") as response:
            self.assertEqual(response.status, 200)
            self.assertNotIn(b"real-code", response.read())
        self.assertEqual(flow.code, "real-code")

    def test_callback_cancel_exits_without_waiting_for_timeout(self):
        flow = suno._AuthFlow(suno._generation)
        self.addCleanup(flow.server.server_close)
        flow.cancelled.set()
        with self.assertRaisesRegex(suno.SunoError, "cancelled"):
            asyncio.run(flow.callback())

    @unittest.skipUnless(SDK_AVAILABLE, "official MCP SDK not installed")
    def test_restart_restores_expiry_and_correct_refresh_endpoint(self):
        storage = suno._Storage(suno._generation)
        suno._write_state({
            "expires_at": time.time() - 30,
            "tokens": {"access_token": "old", "refresh_token": "refresh", "token_type": "Bearer"},
            "client_info": {"client_id": "spielbot", "redirect_uris": ["http://127.0.0.1:4000/suno/callback"],
                            "token_endpoint_auth_method": "none"},
            "oauth_metadata": {
                "issuer": "https://studio-api-prod.suno.com/api/v2/external/oauth",
                "authorization_endpoint": "https://studio-api-prod.suno.com/api/v2/external/oauth/authorize/",
                "token_endpoint": "https://studio-api-prod.suno.com/api/v2/external/oauth/token/",
            },
            "protected_resource_metadata": {"resource": suno.MCP_URL, "authorization_servers": ["https://studio-api-prod.suno.com/api/v2/external/oauth"]},
        })
        provider = suno._provider(storage, None, None, "http://127.0.0.1/suno/callback")

        async def inspect():
            await provider._initialize()
            self.assertFalse(provider.context.is_token_valid())
            self.assertTrue(provider.context.can_refresh_token())
            request = await provider._refresh_token()
            self.assertEqual(str(request.url), "https://studio-api-prod.suno.com/api/v2/external/oauth/token/")
            self.assertIn(b"resource=https%3A%2F%2Fapi.suno.com%2Fmcp", request.content)

            # Exercise the real SDK auth flow against an in-memory HTTP server.
            import httpx
            requests = []

            async def respond(request):
                requests.append(request)
                if request.url.path.endswith("/token/"):
                    return httpx.Response(200, json={"access_token": "renewed", "expires_in": 3600, "token_type": "Bearer"})
                self.assertEqual(request.headers["Authorization"], "Bearer renewed")
                return httpx.Response(200, json={"ok": True})

            async with httpx.AsyncClient(auth=provider, transport=httpx.MockTransport(respond)) as client:
                response = await client.get(suno.MCP_URL)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(requests), 2)
            self.assertEqual(suno._read_state()["tokens"]["refresh_token"], "refresh")
            self.assertTrue(provider.context.is_token_valid())

        asyncio.run(inspect())

    @unittest.skipUnless(SDK_AVAILABLE, "official MCP SDK not installed")
    def test_refresh_preserves_refresh_token_if_not_rotated(self):
        storage = suno._Storage(suno._generation)
        asyncio.run(storage.set_tokens(OAuthToken(access_token="old", refresh_token="refresh", expires_in=60)))
        new_token = OAuthToken(access_token="new", expires_in=120)
        before = time.time()
        asyncio.run(storage.set_tokens(new_token))
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["tokens"]["refresh_token"], "refresh")
        self.assertEqual(new_token.refresh_token, "refresh")
        self.assertGreaterEqual(saved["expires_at"], before + 120)
        self.assertEqual(asyncio.run(storage.get_tokens()).access_token, "new")

    @unittest.skipUnless(SDK_AVAILABLE, "official MCP SDK not installed")
    def test_registration_and_discovery_are_persisted_with_tokens(self):
        from mcp.shared.auth import OAuthMetadata, ProtectedResourceMetadata

        storage = suno._Storage(suno._generation, interactive=True)
        storage.context = SimpleNamespace(
            oauth_metadata=OAuthMetadata(issuer="https://auth.example.test", authorization_endpoint="https://auth.example.test/authorize",
                                         token_endpoint="https://auth.example.test/token"),
            protected_resource_metadata=ProtectedResourceMetadata(resource=suno.MCP_URL, authorization_servers=["https://auth.example.test"]),
        )
        asyncio.run(storage.set_client_info(OAuthClientInformationFull(client_id="new-app", redirect_uris=["http://127.0.0.1:5000/callback"])))
        asyncio.run(storage.set_tokens(OAuthToken(access_token="new-token", expires_in=600)))
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["client_info"]["client_id"], "new-app")
        self.assertEqual(saved["oauth_metadata"]["token_endpoint"], "https://auth.example.test/token")
        self.assertIsNone(asyncio.run(storage.get_tokens()))
        self.assertIsNone(asyncio.run(storage.get_client_info()))


if __name__ == "__main__":
    unittest.main()
