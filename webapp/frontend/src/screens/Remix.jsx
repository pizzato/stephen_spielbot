import React, { useState, useEffect } from 'react'
import { Card, Field, Button, Chip, Icon, Banner } from '../components.jsx'
import { api } from '../api.js'

export default function Remix({ workDir, go }) {
  const [data, setData] = useState(null)
  const [vol, setVol] = useState({ voice: 100, music: 18, ambient: 0 })
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [status, setStatus] = useState('')

  useEffect(() => {
    api.loadRemix(workDir)
      .then((d) => { setData(d); setVol({ voice: d.voice_vol, music: d.music_vol, ambient: d.ambient_vol }) })
      .catch((e) => setError(e.message))
  }, [workDir])

  const set = (k) => (e) => setVol((v) => ({ ...v, [k]: +e.target.value }))

  const remix = async () => {
    setBusy(true); setError(''); setStatus('')
    try {
      const r = await api.applyRemix({
        work_dir: data.work_dir, voice_vol: vol.voice, music_vol: vol.music, ambient_vol: vol.ambient,
      })
      setStatus(r.message)
      if (r.final_url) setData((d) => ({ ...d, final_url: r.final_url + `&t=${Date.now()}` }))
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  if (error && !data) {
    return (
      <div>
        <div className="page-head"><div className="page-head__intro">
          <span className="label-sm">Remix</span><h1 className="display-md">Nothing to remix yet</h1></div></div>
        <Banner tone="info">{error}</Banner>
        <Button variant="primary" icon="film" onClick={() => go('library')}>Browse films</Button>
      </div>
    )
  }
  if (!data) return <div className="page-head"><div className="page-head__intro"><h1 className="display-md">Loading…</h1></div></div>

  return (
    <div>
      <div className="page-head">
        <div className="page-head__intro">
          <span className="label-sm reveal">Finished</span>
          <h1 className="display-md reveal reveal-d1">{data.work_dir.split('/').pop()}</h1>
        </div>
        <div className="row gap-10 reveal reveal-d1">
          {data.final_url && <a className="btn btn--ghost" href={data.final_url} download><Icon name="download" /> Download</a>}
          <Button variant="primary" icon="youtube" onClick={() => go('youtube')}>Publish</Button>
        </div>
      </div>

      <Banner tone="danger">{error}</Banner>
      {status && <Banner tone="ok">{status}</Banner>}

      <div className="bento">
        <Card span={8} className="reveal reveal-d1" style={{ padding: 0, overflow: 'hidden' }}>
          <video src={data.final_url} controls style={{ width: '100%', display: 'block', background: '#15171a', aspectRatio: '16/9' }} />
          <div className="row center between" style={{ padding: '16px 20px' }}>
            <Chip tone="ok" dot>Final cut</Chip>
            <span className="muted mono">{data.work_dir}</span>
          </div>
        </Card>

        <Card span={4} padLg className="reveal reveal-d2">
          <span className="label-sm">Re-mix audio</span>
          <p className="muted" style={{ fontSize: 13, marginTop: 6 }}>Balance the levels and re-mux without re-rendering the video.</p>
          <div className="stack gap-22 mt-24">
            {[['voice', 'Voice', 'microphone-lines'], ['music', 'Music', 'music'], ['ambient', 'Ambient', 'wind']].map(([k, label, ic]) => (
              <Field key={k} label={<span className="row center gap-10"><Icon name={ic} style={{ color: 'var(--ink-3)', width: 16 }} /> {label}</span>} hint={`${vol[k]}%`}>
                <input className="slider" type="range" min={0} max={150} value={vol[k]} onChange={set(k)} />
              </Field>
            ))}
          </div>
          <div className="mt-24"><Button variant="primary" block icon="sliders" disabled={busy} onClick={remix}>{busy ? 'Re-mixing…' : 'Re-mix film'}</Button></div>
        </Card>
      </div>
    </div>
  )
}
