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
SUGGESTIONS_PATH = _CONFIG_DIR / "youtube_suggestions.json"
UPLOAD_TRACKING_PATH = _CONFIG_DIR / "youtube_daily_uploads.json"

SCOPES = [
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
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
You are evaluating a YouTube comment for an educational/documentary channel that produces \
high-quality AI-generated documentary videos on history, science, culture, and biography.

Comment from "{commenter}":
{comment_text}

Respond with a JSON object with EXACTLY these fields:
- "is_request": boolean — is this comment clearly asking for a video about a specific, identifiable topic?
- "suggested_title": string — if is_request is true, a clear YouTube video title (e.g. "The Rise and Fall of the Ottoman Empire"); otherwise ""
- "confidence": number 0.0–1.0 — how confident you are this is a genuine video request
- "interestingness": number 0.0–1.0 — if is_request is true, how interesting and suitable this topic is \
for an educational documentary channel (consider: educational value, broad audience appeal, \
documentary potential, topic depth, likely view count, uniqueness vs over-done topics); otherwise 0.0
- "reason": string — one sentence explanation covering both the request classification and interestingness rating
- "suggested_scene_count": integer 6–50 — estimate how many scenes (each ~30–60 seconds) \
would be needed to properly cover this topic; otherwise 20. Use three tiers: \
SHORT (6–11 scenes) for simple or very focused topics; \
MEDIUM (12–39 scenes) for standard documentary topics; \
LARGE (40–50 scenes) for epic, sweeping, or deeply multi-faceted topics \
(e.g. entire civilisations, long historical arcs, complex science subjects).

Classify as is_request=true ONLY if the comment explicitly asks for a video about a named topic.
Vague compliments, questions about the channel, spam, or off-topic messages are NOT requests.
For interestingness: 0.9–1.0 = outstanding topic with broad appeal; 0.7–0.9 = solid documentary topic; \
0.5–0.7 = decent but niche; below 0.5 = poor fit for documentary format.

Output ONLY the JSON object, no other text."""

_SAFE_DEFAULT = {
    "is_request": False,
    "suggested_title": "",
    "confidence": 0.0,
    "interestingness": 0.0,
    "reason": "Could not parse LLM response",
    "suggested_scene_count": 20,
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
                "interestingness": float(result.get("interestingness", 0.0)),
                "reason": str(result.get("reason", "")),
                "suggested_scene_count": max(6, min(50, int(result.get("suggested_scene_count", 20)))),
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


# ── Upload error backoff ──────────────────────────────────────────────────────

def set_upload_backoff(hours: float = 2.0) -> None:
    """Record a backoff timestamp after a YouTube upload error. Retries are blocked until it expires."""
    try:
        retry_after = time.time() + hours * 3600
        UPLOAD_TRACKING_PATH.parent.mkdir(parents=True, exist_ok=True)
        UPLOAD_TRACKING_PATH.write_text(json.dumps({"retry_after": retry_after}))
        logger.info("Upload backoff set — retries blocked for %.0f hours (until %.0f)", hours, retry_after)
    except Exception as exc:
        logger.warning("set_upload_backoff failed: %s", exc)


def clear_upload_backoff() -> None:
    """Clear any active upload backoff (e.g. user manually resets)."""
    try:
        if UPLOAD_TRACKING_PATH.exists():
            UPLOAD_TRACKING_PATH.write_text(json.dumps({}))
    except Exception as exc:
        logger.warning("clear_upload_backoff failed: %s", exc)


def upload_backoff_remaining() -> float:
    """Return seconds until the upload backoff expires, or 0 if not blocked."""
    try:
        data = json.loads(UPLOAD_TRACKING_PATH.read_text())
        retry_after = float(data.get("retry_after", 0))
        remaining = retry_after - time.time()
        return max(0.0, remaining)
    except Exception:
        return 0.0


def is_upload_blocked() -> bool:
    """Return True if uploads are currently blocked due to a recent API error backoff."""
    return upload_backoff_remaining() > 0


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
            "status": {
                "privacyStatus": privacy_status,
                "selfDeclaredMadeForKids": False,
                "containsSyntheticMedia": True,
            },
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
        err_str = str(exc)
        if any(s in err_str for s in ("uploadLimitExceeded", "dailyLimitExceeded",
                                       "rateLimitExceeded", "Video Uploads per day")):
            set_upload_backoff(hours=2)
            return {"video_id": "", "url": "", "error": f"UPLOAD_LIMIT_EXCEEDED: {err_str[:300]}"}
        return {"video_id": "", "url": "", "error": err_str[:400]}


# ── Comment replies ───────────────────────────────────────────────────────────

def reply_to_comment(client_secrets_path: str, parent_comment_id: str, text: str) -> dict:
    """Post a reply to a top-level comment thread. Returns {success, error}."""
    if not parent_comment_id or not text:
        return {"success": False, "error": "Missing comment ID or reply text"}
    creds = _load_credentials(client_secrets_path)
    if not creds:
        return {"success": False, "error": "Not authenticated"}
    try:
        _Creds, _Req, _Flow, build, _MFU = _google_imports()
        youtube = build("youtube", "v3", credentials=creds)
        youtube.comments().insert(
            part="snippet",
            body={
                "snippet": {
                    "parentId": parent_comment_id,
                    "textOriginal": text,
                }
            },
        ).execute()
        logger.info("Replied to comment %s", parent_comment_id)
        return {"success": True, "error": ""}
    except Exception as exc:
        logger.warning("reply_to_comment failed: %s", exc)
        return {"success": False, "error": str(exc)[:300]}


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


def _queue_sort_key(item: dict) -> tuple:
    """Sort key for pending queue items: comment requests first (oldest first), then the rest."""
    source = item.get("source", "suggestion")
    group = 0 if source == "comment" else 1
    return (group, item.get("created_at", 0))


def sort_queue_by_priority(queue: list[dict]) -> list[dict]:
    """Return queue with pending items sorted by priority; non-pending items stay at the end."""
    non_pending = [q for q in queue if q.get("status") not in ("pending",)]
    pending = [q for q in queue if q.get("status") == "pending"]
    pending.sort(key=_queue_sort_key)
    return pending + non_pending


def add_to_queue(comment: dict, final_title: str, source: str = "") -> dict:
    """Add an approved video request to the queue. Returns the new entry (or {} if duplicate).

    source: "comment" | "suggestion" | "manual". Inferred from comment_id if not provided.
    Comment requests are inserted before suggestions (FIFO within each group).
    """
    queue = load_queue()
    existing_ids = {e.get("comment_id") for e in queue if e.get("comment_id")}
    comment_id = comment.get("comment_id", "")
    if comment_id and comment_id in existing_ids:
        return {}
    if not source:
        source = "comment" if comment_id else "suggestion"
    entry = {
        "id": str(uuid.uuid4())[:8],
        "comment_id": comment_id,
        "video_id": comment.get("video_id", ""),
        "commenter": comment.get("commenter", ""),
        "comment_text": comment.get("text", ""),
        "final_title": final_title,
        "suggested_scene_count": comment.get("suggested_scene_count", 20),
        "interestingness": float(comment.get("interestingness", 0.5)),
        "source": source,
        "status": "pending",
        "created_at": time.time(),
        "video_job_id": None,
        "youtube_video_id": None,
        "youtube_url": None,
    }
    # Insert at the correct priority position among pending items.
    # Non-pending items (creating/done/posted) stay at the end.
    pending_indices = [i for i, q in enumerate(queue) if q.get("status") == "pending"]
    if source == "comment":
        # After the last existing pending comment, before the first pending suggestion
        insert_at = next(
            (i for i in pending_indices if queue[i].get("source", "suggestion") != "comment"),
            pending_indices[-1] + 1 if pending_indices else 0,
        )
    else:
        # After all pending items
        insert_at = pending_indices[-1] + 1 if pending_indices else 0
    queue.insert(insert_at, entry)
    save_queue(queue)
    return entry


def move_queue_item(item_id: str, direction: int) -> bool:
    """Move a pending queue item up (direction=-1) or down (direction=1) among pending items."""
    queue = load_queue()
    pending_indices = [i for i, q in enumerate(queue) if q.get("status") == "pending"]
    try:
        item_idx = next(i for i, q in enumerate(queue) if q.get("id") == item_id)
    except StopIteration:
        return False
    pos_in_pending = pending_indices.index(item_idx) if item_idx in pending_indices else -1
    if pos_in_pending == -1:
        return False  # item is not pending, cannot reorder
    target_pos = pos_in_pending + direction
    if target_pos < 0 or target_pos >= len(pending_indices):
        return False
    other_idx = pending_indices[target_pos]
    queue[item_idx], queue[other_idx] = queue[other_idx], queue[item_idx]
    save_queue(queue)
    return True


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


# ── Suggestions ───────────────────────────────────────────────────────────────

def load_suggestions() -> list[dict]:
    """Load LLM-generated video topic suggestions from disk."""
    try:
        return json.loads(SUGGESTIONS_PATH.read_text())
    except Exception:
        return []


def save_suggestions(suggestions: list[dict]) -> None:
    SUGGESTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SUGGESTIONS_PATH.write_text(json.dumps(suggestions, indent=2))


# ── Channel analytics ─────────────────────────────────────────────────────────

def _parse_duration(iso: str) -> str:
    """Convert ISO 8601 video duration (PT5M30S) to m:ss / h:mm:ss."""
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
    if not m:
        return ""
    h, mins, s = int(m.group(1) or 0), int(m.group(2) or 0), int(m.group(3) or 0)
    if h:
        return f"{h}:{mins:02d}:{s:02d}"
    return f"{mins}:{s:02d}"


def fetch_channel_analytics(client_secrets_path: str, max_videos: int = 25) -> dict:
    """Return channel-level stats and per-video stats for the most recent uploads.

    Returns:
        {
          channel: {name, subscriber_count, view_count, video_count,
                    watch_time_minutes, avg_view_duration_secs},
          videos: [{video_id, title, published_at, thumbnail_url, view_count,
                    like_count, comment_count, duration, privacy,
                    watch_time_minutes, avg_view_duration_secs}]
        }
    """
    import datetime
    creds = _load_credentials(client_secrets_path)
    if not creds:
        return {"channel": {}, "videos": []}
    try:
        _Creds, _Req, _Flow, build, _MFU = _google_imports()
        youtube = build("youtube", "v3", credentials=creds)

        # Channel stats + uploads playlist ID in one call
        ch_resp = youtube.channels().list(
            part="snippet,statistics,contentDetails", mine=True
        ).execute()
        ch_items = ch_resp.get("items", [])
        if not ch_items:
            return {"channel": {}, "videos": []}
        ch = ch_items[0]
        stats = ch.get("statistics", {})
        channel = {
            "name": ch["snippet"]["title"],
            "subscriber_count": int(stats.get("subscriberCount") or 0),
            "view_count": int(stats.get("viewCount") or 0),
            "video_count": int(stats.get("videoCount") or 0),
            "watch_time_minutes": None,
            "avg_view_duration_secs": None,
        }
        uploads_id = ch["contentDetails"]["relatedPlaylists"]["uploads"]

        # Collect video IDs from the uploads playlist
        video_ids: list[str] = []
        next_page: str | None = None
        while len(video_ids) < max_videos:
            resp = youtube.playlistItems().list(
                part="contentDetails",
                playlistId=uploads_id,
                maxResults=min(50, max_videos - len(video_ids)),
                pageToken=next_page,
            ).execute()
            for item in resp.get("items", []):
                vid = item["contentDetails"].get("videoId", "")
                if vid:
                    video_ids.append(vid)
            next_page = resp.get("nextPageToken")
            if not next_page:
                break

        # Batch-fetch snippet + statistics + contentDetails + status
        videos: list[dict] = []
        for i in range(0, len(video_ids), 50):
            batch = video_ids[i:i + 50]
            vresp = youtube.videos().list(
                part="snippet,statistics,contentDetails,status",
                id=",".join(batch),
            ).execute()
            for v in vresp.get("items", []):
                snip = v.get("snippet", {})
                vstats = v.get("statistics", {})
                thumb = (
                    snip.get("thumbnails", {}).get("medium", {}).get("url")
                    or snip.get("thumbnails", {}).get("default", {}).get("url")
                    or ""
                )
                videos.append({
                    "video_id": v["id"],
                    "title": snip.get("title", ""),
                    "published_at": snip.get("publishedAt", ""),
                    "thumbnail_url": thumb,
                    "view_count": int(vstats.get("viewCount") or 0),
                    "like_count": int(vstats.get("likeCount") or 0),
                    "comment_count": int(vstats.get("commentCount") or 0),
                    "duration": _parse_duration(v.get("contentDetails", {}).get("duration", "")),
                    "privacy": v.get("status", {}).get("privacyStatus", ""),
                    "watch_time_minutes": None,
                    "avg_view_duration_secs": None,
                })

        # Re-sort newest first (playlist order, but batch call may shuffle)
        id_order = {vid: idx for idx, vid in enumerate(video_ids)}
        videos.sort(key=lambda v: id_order.get(v["video_id"], 999))

        # ── YouTube Analytics API (yt-analytics.readonly scope) ───────────────
        # Graceful fallback: if the token pre-dates this scope or the API fails,
        # we still return all Data API fields above.
        today = datetime.date.today().isoformat()
        try:
            yt_analytics = build("youtubeAnalytics", "v2", credentials=creds)

            # Channel-level totals
            ch_rep = yt_analytics.reports().query(
                ids="channel==MINE",
                startDate="2000-01-01",
                endDate=today,
                metrics="estimatedMinutesWatched,averageViewDuration",
            ).execute()
            ch_rows = ch_rep.get("rows", [])
            if ch_rows:
                channel["watch_time_minutes"] = int(ch_rows[0][0] or 0)
                channel["avg_view_duration_secs"] = int(ch_rows[0][1] or 0)

            # Per-video watch time + avg duration
            if video_ids:
                vid_rep = yt_analytics.reports().query(
                    ids="channel==MINE",
                    startDate="2000-01-01",
                    endDate=today,
                    dimensions="video",
                    metrics="estimatedMinutesWatched,averageViewDuration",
                    filters="video==" + ",".join(video_ids),
                    maxResults=len(video_ids),
                ).execute()
                headers = [h["name"] for h in vid_rep.get("columnHeaders", [])]
                vid_idx = headers.index("video") if "video" in headers else None
                wt_idx = headers.index("estimatedMinutesWatched") if "estimatedMinutesWatched" in headers else None
                avd_idx = headers.index("averageViewDuration") if "averageViewDuration" in headers else None
                vid_map: dict[str, dict] = {}
                for row in vid_rep.get("rows", []):
                    if vid_idx is not None:
                        vid_id = row[vid_idx]
                        vid_map[vid_id] = {
                            "watch_time_minutes": int(row[wt_idx] or 0) if wt_idx is not None else None,
                            "avg_view_duration_secs": int(row[avd_idx] or 0) if avd_idx is not None else None,
                        }
                for v in videos:
                    if v["video_id"] in vid_map:
                        v.update(vid_map[v["video_id"]])
        except Exception as exc:
            logger.info("YouTube Analytics API unavailable (may need re-auth): %s", exc)

        logger.info("Fetched analytics for %d videos", len(videos))
        return {"channel": channel, "videos": videos}
    except Exception as exc:
        logger.warning("fetch_channel_analytics failed: %s", exc)
        return {"channel": {}, "videos": [], "error": str(exc)[:300]}


# ── Channel video title fetching ──────────────────────────────────────────────

def fetch_channel_video_titles(client_secrets_path: str, max_results: int = 50) -> list[str]:
    """Return titles of videos uploaded to the authenticated user's channel (newest first)."""
    creds = _load_credentials(client_secrets_path)
    if not creds:
        return []
    try:
        _Creds, _Req, _Flow, build, _MFU = _google_imports()
        youtube = build("youtube", "v3", credentials=creds)

        # Get the uploads playlist ID for this channel
        ch_resp = youtube.channels().list(part="contentDetails", mine=True).execute()
        items = ch_resp.get("items", [])
        if not items:
            return []
        uploads_id = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]

        titles: list[str] = []
        next_page: str | None = None
        while len(titles) < max_results:
            resp = youtube.playlistItems().list(
                part="snippet",
                playlistId=uploads_id,
                maxResults=min(50, max_results - len(titles)),
                pageToken=next_page,
            ).execute()
            for item in resp.get("items", []):
                t = item["snippet"].get("title", "")
                if t and t not in ("Private video", "Deleted video"):
                    titles.append(t)
            next_page = resp.get("nextPageToken")
            if not next_page:
                break
        logger.info("Fetched %d channel video titles", len(titles))
        return titles
    except Exception as exc:
        logger.warning("fetch_channel_video_titles failed: %s", exc)
        return []
