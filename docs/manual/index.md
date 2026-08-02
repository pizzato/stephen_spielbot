# The manual

Every screen in the web app, control by control. If you've never run a film through,
start with [Your first film](../first-film.md) instead — this section is the reference you
come back to.

The app lives at [http://localhost:8001](http://localhost:8001) and is one hash-routed
page, so every screen has a URL you can bookmark or share (`#/queue`, `#/edit/my-film`).

## The sidebar

The navigation follows the production pipeline — ideation, then production, then
publishing and audience.

<div class="grid cards" markdown>

-   **Ideation**

    [Home](home.md) · [Create](create.md) · [AI ideas](ideas.md)

-   **Production**

    [Queue](queue.md) · [Script](script.md) · [Render](render.md) ·
    [Activity](render.md#activity) · [Films](films.md)

-   **Publishing & audience**

    [Publishing](publishing.md) · [Community](community.md) ·
    [Channel Analytics](analytics.md)

-   **Configuration**

    [Settings](settings.md) · [Prompts](prompts.md)

</div>

Two screens aren't in the sidebar: [Edit film](edit-film.md), reached by opening a film,
and [Prompts](prompts.md), opened from Settings → Infrastructure.

### Sidebar indicators

| Indicator | Meaning |
|---|---|
| **REC** / a percentage on **Render** | A render is running right now |
| **LIVE** / a count on **Activity** | That many operations are in flight |
| A number on **Queue** | Items waiting to render |
| A number on **Community** | Comments or mentions needing attention |
| A number on **Publishing** | Finished films ready to publish |
| A number on **Films** | New films you haven't opened |

## The route map

Useful for deep links and bookmarks.

| Screen | URL |
|---|---|
| Home | `#/` |
| Create | `#/create` |
| AI ideas | `#/ideas` |
| Queue | `#/queue` |
| Script | `#/script/<film>` |
| Render | `#/render/<film>` |
| Activity | `#/activity` |
| Films | `#/films` |
| Edit film | `#/edit/<film>` (`#/remix/<film>` is the same screen) |
| Publishing | `#/publish` · one film: `#/publish/<film>` |
| Community | `#/community` |
| Channel Analytics | `#/analytics` · predictive model: `#/engagement` |
| Settings | `#/settings` |
| Prompts | `#/prompts` |
| About | `#/about` |

`<film>` is the work directory's basename — the readable `<slug>-<timestamp>` id — so a
link reads like `#/edit/roman-empire-20260607`. An unrecognised URL falls back to Home
rather than breaking.

## The shape of a film's life

```
Create ──▶ Script ──▶ Queue ──▶ Render ──▶ Films ──▶ Edit film ──▶ Publishing
  │                     ▲                                              │
  └── AI ideas ─────────┘                        Community ────────────┘
```

Each arrow is a gate you can automate away — see
[Settings → Automation](settings.md#automation) — or keep as a manual review step.
