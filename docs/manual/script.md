# Script

`#/script/<film>`

The review gate. Everything the render will do is decided here, and nothing has touched a
GPU yet except the scene preview images you ask for.

Views along the top, in the order the work happens: **Scripts**, **Song** (music videos
only), **Story**, **Characters** (which becomes **Characters & Artifacts** when the film
has acted scenes), **Scenes**, **Acted scenes** (only when the film has any) — and
**Cover**, which belongs to publishing, last.

Whichever view you are on, the page header carries **Brief**: it takes you back to
[Create](create.md) with everything this film was asked for — title, direction, length,
scene count, resolution, style, format and singing voice — filled back in, so you can
read what you set, change it, and run it again into a fresh work folder. The film you came
from is left exactly as it was.

## Scripts

Every saved script, as cards. **Load** opens one, **Duplicate** clones it into a fresh work
folder, **Delete** removes it. A *Story draft* chip marks scripts that are still prose with
no scenes yet, a *Song draft* chip a music video whose song is written but whose story is
not; the loaded one is marked too. Duplicating a [Music video](create.md#format) brings its
song along — the track in use, its lyrics and every kept version — so the copy re-renders
against the same song rather than singing a new one.

**New script** goes to [Create](create.md).

## Song

Only for [Music video](create.md#format) films, and the first stop for them: the song
leads and the story is drafted from it afterwards. Because you land here straight from
[Create](create.md), **Brief** in the page header is how you get back to what you asked
for. A song you leave half-finished is kept — it is listed under **Scripts** with a *Song
draft* chip, and reopening it comes back to this tab. See
[Singing films](../performance_films.md#singing-films-the-music-video-format) for what
the format does to the render.

- **Sound** — what the music model is told about the song (genre, tempo, mood,
  arrangement). The lead performer's cast voice is described on top of this at render
  time, so leave the vocalist out.
- **Lyrics** — sung exactly as written, section tags (`[Verse]`, `[Chorus]`, …) on their
  own lines.
- Both boxes have a **Re-generate** button with a *tell it how* caret: re-writing the
  lyrics keeps the sound you have (and writes to it), re-writing the sound describes the
  music for the lyrics you have. Either way both halves are saved, unsaved edits included.
- **Singing voice** — the library voice the song is sung in. At generation it only
  *describes* the vocalist to the music model (gender, age, tone — the engines cannot be
  handed a voice); left on *the model's own vocalist* the song decides. It is also the
  target of the re-voicing below, which is an actual clone.
- **Save edits** keeps your typing (**Discard edits** throws it away); **Generate the
  song** renders the track on a worker, and **Sing this as [voice]** re-voices it with
  seed-vc — always converting the sung
  original, never a previous re-voicing. Every generation and re-voicing is kept as a
  version — the one marked *In use* is the film's track, and they travel with the film
  so either side of a re-voicing can be
  [put back after the render](edit-film.md#a-song-films-song) too.
- **Ending** fixes a song that stops dead, by however many seconds you type:
    - **Extend the ending** keeps the take you have — its last couple of seconds are faded
      out and that many seconds of silence padded after them. It runs on the controller,
      so it is instant, and the arrangement is untouched.
    - **Re-generate that much longer** sings it again that much longer than what is
      playing, so the model has room to land the ending and finish the words. It is a
      fresh take, so the arrangement changes.
    - Either way the previous track stays in the version list and can be put back.
- **Scenes** splits the finished song into performed takes (blank = automatic), then
  **Draft the story →** writes the story from these lyrics. The song's *current* length is
  what gets divided, so extend it before drafting the story.

Every step on this tab can also run unattended — see
[what automation makes](settings.md#what-automation-makes). With its song review gate on,
automation writes and generates the song and then leaves it here: listen, change what you
want, and **Draft the story →** picks the film back up in its queue slot.

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

## Characters & Artifacts

Every reference the film renders from, on one wall, with **one bar to add them all**:
character, location, wardrobe, free-form **image** (any other thing the model should
match — its description tells the model what it is), **video** (a clip whose
extracted frame feeds the slot), and **soundtrack** (an uploaded audio file). Each visual
card takes a generated image, an upload, a **pasted** image, or a **URL** — a direct file
link or a page whose `og:image` / `og:video` points at one.

A **soundtrack** artifact is not a picture slot: the track is pinned into the generation
of every acted take it applies to, so the performance follows the sound and the take keeps
it as its audio — the mechanism behind
[singing films](../performance_films.md#singing-films-the-music-video-format). Scope it
with the same *Used in* scene list as any other artifact; a music video's own song is
listed here too, since it is an input of every singing take.

Catalogue members the film uses — characters with their portraits *and voice clips*,
[assets](settings.md#assets) that feed its slots — appear at the same level, marked
*catalogue* and read-only: they are shared across films, so they edit in Settings. See
[Characters](../characters.md) for the consistency mechanism.

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
screen and on a film's edit screen alike. An empty scene — one just added — has nothing
to convert, so it simply changes type and waits for you to write it. In a music video the
silent option reads **♪ Music video** — same routing, honest label: those scenes perform
the song.

A **narrated or silent** scene has:

| Field | Feeds |
|---|---|
| **Scene title** | Organisation only — not rendered |
| **Narration** | The TTS voice (narration-mode scenes) |
| **Image prompt** | FLUX — static, highly detailed first frame |
| **Video prompt** | LTX — motion and camera |

A **silent** scene also carries a **Duration** and an **On screen** cast. The cast matters
only for a style that [performs its silent scenes](../performance_films.md#silent-scenes-performed):
name who is in the shot and the scene is acted from their portraits instead of animated
from a still. Leave it empty and nothing changes.

When the style *does* perform them, the silent scene is written through the **acted
fields below** instead — on screen, setting, duration, action, camera, sound — because
that is what the take is built from. It is the dialogue editor minus the dialogue, and
the film gets the **Characters & Artifacts** wall like any other performance film. The
image and video prompts stay: the image still paints the frame the take opens on, and
the video prompt stands in as the setting while that field is empty.

Every field label has a regenerate button that rewrites just that field with the LLM,
optionally with a free-text instruction. Edits save when you leave the field.

A **dialogue** scene is [acted on camera](../performance_films.md), so it is written
through different fields — and each is stated once, in its proper place:

| Field | Feeds |
|---|---|
| **On screen** | Who appears, in order — the first is `<Picture 1>`, the second `<Picture 2>` (their portraits are the references; keep it to two) |
| **Setting** | The scenery the model builds — and the prompt behind the optional first frame |
| **Dialogue** | The lines, each with a speaker and a delivery note, acted in that character's own voice |
| **Action** | Timed beats — each reaches the model as a `[2s-6s]` window |
| **Camera / Sound** | One continuous shot; diegetic sound only |

The **video prompt is read-only**: it is assembled from those fields (never write the same
thing twice), with a legend above it saying which reference each `<Picture N>` number is.
**Edit prompt** pins hand-written text instead — the fields stop rebuilding it until you
**Rebuild from the fields**. **Re-generate scene** rewrites the whole take with the LLM
(dialogue, action, setting — same theme, optionally steered), and the sidebar's References
card shows the resolved portraits. An acted scene's **first frame is optional**: painted
from the setting with the cast anchored to their portraits, it rides as the take's
opening-composition reference — and **Remove first frame** drops it again.

## Acted scenes

For films with acted scenes, a second view shows each one as a single card: the portrait
that IS `<Picture 1>`, the voice clip that IS `<Audio 1>`, reference thumbnails, the
editable dialogue, the assembled prompt, the rendered take with its **Takes** strip (every
re-shoot is kept — click one to use it), **Shoot this scene again**, and **Reassemble
film** once takes have changed.

The view lists **every take the film shoots on the reference engine**, so a
[performed silent scene](../performance_films.md#silent-scenes-performed) appears here
too — same card, marked *silent*, with no dialogue editor: it is shot the same way and
its prompt is read the same way. **Re-generate scene** rewrites it (action, setting,
camera) and leaves it silent.

!!! note "Spoken text"
    A scene can have narration text that differs from what the voice reads — useful for
    pronunciation or pacing (`[pause:1.5]` inserts real silence). The split is made on the
    film's [edit screen](edit-film.md); here it shows as a note with an **Unsplit** link.

### First frame

The scene's still, generated on demand:

- **Regenerate image** — repaint it, optionally with an instruction
- **Edit image** — mask a region and describe the fix (masked img2img inpaint)
- The **version strip** keeps every take; click one to make it the frame that renders
- Click the image for a full-size lightbox. **←** / **→** step between scenes and take the
  editor with them — close on a frame you dislike and you are already on its scene, no
  hunting through the filmstrip. **↑** / **↓** flip through that scene's kept versions
  (browsing only; the version strip still chooses which one renders)

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

## Cover

The last view, because it is about publishing rather than the film — everything that
isn't a scene:

- **Title** — max 100 characters, with a regenerate button and *Shorter / Punchier /
  More literal* style chips
- **Resolution** — changing it here re-targets the render
- **Regenerate all scene images** — repaint every first frame
- **YouTube description** — written automatically when the script was generated;
  **Generate** rewrites it
- **Cover image** — the thumbnail, with **Edit cover** for a masked inpaint

The page header carries **Save** and **Delete** here, alongside the **Brief** button that
is on every view.

## Approving

**2. Approve → queue** sends the script to the [Queue](queue.md) ready to render.

If you got here from a queued request, the button reads **2. Save to queue slot** instead —
it keeps that item's position and lets it render straight from this script.
