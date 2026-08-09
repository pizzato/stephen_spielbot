"""Stub the story-first pipeline so a test can get a script without an LLM.

Every script is written story-first (draft the prose, then divide it into
scenes), so a test that just needs "a job with these scenes" has two LLM calls
to stand in for. This is the one place that knows that.
"""
from contextlib import contextmanager
from unittest import mock

import webapp.backend.main as backend

STORY = {
    "topic": "T",
    "video_title": "T",
    "n_scenes": 1,
    "style": "vis",
    "music": "music",
    "characters": [],
    "chapters": [{"chapter": 1, "title": "One", "text": "The story.", "scenes": 1}],
    "status": "draft",
}


@contextmanager
def stub_script(scenes, characters=(), *, music="music", style="vis", story=None):
    """Both story phases stubbed: the draft, and the division into `scenes`.

    The draft echoes back the scene count and cadence plan it was asked for —
    the real one does, and the create brief is written from them."""
    def draft_story(title, n_scenes, **kw):
        return {**(story or STORY), "n_scenes": n_scenes,
                "scene_plan": kw.get("scene_plan")}

    with mock.patch.object(backend.story_mode, "generate_story",
                           side_effect=draft_story) as draft, \
         mock.patch.object(backend.story_mode, "divide_story",
                           return_value=(list(scenes), music, style,
                                         list(characters))) as divide:
        yield draft, divide
