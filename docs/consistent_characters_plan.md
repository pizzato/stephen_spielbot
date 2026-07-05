# Consistent Characters — Feature Plan

> **Status (implementation).**
> - **Phase 1 (text consistency): DONE** — per-style character registry, character
>   sheet into both LLM backends, deterministic injection at generation, Settings
>   Characters card. Shipped.
> - **Phase 2 (reference images): CODE DONE, PENDING LIVE VALIDATION** — `flux2_t2i_ref.json`
>   (ReferenceLatent), N-reference builder in `generate_with_engine`, scene→character
>   ref-image matching (cap 2), upload/clear/generate-portrait endpoints, Settings
>   image UI. **The one open item is the §5.0 spike:** the DGX workers were unreachable
>   from the dev host, so the `ReferenceLatent` node graph has NOT yet been run on a
>   live worker. Load `workflows/flux2_t2i_ref.json` into a worker's ComfyUI and run it
>   before trusting reference generation; if the node is missing, see §5.6 fallback.


> **Goal.** Let a user define named characters (e.g. "Robot XYZ", "Bob John") that
> keep a **consistent look across every scene and every video**. A character can be:
> - **text-defined** — a fixed appearance description the pipeline reuses verbatim, and/or
> - **image-anchored** — pinned to a reference image (a user upload, or a generated portrait the user approves) so the character actually *looks like that image*.
>
> This document is the single source of truth for the feature. It is written so any
> engineer or LLM can pick it up and implement a phase without re-discovering the codebase.

---

## 1. TL;DR for implementers

Add a per-style **character registry** and make it influence image generation two ways:

1. **Text consistency (Phase 1).** Every character has a canonical appearance sentence. It is
   (a) fed to the script LLM as a "character sheet" so scenes describe characters consistently, and
   (b) **deterministically re-injected** into a scene's image prompt at generation time whenever the
   scene mentions the character's name/alias — so consistency survives LLM paraphrasing.

2. **Visual consistency (Phase 2).** A character can carry a **reference image**. Scenes featuring
   that character are generated with a new **FLUX.2 reference-conditioned workflow**
   (`ReferenceLatent`), so the output resembles the reference. FLUX.2 supports multiple references, so
   two characters can share a scene. This is FLUX.2-only (matches the current default engine).

**Nothing on the video side changes** — LTX renders from the scene's first-frame still, so once the
still is consistent, the motion clip inherits it.

**Why FLUX.2, not LoRA:** FLUX.2 Klein (the current default engine) natively supports reference-image
conditioning via ComfyUI's `ReferenceLatent` node. This gets most of the identity-preservation benefit
of a per-character LoRA with **no training pipeline** and **no per-character model files**.

---

## 2. Current architecture (grounding — verified against the code)

Understanding these touch-points is enough to implement the feature. All paths are relative to repo root.

### 2.1 Config & per-style model
- Per-style fields are declared in `STYLE_FIELD_TO_FLAT` — `app.py:286`. Each per-style field maps to a
  legacy "flat" config key that mirrors the **default** style. The `styles` list is the source of truth.
- Styles are normalized by `_ensure_styles()` — `app.py:384`. This is where new fields get defaults,
  nested structures get coerced (see `_norm_size_presets` at `app.py:345` as the template for a
  non-scalar field), engines get validated, and the default style is mirrored back onto flat keys.
- `style_settings(cfg, name)` — `app.py:599` — returns the resolved settings dict for a style.
- Config lives in ONE YAML: `~/.config/video-generator/config.yaml` (`CONFIG_FILE` at `app.py:70`).

### 2.2 Script/prompt generation (BOTH backends — always patch both)
- Entry point: `generate_script()` — `pipeline/llm.py:551`. Dispatches to Claude or local vLLM by
  `cfg["llm_backend"]`.
- **Claude backend**: `_claude_generate()` — `pipeline/llm.py:155`. Builds `style_note`,
  `video_style_note`, `conclusion_note` (`llm.py:170-182`) and fills the `script_claude_initial`
  template. Continuation batches at `llm.py:219+`.
