# Channel Analytics

`#/analytics`

Two tabs: **Analytics** (how your channels are actually doing) and **Predictive Model**
(what the app thinks a new idea will do).

---

## Analytics

Pick **YouTube** or **X**, then the channel or account — a picker appears when more than
one is connected.

You get the channel's headline stats and a per-video table pulled from the platform's API.
**Refresh** re-fetches; results are cached between visits so switching tabs is instant.

Reading X analytics requires a paid API tier — see [X setup](../x_setup.md).

---

## Predictive Model

`#/engagement`

One model, built from *all* your channels' history, that estimates how many views an idea
will get in its first N days. Once built, its predictions appear:

- On [Create](create.md#predicted-reach), as you type the brief
- On each [AI idea](ideas.md) card, and as a sort option
- On the [Queue](queue.md) rows
- On [Publishing](publishing.md#best-time-to-post), as best-time-to-post guidance

### Building it

With no model, the screen offers **Build model**. It reads your published history, embeds
the titles and descriptions, and fits a model — with a progress bar through the phases.

A model built by an older library version or feature set says so and offers **Rebuild
model**.

Three settings in [Settings → Infrastructure](settings.md#predictive-model) shape it:

| Setting | Meaning |
|---|---|
| **Prediction horizon (days)** | The window being predicted — "views in the first N days" |
| **Minimum training samples** | How many videos before the model is considered usable |
| **Data lag / exclusion (days)** | Videos newer than this are excluded, since they have no full window yet |

### Reading the evaluation

| Metric | What it means |
|---|---|
| **Typical error** | Mean absolute error in views, next to what guessing the average would cost you |
| **Correlation** | Actual vs predicted, where 1.0 is perfect |
| **Beats guessing?** | Whether the model is more useful than the channel average |
| **Trained on** | How many videos went in, and how many were excluded |

**Held-out accuracy** plots each video as predicted by a model that never saw it
(cross-validation) — the honest picture.

The footer records when it was built, which embedding model it used, how many Shorts were
in the sample, and the timing model's correlation, which is what drives the best-time
guidance on the Publish tab.

### Try an idea

Type a **title**, optionally a **description**, and pick **Long-form** or **Short** —
Shorts and long-form get very different reach — for an instant prediction with its
reliability rating.

!!! note "It's a prior, not a verdict"
    Reliability is reported honestly: with too few samples, or a model that doesn't beat
    guessing, the app says *rough estimate* rather than pretending. Treat a low-reliability
    number as a tiebreak, not a decision.
