import os
import tempfile
import unittest
from unittest import mock

os.environ["HOME"] = tempfile.mkdtemp(prefix="spielbot-test-home-")

import webapp.backend.main as backend


class YouTubeCompletionReplyTests(unittest.TestCase):
    def test_completion_reply_posts_to_original_comment_and_marks_state(self):
        queue_item = {
            "id": "queue-1",
            "comment_id": "comment-1",
            "final_title": "Jimi Hendrix: The Life and Legend",
        }
        comment = {"comment_id": "comment-1", "text": "Can I have a video on Jimi Hendrix?"}

        with mock.patch.object(backend, "_client_secrets_path", return_value="/tmp/secrets.json"), \
             mock.patch.object(backend.yt, "load_queue", return_value=[queue_item]), \
             mock.patch.object(backend.yt, "reply_to_comment", return_value={"success": True, "error": ""}) as reply, \
             mock.patch.object(backend.yt, "update_queue_item", return_value=True) as update_queue, \
             mock.patch.object(backend.yt, "load_comments_cache", return_value=[comment]), \
             mock.patch.object(backend.yt, "save_comments_cache") as save_comments:
            result = backend._post_completion_reply(
                "queue-1",
                "Jimi Hendrix: The Life and Legend",
                "https://www.youtube.com/watch?v=abc123",
            )

        self.assertEqual(result, {"attempted": True, "success": True})
        reply.assert_called_once()
        self.assertEqual(reply.call_args.args[1], "comment-1")
        self.assertIn("https://www.youtube.com/watch?v=abc123", reply.call_args.args[2])
        update_queue.assert_called_once()
        self.assertTrue(update_queue.call_args.kwargs["completion_replied"])
        self.assertEqual(update_queue.call_args.kwargs["completion_reply_error"], "")
        self.assertTrue(comment["completion_replied"])
        save_comments.assert_called_once()

    def test_completion_reply_skips_already_replied_queue_item(self):
        queue_item = {
            "id": "queue-1",
            "comment_id": "comment-1",
            "completion_replied": True,
        }

        with mock.patch.object(backend.yt, "load_queue", return_value=[queue_item]), \
             mock.patch.object(backend.yt, "reply_to_comment") as reply:
            result = backend._post_completion_reply(
                "queue-1",
                "Jimi Hendrix",
                "https://www.youtube.com/watch?v=abc123",
            )

        self.assertEqual(result, {"attempted": False, "already_replied": True})
        reply.assert_not_called()


if __name__ == "__main__":
    unittest.main()
