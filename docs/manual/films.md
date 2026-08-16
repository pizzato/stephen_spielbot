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
| **All** | Everything finished |
| **Needs approval** | Held by the [publish approval gate](settings.md#publishing-schedule) |
| **Approved** | Approved, waiting on cadence or a manual publish |
| **Published** | Live somewhere |
| **Not published** | Finished, no approval gate, not published |

## The cards

Each film shows its cover, its title, and a state chip — **Published**, **Needs
approval**, **Approved**, or **New** for a film you haven't opened yet. Its resolution sits
in the opposite corner, which is how two renders of the same script
([Render at another size](edit-film.md#render-at-another-size)) tell themselves apart.

Published films list their destinations as chips (YouTube, X, with the channel name) that
link straight to the live post.

| Button | Effect |
|---|---|
| **Edit** | Opens [Edit film](edit-film.md) — also what clicking the card does |
| **New version** | Clones the film into a fresh work folder, so you can re-render variations without overwriting |
| **Approve** | Releases a held film to publish on its normal schedule |
| **Publish** | Opens [Publishing](publishing.md) for this film |

!!! note "New version vs re-render"
    **New version** duplicates into a *new* work folder. Re-rendering from
    [Edit film](edit-film.md) overwrites in place, keeping the same job id and published
    path.

## Where the files are

Work directories live at `~/videos/<slug>-<timestamp>/`. The published final is
`~/videos/<name>.mp4` — a sweep keeps it in step when a film's parts change, but only while
the studio is quiet so it never competes with an active render.
