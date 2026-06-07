import { useEffect, useMemo, useState } from 'react'
import { Card, Field, ResolutionPicker, Check, Button, Icon, Banner } from '../components.jsx'
import { api } from '../api.js'

const PIPELINE = [
  ['feather-pointed', 'Script', 'An LLM drafts every scene'],
  ['palette', 'Visuals', 'FLUX paints, LTX animates'],
  ['microphone-lines', 'Narration', 'F5-TTS reads the script'],
  ['music', 'Score', 'ACE-Step writes the music'],
  ['film', 'Cut', 'FFmpeg muxes the final film'],
]

export default function Create({ seed, meta, onGenerated }) {
  const voiceChoices = useMemo(() => (
    meta.voices?.length ? meta.voices : ['Default (F5-TTS)']
  ), [meta.voices])
  const configuredVoice = meta.config?.default_voice || voiceChoices[0] || 'Default (F5-TTS)'

  const [videoTitle, setVideoTitle] = useState(seed?.title || '')
  const [direction, setDirection] = useState(seed?.description || '')
  const [scenes, setScenes] = useState(seed?.scenes || meta.config?.default_n_scenes || 12)
  const [voice, setVoice] = useState(configuredVoice)
  const [voiceTouched, setVoiceTouched] = useState(false)
  const [robotic, setRobotic] = useState(!!meta.config?.default_voice_robotic)
  const [roboticTouched, setRoboticTouched] = useState(false)
  const [resolution, setResolution] = useState(meta.config?.resolution || meta.default_resolution || '')
  const [style, setStyle] = useState(meta.config?.default_visual_style || '')
  const [autoApprove, setAutoApprove] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    if (!voiceTouched) setVoice(configuredVoice)
  }, [configuredVoice, voiceTouched])

  useEffect(() => {
    if (!voiceChoices.includes(voice)) setVoice(configuredVoice)
  }, [configuredVoice, voice, voiceChoices])

  useEffect(() => {
    if (!roboticTouched) setRobotic(!!meta.config?.default_voice_robotic)
  }, [meta.config?.default_voice_robotic, roboticTouched])

  useEffect(() => {
    if (!seed) return
    setVideoTitle(seed.title || '')
    setDirection(seed.description || '')
    if (seed.scenes) setScenes(seed.scenes)
    if (seed.resolution) setResolution(seed.resolution)
  }, [seed])

  useEffect(() => {
    if (!seed?.scenes && meta.config?.default_n_scenes) setScenes(meta.config.default_n_scenes)
  }, [meta.config?.default_n_scenes, seed?.scenes])

  useEffect(() => {
    if (seed?.resolution) return
    setResolution(meta.config?.resolution || meta.default_resolution || '')
  }, [meta.config?.resolution, meta.default_resolution])

  useEffect(() => {
    setStyle(meta.config?.default_visual_style || '')
  }, [meta.config?.default_visual_style])

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
      })
      onGenerated(data, { voice, voice_robotic: robotic, resolution, autoApprove, queueItemId: seed?.queueItemId || '' })
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
            <Field label="Title">
              <input className="input input--xl" placeholder="The rise and fall of the Roman Empire"
                value={videoTitle} onChange={(e) => setVideoTitle(e.target.value)} />
            </Field>
            <Field label="Direction" hint="Optional — steer the angle, tone, or what to emphasise.">
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

            <Field label="Visual style" hint="Applied to every scene's image prompt.">
              <input className="input" placeholder="Cinematic 35mm, golden hour, painterly lighting"
                value={style} onChange={(e) => setStyle(e.target.value)} />
            </Field>

            <Field label="Narrator voice">
              <select className="select" value={voice} onChange={(e) => { setVoiceTouched(true); setVoice(e.target.value) }}>
                {voiceChoices.map((v) => <option key={v} value={v}>{v}</option>)}
              </select>
              <div className="mt-8">
                <Check checked={robotic} onChange={(v) => { setRoboticTouched(true); setRobotic(v) }}
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
