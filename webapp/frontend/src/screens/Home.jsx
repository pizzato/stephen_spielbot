import { useState, useEffect } from 'react'
import { Card, Chip, Button, Icon } from '../components.jsx'
import { api } from '../api.js'

const PROMPTS = ['Roman Empire', 'Deep sea', 'How bread is made', 'The cold war']

function timeAgo(ts) {
  const s = Math.round(Date.now() / 1000 - ts)
  if (s < 5) return 'just now'
  if (s < 60) return `${s}s ago`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ago`
  return `${Math.floor(m / 60)}h ago`
}

export default function Home({ go }) {
  const [topic, setTopic] = useState('')
  const [films, setFilms] = useState([])
  const [queueCount, setQueueCount] = useState(0)
  const [activity, setActivity] = useState({
    current_op: {}, recent: [], render_active: false,
    render_pct: 0, render_msg: '', render_title: '', queue_pending: 0,
  })

  useEffect(() => {
    api.listJobs().then((d) => setFilms((d.finished || []).filter((f) => !f.archived))).catch(() => {})
    api.getQueue().then((d) => setQueueCount((d.queue || []).filter((q) => q.status === 'pending').length)).catch(() => {})
  }, [])

  useEffect(() => {
    const poll = () => api.getActivity().then(setActivity).catch(() => {})
    poll()
    const t = setInterval(poll, 3000)
    return () => clearInterval(t)
  }, [])

  const op = activity.current_op || {}
  const activeOps = (activity.live || activity.active_ops || []).length
    ? (activity.live || activity.active_ops)
    : (op.name ? [op] : [])
  // Backend now includes durable renders in live[]; only synthesize if missing.
  const hasRenderLive = activeOps.some((a) => a.category === 'render' || a.render)
  const renderOp = activity.render_active && !hasRenderLive
    ? {
        name: `Rendering ${activity.render_pct}%`,
        detail: activity.render_title || activity.render_msg || '',
        render: true,
        pct: activity.render_pct,
        eta_text: activity.render_eta,
        title: activity.render_title,
      }
    : null
  const visibleOps = activeOps.length ? [...activeOps, ...(renderOp ? [renderOp] : [])] : (renderOp ? [renderOp] : [])
  const isActive = visibleOps.length > 0

  return (
    <div>
      <div className="page-head">
        <div className="page-head__intro">
          <span className="label-sm reveal">Studio</span>
          <h1 className="display-lg reveal reveal-d1">What should we make a film about?</h1>
        </div>
      </div>

      <div className="bento">
        <Card span={8} padLg className="reveal reveal-d1">
          <span className="label-sm">New film</span>
          <div className="mt-16 stack gap-16">
            <input className="input input--xl" placeholder="The rise and fall of the Roman Empire…"
              value={topic} onChange={(e) => setTopic(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter' && topic.trim()) go('create', { topic }) }} />
            <div className="row row--wrap center between">
              <div className="row row--wrap gap-6">
                {PROMPTS.map((s) => (
                  <button key={s} className="btn btn--quiet" style={{ padding: '8px 14px', fontSize: 13 }}
                    onClick={() => setTopic(s)}>{s}</button>
                ))}
              </div>
              <Button variant="primary" size="lg" iconRight="arrow-right"
                disabled={!topic.trim()} onClick={() => go('create', { topic })}>Start</Button>
            </div>
          </div>
        </Card>

        <Card span={4} className="reveal reveal-d2" link onClick={() => go('about')}
          style={{ display: 'flex', flexDirection: 'column', justifyContent: 'space-between' }}>
          <div className="row center between">
            <div className="row center gap-16">
              <img src="/assets/StephenSpielbot.png" alt="" style={{ width: 64, height: 64, borderRadius: 16, border: '1px solid var(--line)' }} />
              <div>
                <div className="h-title">Stephen Spielbot</div>
                <div className="muted" style={{ fontSize: 13 }}>AI slop video director</div>
              </div>
            </div>
            <span className="card__arrow"><Icon name="circle-info" /></span>
          </div>
          <p className="body-1 mt-16" style={{ fontSize: 14 }}>
            Give me a topic. I'll write the script, paint every scene, record the narration, score it, and cut the whole thing together.
          </p>
        </Card>

        {/* Activity card — compact feed; full timeline is on Activity */}
        <Card span={12} className="reveal reveal-d2">
          <div className="card__head">
            <span className="label-sm">Activity</span>
            <div className="row center gap-10">
              {isActive
                ? <Chip tone="accent" dot>Running</Chip>
                : <span style={{ fontSize: 12, color: 'var(--ink-4)' }}>Idle</span>}
              {activity.render_eta ? (
                <Chip tone="info" dot>{activity.render_eta} left</Chip>
              ) : null}
              <button
                type="button"
                className="btn btn--quiet"
                style={{ padding: '6px 12px', fontSize: 12.5 }}
                onClick={() => go('activity')}
              >
                Full activity →
              </button>
            </div>
          </div>
          <div className="stream" style={{ marginTop: 4 }}>
            {visibleOps.map((active, i) => (
              <div className="stream-entry" key={`${active.name || 'active'}-${active.started_at || i}`}>
                <span className="stream-ico" style={{
                  background: active.status === 'queued' ? 'var(--paper-2)' : 'var(--accent-soft)',
                  color: active.status === 'queued' ? 'var(--ink-3)' : 'var(--accent)',
                }}>
                  <Icon name={active.status === 'queued' ? 'hourglass-half' : 'rotate'} spin={active.status !== 'queued'} />
                </span>
                <div className="grow">
                  <span className="stream-title">{active.name}</span>
                  {(active.detail || active.title) && (
                    <span className="muted" style={{ fontSize: 12, marginLeft: 8 }}>
                      {active.title && active.detail && !String(active.detail).includes(active.title)
                        ? `${active.detail} · ${active.title}`
                        : (active.detail || active.title)}
                    </span>
                  )}
                </div>
                <span style={{ fontSize: 12, color: 'var(--ink-3)', fontVariantNumeric: 'tabular-nums', whiteSpace: 'nowrap' }}>
                  {active.eta_text ? `${active.eta_text} left` : null}
                  {active.eta_text && active.pct != null ? ' · ' : null}
                  {(active.render || active.pct != null) ? `${active.pct ?? activity.render_pct}%` : null}
                </span>
              </div>
            ))}
            {(activity.recent || []).slice(0, 5).map((ev, i) => (
              <div key={ev.id || i} className="stream-entry">
                <span className="stream-ico" style={{ background: 'var(--paper-2)', color: 'var(--ink-3)' }}>
                  <Icon name="clock-rotate-left" />
                </span>
                <div className="grow">
                  <span className="stream-title">{ev.name}</span>
                  {(ev.detail || ev.title) && (
                    <span className="muted" style={{ fontSize: 12, marginLeft: 8 }}>
                      {ev.title && ev.detail && !String(ev.detail).includes(ev.title)
                        ? `${ev.detail} · ${ev.title}`
                        : (ev.detail || ev.title)}
                    </span>
                  )}
                </div>
                <span style={{ fontSize: 11.5, color: 'var(--ink-4)', whiteSpace: 'nowrap' }}>
                  {ev.duration_s != null ? `${ev.duration_s}s · ` : ''}{timeAgo(ev.ts)}
                </span>
              </div>
            ))}
            {!isActive && (activity.recent || []).length === 0 && (
              <div className="muted" style={{ fontSize: 13, padding: '8px 0' }}>No recent activity.</div>
            )}
          </div>
        </Card>

        <Card span={7} className="reveal reveal-d2">
          <div className="card__head">
            <span className="label-sm">Recent films</span>
            <button className="btn btn--quiet" style={{ padding: '6px 12px', fontSize: 12.5 }} onClick={() => go('library')}>All films →</button>
          </div>
          <div className="stream">
            {films.length === 0 && <div className="muted" style={{ fontSize: 13, padding: '8px 0' }}>No finished films yet.</div>}
            {films.slice(0, 5).map((r, i) => (
              <a key={i} className="stream-entry" onClick={(e) => { e.preventDefault(); go('library') }} href="#">
                <span className="stream-ico" style={{ background: 'var(--ok-soft)', color: 'var(--ok)' }}><Icon name="circle-check" /></span>
                <div className="grow"><span className="stream-title">{r.title || r.label}</span></div>
                <Icon name="chevron-right" style={{ color: 'var(--ink-4)', fontSize: 12 }} />
              </a>
            ))}
          </div>
        </Card>

        <Card span={5} className="reveal reveal-d3" link onClick={() => go('queue')}>
          <div className="card__head"><span className="label-sm">Queue</span><span className="card__arrow"><Icon name="layer-group" /></span></div>
          <div className="metric">{queueCount}</div>
          <div className="muted mt-8" style={{ fontSize: 13 }}>requests waiting to render</div>
        </Card>

        <Card span={6} className="reveal reveal-d4" link onClick={() => go('community')}>
          <div className="card__head"><span className="label-sm">Community</span><span className="card__arrow"><Icon name="comments" /></span></div>
          <p className="body-1" style={{ fontSize: 13.5, marginTop: 0 }}>Pull comments and mentions across YouTube and X, evaluate requests, and reply.</p>
          <div className="row center gap-10 mt-16"><Chip tone="ok" dot>@StephenSpielbot</Chip></div>
        </Card>

        <Card span={6} className="reveal reveal-d4" link onClick={() => go('create')}>
          <div className="card__head"><span className="label-sm">Create</span><span className="card__arrow"><Icon name="wand-magic-sparkles" /></span></div>
          <p className="body-1" style={{ fontSize: 13.5, marginTop: 0 }}>Start from a blank page — set the title, scene count, voice and visual style.</p>
          <div className="row center gap-10 mt-16"><Chip tone="accent" dot>New film</Chip></div>
        </Card>
      </div>
    </div>
  )
}
