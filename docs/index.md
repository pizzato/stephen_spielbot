---
hide:
  - navigation
---

# Stephen Spielbot

An AI video generator that turns a topic into a fully produced short film — cinematic
visuals, narration, and background music — plus the whole channel workflow around it:
a render queue, AI-suggested ideas, per-scene editing, and publishing to YouTube and X.

It runs on your own hardware. A **controller** (your laptop or desktop) hosts the web
app; one or more **GPU workers** do the rendering.

[Install it](installation.md){ .md-button .md-button--primary }
[Make your first film](first-film.md){ .md-button }
[Watch how it works](https://www.youtube.com/watch?v=2h5T0mkW1gc){ .md-button }

---

## What happens when you give it a topic

| # | Step | What runs |
|---|------|-----------|
| 1 | **Script** | An LLM (local vLLM, Claude, Grok, or OpenAI) drafts the whole story as prose, critiques it, and — once you've read it — divides it into scenes |
| 2 | **Images** | [FLUX.2 Klein](https://huggingface.co/Comfy-Org/vae-text-encorder-for-flux-klein-4b) paints each scene's first frame, with recurring [characters](characters.md) held consistent via reference images |
| 3 | **Video** | [LTX 2.3](https://huggingface.co/Lightricks/LTX-2.3) — or opt-in [LTX 2.5](https://huggingface.co/Lightricks/LTX-2.5) / MiniMax H3 — animates each scene from its still, through ComfyUI |
| 4 | **Narration** | [F5-TTS](https://github.com/SWivid/F5-TTS) speaks the script in a cloned voice — or [Chatterbox Multilingual](https://github.com/resemble-ai/chatterbox) for 23 languages |
| 5 | **Dialogue** | Scenes can instead be acted: the characters speak on screen, picture and voice generated together by [MiniMax H3](https://github.com/MiniMax-AI) — see [acted scenes](performance_films.md) |
| 6 | **Music** | [ACE-Step](https://github.com/ace-step/ACE-Step) scores it from the LLM's mood description, mixed in at the very end — or switched off per style or per film |
| 7 | **Assembly** | FFmpeg mixes it all into one film with synced audio |

Everything is reviewable and editable between steps. Nothing renders until you approve
the script, and nothing publishes until you say so.

## And around the pipeline

<div class="grid cards" markdown>

-   :material-view-list: **A queue that runs itself**

    Requests collect in a [render queue](manual/queue.md) with optional
    [automation](manual/settings.md#automation) — from "draft scripts but wait for me"
    all the way to fully hands-free.

-   :material-lightbulb-on: **Ideas, not blank pages**

    [AI ideas](manual/ideas.md) suggests topics for your channel, learns from what you
    accept and decline, and predicts each one's early reach.

-   :material-movie-edit: **Per-scene editing**

    Re-render any scene, [inpaint](manual/edit-film.md) part of an image, swap the
    narration voice, upscale the final cut — with version history at every step.

-   :material-upload: **Publishing on your terms**

    Multi-channel [YouTube](youtube_setup.md) and [X](x_setup.md) publishing with
    playlists, captions, tags, a [cadence scheduler](manual/publishing.md), and C2PA
    "AI-generated" content credentials.

-   :material-comment-processing: **Community loop**

    Pull comments and mentions, draft replies, and turn viewer requests into queued
    videos — see [Community](manual/community.md).

-   :material-chart-line: **Know what lands**

    [Channel Analytics](manual/analytics.md) tracks performance and trains a predictive
    model that estimates a title's first-days views before you render it.

</div>

## Where to go next

<div class="grid cards" markdown>

-   :material-download: **[Installation](installation.md)**

    Requirements, `make install`, single-machine and multi-worker setups.

-   :material-play-circle: **[Your first film](first-film.md)**

    An end-to-end walkthrough: topic → script → render → publish.

-   :material-book-open-variant: **[The manual](manual/index.md)**

    Every screen in the web app, control by control.

-   :material-cog: **[Reference](configuration.md)**

    Configuration keys, environment variables, cluster layout, models, architecture.

</div>

## Requirements at a glance

**Controller** — Python 3.11+, Node.js 20+, FFmpeg, and either a local vLLM server or an
API key for Claude, Grok, or OpenAI.

**Workers** — Linux + NVIDIA GPU, Docker, and the NVIDIA Container Toolkit. ComfyUI and
TTS run as containers that `make install` builds and deploys, so a worker needs no Python
of its own. The controller can also *be* the worker on a single
machine.

Full detail on the [installation page](installation.md).

!!! warning "The web app has no authentication"
    It is a single-user tool meant to run on `localhost`. Anyone who can reach port 8001
    has full control of your channels. See [Security](security.md).

## License

The code is [Apache-2.0](https://github.com/pizzato/stephen_spielbot/blob/main/LICENSE).
The AI **models** it downloads each carry their own licenses — the defaults are
commercial-friendly on purpose. See
[model licensing](tts_licensing.md) and
[THIRD_PARTY_NOTICES.md](https://github.com/pizzato/stephen_spielbot/blob/main/THIRD_PARTY_NOTICES.md)
before monetizing.

> "Stephen Spielbot" is a playful name and is not affiliated with, endorsed by, or
> connected to Steven Spielberg or any of his companies.
