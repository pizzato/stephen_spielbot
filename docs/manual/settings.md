# Settings

`#/settings`

Studio configuration. Everything here writes to
[`~/.config/video-generator/config.yaml`](../configuration.md).

Six tabs: **Infrastructure**, **Styles**, **Characters**, **Voices**, **Channels**,
**Automation**.

!!! warning "Unsaved edits are protected"
    Navigating away with staged changes prompts first. And after pulling code that changes
    the config schema, restart the backend (`make restart-server`) before saving — see
    [Troubleshooting](../troubleshooting.md#settings-changes-dont-stick-after-pulling-new-code).

---

## Infrastructure

### Workers

- **ComfyUI workers** — one URL per line; scenes distribute across them in parallel
- **TTS workers** — narration endpoints (port 8189, derived by `make install`)
- **UI worker idle timeout (min)** — how long the UI must be idle before its
  [reserved render worker](../cluster.md#the-reserved-ui-worker) rejoins the pool
- **Temporal AI upscale timeout (min)**

A read-only cluster status panel sits alongside, and **Container power** lets you start
and stop worker containers over SSH without leaving the app.

### LLM backend

Pick **Local**, **Claude**, **Grok**, or **OpenAI**, then fill in that backend's fields:

| Backend | Fields |
|---|---|
| Local | Local LLM URL (OpenAI-compatible), model name |
| Claude | API key, model |
| Grok | API key, model |
| OpenAI | API key, model |

Keys can also come from [environment variables](../environment.md#credentials). Stored
keys are redacted by the API — the field shows a "saved — leave blank to keep"
placeholder.

### Image models

The **Hugging Face token** used to auto-download gated engine weights onto the workers.
Per-style engine choice lives in the [Styles](#styles) tab.

### Voice models

The available TTS engines, with their licence status shown, so you can see at a glance
which are commercial-safe. See [model licensing](../tts_licensing.md).

### Predictive model

**Prediction horizon (days)**, **Minimum training samples**, and **Data lag / exclusion
(days)** — explained on the [Channel Analytics](analytics.md#predictive-model) page.

### Backup & restore

Export the whole configuration — config, styles, characters, voices, and your prompt
overrides — as one file, and restore it on another machine.

### Prompts

Opens the [prompt editor](prompts.md) at `#/prompts`.

---

## Styles

A **style** is the unit of creative configuration: it owns the voice, the visuals, the
render quality, the channel, and the script instructions. Everything downstream inherits
from it.

Styles can have a **parent**. A child stores only what it overrides and resolves the rest
through the chain, so "Documentary Shorts" can be "Documentary, but portrait and six
scenes".

### Identity & destination

| Field | Purpose |
|---|---|
| **Name**, **Parent style**, **Description** | Identity and inheritance |
| **YouTube channel** | Which connected channel this style publishes to |
| **YouTube playlist** | None, a playlist id, or `__auto__` to find-or-create one named after the style |
| **X account** | Which connected X account it posts from |

!!! note "A style with no channel never uploads"
    That's a legitimate configuration — a channel-less style gives you a clean dedup scope
    for ideas without publishing anywhere. It is never silently mapped to your first
    channel.

### Cover & first frame

**Opening cover** (`none` / `image`) and **cover hold** — how long the cover stays on
screen, in seconds (0.04–3, default 1). A single frame is a flash YouTube's frame picker
discards; a second reads as its own shot. The burn freezes the picture for that long
while the audio keeps running; the cover already carries the title, so the old
big-title-overlay mode is gone (legacy `text` configs burn the cover image).

**Cover typography** — how cover titles look. Diffusion models misspell in-image text,
so covers are never asked to paint it: the background is always generated **text-free**,
with the style's own image engine (it matches the film's look instead of the legacy
FLUX.1-schnell pin), and the title is drawn on top with real fonts. Font (a bundled
thumbnail-grade set plus anything installed on the machine), position, alignment, case,
size, colours, an optional backdrop card, and an **accent rule** (first/last word, last
line, longest word — a different colour and size for the words that matter) are all set
here, with a live preview rendered by the exact code that composites real covers. Per
film, wrap words in `*asterisks*` in the cover phrase to hand-pick the accented words,
and use **Re-apply title text** on the cover card to restyle an existing cover instantly
— no image regeneration needed. Regenerating a cover rerolls only the artwork; the words
are always pixel-perfect.

**Non-Latin titles** — the bundled display faces are Latin-only, so a Chinese or Japanese
title set in one of them would come out as a row of empty boxes. When the chosen font has
no glyphs for the phrase, the renderer silently swaps in a face that does: the bundled
**Noto Sans SC Black** (Simplified Chinese, Japanese kanji and kana — so those covers look
the same on every machine), or, for scripts it does not carry (Korean, Arabic, Hebrew,
Devanagari), the first installed font that covers the phrase. Only the fallback changes;
every other typography setting still applies. Chinese and Japanese are written without
spaces, so their titles break **between characters** instead — a long title fills two or
three big lines rather than one small one, and never starts a line on closing punctuation.
Accent rules that pick a word (first/last/longest) have nothing word-shaped to grab in
those scripts, so they accent the last line instead — nothing when the title fits on one.
`*Asterisks*` still work, and mark the exact characters they wrap.

### Script & content

| Field | Effect |
|---|---|
| **Video length (minutes)** | The style's default runtime. The script's word budget is length × the narrator's cadence, divided into 10–15 s scenes |
| **Scenes** | How many scenes that length becomes. *Auto* leaves it to the scene size (~12 s narrated, ~10 s a take when the scenes are clips); a count divides the length instead, so fewer scenes are longer ones — never past what the video engine holds in one take. [Create](create.md#length-scenes-and-resolution) can override it per film |
| **Visual style** | Appended to every image prompt |
| **Video / motion style** | Camera and subject movement guidance for video prompts |
| **Video negative prompt** | Per-style LTX negative; blank uses the built-in quality default |
| **Title style** | How generated titles should be phrased |
| **Extra script instructions** | Free-text direction handed to the script LLM |
| **Script — avoid** | Things to keep *out* of generated scripts |
| **YouTube description suffix** | Appended to every description |
| **Attribution footer / X hashtags / YouTube tags** | The open-source attribution added to published videos |

**Auto-pick exclude** keeps automation from inventing ideas in this style when topping up
an empty queue. The manual [AI ideas](ideas.md) screen still offers it.

### Video models

A film can hold two kinds of scene, and each has its own model, picked side by side:

| Picker | Renders |
|---|---|
| **Narrated & silent scenes** | Each scene from its first-frame still (LTX 2.5 or MiniMax H3 I2V). In a *mixed* film these scenes render on H3 automatically so the whole film matches the acted takes |
| **Acted (dialogue) scenes** | Each acted scene — picture and spoken dialogue in one pass — from the characters' portraits and cast voices (a MiniMax H3 Ref2VA variant) |
| **Singing scenes — music videos** | How a [song film's](../performance_films.md#singing-films-the-music-video-format) singing takes are shot. Both choices pin the scene's stretch of the real song into the generation. **MiniMax H3** (default) performs from the cast's portraits — best likeness, slow. **LTX 2.5** animates the scene's first-frame still with the song frozen in its audio latent — several times faster; the likeness rides on the first frame instead of portrait references |

**Sampling steps** beneath them is one knob for both: it overrides the step count of every
MiniMax render in the style — narrated I2V and acted Ref2VA alike. 0 keeps each engine's
default (Turbo 4, the others 15); LTX ignores it.

### Narrator & audio

**Voice model** (the TTS engine), **narration language** — which also drives the script's
language on multilingual engines — and **narrator voice**. Plus the audio mix, and the
**Music** toggle: score every film in this style, or none. Music is mixed in at the very
end, never baked into a scene — off leaves a film with only its voices and room tone, and
[Create](create.md#music) can override the choice per film. Acted films never get a score.

**Music model** (shown when music is on) picks what writes the bed:
[ACE-Step 1.5](../models.md#music-engines-per-style) — the default, instrumental and quick
— or the opt-in **MiniMax Music 3**, which is song-shaped and higher fidelity but takes
minutes per film, caps at 6 minutes, and carries its own community licence. Download
either under **Infrastructure → Music models**.

**Cadence** replaces the old voice-speed multiplier: it is the narrator's speaking pace in
**words per minute**. Each voice has a measured *natural* cadence (see the Voices tab);
setting a target cadence speeds the voice up or slows it down (target ÷ natural becomes
the TTS speed), and the same number sizes every script's word budget for the chosen video
length. *Reset to natural pace* clears the target.

### Render quality

**Resolution**, **first-pass steps**, **second-pass steps**. Higher is slower and more
detailed.

### Image model

Separate engines for **generation** and for **edit (mask + prompt)** inpainting. Default is
FLUX.2 Klein for both.

### Size presets

Three buckets — Small / Medium / Large — each pairing a video **length in minutes** with a
**resolution**. [AI ideas](ideas.md) offers these as a one-tap size, so each style's
"Small" means what that style wants it to mean.

### Characters

A read-only summary of the cast this style inherits: every **global** character, plus
the ones that belong to this style or a style above it in the hierarchy. Manage (and
move) them on the **Characters** tab.

---

## Characters

The character library, browsed by home. A picker mirroring the **Styles** tree sits on
top — **Global** first, then the style hierarchy, each pill counting the characters that
live there — and clicking a pill lists that home's characters. **Global characters** are
inherited by every style automatically; a character that **belongs to a style** is used
only by that style and the styles under it — so a parent style's cast flows to all its
children. Each card has **name**, **also known as**, **appearance**, **voice**, an
enabled switch, and a **Belongs to** picker to move it between the global pool and any
style. Reference images stay visible while you edit; uploading, portrait rolls, and
version picks apply after **Save settings**. Generated portraits use the owning style's
image model and visual look (global characters use the default style's). Films inherit
their style's cast and can add film-specific characters of their own.

See [Characters](../characters.md) for how consistency is enforced across scenes.

---

## Assets

The location and wardrobe **catalogue** — reference images that outlive one film, scoped
to styles exactly like characters (global pool + per-style, children inherit). A film's
[Characters & Artifacts](script.md#characters--artifacts) wall shows the catalogue entries it
actually uses, read-only; a film's own visual of the same name shadows the catalogue one.
Each asset has a name, kind, description, an owning style, and a reference image —
generated in the owning style's look or uploaded.

---

## Voices

The voice library — the 10 bundled public-domain LibriVox voices plus anything you add.

- **Test voice** synthesises a sample so you can hear one before assigning it
- **Record** opens a modal to record a reference clip in the browser, with a reading
  script provided; the clip is re-encoded to WAV client-side
- Uploaded reference WAVs work the same way
- **Calibrate** measures a voice's natural **cadence** (words per minute) by timing a
  fixed passage — **Calibrate all cadences** does the whole library. The number is shown
  on each row and keeps refining automatically from every real narration render. Cadence
  drives the [length → word budget](create.md#length-scenes-and-resolution) sizing and the
  per-style cadence control

Voices are cloned from a single reference clip, so a clean 10–20 seconds is worth more than
a long noisy one.

---

## Channels

Where YouTube channels and X accounts are connected.

### YouTube

**Google API** — the path to your `client_secrets.json`. Setting that up is covered in
[YouTube setup](../youtube_setup.md).

Then connect channels via OAuth. Each connected channel has:

| Field | Purpose |
|---|---|
| **Video category** | Default YouTube category |
| **Video language** | Default language |
| **Upload captions** | Attach the script SRT on upload |
| **Publishing cadence** | Videos per day, used by the [scheduler](publishing.md#schedule) |
| **Community engagement prompt** | How replies for this channel should sound |
| **Auto-respond** | Draft and send engagement replies for this channel |

### X

Two import modes: **API keys (no browser)** — key, secret, access token, access token
secret — or **OAuth 2.0 tokens**. See [X setup](../x_setup.md).

Per account: **default post text**, **post language**, **publishing cadence**, **community
engagement prompt**, and **auto-respond**.

!!! note "Re-connecting an account"
    Re-connecting maps orphaned styles back to their X account explicitly. It is opt-in —
    there is deliberately no silent fallback to some other account.

---

## Automation

Off by default. Each checkbox is one gate you're handing over.

### YouTube automation

- **⚡ Fully automated mode** — turns on every step below
- **Fetch & evaluate comments on a schedule**
- **Auto-approve requests above the confidence threshold**
- **Auto-start the next queue item with a ready script** — loops until the queue is empty
- **Auto-write scripts for queued items but don't render** — they wait unapproved for you
  to review, edit, and approve
- **Auto-approve scripts** — also writes missing scripts and renders them without review
- **Run the script critic on every automation-written script** — QC for consistency,
  repetition, and engagement before it can render. It may rewrite, delete, add, or reorder
  scenes. Choose **1, 2, 3, 5 passes** or **Until stable (≤5)**
- **Top up the queue with an AI idea when it runs empty** — needs auto-approved scripts.
  **Clear declined ideas** here lets previously declined topics resurface (ignored ones
  stay hidden)
- **Auto-post to YouTube the moment a film finishes** — off means it waits in the publish
  queue
- **Default privacy** — `private`, `unlisted`, or `public`

The two useful middle grounds: *auto-write scripts but don't render* gives you a queue of
drafts to review, and *auto-start with approval required* renders only what you've ticked.

### X automation

- **Fetch & evaluate X mentions on a schedule** — needs a paid X API tier
- **Auto-approve X requests above the confidence threshold**
- **Auto-post to X the moment a film finishes** — uses the film's style X account; long
  videos fall back to the YouTube link

### Publishing schedule

Finished videos **always** collect in the publish queue
([Publishing → Schedule](publishing.md#schedule)); publishing one manually removes it.

- **Publish on a schedule instead of the moment a film finishes** — releases them on each
  channel's and account's own **Videos per day** cadence. Mutually exclusive with the
  auto-post toggles above
- **Let comment-requested videos skip the schedule and post immediately** — on by default
- **Require approval before publishing** — finished videos are held until you approve them
  in [Films](films.md); comment-requested videos still post automatically
- **…but let automation publish them without waiting for approval** — the scheduler
  releases them on cadence while they still show as unapproved. Turning it back off
  re-holds anything not yet published

### Content Credentials (C2PA)

Signs every published video with tamper-evident provenance declaring it AI-generated by
Stephen Spielbot.

- **Embed Content Credentials in published videos**
- **Signing certificate path** — optional PEM chain for a trusted issuer
- **Signing key path** — the matching ES256 private key (PKCS#8 PEM)

Needs `c2patool` installed (`brew install c2patool`); it's skipped silently otherwise.
With no certificate set, a local self-signed one is generated automatically — readable
everywhere, though validators show "issued by an unknown source".

!!! danger "Automation posts to real channels"
    There is no dry-run mode. Turn the toggles on one at a time, with **Default privacy**
    on `private` until you trust the loop.
