# Queue

`#/queue`

The video request queue: everything waiting to render, what's rendering now, what's
finished and waiting to publish, and the history.

Requests arrive from [AI ideas](ideas.md), from approved [viewer
comments](community.md), from [Create](create.md), or by hand.

## The counters

**Queued** (waiting to render) · **Rendering** (in progress) · **Ready** (waiting to
publish) · **Posted** (live on YouTube).

## Manual controls

Two on-demand buttons, independent of the always-on automation loop:

- **Start next render** — starts the top eligible item. It reports when there's nothing to
  start, e.g. a render is already running or no approved item has a ready script.
- **Post finished** — uploads every finished, unpublished film.

Hands-free behaviour is configured in [Settings → Automation](settings.md#automation), not
here.

## Rendering now

Active work, separate from the waiting queue: title, percentage, current step, and ETA,
with **View render** to open [Render](render.md).

## Up next

The waiting queue, in the order it will run. Each row shows:

- The **title**, with the **source** (`ai_idea`, `comment`, …) and the **style** it will
  render with
- **Interestingness** stars and a **predicted reach** chip where available
- The target **length in minutes** and, when the timing table knows, an **estimated render time**
- The requesting **commenter**, for comment-sourced items

### Approval state

A pending item with a script shows **Approved** or **Needs review**.

That gate only bites when *auto-start* is on — it decides which ready scripts the loop
picks up. With auto-start off it's just a flag you can pre-set, and **Render now** stays
the primary action.

### Row actions

| Action | When it appears |
|---|---|
| **Edit** | Pending item with no script yet — opens an inline panel |
| **Edit script** | Pending item whose script is ready — opens [Script](script.md) |
| **Approve** / **Unapprove** | Pending item with a ready script |
| **Render now** | Any pending item — starts it immediately |
| **Cancel** | An item that's creating |
| **Publish** | A finished item — opens [Publishing](publishing.md) |
| **Retry reply** | A posted, comment-sourced item whose reply didn't send |
| 🗑 | Removes the item (cancelling it first if it's running) |

### The inline edit panel

For items with no script yet: **Title**, **Prompt / direction**, **Scenes**, **Style**, and
**Resolution**. **Save** keeps the edits in the queue; **Create script →** carries them
into [Create](create.md) and drafts the script now.

### Ordering

The arrows on the left move an item up or down, and the next render starts from the top.

Reordering only works in **Queue order** sort. The **Sort** control also offers newest,
oldest, interestingness, predicted views, and fastest-first — those are view-only
reorderings, and the arrows disable while one is active. Comment requests rank above ideas
in the default order.

## Ready to publish

Finished videos waiting for upload, each with a **Publish** button.

## History

Posted, cancelled, and failed items, dimmed. Failed items auto-retry up to three times
before landing here.
