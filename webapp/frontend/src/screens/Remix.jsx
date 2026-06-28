import { useState, useEffect } from 'react'
import { Card, Field, Button, Chip, Icon, Banner } from '../components.jsx'
import { api } from '../api.js'

export default function Remix({ workDir, go }) {
  const [data, setData] = useState(null)
  const [vol, setVol] = useState({ voice: 100, music: 18, ambient: 0 })
  const [musicDesc, setMusicDesc] = useState('')
  const [musicBusy, setMusicBusy] = useState(false)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [status, setStatus] = useState('')
  const [confirmDel, setConfirmDel] = useState(false)
  const [deleting, setDeleting] = useState(false)
  // Aspect ratio is read from the actual video so portrait films aren't letterboxed.
  const [aspect, setAspect] = useState('16 / 9')
  const [portrait, setPortrait] = useState(false)
  const onVideoMeta = (e) => {
    const w = e.target.videoWidth, h = e.target.videoHeight
    if (w && h) { setAspect(`${w} / ${h}`); setPortrait(h > w) }
  }

  useEffect(() => {
    api.loadRemix(workDir)
      .then((d) => { setData(d); setVol({ voice: d.voice_vol, music: d.music_vol, ambient: d.ambient_vol }); setMusicDesc(d.music_desc || '') })
      .catch((e) => setError(e.message))
  }, [workDir])

  const set = (k) => (e) => setVol((v) => ({ ...v, [k]: +e.target.value }))

  const regenMusic = async () => {
    setMusicBusy(true); setError(''); setStatus('')
    try {
      const { task_id } = await api.regenMusic({ work_dir: data.work_dir, music_desc: musicDesc })
      // Music generation runs on a GPU worker and can take a minute or two — poll
      // the shared film-task tracker until it finishes, then refresh the preview.
      await new Promise((resolve, reject) => {
        const poll = setInterval(async () => {
          try {
            const t = await api.filmTaskStatus(task_id)
            if (t.status === 'done') {
              clearInterval(poll)
              if (t.final_url) setData((d) => ({ ...d, final_url: t.final_url }))
              setStatus('Regenerated the music and re-muxed the film.')
              resolve()
            } else if (t.status === 'error' || t.status === 'cancelled') {
              clearInterval(poll); reject(new Error(t.error || `Music regen ${t.status}.`))
            }
          } catch (e) { clearInterval(poll); reject(e) }
        }, 3000)
      })
    } catch (e) { setError(e.message) } finally { setMusicBusy(false) }
  }

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

  const del = async () => {
    setDeleting(true); setError('')
    try { await api.deleteFilm(data.work_dir || workDir); go('library') }
    catch (e) { setError(e.message); setConfirmDel(false) } finally { setDeleting(false) }
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
        <div className="row gap-10 reveal reveal-d1 row--wrap">
          {data.final_url && <a className="btn btn--ghost" href={data.final_url} download><Icon name="download" /> Download</a>}
          <Button variant="ghost" icon="film" onClick={() => go('editfilm', { workDir: data.work_dir || workDir })}>Edit</Button>
          <Button variant="primary" icon="upload" onClick={() => go('publish', { publishWorkDir: data.work_dir || workDir })}>Publish</Button>
          {confirmDel ? (
            <>
              <Button variant="danger" icon="trash-can" disabled={deleting} onClick={del}>{deleting ? 'Deleting…' : 'Confirm delete'}</Button>
              <Button variant="ghost" disabled={deleting} onClick={() => setConfirmDel(false)}>Cancel</Button>
            </>
          ) : (
            <Button variant="danger" icon="trash-can" onClick={() => setConfirmDel(true)}>Delete</Button>
          )}
        </div>
      </div>

      <Banner tone="danger">{error}</Banner>
      {status && <Banner tone="ok">{status}</Banner>}

      <div className="bento">
        <Card span={8} className="reveal reveal-d1" style={{ padding: 0, overflow: 'hidden' }}>
          <video src={data.final_url} controls onLoadedMetadata={onVideoMeta}
            style={{ display: 'block', background: '#15171a', aspectRatio: aspect, margin: '0 auto',
              width: portrait ? 'auto' : '100%', height: portrait ? '78vh' : 'auto', maxHeight: '78vh' }} />
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

        <Card span={12} padLg className="reveal reveal-d2">
          <span className="label-sm row center gap-10"><Icon name="music" style={{ color: 'var(--ink-3)', width: 16 }} /> Background music</span>
          <p className="muted" style={{ fontSize: 13, marginTop: 6 }}>Edit the music prompt and regenerate the soundtrack. This re-runs the music model on a GPU worker, then re-muxes the film with your current levels.</p>
          <div className="mt-24">
            <Field label="Music prompt" hint="What the soundtrack should sound like">
              <textarea className="textarea" rows={3} value={musicDesc} disabled={musicBusy}
                onChange={(e) => setMusicDesc(e.target.value)}
                placeholder="cinematic orchestral background music, atmospheric, instrumental" />
            </Field>
          </div>
          <div className="mt-24"><Button variant="primary" icon="wand-magic-sparkles" disabled={musicBusy} onClick={regenMusic}>{musicBusy ? 'Regenerating music…' : 'Regenerate music'}</Button></div>
        </Card>
      </div>
    </div>
  )
}
