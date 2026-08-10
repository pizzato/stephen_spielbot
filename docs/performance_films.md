# Acted Scenes & Performance Films

An **acted scene** is one where the characters speak on screen: **MiniMax H3 Ref2VA
generates the picture and the speech in a single pass** from the characters' portraits.
There is no first frame, no TTS step, and nothing is lip-synced onto a still — the
performance and the voice are made together.

A **performance film** is a film made entirely of them: no narrator, no music. But acted
scenes are not confined to that — a *Mixed* film puts them alongside narrated and silent
scenes, and each scene takes the path its mode asks for.

| | Narrated scene | Acted scene |
|---|---|---|
| Script | image + video prompts + narration | cast, timed beats, quoted dialogue, soundscape |
| First frame | image engine (FLUX) | none |
| Video | LTX 2.3 or H3 I2V | **H3 Ref2VA** from character portraits |
| Voice | TTS (OpenF5 / Chatterbox) | generated with the picture, cast from the voice library |
| Length | the narration audio | what the dialogue needs, ~10 s |

## Turning it on

Choose the **Dialogue** format in [Create](manual/create.md) for a film that is acted all
the way through, or **Mixed** to let the writer place acted scenes among narrated and silent
ones. A single scene can also be switched to *dialogue* in the
[Script editor](manual/script.md) — any scene with dialogue lines is acted, wherever it sits.

The script is written story-first either way: the prose story comes first, and the division
into scenes is what stages it as performance. The video model is picked per style
(Settings → a style → **Video models → Acted (dialogue) scenes**; see below).

A scene written as dialogue in a mixed script only carries its lines — the cast comes from
who speaks, the length from what they say, and the setting from the scene's own video
prompt.

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

## Editing an acted scene

An acted scene is written through its fields — who is **on screen** (Picture 1, Picture 2…),
the **setting**, the **dialogue** with per-line delivery, timed **action** beats, **camera**
and **sound** — in the [Script editor](manual/script.md) like any other scene. The *Acted
scenes* view adds the resolved references: the portrait that IS `<Picture 1>`, the voice
clip that IS `<Audio 1>`.

**Scenery and wardrobe** reference images — the `<Picture N>` slots beyond the portraits —
live with the characters, under **Characters & visuals**, on both the Script screen and the
film's edit screen. A film's own visuals shadow same-named [assets](manual/settings.md)
from the style's shared catalogue; both feed the prompt the moment they have an image.

The **video prompt is read-only and assembled from those fields**, so nothing is written
twice. **Edit prompt** pins hand-written text instead: the fields stop rebuilding it, and
the render sends exactly what is on screen. **Rebuild from the fields** drops the override
again.

**Re-generate scene** rewrites the whole take with the LLM — dialogue, action, setting,
camera, sound — keeping the film's context and cast, optionally steered by a free-text
instruction. An acted scene is one coherent take, so it regenerates whole rather than
field by field; a pinned prompt is superseded by the rewrite.

Editing a scene of a film that has already rendered keeps the existing clip — it is the
deliverable — and offers **Shoot this scene again** to re-render just that scene.

## Rendering

Each scene is a single Ref2VA generation, run across the ComfyUI workers in parallel
(`resume_generation.py`). An acted scene plans one task — no image, narration or mux task
— and the narrated scenes in the same film plan their usual quartet. Assembly concatenates
everything, keeping each clip's own audio.

**Music** is a final-mix ingredient, never baked into a scene. Switch it off per style
(Settings → *Narrator & audio* → **Music**) or per film (Create → **Music**), and the
final cut is the concatenation itself. An all-acted film never plans a score at all.

**One production, one look.** A mixed film's narrated scenes render on **H3 I2V** rather
than the style's usual video engine — H3 acted takes cut against LTX clips read as two
different productions, with colour and motion shifting shot to shot. A style already on a
MiniMax engine keeps its own pick; unmixed films are untouched.

### Video models

A style carries **two video model pickers**, side by side under *Video models*, because a
film can hold two kinds of scene:

- **Narrated & silent scenes** — the I2V engine (LTX 2.3 or MiniMax H3) that animates each
  scene from its first-frame still.
- **Acted (dialogue) scenes** — the Ref2VA engine that performs each acted scene from
  portraits and voices. Always a MiniMax H3 variant:

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

**Sampling steps** is ONE knob beneath both pickers: it overrides the step count of every
MiniMax H3 render in the style — narrated-scene I2V and acted-scene Ref2VA alike. 0 keeps
each engine's own default (Turbo 4, the others 15); LTX ignores it.

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
- **Acted scenes are not captioned.** There is no TTS step to measure, so the caption
  track covers the narrated scenes only.
- English is what this has been exercised on; other languages are untested.
