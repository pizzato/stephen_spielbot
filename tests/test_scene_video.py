import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pipeline.llm import Scene
from pipeline.scene_video import generate_scene_video


class SceneVideoTests(unittest.TestCase):
    def test_scene_video_requests_full_scene_duration_in_one_clip(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            first_frame = work_dir / "scene_01_preview.png"
            first_frame.write_bytes(b"png")
            scene = Scene(
                id=1,
                title="Scene 1",
                image_prompt="initial image",
                video_prompt="camera move",
                narration="long narration",
            )

            durations: list[float] = []

            def fake_generate(*args, **kwargs):
                durations.append(kwargs["duration_seconds"])
                Path(args[3]).write_bytes(b"video")

            def fake_extract_audio(video_path, output_path, duration=None):
                output_path.write_bytes(b"audio")
                return output_path

            with mock.patch("pipeline.scene_video.generate_video_continuation", side_effect=fake_generate), \
                 mock.patch("pipeline.scene_video._get_duration", return_value=31.2), \
                 mock.patch("pipeline.scene_video.extract_audio", side_effect=fake_extract_audio):
                raw, ambient = generate_scene_video(
                    scene,
                    work_dir,
                    narration_dur=30.0,
                    vid_width=512,
                    vid_height=288,
                    max_clip_secs=20,
                    lora_strength=0.5,
                    first_pass_cfg=1.0,
                    first_pass_steps=8,
                    second_pass_cfg=3.0,
                    second_pass_steps=6,
                    comfy_url="http://worker:8188",
                    scene_first_frame=first_frame,
                )

            self.assertEqual(durations, [31.0])
            self.assertEqual(raw.name, "scene_01_clip_01.mp4")
            self.assertTrue(ambient.exists())

    def test_on_first_frame_fires_after_flux_before_video(self):
        """The image-done callback must run the instant FLUX lands — before the
        much longer I2V step — so a separate image task isn't timed as the video."""
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            scene = Scene(
                id=1,
                title="Scene 1",
                image_prompt="initial image",
                video_prompt="camera move",
                narration="long narration",
            )

            order: list[str] = []

            def fake_image(prompt, output_path, **kwargs):
                order.append("flux")
                Path(output_path).write_bytes(b"png")

            def fake_video(*args, **kwargs):
                order.append("video")
                Path(args[3]).write_bytes(b"video")

            def fake_extract_audio(video_path, output_path, duration=None):
                output_path.write_bytes(b"audio")
                return output_path

            seen: list[Path] = []

            def on_first_frame(frame: Path):
                order.append("callback")
                seen.append(frame)

            with mock.patch("pipeline.scene_video.generate_scene_image", side_effect=fake_image), \
                 mock.patch("pipeline.scene_video.generate_video_continuation", side_effect=fake_video), \
                 mock.patch("pipeline.scene_video._get_duration", return_value=31.2), \
                 mock.patch("pipeline.scene_video.extract_audio", side_effect=fake_extract_audio):
                generate_scene_video(
                    scene,
                    work_dir,
                    narration_dur=30.0,
                    vid_width=512,
                    vid_height=288,
                    max_clip_secs=20,
                    lora_strength=0.5,
                    first_pass_cfg=1.0,
                    first_pass_steps=8,
                    second_pass_cfg=3.0,
                    second_pass_steps=6,
                    comfy_url="http://worker:8188",
                    on_first_frame=on_first_frame,
                )

            self.assertEqual(order, ["flux", "callback", "video"])
            self.assertEqual(seen, [work_dir / "scene_01_first_frame.png"])

    def test_on_first_frame_skipped_when_frame_supplied(self):
        """A pre-supplied first frame means FLUX is skipped, so the callback —
        which exists to time the FLUX step — must not fire."""
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            first_frame = work_dir / "scene_01_preview.png"
            first_frame.write_bytes(b"png")
            scene = Scene(id=1, title="Scene 1", image_prompt="x", video_prompt="y", narration="z")

            fired: list[Path] = []

            def fake_video(*args, **kwargs):
                Path(args[3]).write_bytes(b"video")

            def fake_extract_audio(video_path, output_path, duration=None):
                output_path.write_bytes(b"audio")
                return output_path

            with mock.patch("pipeline.scene_video.generate_video_continuation", side_effect=fake_video), \
                 mock.patch("pipeline.scene_video._get_duration", return_value=31.2), \
                 mock.patch("pipeline.scene_video.extract_audio", side_effect=fake_extract_audio):
                generate_scene_video(
                    scene,
                    work_dir,
                    narration_dur=30.0,
                    vid_width=512,
                    vid_height=288,
                    max_clip_secs=20,
                    lora_strength=0.5,
                    first_pass_cfg=1.0,
                    first_pass_steps=8,
                    second_pass_cfg=3.0,
                    second_pass_steps=6,
                    comfy_url="http://worker:8188",
                    scene_first_frame=first_frame,
                    on_first_frame=fired.append,
                )

            self.assertEqual(fired, [])


if __name__ == "__main__":
    unittest.main()
