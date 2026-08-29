"""Cross-scene shot continuation (``continues_previous``).

A scene whose metadata carries ``continues_previous: true`` picks up the
PREVIOUS scene's shot without a cut — the camera keeps moving through the
scene boundary. The flag is EXPLICIT and authored by the scene's writer: the
LLM divide step may set it (see the story_divide prompt), and the Script/film
editors expose it as a per-scene toggle.

How the render honours it depends on the path:
- acted → acted: the next take is conditioned on the H3 motion context the
  previous take left on its worker (the same mechanism as the editor's
  Continue button), so motion AND audio literally continue.
- everything else (narrated scenes, or a fallback when the context latent is
  unreachable): the previous scene's frame at its cut point becomes this
  scene's first frame, and the video prompt is told to carry the motion on.

This module owns the flag's validation and the chain grouping the scheduler
renders by: consecutive continuing scenes form a chain that must run in
order (each scene needs its predecessor's output), while unrelated chains
still render in parallel across workers.
"""

from __future__ import annotations

import logging

from pipeline import performance as _performance

logger = logging.getLogger("video_gen")


def wants_continuation(scene) -> bool:
    """The authored flag, straight off the scene's metadata."""
    meta = getattr(scene, "metadata_extra", None) or {}
    return bool(meta.get("continues_previous"))


def drop_reason(prev, scene, cfg: dict) -> str | None:
    """Why *scene* cannot continue *prev* (None ⟹ it can).

    - The first scene has nothing to continue.
    - Singing scenes hold the song's exact timeline: their takes are pinned to
      a track segment and trimmed to their window, which is incompatible with
      the join's sampled overlap on either side of the boundary.
    - Acted scenes render in a pre-pass BEFORE the narrated scenes, so an
      acted scene can never wait on a narrated predecessor. (The other way
      round is fine — the narrated phase starts after every take is done.)
    """
    if prev is None:
        return "it is the film's first scene"
    if _performance.is_singing(scene) or _performance.is_singing(prev):
        return "singing scenes hold the song's own timeline"
    if (_performance.renders_acted(scene, cfg)
            and not _performance.renders_acted(prev, cfg)):
        return "acted scenes render before narrated ones"
    return None


def continuation_plan(scenes: list, cfg: dict) -> dict[int, int]:
    """{scene.id: id of the scene it continues}, flags validated in order.

    A flag that cannot be honoured is logged and dropped — the scene renders
    as an ordinary cut rather than failing the film.
    """
    plan: dict[int, int] = {}
    prev = None
    for scene in scenes:
        if wants_continuation(scene):
            reason = drop_reason(prev, scene, cfg)
            if reason is None:
                plan[scene.id] = prev.id
            else:
                logger.warning(
                    "Scene %d: continues_previous dropped — %s", scene.id, reason)
        prev = scene
    return plan


def chain_groups(scene_list: list, plan: dict[int, int]) -> list[list]:
    """Split one render phase's scenes into chains the scheduler runs as units.

    A scene joins the previous group when the plan says it continues that
    group's last scene; otherwise it starts a new group. Groups are
    independent of each other and render in parallel; scenes inside a group
    render strictly in order.
    """
    groups: list[list] = []
    for s in scene_list:
        if groups and plan.get(s.id) == groups[-1][-1].id:
            groups[-1].append(s)
        else:
            groups.append([s])
    return groups


def hard_boundaries(scenes: list, plan: dict[int, int]) -> set[int]:
    """Positional indices i where scenes[i] → scenes[i+1] is a continued shot.

    Final assembly fades between scenes by default; a fade in the middle of a
    continuing shot reads as a glitch, so these boundaries butt-join instead.
    """
    return {i for i in range(len(scenes) - 1)
            if plan.get(scenes[i + 1].id) == scenes[i].id}
