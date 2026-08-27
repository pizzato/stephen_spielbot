"""Character turnaround sheets: engine choice per generation, and hand-picked
panels for an orbit sheet.

Drives the real FastAPI app with the generators mocked — the point under test is
the bookkeeping (which engine ran, what state the UI sees, which frames were
cut), not the models. The orbit's frame extraction runs for real against a tiny
ffmpeg-generated clip, because that is where the timestamps have to line up."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml
from fastapi.testclient import TestClient
from PIL import Image

import app as gapp
from webapp.backend import main as backend
from test_styles import _style


def _has_ffmpeg() -> bool:
    from pipeline.assembler import _resolve_media_tool
    try:
        subprocess.run([_resolve_media_tool("ffmpeg"), "-version"],
                       capture_output=True, check=True)
        return True
    except Exception:
        return False


class CharacterSheetTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="spielbot-sheet-")
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.config_file = tmp / "config" / "config.yaml"
        self.config_file.parent.mkdir(parents=True)
        self.output_dir = tmp / "videos"
        self.output_dir.mkdir()
        for attr, value in [("CONFIG_FILE", self.config_file),
                            ("VOICES_DIR", self.config_file.parent / "voices"),
                            ("OUTPUT_DIR", self.output_dir)]:
            p = mock.patch.object(gapp, attr, value)
            p.start()
            self.addCleanup(p.stop)
        db = mock.patch.dict(os.environ, {"VIDEO_GEN_DB": str(tmp / "orchestrator.sqlite3")})
        db.start()
        self.addCleanup(db.stop)
        self.config_file.write_text(yaml.safe_dump({
            "styles": [_style("Hero")],
            "default_style": "Hero",
            "characters": [{"id": "char_test", "name": "Ada", "description": "an engineer",
                            "ref_image": "char_test.png", "style": ""}],
            "characters_migrated_v2": True, "characters_scoped_v3": True,
        }))
        # The sheet is built FROM the reference image, so the character needs one.
        d = gapp._characters_dir()
        d.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (64, 64), "white").save(d / "char_test.png")
        workers = mock.patch.object(gapp, "_preview_worker_urls", lambda: ["http://w:8188"])
        workers.start()
        self.addCleanup(workers.stop)
        self.client = TestClient(backend.api)

    # ── helpers ─────────────────────────────────────────────────────────────
    def _fake_image(self, engine, prompt, out, **kw):
        Image.new("RGB", (2048, 1024), "grey").save(out)
        self.image_prompt = prompt
        self.image_refs = kw.get("reference_images")
        return out

    def _fake_orbit(self, engine, prompt, refs, out, **kw):
        """A real 3-second clip whose frames differ, so a re-pick is provable."""
        from pipeline.assembler import _resolve_media_tool
        subprocess.run([_resolve_media_tool("ffmpeg"), "-y", "-v", "error", "-f", "lavfi",
                        "-i", "testsrc=size=64x96:rate=24:duration=3", str(out)], check=True)
        self.orbit_prompt = prompt
        return out

    def _build(self, engine):
        return self.client.post("/api/characters/sheet",
                                json={"char_id": "char_test", "engine": engine})

    # ── tests ───────────────────────────────────────────────────────────────
    def test_image_sheet_uses_the_image_engine_and_the_reference(self):
        with mock.patch.object(gapp, "generate_with_engine", self._fake_image):
            self.assertEqual(self._build("image").status_code, 200)
            self._wait()
        state = self.client.get("/api/characters/sheet", params={"char_id": "char_test"}).json()["sheet"]
        self.assertEqual(state["status"], "ready")
        self.assertEqual(state["engine"], "image")
        # No clip: an image sheet has no frames to re-pick.
        self.assertFalse(state["has_clip"])
        self.assertTrue(state["sheet_url"])
        self.assertEqual([Path(p).name for p in self.image_refs], ["char_test.png"])
        self.assertIn("Ada", self.image_prompt)

    def test_unknown_engine_is_rejected(self):
        r = self.client.post("/api/characters/sheet",
                             json={"char_id": "char_test", "engine": "midjourney"})
        self.assertEqual(r.status_code, 400)

    def test_sheet_needs_a_reference_image(self):
        cfg = gapp.load_config()
        cfg["characters"][0]["ref_image"] = ""
        gapp.save_config(cfg)
        r = self._build("image")
        self.assertEqual(r.status_code, 400)
        self.assertIn("reference image", r.json()["detail"])

    @unittest.skipUnless(_has_ffmpeg(), "ffmpeg not available")
    def test_orbit_sheet_keeps_its_clip_and_panels_can_be_repicked(self):
        with mock.patch.object(gapp, "generate_video_h3_ref", self._fake_orbit):
            self.assertEqual(self._build("orbit").status_code, 200)
            self._wait()
        state = self.client.get("/api/characters/sheet", params={"char_id": "char_test"}).json()["sheet"]
        self.assertEqual(state["status"], "ready")
        self.assertEqual(state["engine"], "orbit")
        self.assertTrue(state["has_clip"], "the orbit clip is kept so frames can be re-picked")
        self.assertEqual(len(state["panels"]), gapp.SHEET_PANELS)
        self.assertAlmostEqual(state["duration"], 3.0, places=1)
        # The staging quotes the clip's REAL length, not the requested seconds.
        self.assertIn(f"{gapp.h3_frame_count(gapp.SHEET_ORBIT_SECONDS) / gapp.H3_FPS:.1f}",
                      self.orbit_prompt)

        _, sheet, _, _ = gapp._sheet_paths("char_test")
        with Image.open(sheet) as im:
            four_wide = im.width
        r = self.client.post("/api/characters/sheet/panels",
                             json={"char_id": "char_test", "times": [0.1, 1.0]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["sheet"]["panels"], [0.1, 1.0])
        with Image.open(sheet) as im:
            # Two panels instead of four: the re-stitch actually re-cut the strip.
            self.assertLess(im.width, four_wide)

    @unittest.skipUnless(_has_ffmpeg(), "ffmpeg not available")
    def test_repick_clamps_times_past_the_end_of_the_clip(self):
        with mock.patch.object(gapp, "generate_video_h3_ref", self._fake_orbit):
            self._build("orbit")
            self._wait()
        r = self.client.post("/api/characters/sheet/panels",
                             json={"char_id": "char_test", "times": [0.0, 99.0]})
        self.assertEqual(r.status_code, 200)
        self.assertLess(r.json()["sheet"]["panels"][1], 3.0)

    def test_repick_without_a_clip_is_a_400(self):
        r = self.client.post("/api/characters/sheet/panels",
                             json={"char_id": "char_test", "times": [0.5]})
        self.assertEqual(r.status_code, 400)

    def test_clear_removes_the_sheet(self):
        with mock.patch.object(gapp, "generate_with_engine", self._fake_image):
            self._build("image")
            self._wait()
        r = self.client.post("/api/characters/sheet/clear", json={"char_id": "char_test"})
        self.assertEqual(r.json()["sheet"]["status"], "none")
        self.assertFalse(gapp._character_sheet_dir("char_test").exists())

    def test_a_failed_render_reports_why_instead_of_spinning(self):
        def boom(*a, **kw):
            raise RuntimeError("worker exploded")
        with mock.patch.object(gapp, "generate_with_engine", boom):
            self._build("image")
            self._wait()
        state = gapp.character_sheet_state("char_test")
        self.assertEqual(state["status"], "error")
        self.assertIn("worker exploded", state["error"])

    def test_config_payload_carries_each_character_s_sheet_state(self):
        cfg = self.client.get("/api/config").json()["config"]
        self.assertEqual(cfg["characters"][0]["sheet"]["status"], "none")

    def _wait(self, timeout: float = 20.0):
        """Sheets render on a daemon thread; wait for it to leave 'rendering'."""
        import time
        deadline = time.time() + timeout
        while time.time() < deadline:
            with backend._sheet_lock:
                busy = "char_test" in backend._sheet_jobs
            if not busy:
                return
            time.sleep(0.05)
        self.fail("sheet render did not finish")


if __name__ == "__main__":
    unittest.main()
