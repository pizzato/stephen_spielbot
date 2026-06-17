"""X (Twitter) API v2 integration for Stephen Spielbot (issue #107).

Mirrors ``pipeline/youtube.py`` for X:
- OAuth 2.0 Authorization Code + PKCE (user context, with a fixed loopback
  redirect server), token storage + refresh
- Premium detection (decides whether long videos can be posted)
- Chunked video upload + posting a tweet
- Mentions fetching + replying (added in the comments phase)
- Analytics (added in the analytics phase)

Tokens live alongside YouTube's in ``~/.config/video-generator/`` so the whole
multi-account model (issue #22 style) carries over: ``x_token.json`` for the
legacy/"default" account, ``x_token_{key}.json`` per connected account.

The HTTP surface uses ``requests`` directly (already a dependency) rather than a
heavier client library — the v2 media-upload + premium-detection + posting flow
needs specific endpoints, and keeping it explicit mirrors the rest of the
pipeline's small, dependency-light style.
"""
from __future__ import annotations

import base64
import concurrent.futures
import hashlib
import json
import logging
import re
import secrets
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import requests

logger = logging.getLogger("video_gen")


def _video_duration(path: Path) -> float:
    """Media duration in seconds, or 0.0 if it can't be read. Resolves ffprobe via
    the assembler's locator (lazy-imported to keep this module import-light): the
    launchd service's PATH lacks /opt/homebrew/bin, so a bare "ffprobe" isn't found
    and would silently return 0.0 — making every video look short and defeating the
    long-video YouTube-link fallback (issue #107)."""
    try:
        from pipeline.assembler import _resolve_media_tool
        ffprobe = _resolve_media_tool("ffprobe")
        out = subprocess.run(
            [ffprobe, "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
        return float(out)
    except Exception:
        return 0.0

# ── Endpoints ─────────────────────────────────────────────────────────────────
AUTHORIZE_URL = "https://x.com/i/oauth2/authorize"
TOKEN_URL = "https://api.x.com/2/oauth2/token"
API_BASE = "https://api.x.com/2"
UPLOAD_URL = f"{API_BASE}/media/upload"
UPLOAD_URL_V11 = "https://upload.twitter.com/1.1/media/upload.json"  # OAuth 1.0a media upload
SUBTITLES_URL = f"{API_BASE}/media/subtitles"  # OAuth 2.0 bearer subtitle association
SUBTITLES_URL_V11 = "https://upload.twitter.com/1.1/media/subtitles/create.json"  # OAuth 1.0a

# OAuth 2.0 scopes. ``media.write`` is required to upload media under user
# context; ``offline.access`` yields a refresh token (access tokens last ~2h).
SCOPES = ["tweet.read", "tweet.write", "users.read", "media.write", "offline.access"]

# Fixed loopback redirect — unlike Google's random-port flow, X requires the
# callback URL to be pre-registered EXACTLY in the developer portal. Register
# this value on the X app and we bind the same port locally (RFC 8252).
REDIRECT_PORT = 8723
REDIRECT_URI = f"http://127.0.0.1:{REDIRECT_PORT}/callback"

# X video limits for a standard (non-Premium) account. Premium lifts these
# dramatically (hours / many GB), so they only gate non-Premium posting.
X_MAX_VIDEO_SECONDS = 140
X_MAX_VIDEO_BYTES = 512 * 1024 * 1024
TWEET_TEXT_LIMIT = 280
TWEET_TEXT_LIMIT_PREMIUM = 25000  # X Premium long posts

_CONFIG_DIR = Path.home() / ".config" / "video-generator"
_X_TOKEN_PATH = _CONFIG_DIR / "x_token.json"
COMMENTS_CACHE_PATH = _CONFIG_DIR / "x_comments.json"
ANALYTICS_CACHE_PATH = _CONFIG_DIR / "x_analytics.json"

# Reserved account key for the pre-multi-account login (mirrors YouTube's
# DEFAULT_CHANNEL_KEY); its token lives at the legacy x_token.json path.
DEFAULT_ACCOUNT_KEY = "default"

# ── Auth status cache (avoids a live API call on every tab visit) ─────────────
_auth_cache: dict[str, dict] = {}
_AUTH_CACHE_TTL = 60.0


def _token_path(account: str = "") -> Path:
    """Token file for an account key. '' / 'default' → the legacy single-account
    token; anything else (normally the X user id) gets its own file."""
    key = (account or "").strip()
    if not key or key == DEFAULT_ACCOUNT_KEY:
        return _X_TOKEN_PATH
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", key)
    return _CONFIG_DIR / f"x_token_{safe}.json"


# ── Token storage + refresh ────────────────────────────────────────────────────

def _load_token(account: str = "") -> dict | None:
    """Read the stored token dict for an account, or None if not connected."""
    path = _token_path(account)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception as exc:
        logger.warning("Failed to load X token: %s", exc)
        return None


def _save_token(account: str, token: dict) -> None:
    path = _token_path(account)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(token, indent=2))


def _token_auth(client_id: str, client_secret: str):
    """HTTP Basic auth for confidential clients; None for public PKCE clients."""
    return (client_id, client_secret) if client_secret else None


def _refresh_token(client_id: str, client_secret: str, token: dict) -> dict | None:
    """Exchange a refresh token for a fresh access token. Returns the merged
    token dict (preserving identity fields) or None on failure."""
    rt = token.get("refresh_token")
    if not rt:
        return None
    data = {"grant_type": "refresh_token", "refresh_token": rt, "client_id": client_id}
    try:
        r = requests.post(TOKEN_URL, data=data,
                          auth=_token_auth(client_id, client_secret), timeout=30)
        r.raise_for_status()
        fresh = r.json()
    except Exception as exc:
        logger.warning("Failed to refresh X token: %s", exc)
        return None
    merged = {**token, **fresh}
    # X may or may not rotate the refresh token — keep the old one if absent.
    if not fresh.get("refresh_token"):
        merged["refresh_token"] = rt
    merged["expires_at"] = time.time() + int(fresh.get("expires_in", 7200)) - 60
    return merged


def _bearer(client_id: str, client_secret: str, account: str = "") -> str | None:
    """Return a valid access token for an account, refreshing + persisting if the
    stored one has expired. None if not connected or refresh fails."""
    token = _load_token(account)
    if not token or not token.get("access_token"):
        return None
    if token.get("expires_at", 0) > time.time():
        return token["access_token"]
    refreshed = _refresh_token(client_id, client_secret, token)
    if not refreshed:
        return None
    _save_token(account, refreshed)
    return refreshed.get("access_token")


def _account_auth(client_id: str, client_secret: str, account: str = "") -> dict | None:
    """Resolve how to authenticate requests for an account: OAuth 1.0a user keys
    (no-browser — pasted API key/secret + access token/secret) or a refreshed
    OAuth 2.0 bearer token. Returns {"oauth1": <OAuth1>} or {"bearer": <token>},
    or None if not connected."""
    token = _load_token(account)
    if not token:
        return None
    if token.get("auth") == "oauth1":
        try:
            from requests_oauthlib import OAuth1
        except Exception as exc:
            logger.warning("requests_oauthlib unavailable for OAuth1: %s", exc)
            return None
        return {"oauth1": OAuth1(token.get("api_key", ""), token.get("api_secret", ""),
                                 token.get("access_token", ""), token.get("access_secret", ""))}
    access = _bearer(client_id, client_secret, account)
    return {"bearer": access} if access else None


def _bearer_auth(access_token: str) -> dict:
    """Auth descriptor for a raw bearer token (used before a token is saved)."""
    return {"bearer": access_token}


def _xreq(method: str, url: str, auth: dict, **kw):
    """requests with the account's auth applied (OAuth1 signer or Bearer header)."""
    if auth.get("oauth1") is not None:
        kw["auth"] = auth["oauth1"]
    else:
        kw.setdefault("headers", {})["Authorization"] = f"Bearer {auth.get('bearer', '')}"
    return requests.request(method, url, **kw)


def _api_get(auth: dict, path: str, params: dict | None = None) -> dict:
    r = _xreq("GET", f"{API_BASE}{path}", auth, params=params or {}, timeout=30)
    r.raise_for_status()
    return r.json()


# ── Premium detection ───────────────────────────────────────────────────────────

_PREMIUM_SUBSCRIPTIONS = {"basic", "premium", "premium+", "premiumplus", "premium plus"}


def _premium_from_me(me: dict) -> bool:
    """Decide whether a ``/2/users/me`` payload represents a Premium-capable
    account (long-video posting). Keys on ``subscription_type``, falling back to
    the legacy ``verified_type`` blue check. Pure — unit-tested."""
    data = me.get("data", me) if isinstance(me, dict) else {}
    sub = str(data.get("subscription_type") or "").strip().lower()
    if sub and sub not in ("none", "free"):
        return True
    vt = str(data.get("verified_type") or "").strip().lower()
    return vt in ("blue",)


def _fetch_me(auth: dict) -> dict:
    """GET /2/users/me with the fields needed for identity + premium detection.
    ``auth`` is an _account_auth descriptor (OAuth1 signer or bearer)."""
    return _api_get(auth, "/users/me", {
        "user.fields": "username,name,verified,verified_type,subscription_type",
    })


# ── Auth status ─────────────────────────────────────────────────────────────────

def _clear_auth_cache(account: str = "") -> None:
    _auth_cache.pop((account or "").strip() or DEFAULT_ACCOUNT_KEY, None)


def check_auth_status(client_id: str, client_secret: str = "",
                      force: bool = False, account: str = "") -> dict:
    """Return {connected, account_name, account_id, premium, error} for one account.

    Cached for _AUTH_CACHE_TTL seconds to keep the UI responsive; pass force=True
    after connect/disconnect to bypass.
    """
    if not client_id:
        return {"connected": False, "account_name": "", "account_id": "",
                "premium": False, "error": "X API client ID not configured (see Settings → X)."}
    cache_key = (account or "").strip() or DEFAULT_ACCOUNT_KEY
    now = time.time()
    cached = _auth_cache.get(cache_key)
    if (not force and cached is not None and cached["cid"] == client_id
            and (now - cached["ts"]) < _AUTH_CACHE_TTL):
        return cached["result"]

    def _full_check() -> dict:
        auth = _account_auth(client_id, client_secret, account)
        if not auth:
            return {"connected": False, "account_name": "", "account_id": "",
                    "premium": False, "error": "Not authenticated — click Connect X"}
        me = _fetch_me(auth)
        data = me.get("data", {})
        token = _load_token(account) or {}
        premium = _premium_from_me(me)
        # Cache identity + premium back onto the token so URLs/limits don't need a
        # live call on every post.
        if data:
            token.update({"account_id": data.get("id", token.get("account_id", "")),
                          "username": data.get("username", token.get("username", "")),
                          "premium": premium})
            _save_token(account, token)
        return {"connected": True,
                "account_name": data.get("username", token.get("username", "")),
                "account_id": data.get("id", token.get("account_id", "")),
                "premium": premium, "error": ""}

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = pool.submit(_full_check)
    try:
        result = future.result(timeout=8)
    except concurrent.futures.TimeoutError:
        result = {"connected": False, "account_name": "", "account_id": "",
                  "premium": False, "error": "Connection check timed out — check your network"}
    except Exception as exc:
        result = {"connected": False, "account_name": "", "account_id": "",
                  "premium": False, "error": str(exc)[:300]}
    finally:
        pool.shutdown(wait=False)
    _auth_cache[cache_key] = {"result": result, "ts": now, "cid": client_id}
    return result


# ── OAuth2 PKCE flow ─────────────────────────────────────────────────────────────

def _pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for the S256 PKCE method."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


class _RedirectHandler(BaseHTTPRequestHandler):
    """Captures the OAuth redirect (?code=&state=) on the fixed loopback port."""
    code: str | None = None
    state: str | None = None
    error: str | None = None

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        q = parse_qs(parsed.query)
        _RedirectHandler.code = (q.get("code") or [None])[0]
        _RedirectHandler.state = (q.get("state") or [None])[0]
        _RedirectHandler.error = (q.get("error") or [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(
            b"<html><body style='font-family:sans-serif'>"
            b"<h2>Stephen Spielbot \xe2\x80\x94 X account connected.</h2>"
            b"<p>You can close this tab and return to the app.</p></body></html>")

    def log_message(self, *args):  # silence default request logging
        pass


def _capture_redirect(expected_state: str, timeout: float = 300.0) -> str:
    """Serve the loopback callback once and return the auth code. Raises on
    error, state mismatch, or timeout."""
    _RedirectHandler.code = _RedirectHandler.state = _RedirectHandler.error = None
    server = HTTPServer(("127.0.0.1", REDIRECT_PORT), _RedirectHandler)
    server.timeout = 1.0
    deadline = time.time() + timeout
    try:
        while (_RedirectHandler.code is None and _RedirectHandler.error is None
               and time.time() < deadline):
            server.handle_request()
    finally:
        server.server_close()
    if _RedirectHandler.error:
        raise RuntimeError(f"Authorization denied: {_RedirectHandler.error}")
    if _RedirectHandler.code is None:
        raise RuntimeError("Timed out waiting for X authorization.")
    if expected_state and _RedirectHandler.state != expected_state:
        raise RuntimeError("OAuth state mismatch — possible CSRF; aborted.")
    return _RedirectHandler.code


class _AuthFlow:
    def __init__(self):
        self.running = False
        self.result: dict | None = None
        self.authorize_url = ""
        self._thread: threading.Thread | None = None

    def start(self, client_id: str, client_secret: str = "", finalize=None) -> str:
        """Run the OAuth2 PKCE flow in a background thread. After login, the
        authorized account is identified via /2/users/me and
        ``finalize(account_id, username)`` (if given) decides the storage key.

        The authorize URL is built here (synchronously) and exposed as
        ``self.authorize_url`` so the UI can offer it for an Incognito window —
        the local callback listener catches the redirect regardless of which
        browser completes the consent, sidestepping X's logged-in redirect loop."""
        if self.running:
            return "Auth flow already in progress — check your browser."
        if not client_id:
            return "Error: X API client ID not configured (see Settings → X)."
        self.result = None
        self.running = True
        verifier, challenge = _pkce_pair()
        state = secrets.token_urlsafe(24)
        self.authorize_url = AUTHORIZE_URL + "?" + urlencode({
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "scope": " ".join(SCOPES),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        })

        def _run():
            try:
                import webbrowser
                webbrowser.open(self.authorize_url)
                code = _capture_redirect(state)
                # Exchange the code for tokens.
                resp = requests.post(TOKEN_URL, data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": REDIRECT_URI,
                    "code_verifier": verifier,
                    "client_id": client_id,
                }, auth=_token_auth(client_id, client_secret), timeout=30)
                resp.raise_for_status()
                token = resp.json()
                token["expires_at"] = time.time() + int(token.get("expires_in", 7200)) - 60
                # Identify the account.
                account_id, username, premium = "", "", False
                try:
                    me = _fetch_me(_bearer_auth(token["access_token"]))
                    data = me.get("data", {})
                    account_id = data.get("id", "")
                    username = data.get("username", "")
                    premium = _premium_from_me(me)
                except Exception as exc:
                    logger.warning("Could not resolve X identity after auth: %s", exc)
                token.update({"account_id": account_id, "username": username, "premium": premium})
                key = account_id
                if finalize is not None:
                    try:
                        key = finalize(account_id, username) or key
                    except Exception as exc:
                        logger.warning("X finalize callback failed: %s", exc)
                _save_token(key, token)
                _clear_auth_cache(key)
                self.result = {"success": True, "error": "", "account": key,
                               "account_id": account_id, "account_name": username,
                               "premium": premium}
            except Exception as exc:
                self.result = {"success": False, "error": str(exc)[:300]}
            finally:
                self.running = False

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()
        return "Browser opened — complete the X authorization in your browser."

    def disconnect(self, account: str = ""):
        path = _token_path(account)
        if path.exists():
            path.unlink()
        self.result = None
        self.running = False


_auth_flow = _AuthFlow()


def start_auth_flow(client_id: str, client_secret: str = "", finalize=None) -> str:
    return _auth_flow.start(client_id, client_secret, finalize=finalize)


def poll_auth_flow() -> dict:
    return {"running": _auth_flow.running, "result": _auth_flow.result,
            "authorize_url": _auth_flow.authorize_url}


def disconnect_x(account: str = ""):
    _auth_flow.disconnect(account)
    _clear_auth_cache(account)


def import_tokens(client_id: str, client_secret: str, access_token: str,
                  refresh_token: str = "", finalize=None) -> dict:
    """Connect an account from OAuth2 tokens obtained elsewhere (no browser flow).

    Validates the access token against /2/users/me (refreshing first if it's
    already expired and a refresh token is given), then stores it in the same
    format the OAuth flow produces so all the normal token handling — refresh on
    expiry, premium detection, posting — works unchanged. Returns
    {success, account, account_id, account_name, premium, error}.
    """
    access_token = (access_token or "").strip()
    refresh_token = (refresh_token or "").strip()
    if not access_token:
        return {"success": False, "error": "Access token is required."}
    token = {"access_token": access_token, "refresh_token": refresh_token,
             "token_type": "bearer", "expires_at": time.time() + 7200 - 60}
    me = None
    try:
        me = _fetch_me(_bearer_auth(access_token))
    except Exception as exc:
        # Maybe already expired — try one refresh, then re-validate.
        if refresh_token:
            refreshed = _refresh_token(client_id, client_secret, token)
            if refreshed:
                token = refreshed
                try:
                    me = _fetch_me(_bearer_auth(token["access_token"]))
                except Exception:
                    me = None
        if me is None:
            return {"success": False,
                    "error": f"X rejected the token: {str(exc)[:200]}. Check it has the "
                             "right scopes and was issued by this app."}
    data = me.get("data", {})
    account_id = data.get("id", "")
    username = data.get("username", "")
    premium = _premium_from_me(me)
    token.update({"account_id": account_id, "username": username, "premium": premium})
    key = account_id or DEFAULT_ACCOUNT_KEY
    if finalize is not None:
        try:
            key = finalize(account_id, username) or key
        except Exception as exc:
            logger.warning("X finalize callback failed: %s", exc)
    _save_token(key, token)
    _clear_auth_cache(key)
    return {"success": True, "account": key, "account_id": account_id,
            "account_name": username, "premium": premium, "error": ""}


def import_oauth1(api_key: str, api_secret: str, access_token: str, access_secret: str,
                  finalize=None) -> dict:
    """Connect an account from OAuth 1.0a user keys (no browser, no scopes).

    These are generated with one click in the X developer portal (API Key/Secret
    + Access Token/Secret) and authorize the app for the OWNER's account at the
    app's permission level (Read+Write covers posting AND media upload — so this
    sidesteps both the authorize redirect loop and the media.write scope issue).
    Validates via /2/users/me, then stores an oauth1 token file. Returns
    {success, account, account_id, account_name, premium, error}.
    """
    api_key = (api_key or "").strip()
    api_secret = (api_secret or "").strip()
    access_token = (access_token or "").strip()
    access_secret = (access_secret or "").strip()
    if not (api_key and api_secret and access_token and access_secret):
        return {"success": False, "error": "All four keys are required (API key/secret + access token/secret)."}
    token = {"auth": "oauth1", "api_key": api_key, "api_secret": api_secret,
             "access_token": access_token, "access_secret": access_secret}
    try:
        from requests_oauthlib import OAuth1
    except Exception as exc:
        return {"success": False, "error": f"OAuth1 support unavailable: {exc}"}
    auth = {"oauth1": OAuth1(api_key, api_secret, access_token, access_secret)}
    try:
        me = _fetch_me(auth)
    except Exception as exc:
        return {"success": False,
                "error": f"X rejected the keys: {str(exc)[:200]}. Check they're correct and the "
                         "app has Read and Write permission."}
    data = me.get("data", {})
    account_id = data.get("id", "")
    username = data.get("username", "")
    premium = _premium_from_me(me)
    token.update({"account_id": account_id, "username": username, "premium": premium})
    key = account_id or DEFAULT_ACCOUNT_KEY
    if finalize is not None:
        try:
            key = finalize(account_id, username) or key
        except Exception as exc:
            logger.warning("X finalize callback failed: %s", exc)
    _save_token(key, token)
    _clear_auth_cache(key)
    return {"success": True, "account": key, "account_id": account_id,
            "account_name": username, "premium": premium, "error": ""}


# ── Video-length / Premium decision ──────────────────────────────────────────────

def decide_post_target(video_path: str, premium: bool, youtube_url: str = "",
                       duration_secs: float | None = None,
                       size_bytes: int | None = None) -> dict:
    """Decide how a video should be posted to X given account capability + limits.

    Pure (duration/size are probed only when not supplied, so tests inject them):
      - Premium account, or within the standard limits → ``post_full``.
      - Over the limit but a YouTube link exists → ``post_link`` (text + link).
      - Over the limit and no link → ``skip`` with a clear reason.

    Returns {action, reason, duration_secs, size_bytes}.
    """
    if duration_secs is None:
        duration_secs = _video_duration(Path(video_path)) if video_path else 0.0
    if size_bytes is None:
        try:
            size_bytes = Path(video_path).stat().st_size if video_path else 0
        except Exception:
            size_bytes = 0
    within = duration_secs <= X_MAX_VIDEO_SECONDS and size_bytes <= X_MAX_VIDEO_BYTES
    if premium or within:
        return {"action": "post_full", "reason": "", "duration_secs": duration_secs,
                "size_bytes": size_bytes}
    if youtube_url:
        return {"action": "post_link",
                "reason": "Video exceeds X's non-Premium limit — posting the YouTube link instead.",
                "duration_secs": duration_secs, "size_bytes": size_bytes}
    return {"action": "skip",
            "reason": (f"Video is {duration_secs:.0f}s / {size_bytes / 1024 / 1024:.0f}MB, over X's "
                       f"non-Premium limit ({X_MAX_VIDEO_SECONDS}s / "
                       f"{X_MAX_VIDEO_BYTES // 1024 // 1024}MB), and no YouTube link exists."),
            "duration_secs": duration_secs, "size_bytes": size_bytes}


# ── Video upload + posting ────────────────────────────────────────────────────

def _chunked_upload(auth: dict, video_path: str, progress_callback=None,
                    media_category: str = "tweet_video",
                    media_type: str = "video/mp4") -> str:
    """Upload media via the chunked endpoint (INIT/APPEND/FINALIZE/STATUS).
    OAuth 1.0a uses the v1.1 endpoint (the classic media upload, which needs only
    the app's Read+Write permission — no media.write scope); OAuth2 bearer uses
    the v2 endpoint. ``media_category`` is ``tweet_video`` for short clips,
    ``amplify_video`` for longer ones (the API's longer-video category), or
    ``subtitles`` for an SRT track; ``media_type`` is the MIME type of the file.
    Returns the media id. Raises on failure."""
    url = UPLOAD_URL_V11 if auth.get("oauth1") is not None else UPLOAD_URL
    size = Path(video_path).stat().st_size

    init = _xreq("POST", url, auth, data={
        "command": "INIT", "total_bytes": size,
        "media_type": media_type, "media_category": media_category,
    }, timeout=60)
    init.raise_for_status()
    init_json = init.json()
    media_id = (init_json.get("data", {}).get("id")
                or init_json.get("media_id_string") or init_json.get("media_id"))
    if not media_id:
        raise RuntimeError(f"Media INIT returned no id: {str(init_json)[:200]}")

    chunk_size = 4 * 1024 * 1024
    with open(video_path, "rb") as fh:
        segment = 0
        sent = 0
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            ap = _xreq("POST", url, auth,
                       data={"command": "APPEND", "media_id": media_id, "segment_index": segment},
                       files={"media": chunk}, timeout=120)
            ap.raise_for_status()
            sent += len(chunk)
            segment += 1
            if progress_callback and size:
                pct = sent / size * 100
                progress_callback(pct, f"Uploading to X… {pct:.0f}%")

    fin = _xreq("POST", url, auth, data={"command": "FINALIZE", "media_id": media_id}, timeout=60)
    fin.raise_for_status()
    info = fin.json().get("data", fin.json()).get("processing_info")
    # Poll STATUS until the video finishes transcoding.
    while info and info.get("state") in ("pending", "in_progress"):
        time.sleep(min(int(info.get("check_after_secs", 3)), 10))
        st = _xreq("GET", url, auth, params={"command": "STATUS", "media_id": media_id}, timeout=30)
        st.raise_for_status()
        info = st.json().get("data", st.json()).get("processing_info")
    if info and info.get("state") == "failed":
        raise RuntimeError(f"X media processing failed: {str(info.get('error'))[:200]}")
    return str(media_id)


# Friendly CC-track labels for the languages the pipeline captions in; anything
# else falls back to the uppercased code (it's only the track's display name).
_LANGUAGE_DISPLAY_NAMES = {
    "en": "English", "es": "Spanish", "pt": "Portuguese", "fr": "French",
    "de": "German", "it": "Italian", "ja": "Japanese", "zh": "Chinese",
}


def _attach_subtitles(auth: dict, video_media_id: str, srt_path: str,
                      language: str = "en", media_category: str = "tweet_video") -> None:
    """Associate an SRT caption track with an already-uploaded video so the tweet's
    video carries closed captions — the X mirror of YouTube's caption upload.

    Uploads the SRT as a ``subtitles`` media, then calls the subtitle-association
    endpoint: v1.1 (``subtitle_info.subtitles`` array) for OAuth 1.0a accounts,
    v2 (``subtitles`` object) for bearer accounts — matching ``_chunked_upload``'s
    host split. ``media_category`` must match the video's. Raises on failure;
    callers attach captions best-effort so a failure never blocks the post."""
    srt_media_id = _chunked_upload(auth, srt_path, media_category="subtitles",
                                   media_type="text/plain; charset=UTF-8")
    lang = (language or "en").strip() or "en"
    name = _LANGUAGE_DISPLAY_NAMES.get(lang.lower(), lang.upper())
    if auth.get("oauth1") is not None:
        body = {
            "media_id": int(video_media_id),
            "media_category": media_category,
            "subtitle_info": {"subtitles": [
                {"media_id": int(srt_media_id), "language_code": lang, "display_name": name},
            ]},
        }
        r = _xreq("POST", SUBTITLES_URL_V11, auth, json=body, timeout=60)
    else:
        cat = "AmplifyVideo" if media_category == "amplify_video" else "TweetVideo"
        body = {
            "id": str(video_media_id),
            "media_category": cat,
            "subtitles": {"id": str(srt_media_id), "language_code": lang, "display_name": name},
        }
        r = _xreq("POST", SUBTITLES_URL, auth, json=body, timeout=60)
    r.raise_for_status()


def _post_tweet(auth: dict, text: str, media_id: str | None = None,
                reply_to: str | None = None, max_len: int = TWEET_TEXT_LIMIT) -> dict:
    body: dict[str, Any] = {"text": text[:max_len]}
    if media_id:
        body["media"] = {"media_ids": [media_id]}
    if reply_to:
        body["reply"] = {"in_reply_to_tweet_id": reply_to}
    r = _xreq("POST", f"{API_BASE}/tweets", auth, json=body, timeout=60)
    r.raise_for_status()
    return r.json().get("data", {})


def _tweet_url(account: str, tweet_id: str) -> str:
    username = (_load_token(account) or {}).get("username", "")
    return f"https://x.com/{username}/status/{tweet_id}" if username else f"https://x.com/i/status/{tweet_id}"


def _is_x_video_too_long(exc: Exception) -> bool:
    """True when an X API error is a video-length rejection. The public API caps
    video duration well below the web UI (~2:20) even for Premium, so a long video
    is rejected at tweet creation — we treat that as the signal to fall back to the
    YouTube link, even if our own duration probe under-read (e.g. ffprobe missing).
    The reason is in the response body; requests' HTTPError str() omits it."""
    text = ""
    resp = getattr(exc, "response", None)
    if resp is not None:
        try:
            text = resp.text or ""
        except Exception:
            text = ""
    text = (text or str(exc)).lower()
    return "longer than" in text or ("video" in text and "minute" in text)


def post_video(client_id: str, client_secret: str, video_path: str, text: str,
               account: str = "", premium: bool | None = None,
               youtube_url: str = "", progress_callback=None,
               captions_path: str = "", language: str = "en") -> dict:
    """Post a video to X. Returns
    {tweet_id, url, error, fell_back_to_link, skipped, reason}.

    Honours the Premium/length rule: Premium → full video; non-Premium over the
    limit → the YouTube link if one exists, else skip with a warning.

    When ``captions_path`` points to an SRT file, a closed-caption track is
    attached to the uploaded video (best-effort — a caption failure is logged but
    never blocks the post, and the link-fallback paths carry no video to caption).
    """
    auth = _account_auth(client_id, client_secret, account)
    if not auth:
        return {"tweet_id": "", "url": "", "error": "Not authenticated. Connect X first.",
                "fell_back_to_link": False, "skipped": True, "reason": ""}
    if not video_path or not Path(video_path).exists():
        return {"tweet_id": "", "url": "", "error": f"Video file not found: {video_path}",
                "fell_back_to_link": False, "skipped": True, "reason": ""}
    if premium is None:
        premium = bool((_load_token(account) or {}).get("premium"))

    decision = decide_post_target(video_path, premium, youtube_url)
    # Premium accounts can post long text; others are capped at 280.
    limit = TWEET_TEXT_LIMIT_PREMIUM if premium else TWEET_TEXT_LIMIT
    try:
        if decision["action"] == "skip":
            logger.warning("X post skipped: %s", decision["reason"])
            return {"tweet_id": "", "url": "", "error": decision["reason"],
                    "fell_back_to_link": False, "skipped": True, "reason": decision["reason"]}
        if decision["action"] == "post_link":
            link_text = f"{text}\n{youtube_url}".strip()
            data = _post_tweet(auth, link_text, max_len=limit)
            tid = data.get("id", "")
            return {"tweet_id": tid, "url": _tweet_url(account, tid), "error": "",
                    "fell_back_to_link": True, "skipped": False, "reason": decision["reason"]}
        # post_full. Long videos use the amplify_video category (the API's
        # longer-video path); if X still rejects the native video — the public
        # API caps length below the web UI even for Premium — fall back to the
        # YouTube link when one exists.
        long = decision["duration_secs"] > X_MAX_VIDEO_SECONDS
        category = "amplify_video" if long else "tweet_video"
        try:
            media_id = _chunked_upload(auth, video_path, progress_callback, media_category=category)
            if captions_path and Path(captions_path).exists():
                try:
                    _attach_subtitles(auth, media_id, captions_path,
                                      language=language, media_category=category)
                    logger.info("Attached captions to X video (%s)", Path(captions_path).name)
                except Exception as cexc:
                    detail = ""
                    resp = getattr(cexc, "response", None)
                    if resp is not None:
                        try:
                            detail = (resp.text or "")[:200]
                        except Exception:
                            detail = ""
                    logger.warning("Could not attach captions to X video: %s %s", cexc, detail)
            data = _post_tweet(auth, text, media_id=media_id, max_len=limit)
        except Exception as exc:
            # Fall back to the YouTube link when X rejects the native video for
            # length — either we already knew it was long, or X says so (our
            # duration probe can under-read). Any other failure re-raises.
            if not (youtube_url and (long or _is_x_video_too_long(exc))):
                raise
            logger.warning("X native long-video post failed (%s); posting the YouTube link instead.",
                           str(exc)[:160])
            data = _post_tweet(auth, f"{text}\n{youtube_url}".strip(), max_len=limit)
            tid = data.get("id", "")
            return {"tweet_id": tid, "url": _tweet_url(account, tid), "error": "",
                    "fell_back_to_link": True, "skipped": False,
                    "reason": "X's API wouldn't accept this video length — posted the YouTube link instead."}
        tid = data.get("id", "")
        logger.info("X post complete: %s (tweet_id=%s)", text[:60], tid)
        return {"tweet_id": tid, "url": _tweet_url(account, tid), "error": "",
                "fell_back_to_link": False, "skipped": False, "reason": ""}
    except requests.HTTPError as exc:
        detail = ""
        try:
            detail = exc.response.text[:300]
        except Exception:
            pass
        logger.warning("X post failed: %s %s", exc, detail)
        return {"tweet_id": "", "url": "", "error": f"{exc} {detail}".strip()[:400],
                "fell_back_to_link": False, "skipped": False, "reason": ""}
    except Exception as exc:
        logger.exception("X post failed")
        return {"tweet_id": "", "url": "", "error": str(exc)[:400],
                "fell_back_to_link": False, "skipped": False, "reason": ""}


# ── Mentions (comment management) ─────────────────────────────────────────────
# X's "comments" are mentions/replies. Records use the SAME shape as YouTube's
# comment dicts so the backend's evaluate/engagement/queue logic and
# pipeline.youtube.thread_anchor are reused unchanged. Reading mentions needs a
# paid X API tier — a 403 surfaces as an error the caller reports, like YouTube.

def _account_user_id(client_id: str, client_secret: str, account: str) -> str:
    """The X numeric user id for an account (from the token, else /2/users/me)."""
    token = _load_token(account) or {}
    if token.get("account_id"):
        return str(token["account_id"])
    auth = _account_auth(client_id, client_secret, account)
    if not auth:
        return ""
    return str(_fetch_me(auth).get("data", {}).get("id", ""))


def fetch_mentions(client_id: str, client_secret: str, account: str = "",
                   max_results: int = 50) -> list[dict]:
    """Fetch recent mentions of the account as comment dicts (YouTube shape).

    Raises RuntimeError when not authenticated (mirrors fetch_channel_comments),
    so the backend sweep reports per-account failures.
    """
    auth = _account_auth(client_id, client_secret, account)
    if not auth:
        raise RuntimeError("Not authenticated. Connect X first.")
    user_id = _account_user_id(client_id, client_secret, account)
    if not user_id:
        raise RuntimeError("Could not resolve the X account id.")
    resp = _api_get(auth, f"/users/{user_id}/mentions", {
        "max_results": min(max(max_results, 5), 100),
        "tweet.fields": "created_at,conversation_id,author_id,public_metrics,in_reply_to_user_id",
        "expansions": "author_id",
        "user.fields": "username",
    })
    users = {u["id"]: u for u in resp.get("includes", {}).get("users", [])}
    out: list[dict] = []
    for t in resp.get("data", []):
        metrics = t.get("public_metrics", {})
        author = users.get(t.get("author_id"), {})
        out.append({
            "comment_id": t["id"],
            "video_id": "",
            "conversation_id": t.get("conversation_id", ""),
            "commenter": author.get("username", "user"),
            "text": t.get("text", ""),
            "published_at": t.get("created_at", ""),
            "like_count": int(metrics.get("like_count", 0)),
            "total_reply_count": int(metrics.get("reply_count", 0)),
            # Full thread reconstruction needs a conversation search (paid tier);
            # mentions carry no inline replies, so engagement anchors on the
            # mention itself, exactly like a fresh top-level YouTube comment.
            "replies": [],
        })
    return out


def reply_to_tweet(client_id: str, client_secret: str, in_reply_to_tweet_id: str,
                   text: str, account: str = "") -> dict:
    """Reply to a tweet (the X mirror of reply_to_comment). Returns {success, error}."""
    if not in_reply_to_tweet_id or not text:
        return {"success": False, "error": "Missing tweet id or reply text"}
    auth = _account_auth(client_id, client_secret, account)
    if not auth:
        return {"success": False, "error": "Not authenticated"}
    try:
        _post_tweet(auth, text, reply_to=in_reply_to_tweet_id)
        logger.info("Replied to tweet %s", in_reply_to_tweet_id)
        return {"success": True, "error": ""}
    except Exception as exc:
        logger.warning("reply_to_tweet failed: %s", exc)
        return {"success": False, "error": str(exc)[:300]}


# ── Comments cache (separate file from YouTube; same record shape) ────────────

def load_comments_cache() -> list[dict]:
    try:
        return json.loads(COMMENTS_CACHE_PATH.read_text())
    except Exception:
        return []


def save_comments_cache(comments: list[dict]) -> None:
    COMMENTS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    COMMENTS_CACHE_PATH.write_text(json.dumps(comments, indent=2))


# ── Analytics ─────────────────────────────────────────────────────────────────
# Returns the same {channel, videos} envelope as fetch_channel_analytics so the
# dashboard renders similarly. public_metrics (likes/reposts/replies/impressions)
# are available on any tier with read access; reading tweets at all needs a paid
# X API tier, so without one this degrades to an empty/error result.

def load_analytics_cache() -> dict[str, dict]:
    try:
        return json.loads(ANALYTICS_CACHE_PATH.read_text())
    except Exception:
        return {}


def save_analytics_cache(cache: dict[str, dict]) -> None:
    ANALYTICS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    ANALYTICS_CACHE_PATH.write_text(json.dumps(cache, indent=2))


def fetch_x_analytics(client_id: str, client_secret: str, account: str = "",
                      max_tweets: int = 25) -> dict:
    """Account-level stats + recent-tweet metrics. {channel, videos} envelope."""
    auth = _account_auth(client_id, client_secret, account)
    if not auth:
        return {"channel": {}, "videos": []}
    try:
        me = _api_get(auth, "/users/me", {
            "user.fields": "username,name,public_metrics,verified_type,subscription_type"})
        data = me.get("data", {})
        pm = data.get("public_metrics", {})
        username = data.get("username", "")
        channel = {
            "name": username,
            "display_name": data.get("name", ""),
            "followers_count": int(pm.get("followers_count", 0)),
            "following_count": int(pm.get("following_count", 0)),
            "tweet_count": int(pm.get("tweet_count", 0)),
            "listed_count": int(pm.get("listed_count", 0)),
            "premium": _premium_from_me(me),
            "impressions": None, "likes": None, "reposts": None, "replies": None,
        }
        videos: list[dict] = []
        # Reading tweets is the paid-tier-gated part — isolate it so a 403 leaves
        # the account header intact and just yields no per-tweet rows.
        try:
            user_id = data.get("id", "")
            resp = _api_get(auth, f"/users/{user_id}/tweets", {
                "max_results": min(max(max_tweets, 5), 100),
                "tweet.fields": "created_at,public_metrics",
                "exclude": "retweets,replies",
            })
            for t in resp.get("data", []):
                m = t.get("public_metrics", {})
                videos.append({
                    "tweet_id": t["id"],
                    "text": t.get("text", "")[:140],
                    "created_at": t.get("created_at", ""),
                    "url": (f"https://x.com/{username}/status/{t['id']}" if username
                            else f"https://x.com/i/status/{t['id']}"),
                    "like_count": int(m.get("like_count", 0)),
                    "retweet_count": int(m.get("retweet_count", 0)),
                    "reply_count": int(m.get("reply_count", 0)),
                    "quote_count": int(m.get("quote_count", 0)),
                    "impression_count": int(m.get("impression_count", 0)),
                })
        except Exception as exc:
            logger.info("X tweet metrics unavailable (may need a paid tier): %s", exc)
        if videos:
            channel["impressions"] = sum(v["impression_count"] for v in videos)
            channel["likes"] = sum(v["like_count"] for v in videos)
            channel["reposts"] = sum(v["retweet_count"] for v in videos)
            channel["replies"] = sum(v["reply_count"] for v in videos)
        logger.info("Fetched X analytics for %d tweets", len(videos))
        return {"channel": channel, "videos": videos}
    except Exception as exc:
        logger.warning("fetch_x_analytics failed: %s", exc)
        return {"channel": {}, "videos": [], "error": str(exc)[:300]}
