"""X posting backend wiring (issue #107): the background post task routes the
right account + premium flag to pipeline.x.post_video, and surfaces skip /
YouTube-link-fallback outcomes. Mirrors UploadRoutingTests. No network."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ["HOME"] = tempfile.mkdtemp(prefix="spielbot-test-home-")

import app as gapp
import webapp.backend.main as backend


def _film_dir() -> tuple[Path, Path]:
    wd = Path(tempfile.mkdtemp(prefix="spielbot-test-film-"))
    final = wd / "final.mp4"
    final.write_bytes(b"\0" * 20_000)
    return wd, final


class XPostTaskTests(unittest.TestCase):
    def _run(self, post_result, premium=True):
        wd, final = _film_dir()
        with mock.patch.object(backend, "_x_client_creds", return_value=("cid", "sec")), \
             mock.patch.object(backend.xt, "check_auth_status",
                               return_value={"connected": True, "premium": premium}), \
             mock.patch.object(backend.xt, "post_video", return_value=post_result) as pv, \
             mock.patch.object(backend.gapp, "_write_job_meta"), \
             mock.patch.object(backend.yt, "update_queue_item"):
            backend._run_x_post_task(
                "t1", {"text": "Hello", "title": "T", "account": "acc1"}, wd, final)
        return pv

    def test_posts_full_and_passes_account_and_premium(self):
        pv = self._run({"tweet_id": "x1", "url": "https://x.com/u/status/x1",
                        "error": "", "fell_back_to_link": False, "skipped": False, "reason": ""},
                       premium=True)
        self.assertEqual(pv.call_args.kwargs.get("account"), "acc1")
        self.assertTrue(pv.call_args.kwargs.get("premium"))
        self.assertEqual(backend._x_post_tasks["t1"]["status"], "done")

    def test_fallback_to_link_reports_done_with_message(self):
        self._run({"tweet_id": "x2", "url": "https://x.com/u/status/x2", "error": "",
                   "fell_back_to_link": True, "skipped": False, "reason": "too long"},
                  premium=False)
        task = backend._x_post_tasks["t1"]
        self.assertEqual(task["status"], "done")
        self.assertTrue(task["fell_back_to_link"])
        self.assertIn("link", task["message"].lower())

    def test_skip_reports_warning(self):
        self._run({"tweet_id": "", "url": "", "error": "Video too long, no link",
                   "fell_back_to_link": False, "skipped": True, "reason": "Video too long, no link"},
                  premium=False)
        task = backend._x_post_tasks["t1"]
        self.assertEqual(task["status"], "warning")
        self.assertIn("too long", task["message"].lower())

    def test_api_error_reports_error(self):
        self._run({"tweet_id": "", "url": "", "error": "401 Unauthorized",
                   "fell_back_to_link": False, "skipped": False, "reason": ""})
        self.assertEqual(backend._x_post_tasks["t1"]["status"], "error")


class FinalizeNewXAccountTests(unittest.TestCase):
    def setUp(self):
        self.cfg = dict(gapp.DEFAULT_CFG)
        self.cfg["x_accounts"] = []
        self.saved = None

    def _run(self, account_id, username, status=None):
        def save(cfg):
            self.saved = cfg
        with mock.patch.object(backend.gapp, "load_config", return_value=self.cfg), \
             mock.patch.object(backend.gapp, "save_config", side_effect=save), \
             mock.patch.object(backend, "_x_client_creds", return_value=("cid", "sec")), \
             mock.patch.object(backend.xt, "check_auth_status",
                               return_value=status or {"connected": False, "account_id": ""}):
            return backend._finalize_new_x_account(account_id, username)

    def test_new_account_appends_entry(self):
        key = self._run("99", "nine")
        self.assertEqual(key, "99")
        self.assertEqual(self.saved["x_accounts"], [{"id": "99", "name": "nine", "account_id": "99"}])

    def test_reconnect_known_account_reuses_entry(self):
        self.cfg["x_accounts"] = [{"id": "99", "name": "old", "account_id": "99"}]
        key = self._run("99", "newname")
        self.assertEqual(key, "99")
        self.assertEqual(len(self.saved["x_accounts"]), 1)
        self.assertEqual(self.saved["x_accounts"][0]["name"], "newname")


class XFetchAndEvaluateTests(unittest.TestCase):
    def _eval(self, is_request):
        return {"is_request": is_request, "suggested_title": "Rome" if is_request else "",
                "confidence": 0.9, "interestingness": 0.8, "reason": "", "suggested_scene_count": 20}

    def test_sweeps_accounts_stamps_and_queues(self):
        cfg = dict(gapp.DEFAULT_CFG)
        cfg["x_accounts"] = [{"id": "1"}, {"id": "2"}]
        per_acct = {
            "1": [{"comment_id": "t1", "text": "make a video about Rome", "commenter": "a", "replies": []}],
            "2": [{"comment_id": "t2", "text": "nice!", "commenter": "b", "replies": []}],
        }
        saved = {}
        queued = []
        with mock.patch.object(backend.gapp, "load_config", return_value=cfg), \
             mock.patch.object(backend, "_x_client_creds", return_value=("cid", "sec")), \
             mock.patch.object(backend.xt, "fetch_mentions",
                               side_effect=lambda cid, sec, account="": per_acct[account]), \
             mock.patch.object(backend.xt, "load_comments_cache", return_value=[]), \
             mock.patch.object(backend.xt, "save_comments_cache",
                               side_effect=lambda c: saved.update(cache=c)), \
             mock.patch.object(backend.xt, "reply_to_tweet", return_value={"success": True}), \
             mock.patch.object(backend.yt, "evaluate_comment",
                               side_effect=lambda text, who, cfg: self._eval("Rome" in text)), \
             mock.patch.object(backend.yt, "add_to_queue",
                               side_effect=lambda c, t, source="", source_platform="youtube": queued.append((c["comment_id"], source_platform)) or {"id": "q"}), \
             mock.patch.object(backend.yt, "update_queue_item"), \
             mock.patch.object(backend.llm, "generate_video_prompt", return_value=""):
            out = backend._fetch_and_evaluate_x(auto_approve=True)
        self.assertEqual(out["new"], 2)
        chans = {c["comment_id"]: c["channel"] for c in saved["cache"]}
        self.assertEqual(chans, {"t1": "1", "t2": "2"})
        # The request was queued with the X platform tag.
        self.assertIn(("t1", "x"), queued)

    def test_all_accounts_failing_raises(self):
        cfg = dict(gapp.DEFAULT_CFG)
        cfg["x_accounts"] = [{"id": "1"}]
        with mock.patch.object(backend.gapp, "load_config", return_value=cfg), \
             mock.patch.object(backend, "_x_client_creds", return_value=("cid", "sec")), \
             mock.patch.object(backend.xt, "fetch_mentions", side_effect=RuntimeError("403")), \
             mock.patch.object(backend.xt, "load_comments_cache", return_value=[]), \
             mock.patch.object(backend.xt, "save_comments_cache"):
            with self.assertRaises(backend.HTTPException):
                backend._fetch_and_evaluate_x(auto_approve=False)


class CompletionReplyPlatformTests(unittest.TestCase):
    def test_x_sourced_item_replies_on_x(self):
        item = {"id": "q1", "comment_id": "t1", "channel": "acc1", "source_platform": "x"}
        with mock.patch.object(backend, "_x_client_creds", return_value=("cid", "sec")), \
             mock.patch.object(backend.yt, "load_queue", return_value=[item]), \
             mock.patch.object(backend.xt, "reply_to_tweet",
                               return_value={"success": True, "error": ""}) as xr, \
             mock.patch.object(backend.yt, "reply_to_comment") as yr, \
             mock.patch.object(backend.yt, "update_queue_item", return_value=True), \
             mock.patch.object(backend.yt, "load_comments_cache", return_value=[]), \
             mock.patch.object(backend.yt, "save_comments_cache"):
            backend._post_completion_reply("q1", "T", "https://x.com/u/status/t1")
        self.assertEqual(xr.call_args.kwargs.get("account"), "acc1")
        yr.assert_not_called()

    def test_youtube_sourced_item_still_replies_on_youtube(self):
        item = {"id": "q1", "comment_id": "c1", "channel": "UC2"}  # no source_platform → youtube
        with mock.patch.object(backend, "_client_secrets_path", return_value="/tmp/s.json"), \
             mock.patch.object(backend.yt, "load_queue", return_value=[item]), \
             mock.patch.object(backend.yt, "reply_to_comment",
                               return_value={"success": True, "error": ""}) as yr, \
             mock.patch.object(backend.xt, "reply_to_tweet") as xr, \
             mock.patch.object(backend.yt, "update_queue_item", return_value=True), \
             mock.patch.object(backend.yt, "load_comments_cache", return_value=[]), \
             mock.patch.object(backend.yt, "save_comments_cache"):
            backend._post_completion_reply("q1", "T", "https://youtu.be/v1")
        self.assertEqual(yr.call_args.kwargs.get("channel"), "UC2")
        xr.assert_not_called()


class XImportTokensTests(unittest.TestCase):
    def test_valid_tokens_save_and_resolve_account(self):
        import pipeline.x as xt
        me = {"data": {"id": "77", "username": "bot", "subscription_type": "Premium"}}
        saved = {}
        with mock.patch.object(xt, "_fetch_me", return_value=me), \
             mock.patch.object(xt, "_save_token", side_effect=lambda k, t: saved.update(key=k, token=t)):
            res = xt.import_tokens("cid", "sec", "ACCESS", "REFRESH")
        self.assertTrue(res["success"])
        self.assertEqual(res["account_id"], "77")
        self.assertTrue(res["premium"])
        self.assertEqual(saved["key"], "77")
        self.assertEqual(saved["token"]["access_token"], "ACCESS")
        self.assertEqual(saved["token"]["refresh_token"], "REFRESH")

    def test_empty_access_token_fails(self):
        import pipeline.x as xt
        self.assertFalse(xt.import_tokens("cid", "sec", "", "")["success"])

    def test_refreshes_when_access_token_expired(self):
        import pipeline.x as xt
        me = {"data": {"id": "77", "username": "bot"}}

        def fetch(access):
            if access == "ACCESS":
                raise RuntimeError("401 expired")
            return me

        with mock.patch.object(xt, "_fetch_me", side_effect=fetch), \
             mock.patch.object(xt, "_refresh_token",
                               return_value={"access_token": "FRESH", "refresh_token": "R2", "expires_at": 1}), \
             mock.patch.object(xt, "_save_token"):
            res = xt.import_tokens("cid", "sec", "ACCESS", "REFRESH")
        self.assertTrue(res["success"])
        self.assertEqual(res["account_id"], "77")

    def test_backend_import_registers_account(self):
        with mock.patch.object(backend, "_x_client_creds", return_value=("cid", "sec")), \
             mock.patch.object(backend.xt, "import_tokens",
                               return_value={"success": True, "account": "77", "account_id": "77",
                                             "account_name": "bot", "premium": True}) as imp:
            out = backend.x_auth_import(backend.XImportTokensBody(access_token="A", refresh_token="R"))
        self.assertTrue(out["ok"])
        self.assertEqual(imp.call_args.kwargs.get("finalize"), backend._finalize_new_x_account)


class XPostTextTests(unittest.TestCase):
    """X post text = the description body, before the style's sign-off suffix."""

    def test_strips_style_suffix(self):
        wd = Path("/tmp/film")
        with mock.patch.object(backend, "_work_dir_style_name", return_value="S"), \
             mock.patch.object(backend.gapp, "style_settings",
                               return_value={"description_suffix": "SIGN-OFF."}):
            body = backend._strip_description_suffix("Hello world.\n\nSIGN-OFF.", wd, {})
        self.assertEqual(body, "Hello world.")

    def test_prefers_passed_description_over_cached(self):
        wd = Path("/tmp/film")
        with mock.patch.object(backend, "_work_dir_style_name", return_value="S"), \
             mock.patch.object(backend.gapp, "style_settings", return_value={"description_suffix": "SIG"}), \
             mock.patch.object(backend, "_cached_description", return_value="Cached\n\nSIG"):
            self.assertEqual(backend._x_post_text_for(wd, {}, passed="Edited body.\n\nSIG", fallback="Title"),
                             "Edited body.")

    def test_falls_back_to_cached_then_title(self):
        wd = Path("/tmp/film")
        with mock.patch.object(backend, "_work_dir_style_name", return_value="S"), \
             mock.patch.object(backend.gapp, "style_settings", return_value={"description_suffix": "SIG"}), \
             mock.patch.object(backend, "_cached_description", return_value="Cached body\n\nSIG"):
            self.assertEqual(backend._x_post_text_for(wd, {}, passed="", fallback="Title"), "Cached body")
        with mock.patch.object(backend, "_work_dir_style_name", return_value="S"), \
             mock.patch.object(backend.gapp, "style_settings", return_value={"description_suffix": "SIG"}), \
             mock.patch.object(backend, "_cached_description", return_value=""):
            self.assertEqual(backend._x_post_text_for(wd, {}, passed="", fallback="Title"), "Title")


