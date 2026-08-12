# Configuration

Everything lives in **one YAML file**:

```
~/.config/video-generator/config.yaml
```

The [**Settings** screen](manual/settings.md) is the intended editor — it writes this
file, and a read-only cluster status panel sits alongside it. Editing the file by hand
works too; restart the backend afterwards (`make restart-server`) so it reloads.

Missing keys always fall back to their defaults, so a minimal `config.yaml` is perfectly
valid. The full key set, with defaults and inline documentation, is `DEFAULT_CFG` in
[`app.py`](https://github.com/pizzato/stephen_spielbot/blob/main/app.py).

## Styles are the unit of configuration

Almost every creative setting belongs to a **style**, not to the app. A style bundles a
narrator voice, visual and motion direction, render quality, audio mix, YouTube channel
and playlist, X account, scene-count and resolution presets, and the extra instructions
handed to the script LLM.

```yaml
styles:
  - name: Documentary
    voice: narrator_deep
    visual_style: cinematic 35mm, muted palette
    n_scenes: 12
    channel: UC…
  - name: Documentary Shorts
    parent: Documentary          # inherits everything above…
    n_scenes: 6                  # …and overrides only this
    resolution: Portrait FHD (1080×1920)
```

Two rules matter:

- **The `styles` list is the source of truth.** The flat `default_*` keys in the file
  (`default_voice`, `default_n_scenes`, …) are *mirrors* of the default style, kept in
  sync automatically. Change the style, not the mirror.
- **Child styles are stored sparse.** A style with a `parent` only stores what it
  overrides; the rest resolves through the chain at read time.

!!! warning "Restart after pulling changes"
    The backend keeps styles in memory. If a merge changes the style schema and the
    service wasn't restarted, sparse children can lose their `parent` on the next save.
    Run `make restart-server` after updating.

## Worker lists

Worker endpoints are part of the same file — see [Cluster & workers](cluster.md).

```yaml
comfy_workers:
  - http://s1:8188
tts_workers:
  - http://s1:8189
```

## The highlights

The Settings screen covers everything; this is the short list of what people change first.

| Setting | Description |
|---|---|
| ComfyUI workers | One URL per line — scenes are distributed across them in parallel |
| TTS workers | F5-TTS/Chatterbox endpoints for parallel narration (port 8189, derived by `make install`) |
| Music | Score films in this style (per-film override in Create); music is mixed in at the very end |
| UI worker idle timeout | Minutes the UI must be idle before its reserved render worker rejoins the pool (default 5) |
| LLM backend | `local` (vLLM), `claude` (Anthropic), `grok` (xAI), or `openai` |
| Local LLM URL | OpenAI-compatible endpoint, e.g. `http://localhost:8000/v1/chat/completions` |
| Claude / Grok / OpenAI key + model | API key (or the matching [environment variable](environment.md)) and model name |
| Voice model / language | Per-style TTS engine — `openf5` (default), `chatterbox-multilingual` (23 languages + a narration language that also drives the script), or the non-commercial `f5-original` preview |
| Image engine | Per-style generate and edit engines; default FLUX.2 Klein |
| Video engine | Per-style scene I2V model — `ltx23` (default) or the opt-in `ltx25` / `minimax-h3` / `minimax-h3-turbo` (see [Models → Video engines](models.md#video-engines-per-style)) |
| Sampling steps | MiniMax engines only: overrides the engine's step count (0 = engine default — Turbo 4, base H3 15). More steps = sharper but slower |
| Chained scenes | Render long scenes as **two** clips joined by H3 Motion Context, so a scene can run ~29 s instead of H3's ~15 s ceiling. Always covers acted (dialogue) scenes — Ref2VA is always MiniMax — and narrated scenes when the video engine is MiniMax (LTX narrated scenes ignore it). Scripts are planned to match: fewer, longer scenes with roughly twice the content each. Needs the Motion Context nodes on the workers (see [Models](models.md#chained-scenes)) |
| Resolution | Portrait FHD (1080×1920) by default; landscape / portrait / square presets from 512×288 to 1920×1080 |
| Render quality | First-pass and second-pass steps — higher is slower and more detailed |

## What lives outside config.yaml

| Path | What it holds |
|---|---|
| `~/videos/` | Work directories (one per film) and the published `{name}.mp4` finals |
| `~/.config/video-generator/client_secrets.json` | Google OAuth client for [YouTube](youtube_setup.md) |
| `~/.config/video-generator/*_token.json` | Per-channel YouTube and X tokens |
| `~/.config/video-generator/prompts.yaml` | Your [prompt overrides](manual/prompts.md) — sparse, merged over the packaged baseline |
| `~/.local/share/video-generator/orchestrator.sqlite3` | The [durable task graph](orchestration.md) |
| `publish_queue.json` | The [publish scheduler](manual/publishing.md) inbox |

None of these should ever be committed to a repository — they contain credentials.

## Backup & restore

**Settings → Infrastructure → Backup & restore** exports the whole configuration —
config, styles, characters, voices, and your prompt overrides — as a single file, and
restores it on another machine.

## Prompts

The instructions sent to the language and image models live in `prompts.yaml` and are
editable at `#/prompts` (**Settings → Infrastructure → Prompts**). The packaged baseline
is never overwritten: your edits are stored as a sparse override, merged per field, so
prompts you haven't touched keep improving with app updates. See
[Prompts](manual/prompts.md).

## Related

- [Environment variables](environment.md) — the handful of things that aren't in the YAML
- [Settings manual](manual/settings.md) — every tab, control by control
- [Models](models.md) — what gets downloaded and where
