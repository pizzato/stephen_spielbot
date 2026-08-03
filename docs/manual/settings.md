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
- **EchoMimic workers** — talking-head endpoints for dialogue scenes (port 8190)
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

**Opening cover** (`none` / `image` / `text`) and **cover hold** — how long the cover
stays on screen, in seconds (0.04–3, default 1). A single frame is a flash YouTube's frame
picker discards; a second reads as its own shot. `image` freezes the picture for that long
(audio keeps running), while `text` lays the title over the moving video, so holding it
costs no motion. Then the **font**, **size** (as a % of video width), and **colour** for
the text variant.

### Script & content

| Field | Effect |
|---|---|
| **Default scenes** | Prefilled scene count |
| **Script mode** | Classic or [story-first](create.md#script-mode) |
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

### Narrator & audio

**Voice model** (the TTS engine), **narration language** — which also drives the script's
language on multilingual engines — and **narrator voice**. Plus the audio mix.

### Render quality

**Resolution**, **first-pass steps**, **second-pass steps**. Higher is slower and more
detailed.

### Image model

Separate engines for **generation** and for **edit (mask + prompt)** inpainting. Default is
FLUX.2 Klein for both.

### Size presets

Three buckets — Small / Medium / Large — each pairing a **scenes** count with a
**resolution**. [AI ideas](ideas.md) offers these as a one-tap size, so each style's
"Small" means what that style wants it to mean.

### Characters

A read-only summary of the cast this style inherits: every **global** character, plus
the ones that belong to this style or a style above it in the hierarchy. Manage (and
move) them on the **Characters** tab.

---

## Characters

The character library, grouped by home. **Global characters** are inherited by every
style automatically; a character that **belongs to a style** is used only by that style
and the styles under it — so a parent style's cast flows to all its children. Each card
has **name**, **also known as**, **appearance**, **voice**, an enabled switch, and a
**Belongs to** picker to move it between the global pool and any style. Generated
portraits use the owning style's image model and visual look (global characters use the
default style's). Films inherit their style's cast and can add film-specific characters
of their own.

See [Characters](../characters.md) for how consistency is enforced across scenes.

---

## Voices

The voice library — the 10 bundled public-domain LibriVox voices plus anything you add.

- **Test voice** synthesises a sample so you can hear one before assigning it
- **Record** opens a modal to record a reference clip in the browser, with a reading
  script provided; the clip is re-encoded to WAV client-side
- Uploaded reference WAVs work the same way

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
