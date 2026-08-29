"""End-to-end backend tests for the per-script character endpoints.

Drives the real FastAPI app: generate a script (LLM mocked to identify a
character), then exercise the job-scoped Characters CRUD + promote the editor's
tab uses. No workers, so portrait generation is expected to 503."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml
from fastapi.testclient import TestClient

import app as gapp
from pipeline.llm import Scene
from webapp.backend import main as backend
from scriptstub import stub_script
from test_styles import _style


class ScriptCharacterApiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="spielbot-charapi-")
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
            "default_style": "Hero", "characters": [], "characters_migrated_v2": True,
            "characters_scoped_v3": True,
        }))
        self.client = TestClient(backend.api)

    def _make_job(self, characters, image_prompt="Caesar stands."):
        scene = Scene(id=1, title="T", image_prompt=image_prompt, video_prompt="v", narration="n")
        with stub_script([scene], characters), \
             mock.patch.object(backend, "_describe_in_background"):
            res = backend._do_script_generate(backend.GenerateScriptBody(
                topic="Julius Caesar", n_scenes=1, style_name="Hero"))
        return res

    def test_generate_persists_and_lists_identified_character(self):
        res = self._make_job([{"name": "Julius Caesar", "aliases": ["Caesar"],
                               "description": "a lean Roman general in a red toga"}])
        self.assertEqual(len(res["characters"]), 1)
        job_id = res["job_id"]
        r = self.client.get(f"/api/jobs/{job_id}/characters")
        self.assertEqual(r.status_code, 200)
        chars = r.json()["characters"]
        self.assertEqual(chars[0]["name"], "Julius Caesar")
        self.assertEqual(chars[0]["aliases"], ["Caesar"])
        self.assertFalse(chars[0]["has_image"])  # no worker → no look yet

    def test_generate_skips_characters_already_in_style_catalogue(self):
        # Caesar is scoped to Hero; the LLM also "identifies" Caesar + a new
        # Brutus. Only Brutus becomes a per-script character.
        self.config_file.write_text(yaml.safe_dump({
            "styles": [_style("Hero")],
            "default_style": "Hero",
            "characters": [{"id": "char_caesar", "name": "Julius Caesar",
                            "aliases": ["Caesar"], "description": "catalogue look",
                            "style": "Hero"}],
            "characters_migrated_v2": True, "characters_scoped_v3": True,
        }))
        res = self._make_job([
            {"name": "Julius Caesar", "aliases": ["Caesar"], "description": "LLM override"},
            {"name": "Brutus", "aliases": [], "description": "a senator"},
        ])
        self.assertEqual([c["name"] for c in res["characters"]], ["Brutus"])
        # Global catalogue still wins at render time (no script shadow for Caesar).
        cfg = gapp.load_config()
        job = [c for c in gapp._job_characters(cfg, "Hero", Path(res["work_dir"]))
               if c["name"] == "Julius Caesar"]
        self.assertEqual(len(job), 1)
        self.assertEqual(job[0]["description"], "catalogue look")
        # The Characters & Artifacts wall must still show that read-only style
        # character. Narrated scenes have no metadata.cast, so this resolves the
        # alias from the same image prompt the renderer uses.
        listed = self.client.get(f"/api/jobs/{res['job_id']}/characters").json()
        self.assertEqual([c["name"] for c in listed["catalogue"]], ["Julius Caesar"])

    def test_global_catalogue_character_present_in_story_is_listed(self):
        self.config_file.write_text(yaml.safe_dump({
            "styles": [_style("Hero")],
            "default_style": "Hero",
            "characters": [{"id": "char_kinho", "name": "Kinho",
                            "aliases": [], "description": "global canonical look",
                            "style": ""}],
            "characters_migrated_v2": True, "characters_scoped_v3": True,
        }))
        res = self._make_job(
            [{"name": "Kinho", "aliases": [], "description": "LLM override"}],
            image_prompt="Kinho waits beside the window.")
        listed = self.client.get(f"/api/jobs/{res['job_id']}/characters").json()
        self.assertEqual(listed["characters"], [])
        self.assertEqual([c["name"] for c in listed["catalogue"]], ["Kinho"])

    def test_crud_roundtrip(self):
        job_id = self._make_job([{"name": "Caesar", "description": "a general"}])["job_id"]
        cid = self.client.get(f"/api/jobs/{job_id}/characters").json()["characters"][0]["id"]

        # edit
        r = self.client.put(f"/api/jobs/{job_id}/characters/{cid}",
                            json={"description": "a lean general in a red toga"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("red toga", r.json()["characters"][0]["description"])

        # add
        r = self.client.post(f"/api/jobs/{job_id}/characters",
                            json={"name": "Brutus", "aliases": [], "description": "a senator"})
        self.assertEqual({c["name"] for c in r.json()["characters"]}, {"Caesar", "Brutus"})

        # delete the added one
        bid = next(c["id"] for c in r.json()["characters"] if c["name"] == "Brutus")
        r = self.client.delete(f"/api/jobs/{job_id}/characters/{bid}")
        self.assertEqual([c["name"] for c in r.json()["characters"]], ["Caesar"])

    def test_portrait_without_workers_returns_503(self):
        job_id = self._make_job([{"name": "Caesar", "description": "a general"}])["job_id"]
        cid = self.client.get(f"/api/jobs/{job_id}/characters").json()["characters"][0]["id"]
        with mock.patch.object(gapp, "_preview_worker_urls", return_value=[]):
            r = self.client.post(f"/api/jobs/{job_id}/characters/{cid}/portrait", json={})
        self.assertEqual(r.status_code, 503)

    def test_promote_to_catalogue(self):
        job_id = self._make_job([{"name": "Caesar", "description": "a general"}])["job_id"]
        cid = self.client.get(f"/api/jobs/{job_id}/characters").json()["characters"][0]["id"]
        r = self.client.post(f"/api/jobs/{job_id}/characters/{cid}/promote")
        self.assertEqual(r.status_code, 200)
        lib = r.json()["config"]["characters"]
        self.assertEqual([c["name"] for c in lib], ["Caesar"])
        # the new catalogue character is scoped to the job's style
        self.assertEqual(lib[0]["style"], "Hero")
        # per-script copy remains (non-destructive)
        self.assertEqual(len(self.client.get(f"/api/jobs/{job_id}/characters").json()["characters"]), 1)

    def test_unknown_job_404(self):
        r = self.client.get("/api/jobs/deadbeef/characters")
        self.assertEqual(r.status_code, 404)

    def _run_start_generation(self, job):
        """Drive start_generation with the heavy steps stubbed, recording the
        order of the character pre-build vs the render-plan registration."""
        calls = []
        with mock.patch.object(gapp, "generate_all_script_portraits",
                               side_effect=lambda *a, **k: calls.append("portraits")), \
             mock.patch.object(backend, "generate_all_previews",
                               side_effect=lambda *a, **k: calls.append("previews")), \
             mock.patch.object(backend.DurableStore, "ensure_generation_plan",
                               side_effect=lambda self, *a, **k: calls.append("plan")), \
             mock.patch.object(gapp, "_launch_generation_job",
                               side_effect=lambda *a, **k: calls.append("launch") or {}):
            backend.start_generation(backend.GenerateBody(
                job_id=job["job_id"], work_dir=job["work_dir"], n_scenes=1, style_name="Hero"))
        return calls

    def test_render_builds_character_before_scene_plan(self):
        job = self._make_job([{"name": "Caesar", "description": "a lean general"}])
        calls = self._run_start_generation(job)
        # Portraits + reference-conditioned previews happen BEFORE the plan (and
        # thus before any scene image task), then the worker launches.
        self.assertEqual(calls, ["portraits", "previews", "plan", "launch"])

    def test_render_keeps_existing_scene_images(self):
        """Approving a script whose scenes already have images must NOT repaint
        them. start_generation calls generate_all_previews as a plain function,
        so the endpoint's Query(False) default for `force` never resolved —
        and a Query object is truthy, which regenerated every scene before
        every render."""
        job = self._make_job([{"name": "Caesar", "description": "a lean general"}])
        wd = Path(job["work_dir"])
        preview = wd / "scene_01_preview.png"
        preview.write_bytes(b"png")
        store = backend.DurableStore.default()
        try:
            store.update_scene_preview(job["job_id"], 1, preview)
        finally:
            store.close()
        painted = []
        with mock.patch.object(gapp, "generate_all_script_portraits"), \
             mock.patch.object(gapp, "_preview_worker_urls", return_value=["http://w:8188"]), \
             mock.patch.object(gapp, "_generate_active_scene_preview",
                               side_effect=lambda *a, **k: painted.append(a)), \
             mock.patch.object(backend.DurableStore, "ensure_generation_plan"), \
             mock.patch.object(gapp, "_launch_generation_job", return_value={}):
            backend.start_generation(backend.GenerateBody(
                job_id=job["job_id"], work_dir=job["work_dir"], n_scenes=1, style_name="Hero"))
        self.assertEqual(painted, [])
        self.assertEqual(preview.read_bytes(), b"png")

    def test_render_skips_prebuild_without_characters(self):
        job = self._make_job([])  # abstract topic — no recurring characters
        calls = self._run_start_generation(job)
        self.assertNotIn("portraits", calls)
        self.assertNotIn("previews", calls)
        self.assertEqual(calls, ["plan", "launch"])


if __name__ == "__main__":
    unittest.main()
