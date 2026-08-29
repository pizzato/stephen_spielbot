# Films

`#/films`

Your library. Everything that finished, plus anything still in flight.

## Active and unfinished

Above the library, films that are rendering or need attention.

- **Rendering** → **View render** opens [Render](render.md)
- **Needs attention** → **Continue** resumes it

**Delete** (with a confirm) removes a partial render and its files, cancelling it first if
it's running.

## Filters

The segmented control filters by publish state, and each option carries a count. Buckets
only appear once they have films, so a channel with no approval gate never sees the
approval filters.

| Filter | Meaning |
|---|---|
| **All** | Everything finished (except archived) |
| **★ Starred** | Films you starred |
| **Needs approval** | Held by the [publish approval gate](settings.md#publishing-schedule) |
| **Approved** | Approved, waiting on cadence or a manual publish |
| **Published** | Live somewhere |
| **Not published** | Finished, no approval gate, not published |
| **Archived** | Archived films — hidden from every other view |

Next to it, **channel** and **style** dropdowns narrow the library to one channel or one
style profile; they only appear once your films span more than one. Filters combine —
publish state, channel and style all apply at once, and the status counts follow the
narrowed set. The active filters live in the URL (`#/films?status=published&channel=…`),
so the browser's Back button returns you to the same filtered view, and a filtered URL
can be bookmarked or shared. Clicking **Films** in the sidebar starts fresh, unfiltered.

## The cards

Each film shows its cover, its title, and a state chip — **Published**, **Needs
approval**, **Approved**, or **New** for a film you haven't opened yet. Its resolution sits
in the opposite corner, which is how two renders of the same script
([Render at another size](edit-film.md#render-at-another-size)) tell themselves apart.

The **star** next to the title marks a favourite — starred films get their own **★
Starred** filter. Click it again to unstar.

Published films list their destinations as chips (YouTube, X, with the channel name) that
link straight to the live post.

| Button | Effect |
|---|---|
| **Edit** | Opens [Edit film](edit-film.md) — also what clicking the card does |
| **New version** | Clones the film into a fresh work folder, so you can re-render variations without overwriting |
| **Rename** (pen) | Edits the film's display title in place — Enter saves, Esc cancels |
| **Archive** (box) | Tucks the film away under the **Archived** filter without deleting anything; **Unarchive** brings it back |
| **Approve** | Releases a held film to publish on its normal schedule |
| **Publish** | Opens [Publishing](publishing.md) for this film |

!!! note "What renaming changes"
    Renaming sets the film's *display title* — the name on the card and the title used
    when publishing (the same one the [Publishing](publishing.md) screen edits). The work
    folder and the final video file keep their original names, so published links, deep
    links and the durable job record are untouched.

Archived films stay on disk untouched — they just leave the default view, the Home
screen's recent list, and the auto-publish sweep (a film already queued for publishing
keeps its queue entry). Unarchive at any time from the **Archived** filter.

!!! note "New version vs re-render"
    **New version** duplicates into a *new* work folder, and so does
    [Render at another size](edit-film.md#render-at-another-size) — the new size lands in
    the Library as its own film. Only *per-scene* re-renders from
    [Edit film](edit-film.md) overwrite in place, keeping the same job id and published
    path.

## Where the files are

Work directories live at `~/videos/<slug>-<timestamp>/`. The published final is
`~/videos/<name>.mp4` — a sweep keeps it in step when a film's parts change, once that
film's parts have been quiet for ~5 minutes and nothing is editing or publishing it. The
sweep never touches a film whose published cut is a curated version (an upscale, a
localization, or a hand-burnt cover) — use **Reassemble film** in
[Edit film](edit-film.md) for those.
