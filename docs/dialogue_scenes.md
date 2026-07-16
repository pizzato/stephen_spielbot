# Dialogue, Silent, and Narration Scenes

A video can freely mix three kinds of scene. Narration is the mature default path and is
untouched — the other two modes are additive and opt-in per scene: a scene with a
`narration` string and no `mode` renders through exactly the classic path.

| Mode | Visual | Audio | Length source | Engine |
|---|---|---|---|---|
| **narration** (default) | LTX cinematic clip | narrator voice-over (OpenF5) | narration audio | FLUX + LTX |
| **silent** | LTX cinematic clip | synthesized silence | explicit `duration` | FLUX + LTX |
| **dialogue** | talking-head shot(s) per line | each speaker's cloned voice | per-line audio length | FLUX still + OpenF5 + **EchoMimic-V3** |

## Scene model

`Scene` (`pipeline/llm.py`) carries `mode` (`"narration" | "silent" | "dialogue"`),
`lines` (ordered shot dicts), and `duration` (silent scenes). These persist through the
script.json snapshot and the durable scene records' metadata — no DB migration.
`_norm_scene_lines` validates lines; a dialogue scene with no usable lines degrades to
narration.

A dialogue scene's `lines` are **shots**: speaking shots `{speaker, text, shot}` and
silent action shots `{silent: true, video_prompt, duration}`. "To camera" vs "to each
other" is just who speaks per line — multi-speaker conversations render shot/reverse-shot
(EchoMimic is single-speaker per frame).

## How a dialogue scene renders

`render_dialogue_scene` (`pipeline/dialogue_render.py`), driven from
`resume_generation.py` which splits dialogue scenes from classic scenes and renders them
concurrently, one per EchoMimic worker:

1. **Establishing beat** — the scene opens on its wide first frame with a real LTX
   first→last-keyframe camera push-in toward the first speaker's close-up
   (`generate_keyframed_clip`; gated by `dialogue_establishing_seconds`, default 2.5 s;
   skipped when there's no distinct in-scene close-up to push toward).
2. Per speaking shot: **voice** — OpenF5 synthesizes the line in the speaker's cloned
   voice (`voice_ref_for` resolves character → voice, falling back to the style
   narrator); **still** — a solo close-up of the speaker framed by the line's `shot`
   hint, generated at job resolution in the job's visual style
   (`generate_dialogue_shot_stills` in `app.py`; a repeat speaker reuses their first
   close-up; falls back to the scene frame, then the character portrait); **talk** —
   EchoMimic(still, audio) → lip-synced clip, length driven by the audio.
3. Silent shots render as LTX i2v motion clips from their still (no TTS/EchoMimic).
4. Line clips are concatenated (re-encoded to uniform fps/audio) into
   `scene_NN_final.mp4`, which carries the speech audio.

Downstream assembly is unchanged: `concatenate_scenes` + `mix_background_music` treat a
dialogue scene's mp4 like any other. Big physical action while speaking is out of scope
for EchoMimic — action goes to LTX beats, talking to EchoMimic.

**Silent scenes** stay on the classic path entirely: normal LTX scene video, but the
audio is a synthesized silent WAV of `duration` seconds (`_write_silence_wav`, default
5 s), so mux/concat run unchanged with no voice-over.

## The EchoMimic worker

Mirrors the ComfyUI/TTS worker pattern:

- **Server**: `pipeline/echomimic_server.py` — FastAPI wrapping EchoMimic-V3 flash
  inference (weights: Wan2.1-Fun-1.3B + chinese-wav2vec2 + EchoMimicV3, ~27 GB).
  Endpoints `/health`, `/prewarm`, `/animate`; a single render lock per worker.
- **Container**: `docker/echomimic/Dockerfile`, `docker-compose` service `echomimic`,
  port 8190, weights volume.
- **Client**: `pipeline/echomimic.py` — `animate(image, audio, prompt, host)`,
  `frames_for_audio`, `worker_alive`. Config key `echomimic_workers` (list of
  `http://host:8190` URLs) in the single YAML; editable in Settings → Infra.
- Dialogue lines are durable tasks with worker kind `echomimic`, so they show in
  Progress with their own ETA class. EchoMimic is slow (~minutes per few-second line) —
  acceptable in the async queue.

## LLM-written dialogue

The Create screen has a **format** selector: `narration` (default) | `dialogue` |
`mixed`. When dialogue/mixed, `_build_dialogue_note` (`webapp/backend/main.py`) +
`dialogue_schema` (`pipeline/llm.py`) instruct the LLM to emit scenes with `mode` and
`lines`, casting speakers from the script's characters (see `docs/characters.md`).
Supported on the **Claude, Grok, and OpenAI backends only** — the local vLLM backend
raises if a dialogue note is set.

Dialogue is also fully usable without the LLM: the editor's scene controls
(`SceneTypeControls` in `webapp/frontend/src/components.jsx`, used by both Script.jsx
and EditFilm.jsx) provide the mode selector and a shot-sequence editor (speaker
dropdown, text, add speaking/silent shot, reorder).

## Not implemented

- **Speaker-labeled captions** — `pipeline/captions.py` emits cues for narration scenes
  only; dialogue/silent scenes just advance the caption timeline.
- **Pluggable talking-head engine** — EchoMimic is hard-wired; there is no engine
  registry or alternative (Hallo3 was researched but never built).
- **Local-backend dialogue generation** — narration format only.
- **Multi-speaker in one frame** — shot/reverse-shot covers conversations.
