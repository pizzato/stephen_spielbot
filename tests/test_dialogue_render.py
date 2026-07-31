"""Unit tests for the dialogue-scene render helper (workers mocked)."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pipeline import dialogue_render


def _scene(lines, sid=3):
    return SimpleNamespace(id=sid, lines=lines)


class DialogueRenderTests(unittest.TestCase):
    def test_each_line_uses_its_speaker_voice_and_concats(self):
        with tempfile.TemporaryDirectory() as td:
            wd = Path(td)
            calls = {"tts": [], "animate": [], "stills": []}

            def fake_tts(text, out, reference_wav=None, host="localhost", tts_engine="openf5", language="en", **kw):
                Path(out).write_bytes(b"wav")
                calls["tts"].append((text, str(reference_wav)))

            def fake_animate(still, wav, clip, host, prompt="", steps=8,
                             width=0, height=0, video_length=81):
                Path(clip).write_bytes(b"mp4")
                calls["animate"].append((str(still), video_length))

            def fake_still(scene, speaker, idx):
                p = wd / f"still_{idx}.png"
                p.write_bytes(b"png")
                calls["stills"].append(speaker)
                return p

            concated = {}

            def fake_concat(clips, out):
                Path(out).write_bytes(b"final")
                concated["clips"] = list(clips)
                return out

            scene = _scene([
                {"speaker": "Kinho", "text": "Hi"},
                {"speaker": "Attenbot", "text": "Hello"},
            ])
            final = dialogue_render.render_dialogue_scene(
                scene, wd,
                voice_ref_for=lambda sp: Path(f"/voices/{sp}.wav"),
                make_still=fake_still,
                echomimic_host="http://s1:8190", tts_host="http://s1:8189",
                _tts=fake_tts, _animate=fake_animate, _duration=lambda p: 4.0, _concat=fake_concat,
                canvas=(832, 480), _fit=lambda c, w, h: None,
            )
            self.assertEqual(final.name, "scene_03_final.mp4")
            self.assertTrue(final.exists())
            self.assertEqual([c[0] for c in calls["tts"]], ["Hi", "Hello"])
            self.assertIn("Kinho", calls["tts"][0][1])
            self.assertIn("Attenbot", calls["tts"][1][1])
            self.assertEqual(calls["stills"], ["Kinho", "Attenbot"])
            self.assertEqual(calls["animate"][0][1], 101)  # 4.0s*25 + 1
            self.assertEqual(len(concated["clips"]), 2)

    def test_single_line_is_copied_not_concatenated(self):
        with tempfile.TemporaryDirectory() as td:
            wd = Path(td)

            def fake_animate(still, wav, clip, host, **k):
                Path(clip).write_bytes(b"m")

            final = dialogue_render.render_dialogue_scene(
                _scene([{"speaker": "Kinho", "text": "Solo"}]), wd,
                voice_ref_for=lambda sp: None,
                make_still=lambda s, sp, i: (wd / "s.png"),
                echomimic_host="h",
                _tts=lambda t, o, **k: Path(o).write_bytes(b"w"),
                _animate=fake_animate, _duration=lambda p: 2.0,
                _concat=lambda c, o: self.fail("single line must not concat"),
            )
            self.assertTrue(final.exists())

    def test_silent_shot_routes_to_video_not_tts(self):
        with tempfile.TemporaryDirectory() as td:
            wd = Path(td)
            hits = {"tts": 0, "animate": 0, "silent": []}

            def fake_silent(scene, shot, still, out_clip):
                Path(out_clip).write_bytes(b"mp4")
                hits["silent"].append((shot.get("duration"), shot.get("video_prompt")))
                return Path(out_clip)

            scene = _scene([
                {"speaker": "Kinho", "text": "Look out!"},
                {"silent": True, "shot": "Kinho draws", "video_prompt": "hand snaps to holster", "duration": 3},
            ])
            dialogue_render.render_dialogue_scene(
                scene, wd,
                voice_ref_for=lambda sp: None,
                make_still=lambda scene, sp, idx: (wd / f"s{idx}.png"),
                echomimic_host="http://s1:8190", tts_host="http://s1:8189",
                _tts=lambda *a, **k: hits.__setitem__("tts", hits["tts"] + 1) or Path(a[1]).write_bytes(b"w"),
                _animate=lambda *a, **k: hits.__setitem__("animate", hits["animate"] + 1) or Path(a[2]).write_bytes(b"m"),
                _duration=lambda p: 2.0, _concat=lambda clips, out: Path(out).write_bytes(b"f") or out,
                silent_video=fake_silent,
            )
            self.assertEqual(hits["tts"], 1)       # only the speaking shot
            self.assertEqual(hits["animate"], 1)   # only the speaking shot
            self.assertEqual(hits["silent"], [(3, "hand snaps to holster")])

    def test_establishing_clip_is_prepended(self):
        with tempfile.TemporaryDirectory() as td:
            wd = Path(td)
            concated = {}

            def fake_concat(clips, out):
                concated["names"] = [Path(c).name for c in clips]
                Path(out).write_bytes(b"f")
                return out

            def est(scene):
                p = wd / f"scene_{scene.id:02d}_establish.mp4"
                p.write_bytes(b"est")
                return p

            dialogue_render.render_dialogue_scene(
                _scene([{"speaker": "A", "text": "hi"}]), wd,
                voice_ref_for=lambda s: None,
                make_still=lambda scene, sp, idx: (wd / "s.png"),
                echomimic_host="http://s1:8190", tts_host="http://s1:8189",
                _tts=lambda *a, **k: Path(a[1]).write_bytes(b"w"),
                _animate=lambda *a, **k: Path(a[2]).write_bytes(b"m"),
                _duration=lambda p: 2.0, _concat=fake_concat, _fit=lambda c, w, h: None,
                canvas=(512, 256), establishing=est,
            )
            # establishing shot first, then the talking clip
            self.assertEqual(concated["names"][0], "scene_03_establish.mp4")
            self.assertEqual(len(concated["names"]), 2)

    def test_silent_shot_without_renderer_raises(self):
        with tempfile.TemporaryDirectory() as td:
            wd = Path(td)
            scene = _scene([{"silent": True, "duration": 3}])
            with self.assertRaises(RuntimeError):
                dialogue_render.render_dialogue_scene(
                    scene, wd, voice_ref_for=lambda s: None,
                    make_still=lambda scene, sp, idx: (wd / "s.png"),
                    echomimic_host="http://s1:8190", tts_host="http://s1:8189",
                    _concat=lambda clips, out: out)

    def test_echo_dims_keep_aspect_and_grid(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("PIL not available")
        with tempfile.TemporaryDirectory() as td:
            land = Path(td) / "land.png"
            Image.new("RGB", (512, 256)).save(land)
            self.assertEqual(dialogue_render.echo_dims_for_still(land), (512, 256))
            sq = Path(td) / "sq.png"
            Image.new("RGB", (1024, 1024)).save(sq)
            self.assertEqual(dialogue_render.echo_dims_for_still(sq), (768, 768))
            bad = Path(td) / "bad.png"
            bad.write_bytes(b"not an image")
            self.assertEqual(dialogue_render.echo_dims_for_still(bad), (768, 768))

    def test_no_usable_lines_raises(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):
                dialogue_render.render_dialogue_scene(
                    _scene([{"speaker": "X", "text": "   "}]), Path(td),
                    voice_ref_for=lambda sp: None, make_still=lambda *a: Path("x"),
                    echomimic_host="h", _tts=lambda *a, **k: None, _animate=lambda *a, **k: None,
                )


if __name__ == "__main__":
    unittest.main()
