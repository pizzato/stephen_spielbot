"""YouTube Data API v3 integration for Stephen Spielbot.

Handles:
- OAuth2 authentication (InstalledAppFlow with local redirect server)
- Fetching channel comment threads
- LLM-based video-request evaluation
- Video upload with thumbnail support
- Comments cache and video request queue (JSON files in config dir)
"""
from __future__ import annotations

import json
import logging
import re
import concurrent.futures
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger("video_gen")

# ── Auth status cache (avoids a live API call on every tab visit) ─────────────
_auth_cache: dict[str, Any] = {"result": None, "ts": 0.0, "path": ""}
_AUTH_CACHE_TTL = 60.0  # seconds before re-checking with the API

_CONFIG_DIR = Path.home() / ".config" / "video-generator"
_TOKEN_PATH = _CONFIG_DIR / "youtube_token.json"
COMMENTS_CACHE_PATH = _CONFIG_DIR / "youtube_comments.json"
QUEUE_PATH = _CONFIG_DIR / "youtube_queue.json"

SCOPES = [
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",
]

CATEGORY_OPTIONS = {
    "People & Blogs": "22",
    "Education": "27",
    "Entertainment": "24",
    "Science & Technology": "28",
    "Film & Animation": "1",
    "Howto & Style": "26",
    "News & Politics": "25",
}
PRIVACY_OPTIONS = ["private", "unlisted", "public"]


# ── Lazy imports ──────────────────────────────────────────────────────────────

def _google_imports():
    """Import Google API libraries, raising a clear error if missing."""
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
        return Credentials, Request, InstalledAppFlow, build, MediaFileUpload
    except ImportError as exc:
        raise ImportError(
            "YouTube API libraries not installed. "
            "Run: pip install google-auth google-auth-oauthlib "
            "google-auth-httplib2 google-api-python-client"
        ) from exc


# ── Credentials management ────────────────────────────────────────────────────

def _load_credentials(client_secrets_path: str) -> Any | None:
    """Return valid Credentials from saved token, refreshing if needed. Returns None if not authenticated."""
    Credentials, Request, _Flow, _build, _MFU = _google_imports()
    if not _TOKEN_PATH.exists():
        return None
    try:
        creds = Credentials.from_authorized_user_file(str(_TOKEN_PATH), SCOPES)
    except Exception as exc:
        logger.warning("Failed to load YouTube token: %s", exc)
        return None
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _TOKEN_PATH.write_text(creds.to_json())
            return creds
        except Exception as exc:
            logger.warning("Failed to refresh YouTube token: %s", exc)
    return None


def check_auth_status(client_secrets_path: str, force: bool = False) -> dict:
    """Return {connected, channel_name, channel_id, error}.

    Results are cached for _AUTH_CACHE_TTL seconds to avoid blocking the UI on
    every tab visit.  Pass force=True (e.g. after connect/disconnect) to bypass.
    """
    if not client_secrets_path or not Path(client_secrets_path).expanduser().exists():
        return {
            "connected": False, "channel_name": "", "channel_id": "",
            "error": "client_secrets.json path not configured (see Config tab → YouTube section)",
        }
    now = time.time()
    if (not force
            and _auth_cache["result"] is not None
            and _auth_cache["path"] == client_secrets_path
            and (now - _auth_cache["ts"]) < _AUTH_CACHE_TTL):
        return _auth_cache["result"]
    # Wrap the ENTIRE check (credential load + possible token refresh + API call)
    # in a background thread with a hard timeout.  _load_credentials() calls
    # creds.refresh(Request()) which is a network call with no built-in timeout
    # and can block for 30+ seconds if the network is slow or stalled.
    def _full_check() -> dict:
        creds = _load_credentials(client_secrets_path)
        if not creds:
            return {
                "connected": False, "channel_name": "", "channel_id": "",
                "error": "Not authenticated — click Connect YouTube",
            }
        _Creds, _Req, _Flow, build, _MFU = _google_imports()
        yt = build("youtube", "v3", credentials=creds)
        resp = yt.channels().list(part="snippet", mine=True).execute()
        items = resp.get("items", [])
        if not items:
            return {"connected": True, "channel_name": "(no channel found)", "channel_id": "", "error": ""}
        item = items[0]
        return {
            "connected": True,
            "channel_name": item["snippet"]["title"],
            "channel_id": item["id"],
            "error": "",
        }

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = pool.submit(_full_check)
    try:
        result = future.result(timeout=5)
    except concurrent.futures.TimeoutError:
        result = {"connected": False, "channel_name": "", "channel_id": "",
                  "error": "Connection check timed out — check your network"}
    except Exception as exc:
        result = {"connected": False, "channel_name": "", "channel_id": "", "error": str(exc)[:300]}
    finally:
        pool.shutdown(wait=False)  # don't block — let any slow I/O finish in background
    _auth_cache.update(result=result, ts=now, path=client_secrets_path)
    return result


