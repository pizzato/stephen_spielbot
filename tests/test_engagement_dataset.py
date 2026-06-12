"""build_dataset filtering + day-1/2/3 extraction (issue #50). No network."""
import datetime
import os
import tempfile
import unittest
from unittest import mock

os.environ["HOME"] = tempfile.mkdtemp(prefix="spielbot-test-home-")

import pipeline.engagement as eng


class _FrozenDateTime(datetime.datetime):
    # build_dataset's recency cutoff comes from datetime.now(timezone.utc); pin
    # it so fixture rows keep a stable distance from the cutoff regardless of
    # the wall clock. Inherited classmethods (fromisoformat, ...) still work.
    @classmethod
    def now(cls, tz=None):
        return cls(2024, 3, 20, 12, 0, tzinfo=tz)


class BuildDatasetTests(unittest.TestCase):
    def _build(self, rows):
        with mock.patch.object(eng.yt, "fetch_training_rows", return_value=rows), \
                mock.patch("datetime.datetime", _FrozenDateTime):
            return eng.build_dataset("/tmp/secrets.json")

    def test_filters_and_sums_first_three_days(self):
        rows = [
            {  # kept: public, old enough; views = 100 + 50 + 25; long-form
                "video_id": "ok1", "title": "Romans", "description": "fall of Rome",
                "published_at": "2024-03-15T18:42:00Z", "privacy": "public",
                "duration_seconds": 600,
                "day_views": {"2024-03-15": 100, "2024-03-16": 50, "2024-03-17": 25, "2024-03-18": 9},
            },
            {  # dropped: private
                "video_id": "priv", "title": "secret", "description": "",
                "published_at": "2024-03-15T10:00:00Z", "privacy": "private",
                "day_views": {"2024-03-15": 999},
            },
            {  # dropped: too recent (after the 2024-03-17 cutoff = frozen today - 3)
                "video_id": "new", "title": "fresh", "description": "",
                "published_at": "2024-03-18T10:00:00Z", "privacy": "public",
                "day_views": {},
            },
        ]
        dataset, dropped = self._build(rows)
        self.assertEqual(dropped, 2)
        self.assertEqual(len(dataset), 1)
        row = dataset[0]
        self.assertEqual(row["video_id"], "ok1")
        self.assertEqual(row["views"], 175)          # only the first 3 calendar days
        self.assertEqual(row["weekday"], 4)          # 2024-03-15 is a Friday (Mon=0)
        self.assertEqual(row["hour"], 18)            # UTC hour from the timestamp
        self.assertEqual(row["is_short"], 0)         # 600s > 180s default → long-form

    def test_missing_middle_day_counts_as_zero(self):
        # The Analytics API omits zero-view days; a gap means 0, not "drop".
        rows = [{
            "video_id": "gap", "title": "t", "description": "",
            "published_at": "2024-03-15T23:30:00Z", "privacy": "public",
            "day_views": {"2024-03-15": 10, "2024-03-17": 5},   # 16th absent
        }]
        dataset, dropped = self._build(rows)
        self.assertEqual(dropped, 0)
        self.assertEqual(len(dataset), 1)
        self.assertEqual(dataset[0]["views"], 15)    # 10 + 0 + 5
        self.assertEqual(dataset[0]["hour"], 23)     # late-day publish boundary

    def test_classifies_short_by_duration(self):
        # is_short is derived from duration vs the 180s default threshold.
        rows = [
            {"video_id": "shorty", "title": "s", "description": "",
             "published_at": "2024-03-15T12:00:00Z", "privacy": "public",
             "duration_seconds": 45, "day_views": {"2024-03-15": 10}},
            {"video_id": "longy", "title": "l", "description": "",
             "published_at": "2024-03-15T12:00:00Z", "privacy": "public",
             "duration_seconds": 600, "day_views": {"2024-03-15": 10}},
            {"video_id": "nodur", "title": "n", "description": "",  # missing duration → long
             "published_at": "2024-03-15T12:00:00Z", "privacy": "public",
             "day_views": {"2024-03-15": 10}},
        ]
        dataset, _ = self._build(rows)
        by_id = {d["video_id"]: d for d in dataset}
        self.assertEqual(by_id["shorty"]["is_short"], 1)
        self.assertEqual(by_id["longy"]["is_short"], 0)
        self.assertEqual(by_id["nodur"]["is_short"], 0)

    def test_unparseable_publish_date_dropped(self):
        rows = [{
            "video_id": "bad", "title": "t", "description": "",
            "published_at": "", "privacy": "public", "day_views": {},
        }]
        dataset, dropped = self._build(rows)
        self.assertEqual(len(dataset), 0)
        self.assertEqual(dropped, 1)


if __name__ == "__main__":
    unittest.main()
