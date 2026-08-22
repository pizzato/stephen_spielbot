"""Titles & credits (pipeline/title_cards.py): an ordered list of cards, each
drawn as a still (solid colour or the user's image + text in the display
font), held with a fade, and stacked before ("start") or after ("end") the
finished film — the film itself untouched. The applied record keyed on the cut's DURATION is what
makes re-apply replace (never stack), remove trim exactly, and a rebuilt
final register as clean. Soft caption tracks shift by the opening card."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ["HOME"] = tempfile.mkdtemp(prefix="spielbot-test-home-")

import webapp.backend.main as backend  # noqa: E402
import pipeline.assembler as assembler  # noqa: E402
import pipeline.captions as captions  # noqa: E402
import pipeline.title_cards as tc  # noqa: E402
from pipeline import final_video_history  # noqa: E402

_OUT = Path(tempfile.mkdtemp(prefix="spielbot-test-out-"))
_HAVE_FFMPEG = shutil.which("ffmpeg") is not None


def _synthetic_film(path: Path, seconds: float = 3.0, size: str = "320x180") -> Path:
    subprocess.run([
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", f"testsrc=size={size}:rate=25",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
        "-t", str(seconds), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        str(path),
    ], check=True)
    return path


class NormTests(unittest.TestCase):
    def test_defaults_fill_and_bad_values_coerce(self):
        cfg = tc.norm_title_cards({"cards": [{"seconds": 99, "color": "red", "background": "video",
                                              "placement": "middle", "id": "A/B c"}],
                                   "fade": "lots", "scale": 0, "text_color": "#abcdef"})
        card = cfg["cards"][0]
        self.assertEqual(card["seconds"], tc.SECONDS_MAX)
        self.assertEqual(card["color"], "#000000")
        self.assertEqual(card["background"], "color")
        self.assertEqual(card["placement"], "start")
        self.assertEqual(card["id"], "abc")  # path-safe token
        self.assertEqual(cfg["fade"], tc.FADE_DEFAULT)
        self.assertEqual(cfg["scale"], 0.4)
        self.assertEqual(cfg["text_color"], "#ABCDEF")

    def test_ids_are_assigned_and_made_unique(self):
        cfg = tc.norm_title_cards({"cards": [{}, {"id": "x"}, {"id": "x"}, {"id": "card1"}]})
        self.assertEqual([c["id"] for c in cfg["cards"]], ["card1", "x", "x_2", "card1_2"])

    def test_card_count_is_capped(self):
        cfg = tc.norm_title_cards({"cards": [{}] * 40})
        self.assertEqual(len(cfg["cards"]), tc.MAX_CARDS)

    def test_garbage_is_the_default(self):
        self.assertEqual(tc.norm_title_cards("nope"), tc.norm_title_cards(None))
        self.assertEqual(tc.norm_title_cards(None)["cards"], [])


class RenderCardTests(unittest.TestCase):
    def test_solid_colour_card_draws_the_lines(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "card.png"
            meta = tc.render_card(out, 320, 180, "THE FILM\n\nby someone",
                                  color="#102030", font="Anton")
            self.assertEqual(meta["lines"], ["THE FILM", "", "by someone"])
            self.assertGreater(meta["font_size"], 12)
            with Image.open(out) as im:
                self.assertEqual(im.size, (320, 180))
                # Background colour at a corner, white text somewhere in the frame.
                self.assertEqual(im.getpixel((2, 2)), (16, 32, 48))
                self.assertIn((255, 255, 255), {im.getpixel((x, y))
                                                for x in range(0, 320, 2) for y in range(0, 180, 2)})

    def test_image_background_is_cover_cropped_to_the_frame(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            still = Path(tmp) / "still.png"
            Image.new("RGB", (100, 400), (200, 0, 0)).save(still)
            out = Path(tmp) / "card.png"
            tc.render_card(out, 320, 180, "Title", background="image", image_path=still)
            with Image.open(out) as im:
                self.assertEqual(im.size, (320, 180))
                self.assertEqual(im.getpixel((2, 2)), (200, 0, 0))

    def test_empty_text_is_a_plain_card(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "card.png"
            self.assertEqual(tc.render_card(out, 64, 64, "  \n ")["lines"], [])
            self.assertTrue(out.exists())


@unittest.skipUnless(_HAVE_FFMPEG, "ffmpeg not installed")
class ApplyStripTests(unittest.TestCase):
    def setUp(self):
        self.wd = Path(tempfile.mkdtemp(prefix="spielbot-titles-"))
        self.final = _synthetic_film(self.wd / "film.mp4", seconds=3.0)
        self.cfg = {
            "cards": [
                {"id": "t", "placement": "start", "text": "", "seconds": 1},
                {"id": "d", "placement": "start", "text": "A dedication", "seconds": 1},
                {"id": "e", "placement": "end", "text": "The end", "seconds": 1.5, "color": "#FFFFFF"},
            ],
            "font": "Anton", "fade": 0.3,
        }

    def test_apply_prepends_and_appends_then_strip_restores(self):
        rec = tc.apply_title_cards(self.final, self.wd, self.cfg, title="A TITLE")
        self.assertEqual((rec["head"], rec["tail"]), (2.0, 1.5))
        self.assertAlmostEqual(assembler._get_duration(self.final), 6.5, delta=0.25)
        self.assertTrue(tc.applied_title_cards(self.wd, self.final))
        self.assertEqual(tc.head_seconds(self.wd, self.final), 2.0)
        # Same size and frame rate as the film went in with.
        self.assertEqual(assembler._get_video_dimensions(self.final), (320, 180))

        self.assertTrue(tc.strip_title_cards(self.final, self.wd))
        self.assertAlmostEqual(assembler._get_duration(self.final), 3.0, delta=0.25)
        self.assertIsNone(tc.applied_title_cards(self.wd, self.final))
        self.assertEqual(tc.head_seconds(self.wd, self.final), 0.0)
        self.assertFalse(tc.strip_title_cards(self.final, self.wd))

    def test_reapply_replaces_instead_of_stacking(self):
        tc.apply_title_cards(self.final, self.wd, self.cfg, title="ONE")
        cfg2 = dict(self.cfg, cards=self.cfg["cards"][:2])  # drop the end card
        rec = tc.apply_title_cards(self.final, self.wd, cfg2, title="TWO")
        self.assertEqual((rec["head"], rec["tail"]), (2.0, 0.0))
        self.assertAlmostEqual(assembler._get_duration(self.final), 5.0, delta=0.25)

    def test_start_cards_play_in_list_order(self):
        from PIL import Image
        cfg = {"cards": [
            {"id": "red", "placement": "start", "text": "", "seconds": 1, "color": "#FF0000"},
            {"id": "blue", "placement": "start", "text": "", "seconds": 1, "color": "#0000FF"},
        ], "fade": 0}
        tc.apply_title_cards(self.final, self.wd, cfg)
        for t, want in ((0.5, (255, 0, 0)), (1.5, (0, 0, 255))):
            frame = self.wd / f"f{t}.png"
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(t), "-i", str(self.final),
                            "-frames:v", "1", str(frame)], check=True)
            with Image.open(frame) as im:
                px = im.convert("RGB").getpixel((5, 5))
            self.assertTrue(all(abs(a - b) < 40 for a, b in zip(px, want)), (t, px))

    def test_rebuilt_final_is_not_mistaken_for_a_titled_one(self):
        tc.apply_title_cards(self.final, self.wd, self.cfg, title="ONE")
        # A rebuild from combined.mp4 replaces the file with the clean cut.
        _synthetic_film(self.final, seconds=3.0)
        self.assertIsNone(tc.applied_title_cards(self.wd, self.final))
        self.assertFalse(tc.strip_title_cards(self.final, self.wd))

    def test_needs_a_card(self):
        with self.assertRaises(ValueError):
            tc.apply_title_cards(self.final, self.wd, {"cards": []})


class CaptionOffsetTests(unittest.TestCase):
    def test_cues_shift_by_the_opening_card(self):
        wd = Path(tempfile.mkdtemp(prefix="spielbot-cap-"))
        (wd / "script.json").write_text(json.dumps(
            [{"id": 1, "narration": "First."}, {"id": 2, "narration": "Second."}]))
        durations = {"scene_01_narration.wav": 2.0, "scene_02_narration.wav": 3.0}
        with mock.patch.object(captions, "_duration",
                               side_effect=lambda p: durations.get(Path(p).name, 0.0)):
            content = captions.build_srt(wd, offset=4.0).read_text()
        self.assertIn("00:00:04,000 --> 00:00:06,000", content)
        self.assertIn("00:00:06,000 --> 00:00:09,000", content)


class EndpointTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(backend.gapp, "OUTPUT_DIR", _OUT)
        p.start()
        self.addCleanup(p.stop)
        q = mock.patch.object(backend, "_video_title_for", return_value="The Silent City")
        q.start()
        self.addCleanup(q.stop)
        self.wd = Path(tempfile.mkdtemp(prefix="spielbot-film-", dir=_OUT))
        (self.wd / "job_config.json").write_text("{}")
        self.final = backend.gapp._final_path_for_work_dir(self.wd)
        self.final.write_bytes(b"video")
        self.addCleanup(lambda: self.final.unlink(missing_ok=True))

    def test_form_fills_the_style_font_and_lists_stills(self):
        with mock.patch.object(backend, "_title_cards_default_font", return_value="Anton"):
            form = backend._title_cards_form(self.wd, {})
        self.assertEqual(form["font"], "Anton")
        self.assertEqual(form["cards"], [])
        saved = {"title_cards": {"cards": [{"id": "a", "text": "Custom"}, {"id": "b"}]}}
        tc.card_image_path(self.wd, "b").write_bytes(b"png")
        imgs = backend._title_card_images(self.wd, saved)
        self.assertEqual(list(imgs), ["b"])
        self.assertIn("title_card_b.png", imgs["b"])

    def test_apply_rejects_no_cards_and_missing_still(self):
        body = backend.TitleCardsBody(work_dir=str(self.wd), title_cards={})
        with self.assertRaises(backend.HTTPException) as cm:
            backend.remix_title_cards(body)
        self.assertEqual(cm.exception.status_code, 400)
        body = backend.TitleCardsBody(work_dir=str(self.wd), title_cards={
            "cards": [{"id": "a"}, {"id": "b", "background": "image"}]})
        with self.assertRaises(backend.HTTPException) as cm:
            backend.remix_title_cards(body)
        self.assertIn("Upload a still for card 2", cm.exception.detail)

    def test_still_upload_is_keyed_on_a_safe_card_id(self):
        import base64
        from PIL import Image
        import io
        buf = io.BytesIO()
        Image.new("RGB", (8, 8), (1, 2, 3)).save(buf, "PNG")
        data = base64.b64encode(buf.getvalue()).decode()
        r = backend.remix_title_card_image(backend.TitleCardImageBody(
            work_dir=str(self.wd), card_id="../Evil", data=data))
        self.assertEqual(r["card_id"], "evil")
        self.assertTrue((self.wd / "title_card_evil.png").exists())
        with self.assertRaises(backend.HTTPException):
            backend.remix_title_card_image(backend.TitleCardImageBody(
                work_dir=str(self.wd), card_id="///", data=data))

    def test_apply_persists_settings_and_runs_the_task(self):
        calls = []

        def fake_apply(final_path, wd, cfg, *, title="", default_font=""):
            calls.append((Path(final_path), cfg["cards"][0]["text"], title))
            return {"head": 3.0, "tail": 0.0, "duration": 10.0}

        body = backend.TitleCardsBody(work_dir=str(self.wd), title_cards={
            "cards": [{"id": "a", "text": "Hello", "seconds": 3}]})
        with mock.patch.object(tc, "apply_title_cards", side_effect=fake_apply), \
             mock.patch.object(backend, "_title_cards_default_font", return_value="Anton"), \
             mock.patch.object(backend.threading, "Thread") as thread:
            r = backend.remix_title_cards(body)
            args = thread.call_args.kwargs["args"]
            backend._run_title_cards(*args)
        self.assertTrue(r["ok"])
        jc = json.loads((self.wd / "job_config.json").read_text())
        self.assertEqual(jc["title_cards"]["cards"][0]["text"], "Hello")
        self.assertEqual(calls, [(self.final, "Hello", "The Silent City")])
        task = backend._film_tasks[r["task_id"]]
        self.assertEqual(task["status"], "done")
        self.assertTrue(task["title_cards_applied"])
        hist = final_video_history.history(self.wd)
        self.assertEqual([v["label"] for v in hist["versions"]],
                         ["Original", "Titles & credits"])
        self.assertEqual(hist["versions"][-1]["kind"], "titles")

    def test_remove_switches_off_and_trims_only_a_titled_cut(self):
        jc = {"title_cards": {"cards": [{"id": "a", "text": "x"}]}}
        (self.wd / "job_config.json").write_text(json.dumps(jc))
        body = backend.TitleCardsRemoveBody(work_dir=str(self.wd))
        with mock.patch.object(tc, "applied_title_cards", return_value=None), \
             mock.patch.object(tc, "strip_title_cards") as strip:
            r = backend.remix_title_cards_remove(body)
        self.assertFalse(r["trimmed"])
        strip.assert_not_called()
        jc = json.loads((self.wd / "job_config.json").read_text())
        self.assertEqual(jc["title_cards"]["cards"], [])
        with mock.patch.object(tc, "applied_title_cards",
                               return_value={"head": 3.0, "tail": 0, "duration": 10}), \
             mock.patch.object(tc, "strip_title_cards", return_value=True) as strip:
            r = backend.remix_title_cards_remove(body)
        self.assertTrue(r["trimmed"])
        strip.assert_called_once()
        self.assertEqual(final_video_history.history(self.wd)["versions"][-1]["label"],
                         "Titles removed")

    def test_rebuild_reapplies_only_when_switched_on(self):
        with mock.patch.object(tc, "apply_title_cards") as apply:
            backend._maybe_apply_title_cards(self.wd, self.final)
            apply.assert_not_called()
            (self.wd / "job_config.json").write_text(json.dumps(
                {"title_cards": {"cards": [{"placement": "end", "text": "fin"}]}}))
            with mock.patch.object(backend, "_title_cards_default_font", return_value=""):
                backend._maybe_apply_title_cards(self.wd, self.final)
            apply.assert_called_once()
            self.assertEqual(apply.call_args.kwargs["title"], "The Silent City")

    def test_head_seconds_is_best_effort(self):
        with mock.patch.object(tc, "head_seconds", side_effect=RuntimeError("probe")):
            self.assertEqual(backend._title_cards_head_seconds(self.wd), 0.0)


if __name__ == "__main__":
    unittest.main()