# ── OAuth2 flow ───────────────────────────────────────────────────────────────

class _AuthFlow:
    def __init__(self):
        self.running = False
        self.result: dict | None = None
        self._thread: threading.Thread | None = None

    def start(self, client_secrets_path: str) -> str:
        if self.running:
            return "Auth flow already in progress — check your browser."
        secrets_path = str(Path(client_secrets_path).expanduser())
        if not client_secrets_path or not Path(secrets_path).exists():
            return "Error: client_secrets.json file not found at the configured path."
        self.result = None
        self.running = True

        def _run():
            try:
                _Creds, _Req, InstalledAppFlow, _build, _MFU = _google_imports()
                flow = InstalledAppFlow.from_client_secrets_file(secrets_path, SCOPES)
                creds = flow.run_local_server(port=0, open_browser=True)
                _TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
                _TOKEN_PATH.write_text(creds.to_json())
                self.result = {"success": True, "error": ""}
            except Exception as exc:
                self.result = {"success": False, "error": str(exc)[:300]}
            finally:
                self.running = False

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()
        return "Browser opened — complete the Google authorization in your browser."

    def disconnect(self):
        if _TOKEN_PATH.exists():
            _TOKEN_PATH.unlink()
        self.result = None
        self.running = False


_auth_flow = _AuthFlow()


def start_auth_flow(client_secrets_path: str) -> str:
    return _auth_flow.start(client_secrets_path)


def poll_auth_flow() -> dict:
    return {"running": _auth_flow.running, "result": _auth_flow.result}


def disconnect_youtube():
    _auth_flow.disconnect()


# ── Comment fetching ──────────────────────────────────────────────────────────

def fetch_channel_comments(client_secrets_path: str, max_results: int = 50) -> list[dict]:
    """Fetch recent comment threads from the authenticated user's channel."""
    creds = _load_credentials(client_secrets_path)
    if not creds:
        raise RuntimeError("Not authenticated. Connect YouTube first.")
    _Creds, _Req, _Flow, build, _MFU = _google_imports()
    youtube = build("youtube", "v3", credentials=creds)

    ch_resp = youtube.channels().list(part="id", mine=True).execute()
    items = ch_resp.get("items", [])
    if not items:
        return []

    channel_id = items[0]["id"]
    ct_resp = youtube.commentThreads().list(
        part="snippet",
        allThreadsRelatedToChannelId=channel_id,
        maxResults=min(max_results, 100),
        order="time",
        moderationStatus="published",
    ).execute()

    results = []
    for item in ct_resp.get("items", []):
        top_snippet = item["snippet"]["topLevelComment"]["snippet"]
        results.append({
            "comment_id": item["id"],
            "video_id": item["snippet"].get("videoId", ""),
            "commenter": top_snippet.get("authorDisplayName", "Unknown"),
            "text": top_snippet.get("textOriginal", ""),
            "published_at": top_snippet.get("publishedAt", ""),
            "like_count": int(top_snippet.get("likeCount", 0)),
        })
    return results


# ── LLM evaluation ────────────────────────────────────────────────────────────

