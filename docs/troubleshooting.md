# Troubleshooting

Start with `make status` — it reports the web app plus every worker container's health,
and most problems announce themselves there.

## Installation and startup

### The app doesn't come up on :8001

```bash
make status
make restart-server
```

If the frontend loads but looks stale, the backend is serving the last *build*. Run
`make web-build`, then `make restart-server`.

### Settings changes don't stick after pulling new code

Almost always the backend wasn't restarted, not a bug. The service keeps config and styles
in memory, and a schema change between versions can strip fields on the next save.

```bash
make restart-server
```

### `ffmpeg: not found`, but only under the service

The macOS LaunchAgent gets a minimal `PATH` without `/opt/homebrew/bin`. Set
[`FFMPEG_PATH`](environment.md#media-tools) explicitly.

## Workers and GPUs

### `nvidia-smi` works but CUDA fails inside the container

The classic symptom is `cuInit` returning "unknown error" (999) in the container while
`nvidia-smi` is fine on the host. It happens when the NVIDIA driver or its kernel modules
were (re)loaded *after* the containers were created.

```bash
make start            # recreates containers, picking up current device nodes
make restart W=s2     # or just one host
```

To stop it happening at boot, load the modules before Docker:

```bash
printf 'nvidia\nnvidia-uvm\n' | sudo tee /etc/modules-load.d/nvidia.conf
```

If it persists on a very new driver, switch that host to
[CDI GPU injection](cluster.md#gpu-injection-mode).

### CDI "broke" after a driver upgrade

The CDI spec embeds versioned driver library paths, so an upgrade stales it. The deploy
preflight detects the mismatch:

```bash
sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml
```

Enable `sudo systemctl enable --now nvidia-cdi-refresh.path` to keep it fresh
automatically. Note that after a plain *reboot* (not an upgrade), the real fix is loading
the kernel modules before Docker — see above — not regenerating the spec.

### A worker is alive but renders are slow or failing

```bash
make logs W=s2
```

Containers silently falling back to CPU is a known failure mode after a host
`systemctl daemon-reload`; `make restart W=s2` fixes it. Out-of-memory contention shows up
as failed scene tasks that succeed on retry — reduce concurrency by removing a worker from
`comfy_workers` temporarily, or lower the resolution.

## Renders

### A render is stuck or phantom

State lives in three places — the queue JSON, the work directory's `job.json`, and
`orchestrator.sqlite3`. A phantom "already running" lock usually comes from an orphaned
work directory.

1. Open [Render](manual/render.md) and try **Cancel**, then **Retry failed**.
2. If the queue still shows it rendering, `make clean queue` clears the render and publish
   queues (rendered videos are kept).
3. As a last resort, delete the job and its files from the Render screen's **Delete job &
   files**, which clears all three stores for that film.

### Failures keep retrying

Failed queue items auto-retry up to three times when their style has auto-start on — the
retry lives in the automation loop, so with auto-start off, retry them by hand. The
**Render** screen's task list shows the attempt count and the error for each task.

### The estimate says "rough"

The first render on a given setup has no timing history. ETAs sharpen once a few films
have completed with the same worker count and resolution.

### A scene renders but the film doesn't update

The finished film is `~/videos/<name>.mp4`. A sweep rebuilds films whose parts are newer
than the final, once that film's parts have been quiet for ~5 minutes and nothing is
editing or publishing it. If the published cut is a curated version — an upscale, a
localization, or a hand-burnt cover — the sweep never touches it: press
**Reassemble film** in [Edit film](manual/edit-film.md) to rebuild it anyway.

## Publishing

### Nothing publishes even though films are finished

Check, in order:

1. **Settings → Automation** — is *Require approval before publishing* on? Approve the
   film in [Films](manual/films.md).
2. **Publishing → Schedule** — is the film waiting on its channel's cadence?
3. The style's **channel** — a style with no channel never uploads. See
   [Settings → Styles](manual/settings.md#styles).

### YouTube or X authentication fails

Re-run the OAuth connect from **Settings → Channels**. Tokens are per channel, stored as
`~/.config/video-generator/*_token.json`. Full setup: [YouTube](youtube_setup.md),
[X](x_setup.md).

### X refuses a long video

The X API cannot post video longer than 2 minutes 20 seconds. Longer films fall back to
posting the YouTube link — there is no other path.

## Still stuck?

Open an issue with `make status` output and the relevant lines from `make logs W=<host>`:
[github.com/pizzato/stephen_spielbot/issues](https://github.com/pizzato/stephen_spielbot/issues).
