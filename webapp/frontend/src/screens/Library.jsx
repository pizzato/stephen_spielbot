import React, { useState, useEffect } from 'react'
import { Card, Chip, Button, Icon, Banner } from '../components.jsx'
import { api } from '../api.js'

export default function Library({ go, onOpenRemix }) {
  const [jobs, setJobs] = useState({ finished: [], scripts: [], resumable: [] })
  const [error, setError] = useState('')
  const [loaded, setLoaded] = useState(false)

  useEffect(() => {
    api.listJobs().then((d) => { setJobs(d); setLoaded(true) }).catch((e) => { setError(e.message); setLoaded(true) })
  }, [])

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
          <div className="label-sm" style={{ marginBottom: 12 }}>In progress / resumable</div>
          <div className="bento" style={{ marginBottom: 28 }}>
            {jobs.resumable.map((j, i) => (
              <Card key={i} span={4} className="reveal">
                <div style={{ fontWeight: 700 }}>{j.label}</div>
                <div className="row center between mt-16">
                  <Chip tone="info" dot>In progress</Chip>
                  <Button variant="ghost" icon="play" onClick={() => api.resumeJob(j.work_dir).then(() => go('progress'))}>Resume</Button>
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
              <div className={`gfill g${i % 6}`} style={{ position: 'absolute', inset: 0 }}></div>
              <div style={{ position: 'absolute', top: 12, left: 12 }}><Chip tone="ok" dot>Done</Chip></div>
              <div className="player__play" style={{ position: 'absolute', left: '50%', top: '50%', transform: 'translate(-50%,-50%)' }}><Icon name="play" /></div>
            </div>
            <div style={{ padding: '16px 18px 18px' }}>
              <div style={{ fontWeight: 700, letterSpacing: '-0.01em' }}>{f.label}</div>
            </div>
          </Card>
        ))}
      </div>
    </div>
  )
}
