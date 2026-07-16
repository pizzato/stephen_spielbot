# Characters

Named characters keep a consistent look (and voice) across scenes and videos. Two
mechanisms work together:

1. **Text consistency** — every character has a canonical appearance description that is
   fed to the script LLM and deterministically re-injected into image prompts.
2. **Visual consistency** — a character can carry a reference image; scenes featuring
   that character are generated with FLUX.2 reference conditioning (`ReferenceLatent`),
   so the output resembles the reference. FLUX.2-only; other engines ignore references.

The video side needs no changes: LTX renders from the scene's first-frame still, so once
the still is consistent, the motion clip inherits it.

## Global library + per-style opt-in

Characters live in a single top-level `cfg["characters"]` list (not per style).
`_ensure_characters()` in `app.py` normalizes the library and performs the one-time
migration from the old per-style lists. Each style holds:

- `character_ids` — the library characters it uses (checkboxes in Settings), or
- `auto_accept_characters` — a toggle that opts the style into *every* library character,
  including ones added later.

`_style_characters(cfg, style_name)` resolves a style's effective cast. The Settings →
**Characters** tab manages the library (CRUD, portraits); each style card has the opt-in
checkboxes.

### The character object

```jsonc
{
  "id": "char_a1b2c3",           // stable id; survives renames
  "name": "Bob John",            // display + primary match token
  "aliases": ["Bob", "Mr. John"],// extra match tokens (case-insensitive, word-boundary)
  "description": "a middle-aged man, short grey beard, round glasses, navy wool coat",
  "ref_image": "char_a1b2c3.png",// filename under the characters dir; "" if text-only
  "voice": "...",                // named voice for dialogue scenes (see docs/dialogue_scenes.md)
  "enabled": true
}
```

`description` is an appearance clause with no name inside it, so it can be slotted into a
sentence. A `ref_strength` field is stored and normalized but **not consumed anywhere**
(the `ReferenceLatent` node has no strength input) — it is a forward-compatible
placeholder with no UI.

### Storage

- Global reference images: `~/.config/video-generator/characters/{char_id}.png`
  (`_characters_dir()` in `app.py`). Portrait re-rolls keep version history under the
  config root's `image_history/`; `select_character_image` picks among kept versions.
- Per-script characters (below): `<work_dir>/characters.json` + `<work_dir>/characters/`.

## Text consistency path

- `_character_sheet(characters)` (`app.py`) builds the "RECURRING CHARACTERS" block; the
  script-generate handler in `webapp/backend/main.py` passes it into `generate_script`
  as `character_sheet`.
- All LLM backends fill a `${character_note}` placeholder — prompt keys
  `script_claude_initial`, `script_claude_continuation`, `script_local_story`,
  `script_local_visual` in `prompts.yaml`.
- **Deterministic re-injection** (the reliability net against LLM paraphrasing):
  `_inject_characters()` in `app.py` appends `"{name}: {description}."` to a scene's
  image prompt whenever the scene mentions the character's name/alias and the canonical
  description isn't already present. Matching: `_character_mentions()`; scene cast
  selection: `_characters_for_scene()`. Called from `_generate_active_scene_preview` and
  the dialogue shot-still path.

## Reference-image path (FLUX.2)

- `workflows/flux2_t2i_ref.json` is the single-reference graph (`LoadImage` →
  `VAEEncode` → `ReferenceLatent` feeding the guider). Multi-reference scenes use the
  dynamic builder `_build_flux2_ref_workflow()` in `pipeline/comfyui.py`, which appends
  one LoadImage/VAEEncode/ReferenceLatent chain per extra reference.
- `generate_with_engine(..., reference_images=[...])` uploads each reference and runs the
  ref workflow on FLUX.2; non-FLUX.2 engines ignore references (logged, no crash).
- `_scene_reference_images()` in `app.py` maps a scene's matched characters to their
  reference files, capped at `_MAX_SCENE_REFERENCES = 2` per scene (drops are logged).
- Getting a reference image, per character: **upload** a photo, or **generate a
  portrait** from the description (re-roll until happy, versions kept). Endpoints:
  `/api/characters/image`, `.../image/clear`, `.../image/select`,
  `/api/characters/portrait`.

## Per-script characters

Besides the global library, script generation identifies up to 2 recurring **main**
characters (`_MAX_MAIN_CHARACTERS` in `pipeline/llm.py`), plus a follow-up pass for
recurring supporting characters (`_detect_recurring_characters`, cap 8). Identified
characters that already exist in the style's catalogue are dropped, so only genuinely
new cast become per-script.

- Stored in the work dir: `characters.json` sidecar + `characters/` images
  (`_read_script_characters` / `_write_script_characters` in `app.py`).
- The editor's **Characters** tab (`Script.jsx`) offers CRUD, voice picker, look
  generation/upload, and **Promote to catalogue** — `promote_script_character()` copies
  the character (and its look image) into the global library and opts the current style
  in. One-way; the per-script copy stays.
- At render time `_job_characters()` merges the style's global cast with the script's
  own; a per-script character shadows a global one with the same name.

## Deploy notes

- FLUX.2 on the GB10 workers needs `TORCH_BLAS_PREFER_CUBLASLT=1` (existing gotcha); the
  reference workflow inherits it.
- Backend restart required after `_ensure_styles`/`_ensure_characters` changes
  (`make web-build` + `make restart-server`).
