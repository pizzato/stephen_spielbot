"""Style hierarchy — parent styles with sparse child overrides.

A style may declare ``parent: <other style name>`` and store only the fields it
overrides; everything else resolves through the parent chain in
style_settings. Covers: _ensure_styles keeping children sparse (no default
densification, no coercion materialization), cycle/self-parent handling,
lineage resolution incl. description, the flat-key mirror when the DEFAULT
style is a child, propagation of later parent edits, the auto-pick and
promote-to-catalogue consumers, voice rename/delete sweeps, and the
start_generation job snapshot for a child style.
"""
import json
import os
import tempfile
import unittest
from unittest import mock

os.environ["HOME"] = tempfile.mkdtemp(prefix="spielbot-test-home-")

import app
import webapp.backend.main as backend
from pipeline.orchestrator import DurableStore, job_id_from_work_dir
from test_styles import TempConfigCase, _style


def _child(name, parent, **overrides):
    """A sparse child style: name + parent + explicit overrides only."""
    row = {"name": name, "parent": parent}
    row.update(overrides)
    return row


class EnsureStylesHierarchyTests(TempConfigCase):
    def test_child_stays_sparse_and_root_densifies(self):
        self.write_config({
            "styles": [_style("BHOB"),
                       _child("BHOB ES", "BHOB", voice="Spanish-voice", tts_language="es")],
            "default_style": "BHOB",
        })
        cfg = app.load_config()
        root = next(s for s in cfg["styles"] if s["name"] == "BHOB")
        child = next(s for s in cfg["styles"] if s["name"] == "BHOB ES")
        # Root keeps today's dense normalization.
        for field in app.STYLE_FIELD_TO_FLAT:
            self.assertIn(field, root)
        self.assertNotIn("parent", root)
        # Child holds ONLY its overrides — absent fields keep inheriting, and
        # none of the coercers (script_mode, size_presets, engines, …) may
        # materialize a value onto it.
        self.assertEqual(child, {"name": "BHOB ES", "parent": "BHOB",
                                 "voice": "Spanish-voice", "tts_language": "es"})

    def test_round_trip_is_idempotent_for_children(self):
        self.write_config({
            "styles": [_style("BHOB"), _child("BHOB ES", "BHOB", voice="Spanish-voice")],
            "default_style": "BHOB",
        })
        cfg = app.load_config()
        app.save_config(cfg)
        cfg2 = app.load_config()
        child = next(s for s in cfg2["styles"] if s["name"] == "BHOB ES")
        self.assertEqual(child, {"name": "BHOB ES", "parent": "BHOB",
                                 "voice": "Spanish-voice"})

    def test_child_override_is_still_coerced(self):
        self.write_config({
            "styles": [_style("BHOB"),
                       _child("BHOB Story", "BHOB", script_mode="bogus",
                              image_engine="not-an-engine")],
            "default_style": "BHOB",
        })
        cfg = app.load_config()
        child = next(s for s in cfg["styles"] if s["name"] == "BHOB Story")
        self.assertEqual(child["script_mode"], "classic")
        self.assertEqual(sorted(child), ["image_engine", "name", "parent", "script_mode"])

    def test_self_parent_is_dropped_and_densified(self):
        self.write_config({
            "styles": [{"name": "Loner", "parent": "Loner", "voice": "v"}],
            "default_style": "Loner",
        })
        cfg = app.load_config()
        st = cfg["styles"][0]
        self.assertNotIn("parent", st)
        self.assertEqual(st["voice"], "v")
        for field in app.STYLE_FIELD_TO_FLAT:
            self.assertIn(field, st)

    def test_cycle_is_severed_and_severed_style_densified(self):
        self.write_config({
            "styles": [_child("A", "B", voice="a-voice"), _child("B", "A", voice="b-voice")],
            "default_style": "A",
        })
        cfg = app.load_config()  # must not hang
        a = next(s for s in cfg["styles"] if s["name"] == "A")
        b = next(s for s in cfg["styles"] if s["name"] == "B")
        parents = [bool(a.get("parent")), bool(b.get("parent"))]
        self.assertEqual(sorted(parents), [False, True])  # exactly one link survives
        severed = a if not a.get("parent") else b
        for field in app.STYLE_FIELD_TO_FLAT:
            self.assertIn(field, severed)

    def test_dangling_parent_is_kept_and_child_stays_sparse(self):
        self.write_config({
            "styles": [_style("BHOB"), _child("Orphan", "Ghost", voice="own-voice")],
            "default_style": "BHOB",
        })
        cfg = app.load_config()
        child = next(s for s in cfg["styles"] if s["name"] == "Orphan")
        # The pointer survives (a restore may bring "Ghost" back) and no
        # defaults are frozen in meanwhile.
        self.assertEqual(child, {"name": "Orphan", "parent": "Ghost", "voice": "own-voice"})
        ss = app.style_settings(cfg, "Orphan")
        self.assertEqual(ss["voice"], "own-voice")  # override applies
        # With no reachable parent the walk stops at the orphan, which then
        # resolves against the flat keys — i.e. the default style's mirror.
        self.assertEqual(ss["visual_style"], "BHOB visual")

    def test_no_style_sentinel_cannot_be_a_parent(self):
        self.write_config({
            "styles": [_style("Real"), _child("Kid", app.NO_STYLE, voice="v")],
            "default_style": "Real",
        })
        cfg = app.load_config()
        kid = next(s for s in cfg["styles"] if s["name"] == "Kid")
        self.assertNotIn("parent", kid)


