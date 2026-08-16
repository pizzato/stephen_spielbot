import { useEffect, useMemo, useState } from 'react'
import { Card, Field, Segmented, ResolutionPicker, Check, Button, Icon, Banner, RegenLabel, voiceMetaMap, voiceLabel, effectiveWpm, styleMinutes, lengthEstimate, lengthEstimateLabel, sceneBounds, fmtDuration, LEGACY_SCENE_SECS, SONG_FILE_ACCEPT, SONG_UPLOAD_MAX } from '../components.jsx'
import { api } from '../api.js'
import { resolveStyle, styleTreeOrder } from '../styleUtils.js'

// Read a picked file into a base64 data-URL for upload.
const fileToDataUrl = (file) => new Promise((resolve, reject) => {
  const r = new FileReader()
  r.onload = () => resolve(r.result)
  r.onerror = () => reject(new Error('Could not read that file.'))
  r.readAsDataURL(file)
})

function fmtNum(n) {
  if (n == null) return '—'
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1).replace(/\.0$/, '') + 'M'
  if (n >= 1_000) return (n / 1_000).toFixed(1).replace(/\.0$/, '') + 'K'
  return String(Math.round(n))
}

const PIPELINE = [
  ['feather-pointed', 'Script', 'An LLM drafts every scene'],
  ['palette', 'Visuals', 'FLUX paints, LTX or MiniMax H3 animates'],
  ['microphone-lines', 'Voices', 'F5-TTS narrates, H3 acts the dialogue'],
  ['music', 'Score', 'ACE-Step or MiniMax Music 3 writes the music'],
  ['film', 'Cut', 'FFmpeg muxes the final film'],
]

// Reserved style_name for "no style" (must match app.NO_STYLE): the narrator
// and visual fields unlock for experimentation, no extra script instructions
// are appended, and render quality + audio mix fall back to the default style.
const NO_STYLE = '(none)'

