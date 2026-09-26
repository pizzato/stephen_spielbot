"""Instagram Reels via Meta's Facebook Login API and direct video uploads.

Page tokens are kept outside config.yaml, one per verified Instagram account.
No SDK or public video hosting is required. See docs/instagram_setup.md.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

API_BASE = "https://graph.facebook.com/v22.0"
CONFIG_DIR = Path.home() / ".config" / "video-generator"
MAX_VIDEO_BYTES = 1_000_000_000
MAX_CAPTION_LENGTH = 2200
POLL_SECONDS = 5
POLL_ATTEMPTS = 120


class InstagramError(RuntimeError):
    """A safe, user-facing error (never a raw HTTP exception or token)."""


class PublishUncertain(InstagramError):
    """The publish request may have succeeded; do not blindly send it again."""


def _account_key(account: str) -> str:
    if not re.fullmatch(r"[0-9]+", account or ""):
        raise InstagramError("Choose a connected Instagram account.")
    return account


def _token_path(account: str) -> Path:
    return CONFIG_DIR / f"instagram_token_{_account_key(account)}.json"


def _write_json(path: Path, data: dict) -> None:
    """Replace atomically, keeping credentials private even during the write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _request(method: str, url: str, token: str, *, publishing=False, **kwargs) -> dict:
    headers = {"Authorization": f"Bearer {token}", **kwargs.pop("headers", {})}
    uncertain = PublishUncertain if publishing else InstagramError
    try:
        response = requests.request(method, url, headers=headers, timeout=kwargs.pop("timeout", (15, 60)),
                                    allow_redirects=False, **kwargs)
    except requests.RequestException:
        raise uncertain("Instagram could not be reached. Check your connection and the account's posts.") from None
    try:
        data = response.json()
    except ValueError:
        raise uncertain("Instagram returned an unreadable response. Check the account's posts.") from None
    if not isinstance(data, dict):
        raise uncertain("Instagram returned an unexpected response.")
    if not 200 <= response.status_code < 300 or data.get("error"):
        error = data.get("error") or {}
        code = error.get("code") if isinstance(error, dict) else None
        # Do not surface raw API messages: they can include credentials or URLs.
        if code == 190:
            message = "Instagram token expired or was revoked. Reconnect in Settings → Channels."
        elif code in (10, 200):
            message = "Instagram publishing permission is missing. Check the Meta app permissions and reconnect."
        elif code in (4, 17, 32, 613) or response.status_code == 429:
            message = "Instagram's request or publishing limit was reached. Try again later."
        else:
            message = "Instagram rejected the request. Check the account permissions and Reel requirements."
        if isinstance(code, int):
            message += f" (Meta code {code})"
        raise (uncertain if response.status_code >= 500 else InstagramError)(message)
    return data


def _graph(method: str, endpoint: str, token: str, **kwargs) -> dict:
    return _request(method, f"{API_BASE}/{endpoint}", token, **kwargs)


def connect_account(access_token: str) -> dict:
    token = access_token.strip()
    if not token or any(c.isspace() for c in token):
        raise InstagramError("Enter a Facebook Page access token.")
    page = _graph("GET", "me", token, params={"fields": "id,name,instagram_business_account{id,username}"})
    profile = page.get("instagram_business_account") or {}
    if not profile.get("id") or not profile.get("username"):
        raise InstagramError("Use a Facebook Page token for a Page linked to an Instagram Business or Creator account.")
    account = _account_key(str(profile["id"]))
    # Verify publishing access as well as identity before keeping the token.
    _graph("GET", f"{account}/content_publishing_limit", token)
    info = {"id": account, "name": profile["username"], "page_name": page.get("name", "")}
    _write_json(_token_path(account), {**info, "access_token": token})
    return info


def list_accounts() -> list[dict]:
    accounts = []
    for path in sorted(CONFIG_DIR.glob("instagram_token_*.json")):
        try:
            data = json.loads(path.read_text())
            if _token_path(data["id"]) != path or not data.get("access_token"):
                continue
            accounts.append({k: data.get(k, "") for k in ("id", "name", "page_name")})
        except (OSError, ValueError, KeyError, TypeError, InstagramError):
            continue
    return accounts


def disconnect_account(account: str) -> None:
    _token_path(account).unlink(missing_ok=True)


def _load_token(account: str) -> str:
    try:
        data = json.loads(_token_path(account).read_text())
        token = data.get("access_token")
        if data.get("id") == account and isinstance(token, str) and token:
            return token
    except (OSError, ValueError, TypeError):
        pass
    raise InstagramError("Reconnect this Instagram account in Settings → Channels.")


