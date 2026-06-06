import React, { useState, useEffect, useRef } from 'react'
import { Card, Chip, Button, ProgressBar, Icon, Banner } from '../components.jsx'
import { api, fileUrl } from '../api.js'

const STATUS_TONE = {
  running: 'ok', leased: 'info', queued: 'accent', succeeded: 'neutral',
  failed_retryable: 'warn', failed_terminal: 'danger', lost: 'warn', cancelled: 'neutral',
}

export default function Progress({ workDir, job, go, onOpenScript }) {
  const [p, setP] = useState(null)
  const [error, setError] = useState('')
  const [action, setAction] = useState('')
  const timer = useRef(null)

  useEffect(() => {
    let alive = true
    const tick = async () => {
      try {
        const data = await api.getProgress(workDir)
        if (alive) { setP(data); setError('') }
      } catch (e) { if (alive) setError(e.message) }
    }
    tick()
    timer.current = setInterval(tick, 2500)
    return () => { alive = false; clearInterval(timer.current) }
  }, [workDir])

  const pct = p?.pct ?? 0
  const done = p?.done
  const tasks = p?.tasks || []
  const counts = p?.counts || {}
  const title = p?.title || job?.title || 'Rendering'

  const doAction = async (fn) => {
    setAction('busy'); setError('')
    try { const r = await fn(p?.work_dir || workDir); if (r?.message) setError('') } catch (e) { setError(e.message) } finally { setAction('') }
  }

  return (
    <div>
      <div className="page-head">
        <div className="page-head__intro">
          <span className="label-sm reveal">Rendering</span>
          <h1 className="display-md reveal reveal-d1">{title}</h1>
        </div>
        <div className="row center gap-10 reveal reveal-d1">
          <Button variant="ghost" icon="feather-pointed" disabled={action === 'script' || !(p?.work_dir || workDir)}
            onClick={async () => {
              setAction('script'); setError('')
              try { await onOpenScript(p?.work_dir || workDir) } catch (e) { setError(e.message); setAction('') }
            }}>{action === 'script' ? 'Opening…' : 'Edit script'}</Button>
          {done ? <Chip tone="ok" dot>Done</Chip> : <Chip tone="info" dot>{Math.round(pct)}%</Chip>}
        </div>
      </div>

      <Banner tone="danger">{error}</Banner>

      <div className="bento">
        <Card span={8} padLg className="reveal reveal-d1">
          <div className="row center between">
            <span style={{ fontWeight: 600 }}>{p?.msg || 'Waiting to start…'}</span>
            <span className="muted mono">{Object.entries(counts).map(([k, v]) => `${k}:${v}`).join('  ·  ')}</span>
          </div>
          <div className="mt-16"><ProgressBar pct={pct} /></div>

          {done && (
            <div className="row gap-10 mt-24">
              <Button variant="primary" icon="sliders" onClick={() => go('remix', { workDir: p?.work_dir || workDir })}>Open in Remix</Button>
              {p?.final_url && <a className="btn btn--ghost" href={p.final_url} download><Icon name="download" /> Download</a>}
            </div>
          )}

          <div className="mt-24">
            <span className="label-sm">Tasks</span>
            <div className="stack mt-16" style={{ maxHeight: 360, overflow: 'auto' }}>
              {tasks.length === 0 && <div className="muted" style={{ fontSize: 13 }}>No durable tasks recorded yet.</div>}
              {tasks.map((t, i) => (
                <div key={i} className="row center between" style={{ padding: '8px 0', borderTop: i ? '1px solid var(--line)' : 'none' }}>
                  <div className="grow" style={{ minWidth: 0 }}>
                    <div style={{ fontSize: 13.5, fontWeight: 500, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{t.name}</div>
                    {t.error ? <div className="muted" style={{ fontSize: 12, color: 'var(--danger)' }}>{String(t.error).slice(0, 100)}</div> : null}
                  </div>
                  <span className="muted mono" style={{ fontSize: 11 }}>{t.attempt}/{t.max_attempts}</span>
                  <Chip tone={STATUS_TONE[t.status] || 'neutral'} dot>{t.status}</Chip>
                </div>
              ))}
            </div>
          </div>
        </Card>

        <div className="col-4 stack gap-16">
          <Card className="reveal reveal-d2">
            <span className="label-sm">Workers</span>
            <div className="stack gap-10 mt-16">
              {(p?.workers || []).length === 0 && <div className="muted" style={{ fontSize: 13 }}>No workers registered.</div>}
              {(p?.workers || []).map((w, i) => (
                <div key={i} className="row center between">
                  <span style={{ fontSize: 13 }}>{w.kind} · <span className="muted">{w.endpoint}</span></span>
                  <Chip tone={w.status === 'online' ? 'ok' : 'neutral'} dot>{w.status}</Chip>
                </div>
              ))}
            </div>
          </Card>

          {p?.cover_url && (
            <Card className="reveal reveal-d3">
              <span className="label-sm">Thumbnail</span>
              <div className="mt-16" style={{ position: 'relative', borderRadius: 'var(--r-md)', overflow: 'hidden', aspectRatio: '16/9' }}>
                <img src={p.cover_url} alt="" style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', objectFit: 'cover' }} />
              </div>
            </Card>
          )}

          <Card well className="reveal reveal-d4">
            <span className="label-sm">Controls</span>
            <div className="row gap-10 mt-16 row--wrap">
              <Button variant="ghost" icon="pause" disabled={action === 'busy' || done} onClick={() => doAction(api.pauseJob)}>Pause</Button>
              <Button variant="ghost" icon="rotate-right" disabled={action === 'busy'} onClick={() => doAction(api.retryJob)}>Retry failed</Button>
              <Button variant="ghost" icon="play" disabled={action === 'busy'} onClick={() => doAction(api.resumeJob)}>Resume</Button>
              <Button variant="danger" icon="stop" disabled={action === 'busy'} onClick={() => doAction(api.cancelJob)}>Cancel</Button>
            </div>
            <div className="mt-10">
              <Button variant="danger" icon="trash-can" disabled={action === 'busy'} onClick={async () => {
                setAction('busy'); setError('')
                try { await api.deleteJob(p?.work_dir || workDir); go('library') }
                catch (e) { setError(e.message); setAction('') }
              }}>Delete job &amp; files</Button>
            </div>
          </Card>
        </div>
      </div>
    </div>
  )
}
