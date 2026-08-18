# Acted Scenes & Performance Films

An **acted scene** is one where the characters speak on screen: **MiniMax H3 Ref2VA
generates the picture and the speech in a single pass** from the characters' portraits.
There is no first frame, no TTS step, and nothing is lip-synced onto a still — the
performance and the voice are made together.

A **performance film** is a film made entirely of them: no narrator, no music. But acted
scenes are not confined to that — a *Mixed* film puts them alongside narrated and silent
scenes, and each scene takes the path its mode asks for.

| | Narrated scene | Acted scene |
|---|---|---|
| Script | image + video prompts + narration | cast, timed beats, quoted dialogue, soundscape |
| First frame | image engine (FLUX) | none |
| Video | LTX 2.5 or H3 I2V | **H3 Ref2VA** from character portraits |
| Voice | TTS (OpenF5 / Chatterbox) | generated with the picture, cast from the voice library |
| Length | the narration audio | what the dialogue needs, ~10 s |

## Turning it on

Choose the **Dialogue** format in [Create](manual/create.md) for a film that is acted all
the way through, **Mixed** to let the writer place acted scenes among narrated and silent
ones, or **Silent** for a film told in pictures where a spoken line is the exception. A
single scene can also be switched to *dialogue* in the
[Script editor](manual/script.md) — any scene with dialogue lines is acted, wherever it sits.

Whichever format you pick, the **direction box outranks its balance**: ask for "mostly
silent scenes, one exchange near the end" and the division follows that rather than the
format's own default mix.

The script is written story-first either way: the prose story is drafted with no scene or
clip-length constraints — whatever the film's size, the draft only learns that its
characters will speak on camera — and the division into scenes is what stages it as
performance, applying the per-clip budgets. Instructions in the topic aimed at the
narrator (say, asking the narrator to introduce themselves) survive that staging: they
stay narration scenes rather than being dropped or handed to a character, and a *Mixed*
film is required to genuinely mix acted and narrated scenes throughout. The video model
is picked per style (Settings → a style → **Video models → Acted (dialogue) scenes**; see
below).

A scene written as dialogue in a mixed script only carries its lines — the cast comes from
who speaks, the length from what they say, and the setting from the scene's own video
prompt.

### Silent scenes, performed

A **silent** scene is a visual beat with no voice-over. By default it is animated from a
first-frame still by the style's I2V engine, like a narrated scene without the narration.
Turn on **Settings → a style → Video models → Silent scenes — act them on H3 too**
(`h3_silent_scenes`) and it is *performed* instead: one H3 Ref2VA take carrying its own
ambience and saying nothing — the same path the acted scenes take, so the silent beat and
the takes around it read as one production rather than two.

**The toggle alone decides**, so every silent scene in the style is shot the same way.
What the take is built from depends on what the scene has:

- **The scene's first frame.** Ref2VA has no literal first-frame input, but the scene's own
  image rides as a reference that defines the opening composition — *begin the take looking
  like this picture*. So the image prompt still composes the shot, exactly as it did on the
  I2V path. The Create screen's preview is used when one exists; otherwise the frame is
  generated at render time, on the same worker, right before the take — **unless a location
  reference applies to the scene**: the location is the place, chosen by hand, and a frame
  outranks it, so no frame is invented over it. Removing a scene's first frame therefore
  sticks — the take opens on the location instead.
- **Portraits, when anyone is on screen.** The writer names a **cast** on each silent scene
  (at most two, from the same characters), alongside the setting, camera and soundscape a
  dialogue scene gets, and those portraits join the frame as references — which is what
  keeps a face consistent between a silent beat and the acted scene beside it. A scene with
  nobody in it simply opens on its frame.
- **The film's locations, wardrobe and reference stills.** A performed silent take is fed
  the same reference wall as a dialogue take, so those films get the **Characters &
  Artifacts** tab even when nobody in them ever speaks.

