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
  const [loaded, setLoaded] = useState(false)

  useEffect(() => {
    api.getQueue().then((d) => { setItems(d.queue || []); setLoaded(true) }).catch((e) => { setError(e.message); setLoaded(true) })
  }, [])

  const counts = {
    pending: items.filter((i) => i.status === 'pending').length,
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
        <Button variant="ghost" icon="wand-magic-sparkles" onClick={() => go('create')}>Create directly</Button>
      </div>

      <Banner tone="danger">{error}</Banner>

      <div className="bento">
        <Card span={4} className="reveal reveal-d1"><span className="label-sm">Queued</span><div className="metric mt-8">{counts.pending}</div><div className="muted" style={{ fontSize: 13 }}>waiting to render</div></Card>
        <Card span={4} className="reveal reveal-d2"><span className="label-sm">Rendering</span><div className="metric mt-8">{counts.creating}</div><div className="muted" style={{ fontSize: 13 }}>in progress now</div></Card>
        <Card span={4} className="reveal reveal-d3"><span className="label-sm">Posted</span><div className="metric mt-8">{counts.posted}</div><div className="muted" style={{ fontSize: 13 }}>live on YouTube</div></Card>

        <Card span={12} className="reveal reveal-d3" style={{ padding: 0, overflow: 'hidden' }}>
          <div style={{ padding: '16px 22px', borderBottom: '1px solid var(--line)' }}>
            <span className="label-sm">Up next</span>
            <span className="muted" style={{ fontSize: 12.5, marginLeft: 10 }}>Comment requests rank above ideas.</span>
          </div>
          {loaded && items.length === 0 && <div className="muted" style={{ fontSize: 13, padding: '18px 22px' }}>The queue is empty. Approve a comment request or add one from the YouTube tab.</div>}
          {items.map((it, idx) => {
            const [tone, label] = STATUS_CHIP[it.status] || ['neutral', it.status]
            const titleText = it.final_title || it.title || it.suggested_title || '(untitled)'
            const scenes = it.n_scenes || it.scenes
            return (
              <div key={it.id || idx} className="row center" style={{ gap: 16, padding: '14px 22px', borderBottom: idx < items.length - 1 ? '1px solid var(--line)' : 'none', opacity: ['posted', 'done'].includes(it.status) ? 0.62 : 1 }}>
                <div className="grow">
                  <div style={{ fontWeight: 600, letterSpacing: '-0.01em' }}>{titleText}</div>
                  <div className="row center gap-10 mt-8" style={{ flexWrap: 'wrap' }}>
                    {it.source && <Chip tone="info">{it.source}</Chip>}
                    {it.interestingness != null && <span style={{ color: 'var(--warm)', fontWeight: 600, fontSize: 13 }}><Icon name="star" style={{ fontSize: 11 }} /> {Number(it.interestingness).toFixed(1)}</span>}
                    {scenes ? <span className="muted" style={{ fontSize: 12.5 }}>{scenes} scenes · {tier(scenes)}</span> : null}
                    {it.author && <span className="muted" style={{ fontSize: 12.5 }}>· {it.author}</span>}
                  </div>
                </div>
                <Chip tone={tone} dot>{label}</Chip>
              </div>
            )
          })}
        </Card>
      </div>
    </div>
  )
}
