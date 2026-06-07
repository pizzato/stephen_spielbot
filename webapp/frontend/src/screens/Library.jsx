import { useState, useEffect } from 'react'
import { Card, Chip, Button, Icon, Banner } from '../components.jsx'
import { api } from '../api.js'

export default function Library({ go, onOpenProgress, onOpenRemix, onOpenEdit }) {
  const [jobs, setJobs] = useState({ finished: [], scripts: [], resumable: [] })
  const [error, setError] = useState('')
  const [loaded, setLoaded] = useState(false)
  const [confirmDel, setConfirmDel] = useState('')   // work_dir pending delete confirm
  const [busyDel, setBusyDel] = useState('')

  const load = () => api.listJobs().then((d) => { setJobs(d); setLoaded(true) }).catch((e) => { setError(e.message); setLoaded(true) })
  useEffect(() => { load() }, [])

  // Delete a partial render / unfinished job and its files (cancels it first if running).
  const del = async (wd) => {
    setBusyDel(wd); setError('')
    try { await api.deleteJob(wd); setConfirmDel(''); await load() }
    catch (e) { setError(e.message) } finally { setBusyDel('') }
  }

  return (
    <div>
      <div className="page-head">
        <div className="page-head__intro">
          <span className="label-sm reveal">Library</span>
          <h1 className="display-md reveal reveal-d1">Your films</h1>
        </div>
        <Button variant="primary" icon="plus" onClick={() => go('create')}>New film</Button>
      </div>

      <Banner tone="danger">{error}</Banner>

      {jobs.resumable.length > 0 && (
        <>
          <div className="label-sm" style={{ marginBottom: 12 }}>Active and unfinished</div>
          <div className="bento" style={{ marginBottom: 28 }}>
            {jobs.resumable.map((j, i) => (
              <Card key={i} span={4} className="reveal">
                <div style={{ fontWeight: 700 }}>{j.label}</div>
                <div className="row center between mt-16">
                  <Chip tone={j.running ? 'info' : 'warn'} dot>{j.running ? 'Rendering' : 'Needs attention'}</Chip>
                  <div className="row gap-6">
                    {j.running ? (
                      <Button variant="ghost" icon="gauge-high" onClick={() => onOpenProgress(j.work_dir)}>View render</Button>
                    ) : (
                      <Button variant="ghost" icon="play" onClick={() => onOpenProgress(j.work_dir)}>Continue</Button>
                    )}
                    {confirmDel === j.work_dir ? (
                      <>
                        <Button variant="danger" icon="trash-can" disabled={busyDel === j.work_dir} onClick={() => del(j.work_dir)}>{busyDel === j.work_dir ? 'Deleting…' : 'Confirm'}</Button>
                        <Button variant="ghost" disabled={busyDel === j.work_dir} onClick={() => setConfirmDel('')}>Cancel</Button>
                      </>
                    ) : (
                      <Button variant="ghost" icon="trash-can" onClick={() => setConfirmDel(j.work_dir)}>Delete</Button>
                    )}
                  </div>
                </div>
              </Card>
            ))}
          </div>
        </>
      )}

      <div className="label-sm" style={{ marginBottom: 12 }}>Finished</div>
      <div className="bento">
        {loaded && jobs.finished.length === 0 && <Card span={12}><p className="muted" style={{ fontSize: 13 }}>No finished films yet — create your first one.</p></Card>}
        {jobs.finished.map((f, i) => (
          <Card key={i} span={4} link onClick={() => onOpenRemix(f.work_dir)} className={`reveal reveal-d${(i % 4) + 1}`} style={{ padding: 0, overflow: 'hidden' }}>
            <div style={{ position: 'relative', aspectRatio: '16/9' }}>
              {f.cover_url
                ? <img src={f.cover_url} alt="" style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', objectFit: 'cover' }} />
                : <div className={`gfill g${i % 6}`} style={{ position: 'absolute', inset: 0 }}></div>
              }
              <div style={{ position: 'absolute', top: 12, left: 12 }}><Chip tone="ok" dot>Done</Chip></div>
              <div className="player__play" style={{ position: 'absolute', left: '50%', top: '50%', transform: 'translate(-50%,-50%)' }}><Icon name="play" /></div>
            </div>
            <div style={{ padding: '14px 18px 16px' }}>
              <div style={{ fontWeight: 700, letterSpacing: '-0.01em' }}>{f.label}</div>
              <div className="row gap-10 mt-16">
                <Button variant="ghost" icon="film" onClick={(e) => { e.stopPropagation(); onOpenEdit(f.work_dir) }}>Edit</Button>
                <Button variant="ghost" icon="sliders" onClick={(e) => { e.stopPropagation(); onOpenRemix(f.work_dir) }}>Remix</Button>
                <Button variant="primary" icon="youtube" onClick={(e) => { e.stopPropagation(); go('youtube', { publishWorkDir: f.work_dir }) }}>Publish</Button>
              </div>
            </div>
          </Card>
        ))}
      </div>
    </div>
  )
}
