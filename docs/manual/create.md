# Create

`#/create`

Set the brief for a new film. Pressing **1. Draft the story** (or **1. Write the song** for
a music video) costs one LLM call and produces a draft you then review — no GPU work
happens here.

## Style

The first choice, because it decides the others. A **style** owns the narrator voice,
visual direction, render quality, audio mix, and which channel the film
publishes to. Pick one and the fields it owns lock to it — the hint under the picker shows
the style's description.

Child styles are shown indented under their parent (`↳`), and the default style is marked.

**No style — experiment** unlocks the narrator voice and visual style so you
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

## Length, Scenes and Resolution

**Length** is a slider in minutes — for a music video it is how long the *song* runs, and
the film runs exactly as long as the song. The script's word budget comes from the
narrator's cadence (words per minute — measured per voice, see Settings → Voices), and the
story is divided into scenes of 10–15 seconds each; the hint under the slider shows the
estimated word count and scene count live. Picking a style prefills its default length.

**Scenes** decides how many scenes that length becomes. Left on *Auto* it's whatever the
length needs at the usual scene size — about 12 seconds of narration each, or one ~10 s
take in a dialogue or silent film. Type a count and the length is divided by it instead, so
**fewer scenes are longer ones** and more scenes are shorter, quicker cuts. The hint under
the box shows what a scene works out to. Picking a style prefills its default
([Settings → Styles](settings.md#script-content)); the count you type here overrides it for
this film only.

A scene can only run as long as the style's video engine holds in a single take — about
40 s on LTX, and 12 s on MiniMax H3 (23 s where the style
[chains its scenes](../performance_films.md)). Ask for a count so low that the scenes
can't fill the length, and **the count wins**: the hint tells you what the film will
actually run.

**Resolution** picks orientation first, then quality — higher is slower. Portrait means the
film is treated as a Short, which the predictive model weighs differently.

## Visual style

Free text appended to every scene's image prompt — *"Cinematic 35mm, golden hour, painterly
lighting"*. Locked to the style's own visual style while a style is active — unless that
style leaves it blank, in which case the field stays free and what you write applies to
this film only.

## Narrator voice

The cloned voice that reads the narration. Locked while a style is active. Voices come from
the bundled LibriVox library plus anything you've recorded or uploaded in
[Settings → Voices](settings.md#voices).

## Format

| Format | What you get |
|---|---|
| **Narration** | Voice-over throughout. The mature default path |
| **Dialogue** | The characters act and speak on screen. Needs characters with a portrait (a voice keeps them consistent) |
| **Mixed** | The AI blends narration, dialogue, and silent scenes |
| **Silent** | Told in pictures: no narrator, and a spoken line only where a beat truly needs one |
| **Music video** | The story becomes a **song**: the AI writes tagged lyrics from the approved draft, the music model sings them over the whole film, and the lead character performs it on camera in silent acted takes. Or [bring your own song](#the-song) and the film is built around that |

See [acted scenes](../performance_films.md) for what the modes actually render, and
[singing films](../performance_films.md#singing-films-the-music-video-format) for the
Music-video format in detail. The picker starts on the style's **default format**
([Settings → Styles](settings.md#script-content)) — switching styles re-seeds it, and you
can still change it freely for this film. Films the queue makes on its own have no-one to
ask, so they use that default as-is; the same goes for ideas you send to Create, until
you pick otherwise.

A dialogue, silent or music-video film is measured in clips rather than words: every scene
is one take of about ten seconds, so the length you ask for becomes a scene count at that
rate — unless you set **Scenes** yourself, which stretches or shortens the takes instead.
For a music video the takes then bend a little around that length so each one starts and
ends **between sung lines** rather than mid-sentence — see
[where the scenes cut](../performance_films.md#singing-films-the-music-video-format).

**The direction box outranks the format's balance.** Whatever you pick, an instruction about
staging — "mostly silent, one exchange near the end", "no narrator" — is followed rather
than the format's own default mix. Use *Mixed* plus a direction when you want a balance no
button describes; use *Silent* when pictures carry the whole film. (Instructions aimed at
the narrator still survive in the formats that have one: a topic asking the narrator to
introduce themselves becomes a narration scene saying exactly that.)

## How the script is written

Every script is **story-first**. The LLM drafts and critiques the whole story as prose, you
read and edit it, and only then is it divided into scenes — in whichever format you chose
above. It's what keeps a long video coherent instead of a sequence of scenes that each make
sense alone.

## Music

**Score this film with background music** — on by default, following the style. Music is
mixed in at the very end, never baked into a scene, so switching it off simply leaves the
film with its voices and room tone. An all-dialogue film has no score at all: the acted
takes already carry their own sound, so the toggle is disabled. A **music-video** film is
the opposite extreme — it *is* its song, played at full volume, so the toggle disappears
and a **Singing voice** picker takes its place.

## The song

Music videos only. Where the film's song comes from:

- **Write it for me** — the default. The AI writes the song from the brief above and the
  music model sings it; you audition it in the [Song tab](script.md#song) before anything
  else is built.
- **I have the song** — a **Song file** picker appears, and the song you upload becomes
  the film's soundtrack exactly as it is. Nothing is written and nothing is generated: no
  LLM, no music model. Use it for your own recording, or for a track this app generated
  for another film and you kept. WAV, mp3, m4a, flac, ogg or opus, up to 80 MB. The
  **Length** above is ignored — an uploaded song *is* the film's length, and it is what
  the scenes are divided out of. Write its lyrics into the [Song tab](script.md#song)
  next, so the story and the performed scenes follow the words.

## Singing voice

Music videos only. Who sings the film: the vocalist is described to the music model from
this voice's gender, age and tone — matched by description, never cloned, because the music
engines cannot be handed a voice. Left on *the model's own vocalist*, the song decides. You
can change it, and actually re-voice the finished track, in the
[Song tab](script.md#song). With **I have the song** nothing is described to a music model
— your song is already sung — so it is only the target for re-voicing it there.

## Auto-approve

**Auto-approve the scenes → straight to the queue after dividing** skips the
[Script](script.md) review: once you divide the story, the scenes go to the queue ready to
render. The story review still happens — that's the point of it.

## Predicted reach

If the [predictive model](analytics.md#predictive-model) has been trained, a card estimates
the film's views in its first few days from the title, direction, and format. It updates as
you type. With too little training data it says *rough estimate*; with none, the card
simply doesn't appear.

## Generating

**1. Draft the story** writes the prose and moves you to [Script](script.md), where you
review it and divide it into scenes.

For a **music video** the button reads **1. Write the song** instead, and lands you on the
[Song tab](script.md#song): the song is written first, you generate and audition it there,
and the story is drafted from the finished track. With [I have the song](#the-song) it
reads **1. Use this song** and uploads your file into that same tab.

If you arrived here from a queued request, a banner says so — generating fills that
existing queue slot and keeps its position. If you arrived via **Brief** from an existing
film ([Script](script.md) page header, on every view), your previous settings are restored
— including the format and, for a music video, the singing voice and scene count — and
generating creates a fresh work folder, leaving that film untouched.