class LineageResolutionTests(TempConfigCase):
    def _seed(self):
        self.write_config({
            "styles": [
                _style("BHOB", tts_language="en"),
                _child("BHOB ES", "BHOB", voice="Spanish-voice", tts_language="es"),
                _child("BHOB ES Kids", "BHOB ES", voice_speed=0.8),
            ],
            "default_style": "BHOB",
        })
        return app.load_config()

    def test_child_inherits_and_overrides(self):
        cfg = self._seed()
        ss = app.style_settings(cfg, "BHOB ES")
        self.assertEqual(ss["voice"], "Spanish-voice")          # own override
        self.assertEqual(ss["tts_language"], "es")              # own override
        self.assertEqual(ss["visual_style"], "BHOB visual")     # inherited
        self.assertEqual(ss["title_style"], "BHOB title style") # inherited
        self.assertEqual(ss["music_vol"], 11)                   # inherited mix
        self.assertEqual(ss["description"], "BHOB look")        # inherited
        self.assertEqual(ss["name"], "BHOB ES")

    def test_grandchild_resolves_through_the_whole_chain(self):
        cfg = self._seed()
        ss = app.style_settings(cfg, "BHOB ES Kids")
        self.assertEqual(ss["voice_speed"], 0.8)             # own
        self.assertEqual(ss["voice"], "Spanish-voice")       # from BHOB ES
        self.assertEqual(ss["tts_language"], "es")           # from BHOB ES
        self.assertEqual(ss["visual_style"], "BHOB visual")  # from BHOB

    def test_parent_edit_flows_to_children(self):
        cfg = self._seed()
        root = next(s for s in cfg["styles"] if s["name"] == "BHOB")
        root["visual_style"] = "new premise look"
        app.save_config(cfg)
        cfg2 = app.load_config()
        self.assertEqual(app.style_settings(cfg2, "BHOB ES")["visual_style"],
                         "new premise look")
        self.assertEqual(app.style_settings(cfg2, "BHOB ES Kids")["visual_style"],
                         "new premise look")

    def test_child_description_override_wins(self):
        self.write_config({
            "styles": [_style("BHOB"),
                       _child("BHOB ES", "BHOB", description="la versión en español")],
            "default_style": "BHOB",
        })
        cfg = app.load_config()
        self.assertEqual(app.style_settings(cfg, "BHOB ES")["description"],
                         "la versión en español")

    def test_default_style_as_child_mirrors_effective_values_to_flat(self):
        self.write_config({
            "styles": [_style("BHOB"), _child("BHOB ES", "BHOB", voice="Spanish-voice")],
            "default_style": "BHOB ES",
        })
        cfg = app.load_config()
        # resume_generation.py / pipeline/llm.py read these raw — they must see
        # the child's EFFECTIVE settings, not blanks.
        self.assertEqual(cfg["default_voice"], "Spanish-voice")
        self.assertEqual(cfg["default_visual_style"], "BHOB visual")
        self.assertEqual(cfg["music_vol"], 11)
        app.save_config(cfg)
        raw = self.read_config()
        self.assertEqual(raw["default_voice"], "Spanish-voice")
        self.assertEqual(raw["default_visual_style"], "BHOB visual")

    def test_no_style_blanks_content_even_with_child_default(self):
        self.write_config({
            "styles": [_style("BHOB"), _child("BHOB ES", "BHOB", voice="Spanish-voice")],
            "default_style": "BHOB ES",
        })
        cfg = app.load_config()
        ss = app.style_settings(cfg, app.NO_STYLE)
        self.assertEqual(ss["voice"], "")
        self.assertEqual(ss["visual_style"], "")
        self.assertEqual(ss["music_vol"], 11)  # mix still tracks the default chain


