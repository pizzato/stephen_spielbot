<p align="center">
  <img src="assets/StephenSpielbot.png" alt="Stephen Spielbot" width="220">
</p>

# Stephen Spielbot

An AI video generator that turns a topic into a fully produced short film — complete with cinematic visuals, narration or scenes acted out by its characters, and background music.

<p align="center">
  <a href="https://www.youtube.com/watch?v=1XMU1_QnRa4">
    <img src="https://img.youtube.com/vi/1XMU1_QnRa4/maxresdefault.jpg" alt="Watch: how Stephen Spielbot works" width="640"><br>
    ▶️ Watch how it works
  </a>
</p>

<p align="center">
  <b><a href="https://pizzato.github.io/stephen_spielbot/">📖 Documentation &amp; manual</a></b>
  &nbsp;·&nbsp;
  <b><a href="https://medium.com/@pizzato/i-will-never-direct-a-movie-again-65bd4e9e6797">✍️ The story: “I will never direct a movie again”</a></b>
</p>

## What it does

1. **Script** — an LLM (local vLLM, Claude, Grok, or OpenAI) drafts and critiques the whole story as prose, you review it, and it is then divided into scenes with visual prompts, narration, and a mood-matched music description
2. **Images** — FLUX.2 Klein (the default per-style image engine) generates each scene's first-frame still, with optional recurring [characters](docs/characters.md) kept consistent via reference images
3. **Video** — [LTX 2.3](https://huggingface.co/Lightricks/LTX-2.3) animates each scene from its still via ComfyUI (local or distributed workers)
4. **Narration** — [F5-TTS](https://github.com/SWivid/F5-TTS) synthesises speech with voice cloning from a reference WAV. The default weights are the Apache-2.0 [OpenF5-TTS-Base](https://huggingface.co/mrfakename/OpenF5-TTS-Base) so narration is licensed for commercial use — see [docs/tts_licensing.md](docs/tts_licensing.md). A per-style voice-model picker adds [Chatterbox Multilingual](https://github.com/resemble-ai/chatterbox) (23 languages, with a per-style narration language that also drives the script's language)
5. **Dialogue** — scenes can instead be [acted or silent](docs/performance_films.md): the characters speak on screen, with MiniMax H3 Ref2VA generating picture and voice together from their portraits
6. **Music** — [ACE-Step](https://github.com/ace-step/ACE-Step) generates background music from the LLM's mood description, mixed in at the very end (switch it off per style or per film)
7. **Assembly** — FFmpeg mixes everything into a single video with synced audio

A film's **format** decides how the story is staged: narrated throughout, entirely
[**acted**](docs/performance_films.md) — the characters speaking on screen, no narrator and
no music — or mixed, with acted, narrated and silent scenes side by side. Each scene then
takes the render path its mode asks for.

Around the pipeline, the web app also handles the full channel workflow: a render
queue with automation, AI-suggested video ideas, per-scene editing with image
inpainting and version history, misspelling-proof cover thumbnails (text-free
artwork in the style's own engine + real-font typography with per-style fonts,
colours, and accent words), publishing to **YouTube** (multi-channel, with
playlists, captions, and tags) and **X**, a publish scheduler with per-channel
cadence, comment fetching / AI replies / community engagement, a predictive
engagement model, and C2PA "AI-generated" content credentials on published
videos.

## Quick start

**Controller** (runs the web app): Python 3.11+, Node.js 20+, FFmpeg, and a local
vLLM server **or** an API key (Claude, Grok, or OpenAI) for script generation.
**Workers** (GPU machines): Docker + the
[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) —
ComfyUI and TTS run as containers that `make install` builds and
deploys, so a worker needs no Python of its own. One machine can be both.

```bash
git clone https://github.com/pizzato/stephen_spielbot
cd stephen_spielbot
make install WORKERS="s1 s2 s3"   # deps, models, workers, config.yaml, web UI
make start                        # start ComfyUI on all workers, then launch the app
```

Omit `WORKERS=...` for a single-machine (localhost) setup, or run `make install`
with no args to be prompted. Then open [http://localhost:8001](http://localhost:8001).

```bash
make stop      # stop everything
make status    # check health of the app and all workers
```

Full setup, cluster deployment, and uninstall instructions:
**[Installation](https://pizzato.github.io/stephen_spielbot/installation/)**.

## Security

The web app has **no authentication** and is meant to run bound to `localhost`
as a single-user tool — anyone who can reach port 8001 has full control. Don't
expose it to untrusted networks. `make tailscale` shares it with your tailnet
(no app auth, tailnet-only — never public). See [`SECURITY.md`](SECURITY.md).

## Documentation

Everything lives at **[pizzato.github.io/stephen_spielbot](https://pizzato.github.io/stephen_spielbot/)**:

- [Installation](https://pizzato.github.io/stephen_spielbot/installation/) — requirements, `make install`, day-to-day commands
- [Your first film](https://pizzato.github.io/stephen_spielbot/first-film/) — topic → script → render → publish, end to end
- [The manual](https://pizzato.github.io/stephen_spielbot/manual/) — every screen in the web app, control by control
- [Configuration](https://pizzato.github.io/stephen_spielbot/configuration/) · [Environment variables](https://pizzato.github.io/stephen_spielbot/environment/) · [Models](https://pizzato.github.io/stephen_spielbot/models/)
- [Cluster & workers](https://pizzato.github.io/stephen_spielbot/cluster/) · [Architecture](https://pizzato.github.io/stephen_spielbot/architecture/) · [Troubleshooting](https://pizzato.github.io/stephen_spielbot/troubleshooting/)

The source for that site is [`docs/`](docs/) — `make docs-serve` previews it locally.
Guides that also read well on GitHub:

- [`docs/characters.md`](docs/characters.md) — recurring characters: consistent looks, reference images, voices
- [`docs/performance_films.md`](docs/performance_films.md) — acted scenes and performance films: portraits + dialogue straight to video, no first frame or TTS
- [`docs/orchestration.md`](docs/orchestration.md) — the durable SQLite task layer and how renders execute
- [`docs/youtube_setup.md`](docs/youtube_setup.md) — Google Cloud / OAuth setup for YouTube publishing
- [`docs/x_setup.md`](docs/x_setup.md) — X (Twitter) developer app setup for posting
- [`docker/README.md`](docker/README.md) — the containerized worker stack in detail
- [`webapp/README.md`](webapp/README.md) — web UI architecture and development workflow
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — dev setup, tests, and the CI gate

## Channels using this tool

<!-- CHANNELS:START -->
- [Stephen Spielbot (@StephenSpielbot)](https://www.youtube.com/@StephenSpielbot) — YouTube · The original
- [A Brief History of Botkind (@BHOBk)](https://www.youtube.com/@BHOBk) — YouTube
- [A Brief History of Botkind (@aBHOBk)](https://x.com/aBHOBk) — X
- [Amelia and the World (@AmeliaAndTheWorld)](https://www.youtube.com/@AmeliaAndTheWorld) — YouTube
<!-- CHANNELS:END -->

Making films with Stephen Spielbot? Add your channel to
[`channels.yaml`](channels.yaml) and open a pull request — that file is the only
thing you need to edit. On merge, a GitHub Action regenerates this list and the
app's **About** screen, and opens a follow-up pull request with the result. Run
`make channels` if you want to preview the result locally.

## License

Stephen Spielbot's code is licensed under [Apache-2.0](LICENSE).

The AI **models** it downloads each carry their own licenses — see
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) and
[`docs/tts_licensing.md`](docs/tts_licensing.md).
The defaults (FLUX.2 Klein, LTX-Video, ACE-Step, and the OpenF5 narration model)
are commercial-friendly; the original F5-TTS narration weights are offered only
as an opt-in **non-commercial** preview. Review the notices before monetizing.

> "Stephen Spielbot" is a playful name and is not affiliated with, endorsed by,
> or connected to Steven Spielberg or any of his companies.