_EVAL_PROMPT = """\
You are evaluating a YouTube comment to determine if it is requesting a specific video topic.

Comment from "{commenter}":
{comment_text}

Respond with a JSON object with EXACTLY these fields:
- "is_request": boolean — is this comment clearly asking for a video about a specific, identifiable topic?
- "suggested_title": string — if is_request is true, a clear YouTube video title (e.g. "The Rise and Fall of the Ottoman Empire"); otherwise ""
- "confidence": number 0.0–1.0
- "reason": string — one sentence explanation
- "suggested_scene_count": integer 3–15 — if is_request is true, estimate how many scenes (each ~30–60 seconds) would be needed to properly cover this topic with adequate depth; otherwise 5. A simple topic needs 3–5 scenes; a complex historical or scientific topic needs 8–15.

Classify as is_request=true ONLY if the comment explicitly asks for a video about a named topic.
Vague compliments, questions about the channel, spam, or off-topic messages are NOT requests.

Output ONLY the JSON object, no other text."""

_SAFE_DEFAULT = {
    "is_request": False,
    "suggested_title": "",
    "confidence": 0.0,
    "reason": "Could not parse LLM response",
    "suggested_scene_count": 5,
}


def _parse_eval(text: str) -> dict:
    try:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            result = json.loads(m.group())
            return {
                "is_request": bool(result.get("is_request", False)),
                "suggested_title": str(result.get("suggested_title", "")),
                "confidence": float(result.get("confidence", 0.0)),
                "reason": str(result.get("reason", "")),
                "suggested_scene_count": max(3, min(15, int(result.get("suggested_scene_count", 5)))),
            }
    except Exception:
        pass
    return _SAFE_DEFAULT.copy()


def evaluate_comment(comment_text: str, commenter: str, cfg: dict) -> dict:
    """Return {is_request, suggested_title, confidence, reason} via LLM."""
    prompt = _EVAL_PROMPT.format(
        commenter=(commenter or "Unknown")[:100],
        comment_text=(comment_text or "").strip()[:2000],
    )
    backend = cfg.get("llm_backend", "local")
    try:
        if backend == "claude":
            return _eval_claude(prompt, cfg)
        return _eval_local(prompt, cfg)
    except Exception as exc:
        logger.warning("Comment evaluation failed: %s", exc)
        return {**_SAFE_DEFAULT, "reason": f"Evaluation error: {str(exc)[:120]}"}


def _eval_claude(prompt: str, cfg: dict) -> dict:
    import anthropic
    import os
    api_key = cfg.get("claude_api_key", "") or os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise RuntimeError("No Claude API key configured")
    client = anthropic.Anthropic(api_key=api_key)
    msg = client.messages.create(
        model=cfg.get("claude_model", "claude-sonnet-4-6"),
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )
    return _parse_eval(msg.content[0].text)


def _eval_local(prompt: str, cfg: dict) -> dict:
    url = cfg.get("local_llm_url", "http://localhost:8000/v1/chat/completions")
    model = cfg.get("local_llm_model", "openai/gpt-oss-120b")
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 256,
        "temperature": 0.1,
    }).encode()
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    return _parse_eval(data["choices"][0]["message"]["content"])


# ── Video upload ──────────────────────────────────────────────────────────────

