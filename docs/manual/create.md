# Create

`#/create`

Set the brief for a new film. Pressing **Generate script** costs one LLM call and produces
a script you then review — no GPU work happens here.

## Style

The first choice, because it decides the others. A **style** owns the narrator voice,
visual direction, script mode, render quality, audio mix, and which channel the film
publishes to. Pick one and the fields it owns lock to it — the hint under the picker shows
the style's description.

Child styles are shown indented under their parent (`↳`), and the default style is marked.

**No style — experiment** unlocks the narrator voice, visual style, and script mode so you
can try something one-off. Render quality and audio mix still come from the default style.
Switching to it clears the previous style's imprint rather than inheriting it, so you start
clean.

Manage styles in [Settings → Styles](settings.md#styles).

## Title and Direction

**Title** is what the film is about. **Direction** is optional and steers the angle, tone,
or emphasis — *"Focus on the economic decline, the military overreach, and the slow rise of
Christianity."*

Both labels carry a regenerate button: it rewrites the field with the LLM, and you can give
it a free-text instruction or use the chips — *Shorter*, *Punchier*, *More specific* for
the title; *Sharper angle*, *More detail*, *Simpler* for the direction.

## Length and Resolution

**Length** is a slider in minutes. The script's word budget comes from the narrator's
cadence (words per minute — measured per voice, see Settings → Voices), and the story is
divided into scenes of 10–15 seconds each; the hint under the slider shows the estimated
word count and scene count live. Picking a style prefills its default length.

**Resolution** picks orientation first, then quality — higher is slower. Portrait means the
film is treated as a Short, which the predictive model weighs differently.

## Visual style

Free text appended to every scene's image prompt — *"Cinematic 35mm, golden hour, painterly
lighting"*. Locked while a style is active.

## Narrator voice

The cloned voice that reads the narration. Locked while a style is active. Voices come from
the bundled LibriVox library plus anything you've recorded or uploaded in
[Settings → Voices](settings.md#voices).

## Format

| Format | What you get |
|---|---|
| **Narration** | Classic voice-over. The mature default path |
| **Dialogue** | Characters speak their lines, lip-synced. Needs characters with a portrait and a voice |
| **Mixed** | The AI blends narration, dialogue, and silent scenes |

See [dialogue scenes](../dialogue_scenes.md) for what the modes actually render.

## Script mode

| Mode | How the script is written |
|---|---|
| **Classic** | Scenes are generated directly, in batches |
| **Story-first** | The LLM drafts and critiques the whole story as prose, shows it to you for review, then divides it into scenes |

Story-first keeps long videos coherent — it's the mode to reach for above ten or so scenes.
It's narration-only for now: Dialogue and Mixed always run Classic, and the backend enforces
the same rule.

Like the voice and visuals, script mode is owned by the style unless you're on *No style*.

## Auto-approve

**Auto-approve the script → send straight to the queue** skips the [Script](script.md)
review entirely: the draft goes to the queue ready to render. Useful once you trust a
style; not what you want on a first run.

Story-first replaces this checkbox with a note, because reviewing the story *is* the next
step.

## Predicted reach

If the [predictive model](analytics.md#predictive-model) has been trained, a card estimates
the film's views in its first few days from the title, direction, and format. It updates as
you type. With too little training data it says *rough estimate*; with none, the card
simply doesn't appear.

## Generating

**1. Generate script** (or **1. Draft the story** in story-first mode) drafts the script and
moves you to [Script](script.md).

If you arrived here from a queued request, a banner says so — generating fills that
existing queue slot and keeps its position. If you arrived via **Re-draft** from an existing
film, your previous settings are restored and generating creates a fresh work folder.
