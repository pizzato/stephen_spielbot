import React, { useState } from 'react'
import { Card, Field, Segmented, Check, Button, Icon, Banner } from '../components.jsx'
import { api } from '../api.js'

const PIPELINE = [
  ['feather-pointed', 'Script', 'An LLM drafts every scene'],
  ['palette', 'Visuals', 'FLUX paints, LTX animates'],
  ['microphone-lines', 'Narration', 'F5-TTS reads the script'],
  ['music', 'Score', 'ACE-Step writes the music'],
  ['film', 'Cut', 'FFmpeg muxes the final film'],
]

export default function Create({ initialTopic, meta, onGenerated }) {
  const [videoTitle, setVideoTitle] = useState(initialTopic || '')
  const [direction, setDirection] = useState('')
  const [scenes, setScenes] = useState(meta.config?.default_n_scenes || 12)
  const [voice, setVoice] = useState(meta.voices?.[0] || 'Default (F5-TTS)')
  const [resolution, setResolution] = useState(meta.config?.resolution || meta.default_resolution || '')
  const [style, setStyle] = useState(meta.config?.default_visual_style || '')
  const [autoApprove, setAutoApprove] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const generate = async () => {
    setBusy(true); setError('')
    try {
      const data = await api.generateScript({
        video_title: videoTitle.trim(),
        topic: direction.trim() || videoTitle.trim(),
        n_scenes: Number(scenes),
        visual_style: style.trim() || null,
      })
      onGenerated(data, { voice, resolution, autoApprove })
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
                <Field label="Resolution" hint="Higher = better quality, slower.">
                  <select className="select" value={resolution} onChange={(e) => setResolution(e.target.value)}>
                    {(meta.resolutions || []).map((r) => <option key={r} value={r}>{r}</option>)}
                  </select>
                </Field>
              </div>
            </div>

            <Field label="Visual style" hint="Applied to every scene's image prompt.">
              <input className="input" placeholder="Cinematic 35mm, golden hour, painterly lighting"
                value={style} onChange={(e) => setStyle(e.target.value)} />
            </Field>

            <Field label="Narrator voice">
              <select className="select" value={voice} onChange={(e) => setVoice(e.target.value)}>
                {(meta.voices || ['Default (F5-TTS)']).map((v) => <option key={v} value={v}>{v}</option>)}
              </select>
            </Field>

            <div className="row center between mt-8 row--wrap gap-16">
              <Check checked={autoApprove} onChange={setAutoApprove} label="Auto-approve the script and start rendering" />
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
