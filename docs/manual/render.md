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

On a [music video](../performance_films.md), each take carries the slice of the song it
performs, so playing a tile plays the performance with its music — that is how the
alignment is checked while the render is still going. The wall also carries the track on
its own: the **whole song** above the tiles (it exists before the first take does), and
under each singing tile that take's slice (`♪ song 12.0s–22.0s`), for hearing the window
by itself.

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
the remaining time, and **Open render**. The banner follows the film whose render process
is actually running — creating another film while one renders doesn't steal it. A film
whose script or song is still being written before its render starts appears in its own
group as **Render queued** until the render takes over.

Under the banner, **Also in flight** lists every other film with work on a worker — one
line per film showing its current step (writing the song, re-voicing, a final upscale)
with its progress or elapsed time and an open button, so anything going on can be
followed from the top of the screen. When nothing is rendering but other work is live,
the same card appears as **Happening now**.

### In the queue

A worker runs one job at a time, fleet-wide, so anything asked for past the number of
free workers is accepted and waits. **In the queue** lists exactly what is waiting — the
step, the film, and how long it has been queued — longest wait first, which is roughly
the order the work will start. The counter beside the title splits the two: *N running*
is work on a worker right now, *N queued* is work behind it. The card is only there when
something is waiting; nothing is lost, and nothing is running twice on one GPU.

Films whose render hasn't begun (script or song still being written) queue here too, as
**Render queued**.

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
hourglass, read *waiting for a free worker*, and have no ETA counting down until they are
on a GPU. Completed entries show how long they took and when — and whether they failed.

The song studio's slow steps appear here too, grouped under their film: **Singing the
song** while the music engine renders the track, **Re-voicing the song as &lt;voice&gt;**
while seed-vc converts it, and the same for a music video written unattended by
automation. So do the character portraits painted after a script is written.

This is the screen to open when several films are rendering at once and you want to know
which worker is doing what.