class XAutoPostRetryTests(unittest.TestCase):
    """A failed auto-post must release its claim so a later tick retries — capped
    at _X_AUTO_POST_MAX_ATTEMPTS, after which the job stays claimed (issue #107)."""

    def _job(self, meta: dict) -> Path:
        wd = Path(tempfile.mkdtemp(prefix="spielbot-test-film-"))
        (wd / "job.json").write_text(json.dumps(meta))
        return wd

    def test_release_on_failure_under_cap(self):
        wd = self._job({"status": "done", "_x_auto_post_triggered": True})
        backend._x_auto_release_on_failure(wd)
        m = json.loads((wd / "job.json").read_text())
        self.assertFalse(m["_x_auto_post_triggered"])      # released -> retriable
        self.assertEqual(m["_x_auto_post_attempts"], 1)

    def test_gives_up_at_cap(self):
        wd = self._job({"status": "done", "_x_auto_post_triggered": True,
                        "_x_auto_post_attempts": backend._X_AUTO_POST_MAX_ATTEMPTS - 1})
        backend._x_auto_release_on_failure(wd)
        m = json.loads((wd / "job.json").read_text())
        self.assertTrue(m["_x_auto_post_triggered"])       # stays claimed -> given up
        self.assertEqual(m["_x_auto_post_attempts"], backend._X_AUTO_POST_MAX_ATTEMPTS)

    def test_noop_for_manual_post(self):
        wd = self._job({"status": "done"})                 # never auto-claimed
        backend._x_auto_release_on_failure(wd)
        m = json.loads((wd / "job.json").read_text())
        self.assertNotIn("_x_auto_post_triggered", m)
        self.assertNotIn("_x_auto_post_attempts", m)


