# AI ideas

`#/ideas`

Choose **Topic Ideas** for AI-generated topics or **News** for ideas drawn from the X
posts your styles monitor. Each tab has its own ideas, accepted and declined lists.
Accept the ones you like, decline the ones you don't, and the generator steers
accordingly next time.

## The three views

Within either tab, a segmented control switches between three views. Counts include
only that tab's ideas for the selected style:

| View | What's in it |
|---|---|
| **Ideas** | Fresh suggestions waiting for a verdict |
| **Accepted** | Topics you kept, waiting to be queued or created |
| **Declined** | Topics you turned down — kept out of future suggestions |

## Generating ideas

In **Topic Ideas**, pick a **style** — ideas are generated for that style's channel and voice, and pitched to
suit its [default format](settings.md#script-content): a music-video style is offered
topics that make good songs, an acted style topics that play as scenes. With more than
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

## News monitor

Open **AI ideas → News** to review news ideas and use the **News monitor** panel. The
News tab loads saved news ideas without generating general topics. Its **All styles**
option shows news from all styles, including styles excluded from automatic topic
selection. The Topic Ideas guidance box and generation buttons remain in Topic Ideas.

The monitor panel shows the selected style's monitoring state, search query, interval,
last check, last successful check and any error. It also reports posts fetched, new
posts and ideas added, distinguishing an empty X search from already checked posts,
duplicate ideas and a generation failure. If you edit the query, results from the
previous query remain labelled until the next check.

**Check news now** checks enabled monitors immediately. Scheduled checks continue
while the backend is running; servers with background jobs disabled show **Manual
checks only**. Missing search credentials or an account-selection requirement are
shown explicitly and disable the check button. A failed check shows the provider
error instead of reporting zero ideas as a successful check.

Enable monitoring, set the query and choose automation in
[Settings → Styles → News monitoring](settings.md#news-monitoring). Choose a connected
X account for search, or provide an optional standalone bearer token, in
[Settings → Channels → X → X account for all news searches](settings.md#x-news-search).
This global account is shared by every style, regardless of its publishing account.
Switching styles changes the displayed ideas and monitor query; the search account
stays the same. A sole connected account is used
automatically when no bearer token is saved. Connecting with your Client ID and
Client Secret supplies renewable account credentials; the X app still needs access
to recent search.

News ideas carry a **From X news** label, a summary, links to the source posts and
linked articles, and the named people involved. These links are evidence for the idea;
an X post alone does not establish that a claim is true. The appearance option records
whether reference pictures should be sought when the idea becomes a film.

Expand **Complete directions** on a news idea to read its self-contained production
brief. It includes the reported event, source attribution, available dates and details,
people's reported roles, the style's creative treatment and the collected post text.
Music briefs include a song premise, point of view, hook and verse/chorus progression.
Create's **Direction** field and queued news prompts carry this complete brief, including
when ideas are accepted and queued automatically. Existing saved ideas also include
their saved source text without needing a new search.

Each check makes one recent-search request for up to 50 recent posts. X recent search
covers the last seven days; a busy query can miss older matches between polls.
Long posts use the extended text returned by X when available.
Linked article URLs are retained for review, but the monitor does not fetch or verify
the article contents. Its summaries and ideas are based on the retrieved post text.
Directions mark those links as unread citations and tell the writer to work only from
the included material, with no browsing or research step. Missing details stay unknown;
the idea generator is instructed to skip posts that lack enough detail to make a video.
Reference photographs, when enabled and found, are attached by the app as character
assets; the writer is not asked to find images or invent a missing person's appearance.

Within the News tab, news follows the same Ideas → Accepted → Queue/Create flow.
These lists stay separate from Topic Ideas when accepting, declining or reviving an
idea. If automatic acceptance is
on, ideas arrive in Accepted. If automatic queueing is also on, they are marked Queued.
You can enable queueing alone and accept ideas manually; the next monitor check queues
those accepted ideas, using the size saved when accepting the idea (Small by default).
The source context and appearance setting travel with the idea through both Queue and
Create. A music-video style uses the news as context for its song.

## The Accepted list

Split into **Not created yet** and **Acted on**, so you always know what's still waiting.

Each row keeps its own size choice and offers:

- **Queue** — adds it to the [render queue](queue.md) with that size's length and resolution
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

With *Top up the empty queue with AI ideas* enabled for a style in
[Settings → Automation](settings.md#what-automation-makes), the studio invents its own
topics for it when the queue empties, rotating across every style that asks to be fed.
A style can also leave the rotation by unticking **Include in auto-picked ideas** in
[Settings → Automation](settings.md#what-automation-makes).

Invented films render without review, so a style is only fed when its own automation also
auto-approves scripts and auto-starts the queue — a style in review mode never receives
invented ideas.

News monitoring has separate acceptance and queueing toggles, so it can supply topics
without enabling invented AI queue top-ups. To create news videos without review, use
**Enable unattended news videos** in the style's news-monitor panel, then save settings.
It enables the required script, queue and (for music videos) song approval steps.

## How dedup works

The generator is given your recent titles and the accepted/declined/ignored lists, so it
avoids repeating what you've already made or rejected. Titles are cached briefly, so two
generations a minute apart won't produce the same batch.
