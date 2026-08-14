# Render & Activity

Two views on work in flight: **Render** follows one film, **Activity** shows the whole
studio.

---

## Render

`#/render/<film>`

The live picture of a single render. You can close the browser — the render is a resumable
subprocess with durable state, not something the page is driving.

### Progress

A percentage, the current step, a per-stage counter, and a progress bar. The header chip
shows the ETA while it's running and **Done** when it finishes, at which point
**Edit film** and **Download** appear.

**Edit script** opens this film's [Script](script.md) at any time.

### Tasks

The [durable task graph](../orchestration.md) for this film — one row per unit of work:
story, each image, each narration, music, each scene video, each mux, and final assembly.

Each row shows its name, its status, and the attempt count (`2/3`). A failed task shows its
error inline. This is the ground truth about what actually ran, and it survives restarts.

### Scenes

The film's scene wall, read straight from the work directory as the render goes — the
same files the [film editor](edit-film.md) shows, without waiting for the render to
finish (and without the editor's re-render buttons getting in an active render's way).

Each scene tile shows the furthest thing that exists on disk so far: the finished clip
(playable in place), else the first frame, else a placeholder — with a stage chip:

| Chip | Meaning |
|---|---|
| **Waiting** | Nothing on disk yet |
| **Voiced** | Narration recorded (an audio player appears on the tile) |
| **Video** | A raw clip exists, not yet muxed with its audio |
| **Rendered** | The scene's final clip is done |

The wall appears as soon as the script has been divided into scenes and refreshes on its
own every few seconds.

### Time estimate

The remaining time, the full-render estimate, and the worker mix behind it
(`3× comfy (+1 held for UI) · 3× tts`). Underneath, a per-stage breakdown of where the
time goes.

The chip reads **learned** once the timing table has data for this shape of render, or
**rough** on a first run — *"First render builds the timing table — estimates sharpen next
time."*

The `(+1 held for UI)` note is the [reserved UI worker](../cluster.md#the-reserved-ui-worker).

### Workers

Every configured worker with its endpoint, kind, and current state, so you can see which
machine is busy and which is idle.

### Thumbnail

The cover as it currently stands.

### Controls

| Button | Effect |
|---|---|
| **Pause** | Stop after the current task |
| **Retry failed** | Re-queue the tasks that failed |
| **Resume** | Continue a paused or interrupted render |
| **Cancel** | Stop the render (sends SIGTERM) |
| **Delete job & files** | Remove the film and its work directory entirely |

Cancel and delete are the ones to reach for when a render wedges — see
[Troubleshooting](../troubleshooting.md#a-render-is-stuck-or-phantom).

---

## Activity

`#/activity`

Everything the studio is doing — renders, upscales, scripts, publishes — with ETAs where
they exist, plus a history of how long each step took.

### Rendering now

When a film render is active, a banner at the top gives it a progress bar, a percentage,
the remaining time, and **Open render**.

### Filters

| Filter | Shows |
|---|---|
| **All** | Everything, live and historical |
| **Live** | Only what's running right now |
| **By film** | Grouped per film, so you can fold away the ones you don't care about |
| **Background** | Automation work — comment fetches, publishes, sweeps — separate from films |

**Expand all** / **Collapse groups** toggles the groups, and **Refresh** re-polls
immediately (it polls on its own anyway).

### Reading it

Live entries carry a spinner, a detail line, an ETA, and a percentage; queued ones show an
hourglass. Completed entries show how long they took and when.

This is the screen to open when several films are rendering at once and you want to know
which worker is doing what.