class XLongVideoFallbackTests(unittest.TestCase):
    """Premium accounts still hit the API's lower video-length cap (issue #107).
    post_video should fall back to the YouTube link when X rejects the native
    long video, and otherwise post natively."""

    def _post_video(self, tweet_side_effect, duration=300, youtube_url="https://youtu.be/x"):
        import pipeline.x as xt
        with mock.patch.object(xt, "_account_auth", return_value={"bearer": "t"}), \
             mock.patch.object(xt, "decide_post_target",
                               return_value={"action": "post_full", "reason": "",
                                             "duration_secs": duration, "size_bytes": 1}), \
             mock.patch.object(xt, "_chunked_upload", return_value="m1"), \
             mock.patch.object(xt, "_post_tweet", side_effect=tweet_side_effect), \
             mock.patch("pipeline.x.Path.exists", return_value=True):
            return xt.post_video("cid", "sec", "/v.mp4", "Title", account="a",
                                 premium=True, youtube_url=youtube_url)

    def test_falls_back_to_link_when_native_rejected(self):
        def post_tweet(auth, text, media_id=None, reply_to=None, max_len=None):
            if media_id:
                raise RuntimeError("403 longer than 2 minutes")
            return {"id": "L1"}
        res = self._post_video(post_tweet)
        self.assertTrue(res["fell_back_to_link"])
        self.assertEqual(res["tweet_id"], "L1")
        self.assertFalse(res.get("error"))

    def test_no_link_surfaces_the_error(self):
        def post_tweet(auth, text, media_id=None, reply_to=None, max_len=None):
            if media_id:
                raise RuntimeError("403 longer than 2 minutes")
            return {"id": "L1"}
        res = self._post_video(post_tweet, youtube_url="")
        self.assertFalse(res["fell_back_to_link"])
        self.assertIn("longer than", res["error"].lower())

    def test_native_success_no_fallback(self):
        res = self._post_video(lambda auth, text, media_id=None, reply_to=None, max_len=None: {"id": "N1"})
        self.assertFalse(res["fell_back_to_link"])
        self.assertEqual(res["tweet_id"], "N1")


