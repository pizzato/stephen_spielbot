# Dialogue / Performance Videos — Feature Plan

> **Goal.** Let a video mix three kinds of scene freely: today's **narration** scenes
> (unchanged), **silent / action** scenes (visuals, no voice-over), and **dialogue /
> performance** scenes where a character *speaks* — to the camera or to another
> character — with lip-synced video in a **consistent cloned voice**.
>
> **Engine decision (validated on the GB10 fleet, 2026-07-11):** the talking parts are
> produced by **EchoMimic-V3 (1.3B)** — portrait + audio → lip-synced talking video —
> driven by our existing **OpenF5** cloned voices. InfiniteTalk-14B was ruled out
> (memory ceiling, rebooted the host); EchoMimic ran cleanly with 2 patches and no
> memory wall. See [`project_infinitetalk_gb10_spike`](../).

---

## 0. The hard invariant: narration is not touched

Narration is the mature, liked path and must keep working **byte-identically**. Every
change below is **additive and opt-in per scene**:

- A scene with a `narration` string and no `mode` (i.e. every existing scene and every
  script.json on disk) renders through **exactly today's code path** — FLUX first frame →
  LTX I2V → mux narration → concat → music. No new code runs for it.
- The new modes are reached only when a scene explicitly opts in (`mode: "silent"` or
  `mode: "dialogue"`), or a style enables dialogue generation.
- **Acceptance gate for every phase:** an existing narration-only script re-renders to a
  bit-identical final video, and a style with no dialogue characters behaves as today.

---

## 1. The three scene modes

| Mode | Visual | Audio | Length source | Engine |
|---|---|---|---|---|
| **narration** (default, today) | LTX cinematic clip | narrator voice-over (F5/OpenF5) | narration audio | FLUX + LTX (unchanged) |
| **silent / action** | LTX cinematic clip | none (or low ambient) | explicit `duration` | FLUX + LTX |
| **dialogue / performance** | talking-head shot(s) of the speaker | that character's cloned voice | the spoken line's audio | FLUX still + OpenF5 + **EchoMimic** |

**"To camera" vs "to each other"** are the *same* dialogue mode — the difference is just
who speaks and the framing prompt:
- **To camera** = a scene whose `lines` are one character addressing the viewer.
- **To each other** = a scene whose `lines` alternate speakers; each line becomes its own
  single-speaker talking-head shot, cut together **shot / reverse-shot** (the natural way
  film dialogue is shot, and what a single-speaker model like EchoMimic-flash fits).

---

## 2. How a dialogue scene renders (the new path)

A dialogue scene holds an ordered list of **lines**. Each line renders independently and
the line-clips are concatenated into the scene's final mp4:

```
for each line { speaker, text } in scene.lines:
  1. STILL   — FLUX generates a talking-framed still of `speaker`
               (reuses scene-image generation + the character's reference image
                for identity — pipeline already does this via _scene_reference_images)
  2. VOICE   — OpenF5 synthesizes `text` in speaker's cloned voice
               (reuses generate_narration(reference_wav = speaker's voice clip))
  3. TALK    — EchoMimic(still, voice_audio) → lip-synced clip for this line
  4. clip length = the voice audio length (drives EchoMimic --video_length)
concat line-clips (hard cuts) → scene_NN_final.mp4   (carries the speech audio)
```

**Why this slots in cleanly:** downstream assembly (`concatenate_scenes` + `mix_background_music`)
is already scene-agnostic — it just concatenates each scene's final mp4 and its audio
track, then mixes music. A dialogue scene's final mp4 simply *is* the EchoMimic clip(s)
with the speech baked in. **The muxer, scene concat, music mix, publish path, and captions
timeline model are unchanged in shape.**

**Scope honesty:** EchoMimic animates head/face + subtle motion on a mostly-static frame.
Big physical *action while speaking* is not its strength. So: **action → narration/LTX**,
**talking → EchoMimic**. A scene needing both is split (an LTX action beat + an EchoMimic
line), not forced into one shot.

---

## 3. Data model (additive, back-compat)

Extend the scene object; **all new fields optional, absent = narration**:

