# Edit film

`#/edit/<film>` — `#/remix/<film>` is a deep-link alias for the same screen.

Post-production on a finished film. Tabs: **Film**, **Characters & Artifacts** (plain
**Characters** on films with no takes shot on the reference engine), **Scenes** — and
**Acted scenes** whenever the film has any such take, spoken or
[performed silent](../performance_films.md#silent-scenes-performed).

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

The thumbnail, plus the **cover phrase** — the short text drawn on the cover with the
style's [cover typography](settings.md#cover-first-frame). It follows the title until you
edit it, then stays put; wrap a word in `*asterisks*` to give it the accent colour.
Saving the phrase re-draws the title on the cover instantly (the artwork is untouched),
and **Re-apply title text** does the same after a typography tweak in Settings.

**Edit cover** opens a masked inpaint. The edit runs on the cover's text-free background
and the title is re-drawn on top, so inpainting can't smear the lettering.

Regenerated and edited covers are kept as **versions** — click one to make it the film's
cover. For styles whose opening burn is `image`, picking a different cover also marks the
final stale, so the automatic reassembly re-burns the chosen cover into the opening a few
quiet minutes later.

### Opening cover

**Burn into the opening** stamps the cover image onto the start of the final video —
YouTube Shorts ignore uploaded thumbnails and pick their own frame, which is what this
is for. The cover already carries the title (cover typography), so there is nothing to
choose: the burn is always the cover image.

**Hold for (seconds)** is how long it stays on screen, prefilled from the style's
[cover hold](settings.md#cover-first-frame) and overridable for this one burn. A single
frame is a 40ms flash that YouTube's frame picker throws away; a second reads as its own
shot. Nothing is prepended, so the timing never shifts — the cover freezes the picture
while the audio keeps running. Re-renders re-apply the burn automatically.

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
title, description, captions, and **cover**: the same cover art is re-titled with the
translated cover phrase (no image regeneration), and that localized cover is what gets
uploaded as the thumbnail and burned into the opening when the localized cut publishes.
Covers that predate text-free backgrounds need one regeneration before this works.
[Publishing](publishing.md) lets you pick which version to post, so one film can serve
several channels.

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

Edit the music prompt and **Regenerate music**. This re-runs the film's
[music engine](../models.md#music-engines-per-style) on a GPU worker and re-muxes with
your current levels. Every generated track is kept in a version strip.

#### A song film's song

For a [music video](../performance_films.md#singing-films-the-music-video-format) the
card becomes **The film's song**: the lyrics as sung (read-only here — the words are the
[Script screen's](script.md#song) to edit), the sound caption, **Sing it again**, and
**Sing it as [voice]**.

*Sing it as* is the seed-vc re-voicing: melody, timing and words kept, the singer's
timbre swapped for a library voice's. It takes a few minutes, then the film is re-muxed
so the finished cut plays the new vocals. Two things make it safe to try:

- **Nothing is thrown away.** The sung original and every re-voicing are kept side by
  side in the version strip — play each one, and **Use** puts it back and re-mixes the
  final immediately (ffmpeg only, no GPU). Before and after are both one click from
  being the released soundtrack.
- **It always converts the original vocals**, never the previous re-voicing, so trying a
  second voice doesn't clone a clone.

The picture doesn't change: the takes were shot against the song and ship muted, so
re-voicing after the render swaps the soundtrack alone. (Re-voice from the Script
screen's [Song tab](script.md#song) *before* rendering and the per-scene stretches pinned
into the takes sing in that voice too.)

Re-voicing needs seed-vc on the controller (`scripts/install_svc.sh`); without it the
button is disabled and says so. The conversion itself runs on whichever
[GPU worker is free](../cluster.md#song-re-voicing-rides-along-in-the-comfyui-container),
which is why it takes a couple of minutes and not ten.

---

## Characters & Artifacts

The same reference wall as the [Script screen](script.md#characters--visuals): one bar
adds **character · location · wardrobe · image · video**, the film's own entries are
editable cards, and the catalogue members the film uses appear read-only with their
portraits and voice clips. Visual cards take generated images, uploads, pasted images, or
a URL. **Save to catalogue** copies a film character into
[Settings → Characters](settings.md#characters) under the film's style.

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

Each button does only its own part. A narrated scene has:

| Button | Re-renders |
|---|---|
| **Narration** | Just the audio for this scene |
| **Image** | Just the first frame (takes an instruction) |
| **Edit image** | Masked inpaint — draw a region, describe the fix |
| **Video** | Just this scene's clip (takes an instruction) |
| **Trim** | Cuts the tail off the existing clip — no re-render |

An **acted** scene trades the Image buttons for **Remove first frame** (its frame is a
reference, not a render input — see [acted scenes](../performance_films.md)), and its
Video button reads **Shoot again**: the whole take re-renders from the references, and its
instruction directs the performance (*"make her angrier"*, *"hold the pause longer"*) rather
than the picture. It also
gains **Continue**, which shoots more of the take it already has instead of replacing it.
The **scene type** switch converts a scene between narration, dialogue and silent — same
theme, the other shape — and keeps the version you leave, so switching back restores it.

#### Trimming a scene

When a clip is fine except for its tail — a drifting last second, a gesture that overruns —
**Trim** cuts it without spending a re-render. Drag the handle to the new end point; the
player seeks there so you see the frame the scene will hold, and the readout shows what is
kept and what is cut. Applying it cuts the audio with the video, so watch (and listen to)
the tail before trimming into narration. The trimmed cut becomes a new video take and the
untrimmed one stays in the strip, one click away.

#### Continuing an acted scene

When a dialogue take ends too early — the line lands but the moment has nowhere to go, or
you simply want a few seconds more — **Continue** shoots the next clip carrying on from the
last frame, rather than re-shooting the scene and losing the take you liked. The camera does
not cut: the new clip is conditioned on the motion the previous one ended with, so the room,
the framing, the faces and the voices carry across the join.

The dialog shows the take parked on its final frame — the moment being continued — and asks
for three things: **how much longer** (4 to 12 seconds), an optional note for what happens
next ("she finally looks up"), and any **dialogue** spoken in the continuation, in the same
voices as the scene. Leave the dialogue empty and the moment simply plays on. Anything said
here is added to the scene's script, so captions and the description stay in step with what
the film actually says.

The join is made once the clip renders, and the shorter take is kept in the takes strip —
if the continuation is not what you wanted, click back to the take before it. Continue again
to keep going: each continuation picks up where the last one stopped.

Continue is offered only where it can actually work, so the button is absent when:

- the scene is narrated (only acted takes carry the motion context);
- the take was shot before this feature existed — shoot it again once and it becomes
  continuable from then on;
- the clip in the cut is no longer the take the continuation point belongs to, because a
  different take was selected or this one was trimmed;
- the worker that shot the take is offline. The context lives on that machine's disk, so a
  continuation can only render there — bring it back, or shoot the scene again.

**Reassemble film** (top of the list) re-cuts the published final from the scene parts —
after re-shoots, take picks, or reorders — re-mixing music only where the film has it.

### Version history

Two strips under each scene: **image takes** and **video takes**. Click one to select it,
or delete the ones you don't want. Selecting a take re-muxes the final film atomically, so
there's never a half-updated video on disk.

Video takes keep the last ten (plus whichever is selected).

### Restructuring

The chevrons move a scene up or down; **Delete** removes it. Both re-cut the final film.

!!! tip "Re-renders are queued, not fired in parallel"
    Scene re-renders share the render pool, so hitting several at once won't overload a
    worker — they queue, and survive a restart.

---

## Acted scenes

The acted view from the [Script screen](script.md#acted-scenes), with each scene's
rendered clip in it: cast slots with portraits and voices, reference thumbnails, the
editable dialogue and assembled prompt, **Re-generate scene**, **Shoot this scene again**,
the **Takes** strip (every re-shoot kept), and **Reassemble film**.

It covers every take the film shot on H3 — including its
[performed silent scenes](../performance_films.md#silent-scenes-performed), which appear
as the same card marked *silent*, without the dialogue editor.
