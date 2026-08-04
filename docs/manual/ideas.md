# AI ideas

`#/ideas`

Topic suggestions for your channel, with a memory. Accept the ones you like, decline the
ones you don't, and the generator steers accordingly next time.

## The three views

A segmented control switches between them:

| View | What's in it |
|---|---|
| **Ideas** | Fresh suggestions waiting for a verdict |
| **Accepted** | Topics you kept, waiting to be queued or created |
| **Declined** | Topics you turned down — kept out of future suggestions |

## Generating ideas

Pick a **style** — ideas are generated for that style's channel and voice. With more than
one style, **All styles (mix)** shows a blended view and tags each card with its style.

The free-text box guides the batch: *"Rock bands of the 90s"*. Leave it empty and the
button reads **Generate more**; type something and it becomes **Generate ideas**.

**Sort** reorders the cards without dropping any — newest, oldest, most interesting, or
predicted views.

## An idea card

Each card carries:

- The **title** and a one-line italic *reason* — why the AI thinks it fits your channel
- A **star rating** (interestingness) and, when a model exists, a **predicted reach** chip
- A **size** — Small / Medium / Large — which sets the video length and resolution from
  that style's [size presets](settings.md#size-presets). The line underneath shows exactly
  what you'll get: *"1 min · Portrait"*

Three verdicts:

| Button | Effect |
|---|---|
| **Accept** | Moves it to the Accepted list, ready to queue or create |
| **Decline** | Moves it to the Declined list — the AI steers away from this topic |
| **Ignore** | Hides it for good, without adding it to Declined |

All three keep the topic out of future suggestions.

## The Accepted list

Split into **Not created yet** and **Acted on**, so you always know what's still waiting.

Each row keeps its own size choice and offers:

- **Queue** — adds it to the [render queue](queue.md) with that size's scenes and resolution
- **Create** — opens [Create](create.md) prefilled, for a brief you want to hand-tune
- **Decline** — moves it to the Declined list
- **Remove** — drops it from the list entirely (it may resurface organically later)

Queueing or creating doesn't remove the idea; it stamps it as acted on and leaves it
listed.

## The Declined list

- **Accept** — move it over to Accepted
- **Revive** — put it back among the active ideas
- **Forget** — remove it from the list for good

Ignored ideas stay hidden and never appear here.

To let *every* declined topic resurface, use **Clear declined ideas** in
[Settings → Automation](settings.md#automation).

## Automation

With *Top up the queue with an AI idea when it runs empty* enabled in
[Settings → Automation](settings.md#automation), the studio invents its own topics when
the queue empties, rotating across styles. A style can opt out of the rotation with
**auto-pick exclude** in [Settings → Styles](settings.md#styles).

That toggle needs auto-approved scripts, since nobody is there to review them.

## How dedup works

The generator is given your recent titles and the accepted/declined/ignored lists, so it
avoids repeating what you've already made or rejected. Titles are cached briefly, so two
generations a minute apart won't produce the same batch.
