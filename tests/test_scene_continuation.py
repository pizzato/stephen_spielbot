"""Cross-scene continuation (continues_previous): validation, chain grouping,
and the fade-free joins the assembly owes a continued shot.

The contract: the flag is EXPLICIT (the divide step or the editor sets it),
dependent scenes render strictly after the scene they continue, and everything
that cannot be honoured degrades to an ordinary cut instead of failing a film.
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import continuity  # noqa: E402
from pipeline.llm import Scene  # noqa: E402


def _scene(sid, mode="narration", continues=False, singing=False, lines=None):
    extra = {}
    if continues:
        extra["continues_previous"] = True
    if singing:
        extra["singing"] = True
    return Scene(
        id=sid, title=f"Scene {sid}", image_prompt="img", video_prompt="vid",
        narration="words" if mode == "narration" else "",
        mode=mode, lines=list(lines or []), metadata_extra=extra,
    )


def _dialogue(sid, continues=False):
    return _scene(sid, mode="dialogue", continues=continues,
                  lines=[{"speaker": "Ada", "text": "Hello."}])


class TestContinuationPlan(unittest.TestCase):
    def test_honoured_chain(self):
        scenes = [_scene(1), _scene(2), _scene(3, continues=True), _scene(4)]
        self.assertEqual(continuity.continuation_plan(scenes, {}), {3: 2})

    def test_first_scene_is_dropped(self):
        scenes = [_scene(1, continues=True), _scene(2)]
        self.assertEqual(continuity.continuation_plan(scenes, {}), {})

    def test_singing_scenes_are_dropped_on_either_side(self):
        sing = _scene(2, mode="silent", singing=True)
        self.assertEqual(continuity.continuation_plan(
            [_scene(1), sing, _scene(3, continues=True)], {}), {})
        sing2 = _scene(2, mode="silent", singing=True, continues=True)
        self.assertEqual(continuity.continuation_plan([_scene(1), sing2], {}), {})

    def test_acted_cannot_continue_narrated(self):
        # Acted scenes render in a pre-pass before any narrated clip exists.
        scenes = [_scene(1), _dialogue(2, continues=True)]
        self.assertEqual(continuity.continuation_plan(scenes, {}), {})

    def test_narrated_can_continue_acted(self):
        scenes = [_dialogue(1), _scene(2, continues=True)]
        self.assertEqual(continuity.continuation_plan(scenes, {}), {2: 1})

    def test_acted_continues_acted(self):
        scenes = [_dialogue(1), _dialogue(2, continues=True)]
        self.assertEqual(continuity.continuation_plan(scenes, {}), {2: 1})

    def test_stored_rows_work_too(self):
        # The backend's rebuild paths pass store rows, not Scene objects.
        rows = [{"id": 1, "metadata": {}},
                {"id": 2, "metadata": {"continues_previous": True}}]
        self.assertEqual(continuity.continuation_plan(rows, {}), {2: 1})
        self.assertEqual(continuity.hard_boundaries(rows, {2: 1}), {0})


class TestChainGroups(unittest.TestCase):
    def test_the_four_scene_example(self):
        # 4 scenes, scene 3 continues scene 2: [1] and [2,3] and [4] — the
        # dependent pair renders in order, the rest in parallel.
        scenes = [_scene(1), _scene(2), _scene(3, continues=True), _scene(4)]
        plan = continuity.continuation_plan(scenes, {})
        groups = continuity.chain_groups(scenes, plan)
        self.assertEqual([[s.id for s in g] for g in groups], [[1], [2, 3], [4]])

    def test_group_breaks_when_predecessor_is_in_another_phase(self):
        # A narrated scene continuing an ACTED one starts its own group in the
        # narrated pool — the handoff comes from the acted final instead.
        acted = _dialogue(1)
        classic = [_scene(2, continues=True), _scene(3)]
        plan = continuity.continuation_plan([acted, *classic], {})
        groups = continuity.chain_groups(classic, plan)
        self.assertEqual([[s.id for s in g] for g in groups], [[2], [3]])
        self.assertEqual(plan, {2: 1})

    def test_longer_chain_stays_ordered(self):
        scenes = [_scene(1), _scene(2, continues=True), _scene(3, continues=True)]
        plan = continuity.continuation_plan(scenes, {})
        groups = continuity.chain_groups(scenes, plan)
        self.assertEqual([[s.id for s in g] for g in groups], [[1, 2, 3]])


class TestHardBoundaries(unittest.TestCase):
    def test_positional_indices(self):
        scenes = [_scene(1), _scene(2), _scene(3, continues=True), _scene(4)]
        plan = continuity.continuation_plan(scenes, {})
        self.assertEqual(continuity.hard_boundaries(scenes, plan), {1})

    def test_no_flags_no_boundaries(self):
        scenes = [_scene(1), _scene(2)]
        self.assertEqual(continuity.hard_boundaries(scenes, {}), set())


class TestDivideLiftsTheFlag(unittest.TestCase):
    """The divide step's JSON reaches the Scene as metadata."""

    def test_narration_scene(self):
        from pipeline.story import _scene_from_item
        s = _scene_from_item(2, {"title": "T", "narration": "n",
                                 "continues_previous": True}, "Film", None)
        self.assertTrue(continuity.wants_continuation(s))
        self.assertTrue(s.metadata["continues_previous"])

    def test_never_on_scene_one(self):
        from pipeline.story import _scene_from_item
        s = _scene_from_item(1, {"title": "T", "narration": "n",
                                 "continues_previous": True}, "Film", None)
        self.assertFalse(continuity.wants_continuation(s))

    def test_absent_flag_keeps_metadata_sparse(self):
        from pipeline.story import _scene_from_item
        s = _scene_from_item(2, {"title": "T", "narration": "n"}, "Film", None)
        self.assertNotIn("continues_previous", s.metadata or {})

    def test_silent_scene(self):
        from pipeline.story import _scene_from_item
        s = _scene_from_item(3, {"title": "T", "mode": "silent", "seconds": 8,
                                 "continues_previous": True}, "Film", None)
        self.assertTrue(continuity.wants_continuation(s))

    def test_dialogue_scene(self):
        from pipeline.performance import scene_from_raw
        s = scene_from_raw(2, {"title": "T", "continues_previous": True,
                               "lines": [{"speaker": "Ada", "text": "Hi."}]})
        self.assertTrue(continuity.wants_continuation(s))