class HierarchyConsumerTests(TempConfigCase):
    def test_auto_pick_exclude_inherits_and_child_can_opt_back_in(self):
        self.write_config({
            "styles": [
                _style("Main", auto_pick_exclude=True),
                _child("Kid Out", "Main"),
                _child("Kid In", "Main", auto_pick_exclude=False),
                _style("Other"),
            ],
            "default_style": "Main",
        })
        cfg = app.load_config()
        self.assertEqual(app._auto_pick_styles(cfg), ["Kid In", "Other"])

    def test_voice_rename_touches_overrides_only_and_flows_via_parent(self):
        self.write_config({
            "voices": [{"name": "Old-voice", "path": str(self.config_file.parent / "old.wav")}],
            "styles": [
                _style("Main", voice="Old-voice"),
                _child("Inheriting", "Main"),
                _child("Overriding", "Main", voice="Old-voice"),
            ],
            "default_style": "Main",
        })
        app.update_voice("Old-voice", new_name="New-voice")
        cfg = app.load_config()
        inheriting = next(s for s in cfg["styles"] if s["name"] == "Inheriting")
        overriding = next(s for s in cfg["styles"] if s["name"] == "Overriding")
        self.assertNotIn("voice", inheriting)  # still sparse
        self.assertEqual(overriding["voice"], "New-voice")
        self.assertEqual(app.style_settings(cfg, "Inheriting")["voice"], "New-voice")

    def test_x_and_channel_clears_leave_sparse_children_alone(self):
        self.write_config({
            "styles": [_style("Main", channel="gone", x_account="gone"),
                       _child("Kid", "Main")],
            "default_style": "Main",
        })
        cfg = app.load_config()  # no connected channels/accounts → clears refs
        kid = next(s for s in cfg["styles"] if s["name"] == "Kid")
        self.assertNotIn("channel", kid)
        self.assertNotIn("x_account", kid)
        self.assertEqual(app.style_settings(cfg, "Kid")["channel"], "")