class XImportOAuth1Tests(unittest.TestCase):
    def test_valid_keys_save_oauth1_token(self):
        import pipeline.x as xt
        me = {"data": {"id": "55", "username": "bot", "subscription_type": "Premium"}}
        saved = {}
        with mock.patch.object(xt, "_fetch_me", return_value=me), \
             mock.patch.object(xt, "_save_token", side_effect=lambda k, t: saved.update(key=k, token=t)):
            res = xt.import_oauth1("ak", "asec", "at", "atsec")
        self.assertTrue(res["success"])
        self.assertEqual(res["account_id"], "55")
        self.assertTrue(res["premium"])
        self.assertEqual(saved["token"]["auth"], "oauth1")
        self.assertEqual(saved["token"]["api_key"], "ak")
        self.assertEqual(saved["token"]["access_secret"], "atsec")

    def test_missing_key_fails(self):
        import pipeline.x as xt
        self.assertFalse(xt.import_oauth1("ak", "", "at", "atsec")["success"])

    def test_account_auth_routes_by_token_type(self):
        import pipeline.x as xt
        with mock.patch.object(xt, "_load_token", return_value={"auth": "oauth1", "api_key": "k",
                               "api_secret": "s", "access_token": "t", "access_secret": "ts"}):
            a = xt._account_auth("cid", "sec", "acc")
        self.assertIn("oauth1", a)
        with mock.patch.object(xt, "_load_token", return_value={"access_token": "x"}), \
             mock.patch.object(xt, "_bearer", return_value="BEAR"):
            b = xt._account_auth("cid", "sec", "acc")
        self.assertEqual(b, {"bearer": "BEAR"})

    def test_backend_import_keys_registers_account(self):
        with mock.patch.object(backend.xt, "import_oauth1",
                               return_value={"success": True, "account": "55", "account_id": "55",
                                             "account_name": "bot", "premium": True}) as imp:
            out = backend.x_auth_import_keys(backend.XImportKeysBody(
                api_key="ak", api_secret="asec", access_token="at", access_secret="atsec"))
        self.assertTrue(out["ok"])
        self.assertEqual(imp.call_args.kwargs.get("finalize"), backend._finalize_new_x_account)