class TestConcatHardJoins(unittest.TestCase):
    """concatenate_scenes must not fade across a continued boundary."""

    def _filtergraph(self, hard):
        from pipeline import assembler
        paths = [Path(f"/tmp/s{i}.mp4") for i in range(3)]
        with mock.patch.object(assembler, "_run") as run, \
             mock.patch.object(assembler, "_get_duration", return_value=10.0), \
             mock.patch.object(assembler, "_has_audio_stream", return_value=True):
            assembler.concatenate_scenes(paths, Path("/tmp/out.mp4"),
                                         hard_boundaries=hard)
            cmd = run.call_args[0][0]
        return cmd[cmd.index("-filter_complex") + 1]

    def test_fades_everywhere_by_default(self):
        # ",fade=" is the video filter — "afade" would match a bare "fade".
        graph = self._filtergraph(None)
        self.assertEqual(graph.count(",fade=t=out"), 2)
        self.assertEqual(graph.count(",fade=t=in"), 2)
        self.assertEqual(graph.count("afade=t=out"), 2)
        self.assertEqual(graph.count("afade=t=in"), 2)

    def test_hard_boundary_drops_video_fades_but_keeps_audio_declick(self):
        # Boundary 0→1 is a continued shot: clip 0 loses its fade-out, clip 1
        # its fade-in (the untouched 1→2 boundary keeps both). The 0.05 s
        # audio fades survive everywhere — they are a declick, not a
        # transition, and a frame-handoff join splices two unrelated
        # narration tracks that would pop without them.
        graph = self._filtergraph({0})
        self.assertEqual(graph.count(",fade=t=out"), 1)
        self.assertEqual(graph.count(",fade=t=in"), 1)
        self.assertEqual(graph.count("afade=t=out"), 2)
        self.assertEqual(graph.count("afade=t=in"), 2)


if __name__ == "__main__":
    unittest.main()