- **Local backend**: two-stage — `_local_generate_story()` (`llm.py:344`) then per-scene
  `_local_generate_visual()` (`llm.py:447`).
- Prompt templates live in `prompts.yaml` (no code change needed to edit prompt text). Relevant keys:
  `script_claude_initial` (line 16), `script_claude_continuation` (line 47), `script_local_story`
  (line 83), `script_local_visual` (line 113). Placeholders use `${name}` (`string.Template`,
  `safe_substitute`, so unknown placeholders are left intact).
- **The single real caller** that assembles style hints: `_do_script_generate()` —
  `webapp/backend/main.py:633`. It reads `style_settings`, appends `extra_instructions` to the topic
  (`main.py:645-647`), and passes `style_hint` / `video_style_hint` into `generate_script`
  (`main.py:654`). **This is where the character sheet gets assembled and passed in.**

### 2.3 Image generation
- `_generate_active_scene_preview()` — `app.py:1454` — is where a scene's still is produced. It:
  - resolves the style's engine (`engines.resolve`, `app.py:1500`),
  - composes the visual style prefix (`_compose_visual_style`, `app.py:1436`/called `:1508`),
  - builds the final prompt `f"{combined_style}. {base_prompt}"` (`app.py:1510`),
  - calls `generate_with_engine(...)` (`app.py:1514`).
- **This is the deterministic character-injection point (Phase 1) and the reference-image branch point (Phase 2).**
- `generate_with_engine()` — `pipeline/comfyui.py:786`. For FLUX.2 it fills `flux2_t2i.json` and calls
  `_run_and_save`. For FLUX.1 it delegates to the legacy path.
- `edit_with_engine()` — `pipeline/comfyui.py:812`. FLUX.2 branch uploads base+mask via `_upload_image`
  (`comfyui.py:488`) and fills `flux2_edit.json`.
- Workflow templating: `_load_workflow` (`comfyui.py:94`) + `_fill_template` (`comfyui.py:99`) replace
  `{{KEY}}` placeholders in the raw JSON, then `json.loads`. `_run_and_save` (`comfyui.py:756`) runs it.

### 2.4 Engine registry
- `pipeline/engines.py`. Each engine bundles workflow filenames + encoder/model files + steps + license.
- `flux2-klein` (default, `engines.py:55`) already declares `t2i_workflow: flux2_t2i.json` and
  `edit_workflow: flux2_edit.json`. **Add a `t2i_ref_workflow` key here for Phase 2.**
- `DEFAULT_ENGINE = "flux2-klein"` (`engines.py:92`).

### 2.5 ComfyUI workflows (verified node graphs)
- `workflows/flux2_t2i.json` — text→image. Nodes: `UNETLoader`(1) → `CLIPLoader`(2, type `flux2`) →
  `VAELoader`(3) → `CLIPTextEncode`(4) → `FluxGuidance`(5) → `EmptyFlux2LatentImage`(6) →
  `RandomNoise`(7)/`BasicGuider`(8, model+cond)/`KSamplerSelect`(9)/`Flux2Scheduler`(10) →
  `SamplerCustomAdvanced`(11) → `VAEDecode`(12) → `SaveImage`(13). **No image input.**
- `workflows/flux2_edit.json` — masked edit. Same spine plus `LoadImage`(13 base) / `LoadImage`(14 mask)
  → `ImageToMask`(15) / `VAEEncode`(16) → `SetLatentNoiseMask`(17) feeding the sampler's `latent_image`.
  **Single base image + mask; no separate reference/identity image.**

### 2.6 Frontend
- Script edit screen: `webapp/frontend/src/screens/Script.jsx` (scene list; per-scene image_prompt /
  video_prompt / narration; opens `InpaintModal`).
- Inpaint modal: `webapp/frontend/src/components.jsx:294` (canvas mask paint → base64 PNG mask + prompt +
  denoise). **No reference-image input UI anywhere today.**
