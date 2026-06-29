"""Tests for the issue #88 regenerate endpoints: comment reply drafting, Create
brief improvement, and YouTube title regeneration. The LLM call is mocked so the
tests assert prompt wiring and response shaping, not model output."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ["HOME"] = tempfile.mkdtemp(prefix="spielbot-test-home-")

import webapp.backend.main as backend

# Work dirs must live under OUTPUT_DIR (endpoints reject paths outside it).
_OUT = Path(tempfile.mkdtemp(prefix="spielbot-test-out-"))


class DraftReplyTests(unittest.TestCase):
    def test_draft_reply_uses_channel_guidance_and_returns_reply(self):
        comment = {"comment_id": "c1", "channel": "chanA", "text": "Love this!",
                   "commenter": "Sam", "replies": []}
        cfg = {"youtube_channels": [{"id": "chanA", "engagement_prompt": "Be playful."}]}
        with mock.patch.object(backend.yt, "load_comments_cache", return_value=[comment]), \
             mock.patch.object(backend.gapp, "load_config", return_value=cfg), \
             mock.patch.object(backend, "_llm_complete", return_value='"Thanks Sam!"') as llm:
            result = backend.youtube_draft_reply(backend.CommentActionBody(comment_id="c1"))

        # The surrounding quotes from the model are stripped.
        self.assertEqual(result, {"reply": "Thanks Sam!"})
        user_prompt = llm.call_args.args[1]
        self.assertIn("Be playful.", user_prompt)   # channel voice fed in
        self.assertIn("Love this!", user_prompt)    # the comment text fed in

    def test_draft_reply_missing_comment_is_404(self):
        with mock.patch.object(backend.yt, "load_comments_cache", return_value=[]):
            with self.assertRaises(backend.HTTPException) as ctx:
                backend.youtube_draft_reply(backend.CommentActionBody(comment_id="nope"))
        self.assertEqual(ctx.exception.status_code, 404)


class CreateImproveTests(unittest.TestCase):
    def test_improve_title_returns_value_with_current_text_in_prompt(self):
        with mock.patch.object(backend.gapp, "load_config", return_value={}), \
             mock.patch.object(backend, "_llm_complete", return_value="A Sharper Title") as llm:
            result = backend.create_improve(
                backend.BriefImproveBody(field="title", title="old title", direction="angle"))
        self.assertEqual(result, {"value": "A Sharper Title"})
        self.assertIn("old title", llm.call_args.args[1])

    def test_improve_direction_returns_value(self):
        with mock.patch.object(backend.gapp, "load_config", return_value={}), \
             mock.patch.object(backend, "_llm_complete", return_value="Sharper direction."):
            result = backend.create_improve(
                backend.BriefImproveBody(field="direction", title="t", direction="vague"))
        self.assertEqual(result, {"value": "Sharper direction."})

    def test_improve_rejects_unknown_field(self):
        with self.assertRaises(backend.HTTPException) as ctx:
            backend.create_improve(backend.BriefImproveBody(field="bogus"))
        self.assertEqual(ctx.exception.status_code, 400)


class PostTitleTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(backend.gapp, "OUTPUT_DIR", _OUT)
        p.start()
        self.addCleanup(p.stop)

    def test_post_title_caps_at_100_chars(self):
        wd = tempfile.mkdtemp(prefix="spielbot-film-", dir=_OUT)
        with mock.patch.object(backend.gapp, "load_config", return_value={}), \
             mock.patch.object(backend.gapp, "_load_scenes_for_work_dir", return_value=[]), \
             mock.patch.object(backend.gapp, "style_settings", return_value={"title_style": ""}), \
             mock.patch.object(backend, "_work_dir_style_name", return_value=""), \
             mock.patch.object(backend, "_video_title_for", return_value="current"), \
             mock.patch.object(backend, "_llm_complete", return_value="x" * 250):
            result = backend.yt_post_title(backend.DescribeBody(work_dir=wd, title="current"))
        self.assertLessEqual(len(result["title"]), 100)

    def test_post_title_missing_film_is_404(self):
        with self.assertRaises(backend.HTTPException) as ctx:
            backend.yt_post_title(backend.DescribeBody(work_dir=str(_OUT / "no-such-film-dir"), title="t"))
        self.assertEqual(ctx.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
