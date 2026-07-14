"""The progress bar % is reconciled with the task ETA so they never disagree."""
import unittest

from webapp.backend.main import _display_pct


class DisplayPctTests(unittest.TestCase):
    def test_derives_from_eta_not_band(self):
        # band says 49% but the ETA says ~1 min of a 40-min render remains → ~98%
        eta = {"total_seconds": 2400, "eta_seconds": 40}
        self.assertEqual(_display_pct(49, eta, "running", False), 98)

    def test_half_done(self):
        eta = {"total_seconds": 1000, "eta_seconds": 500}
        self.assertEqual(_display_pct(10, eta, "running", False), 50)

    def test_never_0_or_100_while_running(self):
        self.assertEqual(_display_pct(0, {"total_seconds": 100, "eta_seconds": 100}, "running", False), 1)
        self.assertEqual(_display_pct(0, {"total_seconds": 100, "eta_seconds": 0}, "running", False), 99)

    def test_done_is_100(self):
        self.assertEqual(_display_pct(80, {"total_seconds": 100, "eta_seconds": 10}, "running", True), 100)

    def test_falls_back_to_band_without_eta(self):
        # before durable tasks exist (script gen / pre-build) there is no ETA
        self.assertEqual(_display_pct(35, None, "running", False), 35)

    def test_falls_back_on_error(self):
        eta = {"total_seconds": 1000, "eta_seconds": 500}
        self.assertEqual(_display_pct(42, eta, "error", False), 42)


if __name__ == "__main__":
    unittest.main()
