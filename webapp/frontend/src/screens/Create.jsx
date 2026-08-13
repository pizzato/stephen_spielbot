import { useEffect, useMemo, useState } from 'react'
import { Card, Field, ResolutionPicker, Check, Button, Icon, Banner, RegenLabel, voiceMetaMap, voiceLabel, effectiveWpm, styleMinutes, lengthEstimateLabel, fmtDuration, LEGACY_SCENE_SECS } from '../components.jsx'
import { api } from '../api.js'
import { resolveStyle, styleTreeOrder } from '../styleUtils.js'

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
  ['music', 'Score', 'ACE-Step writes the music'],
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

  // Legacy scene-count seeds (old queue items / re-drafts) → the length those
  // scenes used to produce (~9 s each).
  const legacyMinutes = (n) => Math.round((n * LEGACY_SCENE_SECS / 60) * 100) / 100

  const [videoTitle, setVideoTitle] = useState(seed?.title || '')
  const [direction, setDirection] = useState(seed?.description || '')
  const [minutes, setMinutes] = useState(
    seed?.minutes || (seed?.scenes ? legacyMinutes(seed.scenes) : 0)
    || (profile ? styleMinutes(profile) : 1))
  const [voice, setVoice] = useState(profile?.voice || voiceChoices[0] || 'Default (F5-TTS)')
  const [resolution, setResolution] = useState(profile?.resolution || meta.default_resolution || '')
  const [style, setStyle] = useState(profile?.visual_style || '')
  const [autoApprove, setAutoApprove] = useState(false)
  const [format, setFormat] = useState(seed?.format || 'narration')  // narration | dialogue | mixed | silent
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
    if (seed.resolution) setResolution(seed.resolution)
    if (seed.styleName) setStyleName(seed.styleName)
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
  // voices generated with its picture.
  const musicable = format !== 'dialogue'

  const generate = async () => {
    setBusy(true); setError('')
    try {
      const body = {
        video_title: videoTitle.trim(),
        topic: direction.trim() || videoTitle.trim(),
        minutes: Number(minutes) || 0,
        visual_style: style.trim() || null,
        auto_approve: autoApprove,
        voice,
        resolution,
        format,
        queue_item_id: seed?.queueItemId || '',
        style_name: profile ? (profile.name || '') : NO_STYLE,
        music: musicable && music,
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
        <Banner tone="info">Previous Create settings restored. Adjust anything, then generate a fresh script (new work folder).</Banner>
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
                  hint={`${lengthEstimateLabel(minutes, effectiveWpm(meta, profile || {}, voice).wpm, profile?.tts_sentence_pause)} at ${voice || 'the narrator'}’s cadence.`}>
                  <input className="slider" type="range" min={0.5} max={30} step={0.25} value={minutes} onChange={(e) => setMinutes(+e.target.value)} />
                </Field>
              </div>
              <div className="grow">
                <Field label="Resolution" hint="Orientation, then quality (higher = slower).">
                  <ResolutionPicker value={resolution} onChange={setResolution} meta={meta} />
                </Field>
              </div>
            </div>

            <Field label="Visual style"
              hint={locked ? 'Set by the style — pick “No style” to experiment.' : "Applied to every scene's image prompt."}>
              <input className="input" placeholder="Cinematic 35mm, golden hour, painterly lighting"
                value={style} disabled={locked} onChange={(e) => setStyle(e.target.value)} />
            </Field>

            <Field label="Narrator voice"
              hint={locked ? 'Set by the style — pick “No style” to experiment.' : undefined}>
              <select className="select" value={voice} disabled={locked} onChange={(e) => setVoice(e.target.value)}>
                {voiceChoices.map((v) => <option key={v} value={v}>{voiceLabel(v, vmeta)}</option>)}
              </select>
            </Field>

            <Field label="Format"
              hint="Narration = classic voice-over. Dialogue = the characters act and speak on screen (needs characters with a portrait). Mixed = the AI blends narration, dialogue and silent scenes. Silent = told in pictures, no narrator, with a spoken line only where a beat needs one. Whichever you pick, the direction box can steer the balance — “mostly silent, one exchange near the end”.">
              <div className="row gap-8">
                {[['narration', 'Narration'], ['dialogue', 'Dialogue'], ['mixed', 'Mixed'], ['silent', 'Silent']].map(([f, lbl]) => (
                  <Button key={f} variant={format === f ? 'primary' : 'ghost'} onClick={() => setFormat(f)}>{lbl}</Button>
                ))}
              </div>
            </Field>

            <Field label="Music"
              hint={musicable
                ? 'Background score, mixed in at the very end. Off leaves the film with only its voices and room tone.'
                : 'An acted film carries its own sound — the characters\u2019 voices are generated with the picture, so there is no score.'}>
              <Check checked={musicable && music} onChange={setMusic} disabled={!musicable}
                label="Score this film with background music" />
            </Field>

            <div className="row center between mt-8 row--wrap gap-16">
              <div className="stack gap-4">
                <span className="muted" style={{ fontSize: 12.5 }}>
                  You'll review the story next, then divide it into scenes.
                  {format === 'dialogue' && ' Each scene becomes one acted clip of about 10 seconds.'}
                  {format === 'silent' && ' Each scene becomes one clip of about 10 seconds, with no voice-over.'}
                </span>
                <Check checked={autoApprove} onChange={setAutoApprove}
                  label="Auto-approve the scenes → straight to the queue after dividing" />
              </div>
              <Button variant="primary" size="lg" iconRight="wand-magic-sparkles"
                disabled={!videoTitle.trim() || busy}
                onClick={generate}>
                {busy ? 'Drafting the story…' : '1. Draft the story →'}
              </Button>
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
