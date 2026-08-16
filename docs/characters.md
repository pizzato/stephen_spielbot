# Characters

Named characters keep a consistent look (and voice) across scenes and videos. Two
mechanisms work together:

1. **Text consistency** — every character has a canonical appearance description that is
   fed to the script LLM ([when the brief asks for them](#casting-is-opt-in-by-name))
   and deterministically re-injected into image prompts.
2. **Visual consistency** — a character can carry a reference image; scenes featuring
   that character are generated with FLUX.2 reference conditioning (`ReferenceLatent`),
   so the output resembles the reference. FLUX.2-only; other engines ignore references.

The video side needs no changes: LTX renders from the scene's first-frame still, so once
the still is consistent, the motion clip inherits it.

## One library, scoped by style

Characters live in a single top-level `cfg["characters"]` list; each entry carries a
`style` scope that says who inherits it:

- `style: ""` — the **global pool**: every style inherits the character automatically
  (inheriting a character makes it *available* to that style — it does not cast it into
  every film; see [Casting is opt-in, by name](#casting-is-opt-in-by-name)).
- `style: "<name>"` — owned by that style: visible to it **and every style under it**
  in the [style hierarchy](configuration.md) (children inherit the parent's cast;
  siblings and unrelated styles never see it).

`_style_characters(cfg, style_name)` resolves a style's effective cast (global pool +
its lineage, library order; the `(none)` experiment style gets nothing). The Settings →
**Characters** tab has a scope picker mirroring the Styles tab's tree — a **Global**
pill first, then the style hierarchy, each pill showing how many characters live there;
picking a scope lists the characters homed in it, and each card has a **Belongs to**
picker to move it. Reference images stay visible while the form has unsaved edits; only
the operations that persist immediately (upload / portrait / version picks) wait for a
clean form. The style cards under **Styles** show a read-only summary of the cast the
selected style inherits.

`_ensure_characters()` in `app.py` normalizes the library and performs the one-time
migrations: v2 hoisted the old per-style lists into the shared library; v3
(`characters_scoped_v3`) replaced the old opt-in fields (`character_ids` /
`auto_accept_characters`) with scopes — a character listed by exactly one style became
that style's, one named exactly like a style (narrator personas) went to that style,
everything else stayed global. A scope naming a deleted style is kept (dormant) until
the style returns or the user re-homes the character; renaming a style re-points its
characters, deleting one re-homes them to its parent (or the global pool).

### The character object

```jsonc
{
  "id": "char_a1b2c3",           // stable id; survives renames
  "name": "Bob John",            // display + primary match token
  "aliases": ["Bob", "Mr. John"],// extra match tokens (case-insensitive, word-boundary)
  "description": "a middle-aged man, short grey beard, round glasses, navy wool coat",
  "ref_image": "char_a1b2c3.png",// filename under the characters dir; "" if text-only
  "voice": "...",                // named voice for acted scenes (see docs/performance_films.md)
  "enabled": true,
  "style": "Children Story"      // "" = global pool, else the owning style
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
  portrait** from the description (re-roll until happy, versions kept). Portraits are
  anchored to the **owning style's** image engine + visual look — global-pool
  characters fall back to the default style's. Endpoints:
  `/api/characters/image`, `.../image/clear`, `.../image/select`,
  `/api/characters/portrait`.

## Casting is opt-in, by name

A style's catalogue is what a film *may* use, not what it *will* use. Script generation
only tells the LLM about a catalogue character when the brief names them — their name or
any alias appearing in the **topic/description**, the **video title**, or the style's
**extra instructions** (`_requested_characters` in `app.py`, applied to all three script
stages: story draft, divide, and redraft). Otherwise no character sheet is sent and the
story invents its own cast, which becomes [per-script characters](#per-script-characters).

- Ask for one by name — "A day in the life of Bob John" — and the sheet is sent, so the
  scenes refer to them by name and pick up their canonical look and voice.
- Want a character in *every* film of a style (a host, a mascot)? Name them in that
  style's **extra instructions**.
- Nothing downstream depends on the sheet: `_characters_for_scene` matches by name, so a
  character the story does name gets their look and voice either way.

This matters most for [performance films](performance_films.md), where the cast doubles
as the speaker roster: handing over the whole library put library characters into every
acted, silent or music-video script.

## Per-script characters

Besides the catalogue, script generation identifies up to 2 recurring **main**
characters (`_MAX_MAIN_CHARACTERS` in `pipeline/llm.py`), plus a follow-up pass for
recurring supporting characters (`_detect_recurring_characters`, cap 8). Identified
characters that already exist in the style's catalogue are dropped, so only genuinely
new cast become per-script.

- Stored in the work dir: `characters.json` sidecar + `characters/` images
  (`_read_script_characters` / `_write_script_characters` in `app.py`).
- The editor's **Characters** tab (`Script.jsx`) offers CRUD, voice picker, look
  generation/upload, and **Promote to catalogue** — `promote_script_character()` copies
  the character (and its look image) into the library scoped to the job's style, so
  that style and its children reuse it. One-way; the per-script copy stays.
- At render time `_job_characters()` merges the cast the style inherits (global pool +
  lineage) with the script's own; a per-script character shadows a catalogue one with
  the same name.

## Deploy notes

- FLUX.2 on the GB10 workers needs `TORCH_BLAS_PREFER_CUBLASLT=1` (existing gotcha); the
  reference workflow inherits it.
- Backend restart required after `_ensure_styles`/`_ensure_characters` changes
  (`make web-build` + `make restart-server`).
