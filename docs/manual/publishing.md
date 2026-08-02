# Publishing

`#/publish` — one film: `#/publish/<film>`

Two tabs: **Schedule** (the publish queue) and **Publish a film** (the upload form).

Before anything can post, connect a channel — see [YouTube setup](../youtube_setup.md) and
[X setup](../x_setup.md).

---

## Publish a film

The manual upload form.

### What to publish

- **Film** — pick any finished film
- **Version** — which final cut. A [localized](edit-film.md#localizations) version swaps in
  its translated title and description; an upscale swaps in the upscaled cut
- **Publish to** — YouTube, X, or both. YouTube uploads go to the channel prefilled from
  the film's style, overridable here

### Metadata

- **Title** — max 100 characters, with a regenerate button (*Shorter / Punchier / More
  literal*)
- **Description** — **Generate** writes one from the script
- **Category** and **Privacy** — privacy defaults to `private`; raise it deliberately
- **Thumbnail** and **cover phrase** — the phrase is the short text painted on the cover
  and burned into the first frame
- **First frame** — the burn mode for this film, same control as
  [Edit film](edit-film.md#first-frame)

### What happens automatically

- **Captions** — the script's SRT is attached on upload, in the film's language
- **Tags** — the LLM's topic tags become YouTube tags and X hashtags
- **Playlist** — the style's playlist, if it has one (`__auto__` finds or creates one named
  after the style)
- **Content Credentials** — with `c2patool` installed, the file is signed as AI-generated
  as the last step before upload

### Best time to post

When the [predictive model](analytics.md#predictive-model) has data, a weekday × hour
heatmap suggests when to post, with the best slot called out. It's **advisory** — uploads
still post immediately, and the times are UTC.

!!! warning "X can't take long video"
    The X API cannot post video longer than 2 minutes 20 seconds. Longer films fall back to
    posting the YouTube link; there is no other path.

---

## Schedule

The **publish queue** is always on. Every finished, unpublished film collects here —
publishing one manually removes it.

What happens to the queue depends on
[Settings → Automation](settings.md#publishing-schedule):

| Setting | Behaviour |
|---|---|
| Nothing enabled | Films wait here until you publish them by hand |
| *Auto-post the moment a film finishes* | They post as soon as they render |
| *Publish on a schedule* | They're released on each channel's and account's own **Videos per day** cadence |

The last two are mutually exclusive.

### The counters

**Queued** (waiting on cadence) · **Publishing** (uploading now) · **Published**
(released).

### Waiting & in progress

The queue in release order — top first, as each cadence allows.

- The arrows reorder it, in **Manual order** sort. The other sorts — newest, oldest,
  interestingness, predicted views — are view-only, and the arrows disable while one is
  active
- **Publish now** releases an entry immediately, ignoring the cadence
- **Remove** drops it from the queue

**Scan for unpublished** pulls in the backlog — every finished film that never made it into
the queue. **Cadence settings** jumps to Settings.

### History

Published, skipped, and errored entries.

### Comment requests

By default, videos made from an approved [viewer request](community.md) **skip the
schedule** and post immediately, so the requester isn't left waiting on cadence. Turn that
off in Settings if you'd rather they queue like everything else.

### Approval gate

With *Require approval before publishing* on, finished films are held until you approve
them in [Films](films.md) — the scheduler won't release an unapproved film. Comment-
requested videos still post automatically.

There's a deliberate escape hatch: *…but let automation publish them without waiting for
approval* releases films on cadence while still showing them as unapproved. Turning it back
off re-holds anything not yet published.
