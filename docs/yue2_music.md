# YuE2 music engine (planned extra)

!!! warning "Not wired"
    This page is a research note and an implementation plan. **YuE2 is not a
    selectable music engine.** The live backends remain
    [ACE-Step 1.5](models.md#music-engines-per-style) (default) and the opt-in
    [MiniMax Music 3](models.md#music-engines-per-style). Settings shows YuE2
    under Infrastructure as **not wired** so it cannot be picked per style, and
    `generate_music` does not call it. A hand-edited `music_engine: yue2` in
    config falls back to ACE-Step on purpose.

[YuE2](https://map-yue2.github.io/) (M·A·P / HKUST and collaborators, September
2026) is a lyrics-to-song model with an editable symbolic plan. Quality on
WildSongBench is competitive with the evaluated proprietary systems, including
Suno v5, and ahead of ACE-Step 1.5 and MiniMax Music 3 on the published
SongBench average. That is why it is worth a later extra backend — and why it
must stay extra: **the weights are CC BY-NC 4.0**.

## Findings

### What it is

One AR–NAR Mixture-of-Transformers (~3.59B parameters, 28 layers) writes an
ABC score, then semantic tokens, then acoustic latents; a VAE decodes 48 kHz
stereo. Creation, covering, and agentic score editing share the same
checkpoint. Planning mode (`cot`) is `full` (melody + chords, default),
`melody` (covers), or `off` (no score).

Inputs are a **style prompt** (genre, instruments, vocal character, language,
tempo) and **tagged lyrics** (`[Verse]` / `[Chorus]` / …), optionally an ABC
score. Duration is **not** a first-class argument — length follows the lyrics
and the generation budget.

### Availability (usable today, locally)

| Resource | Where | Notes |
|---|---|---|
| Demo / paper page | [map-yue2.github.io](https://map-yue2.github.io/) | Listening demos, scores, WildSongBench |
| Code | [github.com/multimodal-art-projection/YuE](https://github.com/multimodal-art-projection/YuE) | `yue2-infer` 0.1.6; Apache-2.0 **code** |
| Weights | [`m-a-p/YuE2-3B`](https://huggingface.co/m-a-p/YuE2-3B) | Ungated; `model.safetensors`; ~3.63B BF16 params |
| Listening decoder | [`m-a-p/YuE2-Vae`](https://huggingface.co/m-a-p/YuE2-Vae) | Default; ~530 MB |
| Benchmark decoder | [`m-a-p/YuE2-Vae-legacy`](https://huggingface.co/m-a-p/YuE2-Vae-legacy) | Paper protocol only |
| Covers (optional) | [`m-a-p/SheetSage2`](https://huggingface.co/m-a-p/SheetSage2) + MERT2 | Audio → ABC; not required for generation |
| HF Spaces | `mrfakename/yue2-3b`, `Zuzuus/yue2-3b` | Demos, not a production API |

There is **no official hosted inference API**. Tokenwave.AI supplied synthetic
training data; MBZUAI, NYU, Stanford, NOIZ, and ACE Studio are listed as
collaborators. ACE Studio is already the lineage behind this project's
ACE-Step default — that is a different model, Apache-2.0, already wired.
Contact on the YuE repo (`gezhang@umich.edu`) is the path if someone later
wants a commercial weight license; do not assume one exists.

Install (upstream): Python 3.10+ (3.12 recommended), `pip install .` from the
YuE repo, or the published `yue2_infer` wheel. First load downloads the HF
repos. Authors report GPU generation/cover/editing runs and a 3.6-minute song
in ~71 s on an RTX 4090 (full CoT, ~11 GiB peak; 24 GB GPU / 24 GB host RAM
recommended; one song at a time).

### License — the blocker for monetized YouTube

| Piece | License |
|---|---|
| First-party inference code, skill, docs | Apache-2.0 |
| **YuE2-3B, YuE2-Vae, YuE2-Vae-legacy weights** | **[CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/)** |
| YuE v1 archive (`YuE-v1` branch) | Its own terms (v1 weights were later Apache-2.0; **do not confuse with YuE2**) |

CC BY-NC 4.0 grants reproduction and adaptation **for NonCommercial purposes
only**. Spielbot publishes to potentially monetized YouTube/X channels.
**Do not make YuE2 the default. Do not silently fall back from a chosen YuE2
run onto ACE-Step** once it is wired (that would mix licenses and surprise
the operator). Mirror the `f5-original` TTS preview: opt-in, flagged
non-commercial, refused for anyone who needs to monetize.

Training data is described as primarily CC0 music plus Tokenwave.AI synthetic
data under license. That does not relax the weight license.

### Hardware and latency

| | YuE2 (upstream, 4090) | ACE-Step 1.5 (this project) | MiniMax Music 3 (this project) |
|---|---|---|---|
| Size | ~3.6B + VAE (~8 GB weights) | ~5 GB | ~14 GB |
| VRAM | ~11 GiB peak (max-context test 14 GiB) | Fits current workers | Fits current workers |
| Floor | 24 GB NVIDIA, BF16 | Current ComfyUI workers | ComfyUI ≥ v0.33.0 |
| Latency | ~71 s for ~3.6 min of audio | Seconds per film | Minutes (83 s for a 30 s bed on a GB10) |
| Length control | Lyrics-driven, not seconds | Any length (up to 1000 s) | Cap 6 min, then loop |

A GB10 worker has enough unified memory. Do **not** co-resident YuE2 with an
H3 or LTX render on the same GPU without an explicit lease: 11 GiB plus a
22B/33B video pass will not fit a 24 GB card.

## How music works today

The per-style key is `music_engine` (`default_music_engine` in the flat
config), resolved by `pipeline/engines.py` `MUSIC_ENGINES`. Both live engines
are **ComfyUI graphs** with the same placeholders (`TAGS` / `DURATION` /
`SEED` / `LYRICS`):

1. Settings → Styles → Narrator & audio → **Music model** picks the engine
   (only when music is on). Child styles inherit it.
2. Settings → Infrastructure → **Music models** downloads weights onto
   ComfyUI workers. Availability is "UNET on the worker" plus, for MiniMax,
   the native node class.
3. The LLM writes a 20–40 word **music description** (`prompts.yaml`) for
   narrated films, or a structured **caption + tagged lyrics** for a
   [music video](performance_films.md#singing-films-the-music-video-format).
4. `resume_generation.py` / `worker_agent.py` call
   `pipeline.comfyui.generate_music(...)`, which loads
   `workflows/ace_music.json` or `workflows/minimax_music.json`, queues
   ComfyUI, and waits `eng["timeout"]`.
5. ACE-Step can **repaint-extend** an existing take
   (`ace_music_extend.json` + `AudioLatentExtendMask`). MiniMax cannot.
6. `assembler.mix_background_music` loops a bed that is shorter than the
   picture (MiniMax's 6-minute cap, or a song that ended early).
7. Licenses live in `THIRD_PARTY_NOTICES.md` and on the Settings chips.

The worker image (`docker/comfyui/`) is **vanilla ComfyUI** — native nodes
only, `COMFYUI_REF=v0.33.0`. Custom-node packs are out of policy.

## Why YuE2 does not drop into that graph

- Official inference is the **`yue2` Python package**, not ComfyUI. Community
  packs ([T8mars/Comfyui-YuE2-T8](https://github.com/T8mars/Comfyui-YuE2-T8),
  [piscesbody/ComfyUI-YuE2](https://github.com/piscesbody/ComfyUI-YuE2)) exist
  and would fight the vanilla-worker rule, vendor extra licenses (Seed-VC is
  already a separate GPL process here), and lag the 0.1.6 kit.
- The API is `style` + `lyrics` + `cot` + `seed`, not `seconds`. Forcing a
  duration into a MiniMax-shaped workflow would be fiction.
- It is a **singer**. ACE-Step's job in this app is usually an instrumental
  bed. YuE2's natural fit is the music-video path that already passes tagged
  lyrics through `generate_music(..., lyrics=...)`.
- `test_every_engine_has_a_workflow_file` requires every `MUSIC_ENGINES`
  entry to ship `{{TAGS}}` / `{{DURATION}}` / `{{SEED}}`. Putting `yue2` in
  that dict without a real graph would either fail CI or pretend it is
  wired. **Keep it out of `MUSIC_ENGINES` until a backend exists.**

## Recommended approach (when someone wires it)

A **sidecar worker**, same shape as TTS (`pipeline/tts_worker.py` +
`docker/tts/`), not a ComfyUI custom node.

```
Controller  ──generate_music──►  ComfyUI :8188   (ACE-Step / MiniMax, unchanged)
            ──if engine yue2──►  YuE2    :8190   (new; optional)
```

1. **Registry.** Add `yue2` to `MUSIC_ENGINES` only in the wiring PR, with
   `commercial_ok: False`, no `workflow` (or a sentinel `backend: "yue2"`
   that `generate_music` branches on **before** `_load_workflow`), no
   `extend_workflow`, `downloadable` via HF repos not ComfyUI object_info.
   Until then it lives in `PLANNED_MUSIC_ENGINES` and the style picker
   ignores it.
2. **`generate_music`.** If `eng.get("backend") == "yue2"`: POST style /
   lyrics / seed / cot to the sidecar; write WAV/FLAC to `output_path`;
   do not queue ComfyUI. Map today's `tags` → YuE2 `style`, `lyrics` →
   `lyrics` (empty lyrics allowed; the caption should still say
   instrumental). Ignore `duration_seconds` except as a post-trim /
   loop hint for beds. `extend_from` must raise (no latent extend).
3. **Fallback.** Missing sidecar, missing weights, or a failed generate:
   **fail the job** with a clear error. Do not swap to ACE-Step. Unknown
   keys (including `yue2` *before* it is wired) keep today's
   `resolve_music` → ACE-Step behaviour so old configs stay safe.
4. **Worker.** New `docker/yue2/` image: Python 3.12, pinned `yue2-infer`
   (or `pip install` from a pinned YuE commit), torch matching the
   official 2.10 stack — **do not** install this into the ComfyUI
   container (torch 2.10 vs the worker's CUDA 13 stack is a fight).
   HTTP surface modelled on the TTS worker: `/health`, `/generate`,
   `/prewarm`. One request at a time. Lease like TTS so a film's YuE2
   pass does not share the GPU with H3/LTX.
5. **Settings.** Infrastructure row becomes a real download/prewarm (HF
   `m-a-p/YuE2-3B` + `m-a-p/YuE2-Vae`) with the **non-commercial** chip.
   Styles → Music model grows a `yue2` option **only after** generate
   works, with the same chip and license note. Default stays `ace-step`.
6. **Prompts.** Music-video caption + lyrics already match YuE2. For
   narrated beds, keep the ACE-Step tag list; a YuE2 bed may still sing
   unless the caption says instrumental and lyrics stay empty — document
   that, do not invent a second prompt family until we hear it fail.
7. **Optional later:** save `score.abc` next to the wav in music history;
   SheetSage2 covers; `cot` / CFG knobs. None of that is required for v1.

### Files a wiring PR would touch

| Area | Files |
|---|---|
| Registry | `pipeline/engines.py` (move `yue2` into `MUSIC_ENGINES`, `backend`) |
| Generate | `pipeline/comfyui.py` `generate_music`; new `pipeline/yue2.py` |
| Worker | `docker/yue2/Dockerfile`, compose, `scripts/install_worker_container.sh` |
| Orchestration | `resume_generation.py`, `worker_agent.py`, `pipeline/orchestrator.py` (timeout / lease) |
| API / UI | `webapp/backend/main.py` (`/api/models/engines`, install/prewarm); `Settings.jsx` picker |
| Config | `app.py` `_norm_music_engine` (already key-based) |
| Tests | `tests/test_music_engines.py` plus a sidecar client test with mocks |
| Docs / legal | this page, `docs/models.md`, `THIRD_PARTY_NOTICES.md`, `docker/README.md` |

Do **not** add `yue2` to `MUSIC_ENGINES` or ship a fake `workflows/yue2_music.json`.

### Config knobs (wiring PR)

| Knob | Where | Default |
|---|---|---|
| `music_engine: yue2` | per style | off — ACE-Step stays default |
| `yue2_workers` | Infrastructure | empty (engine unusable until set) |
| `yue2_cot` | optional, later | `full` |
| `yue2_cfg_scale` | optional, later | upstream default |

No new secrets. Weights are ungated; an HF token is not required.

## Risks

| Risk | Handling |
|---|---|
| **CC BY-NC 4.0 vs monetized YouTube** | Never default; chip + license note; docs; fail closed. Same posture as `f5-original`. |
| **VRAM / contention** | Dedicated sidecar, exclusive GPU lease, one song at a time. |
| **Latency** | Timeout on the order of MiniMax (1800 s) until measured; Activity should say "YuE2", not "ComfyUI". |
| **Length** | No seconds control; loop short beds; music videos already time off the sung file. |
| **Vocals on a "bed"** | Prefer YuE2 for music videos; warn that instrumental requests may still sing. |
| **Community ComfyUI nodes** | Out of scope. Vanilla ComfyUI stays ACE/MiniMax. |
| **YuE v1 vs YuE2 license mix-up** | This page and `THIRD_PARTY_NOTICES.md` name YuE2 weights as NC. |
| **Upstream still moving** | Pin a YuE commit / `yue2-infer` version; the technical report is "coming soon". |

## What this repository does today

- Documents the research and the wiring plan (this page).
- Lists YuE2 in `THIRD_PARTY_NOTICES.md` as a **planned extra, not downloaded**.
- Exposes `PLANNED_MUSIC_ENGINES["yue2"]` for a Settings **not wired** row.
- Leaves `MUSIC_ENGINES`, ComfyUI workflows, worker images, and the style
  picker unchanged. Defaults stay ACE-Step.

A follow-up PR should only start once someone is ready to ship the sidecar
and accept the non-commercial restriction for that style's output.
