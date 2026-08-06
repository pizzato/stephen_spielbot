# Your first film

This walks the whole loop once — topic in, published video out — so the rest of the
[manual](manual/index.md) has something to hang on.

You'll need a working [installation](installation.md) and at least one GPU worker
reporting healthy in `make status`. Publishing (step 7) additionally needs a
[connected YouTube channel](youtube_setup.md); skip it the first time if you'd rather just
watch the file.

---

## 1. Open the studio

```bash
make start
```

Then open [http://localhost:8001](http://localhost:8001). The **Home** screen asks the
only question that matters: *What should we make a film about?*

Type a topic — `The rise and fall of the Roman Empire` — and press **Start**. That drops
you into **Create** with the topic already filled in.

## 2. Set the brief

[Create](manual/create.md) is where you decide what kind of film this is.

- **Style** — pick one, or *No style — experiment*. A style owns the narrator voice,
  visual direction, render quality, and which channel it publishes to. On a fresh install
  there's one default style; that's fine for now.
- **Title** and **Direction** — the direction is optional and steers the angle
  ("focus on the economic decline, not the battles"). The ✨ button next to each label
  rewrites the field with the LLM, and you can tell it *how*.
- **Length** — the video's runtime in minutes. The script is sized to the narrator's
  cadence (words per minute) and divided into 10–15 second scenes; the hint under the
  slider shows the estimated words and scenes. Start small: your first render is also the
  one that builds the timing table.
- **Resolution** — orientation first, then quality. Portrait makes a Short.
- **Format** — leave it on **Narration** for a first run. Dialogue needs characters with
  portraits and voices.

Leave *auto-approve* unchecked so you get to read the script, and press
**1. Generate script**.

!!! tip "Nothing has rendered yet"
    Script generation costs an LLM call and a few seconds. No GPU work happens until you
    approve.

## 3. Read the script

You land in [Script](manual/script.md), on the **Scenes** view. Each scene has four
editable fields:

| Field | Feeds |
|---|---|
| **Scene title** | Organisation only |
| **Narration** | The TTS voice |
| **Image prompt** | FLUX — the static first frame |
| **Video prompt** | LTX — motion and camera |

Move through scenes with **Prev** / **Next**. Every field has its own ✨ regenerate button
with a free-text instruction, so you can fix one line without redrafting the film.

Two whole-script tools are worth knowing on day one:

- **Run critic** — an LLM editor reads the entire script for consistency, repetition, and
  engagement, and may rewrite, delete, add, or reorder scenes. Pick the number of passes,
  or *Until stable*. Its verdict appears above the scenes.
- **Script history** — every critic pass and restore point is a snapshot you can roll back
  to.

Check the **Cover** view too: it holds the title, the cover phrase painted on the
thumbnail, and the cover image itself.

When you're happy, press **2. Approve → queue**.

## 4. Render it

The film is now in the [Queue](manual/queue.md). Press **Render now** on it (or
**Start next render** at the top) and the studio gets to work.

[Render](manual/render.md) shows the live picture: a progress bar, the durable task list —
one row per image, narration, scene video, mux, and final assembly, with attempt counts —
the worker states, and a time estimate.

That estimate says **rough** on your first film, because there's no timing history yet. It
sharpens on the next one.

You can leave. The render is a resumable subprocess with durable state; closing the browser
changes nothing. **Activity** shows everything the studio is doing across all films.

!!! note "How long?"
    Six 1080×1920 scenes on a single modern GPU is tens of minutes. Every worker you add
    divides the scene work.

## 5. Watch it

When the render finishes, the film appears in [Films](manual/films.md) and the final file
lands at `~/videos/<name>.mp4`.

Click the card to open [Edit film](manual/edit-film.md) and play it.

## 6. Fix the one scene that isn't right

There's always one. You don't re-render the film — you re-render the scene.

In **Edit film → Scenes**, pick the scene and choose what to redo:

- **Edit the image prompt** and re-render just the video for that scene
- **Edit image** — mask a region, describe the fix, and inpaint it
- **Change the narration** — or its voice — and re-render only the narration
- Delete the scene entirely

Every re-render is kept as a **take**, so you can compare and switch back. The final cut is
re-muxed atomically once you pick.

The **Film** tab handles whole-film changes: re-mix the audio levels, regenerate the music
from a new prompt, upscale the final cut, or add a translated
[localization](manual/edit-film.md#localizations).

## 7. Publish it

Open **Publishing → Publish a film** (or hit **Publish** on the film card).

- Pick the **film** and which **version** — a localized cut swaps in its translated title,
  description, and cover.
- Choose where: **YouTube**, **X**, or both.
- The **title** is editable with its own regenerate button; **Generate** writes the
  description.
- Set **privacy** — new uploads default to `private`. Raise it deliberately.
- Check the **thumbnail** and the **cover phrase**.

Press publish. Captions from the script are attached automatically, tags come from the
LLM's topic tags, and — if `c2patool` is installed — the file is signed with C2PA
credentials declaring it AI-generated.

## Then what?

- Turn the loop around: [AI ideas](manual/ideas.md) suggests the next topic, learns from
  what you accept and decline, and predicts each idea's reach.
- Let it run itself: [Settings → Automation](manual/settings.md#automation) goes from
  "draft scripts but wait for me" to fully hands-free, one checkbox at a time.
- Make it yours: define a [style](manual/settings.md#styles) with your voice, your visual
  direction, and your channel — then everything above inherits it.
- Give it a cast: [characters](characters.md) keep a recurring look and voice across films.