```jsonc
{
  "id": 4, "title": "...",
  "image_prompt": "...", "video_prompt": "...",   // still used by narration/silent
  "narration": "...",                              // present ⟹ narration mode (unchanged)
  "mode": "dialogue",                              // NEW: "narration"(default) | "silent" | "dialogue"
  "duration": 6.0,                                 // NEW: required for silent
  "lines": [                                       // NEW: dialogue only
    { "speaker": "Kinho",   "text": "We can't stay here." },
    { "speaker": "Attenbot","text": "Then we run." }
  ]
}
```

- `speaker` is a character name/alias (→ that character's voice + reference image) or the
  reserved `"Narrator"`.
- **Inference rule (no migration needed):** `mode` absent + `narration` non-empty →
  narration. This keeps every existing `script.json` valid untouched.
- Storage: add to the `Scene` dataclass (`pipeline/llm.py`), the `script.json` snapshot,
  and `_row_to_scene`; the scenes table's free-form `metadata_json` can carry `lines`/
  `duration`/`mode` with **no DB migration**.

**Per-character voice** (the one field the characters feature deferred): add `voice` to the
character dict (`_norm_characters` in `app.py`), pointing at the existing named-voice store.
Speaker → voice: `character.voice` → `voice_path_for()` → `reference_wav` for OpenF5.

---

## 4. New infrastructure: the EchoMimic worker

Productionize the validated spike into a fleet worker, mirroring the ComfyUI/TTS pattern:

- **Container** `spielbot-echomimic` on the GB10 workers: bakes in the repo + the 2 patches
  (decord stub, pyloudnorm) + the filtered deps (drop tensorflow/retina-face) + the weights
  (Wan2.1-Fun-1.3B + wav2vec + EchoMimic flash transformer, ~27 GB) + an HTTP server
  `POST /animate {image_b64, audio_b64, prompt, steps, size} → mp4 bytes` (mirrors
  `pipeline/tts_server.py`).
- **Config**: `echomimic_workers: [http://s1:8190, ...]` in the single YAML, derived by
  `make install` like `tts_workers`. Health check like the TTS worker.
- **Client**: `pipeline/echomimic.py` — `animate(image, audio, prompt, host) -> Path`,
  mirroring `pipeline/tts_worker.py`'s HTTP transport.
- Deploy = a new `docker/echomimic/` build + `scripts/install_worker_container.sh` hook +
  `make` targets, same as the existing workers.

*Speed reality:* ~9 min per ~3 s window at 8 steps / 768². Dialogue lines are short (2–5 s),
so a line ≈ one window. Levers: fewer steps, lower `--sample_size`, TeaCache, and the fleet
already renders scenes in parallel across workers. Dialogue scenes are simply slower than
narration scenes — acceptable in the async queue, worth surfacing in the ETA model
(`pipeline/timing.py` gets an `echomimic` task kind).

---

## 5. Supporting changes

- **LLM (gated, optional, Phase 2)** — a per-style/script **format**: `narration` (today)
  vs `dialogue`/`mixed`. When dialogue, the LLM emits scenes with `mode` + `lines` (speakers
  from the defined cast) + framing. Threads through **all 3 backends** (Claude+Grok JSON,
  local plain-text) and **both parsers**. Narration-style styles are never affected.
  **Dialogue is fully usable *before* this** via manual authoring in the editor.
- **Timing** — dialogue line-clip length = its OpenF5 audio length → EchoMimic `--video_length`
  = round(secs × fps). Scene length = sum of line clips. Silent = explicit `duration`.
  Narration timing unchanged.
- **Assembly** — unchanged shape. New helper concatenates a dialogue scene's line-clips into
  `scene_NN_final.mp4`; then the existing `concatenate_scenes` + `mix_background_music` run
  as-is. Loudness-normalize EchoMimic audio to sit with narration/music.
- **Captions** (`pipeline/captions.py`) — dialogue scenes emit speaker-labeled cues
  ("Kinho: …") from per-line text + measured per-line durations; narration scenes unchanged.
- **Editor UI** (`webapp/frontend/src/screens/Script.jsx`) — per-scene **mode selector**
  (Narration / Silent / Dialogue); Dialogue reveals a **lines editor** (speaker dropdown
  bound to the script's cast + text + add/remove/reorder); narration scenes look and behave
  exactly as today. Characters tab gets a **voice dropdown** per character (reuses the
  existing voice picker + "test voice").
- **Both orchestration paths** — `resume_generation.py` (monolithic) **and**
  `worker_agent.py` + the SQLite orchestrator plan get the mode branch, same discipline as
  the dual-backend rule. Add `scene.dialogue.render` task kind(s) to the plan.

---

## 6. Phased delivery

- **P0 — EchoMimic worker (infra).** Container + HTTP server + config + `pipeline/echomimic.py`
  client + a CLI smoke that reproduces the two proof clips. No app/data-model change yet.
- **P1 — Dialogue end-to-end, manually authored.** Scene `mode`/`lines`/`duration` (additive,
  back-compat) + per-character `voice` + the dialogue render path (FLUX still → OpenF5 →
  EchoMimic → concat) in **both** orchestration paths + editor mode/lines UI + captions.
  Also `silent` mode (LTX visuals, duration-driven). **Delivers dialogue + no-narration +
  mixed videos**, driven by defining a cast and typing/importing lines — no LLM change needed.
- **P2 — LLM writes the dialogue.** Format selector + dialogue prompts across all 3 backends,
  so a topic can produce a dialogue/mixed script.
- **P3 — Polish.** Speed tuning (steps/res/TeaCache), ETA integration, transitions between
  talking-head and LTX shots, optional per-line LTX establishing beats.

**Ship order rationale:** P0 is pure infra (safe). P1 is additive and guarded by the
invariant. Nothing in P0–P1 can change a narration render.

---

## 7. Files to touch (grounded in the current code)

- `docker/echomimic/*`, `scripts/install_worker_container.sh`, `Makefile`, config
  (`echomimic_workers`) — P0 worker.
- `pipeline/echomimic.py` (new client), `pipeline/tts_server.py` (pattern reference) — P0.
- `pipeline/llm.py` (`Scene` dataclass + parsers), `script.json` snapshot,
  `pipeline/orchestrator.py` (`_row_to_scene`, plan/task kinds) — P1 data model.
- `app.py` (`_norm_characters` + `voice`; scene mode helpers; `_characters_for_scene` /
  `_scene_reference_images` already give per-speaker stills) — P1.
- `resume_generation.py` **and** `worker_agent.py` / `pipeline/scene_video.py` — P1 render
  branch (dialogue path) + `silent` duration path.
- `pipeline/assembler.py` (new line-clip concat helper; loudnorm), `pipeline/captions.py` — P1.
- `webapp/frontend/src/screens/Script.jsx`, `Settings.jsx` (character voice) — P1 UI.
- `prompts.yaml` + `pipeline/llm.py` (dialogue prompt variants, 3 backends) — P2.
- `pipeline/timing.py` (echomimic task kind / ETA) — P3.

---

## 8. Decisions / open questions

1. **Dialogue-scene visual**: FLUX still of the character in a setting (recommended — reuses
   scene-image gen + character reference conditioning, so talking happens "in scene"), vs a
   bare uploaded portrait. Default: FLUX still.
2. **Speech in a field vs the prompt**: dialogue text lives in the structured `lines`
   (recommended — needed for TTS + captions + editing), the EchoMimic `--prompt` gets only
   framing/appearance.
3. **Multi-speaker in one frame** (both faces talking together): out of scope for v1 —
   EchoMimic-flash is single-speaker, and shot/reverse-shot covers the need. (MultiTalk could
   do it but is 14B / not viable on the GB10.)
4. **Speed**: acceptable at 8 steps/768 for the async queue, or tune first? (Phase-3 lever.)

---

## 9. Rollout notes
- Deploy each phase = `make web-build` + `make restart-server` for app changes; worker
  changes = re-deploy the `echomimic` container (like comfy/tts).
- Land on the existing `claude/character-dialogue-videos-aa3307` branch; PR per phase; keep
  the narration-invariant acceptance test green in each PR.