def upload_video(
    client_secrets_path: str,
    video_path: str,
    title: str,
    description: str = "",
    tags: list[str] | None = None,
    category_id: str = "22",
    privacy_status: str = "private",
    thumbnail_path: str | None = None,
    progress_callback=None,
) -> dict:
    """Upload video to YouTube. Returns {video_id, url, error}.

    progress_callback(pct, msg) is called during upload if provided.
    """
    creds = _load_credentials(client_secrets_path)
    if not creds:
        return {"video_id": "", "url": "", "error": "Not authenticated. Connect YouTube first."}
    if not video_path or not Path(video_path).exists():
        return {"video_id": "", "url": "", "error": f"Video file not found: {video_path}"}

    try:
        _Creds, _Req, _Flow, build, MediaFileUpload = _google_imports()
        youtube = build("youtube", "v3", credentials=creds)

        body = {
            "snippet": {
                "title": (title or "Untitled Video")[:100],
                "description": description or "Generated by Stephen Spielbot.",
                "tags": tags or [],
                "categoryId": category_id,
            },
            "status": {"privacyStatus": privacy_status},
        }
        media = MediaFileUpload(
            video_path, mimetype="video/mp4", resumable=True, chunksize=5 * 1024 * 1024
        )
        request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

        file_size = Path(video_path).stat().st_size
        response = None
        while response is None:
            status, response = request.next_chunk()
            if status and progress_callback:
                uploaded = status.resumable_progress
                pct = uploaded / file_size * 100 if file_size else 0
                progress_callback(pct, f"Uploading… {pct:.0f}% ({uploaded / 1024 / 1024:.1f} MB)")

        video_id = response.get("id", "")

        if thumbnail_path and Path(thumbnail_path).exists() and video_id:
            try:
                youtube.thumbnails().set(
                    videoId=video_id,
                    media_body=MediaFileUpload(thumbnail_path),
                ).execute()
                logger.info("Thumbnail uploaded for video %s", video_id)
            except Exception as exc:
                logger.warning("Thumbnail upload failed: %s", exc)

        logger.info("YouTube upload complete: %s (video_id=%s)", title, video_id)
        return {
            "video_id": video_id,
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "error": "",
        }
    except Exception as exc:
        logger.exception("YouTube upload failed")
        return {"video_id": "", "url": "", "error": str(exc)[:400]}


# ── Comments cache ────────────────────────────────────────────────────────────

def load_comments_cache() -> list[dict]:
    try:
        return json.loads(COMMENTS_CACHE_PATH.read_text())
    except Exception:
        return []


def save_comments_cache(comments: list[dict]) -> None:
    COMMENTS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    COMMENTS_CACHE_PATH.write_text(json.dumps(comments, indent=2))


def upsert_comment(comment: dict) -> None:
    """Merge a fetched/evaluated comment into the cache by comment_id."""
    cache = load_comments_cache()
    idx = next((i for i, c in enumerate(cache) if c.get("comment_id") == comment.get("comment_id")), -1)
    if idx >= 0:
        cache[idx].update(comment)
    else:
        cache.insert(0, comment)
    save_comments_cache(cache)


def get_pending_requests(cache: list[dict] | None = None) -> list[dict]:
    """Return comments evaluated as video requests that haven't been approved/rejected."""
    if cache is None:
        cache = load_comments_cache()
    return [
        c for c in cache
        if c.get("is_request") and c.get("status") not in ("approved", "rejected")
    ]


# ── Video request queue ───────────────────────────────────────────────────────

def load_queue() -> list[dict]:
    try:
        return json.loads(QUEUE_PATH.read_text())
    except Exception:
        return []


def save_queue(queue: list[dict]) -> None:
    QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    QUEUE_PATH.write_text(json.dumps(queue, indent=2))


def add_to_queue(comment: dict, final_title: str) -> dict:
    """Add an approved video request to the queue. Returns the new entry (or {} if duplicate)."""
    queue = load_queue()
    existing_ids = {e.get("comment_id") for e in queue if e.get("comment_id")}
    comment_id = comment.get("comment_id", "")
    if comment_id and comment_id in existing_ids:
        return {}
    entry = {
        "id": str(uuid.uuid4())[:8],
        "comment_id": comment_id,
        "video_id": comment.get("video_id", ""),
        "commenter": comment.get("commenter", ""),
        "comment_text": comment.get("text", ""),
        "final_title": final_title,
        "suggested_scene_count": comment.get("suggested_scene_count", 5),
        "status": "pending",
        "created_at": time.time(),
        "video_job_id": None,
        "youtube_video_id": None,
        "youtube_url": None,
    }
    queue.insert(0, entry)
    save_queue(queue)
    return entry


def update_queue_item(item_id: str, **updates) -> bool:
    queue = load_queue()
    for entry in queue:
        if entry.get("id") == item_id:
            entry.update(updates)
            save_queue(queue)
            return True
    return False


def remove_queue_item(item_id: str) -> bool:
    queue = load_queue()
    new_q = [e for e in queue if e.get("id") != item_id]
    if len(new_q) == len(queue):
        return False
    save_queue(new_q)
    return True
