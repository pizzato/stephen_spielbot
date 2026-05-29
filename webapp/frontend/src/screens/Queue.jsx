import React, { useState, useEffect } from 'react'
import { Card, Chip, Button, Icon, Banner } from '../components.jsx'
import { api } from '../api.js'

const STATUS_CHIP = {
  pending: ['accent', 'Queued'], creating: ['info', 'Rendering'], running: ['info', 'Rendering'],
  done: ['ok', 'Done'], posted: ['ok', 'Posted'], failed: ['danger', 'Failed'], cancelled: ['neutral', 'Cancelled'],
}
function tier(n) { if (!n) return ''; if (n <= 11) return 'SHORT'; if (n <= 39) return 'MEDIUM'; return 'LARGE' }

export default function Queue({ go }) {
  const [items, setItems] = useState([])
  const [error, setError] = useState('')
  const [status, setStatus] = useState('')
  const [busy, setBusy] = useState('')
  const [loaded, setLoaded] = useState(false)

  const refresh = () => api.getQueue().then((d) => { setItems(d.queue || []); setLoaded(true) }).catch((e) => { setError(e.message); setLoaded(true) })
  useEffect(() => { refresh() }, [])

  const run = async (key, fn, after) => {
    setBusy(key); setError(''); setStatus('')
    try { const r = await fn(); if (after) after(r); await refresh() }
    catch (e) { setError(e.message) } finally { setBusy('') }
  }

  const addManual = () => {
    const title = window.prompt('Video title to add to the queue:', '')
    if (!title) return
    const n = parseInt(window.prompt('How many scenes? (6–50)', '6') || '6', 10)
    run('add', () => api.queueAdd(title, n, ''), () => setStatus(`Added: ${title}`))
  }

  const pendingCount = items.filter((i) => i.status === 'pending').length
  const counts = {
    pending: pendingCount,
    creating: items.filter((i) => ['creating', 'running'].includes(i.status)).length,
    posted: items.filter((i) => i.status === 'posted').length,
  }

  return (
    <div>
      <div className="page-head">
        <div className="page-head__intro">
          <span className="label-sm reveal">Queue</span>
          <h1 className="display-md reveal reveal-d1">Video request queue</h1>
        </div>
        <Button variant="ghost" icon="pen" onClick={addManual}>Add manually</Button>
      </div>

      <Banner tone="danger">{error}</Banner>
      {status && <Banner tone="ok">{status}</Banner>}

      <div className="bento">
        {/* Automation — on-demand steps (the always-on loop is driven by the Settings toggles). */}
        <Card span={12} well className="reveal reveal-d1">
          <div className="row center between row--wrap gap-16">
            <div className="row center gap-10">
              <span className="stream-ico" style={{ background: 'var(--accent-soft)', color: 'var(--accent)' }}><Icon name="robot" /></span>
              <div><div style={{ fontWeight: 600 }}>Automation</div><div className="muted" style={{ fontSize: 12.5 }}>Run a step now, or enable hands-free mode in Settings.</div></div>
            </div>
            <div className="row gap-10 row--wrap">
              <Button variant="ghost" icon="rotate" disabled={!!busy} onClick={() => run('fetch', api.autoFetch, (r) => setStatus(`Fetched ${r.new} new · ${r.auto_approved} auto-approved`))}>{busy === 'fetch' ? 'Fetching…' : 'Fetch & evaluate'}</Button>
              <Button variant="ghost" icon="play" disabled={!!busy} onClick={() => run('start', api.autoStart, (r) => setStatus(r.started ? `Started: ${r.started.title}` : 'Nothing to start (busy or empty).'))}>{busy === 'start' ? 'Starting…' : 'Auto-start best'}</Button>
              <Button variant="ghost" icon="youtube" disabled={!!busy} onClick={() => run('post', api.autoPost, (r) => setStatus(`Posted ${r.posted.length} video(s).`))}>{busy === 'post' ? 'Posting…' : 'Auto-post finished'}</Button>
              <Button variant="primary" icon="bolt" disabled={!!busy} onClick={() => run('tick', api.autoTick, () => setStatus('Ran one automation tick.'))}>{busy === 'tick' ? 'Running…' : 'Run full tick'}</Button>
            </div>
          </div>
        </Card>

        <Card span={4} className="reveal reveal-d1"><span className="label-sm">Queued</span><div className="metric mt-8">{counts.pending}</div><div className="muted" style={{ fontSize: 13 }}>waiting to render</div></Card>
        <Card span={4} className="reveal reveal-d2"><span className="label-sm">Rendering</span><div className="metric mt-8">{counts.creating}</div><div className="muted" style={{ fontSize: 13 }}>in progress now</div></Card>
        <Card span={4} className="reveal reveal-d3"><span className="label-sm">Posted</span><div className="metric mt-8">{counts.posted}</div><div className="muted" style={{ fontSize: 13 }}>live on YouTube</div></Card>

        <Card span={12} className="reveal reveal-d3" style={{ padding: 0, overflow: 'hidden' }}>
          <div style={{ padding: '16px 22px', borderBottom: '1px solid var(--line)' }}>
            <span className="label-sm">Up next</span>
            <span className="muted" style={{ fontSize: 12.5, marginLeft: 10 }}>Comment requests rank above ideas. Reorder with the arrows.</span>
          </div>
          {loaded && items.length === 0 && <div className="muted" style={{ fontSize: 13, padding: '18px 22px' }}>The queue is empty. Approve a comment request (YouTube tab) or add one manually.</div>}
          {items.map((it, idx) => {
            const [tone, label] = STATUS_CHIP[it.status] || ['neutral', it.status]
            const titleText = it.final_title || it.title || '(untitled)'
            const scenes = it.suggested_scene_count
            const isPending = it.status === 'pending'
            return (
              <div key={it.id || idx} className="row center" style={{ gap: 14, padding: '14px 22px', borderBottom: idx < items.length - 1 ? '1px solid var(--line)' : 'none', opacity: ['posted', 'done', 'cancelled'].includes(it.status) ? 0.62 : 1 }}>
                <div className="stack" style={{ gap: 2 }}>
                  <button className="qmove" disabled={!isPending || !!busy} onClick={() => run('m' + it.id, () => api.queueMove(it.id, -1))}><Icon name="chevron-up" /></button>
                  <button className="qmove" disabled={!isPending || !!busy} onClick={() => run('m' + it.id, () => api.queueMove(it.id, 1))}><Icon name="chevron-down" /></button>
                </div>
                <div className="grow">
                  <div style={{ fontWeight: 600, letterSpacing: '-0.01em' }}>{titleText}</div>
                  <div className="row center gap-10 mt-8" style={{ flexWrap: 'wrap' }}>
                    {it.source && <Chip tone="info">{it.source}</Chip>}
                    {it.interestingness != null && <span style={{ color: 'var(--warm)', fontWeight: 600, fontSize: 13 }}><Icon name="star" style={{ fontSize: 11 }} /> {Number(it.interestingness).toFixed(1)}</span>}
                    {scenes ? <span className="muted" style={{ fontSize: 12.5 }}>{scenes} scenes · {tier(scenes)}</span> : null}
                    {it.commenter && <span className="muted" style={{ fontSize: 12.5 }}>· {it.commenter}</span>}
                  </div>
                </div>
                <Chip tone={tone} dot>{label}</Chip>
                <div className="row gap-6">
                  {isPending && <Button variant="primary" icon="play" disabled={!!busy} onClick={() => run('s' + it.id, () => api.queueStart(it.id), () => { setStatus('Render started.'); go('progress') })}>Render now</Button>}
                  {it.status === 'done' && <Button variant="ghost" icon="youtube" onClick={() => go('youtube')}>Publish</Button>}
                  {(isPending || ['done', 'posted', 'cancelled'].includes(it.status)) &&
                    <button className="qmove qmove--lg" disabled={!!busy} onClick={() => run('d' + it.id, () => api.queueRemove(it.id))} title="Remove"><Icon name="trash-can" /></button>}
                </div>
              </div>
            )
          })}
        </Card>
      </div>
    </div>
  )
}