class XAnalyticsTests(unittest.TestCase):
    def test_aggregates_public_metrics(self):
        import pipeline.x as xt
        me = {"data": {"id": "9", "username": "bot", "name": "Bot",
                       "public_metrics": {"followers_count": 100, "tweet_count": 5},
                       "subscription_type": "Premium"}}
        tweets = {"data": [{"id": "t1", "text": "hi", "created_at": "2026-01-01",
                            "public_metrics": {"like_count": 3, "retweet_count": 1,
                                               "reply_count": 0, "quote_count": 0,
                                               "impression_count": 50}}]}

        def api_get(auth, path, params=None):
            return me if path == "/users/me" else tweets

        with mock.patch.object(xt, "_account_auth", return_value={"bearer": "tok"}), \
             mock.patch.object(xt, "_api_get", side_effect=api_get):
            data = xt.fetch_x_analytics("cid", "sec", account="acc1")
        self.assertEqual(data["channel"]["followers_count"], 100)
        self.assertTrue(data["channel"]["premium"])
        self.assertEqual(data["channel"]["impressions"], 50)
        self.assertEqual(data["videos"][0]["like_count"], 3)

    def test_no_auth_returns_empty(self):
        import pipeline.x as xt
        with mock.patch.object(xt, "_account_auth", return_value=None):
            data = xt.fetch_x_analytics("cid", "sec", account="acc1")
        self.assertEqual(data, {"channel": {}, "videos": []})

    def test_backend_serves_cache_first(self):
        cached = {"acc1": {"channel": {"name": "bot"}, "videos": []}}
        with mock.patch.object(backend.xt, "load_analytics_cache", return_value=cached), \
             mock.patch.object(backend.xt, "fetch_x_analytics") as fetch, \
             mock.patch.object(backend, "_x_client_creds", return_value=("cid", "sec")):
            out = backend.x_analytics(account="acc1", refresh=False)
        self.assertEqual(out["channel"]["name"], "bot")
        fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