class PromoteNearestOwnerTests(TempConfigCase):
    def _seed_script_char(self):
        wd = self.output_dir / "vid-20260101-000000"
        wd.mkdir(parents=True, exist_ok=True)
        saved = app._write_script_characters(wd, [{"name": "Caesar", "description": "a lean general"}])
        return wd, saved[0]["id"]

    def test_promote_via_child_lands_on_the_parents_roster(self):
        self.write_config({
            "characters": [], "characters_migrated_v2": True,
            "styles": [_style("BHOB", character_ids=[]),
                       _child("BHOB ES", "BHOB", voice="Spanish-voice")],
            "default_style": "BHOB",
        })
        wd, cid = self._seed_script_char()
        cfg = app.promote_script_character(wd, cid, "BHOB ES")
        new_id = cfg["characters"][0]["id"]
        root = next(s for s in cfg["styles"] if s["name"] == "BHOB")
        child = next(s for s in cfg["styles"] if s["name"] == "BHOB ES")
        self.assertIn(new_id, root["character_ids"])
        self.assertNotIn("character_ids", child)  # roster stays shared
        self.assertIn(new_id, app.style_settings(cfg, "BHOB ES")["character_ids"])

    def test_promote_respects_a_childs_own_roster_override(self):
        self.write_config({
            "characters": [], "characters_migrated_v2": True,
            "styles": [_style("BHOB", character_ids=[]),
                       _child("BHOB ES", "BHOB", character_ids=[])],
            "default_style": "BHOB",
        })
        wd, cid = self._seed_script_char()
        cfg = app.promote_script_character(wd, cid, "BHOB ES")
        new_id = cfg["characters"][0]["id"]
        root = next(s for s in cfg["styles"] if s["name"] == "BHOB")
        child = next(s for s in cfg["styles"] if s["name"] == "BHOB ES")
        self.assertIn(new_id, child["character_ids"])
        self.assertNotIn(new_id, root["character_ids"])

    def test_inherited_auto_accept_skips_roster_write(self):
        self.write_config({
            "characters": [], "characters_migrated_v2": True,
            "styles": [_style("BHOB", character_ids=[], auto_accept_characters=True),
                       _child("BHOB ES", "BHOB")],
            "default_style": "BHOB",
        })
        wd, cid = self._seed_script_char()
        cfg = app.promote_script_character(wd, cid, "BHOB ES")
        root = next(s for s in cfg["styles"] if s["name"] == "BHOB")
        self.assertEqual(root["character_ids"], [])  # auto-accept covers it


class ChildStyleJobSnapshotTests(TempConfigCase):
    def test_job_config_carries_inherited_and_overridden_values(self):
        self.write_config({
            "styles": [
                _style("A", music_vol=42, lora_strength=0.9),
                _child("A ES", "A", voice="Spanish-voice", voice_speed=0.5),
            ],
            "default_style": "A",
        })
        work_dir = self.output_dir / "child-job-20260610-101010"
        work_dir.mkdir()
        job_id = job_id_from_work_dir(work_dir)
        store = DurableStore.default()
        try:
            store.create_or_update_job(job_id, work_dir, "Child job",
                                       config={"style_name": "A ES"})
            store.upsert_scenes(job_id, [{
                "id": 1, "title": "One", "image_prompt": "a frame",
                "video_prompt": "a move", "narration": "words",
            }])
        finally:
            store.close()
        with mock.patch.object(app, "_launch_generation_job", return_value={}):
            backend.start_generation(backend.GenerateBody(
                job_id=job_id, work_dir=str(work_dir), video_title="Child job",
            ))
        jc = json.loads((work_dir / "job_config.json").read_text())
        self.assertEqual(jc["style_name"], "A ES")
        self.assertEqual(jc["default_voice"], "Spanish-voice")  # override
        self.assertEqual(jc["voice_speed"], 0.5)                # override
        self.assertEqual(jc["music_vol"], 42)                   # inherited
        self.assertEqual(jc["lora_strength"], 0.9)              # inherited
        self.assertNotIn("styles", jc)


class ConfigEndpointHierarchyTests(TempConfigCase):
    def test_posted_sparse_child_round_trips_sparse(self):
        self.write_config({
            "styles": [_style("BHOB")],
            "default_style": "BHOB",
        })
        cfg = app.load_config()
        posted = dict(app.public_config(cfg))
        posted["styles"] = list(cfg["styles"]) + [
            _child("BHOB ES", "BHOB", voice="Spanish-voice")]
        out = backend.post_config(backend.ConfigUpdate(config=posted))
        child = next(s for s in out["config"]["styles"] if s["name"] == "BHOB ES")
        self.assertEqual(child, {"name": "BHOB ES", "parent": "BHOB",
                                 "voice": "Spanish-voice"})
        raw = self.read_config()
        raw_child = next(s for s in raw["styles"] if s["name"] == "BHOB ES")
        self.assertEqual(raw_child, {"name": "BHOB ES", "parent": "BHOB",
                                     "voice": "Spanish-voice"})


if __name__ == "__main__":
    unittest.main()