- Settings/style config: `webapp/frontend/src/screens/Settings.jsx` (style cards; "Script & content"
  ~`:1270`, "Image model" ~`:1348`). Style edits use a `dirtyRef` staging pattern; Settings refetches
  config on mount. Follow the existing card pattern for the new Characters card.
- **There is no user image-upload endpoint anywhere in the app today** — Phase 2 adds the first one.

### 2.7 Scene storage
- SQLite via `pipeline/orchestrator.py`. `scenes` table has a free-form `metadata_json` column
  (`orchestrator.py:249-261`), currently unused for characters — available to persist per-scene
  character assignments if we want explicit (rather than name-matched) assignment later.

### 2.8 What does NOT exist yet (confirmed by grep)
- No `character` / `persona` / `protagonist` concept in prompts, models, or UI (the one "persona" hit is
  YouTube-comment engagement guidance — unrelated).
- No reference-image conditioning in any workflow.
- No user file-upload endpoint.

---

## 3. Data model

### 3.1 The character object
Stored per style (see §3.3 for the scope decision). Shape:

```jsonc
{
  "id": "char_a1b2c3",          // stable slug/uuid; survives renames
  "name": "Bob John",           // display + primary match token
  "aliases": ["Bob", "Mr. John"],// extra match tokens (case-insensitive, word-boundary)
  "description": "a middle-aged man, short grey beard, round glasses, navy wool coat, kind eyes",
  "ref_image": "char_a1b2c3.png",// filename under the characters dir; "" if text-only
  "ref_strength": 1.0,          // Phase 2 knob: how strongly the reference conditions (0.0–1.0)
  "enabled": true
}
```

- `description` is a **noun phrase / appearance clause**, no name inside it, so it can be slotted into a
  sentence like `"Bob John (a middle-aged man, short grey beard, ...)"`.
- Reference images live in a new dir: `~/.config/video-generator/characters/` (sibling of the config
  YAML). Store as PNG. Filename = `id`.png so renames don't orphan files.

### 3.2 Normalization
Add `_norm_characters(value) -> list[dict]` in `app.py`, modeled on `_norm_size_presets`
(`app.py:345`). It must:
- accept a list, drop non-dicts and entries with empty `name`,
- generate a stable `id` if missing, dedupe ids,
- coerce `aliases` to a list of non-empty strings, `description`/`ref_image` to str, `ref_strength` to a
  clamped float, `enabled` to bool,
- **never** return the shared default object (return fresh dicts).

Call it inside `_ensure_styles()` (`app.py:384`) in the per-row loop (next to the
`row["size_presets"] = _norm_size_presets(...)` line at `app.py:430`).

### 3.3 Scope decision — per-style vs global  ⚠️ DECISION NEEDED
Two viable placements:

- **Per-style (recommended for v1).** Add `"characters": "default_characters"` to `STYLE_FIELD_TO_FLAT`
  (`app.py:286`). Cheapest — reuses the entire style mirror/migration machinery, matches how
  `size_presets` was added. Downside: a character shared by two styles is duplicated.
- **Global registry + per-style opt-in.** A top-level `cfg["characters"]` list, and each style keeps a
  list of character ids it uses. More correct for "Bob John appears in several shows", but adds a second
  storage location and its own normalization/migration.

**Recommendation:** ship **per-style** in v1 (it satisfies the request and is a small, safe diff). If
cross-style reuse becomes a real need, promote to global later — the character object shape above is
forward-compatible.

---

## 4. Phase 1 — Text consistency (no workflow changes, ships alone)

