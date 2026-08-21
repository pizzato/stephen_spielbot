# Community

`#/community`

Comments and mentions from every connected channel, ranked, with the ones asking for a
video turned into queue items.

Switch between **YouTube** and **X** at the top. With several accounts connected, a chip
shows how many, and each card is tagged with the channel it came from.

Next to the platform toggle, a status filter and a **channel/account** dropdown narrow the
cards: **Needs action** (requests awaiting a decision, reply drafts awaiting a send),
**Requests**, **Approved**, **Rejected**, **Replied** — each with a count, and a comment
can match more than one (an approved request is still a request). Filters combine —
platform included — and live in the URL (`#/community?platform=x&status=action`), so Back
returns to the same filtered view. Switching platform clears the channel filter, since the
ids belong to the other platform.

!!! note "Reading mentions on X needs a paid tier"
    Posting works on the free X API tier. Fetching mentions, replies, and analytics does
    not — see [X setup](../x_setup.md).

## Fetch & evaluate

Pulls the latest comments and asks the LLM to classify each one: is this a **request** for
a video, and if so, how interesting is it?

The same step runs on a schedule when *Fetch & evaluate comments on a schedule* is enabled
in [Settings → Automation](settings.md#automation).

## Requests

A card marked **Request** carries:

- The comment text, with any existing reply thread underneath
- A **star rating** (interestingness) and a **confidence** percentage
- A **suggested scene count** and size tier
- A one-line italic reason — why the AI read it as a request

Three actions:

| Action | Effect |
|---|---|
| **Approve → queue** | Adds it to the [render queue](queue.md), using the editable **Title for the queue** |
| **Reply** | Opens the composer to answer without queueing anything |
| **Reject** | Declines the request |

Comment-sourced items rank above AI ideas in the queue's default order, and by default
[skip the publish schedule](publishing.md#comment-requests) so the requester isn't left
waiting.

With *Auto-approve requests above the confidence threshold* on, requests clearing the
threshold queue themselves.

## Non-request comments — engagement replies

Comments that aren't requests get a **suggested reply** drafted for you.

- Edit it inline, or regenerate it with an instruction — *Shorter*, *Warmer*, *Funnier*,
  *More formal*
- **Send reply** posts it, **Dismiss** drops the draft
- Replied and dismissed comments keep a chip, and you can still reply manually afterwards

Drafting is per channel: each channel has its own **community engagement prompt** and its
own *auto-respond* switch in [Settings → Channels](settings.md#channels).

Threads re-open when a viewer replies again, so an ongoing conversation comes back to your
attention rather than being marked done forever.

!!! info "Replies are matched strictly"
    A reply is tied to its queue item by id, never by title. Two videos with the same title
    can't get each other's replies.

## Completion replies

When a comment-requested video is published, the requester gets a reply pointing at it
automatically. If that send fails, the queue row offers **Retry reply** — see
[Queue](queue.md#row-actions).
