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
  its translated title, description, and cover (the same art re-titled in that language);
  an upscale swaps in the upscaled cut
- **Publish to** — YouTube, X, or both. YouTube uploads go to the channel prefilled from
  the film's style, overridable here

### Metadata

- **Title** — max 100 characters, with a regenerate button (*Shorter / Punchier / More
  literal*)
- **Description** — **Generate** writes one from the script
- **Category** and **Privacy** — privacy defaults to `private`; raise it deliberately
- **Thumbnail** and **cover phrase** — the phrase is the short text painted on the cover
  and burned into the opening frames
- **Opening cover** — the burn hold for this film, same control as
  [Edit film](edit-film.md#opening-cover)

### What happens automatically

- **Captions** — the script's SRT is attached on upload, in the film's language.
  Narrated scenes only: a [music video](../performance_films.md#singing-films-the-music-video-format)
  or an all-dialogue film has no narration to caption, so no SRT is attached.
  Styles with [burned-in subtitles](settings.md#cover-first-frame) carry the same
  captions in the picture itself as well
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
    The X API cannot post video longer than 2 minutes 20 seconds, or larger than 512 MB.
    Films over either limit fall back to posting the YouTube link; there is no other path.

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

### Filters

Below the counters, a segmented status filter — **Queued**, **Held** (awaiting approval),
**Publishing**, **Published**, **Errors**, each with a count — and a **channel/account**
dropdown (shown once the queue spans more than one destination). Filters combine and
narrow both sections; the counters follow the channel filter. A film heading to two
platforms can match several status buckets at once — done on YouTube while still queued
on X. Filters live in the URL (`#/publish?status=queued&dest=…`), so Back returns to the
same filtered view. They are view-only — unlike the sort, they never change what the
scheduler releases.

### Waiting & in progress

The queue in release order — top first, as each cadence allows.

- The arrows reorder it, in **Manual order** sort. The other sorts — newest, oldest,
  interestingness, predicted views — are saved and become the real release order (the
  scheduler publishes each channel's top waiting item first); only the arrows disable
  while one is active
- **Publish now** releases an entry immediately, ignoring the cadence
- **Remove** drops it from the queue

Each line shows the channel or account the film will publish to — its style's, looked up
fresh each time the queue loads. Reassign a style to a different channel and everything
still waiting moves with it; already-published entries keep the channel they went out on.

**Scan for unpublished** pulls in the backlog — every finished film that never made it into
the queue. **Cadence settings** jumps to Settings.

### History

Published, skipped, and errored entries.

Every release attempt counts against the cadence — even one that later errors — so a
failure can't make the scheduler release the next film early. Deleting a film in
[Films](films.md) closes out its queue entry: if it already published, the entry stays
here as published (deleting the local film doesn't remove the upload); if it was still
waiting, it leaves the queue.

### Comment requests

By default, videos made from an approved [viewer request](community.md) **skip the
schedule** and post immediately, so the requester isn't left waiting on cadence. Turn that
off in Settings if you'd rather they queue like everything else.

### Approval gate

With *Require approval before publishing* on, finished films are held until you approve
them — in [Films](films.md), or right on this Schedule tab, where a held entry shows an
**Awaiting approval** chip with an inline **Approve** button — the scheduler won't
release an unapproved film. Comment-
requested videos still post automatically.

There's a deliberate escape hatch: *…but let automation publish them without waiting for
approval* releases films on cadence while still showing them as unapproved. Turning it back
off re-holds anything not yet published.
