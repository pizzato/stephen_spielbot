# Home

`#/`

The studio dashboard. One question at the top, a live feed underneath, and shortcuts to
everywhere else.

## New film

The big input is the fast path: type a topic, press **Start** (or Enter), and you land in
[Create](create.md) with the topic filled in. Four suggestion chips — *Roman Empire*,
*Deep sea*, *How bread is made*, *The cold war* — fill the box if you just want to see
what the thing does.

## Activity

A compact feed of what the studio is doing right now, polled every few seconds.

- Running operations appear with a spinner, a detail line, an ETA, and a percentage.
- Below them sit the last five completed steps with how long each took.
- The chip in the header reads **Running** or **Idle**, plus a "N left" estimate while a
  render is in progress.

**Full activity →** opens the [Activity](render.md#activity) screen, which shows every
concurrent operation grouped by film rather than the top few.

## Recent films

The five most recently finished films, newest first — [archived](films.md#the-cards)
films are skipped. **All films →** opens [Films](films.md).

## The tiles

| Tile | What it shows |
|---|---|
| **Queue** | How many requests are waiting to render — opens [Queue](queue.md) |
| **Community** | Opens [Community](community.md) for comments and mentions |
| **Create** | Opens [Create](create.md) with a blank brief |
| The Spielbot card | Opens **About** (`#/about`) |

## About

Reachable from the Spielbot card here or the handle at the bottom of the sidebar. It
covers what the app is, the models behind it, links to the
[write-up](https://medium.com/@pizzato/i-will-never-direct-a-movie-again-65bd4e9e6797) and
[video](https://www.youtube.com/watch?v=1XMU1_QnRa4) about the project, and the list of
channels using it — generated from
[`channels.yaml`](https://github.com/pizzato/stephen_spielbot/blob/main/channels.yaml). To
add yours, see [Contributing](../contributing.md#adding-your-channel).
