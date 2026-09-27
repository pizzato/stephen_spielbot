# AI ideas

`#/ideas`

Choose **Topic Ideas** for AI-generated topics or **News** for ideas drawn from the
subjects your styles monitor through web research or optional X post search. Each tab has its own ideas, accepted and declined lists.
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

The monitor panel shows the selected style's check mode, subjects, daily cadence,
last check, last successful check and any error. For LLM research it also shows the
shared provider/model, new research checks used against the UTC daily limit, the number
of cited web sources, whether saved research was reused, and ideas added. It distinguishes
no recent news, previously checked sources, duplicate ideas and failures. With the
optional X source, it reports posts fetched and new posts instead. If you edit the
subject, results from the previous subject remain labelled until the next check.

**Check news now** checks enabled monitors immediately, bypassing scheduled spacing;
styles set to **Off** are always skipped. **On demand** makes no automatic searches.
**When opening AI Ideas** checks all styles using that mode once per visit to AI Ideas,
even if you start on Topic Ideas. Switching tabs or styles, reading status, and leaving
the page open do not trigger further searches. Visits within one minute are debounced.
**Scheduled** defaults to one check per day, evenly spaced like publishing; it continues
while the backend is running. Servers with background jobs disabled show the schedule
as paused; on-demand and page-visit checks still work. Missing research credentials or
unsupported local research configuration are shown explicitly and disable the check
button. A failed check shows the provider error rather than reporting an empty success.
Existing minute-based monitors become **On demand** on upgrade; select **Scheduled**
explicitly to restart automatic checks.

### Web research

**LLM web research** is the default source. Choose a provider, optional model override,
country, lookback window and daily limit in
[Settings → Infrastructure → News research](settings.md#news-research). It reuses your
existing OpenAI, Claude or Grok API key. Local models cannot run the web-research step;
you can use a cloud provider for research while retaining a different backend for
scripts. Grok research enables web tools only, without reading X posts.

Set plain-language subjects such as *Medicare and public healthcare in Australia* or
*OpenAI announcements*, and choose when to check, under
[Settings → Styles → News monitoring](settings.md#news-monitoring). Research asks for
recent substantive developments in the selected country and time window, with event
dates, named people and roles, relevant figures, attribution and uncertainty. The
provider must run a web search; a model answer from memory is not accepted as research.
Source citations and the resulting factual brief are saved before creative ideas are
generated for each style.

Matching subjects with the same research settings reuse a durable one-hour cache
across styles and repeat manual checks. Failed attempts have a five-minute cooldown.
The default global cap is **three new research checks per UTC day**, including failures.
Reusing saved research and generating creative ideas from it do not consume another
research check. Those creative calls still have their own LLM usage. **Check news now**
does not bypass the cache, cooldown or daily cap. One research check may involve
multiple web-tool calls and tokens; the daily count is not a fixed spending budget.
The cache and per-attempt usage audit survive restarts; see
[configuration](../configuration.md#shared-web-research).

Web-researched ideas carry **From web research**, a summary, named people, links to
actual cited websites and an expandable **Saved research brief**. Review the sources
and uncertainty before accepting an idea. A source link is evidence to assess, not an
independent guarantee that every generated statement is accurate.

### Complete production directions

Expand **Complete directions** on a news idea to read its self-contained production
brief. It includes the reported event, source attribution, available dates and details,
people's reported roles, the style's creative treatment and the saved research or post
text. Music briefs include a song premise, point of view, hook and verse/chorus
progression. Create's **Direction** field and queued news prompts carry this complete
brief, including when ideas are accepted and queued automatically. Existing saved
ideas also include their saved source text without needing a new search. Story and
song writers use the included material without a later browsing step; missing details
remain unknown. The appearance option travels with the idea.

### Optional X post search

Select **X post search** under
[Settings → Infrastructure → News research](settings.md#news-research), then choose the
shared account or optional bearer token under
[Settings → Channels → X](settings.md#x-news-search). This account is shared by every
style regardless of its publishing account. Each style's subject field becomes an X
search query. X post reads use X API credits; the LLM research daily cap and cache do
not apply to this source.

X ideas carry **From X news**, links to source posts and linked articles, and the named
people involved. Each check makes one recent-search request for up to 50 posts.
X recent search covers the last seven days; a busy query can miss older matches
between polls. Long posts use extended text when X supplies it. Linked article URLs
are retained for review, but X mode does not fetch or verify the articles. Its summaries
and ideas are based on retrieved post text. Directions mark those article links as
unread citations. Posts without enough detail should not become video ideas, and a post
alone does not establish that a claim is true.

### People and video creation

When appearance references are enabled, the app creates characters for the named
people, including people mentioned by surname or nickname. It attaches reference
photographs when found and uses the best available identity match, with a warning
when uncertain. If lookup fails, a named character and best-guess description still
let automatic portrait generation proceed. Generated portraits are not verified
likenesses. The writer is not asked to fetch images; optional review is available in
**Characters & Artifacts**.

Within the News tab, news follows the same Ideas → Accepted → Queue/Create flow.
These lists stay separate from Topic Ideas when accepting, declining or reviving an
idea. If automatic acceptance is
on, ideas arrive in Accepted. If automatic queueing is also on, they are marked Queued.
You can enable queueing alone and accept ideas manually; the next monitor check queues
those accepted ideas, using the size saved when accepting the idea (Small by default).
The source context and appearance setting travel with the idea through both Queue and
Create. A music-video style uses the news as context for its song.

For news music videos, the app prepares the news characters **before** writing the
song. The first available character in the brief's principal-subject order becomes
the lead singer, ahead of the style's recurring performers. That choice and its
photograph or generated portrait follow the song through story drafting, redrafts and scene
division, including unattended creation. Other news people can appear as supporting
cast. If no news character is available, the usual style-catalogue selection applies.
You can change the lead in the [Song tab](script.md#song).

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