export default function Create({ seed, meta, onGenerated }) {
  const voiceChoices = useMemo(() => (
    meta.voices?.length ? meta.voices : ['Default (F5-TTS)']
  ), [meta.voices])
  const vmeta = useMemo(() => voiceMetaMap(meta.config?.voices), [meta.config?.voices])

  // Style profiles (issue #66): the picked style OWNS the narrator voice
  // and visual style (those inputs are locked to it), prefills
  // scenes/resolution, and rides along with the job so the render uses its
  // quality + audio mix too. `profile == null` means "No style" — the locked
  // fields open up, keeping their last values as a starting point.
  const styleList = meta.config?.styles || []
  const [styleName, setStyleName] = useState(seed?.styleName || '')
  const profile = useMemo(() => {
    if (styleName === NO_STYLE) return null
    const raw = styleList.find((s) => s.name === styleName)
      || styleList.find((s) => s.name === meta.config?.default_style)
      || styleList[0] || null
    // Child styles are stored sparse — resolve through the parent chain so the
    // prefills below sees the effective voice/scenes/visual style.
    return raw ? resolveStyle(styleList, raw.name) : null
  }, [styleList, styleName, meta.config?.default_style])
  const locked = !!profile
  // The visual style locks only when the style actually HAS one. A style that
  // leaves it blank has nothing to sync to, so the field stays free to write
  // (whatever is typed is combined with the profile server-side) instead of
  // sitting disabled and empty, showing its example placeholder as if that
  // were the locked-in style.
  const styleLocked = locked && !!(profile?.visual_style || '').trim()

  // Legacy scene-count seeds (old queue items / re-drafts) → the length those
  // scenes used to produce (~9 s each).
  const legacyMinutes = (n) => Math.round((n * LEGACY_SCENE_SECS / 60) * 100) / 100

  const [videoTitle, setVideoTitle] = useState(seed?.title || '')
  const [direction, setDirection] = useState(seed?.description || '')
  const [minutes, setMinutes] = useState(
    seed?.minutes || (seed?.scenes ? legacyMinutes(seed.scenes) : 0)
    || (profile ? styleMinutes(profile) : 1))
  // How many scenes that length is divided into — 0 = as many as the scene
  // contract implies. Pinning a count makes the scenes longer or shorter
  // (length ÷ count), which is why the style owns a default for it.
  const [sceneCount, setSceneCount] = useState(seed?.sceneCount ?? (profile?.video_scenes || 0))
  const [voice, setVoice] = useState(profile?.voice || voiceChoices[0] || 'Default (F5-TTS)')
  const [resolution, setResolution] = useState(profile?.resolution || meta.default_resolution || '')
  const [style, setStyle] = useState(profile?.visual_style || '')
  const [autoApprove, setAutoApprove] = useState(false)
  const [format, setFormat] = useState(seed?.format || 'narration')  // narration | dialogue | mixed | silent | song
  const [music, setMusic] = useState(true)   // score this film? (style default, overridable here)
  // Script mode ('classic' | 'story'): owned by the style, like the narrator
  // voice and visual style — locked while a style is active, editable under
  // "No style". Story-first drafts + judges a prose story, then hands off to
  // the Script screen's Story view for review and scene division. Dialogue/
  // mixed formats always run classic (story mode is v1 narration-only — the
  // backend enforces the same).
  const [busy, setBusy] = useState(false)
  const [improving, setImproving] = useState('')   // which brief field is regenerating (issue #88)
  const [error, setError] = useState('')
  // Music-video flow: the SONG comes first — "Write the song" drafts it and
  // hands off to the Script screen's SONG TAB (the song studio: generate,
  // listen, re-voice, accept a version, then draft the story from it).
  const [songVoice, setSongVoice] = useState(seed?.songVoice || '')  // '' = the model's own vocalist
  // …unless the song already exists as a file. Then nothing is written or
  // generated: the upload IS the film's track, and the story is drafted from it.
  const [songSource, setSongSource] = useState('write')   // 'write' | 'file'
  const [songFile, setSongFile] = useState(null)
  const [reach, setReach] = useState(null)   // predicted 3-day views (issue #50); null until a model exists

  // An active style keeps narrator + visuals synced to it (the inputs are
  // disabled, so this is the only writer). Switching to "No style" stops the
  // syncing and leaves the fields editable where they are.
  useEffect(() => {
    if (!profile) return
    setVoice(profile.voice || voiceChoices[0] || 'Default (F5-TTS)')
    setStyle(profile.visual_style || '')
    setMusic(profile.music_enabled !== false)
  }, [profile, voiceChoices])

  // In No-style mode, keep a manually chosen voice valid if the voice list changes.
  useEffect(() => {
    if (!locked && !voiceChoices.includes(voice)) setVoice(voiceChoices[0] || 'Default (F5-TTS)')
  }, [locked, voice, voiceChoices])

  useEffect(() => {
    if (!seed) return
    setVideoTitle(seed.title || '')
    setDirection(seed.description || '')
    if (seed.minutes) setMinutes(seed.minutes)
    else if (seed.scenes) setMinutes(legacyMinutes(seed.scenes))
    if (seed.sceneCount != null) setSceneCount(seed.sceneCount)
    if (seed.resolution) setResolution(seed.resolution)
    if (seed.styleName) setStyleName(seed.styleName)
    if (seed.format) setFormat(seed.format)
    if (seed.songVoice) setSongVoice(seed.songVoice)
    // No-style free fields (locked styles re-sync voice/visuals from the profile).
    if (seed.voice) setVoice(seed.voice)
    if (seed.visualStyle) setStyle(seed.visualStyle)
    if (seed.autoApprove != null) setAutoApprove(!!seed.autoApprove)
  }, [seed])

  // After seed applies a style name, locked profiles still own voice/visuals —
  // re-apply any explicit seed voice/visual only when re-drafting as No style.
  useEffect(() => {
    if (!seed || profile) return
    if (seed.voice) setVoice(seed.voice)
    if (seed.visualStyle) setStyle(seed.visualStyle)
  }, [seed, profile])

  useEffect(() => {
    if (!seed?.minutes && !seed?.scenes && profile) setMinutes(styleMinutes(profile))
  }, [profile, profile?.video_minutes, profile?.n_scenes, seed?.minutes, seed?.scenes])

  useEffect(() => {
    if (seed?.sceneCount != null) return   // a restored brief owns its count
    if (profile) setSceneCount(profile.video_scenes || 0)
  }, [profile, profile?.video_scenes, seed?.sceneCount])

  useEffect(() => {
    if (seed?.resolution || !profile) return
    setResolution(profile.resolution || meta.default_resolution || '')
  }, [profile, profile?.resolution, meta.default_resolution, seed?.resolution])

  // Estimate the idea's early-window reach (debounced). Silently no-ops when no model
  // has been built — the card simply doesn't render. A portrait resolution means
  // a Short, which the model weighs differently. (issue #50)
  useEffect(() => {
    if (!videoTitle.trim() && !direction.trim()) { setReach(null); return }
    const t = setTimeout(() => {
      api.engagementPredict({ title: videoTitle, description: direction, is_short: resolution.startsWith('Portrait'), style_name: styleName })
        .then(setReach).catch(() => setReach(null))
    }, 600)
    return () => clearTimeout(t)
  }, [videoTitle, direction, resolution, styleName])

  // Switching to "No style" clears the style's imprint: blank the visual style
  // and reset the narrator to the default voice, so you start from a
  // clean slate rather than inheriting the last style's fields.
  const onStyleChange = (name) => {
    setStyleName(name)
    if (name === NO_STYLE) {
      setStyle('')
      setVoice(voiceChoices[0] || 'Default (F5-TTS)')
      setScriptMode('classic')
    }
  }

  // Improve the title or direction in place via the LLM (issue #88).
  const improve = async (field, instruction = '') => {
    setImproving(field); setError('')
    try {
      const r = await api.improveBrief(field, videoTitle, direction, profile ? (profile.name || '') : NO_STYLE, instruction)
      if (field === 'title') setVideoTitle(r.value)
      else setDirection(r.value)
    } catch (e) { setError(e.message) } finally { setImproving('') }
  }

  // An all-acted film has no room for a score: every scene already carries the
  // voices generated with its picture. A song film is the opposite extreme —
  // it IS its music, so the score can't be opted out of.
  const musicable = format !== 'dialogue'
  const songFmt = format === 'song'

  // A dialogue/silent/song film is made of CLIPS, not narration: its scenes
  // have no word budget, and their length is what the video model holds in one
  // take.
  const acted = format === 'dialogue' || format === 'silent' || songFmt
  const bounds = useMemo(() => sceneBounds(profile || {}, format), [profile, format])
  const est = useMemo(
    () => lengthEstimate(minutes, effectiveWpm(meta, profile || {}, voice).wpm,
                         profile?.tts_sentence_pause, sceneCount, bounds),
    [minutes, meta, profile, voice, sceneCount, bounds])
  // A count the length can't fill at these scene lengths: the count wins and
  // the film comes out at whatever it adds up to.
  const lengthGaveWay = Math.abs(est.minutes - (Number(minutes) || 0)) > 0.02

  const draftSong = async () => {
    setBusy(true); setError('')
    try {
      const r = await api.songDraft({
        video_title: videoTitle.trim(),
        topic: direction.trim() || videoTitle.trim(),
        minutes: Number(minutes) || 0,
        // Carries into the Song tab's Scenes control (0 = Auto) so a count
        // chosen here isn't asked for twice.
        n_scenes: Number(sceneCount) || 0,
        style_name: profile ? (profile.name || '') : NO_STYLE,
        voice: songVoice,
      })
      // The song studio is the Script screen's Song tab — hand straight off.
      onGenerated(r, { voice, resolution, autoApprove: false, queueItemId: seed?.queueItemId || '', styleName: r.style_name || profile?.name || '' })
    } catch (e) { setError(e.message); setBusy(false) }
  }

  // The same hand-off, for a song that already exists: the file is uploaded as
  // the film's track and the Song tab opens on it — no LLM, no music model.
  const importSong = async () => {
    if (!songFile) return
    if (songFile.size > SONG_UPLOAD_MAX) {
      setError('That file is over 80 MB — upload an mp3, or a shorter track.'); return
    }
    setBusy(true); setError('')
    try {
      const r = await api.songImport({
        video_title: videoTitle.trim(),
        topic: direction.trim() || videoTitle.trim(),
        n_scenes: Number(sceneCount) || 0,
        style_name: profile ? (profile.name || '') : NO_STYLE,
        voice: songVoice,
        filename: songFile.name,
        data: await fileToDataUrl(songFile),
      })
      onGenerated(r, { voice, resolution, autoApprove: false, queueItemId: seed?.queueItemId || '', styleName: r.style_name || profile?.name || '' })
    } catch (e) { setError(e.message); setBusy(false) }
  }

  const generate = async () => {
    setBusy(true); setError('')
    try {
      const body = {
        video_title: videoTitle.trim(),
        topic: direction.trim() || videoTitle.trim(),
        minutes: Number(minutes) || 0,
        n_scenes: Number(sceneCount) || 0,
        visual_style: style.trim() || null,
        auto_approve: autoApprove,
        voice,
        resolution,
        format,
        queue_item_id: seed?.queueItemId || '',
        style_name: profile ? (profile.name || '') : NO_STYLE,
        music: songFmt ? true : (musicable && music),
      }
      // Phase 1: draft the story, then open the Script screen's Story view —
      // the draft is persisted server-side, so the review survives leaving.
      // Dividing it into scenes (phase 2) is what writes the script.
      const data = await api.generateStory(body)
      onGenerated(data, { voice, resolution, autoApprove: false, queueItemId: seed?.queueItemId || '', styleName: data.style_name || profile?.name || '' })
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div>
      <div className="page-head">
        <div className="page-head__intro">
          <span className="label-sm reveal">{seed?.title || seed?.description ? 'Re-draft' : 'New film'}</span>
          <h1 className="display-md reveal reveal-d1">Set the brief</h1>
        </div>
      </div>

      <Banner tone="danger">{error}</Banner>
      {seed?.queueItemId && <Banner tone="info">Editing a queued request — generating a script here will fill its existing queue slot (it keeps its position) and make it render faster.</Banner>}
      {!seed?.queueItemId && (seed?.title || seed?.description) && (
        <Banner tone="info">Previous Create settings restored. Adjust anything, then {songFmt ? 'write the song again' : 'generate a fresh script'} — it starts a new work folder, leaving the existing one untouched.</Banner>
      )}

      <div className="bento">
        <Card span={8} padLg className="reveal reveal-d1">
          <div className="stack gap-22">
            {styleList.length > 0 && (
              <Field label="Style"
                hint={profile
                  ? (profile.description || 'Sets the narrator and visuals below, plus render quality and audio mix — manage styles in Settings.')
                  : 'Experiment freely — narrator and visuals are yours; render quality and audio mix come from the default style.'}>
                <select className="select" value={profile ? profile.name : NO_STYLE} onChange={(e) => onStyleChange(e.target.value)} style={{ maxWidth: 320 }}>
                  {styleTreeOrder(styleList).map(({ style: s, depth }) => (
                    <option key={s.name} value={s.name}>
                      {'  '.repeat(depth)}{depth ? '↳ ' : ''}{s.name}{meta.config?.default_style === s.name ? ' (default)' : ''}
                    </option>
                  ))}
                  <option value={NO_STYLE}>No style — experiment</option>
                </select>
              </Field>
            )}
            <Field label={<RegenLabel busy={improving === 'title'} disabled={busy} onRegen={(instr) => improve('title', instr)} chips={['Shorter', 'Punchier', 'More specific']}>Title</RegenLabel>}>
              <input className="input input--xl" placeholder="The rise and fall of the Roman Empire"
                value={videoTitle} onChange={(e) => setVideoTitle(e.target.value)} />
            </Field>
            <Field label={<RegenLabel busy={improving === 'direction'} disabled={busy} onRegen={(instr) => improve('direction', instr)} chips={['Sharper angle', 'More detail', 'Simpler']}>Direction</RegenLabel>}
              hint="Optional — steer the angle, tone, or what to emphasise.">
              <textarea className="textarea" rows={3} placeholder="Focus on the economic decline, the military overreach, and the slow rise of Christianity."
                value={direction} onChange={(e) => setDirection(e.target.value)} />
            </Field>

            <div className="row gap-22 row--wrap">
              <div className="grow">
                <Field label={`Length — ${fmtDuration(minutes)}`}
                  hint={songFmt && songSource === 'file'
                    ? 'Ignored for a song you upload — the film runs exactly as long as your file, and you split that into scenes in the Song tab.'
                    : songFmt
                    ? 'How long the SONG runs. The film runs exactly as long as the song, and you split it into scenes in the Song tab.'
                    : acted
                    ? `${est.nScenes} scene${est.nScenes === 1 ? '' : 's'} of ~${Math.round(est.sceneSecs)} s — no narration to budget.`
                    : `${lengthEstimateLabel(minutes, effectiveWpm(meta, profile || {}, voice).wpm, profile?.tts_sentence_pause, sceneCount, bounds)} at ${voice || 'the narrator'}’s cadence.`}>
                  <input className="slider" type="range" min={0.5} max={30} step={0.25} value={minutes} onChange={(e) => setMinutes(+e.target.value)} />
                </Field>
              </div>
              <div className="grow">
                <Field label="Resolution" hint="Orientation, then quality (higher = slower).">
                  <ResolutionPicker value={resolution} onChange={setResolution} meta={meta} />
                </Field>
              </div>
            </div>

            <Field label="Scenes"
              hint={songFmt && songSource === 'file'
                ? (sceneCount > 0
                  ? `Your song split ${sceneCount} ways. Fewer scenes are longer takes — the Song tab shows what that works out to once your file is uploaded.`
                  : 'Automatic — your song is split into takes of about 5 s. Set a count to make the scenes longer or shorter.')
                : songFmt
                ? (sceneCount > 0
                  ? `The song split ${sceneCount} ways — ~${(Math.max(0.25, Number(minutes) || 0) * 60 / sceneCount).toFixed(1)} s a scene. Fewer scenes are longer takes. You can change this in the Song tab once you hear it.`
                  : `Automatic — the song becomes ${Math.max(1, Math.round(Math.max(0.25, Number(minutes) || 0) * 60 / 5))} scene${Math.round(Math.max(0.25, Number(minutes) || 0) * 60 / 5) === 1 ? '' : 's'} of ~5 s. Set a count to make the scenes longer or shorter.`)
                : sceneCount > 0
                ? (lengthGaveWay
                  ? `${sceneCount} scenes of ~${Math.round(est.sceneSecs)} s — as far as this style’s video model stretches a single take, so the film runs ${fmtDuration(est.minutes)} rather than ${fmtDuration(minutes)}.`
                  : `${fmtDuration(minutes)} split ${sceneCount} ways — ~${Math.round(est.sceneSecs)} s a scene. Fewer scenes are longer ones.`)
                : `Automatic — the length becomes ${est.nScenes} scene${est.nScenes === 1 ? '' : 's'} of ~${Math.round(est.sceneSecs)} s. Set a count to make the scenes longer or shorter.`}>
              <div className="row center gap-8">
                <input className="input" type="number" min={0} max={200} step={1}
                  value={sceneCount || ''} placeholder="Auto" style={{ width: 110 }}
                  onChange={(e) => setSceneCount(Math.max(0, Math.min(200, parseInt(e.target.value, 10) || 0)))} />
                {sceneCount > 0 && (
                  <Button variant="ghost" onClick={() => setSceneCount(0)}>Auto</Button>
                )}
              </div>
            </Field>

            <Field label="Visual style"
              hint={styleLocked ? 'Set by the style — pick “No style” to experiment.'
                : locked ? "This style sets no visual style — write one for this film."
                  : "Applied to every scene's image prompt."}>
              <input className="input" placeholder="Cinematic 35mm, golden hour, painterly lighting"
                value={style} disabled={styleLocked} onChange={(e) => setStyle(e.target.value)} />
            </Field>

            <Field label="Narrator voice"
              hint={locked ? 'Set by the style — pick “No style” to experiment.' : undefined}>
              <select className="select" value={voice} disabled={locked} onChange={(e) => setVoice(e.target.value)}>
                {voiceChoices.map((v) => <option key={v} value={v}>{voiceLabel(v, vmeta)}</option>)}
              </select>
            </Field>

            <Field label="Format"
              hint="Narration = classic voice-over. Dialogue = the characters act and speak on screen (needs characters with a portrait). Mixed = the AI blends narration, dialogue and silent scenes. Silent = told in pictures, no narrator, with a spoken line only where a beat needs one. Music video = the story becomes a SONG — sung vocals over the whole film — while the lead character performs it on camera. Whichever you pick, the direction box can steer the balance — “mostly silent, one exchange near the end”.">
              <div className="row gap-8">
                {[['narration', 'Narration'], ['dialogue', 'Dialogue'], ['mixed', 'Mixed'], ['silent', 'Silent'], ['song', 'Music video']].map(([f, lbl]) => (
                  <Button key={f} variant={format === f ? 'primary' : 'ghost'} onClick={() => setFormat(f)}>{lbl}</Button>
                ))}
              </div>
            </Field>

            {!songFmt && (
            <Field label="Music"
              hint={musicable
                ? 'Background score, mixed in at the very end. Off leaves the film with only its voices and room tone.'
                : 'An acted film carries its own sound — the characters\u2019 voices are generated with the picture, so there is no score.'}>
              <Check checked={musicable && music} onChange={setMusic} disabled={!musicable}
                label="Score this film with background music" />
            </Field>
            )}

            {songFmt && (
              <Field label="The song"
                hint={songSource === 'file'
                  ? 'Your file becomes the film’s soundtrack as it is — nothing is written or generated. Its length is the film’s length. Write its lyrics in the Song tab next, so the story and the scenes follow the words.'
                  : 'The AI writes the song from the brief above, and the music model sings it. You audition it in the Song tab before anything else is built.'}>
                <Segmented value={songSource} onChange={setSongSource} options={[
                  { value: 'write', label: 'Write it for me' },
                  { value: 'file', label: 'I have the song' },
                ]} />
              </Field>
            )}

            {songFmt && songSource === 'file' && (
              <Field label="Song file"
                hint="A song you already have — your own recording, or one this app generated for another film and you kept. WAV, mp3, m4a, flac, ogg or opus, up to 80 MB.">
                <input className="input" type="file" accept={SONG_FILE_ACCEPT}
                  onChange={(e) => setSongFile(e.target.files?.[0] || null)} />
              </Field>
            )}

            {songFmt && (
              <Field label="Singing voice"
                hint={songSource === 'file'
                  ? 'Only the target for re-voicing your file in the Song tab (seed-vc clones it onto the vocal). Nothing is described to a music model — your song is already sung.'
                  : 'Who sings the film. The vocalist is described to the music model from this voice’s gender, age and tone (matched by description, not cloned). Leave it on the model’s own vocalist to let the song decide.'}>
                <select className="select" value={songVoice} onChange={(e) => setSongVoice(e.target.value)}>
                  <option value="">The model’s own vocalist</option>
                  {voiceChoices.filter((v) => v !== 'Default (F5-TTS)').map((v) => (
                    <option key={v} value={v}>{voiceLabel(v, vmeta)}</option>
                  ))}
                </select>
              </Field>
            )}

            <div className="row center between mt-8 row--wrap gap-16">
              <div className="stack gap-4">
                <span className="muted" style={{ fontSize: 12.5 }}>
                  {songFmt
                    ? (songSource === 'file'
                      ? 'The song comes first: your file opens in the Song tab, where you write in its lyrics, re-voice it if you want — and draft the story from it.'
                      : 'The song comes first: it opens in the Song tab, where you generate it, re-voice it, pick the version you like — and draft the story from it.')
                    : "You'll review the story next, then divide it into scenes."}
                  {format === 'dialogue' && ` Each scene becomes one acted clip of about ${Math.round(est.sceneSecs)} seconds.`}
                  {format === 'silent' && ` Each scene becomes one clip of about ${Math.round(est.sceneSecs)} seconds, with no voice-over.`}
                  {format === 'song' && songSource !== 'file' && ` Each scene becomes one performed clip of about ${Math.round(est.sceneSecs)} seconds.`}
                </span>
                <Check checked={autoApprove} onChange={setAutoApprove}
                  label="Auto-approve the scenes → straight to the queue after dividing" />
              </div>
              {songFmt && songSource === 'file' ? (
                <Button variant="primary" size="lg" iconRight="music"
                  disabled={!videoTitle.trim() || !songFile || busy}
                  onClick={importSong}>
                  {busy ? 'Uploading the song…' : '1. Use this song →'}
                </Button>
              ) : songFmt ? (
                <Button variant="primary" size="lg" iconRight="music"
                  disabled={!videoTitle.trim() || busy}
                  onClick={draftSong}>
                  {busy ? 'Writing the song…' : '1. Write the song →'}
                </Button>
              ) : (
                <Button variant="primary" size="lg" iconRight="wand-magic-sparkles"
                  disabled={!videoTitle.trim() || busy}
                  onClick={generate}>
                  {busy ? 'Drafting the story…' : '1. Draft the story →'}
                </Button>
              )}
            </div>
          </div>
        </Card>

        <div className="col-4 stack gap-16">
          {reach?.available && (
            <Card className="reveal reveal-d1">
              <span className="label-sm">Predicted reach</span>
              <div className="row center gap-12 mt-16">
                <div style={{ fontSize: 30, fontWeight: 700, color: 'var(--accent)', letterSpacing: '-0.02em' }}>{fmtNum(reach.predicted_views)}</div>
                <div className="muted" style={{ fontSize: 12 }}>
                  est. views in the first {reach.prediction_days || 3} days
                  {reach.reliability && reach.reliability !== 'ok' && <><br /><span style={{ color: 'var(--warn)' }}>rough estimate ({reach.reliability})</span></>}
                </div>
              </div>
            </Card>
          )}
          <Card className="reveal reveal-d2">
            <span className="label-sm">The pipeline</span>
            <div className="stack gap-16 mt-16">
              {PIPELINE.map(([ic, t, d], i) => (
                <div key={i} className="row center gap-10">
                  <span className="stream-ico" style={{ background: 'var(--accent-soft)', color: 'var(--accent)' }}><Icon name={ic} /></span>
                  <div className="grow">
                    <div style={{ fontWeight: 600, fontSize: 13.5 }}>{t}</div>
                    <div className="muted" style={{ fontSize: 12 }}>{d}</div>
                  </div>
                </div>
              ))}
            </div>
          </Card>
          <Card well className="reveal reveal-d3">
            <div className="row center gap-10">
              <Icon name="circle-info" style={{ color: 'var(--ink-3)' }} />
              <span className="muted" style={{ fontSize: 12.5 }}>You'll review and edit the script before anything renders.</span>
            </div>
          </Card>
        </div>
      </div>
    </div>
  )
}