Because the take is built from those fields, the [Script editor](manual/script.md) and a
film's edit screen write a silent scene through the **acted setup** whenever the style
performs it — on screen, setting, duration, action beats, camera and sound, the dialogue
editor minus the dialogue — rather than a bare duration. The image and video prompts stay
beside it: the image paints the frame the take opens on, and the video prompt stands in as
the setting while that field is empty. Below them sits the **Acted prompt** — the
assembled H3 text, exactly as a dialogue scene shows it, editable as a pinned override.

A performed silent scene is also listed in the **Acted scenes** view on both screens, as
the same card a spoken take gets — numbered reference slots, the prompt, the rendered take
and its re-shoots — marked *silent* and without the dialogue editor.

Nothing else about the scene changes: it is still silent by contract. The prompt says so
outright, and the same gate that watches an establishing wide transcribes the take and
retakes (then mutes) it if the model babbles into the silence.

**Length.** A performed silent beat runs 5–12 s, H3's single-clip window — an authored
20 s is held back to 12. With **Chained scenes** on it stretches to ~23 s (the writer is
asked for ~19), shot as two clips joined by Motion Context: the take's own beats are split
across the clips by their timings rather than all landing in the first, so the second clip
has something to do besides hold the frame. A silent scene that already fits one clip stays
one clip either way, rather than paying the join's overhead for nothing.

!!! note "It costs an acted scene, not an I2V clip"
    A performed silent beat renders in ~6 minutes per 10 s on a GB10, against roughly a
    minute for the LTX clip it replaces.

### Singing films (the Music-video format)

