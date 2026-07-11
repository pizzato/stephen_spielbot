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

            def fake_tts(text, out, reference_wav=None, host="localhost", tts_engine="openf5"):
                Path(out).write_bytes(b"wav")
                calls["tts"].append((text, str(reference_wav)))

            def fake_animate(still, wav, clip, host, prompt="", steps=8, size=768, video_length=81):
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
