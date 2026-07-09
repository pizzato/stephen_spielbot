import { useEffect, useMemo, useState } from 'react'
import { Card, Field, ResolutionPicker, Check, Button, Icon, Banner, RegenLabel } from '../components.jsx'
import { api } from '../api.js'

function fmtNum(n) {
  if (n == null) return '—'
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1).replace(/\.0$/, '') + 'M'
  if (n >= 1_000) return (n / 1_000).toFixed(1).replace(/\.0$/, '') + 'K'
  return String(Math.round(n))
}

const PIPELINE = [
  ['feather-pointed', 'Script', 'An LLM drafts every scene'],
  ['palette', 'Visuals', 'FLUX paints, LTX animates'],
  ['microphone-lines', 'Narration', 'F5-TTS reads the script'],
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

  // Style profiles (issue #66): the picked style OWNS the narrator voice,
  // robotic toggle and visual style (those inputs are locked to it), prefills
  // scenes/resolution, and rides along with the job so the render uses its
  // quality + audio mix too. `profile == null` means "No style" — the locked
  // fields open up, keeping their last values as a starting point.
  const styleList = meta.config?.styles || []
  const [styleName, setStyleName] = useState(seed?.styleName || '')
  const profile = useMemo(() => {
    if (styleName === NO_STYLE) return null
    return styleList.find((s) => s.name === styleName)
      || styleList.find((s) => s.name === meta.config?.default_style)
      || styleList[0] || null
  }, [styleList, styleName, meta.config?.default_style])
  const locked = !!profile

  const [videoTitle, setVideoTitle] = useState(seed?.title || '')
  const [direction, setDirection] = useState(seed?.description || '')
  const [scenes, setScenes] = useState(seed?.scenes || profile?.n_scenes || 6)
  const [voice, setVoice] = useState(profile?.voice || voiceChoices[0] || 'Default (F5-TTS)')
  const [robotic, setRobotic] = useState(!!profile?.voice_robotic)
  const [resolution, setResolution] = useState(profile?.resolution || meta.default_resolution || '')
  const [style, setStyle] = useState(profile?.visual_style || '')
  const [autoApprove, setAutoApprove] = useState(false)
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
    setRobotic(!!profile.voice_robotic)
    setStyle(profile.visual_style || '')
  }, [profile, voiceChoices])

  // In No-style mode, keep a manually chosen voice valid if the voice list changes.
  useEffect(() => {
    if (!locked && !voiceChoices.includes(voice)) setVoice(voiceChoices[0] || 'Default (F5-TTS)')
  }, [locked, voice, voiceChoices])

  useEffect(() => {
    if (!seed) return
    setVideoTitle(seed.title || '')
    setDirection(seed.description || '')
    if (seed.scenes) setScenes(seed.scenes)
    if (seed.resolution) setResolution(seed.resolution)
    if (seed.styleName) setStyleName(seed.styleName)
  }, [seed])

  useEffect(() => {
    if (!seed?.scenes && profile?.n_scenes) setScenes(profile.n_scenes)
  }, [profile?.n_scenes, seed?.scenes])

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

  // Improve the title or direction in place via the LLM (issue #88).
  const improve = async (field, instruction = '') => {
    setImproving(field); setError('')
    try {
      const r = await api.improveBrief(field, videoTitle, direction, profile ? (profile.name || '') : NO_STYLE, instruction)
      if (field === 'title') setVideoTitle(r.value)
      else setDirection(r.value)
    } catch (e) { setError(e.message) } finally { setImproving('') }
  }

  const generate = async () => {
    setBusy(true); setError('')
    try {
      const data = await api.generateScript({
        video_title: videoTitle.trim(),
        topic: direction.trim() || videoTitle.trim(),
        n_scenes: Number(scenes),
        visual_style: style.trim() || null,
        auto_approve: autoApprove,
        voice,
        voice_robotic: robotic,
        resolution,
        queue_item_id: seed?.queueItemId || '',
        style_name: profile ? (profile.name || '') : NO_STYLE,
      })
      onGenerated(data, { voice, voice_robotic: robotic, resolution, autoApprove, queueItemId: seed?.queueItemId || '', styleName: data.style_name || profile?.name || '' })
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
          <span className="label-sm reveal">New film</span>
          <h1 className="display-md reveal reveal-d1">Set the brief</h1>
        </div>
      </div>

      <Banner tone="danger">{error}</Banner>
      {seed?.queueItemId && <Banner tone="info">Editing a queued request — generating a script here will fill its existing queue slot (it keeps its position) and make it render faster.</Banner>}

      <div className="bento">
        <Card span={8} padLg className="reveal reveal-d1">
          <div className="stack gap-22">
            {styleList.length > 0 && (
              <Field label="Style"
                hint={profile
                  ? (profile.description || 'Sets the narrator and visuals below, plus render quality and audio mix — manage styles in Settings.')
                  : 'Experiment freely — narrator and visuals are yours; render quality and audio mix come from the default style.'}>
                <select className="select" value={profile ? profile.name : NO_STYLE} onChange={(e) => setStyleName(e.target.value)} style={{ maxWidth: 320 }}>
                  {styleList.map((s) => (
                    <option key={s.name} value={s.name}>
                      {s.name}{meta.config?.default_style === s.name ? ' (default)' : ''}
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
                <Field label={`Scenes — ${scenes}`} hint="Roughly 20 seconds each.">
                  <input className="slider" type="range" min={4} max={40} value={scenes} onChange={(e) => setScenes(+e.target.value)} />
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
                {voiceChoices.map((v) => <option key={v} value={v}>{v}</option>)}
              </select>
              <div className="mt-8">
                <Check checked={robotic} disabled={locked} onChange={setRobotic}
                  label="Make it robotic — a synthetic monotone so it isn't mistaken for a human" />
              </div>
            </Field>

            <div className="row center between mt-8 row--wrap gap-16">
              <Check checked={autoApprove} onChange={setAutoApprove} label="Auto-approve the script → send straight to the queue" />
              <Button variant="primary" size="lg" iconRight="wand-magic-sparkles"
                disabled={!videoTitle.trim() || busy}
                onClick={generate}>{busy ? 'Drafting the script…' : '1. Generate script →'}</Button>
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
