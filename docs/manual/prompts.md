# Prompts

`#/prompts` — opened from **Settings → Infrastructure → Prompts**

The raw instructions sent to the language and image models behind every generation:
scripts, narration, image prompts, descriptions, tags, and replies.

It's deliberately off the sidebar. A bad edit here degrades every future video, so each
prompt is **read-only until you unlock it** and saving asks for confirmation.

## How overrides work

The app's packaged `prompts.yaml` is the baseline and is **never written to**. Your edits
are stored as a sparse override at `~/.config/video-generator/prompts.yaml`, merged per
field.

Two consequences worth knowing:

- **Revert to original** always has somewhere to go — the shipped wording is still there
- **Prompts you haven't edited keep improving with app updates**, because you're not
  holding a stale copy of them

Your overrides are included in a full [settings backup](settings.md#backup-restore).

## Editing a prompt

Each card expands to show its fields:

| Field | What it is |
|---|---|
| **System** | The role and rules the model is given |
| **User** | The request itself, filled in per video |

**Edit prompt** unlocks them. **Save** confirms first; **Revert to original** restores the
shipped text for that prompt. **Revert all to original** at the top clears every override.

## Placeholders

`${...}` tokens are filled in by the app at generation time — the topic, the scene list,
the style's instructions, and so on. Each prompt lists the ones it fills in.

Drop a token from a prompt and it turns **red and struck through**: the app will stop
passing that content to the model, and the save dialog warns you explicitly. This is the
most common way to quietly break generation, so read the warning.

## What to edit here vs. in a style

Reach for a [style](settings.md#styles) first — **extra script instructions**, **script —
avoid**, **title style**, **visual style**, and **video / motion style** all steer
generation without touching the machinery, and they're per style rather than global.

Edit prompts here when you want to change the *structure* of what the model is asked for,
not the flavour of one channel's output.
