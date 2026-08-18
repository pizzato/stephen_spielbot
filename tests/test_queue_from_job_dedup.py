"""queue_from_job must not create a second queue row for a script that is
already queued.

A "manual" slot that has had a script attached holds a work_dir + video_job_id.
Re-approving that same generated script (e.g. from the Create/Script tab, which
does not always carry the slot's queue_item_id) used to fall through to
add_to_queue and append a duplicate "script" row pointing at the *same*
work_dir. Two rows on one work_dir then moved in lockstep and a Library delete
(which removes every row matching the work_dir) wiped both at once.
"""

import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("HOME", tempfile.mkdtemp(prefix="spielbot-test-home-"))

import webapp.backend.main as backend  # noqa: E402


class QueueFromJobDedupTests(unittest.TestCase):
    def setUp(self):
        # An in-memory queue the patched yt helpers read/write.
        self.queue = []
        mock.patch.object(backend.yt, "load_queue", side_effect=lambda: self.queue).start()
        mock.patch.object(backend.gapp, "load_config", return_value={}).start()
        self.add = mock.patch.object(backend.yt, "add_to_queue").start()
        self.update = mock.patch.object(backend.yt, "update_queue_item").start()
        self.addCleanup(mock.patch.stopall)

    def body(self, **kw):
        base = dict(job_id="job_abc", work_dir="/videos/topic-150008",
                    video_title="Topic", n_scenes=10)
        base.update(kw)
        return backend.FromJobBody(**base)

    def test_reapprove_updates_existing_slot_instead_of_appending(self):
        # A pending slot already points at this job (queue_item_id NOT passed).
        self.queue.append({"id": "slot1", "status": "pending",
                           "video_job_id": "job_abc", "work_dir": "/videos/topic-150008"})
        out = backend.queue_from_job(self.body(queue_item_id=""))
        self.assertTrue(out["updated_in_place"])
        self.assertEqual(out["queue_item_id"], "slot1")
        self.update.assert_called_once()
        self.assertEqual(self.update.call_args.args[0], "slot1")
        self.add.assert_not_called()

    def test_matches_on_work_dir_when_job_id_differs(self):
        self.queue.append({"id": "slot1", "status": "pending",
                           "video_job_id": "", "work_dir": "/videos/topic-150008"})
        out = backend.queue_from_job(self.body(job_id="", queue_item_id=""))
        self.assertEqual(out["queue_item_id"], "slot1")
        self.add.assert_not_called()

    def test_no_match_still_appends_a_new_row(self):
        # An unrelated pending slot must not be hijacked.
        self.queue.append({"id": "other", "status": "pending",
                           "video_job_id": "job_other", "work_dir": "/videos/other-999"})
        self.add.return_value = {"id": "new1"}
        out = backend.queue_from_job(self.body(queue_item_id=""))
        self.add.assert_called_once()
        self.assertEqual(out["queue_item_id"], "new1")

    def test_empty_body_does_not_match_a_scriptless_slot(self):
        # A manual slot with no script yet (no work_dir/job) must never be
        # matched by a stray empty-keyed approve.
        self.queue.append({"id": "bare", "status": "pending"})
        self.add.return_value = {"id": "new1"}
        out = backend.queue_from_job(self.body(job_id="", work_dir=""))
        self.add.assert_called_once()
        self.assertEqual(out["queue_item_id"], "new1")

    def test_creating_slot_updates_in_place_without_unapproving(self):
        # Automation claims the row ("creating", approved) BEFORE the script
        # generation, whose queue_from_job (queue_item_id via the create brief,
        # approved=False) used to fall through and append a duplicate row on
        # the same work_dir — which auto-approve later re-rendered.
        self.queue.append({"id": "slot1", "status": "creating", "approved": True,
                           "video_job_id": "", "work_dir": ""})
        out = backend.queue_from_job(self.body(queue_item_id="slot1", approved=False))
        self.assertTrue(out["updated_in_place"])
        self.assertEqual(out["queue_item_id"], "slot1")
        self.add.assert_not_called()
        self.update.assert_called_once()
        # Starting was the approval — linking the script must not revoke it.
        self.assertNotIn("approved", self.update.call_args.kwargs)

    def test_creating_slot_matched_by_work_dir_without_id(self):
        self.queue.append({"id": "slot1", "status": "creating", "approved": True,
                           "video_job_id": "job_abc", "work_dir": "/videos/topic-150008"})
        out = backend.queue_from_job(self.body(queue_item_id=""))
        self.assertTrue(out["updated_in_place"])
        self.assertEqual(out["queue_item_id"], "slot1")
        self.add.assert_not_called()

    def test_creating_slot_keeps_explicit_approval(self):
        self.queue.append({"id": "slot1", "status": "creating", "approved": False,
                           "video_job_id": "", "work_dir": ""})
        backend.queue_from_job(self.body(queue_item_id="slot1", approved=True))
        self.assertTrue(self.update.call_args.kwargs.get("approved"))


if __name__ == "__main__":
    unittest.main()
