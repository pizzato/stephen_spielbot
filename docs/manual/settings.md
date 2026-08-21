# Settings

`#/settings`

Studio configuration. Everything here writes to
[`~/.config/video-generator/config.yaml`](../configuration.md).

Seven tabs: **Infrastructure**, **Styles**, **Characters**, **Assets**, **Voices**,
**Channels**, **Automation**.

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
- **Temporal AI upscale chunk (sec)** — a **recovery** setting, not a threshold.
  Every clip is upscaled whole, however long it is. Only when a worker runs out of
  memory — which shows up as a black result — is the clip retried in pieces this long,
  halving until it fits or it gives up. `0` uses the packaged default of 12s.
  Splitting is never the default: joining separately upscaled pieces breaks continuity
  at every seam, worst of all with the generative IC-LoRA upscaler.

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

The **Hugging Face token** used to auto-download gated engine weights onto the workers,
then every image engine with its licence and an install state — **Download** pushes the
weights to every ComfyUI worker over SSH. Per-style engine choice lives in the
[Styles](#styles) tab.

### Video models

The same list for the scene engines — LTX 2.5, the MiniMax H3 variants and their Turbo
distillations — with each one's licence note. Nodes ship with ComfyUI itself, so
*not installed* on a worker whose weights are present usually means its container needs a
rebuild. See [Models → Video engines](../models.md#video-engines-per-style).

### Music models

ACE-Step 1.5 and the opt-in MiniMax Music 3, downloaded the same way. See
[Models → Music engines](../models.md#music-engines-per-style).

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

A style also carries its own [automation](#automation) — how much of the pipeline runs
unattended for its films — edited on the Automation tab rather than here, because those
settings have a global baseline of their own that every style inherits.

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

**Burned-in subtitles** — burn the script's captions into the video picture itself
(open captions) when a render finishes. The same track that would be attached as an
SRT on upload is rendered into the frames, so it survives any player — including
X posts, where API uploads can't always carry a caption track. Every rebuild that
regenerates the final (remix, re-voice, music change, reassemble) re-burns it, and a
[localized cut](edit-film.md#localize) is burned in its own language. The track covers
everything spoken or sung: narration, the dialogue lines acted scenes perform, and a
song film's lyrics (paced through the measured singing, so instrumental stretches stay
clean); only truly silent scenes carry no cues. A finished film can also gain or lose
the burn after the fact from the film editor's **Subtitles** card
(see [Edit film](edit-film.md#film)). Viewers can't
switch burned captions off, so a channel that also attaches the SRT (the channel's
**Upload captions** toggle) would show doubled text to viewers who turn CC on.

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
| **Default format** | `Narration`, `Dialogue`, `Mixed`, `Silent`, or `Music video` — what this style films by default: the [Create screen](create.md#format) starts on it, [AI ideas](ideas.md) are pitched to suit it, and unattended films are written in it. Every film can still switch formats on the Create screen. A child style inherits its parent's; `Music video` unfolds its song steps under [Automation](#what-automation-makes) |
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

### Video models

A film can hold two kinds of scene, and each has its own model, picked side by side:

| Picker | Renders |
|---|---|
| **Narrated & silent scenes** | Each scene from its first-frame still (LTX 2.5 or MiniMax H3 I2V). In a *mixed* film these scenes render on H3 automatically so the whole film matches the acted takes |
| **Acted (dialogue) scenes** | Each acted scene — picture and spoken dialogue in one pass — from the characters' portraits and cast voices (a MiniMax H3 Ref2VA variant) |

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

**Lyric timing** — *Align lyrics to the sung track* (on by default) applies to
[music videos](../performance_films.md#singing-films-the-music-video-format) only: at
divide time the lyric sheet is whisper-aligned against the song's separated vocal stem,
so every scene names and cuts on the words actually sung under it. It needs the
re-voicing install (`scripts/install_svc.sh`); without it — or when the alignment can't
be trusted — the energy measurement is used instead, so it is safe to leave on.

**Cadence** replaces the old voice-speed multiplier: it is the narrator's speaking pace in
**words per minute**. Each voice has a measured *natural* cadence (see the Voices tab);
setting a target cadence speeds the voice up or slows it down (target ÷ natural becomes
the TTS speed), and the same number sizes every script's word budget for the chosen video
length. *Reset to natural pace* clears the target.

### Render quality

**Resolution**, **first-pass steps**, **second-pass steps**. Higher is slower and more
detailed.

The resolution picker includes the **QHD** and **4K** finishing targets: the engines
cannot render at those sizes, so a film aimed at one renders at FHD (same orientation)
and is lifted to the target by a finishing upscale before the final is assembled. When
anything in the style targets QHD/4K — the resolution here or a size preset below — a
**Finishing upscaler** picker appears: **Fast** (plain ffmpeg resample, the default) or
one of the AI modes from the [Edit film upscaler](edit-film.md#upscale-video)
(LTX latent, LTX IC-LoRA, H3 latent), which shoot every scene through a ComfyUI
upscaler and take real render time. If an AI mode fails on a scene, that scene falls
back to Fast rather than failing the film.

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
[Characters & Artifacts](script.md#characters-artifacts) wall shows the catalogue entries it
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

### Scope: Global, then per style

The tab opens on a **scope picker** — the same one the
[Characters](#characters) tab uses: a **Global** pill, then your style hierarchy. Global is
the baseline every style automates by; picking a style shows the same settings for that
style alone, and it records **only what you change**. Everything else keeps following
Global — live, so changing Global later moves every style that hasn't overridden it.

A child style inherits its parent's overrides on top of Global, nearest ancestor winning.
Under each setting a line says where the value comes from — *Follows Global*, *Follows
“BHOB”* — and once you override it, that line offers the inherited value back: click it to
drop the override and follow again. The number on a pill is how many overrides that style
has recorded.

So one channel can prepare scripts for you to review while another renders music videos
end to end, from one queue.

Only the settings that resolve against a single **film** are scoped this way. Comment
fetching, AI-idea top-ups and publishing are queue- or channel-wide — there is no one film
to resolve them against — so they appear under Global only.

### What automation makes

Per style (see above), or globally as the baseline.

- **Auto-write scripts for queued items but don't render** — they wait unapproved for you
  to review, edit, and approve
- **Auto-approve scripts** — also writes missing scripts and renders them without review
- **Auto-start the next queue item with a ready script** — loops until the queue is empty.
  Off for a style, its films are prepared but never started for you
- **Top up the empty queue with AI ideas for this style** — when the queue runs dry,
  automation invents a topic and runs it end to end. Invented films render without review,
  so a style is only fed when its own resolved flags also have *auto-approve* and
  *auto-start* on, and it's included in auto-pick (below). Enabled on several styles,
  top-ups rotate between them
- **Include in auto-picked ideas** — whether queue top-ups may invent films in this style
  at all (per style; child styles inherit it). Unticked, the style is manual-only — the
  [AI ideas](ideas.md) screen still offers it
- **Run the script critic on every automation-written script** — QC for consistency,
  repetition, and engagement before it can render. It may rewrite, delete, add, or reorder
  scenes. Choose **1, 2, 3, 5 passes** or **Until stable (≤5)**
- **Default format** — set per style in the [Styles tab](#script-content); a style's
  scope here just shows its resolved default and links there. The Global scope keeps a
  picker for the baseline — what a style films unless it sets its own. Choosing
  `Music video` (either place) unfolds the song steps below

The two useful middle grounds: *auto-write scripts but don't render* gives you a queue of
drafts to review, and *auto-start with approval required* renders only what you've ticked.

Choosing **Music video** unfolds the song steps, because a music video is built the other
way round from every other film — the song comes first and the pictures follow it:

- **Write and generate the song before the story** — the whole point of the setting. With
  it on, automation does unattended what the [Song tab](script.md#song) does by hand: the
  scenes are then divided against the real track's length and each performed take has its
  own stretch of the song pinned in, so the cast sings the actual words. Off, the song is
  only made at render time, alongside the takes — which means the takes have nothing to
  sing to and the scene timings are guesses
- **Song critic** — `Off`, or **1, 2, 3 passes** of QC over the lyrics (length against the
  clock, singability, hook, subject) before the track is rendered. A pass is one LLM call;
  a bad song caught after the render costs a worker slot
- **Singing voice** — the [voice](#voices) automation asks for, described to the music
  model by gender, age and tone. Left on *the model's own vocalist*, the song decides
- **Re-voice the finished track as that voice** — runs the voice conversion on the
  controller, as the Song tab's *Sing this as…* does. The sung original is kept as a
  version either way, so you can put it back
- **Auto-approve songs** — off, automation stops once the song exists and parks it in the
  Song tab: no story, no scenes and no render are built on a song you haven't heard. Open
  the film's Song tab, listen, and **Draft the story** to carry it on into the normal
  script review

### YouTube automation

Global only.

- **⚡ Fully automated mode** — turns on every global step, in this card and the one above
- **Fetch & evaluate comments on a schedule**
- **Auto-approve requests above the confidence threshold**
- **Clear declined ideas** — lets previously declined topics resurface in new AI
  suggestions (ignored ones stay hidden). The AI-ideas top-up itself is per style — see
  [What automation makes](#what-automation-makes)
- **Auto-post to YouTube the moment a film finishes** — off means it waits in the publish
  queue
- **Default privacy** — `private`, `unlisted`, or `public`

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
