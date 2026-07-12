"""Regression tests for the bugs that made dialogue/silent scenes render as narration.

1. start_generation's script.json rewrite dropped scene metadata → authored
   dialogue silently reverted to narration at render start.
2. The LLM system prompt pins the scene schema, so the dialogue fields must be
   substituted INTO it (a user-message note alone gets ignored).
3. Silent scenes went through TTS with leftover narration text → still narrated.
4. The self-heal pass force-filled narration into silent scenes and its
   script.json rewrite dropped metadata.
5. Captions rendered leftover narration text for silent/dialogue scenes.
"""
import json
import tempfile
import unittest
from pathlib import Path

from pipeline import prompts as _prompts
from pipeline.llm import Scene


class SnapshotRowTests(unittest.TestCase):
    def test_narration_scene_row_has_no_metadata(self):
        import webapp.backend.main as m
        s = Scene(id=1, title="t", image_prompt="i", video_prompt="v", narration="n")
        row = m._scene_snapshot_row(s)
        self.assertNotIn("metadata", row)
        self.assertEqual(row["narration"], "n")

    def test_dialogue_scene_row_keeps_mode_and_lines(self):
        import webapp.backend.main as m
        s = Scene(id=2, title="t", image_prompt="i", video_prompt="v", narration="",
                  mode="dialogue", lines=[{"speaker": "Kinho", "text": "Hi"}])
        row = m._scene_snapshot_row(s)
        self.assertEqual(row["metadata"]["mode"], "dialogue")
        self.assertEqual(row["metadata"]["lines"][0]["speaker"], "Kinho")

    def test_silent_scene_row_keeps_duration(self):
        import webapp.backend.main as m
        s = Scene(id=3, title="t", image_prompt="i", video_prompt="v", narration="",
                  mode="silent", duration=7.0)
        row = m._scene_snapshot_row(s)
        self.assertEqual(row["metadata"], {"mode": "silent", "duration": 7.0})


class DialogueSchemaPromptTests(unittest.TestCase):
    def test_system_prompt_without_schema_is_clean(self):
        text = _prompts.system("script_claude_initial", dialogue_schema="")
        self.assertNotIn("${dialogue_schema}", text)
        self.assertNotIn('"mode"', text)

    def test_system_prompt_with_schema_defines_mode_and_lines(self):
        schema = '\n      - "mode": "narration" | "dialogue" | "silent"\n      - "lines": ...'
        for name in ("script_claude_initial", "script_claude_continuation"):
            text = _prompts.system(name, dialogue_schema=schema)
            self.assertIn('"mode"', text, name)
            self.assertIn('"lines"', text, name)


class SilentSceneTests(unittest.TestCase):
    def test_write_silence_wav_duration(self):
        import resume_generation as rg
        from pipeline.assembler import _get_duration
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "s.wav"
            rg._write_silence_wav(p, 3.0)
            self.assertAlmostEqual(_get_duration(p), 3.0, delta=0.1)

    def test_heal_skips_silent_narration_and_keeps_metadata(self):
        import resume_generation as rg
        with tempfile.TemporaryDirectory() as td:
            wd = Path(td)
            silent = Scene(id=1, title="Quiet", image_prompt="a vista", video_prompt="pan",
                           narration="", mode="silent", duration=4.0)
            dialog = Scene(id=2, title="Talk", image_prompt="", video_prompt="",
                           narration="", mode="dialogue",
                           lines=[{"speaker": "Kinho", "text": "Hi"}])
            narr = Scene(id=3, title="Story", image_prompt="castle", video_prompt="dolly",
                         narration="")
            # cfg with no usable LLM → heal falls through to the title fallback
            rg._heal_empty_scenes([silent, dialog, narr], "T", {"llm_backend": "local"}, wd)
            self.assertEqual(silent.narration, "")     # silent stays voiceless
            self.assertEqual(dialog.narration, "")     # dialogue stays voiceless
            self.assertTrue(narr.narration)            # narration scene healed
            data = json.loads((wd / "script.json").read_text())
            by_id = {s["id"]: s for s in data}
            self.assertEqual(by_id[1]["metadata"]["mode"], "silent")
            self.assertEqual(by_id[2]["metadata"]["lines"][0]["text"], "Hi")
            self.assertNotIn("metadata", by_id[3])


class CaptionSkipTests(unittest.TestCase):
    def test_non_narration_scenes_advance_timeline_without_cues(self):
        from pipeline import captions
        with tempfile.TemporaryDirectory() as td:
            wd = Path(td)
            import resume_generation as rg
            rg._write_silence_wav(wd / "scene_01_narration.wav", 4.0)
            rg._write_silence_wav(wd / "scene_02_narration.wav", 4.0)
            (wd / "script.json").write_text(json.dumps([
                {"id": 1, "title": "s", "narration": "Leftover text. Should not show.",
                 "metadata": {"mode": "silent"}},
                {"id": 2, "title": "n", "narration": "Real narration here. Two sentences."},
            ]))
            srt = captions.build_srt(wd)
            text = srt.read_text()
            self.assertNotIn("Leftover", text)
            self.assertIn("Real narration", text)
            self.assertIn("00:00:04", text)  # narrated cue starts after the silent scene


class SceneLoadTests(unittest.TestCase):
    def test_resume_partition_reads_metadata(self):
        # mirrors resume_generation.main's hydration + partition
        rows = [
            {"id": 1, "title": "a", "image_prompt": "i", "video_prompt": "v",
             "narration": "n"},
            {"id": 2, "title": "b", "image_prompt": "i", "video_prompt": "v",
             "narration": "", "metadata": {"mode": "dialogue",
                                           "lines": [{"speaker": "K", "text": "hi"}]}},
        ]
        scenes = [
            Scene(id=s["id"], title=s["title"], image_prompt=s["image_prompt"],
                  video_prompt=s["video_prompt"], narration=s.get("narration", ""),
                  mode=str((s.get("metadata") or {}).get("mode") or "narration"),
                  lines=list((s.get("metadata") or {}).get("lines") or []),
                  duration=float((s.get("metadata") or {}).get("duration") or 0.0))
            for s in rows
        ]
        dialogue = [s for s in scenes if s.mode == "dialogue" and s.lines]
        classic = [s for s in scenes if s not in dialogue]
        self.assertEqual([s.id for s in dialogue], [2])
        self.assertEqual([s.id for s in classic], [1])


if __name__ == "__main__":
    unittest.main()
