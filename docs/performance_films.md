# Performance Films

A performance film is a second kind of video, not a variation on the narrated one. The
characters act and speak on screen, and **MiniMax H3 Ref2VA generates the picture and the
speech in a single pass** from the characters' portraits. There is no narrator, no first
frame, no TTS step and no background music.

Narrated films are untouched by all of this. The two pathways share only the queue, the
work dir, and publishing.

| | Narrated film | Performance film |
|---|---|---|
| Script | image + video prompts + narration | cast, timed beats, quoted dialogue, soundscape |
| First frame | image engine (FLUX) per scene | none |
| Video | LTX 2.3 or H3 I2V | **H3 Ref2VA** from character portraits |
| Voice | TTS (OpenF5 / Chatterbox) | generated with the picture, cast from the voice library |
| Music | ACE-Step score, mixed in | none |
| Scene length | narration audio | one acted clip, ~10 s |

## Turning it on

Settings → a style → **Script & content** → *Script mode* → **Performance**. A second
picker appears for the video model (below). In [Create](manual/create.md) the mode can
also be chosen per film when you're on *No style*.

Performance ignores the Narration/Dialogue/Mixed format switch — an acted film is already
dialogue.

## What you need first

**Characters with portraits.** The portraits *are* the conditioning: each cast member's
reference image becomes a `<Picture N>` reference for the scenes they appear in. A scene
whose cast resolves to no portrait at all fails rather than inventing a look — see
[characters](characters.md) for the catalogue and the per-script cast.

**Voices.** Each character's cast voice (assigned automatically at script creation, from
the same library the narrator uses) is passed as an `<Audio N>` reference so the character
sounds the same in every scene. A character with no voice still speaks — the model simply
invents one, and it will drift between scenes.

## The script

The LLM writes a different shape (`pipeline/performance.py`): per scene a `setting`, the
`cast` present, `beats` with timings, `lines` of `{speaker, delivery, text}`, a `camera`
line, and a diegetic `soundscape`. `build_h3_prompt` assembles those into the six-block
prompt the model responds to — reference roles, style, timed beats with the dialogue
quoted verbatim, camera, audio, then a refusal list.

The assembly is deterministic rather than LLM prose, because two of those blocks are not
optional: every reference must be given an explicit job or the model blends them, and the
"do not add subtitles" refusals must be present on every scene or H3 burns subtitles into
the picture.

The assembled prompt is stored in each scene's **video prompt**, so the
[Script editor](manual/script.md) shows and edits exactly what the model receives. The
structured fields stay in the scene metadata, and the renderer rebuilds the prompt from
them so the reference numbering always matches the references actually wired up.

Keep spoken lines short: roughly 2.5 words per second of clip, so about 25 words in a
10-second scene. Over that and the model cuts the line off.

## Rendering

Each scene is a single Ref2VA generation, one per ComfyUI worker in parallel
(`resume_generation.py`). No image task, no narration task, no mux and no music task are
planned at all — a performance film's task list is one task per scene plus assembly.
Assembly is a straight concat that keeps each clip's own audio.

### Video model

| Engine | Speed | Notes |
|---|---|---|
| `minimax-h3-ref-w4a8` (default) | ~6.6 min per 10 s scene | 4-bit weights, **15 real steps** — turbo's speed without the distillation look. Needs ComfyUI ≥ 0.31.0 |
| `minimax-h3-ref-turbo` | ~6 min | Distilled 4-step LoRA on the full 34 GB checkpoint — fastest, but over-saturated and over-sharpened |
| `minimax-h3-ref` | ~23 min | 15 steps + EasyCache on the 21 GB pruned checkpoint — the fidelity reference |

Measured on a DGX Spark GB10, same shot and seed throughout (704×1280): edge
energy 4.2 for w4a8 against turbo's 5.5 (and 6.1 at 8 steps), with the base
engine at 4.4 — the "over-sharpened" look is the distillation, not the step
count, and w4a8 avoids it at turbo's wall clock. Download them from
Settings → Infrastructure like any other engine; see [models](models.md).

!!! warning "w4a8 needs ComfyUI ≥ 0.31.0 on every worker"
    Below that version the checkpoint does not error — it renders **black
    frames**. The render refuses up front rather than shipping them, but a
    mixed fleet still means some workers cannot take the job at all. Rebuild
    all of them together (`COMFYUI_REF`), exactly like SageAttention.

!!! warning "Licence"
    MiniMax H3 is **not licensed for use in the USA, EU, UK or South Korea**, and requires
    machine-generated disclosure and "MiniMax H3" attribution. The picker repeats this.

## Consistency

Consistency comes from three mechanisms, each born from a measured failure:

- **One scene, one generation.** A whole conversation renders as a single
  continuous clip — both speakers in frame, placed left/right with locked
  positions, every line in order. Identity is protected by the prompt's
  identity locks and verified by the gate; be aware the model *can* still swap
  two same-kind people in one clip. For content where identity outranks flow,
  `performance_shot_split: true` renders shot/reverse-shot instead — one face
  and one voice per clip (structurally swap-proof), with a silent wide opening
  each scene (`performance_establishing`).
- **A reference budget.** Measured directly: at three picture references
  everything held (face, outfit colour, location); at four the weakest dropped.
  The renderer enforces it — later shots swap the scene's own first frame in
  for the location asset, and the wide drops wardrobe (invisible at that
  distance anyway).
- **A quality gate.** Every talking shot is transcribed (faster-whisper, CPU,
  seconds against a ~6-minute render) and scored against its scripted line; a
  miss is retaken with a fresh seed and the better take kept
  (`performance_verify`, `performance_verify_retakes`, default one retake).
  The gate verifies **speech, not picture** — a visually broken shot that says
  its line still passes.

Shots are sized to their words (~2.5 words/second plus a beat of air) rather
than to a share of the scene, because oversized shots left the model padding
the tail with speech nobody scripted. And cast **distinct voices** for
co-stars: two reference voices five hertz apart will bleed into each other,
and no prompt can separate them.

## Limits

- **15 seconds is a hard ceiling** per scene, and cost grows faster than length — scenes
  are written to ~10 s.
- **One voice reference bleeds onto other speakers** in the same clip. Give every speaker
  their own voice (the model accepts 3 per scene), or write scenes with one speaker.
- **Nine portraits and three voices** per scene, maximum.
- **Caption timing is approximate.** There is no TTS step to measure, so a scene's
  captions cover the clip rather than each line.
- English is what this has been exercised on; other languages are untested.