The **Music video** format in [Create](manual/create.md#format) makes the film a song. The
pipeline changes in three places:

- **The story becomes lyrics.** After you approve the prose draft, the divide step also
  writes the film's **song** (`song.json` in the work dir): tagged lyrics —
  `[Verse]`/`[Chorus]`/`[Bridge]` — telling the story's arc in singable lines, plus a
  music **caption** (genre, tempo, mood, arrangement) that becomes the film's music
  description. Both music engines sing lyrics natively; the caption reads best structured,
  and for song films it must *not* say "instrumental".
- **The cast performs it.** Every scene is staged as a **performed silent take** —
  the same H3 Ref2VA path as [silent scenes, performed](#silent-scenes-performed),
  no style toggle needed — stamped `singing` in its metadata. Scenes with cast on screen
  are prompted for a visible performance (mouth moving with the words, moving with the
  beat) instead of the silent film's closed mouth, and the take ships carrying **its own
  slice of the song**: the model's a-cappella audio is replaced by the exact stretch of
  the real track the scene was generated against, so a clip played on its own — on the
  render wall, in the film editor — shows the performance against the music it is meant
  to follow, which is the only way to see whether the mouth lands on the words. That
  audio is never mixed into the film (voice and ambient are pinned to 0 %, below), so
  nothing doubles. The speech gate stands down for these takes — singing is what was
  asked for. A beat can also be told **not** to sing: **Re-generate scene** with the
  "Nobody sings in this shot" chip (or any instruction to that effect) marks the scene
  as non-performing — the song still plays over it, but the cast listens and moves with
  the music instead of miming.
- **A shot with nobody in it stays empty.** A song film's story usually includes scenes
  written with no one in frame — an empty street, a sky, a diagram. Those are rendered as
  **scenery**: the song still plays over them, but nothing is asked to mime it. Asking for
  a performer in a shot with no cast and only a landscape reference made the model invent
  one, differently in each such scene, and where the reference was a diagram rather than a
  place it abandoned the art style to fit a singer in.
- **The song is the soundtrack.** Music is forced on and mixed at **full volume** over the
  whole film (a song film with the 18 % bed gain would be a near-silent music video), and
  it is the *whole* mix: voice and ambient levels are pinned to **0 %**, at render and at
  every later re-mix, so a stray spoken beat or a soundscape can never bleed in under the
  track. The film editor offers a music video one level — Music — for that reason. Its
  *Sing it again* re-sings the same lyrics; edit the caption there to
  change the sound. The script's [Song tab](manual/script.md#song) edits — or re-writes
  with the LLM — both the words and the sound.

**When it stops dead.** Both engines fill exactly the seconds they are asked for, so a
song can end mid-bar or mid-word. The Song tab's **Ending** control cures either fault:
*Extend the ending* fades the take's last seconds out into a tail of silence (ffmpeg on
the controller — the approved arrangement is kept and the extended track becomes the
film's, so the scene division divides the longer length), while *Re-generate that much
longer* asks the engine for the current length plus those seconds, which gives it room to
finish but yields a fresh take. Both keep the previous track as a version.

**Whose voice sings?** Two levels. The music engines can't clone a voice, so at
generation time the chosen singing voice only *describes* the vocalist (below). But the
song panel's **"Sing this as [voice]"** step is an actual clone: seed-vc re-voices the
generated track with any library voice's timbre — melody, timing and words kept — from
its ~10 s reference clip, zero-shot. Install it once with `scripts/install_svc.sh`
(GPL-3.0, see THIRD_PARTY_NOTICES) — that install separates the vocal stem from the
backing and does the re-mix; the diffusion itself goes to whichever
[GPU worker is free](cluster.md#song-re-voicing-rides-along-in-the-comfyui-container),
falling back to the controller (minutes rather than seconds) when none is. The vocals
are converted alone and laid back over the untouched instruments, so the arrangement
survives. The converted track replaces
`background_music.wav` — the film, and every pinned per-scene segment, then sings in
that voice.

**Both sides are kept.** The sung original and every re-voicing live on as music
versions (`music_history/` in the work dir, and they travel with a story re-divide), so
either can be put back with one click — from the Song tab before the render, or from the
finished film's [edit screen](manual/edit-film.md#a-song-films-song), which also *does*
the re-voicing and re-mixes the final with whichever version you pick. A re-voicing
always converts the sung original rather than the last conversion, so a second voice is a
clone of the engine's vocals and not of the first clone. After the render only the
soundtrack changes — the takes were shot against the original song, and the slice each
one carries is that original too. Nothing is
pruned, but a version you aren't using can be **deleted** from the Song tab — the list is
otherwise every take it took to land the song. The one in use can't be (put another back
first), and neither can the last one left: it *is* the film's song.

**The song needn't be generated at all.** Create's [I have the song](manual/create.md#the-song)
starts a music video from an audio file instead — your own recording, or a track this app
sang for another film — and the Song tab's **Use a song from a file** swaps one into an
existing film. The upload is re-encoded to `background_music.wav` and kept as a music
version like any generation, so the take it replaced can be put back, and it can be
re-voiced from there just the same. Everything downstream is unchanged: the uploaded
track's real length is what the scenes are divided out of, and each take gets its stretch
of it pinned in. The lyrics are the one thing nothing can supply — type them into the Song
tab, because the story is drafted from them and the scenes perform them.

At generation time, the fallback description: the render describes the **lead performer's cast library voice**
(gender, age, tone, accent — e.g. *"mature female vocalist, warm smoky voice, Irish
accent"*) and appends it to the caption: the singer on screen and the voice on the track
are matched by description, not by cloning. Pick the character's voice in the script's
Characters tab to steer it. Lip movement is a performance, not a phoneme-accurate sync —
exactly like a real music video shot without playback.

**Engine choice matters.** MiniMax Music 3's caption-driven vocals are the stronger
singer; ACE-Step also sings but our workflow pins its BPM, key and **English-only**
language, so non-English songs need Music 3. And Music 3 caps a track at ~6 minutes — a
longer film loops the song, restart audible, so keep song films short.

!!! note "It is still one song under many takes"
    H3 cannot lip-sync to an external track (it writes picture and audio together), so a
    music video is the honest shape of a "singing character": the film's real vocals come
    from the music engine, and the takes perform them.

**Unattended.** [Settings → Automation](manual/settings.md#what-automation-makes) has a
**Default format** picker — the style's default, which seeds the Create screen and is
what automation writes when there is no Create screen to ask — and,
for `Music video`, the song steps: write and generate the song, QC the lyrics, re-voice
the track, and a gate that parks the song for you to hear before anything is built on it.
All of it is set globally or [per style](manual/settings.md#scope-global-then-per-style),
so one channel can be a music-video channel while the rest stay narrated.

The order is the reason those exist rather than one blanket toggle. A music video's scene
windows are timed against the **real** track and each take has its stretch of it pinned
in, so the song has to be finished *before* the story is divided — exactly what the Song
tab enforces by hand. Left to the render, the music task runs alongside the video tasks:
the takes are shot with nothing to sing to and the windows are timed against an estimate.

**The lyrics are placed where the singing is.** Once the track exists, the divide step
*measures* it — on the separated **vocal stem** when demucs is installed (it comes with
the re-voicing install, `scripts/install_svc.sh`, and the
first divide against a track separates and caches the stem): on a stem, an intro, a solo
and an outro are real silence, so the measurement stays exact however loud the
arrangement is — a distorted-guitar intro used to read as singing on the mix-level
measurement, and the lead mouthed a verse over it. The lyric lines are
then paced through the **singing** rather than through the running time, and each scene
is told two things — the words its own slice actually contains, and when inside its clip
a voice is heard. That second part is what keeps a mouth shut over an intro: a song
opening with a 7.5-second instrumental used to have the lead mouthing a verse to silence
while the whole film ran a scene ahead of its own song. A track with no bare-instrumental
stretch is treated as sung end to end, and if the measurement cannot be made the older
proportional split is used unchanged. Without demucs, the coarser mix-level split stands:
it finds intros and breaks on tracks whose bed is quieter than the voice, but cannot
tell a loud instrumental solo from a sung line, since both are simply loud.

**Each line's time can be measured, not estimated** — the *Align lyrics to the sung
track* option ([Settings → Music](manual/settings.md), `song_align_lyrics`, on by
default). Level measurement knows *where* singing is but never *which* line is being
sung, so line times are otherwise estimated by pacing the lines evenly through the
singing — close, but a line can smear across a phrase gap into the neighbouring scene.
With the option on, the divide transcribes the vocal stem with word timestamps
(faster-whisper, installed by the same `scripts/install_svc.sh` beside demucs) and
matches the transcript against the lyric sheet it already knows — alignment, not
transcription, so the fixed line order disambiguates a chorus sung four times, a garbled
word interpolates from its neighbours, and words "heard" outside the measured singing
are discarded as hallucinations. Scenes then name and cut on the words actually sung
under them. When fewer than half the lyric words match — or whisper is not installed —
the paced estimate is used instead, so the option never makes timing worse. The model
(whisper *small*, multilingual) downloads on the first alignment; the divide's "Timing
the song's lyrics" step covers it in Activity.

**The scenes cut between sentences.** The same lyric timeline decides *where* one take
ends and the next begins. Rather than dividing the track into mathematically equal
windows — which lands cuts mid-line as often as not — each seam snaps to the nearest gap
between lyric lines, so every take carries whole sung lines. The middle of a measured
instrumental break is preferred even over a somewhat nearer line gap: cutting silence is
exact, while the line gaps are estimates. The takes bend around the planned
length to do it, within bounds: no take shrinks below the 5-second acted minimum or
grows past the 12-second single-clip cap, and a seam with no line boundary in reach
falls back to the even grid — takes planned right at the 5-second floor have no slack
and keep the grid exactly. When the lyric lines were whisper-aligned (above), the seams
cut between *measured* lines; when the alignment is off or fell back to the paced
estimate, a cut can still graze a word if the delivery is very uneven. Cuts that land in
instrumental breaks are exact either way.

## What you need first

**Characters with portraits.** The portraits *are* the conditioning: each cast member's
reference image becomes a `<Picture N>` reference for the scenes they appear in. A scene
whose cast resolves to no portrait at all fails rather than inventing a look — see
[characters](characters.md) for the catalogue and the per-script cast.

An acted story casts **its own** characters: the style's catalogue is only offered as
speakers when the brief names one of them (topic, title, or the style's extra
instructions — see [casting is opt-in](characters.md#casting-is-opt-in-by-name)).
Otherwise the writer invents the cast, and the portraits are generated for those
per-script characters.

**Voices.** Each character's cast voice (assigned automatically at script creation, from
the same library the narrator uses) is passed as an `<Audio N>` reference so the character
sounds the same in every scene. A character with no voice still speaks — the model simply
invents one, and it will drift between scenes.

## The script

The LLM writes a different shape (`pipeline/performance.py`): per scene a `setting`, the
`cast` present, `beats` with timings, `lines` of `{speaker, delivery, text}`, a `camera`
line, and a diegetic `soundscape`. `build_h3_prompt` assembles those into the six-block
prompt the model responds to — reference roles, style, timed beats with the dialogue
quoted verbatim, camera, audio, then a refusal list.

The assembly is deterministic rather than LLM prose, because two of those blocks are not
optional: every reference must be given an explicit job or the model blends them, and the
"do not add subtitles" refusals must be present on every scene or H3 burns subtitles into
the picture.

The assembled prompt is stored in each scene's **video prompt**, so the
[Script editor](manual/script.md) shows and edits exactly what the model receives. The
structured fields stay in the scene metadata, and the renderer rebuilds the prompt from
them so the reference numbering always matches the references actually wired up.

Keep spoken lines short: roughly 2.5 words per second of clip, so about 25 words in a
10-second scene. Over that and the model cuts the line off.

## Editing an acted scene

An acted scene is written through its fields — who is **on screen** (Picture 1, Picture 2…),
the **setting**, the **dialogue** with per-line delivery, timed **action** beats, **camera**
and **sound** — in the [Script editor](manual/script.md) like any other scene. The *Acted
scenes* view adds the resolved references: the portrait that IS `<Picture 1>`, the voice
clip that IS `<Audio 1>`.

**A first frame, optionally.** An acted scene can carry a first-frame image like a
narrated one: on the Script screen its card paints the image from the scene's *setting*
with the cast anchored to their portraits (or use Edit image / upload). Ref2VA has no
literal first-frame input — the image rides as the take's **opening-composition
reference**: a `<Picture N>` whose authority is the space, light, framing and where
everyone stands, while faces and voices stay bound to their own references. When a scene
has one, it supersedes the location asset for that scene (the frame IS the place,
photographed), keeping the reference budget tight — measured, three picture references
hold; more and the weakest starts dropping. Because of that ranking, the first frame is
opt-in for every acted scene: nothing auto-paints one (the Script screen's
missing-preview pass skips acted, singing and performed-silent scenes alike), and
**Remove first frame** is permanent — the location takes over, and neither the screen nor
the render quietly paints the frame back.

**Scenery, wardrobe, and free-form references** — the `<Picture N>` slots beyond the
portraits — live with the characters, under **Characters & Artifacts**, on both the Script
screen and the film's edit screen. One bar adds them all: **character, location, wardrobe,
image** (any other thing the model should match — a prop, a vehicle, a logo; the
description tells the model what it is), **video** (a clip whose extracted frame feeds
the slot), and **soundtrack** (an audio file — see below). The free-form kinds — image,
video and soundtrack — take an optional **How it's used** note that rides into the take's
prompt in the user's own words (*"the characters copy this dance's movements"*): for an
image or video it replaces the default *match it exactly* authority line, so a reference
can direct the performance instead of just appearing in it.

**Soundtrack artifacts.** An **audio** artifact is not a reference picture: the whole
track is **pinned into the H3 generation** of every acted take it applies to
(audio-driven generation, the same mechanism that powers
[singing films](#singing-films-the-music-video-format)), so the performance follows the
sound and the take keeps it as its audio. Its *How it's used* note becomes a
`[SOUNDTRACK]` prompt section telling the performance what to do with the music it hears
(*"the characters dance to this track"*), and the prompt's music refusals stand down to
"no music beyond the clip's own soundtrack". Scope it with the same *Used in* scene list as
any other artifact. The speech gate stands down for those takes — the audio was provided,
not scripted — and a song film's own per-scene segments outrank artifacts. Every card takes an upload, a **pasted** image, or a **URL** — a direct file
link or a page, whose `og:image` / `og:video` is fetched. Everything the film renders from appears there at the same level:
the script's own characters and visuals (editable), and the style catalogue's (marked
*catalogue*, read-only — shared across films, they edit in Settings). A film's own visuals
shadow same-named [assets](manual/settings.md) from the catalogue; both feed the prompt the
moment they have an image. Each acted scene's card also shows its resolved references as
thumbnails — the portraits, location and wardrobe that exact take renders from.

The **video prompt is read-only and assembled from those fields**, so nothing is written
twice. **Edit prompt** pins hand-written text instead: the fields stop rebuilding it, and
the render sends exactly what is on screen. **Rebuild from the fields** drops the override
again.

**Re-generate scene** rewrites the whole take with the LLM — dialogue, action, setting,
camera, sound — keeping the film's context and cast, optionally steered by a free-text
instruction. An acted scene is one coherent take, so it regenerates whole rather than
field by field; a pinned prompt is superseded by the rewrite. On a music-video beat the
rewrite also decides whether the cast **sings on camera**: the singing directives in the
prompt come from the scene's flags rather than from the rewrite's prose, so an
instruction like the default **"Nobody sings in this shot"** chip flips the scene to a
listening beat — the song still plays over the shot (and stays pinned into the take),
but nobody mimes it — and a later rewrite can flip it back.

Editing a scene of a film that has already rendered keeps the existing clip — it is the
deliverable — and offers **Shoot this scene again** to re-render just that scene, right
beside **Re-generate scene**: rewrite the take, then shoot it again to film it. Every
re-shoot is kept as a **take** (the last ten, plus whichever is selected): a strip under
the player flips between them, and the selected take is what Reassemble puts in the film.

## Rendering

Each scene is a single Ref2VA generation, run across the ComfyUI workers in parallel
(`resume_generation.py`). An acted scene plans one task — no image, narration or mux task
— and the narrated scenes in the same film plan their usual quartet. Assembly concatenates
everything, keeping each clip's own audio.

**Music** is a final-mix ingredient, never baked into a scene. Switch it off per style
(Settings → *Narrator & audio* → **Music**) or per film (Create → **Music**), and the
final cut is the concatenation itself. An all-acted film never plans a score at all.

**One production, one look.** A mixed film's narrated scenes render on **H3 I2V** rather
than the style's usual video engine — H3 acted takes cut against LTX clips read as two
different productions, with colour and motion shifting shot to shot. A style already on a
MiniMax engine keeps its own pick; unmixed films are untouched.

### Video models

A style carries **two video model pickers**, side by side under *Video models*, because a
film can hold two kinds of scene:

- **Narrated & silent scenes** — the I2V engine (LTX 2.5 or MiniMax H3) that animates each
  scene from its first-frame still. (Silent scenes leave for the Ref2VA picker below when
  the style acts them — see [Silent scenes, performed](#silent-scenes-performed).)
- **Acted (dialogue) scenes** — the Ref2VA engine that performs each acted scene from
  portraits and voices. Always a MiniMax H3 variant:

| Engine | Speed | Notes |
|---|---|---|
| `minimax-h3-ref-w4a8` (default) | ~6.6 min per 10 s scene | 4-bit weights, **15 real steps** — turbo's speed without the distillation look. Needs ComfyUI ≥ 0.31.0 |
| `minimax-h3-ref-turbo` | ~6 min | Distilled 4-step LoRA on the full 34 GB checkpoint — fastest, but over-saturated and over-sharpened |
| `minimax-h3-ref` | ~23 min | 15 steps + EasyCache on the 21 GB pruned checkpoint — the fidelity reference |

Measured on a DGX Spark GB10, same shot and seed throughout (704×1280): edge
energy 4.2 for w4a8 against turbo's 5.5 (and 6.1 at 8 steps), with the base
engine at 4.4 — the "over-sharpened" look is the distillation, not the step
count, and w4a8 avoids it at turbo's wall clock. Download them from
Settings → Infrastructure like any other engine; see [models](models.md).

**Sampling steps** is ONE knob beneath both pickers: it overrides the step count of every
MiniMax H3 render in the style — narrated-scene I2V and acted-scene Ref2VA alike. 0 keeps
each engine's own default (Turbo 4, the others 15); LTX ignores it.

!!! warning "w4a8 needs ComfyUI ≥ 0.31.0 on every worker"
    Below that version the checkpoint does not error — it renders **black
    frames**. The render refuses up front rather than shipping them, but a
    mixed fleet still means some workers cannot take the job at all. Rebuild
    all of them together (`COMFYUI_REF`), exactly like SageAttention.

!!! warning "Licence"
    MiniMax H3 is **not licensed for use in the USA, EU, UK or South Korea**, and requires
    machine-generated disclosure and "MiniMax H3" attribution. The picker repeats this.

## Consistency

Consistency comes from three mechanisms, each born from a measured failure:

- **One scene, one generation.** A whole conversation renders as a single
  continuous clip — both speakers in frame, placed left/right with locked
  positions, every line in order. Identity is protected by the prompt's
  identity locks and verified by the gate; be aware the model *can* still swap
  two same-kind people in one clip. For content where identity outranks flow,
  `performance_shot_split: true` renders shot/reverse-shot instead — one face
  and one voice per clip (structurally swap-proof), with a silent wide opening
  each scene (`performance_establishing`).
- **A reference budget.** Measured directly: at three picture references
  everything held (face, outfit colour, location); at four the weakest dropped.
  The renderer enforces it — later shots swap the scene's own first frame in
  for the location asset, and the wide drops wardrobe (invisible at that
  distance anyway).
- **A quality gate.** Every talking shot is transcribed (faster-whisper, CPU,
  seconds against a ~6-minute render) and scored against its scripted line; a
  miss is retaken and the better take kept
  (`performance_verify`, `performance_verify_retakes`, default one retake).
  The gate verifies **speech, not picture** — a visually broken shot that says
  its line still passes.
- **A truncated take is retaken LONGER, not just re-rolled.** A shot that says
  the start of its line and then hits the last frame is a separate failure from
  one that says the wrong words: a fresh seed reproduces the same cut, because
  the clip is too short rather than unlucky. The gate spots it (the words that
  came out match the head of the line) and sizes the retake from the pace that
  take actually delivered — so a scene the word count under-bought gets the
  seconds its own delivery needed, capped at the model's 15 s ceiling.

Shots are sized to their words (~2.5 words/second plus a beat of air) rather
than to a share of the scene, because oversized shots left the model padding
the tail with speech nobody scripted. That estimate is a **median**, not a
guarantee: measured across 29 rendered shots, H3 delivers between 1.4 and 2.7
words a second depending on the line — short dramatic sentences run at half the
rate of flowing prose, because each full stop buys a pause. Rather than pay for
the slow tail on every scene, the length stays tuned to the middle and the gate
buys time for the takes that overrun. And cast **distinct voices** for
co-stars: two reference voices five hertz apart will bleed into each other,
and no prompt can separate them.

## Limits

- **15 seconds is a hard ceiling** per single-clip scene, and cost grows faster than
  length — scenes are written to ~10 s, or up to 12 s when
  [Scenes](manual/create.md#length-scenes-and-resolution) asks for fewer, longer takes
  (that is where the script budget stops stretching, so the film comes out shorter than
  the length asked for rather than the takes truncating). With **Chained scenes** on (see
  [Models → Chained scenes](models.md#chained-scenes)) a dialogue or performed silent
  scene may run to ~29 s: it is shot as two clips joined by H3 Motion Context, the
  script budget doubles, and exchanges that used to split into consecutive scenes
  stay one take.
  A take that still stops too early can be extended after the fact with
  [Continue](manual/edit-film.md#continuing-an-acted-scene), which shoots the next
  clip against the same motion context rather than re-shooting the scene — the one
  catch being that the context lives on the worker that shot the take, so that
  machine has to be up.
- **One voice reference bleeds onto other speakers** in the same clip. Give every speaker
  their own voice (the model accepts 3 per scene), or write scenes with one speaker.
- **Nine portraits and three voices** per scene, maximum.
- **Acted scenes are not captioned.** There is no TTS step to measure, so the caption
  track covers the narrated scenes only.
- English is what this has been exercised on; other languages are untested.