def validate_video(path: Path) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise InstagramError("No finished video was found.")
    if path.suffix.lower() not in (".mp4", ".mov") or path.stat().st_size > MAX_VIDEO_BYTES:
        raise InstagramError("Instagram Reels require an MP4 or MOV file no larger than 1 GB.")
    from pipeline.assembler import _resolve_media_tool
    try:
        result = subprocess.run(
            [_resolve_media_tool("ffprobe"), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30, check=True,
        )
        info = json.loads(result.stdout)
        video = next(s for s in info["streams"] if s.get("codec_type") == "video")
        duration = float(info["format"]["duration"])
        num, den = video["avg_frame_rate"].split("/")
        fps = float(num) / float(den)
        width = int(video["width"])
        audios = [s for s in info["streams"] if s.get("codec_type") == "audio"]
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, StopIteration, ZeroDivisionError):
        raise InstagramError("Could not inspect this video. Check that ffprobe is installed and the final cut plays.") from None
    if not 3 <= duration <= 900:
        raise InstagramError("Instagram Reels must be between 3 seconds and 15 minutes long.")
    if video.get("codec_name") not in ("h264", "hevc") or not 23 <= fps <= 60 or width > 1920:
        raise InstagramError("Instagram needs H.264 or HEVC video at 23–60 fps, at most 1920 pixels wide. Render a compatible cut first.")
    if any(a.get("codec_name") != "aac" or int(a.get("sample_rate", 0)) > 48000 or int(a.get("channels", 0)) > 2 for a in audios):
        raise InstagramError("Instagram needs AAC audio, at most 48 kHz and two channels.")


def state_path(work_dir: Path, account: str) -> Path:
    return work_dir / f"instagram_publish_{_account_key(account)}.json"


def read_state(work_dir: Path, account: str) -> dict:
    try:
        data = json.loads(state_path(work_dir, account).read_text())
    except FileNotFoundError:
        return {"status": "idle"}
    if not isinstance(data, dict) or data.get("status") not in ("idle", "uploading", "processing", "publishing", "done", "error", "uncertain"):
        raise InstagramError("Instagram publishing history is unreadable. Restore it before retrying.")
    return data


def write_state(work_dir: Path, account: str, data: dict) -> None:
    _write_json(state_path(work_dir, account), {**data, "updated_at": time.time()})


def published_posts(work_dir: Path) -> list[dict]:
    posts = []
    for path in work_dir.glob("instagram_publish_*.json"):
        try:
            data = json.loads(path.read_text())
            if data.get("status") == "done" and data.get("media_id"):
                posts.append(data)
        except (OSError, ValueError, AttributeError):
            continue
    return posts


def publish_reel(path: Path, caption: str, account: str, share_to_feed: bool, progress) -> dict:
    """Upload once, await processing, then publish once. Persist before publishing.

    The caller holds a per-film/account lock and saves progress durably, so a
    lost response or process restart cannot silently create a duplicate Reel.
    """
    _account_key(account)
    if len(caption) > MAX_CAPTION_LENGTH:
        raise InstagramError("Instagram captions can contain at most 2,200 characters.")
    validate_video(path)
    token = _load_token(account)
    container = _graph("POST", f"{account}/media", token, data={
        "media_type": "REELS", "upload_type": "resumable", "caption": caption,
        "share_to_feed": "true" if share_to_feed else "false",
    })
    container_id = str(container.get("id") or "")
    upload_url = str(container.get("uri") or "")
    parsed = urlparse(upload_url)
    if not container_id.isdigit() or parsed.scheme != "https" or parsed.hostname != "rupload.facebook.com" or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise InstagramError("Instagram returned an invalid upload destination.")
    progress(status="uploading", container_id=container_id)
    with path.open("rb") as stream:
        uploaded = _request("POST", upload_url, token, data=stream, timeout=(15, 600), headers={
            "Authorization": f"OAuth {token}", "offset": "0", "file_size": str(path.stat().st_size),
            "Content-Type": "application/octet-stream",
        })
    if not uploaded.get("success"):
        raise InstagramError("Instagram did not accept the video upload.")
    progress(status="processing")
    for _ in range(POLL_ATTEMPTS):
        status = _graph("GET", container_id, token, params={"fields": "status_code"}).get("status_code")
        if status == "FINISHED":
            break
        if status in ("ERROR", "EXPIRED"):
            raise InstagramError("Instagram could not process this Reel. Check the video requirements and try again.")
        if status == "PUBLISHED":
            raise PublishUncertain("Instagram reports this container is already published. Check the account before retrying.")
        time.sleep(POLL_SECONDS)
    else:
        raise InstagramError("Instagram is still processing after 10 minutes. No publish request was sent; try again later.")
    progress(status="publishing")
    published = _graph("POST", f"{account}/media_publish", token, publishing=True, data={"creation_id": container_id})
    media_id = str(published.get("id") or "")
    if not media_id.isdigit():
        raise PublishUncertain("Instagram did not return a post ID. Check the account before retrying.")
    # Commit success before the optional permalink lookup: a lookup failure must
    # never turn a successfully published Reel into a retryable failure.
    progress(status="done", media_id=media_id)
    url = ""
    try:
        link = _graph("GET", media_id, token, params={"fields": "permalink"}).get("permalink", "")
        parsed = urlparse(link)
        if parsed.scheme == "https" and parsed.hostname in ("www.instagram.com", "instagram.com"):
            url = link
    except InstagramError:
        pass
    return {"media_id": media_id, "url": url}
