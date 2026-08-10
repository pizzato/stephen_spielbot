# Script

`#/script/<film>`

The review gate. Everything the render will do is decided here, and nothing has touched a
GPU yet except the scene preview images you ask for.

Five views along the top: **Scripts**, **Story**, **Cover**,
**Characters**, **Scenes**.

## Scripts

Every saved script, as cards. **Load** opens one, **Duplicate** clones it into a fresh work
folder, **Delete** removes it. A *Story draft* chip marks scripts that are still prose with
no scenes yet; the loaded one is marked too.

**New script** goes to [Create](create.md).

## Story

The prose behind the scenes, one
editable box per chapter, each labelled with how many scenes it will become.

- **Save story** keeps your edits and lets you come back later — the draft persists until
  you divide it.
- **Divide into N scenes →** hands the story to the scene divider and moves you to
  **Scenes**.
- Changing the **Scenes** number turns the button into **Redraft in N scenes…**, which
  rewrites the *whole* prose story to fit the new length. It asks for confirmation, because
  the current draft is replaced.

If the script already has scenes, dividing again **forks** the edited story into a new
script — the existing scenes stay untouched.

The **AI editor verdict** card shows the critique the drafter ran on itself: *pass*,
*revise* (issues were flagged and fixed before division), or *skipped*, plus the notes.

## Cover

Everything that isn't a scene:

- **Title** — max 100 characters, with a regenerate button and *Shorter / Punchier /
  More literal* style chips
- **Resolution** — changing it here re-targets the render
- **Regenerate all scene images** — repaint every first frame
- **YouTube description** — written automatically when the script was generated;
  **Generate** rewrites it
- **Cover image** — the thumbnail, with **Edit cover** for a masked inpaint

The page header carries **Save**, **Re-draft** (back to [Create](create.md) with this
film's brief restored), and **Delete**.

## Characters

The cast for this film — see [Characters](../characters.md) for the full mechanism.

Each character has:

| Field | Purpose |
|---|---|
| **Name** | Used verbatim in image prompts, so the look stays keyed to it |
| **Also called** | Comma-separated aliases the narration may use |
| **Appearance** | The fixed look — drawn the same way in every scene |
| **Voice** | The cloned voice they speak with in dialogue scenes |

Each also carries a **reference look** — a portrait the image engine uses for consistency.
**Remove look** clears it; **Save to catalogue** copies the character into
[Settings → Characters](settings.md#characters) under this film's style, so that style
and the styles beneath it reuse it in future films (move it to the global pool there if
every style should have it).

## Scenes

The main event. One scene at a time, with the whole filmstrip underneath.

### Navigating

**Prev** / **Next**, or click any thumbnail in **All scenes**. The header shows
`Scene 3 / 12` and a `~20s` chip — the rough per-scene runtime.

### Restructuring

The arrow buttons move the current scene earlier or later. **Add scene** inserts a new one
after it. The trash icon deletes it (with a confirm, and never the last remaining scene).

Scene ids are renumbered 1..N on save, and the edit order is tracked separately, so
reordering is safe.

### The fields

Above the fields sits the **scene type** control — narration, dialogue, or silent — and
the fields below change with it. Switching the type **converts** the scene: the LLM
rewrites the content into the other shape with the same theme and feel (a narrated beat
becomes lines the characters speak, and vice versa). The version you leave is kept —
switch back and it is restored exactly as it was, no rework. This works on the Script
screen and on a film's edit screen alike.

A **narrated or silent** scene has:

| Field | Feeds |
|---|---|
| **Scene title** | Organisation only — not rendered |
| **Narration** | The TTS voice (narration-mode scenes) |
| **Image prompt** | FLUX — static, highly detailed first frame |
| **Video prompt** | LTX — motion and camera |

Every field label has a regenerate button that rewrites just that field with the LLM,
optionally with a free-text instruction. Edits save when you leave the field.

A **dialogue** scene is [acted on camera](../performance_films.md), so it is written
through different fields — and each is stated once, in its proper place:

| Field | Feeds |
|---|---|
| **On screen** | Who appears, in order — the first is `<Picture 1>`, the second `<Picture 2>` (their portraits are the references; keep it to two) |
| **Setting** | The scenery the model builds — an acted scene has no first frame or image prompt |
| **Dialogue** | The lines, each with a speaker and a delivery note, acted in that character's own voice |
| **Action** | Timed beats — each reaches the model as a `[2s-6s]` window |
| **Camera / Sound** | One continuous shot; diegetic sound only |

The **video prompt is read-only**: it is assembled from those fields (never write the same
thing twice), with a legend above it saying which reference each `<Picture N>` number is.
**Edit prompt** pins hand-written text instead — the fields stop rebuilding it until you
**Rebuild from the fields**.

!!! note "Spoken text"
    A scene can have narration text that differs from what the voice reads — useful for
    pronunciation or pacing (`[pause:1.5]` inserts real silence). The split is made on the
    film's [edit screen](edit-film.md); here it shows as a note with an **Unsplit** link.

### First frame

The scene's still, generated on demand:

- **Regenerate image** — repaint it, optionally with an instruction
- **Edit image** — mask a region and describe the fix (masked img2img inpaint)
- The **version strip** keeps every take; click one to make it the frame that renders
- Click the image for a full-size lightbox

### All scenes

The filmstrip, with **Regenerate all** to repaint every frame.

## The critic

In the Scenes view header: pick **1, 2, 3, 5 passes** or **Until stable**, then
**Run critic**. An LLM editor reads the whole script for consistency, repetition, and
engagement, and may rewrite, delete, add, or reorder scenes. Its verdict appears above the
scenes when it finishes.

Automation can run the critic on every auto-written script — see
[Settings → Automation](settings.md#automation).

## Script history

Every critic pass and restore point is a snapshot, listed with its label, timestamp, and
scene count. Pick one and **Restore** to roll back.

## Approving

**2. Approve → queue** sends the script to the [Queue](queue.md) ready to render.

If you got here from a queued request, the button reads **2. Save to queue slot** instead —
it keeps that item's position and lets it render straight from this script.
