# Edit film

`#/edit/<film>` — `#/remix/<film>` is a deep-link alias for the same screen.

Post-production on a finished film. Three tabs: **Film**, **Characters**, **Scenes**.

Nothing here re-renders the whole film. Every operation touches the smallest thing it can
and re-muxes the final cut atomically when it's done.

---

## Film

Whole-film changes.

### Film details

The **title** (with a regenerate button) and the description that publishing will use.
Also **Approve** when the film is held by the [approval gate](settings.md#publishing-schedule),
**Publish** to open [Publishing](publishing.md), and **Delete**.

### Cover image

The thumbnail, plus the **cover phrase** — the short text painted on the cover and burned
into the first frame. It follows the title until you edit it, then stays put.

**Edit cover** opens a masked inpaint on the cover image.

### First frame

**Add to first frame** burns the cover into the opening of the final video — `none`,
`image`, or `text`. YouTube Shorts ignore uploaded thumbnails and pick their own frame,
which is what this is for.

The cover is held for the style's [**cover hold**](settings.md#cover-first-frame) (1
second by default) — a single frame is a 40ms flash that YouTube's frame picker throws
away. Nothing is prepended, so the timing never shifts: `image` covers the picture while
the audio keeps running, and `text` lays the title over the moving video. Font, size, and
colour come from the style's settings; re-renders re-apply the burn automatically.

### Re-mix audio

Three sliders — **Voice**, **Music**, **Ambient** (0–150%) — then **Re-mix film**. This
balances the levels and re-muxes without re-rendering any video.

### Narrator

Change the narrator voice for **every** scene and rebuild the final audio. Per-scene voices
set in the Scenes tab are preserved.

### Localize this film

Translate the narration and re-speak it in another language — same voice, same visuals,
same music, no re-rendering. Pick a **target language** and **Localize film**.

#### Localizations

Each localization is kept as a **switchable version** of the film, with its own translated
title, description, and captions. [Publishing](publishing.md) lets you pick which version
to post, so one film can serve several channels.

Dialogue and silent scenes keep their original language.

### Upscale video

Upscale the finished film and keep the result as a selectable final version.

| Mode | What it does |
|---|---|
| **Fast** | Plain ffmpeg scale |
| **LTX latent** | The simple model upscaler (latent 2×) |
| **LTX IC-LoRA** | The generative [Pixel Spatial Upscaler](https://huggingface.co/Lightricks/LTX-2.3-22b-IC-LoRA-Pixel-Spatial-Upscaler) |

Pick a **target resolution** — only larger ones are offered — then a mode. The original
stays available.

### Background music

Edit the music prompt and **Regenerate music**. This re-runs ACE-Step on a GPU worker and
re-muxes with your current levels. Every generated track is kept in a version strip.

---

## Characters

The film's cast, same fields as the [Script](script.md#characters) view: name, aliases,
appearance, voice, and a reference look. **Save to catalogue** promotes one to the global
library. **Open Scenes** jumps to the scene list to assign them.

See [Characters](../characters.md) for how consistency actually works.

---

## Scenes

One card per scene. Collapsed, a card shows its thumbnail and prompt summaries; **Edit**
opens the fields.

### Editable fields

| Field | Notes |
|---|---|
| **Title** | With a regenerate button |
| **Scene type** | Narration, dialogue, or silent — and who speaks |
| **Narration** | With a regenerate button |
| **Spoken text** | Optional split, see below |
| **Narrator voice** | Per-scene override; defaults to the film narrator |
| **Image prompt** | FLUX — static frame |
| **Video prompt** | LTX — motion & camera |

Every regenerate button takes a free-text instruction plus quick chips, so you can say
*"make it less dramatic"* rather than rewriting the line yourself.

#### Splitting spoken text

**Split spoken text from the narration** gives the voice its own line while the captions
keep the written narration. Use it to respell tricky words (*lead pipes* → *led pipes*) and
to place real silence with `[pause]` or `[pause:1.5]`. Untick it to go back to speaking the
narration.

### Re-rendering a scene

Four buttons, each doing only its own part:

| Button | Re-renders |
|---|---|
| **Narration** | Just the audio for this scene |
| **Image** | Just the first frame (takes an instruction) |
| **Edit image** | Masked inpaint — draw a region, describe the fix |
| **Video** | Just this scene's clip (takes an instruction) |

### Version history

Two strips under each scene: **image takes** and **video takes**. Click one to select it,
or delete the ones you don't want. Selecting a take re-muxes the final film atomically, so
there's never a half-updated video on disk.

Video takes keep the last three.

### Restructuring

The chevrons move a scene up or down; **Delete** removes it. Both re-cut the final film.

!!! tip "Re-renders are queued, not fired in parallel"
    Scene re-renders share the render pool, so hitting several at once won't overload a
    worker — they queue, and survive a restart.