Delivers consistent characters for stylized/described subjects (robots, mascots, "a knight in red
armor"). Weak for real human faces — that's Phase 2. **Independently shippable and useful.**

### 4.1 Character sheet → the script LLM
In `_do_script_generate()` (`webapp/backend/main.py:633`), build a character sheet string from the
style's enabled characters and thread it into script generation:

```
RECURRING CHARACTERS — when a scene features one of these, describe their appearance
EXACTLY as written here, every time, so they look identical across scenes:
- Robot XYZ: matte-black humanoid chassis, single cyan optical sensor, exposed brass joints
- Bob John: a middle-aged man, short grey beard, round glasses, navy wool coat
Only mention a character when the narration involves them. Keep other scenes unaffected.
```

Plumb it through `generate_script()` (`pipeline/llm.py:551`) as a new optional arg
`character_sheet: str | None`, then:
- **Claude** (`_claude_generate`, `llm.py:155`): add a `${character_note}` placeholder to
  `script_claude_initial` **and** `script_claude_continuation` in `prompts.yaml`, filled next to
  `style_note`/`video_style_note` (`llm.py:186-193` and the continuation fill ~`llm.py:241`).
- **Local** (`_local_generate_story` `llm.py:344` + `_local_generate_visual` `llm.py:447`): add
  `${character_note}` to `script_local_story` and `script_local_visual` in `prompts.yaml` and pass it in
  both stages. **Both backends must be patched** (project rule: `pipeline/llm.py` has Claude AND local
  paths; fixes must cover both).

### 4.2 Deterministic re-injection at generation time (the reliability net)
LLMs drift, so do not rely on §4.1 alone. In `_generate_active_scene_preview()` (`app.py:1454`), right
where the final prompt is composed (`app.py:1508-1510`), add a step:

```python
# after: base_prompt = image_prompt or scene.get("image_prompt") or title
base_prompt = _inject_characters(base_prompt, scene, cfg, style_name)
prompt = f"{combined_style}. {base_prompt}" if combined_style else base_prompt
```

`_inject_characters(text, scene, cfg, style_name)`:
- gets the style's enabled characters,
- for each, tests whether `name` or any `alias` appears in the scene's `image_prompt`+`narration`
  (case-insensitive, word-boundary regex),
- if matched and the canonical `description` is **not already present**, rewrites the first mention to
  `"{name} ({description})"` (or appends `", {name}: {description}"` if simpler/safer),
- returns the augmented prompt. No match → return unchanged.

This guarantees the exact appearance string reaches FLUX even if the LLM paraphrased or omitted it.

### 4.3 Cover thumbnail (optional, cheap)
`cover_image` in `prompts.yaml:356` builds its `subject_hint` from sampled scene prompts — since those
now carry canonical descriptions, the cover benefits automatically. No change required.

### 4.4 Settings UI — Characters card
New card in `Settings.jsx` per style (near "Script & content", ~`:1270`), following the existing
`dirtyRef` staging pattern:
- list of character rows: name, aliases (comma field), description (textarea), enabled toggle, delete,
- "Add character" button,
- Phase 2 adds the image controls to each row (§5.5).

### 4.5 Phase 1 acceptance
- Define "Robot XYZ" with a description; generate a multi-scene script that mentions it; confirm the
  canonical clause appears in the relevant scenes' image prompts and the rendered stills look consistent.
- A scene not mentioning any character is byte-identical to today's output (no leakage).
- Both backends (Claude + local) inject the sheet. `_ensure_styles` round-trips characters through a
  config save/load without loss. A style with zero characters behaves exactly as today.

---

## 5. Phase 2 — Image-anchored consistency (reference images)

Delivers "Bob John looks like *this* photo/portrait" across every scene and video. **FLUX.2 only.**

### 5.0 Pre-req spike (do this FIRST — ~30 min, before writing code)  ⚠️
Confirm the ComfyUI build on the GB10/DGX workers exposes FLUX.2 reference conditioning. Load
`flux2_t2i.json` in the ComfyUI UI on a worker, add `LoadImage → VAEEncode → ReferenceLatent`, wire
`ReferenceLatent` between `FluxGuidance`(5) and `BasicGuider`(8)'s `conditioning`, and run. If
`ReferenceLatent` (or the current equivalent Flux.2 reference node) isn't available, either update
ComfyUI on the workers or fall back to the Phase-2b alternative (§5.6). **Do not build the plumbing
until the node graph is confirmed to run on a real worker.**

### 5.1 New workflow: `workflows/flux2_t2i_ref.json`
Clone `flux2_t2i.json` and insert reference conditioning. Concretely (node ids illustrative):
- add `LoadImage`(20) → `image: "{{REF_IMAGE}}"`,
- add `VAEEncode`(21) → `pixels: ["20",0], vae: ["3",0]`,
- add `ReferenceLatent`(22) → `conditioning: ["5",0]` (from `FluxGuidance`), `latent: ["21",0]`,
- repoint `BasicGuider`(8) `conditioning` from `["5",0]` to `["22",0]`.
For **multiple references** (two characters in a scene), chain a second `LoadImage`+`VAEEncode`+
`ReferenceLatent`, feeding the first `ReferenceLatent` output into the second's `conditioning`. Generate
the template with N-reference support or emit it dynamically (a small builder in `comfyui.py` that
appends reference chains is cleaner than many static templates).

### 5.2 Engine registry
In `pipeline/engines.py`, add to the `flux2-klein` entry (`:55`):
```python
"t2i_ref_workflow": "flux2_t2i_ref.json",
```

### 5.3 `generate_with_engine` gains references
Extend `generate_with_engine()` (`pipeline/comfyui.py:786`) with an optional param:
```python
def generate_with_engine(engine, prompt, output_path, *, width, height, seed=None,
                         reference_images: list[Path] | None = None, comfy_url=COMFYUI_URL):
```
- FLUX.2 + `reference_images`: upload each via `_upload_image` (`comfyui.py:488`), load
  `engine["t2i_ref_workflow"]`, fill `{{REF_IMAGE}}` (or the N-reference chain) + existing replacements,
  `_run_and_save`.
- FLUX.2 + no references: unchanged (`flux2_t2i.json`).
- FLUX.1: unchanged; if references are passed to a non-flux2 engine, ignore them (log once) — reference
  conditioning is a FLUX.2-only capability.

### 5.4 Scene → character → reference wiring
In `_generate_active_scene_preview()` (`app.py:1454`), after `_inject_characters` (§4.2):
- collect the matched characters that have a non-empty `ref_image`,
- resolve each to an absolute path under the characters dir,
- pass them as `reference_images=[...]` to `generate_with_engine` (`app.py:1514`).
Matched-by-name is the default. (Optional later: explicit per-scene assignment persisted in the scene's
`metadata_json`, `orchestrator.py:249`, for when name matching is ambiguous.)

Cap the number of references per scene (e.g. 2–3) to bound VRAM on the GB10 workers; if more characters
match, prefer the ones with reference images and log the drop (project rule: **no silent truncation**).

### 5.5 Getting the reference image — TWO paths (both requested)
1. **Upload** ("Bob John = my photo"). New endpoint, e.g.
   `POST /api/styles/{style}/characters/{id}/image` (multipart) in `webapp/backend/main.py` — this is
   the app's **first** user upload, so: validate content-type + size, decode with PIL, re-encode to PNG,
   write to `~/.config/video-generator/characters/{id}.png`, store the filename on the character.
   Add a matching `DELETE` to clear it. Frontend: a file input on the character row (`Settings.jsx`).
2. **Generate a portrait** ("Robot XYZ, consistent invented look"). A "Generate portrait" button that
   runs plain `generate_with_engine` (no reference) from the character's `description` via the style's
   engine, shows the result, lets the user **re-roll** until happy, then **locks it in** as `ref_image`.
   This is the key move: even fully invented characters get anchored to one **approved** still instead of
   being re-imagined per scene. Reuse the existing worker-pool acquire/release pattern from
   `_generate_active_scene_preview`.

Serve stored character images back to the UI via a small static/route so thumbnails render in Settings.

### 5.6 Fallback if `ReferenceLatent` is unavailable (Phase 2b)
If the spike (§5.0) fails, the weaker-but-workable path is **img2img seeding**: use the reference image
as the base latent (like `flux2_edit.json`'s `VAEEncode` at node 16) at a **high denoise** (~0.75–0.9)
with the scene prompt. This bleaks pose/background from the reference (worse than `ReferenceLatent`) but
needs no new node type. Prefer §5.1 whenever available.

### 5.7 Phase 2 acceptance
- Upload a face for "Bob John"; scenes mentioning Bob render a recognizably consistent person across
  scenes and across two different videos.
- Generate + approve a portrait for "Robot XYZ"; subsequent scenes match the approved portrait.
- Two characters in one scene both condition (within the reference cap).
- FLUX.1-schnell styles keep working (references ignored, no crash). No-character scenes unchanged.

---

## 6. Files to touch (checklist)

**Phase 1**
- `app.py` — `_norm_characters` (new), call it in `_ensure_styles` (`:384`); `_inject_characters` (new),
  call it in `_generate_active_scene_preview` (`:1508`); add `characters`→`default_characters` to
  `STYLE_FIELD_TO_FLAT` (`:286`) + `DEFAULT_CFG` default.
- `pipeline/llm.py` — thread `character_sheet` through `generate_script` (`:551`), `_claude_generate`
  (`:155`), `_local_generate_story` (`:344`), `_local_generate_visual` (`:447`).
- `prompts.yaml` — add `${character_note}` to `script_claude_initial`, `script_claude_continuation`,
  `script_local_story`, `script_local_visual`.
- `webapp/backend/main.py` — build character sheet in `_do_script_generate` (`:633`), pass to
  `generate_script` (`:654`).
- `webapp/frontend/src/screens/Settings.jsx` — Characters card (CRUD, text fields).
- Tests: extend `tests/test_styles.py` (characters survive `_ensure_styles` round-trip; injection logic).

**Phase 2 (adds to the above)**
- `workflows/flux2_t2i_ref.json` — new reference-conditioned workflow (after §5.0 spike).
- `pipeline/engines.py` — `t2i_ref_workflow` on `flux2-klein` (`:55`).
- `pipeline/comfyui.py` — `reference_images` param on `generate_with_engine` (`:786`); optional
  N-reference workflow builder.
- `app.py` — resolve matched characters' ref images and pass `reference_images` in
  `_generate_active_scene_preview` (`:1514`); reference cap + drop logging.
- `webapp/backend/main.py` — character image upload/delete endpoints + generate-portrait endpoint;
  static serving of character images.
- `webapp/frontend/src/screens/Settings.jsx` — per-character upload / generate-portrait / re-roll UI +
  thumbnail.
- Storage dir `~/.config/video-generator/characters/` (created on demand).

---

## 7. Explicitly out of scope (v1)
- **Per-character LoRA training.** FLUX.2 reference conditioning covers the need without a training
  pipeline or per-character model files. Revisit only if identity fidelity proves insufficient.
- **Consistent character *voice*** (TTS). This plan is visual only; a per-character voice could layer on
  the existing per-style voice model picker later.
- **Explicit per-scene character assignment UI.** v1 matches by name/alias; the scene `metadata_json`
  slot is reserved for explicit assignment if name-matching proves ambiguous.
- **Global cross-style character library.** v1 is per-style (§3.3); the object shape is forward-compatible.

---

## 8. Rollout / deploy notes
- Deploy = `make web-build` then `make restart-server` (the service serves the prebuilt SPA and does NOT
  rebuild it). Backend restart is required for new endpoints / `_ensure_styles` changes to take effect —
  a "feature doesn't save after merge" symptom is usually a stale un-restarted service.
- FLUX.2 on the GB10 workers needs `TORCH_BLAS_PREFER_CUBLASLT=1` (existing known gotcha) — the reference
  workflow inherits the same requirement.
- Ship Phase 1 behind nothing (safe, additive: zero characters = today's behavior). Gate Phase 2 image
  generation on the §5.0 spike passing on a real worker.
- Follow the repo rule: land large/risky work on a branch; commit + push often; open a PR when tests pass.
