import { useState, useEffect, useMemo, useCallback } from 'react'
import { Card, Chip, Button, Icon, Banner, ProgressBar } from '../components.jsx'
import { api } from '../api.js'
import { nameOf } from '../nav.js'

function timeAgo(ts) {
  if (!ts) return ''
  const s = Math.round(Date.now() / 1000 - ts)
  if (s < 5) return 'just now'
  if (s < 60) return `${s}s ago`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ago`
  const h = Math.floor(m / 60)
  if (h < 48) return `${h}h ago`
  return `${Math.floor(h / 24)}d ago`
}

function fmtDuration(secs) {
  if (secs == null || Number.isNaN(secs)) return ''
  const s = Math.max(0, Number(secs))
  if (s < 90) return `${Math.round(s)}s`
  const m = Math.floor(s / 60)
  if (m < 90) return `${m}m ${Math.round(s % 60)}s`
  const h = Math.floor(m / 60)
  return `${h}h ${m % 60}m`
}

function wallTime(ts) {
  if (!ts) return ''
  try {
    return new Date(ts * 1000).toLocaleString(undefined, {
      month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
    })
  } catch {
    return ''
  }
}

const STATUS_TONE = {
  running: 'ok',
  queued: 'info',
  done: 'neutral',
  error: 'danger',
  cancelled: 'warn',
}

const CATEGORY_ICON = {
  render: 'gauge-high',
  film: 'film',
  script: 'feather-pointed',
  automation: 'robot',
  publish: 'upload',
  system: 'gear',
  other: 'circle-info',
}

function EventRow({ ev, go }) {
  const running = ev.status === 'running'
  const queued = ev.status === 'queued'
  const live = running || queued
  const icon = CATEGORY_ICON[ev.category] || 'circle-info'
  const right = running
    ? [
        ev.eta_text ? `${ev.eta_text} left` : null,
        ev.elapsed_s != null ? `${fmtDuration(ev.elapsed_s)} elapsed` : null,
        ev.pct != null ? `${ev.pct}%` : null,
      ].filter(Boolean).join(' · ')
    : queued
      ? (ev.elapsed_s != null ? `waiting ${fmtDuration(ev.elapsed_s)}` : 'waiting')
      : [
          ev.duration_s != null ? fmtDuration(ev.duration_s) : null,
          timeAgo(ev.ts),
        ].filter(Boolean).join(' · ')

  const openFilm = () => {
    if (!ev.work_dir || !go) return
    if (ev.category === 'render' && running) go('progress', { workDir: ev.work_dir })
    else go('editfilm', { workDir: ev.work_dir })
  }

  return (
    <div className="stream-entry" style={{ alignItems: 'flex-start' }}>
      <span
        className="stream-ico"
        style={{
          background: running ? 'var(--accent-soft)' : 'var(--paper-2)',
          color: running ? 'var(--accent)' : 'var(--ink-3)',
          marginTop: 2,
        }}
      >
        <Icon name={running ? 'rotate' : queued ? 'hourglass-half' : icon} spin={running} />
      </span>
      <div className="grow" style={{ minWidth: 0 }}>
        <div className="row center gap-8" style={{ flexWrap: 'wrap' }}>
          <span className="stream-title">{ev.name}</span>
          {ev.status && ev.status !== 'done' && (
            <Chip tone={STATUS_TONE[ev.status] || 'neutral'} dot>
              {ev.status}
            </Chip>
          )}
        </div>
        {ev.detail ? (
          <div className="muted" style={{ fontSize: 12.5, marginTop: 2 }}>
            {ev.detail}
          </div>
        ) : null}
        {running && ev.pct != null ? (
          <div className="mt-8" style={{ maxWidth: 280 }}>
            <ProgressBar pct={ev.pct} />
          </div>
        ) : null}
        {!live && ev.ts ? (
          <div className="muted" style={{ fontSize: 11, marginTop: 3 }}>
            {wallTime(ev.started_at || ev.ts)}
            {ev.duration_s != null ? ` · took ${fmtDuration(ev.duration_s)}` : ''}
          </div>
        ) : null}
      </div>
      <div className="stack gap-6" style={{ alignItems: 'flex-end', flexShrink: 0 }}>
        {right ? (
          <span
            className="muted mono"
            style={{ fontSize: 11.5, whiteSpace: 'nowrap', textAlign: 'right' }}
          >
            {right}
          </span>
        ) : null}
        {ev.work_dir && go ? (
          <button
            type="button"
            className="btn btn--quiet"
            style={{ padding: '4px 10px', fontSize: 12 }}
            onClick={openFilm}
          >
            {running && ev.category === 'render' ? 'Open render' : 'Open film'}
          </button>
        ) : null}
      </div>
    </div>
  )
}

function GroupCard({ group, expandAll, go }) {
  // Local open state so collapsing a group survives activity polls.
  // Live groups start open; background noise starts collapsed; Expand all forces open.
  const live = (group.live_count || 0) > 0
  const [open, setOpen] = useState(live || expandAll)
  useEffect(() => {
    if (expandAll) setOpen(true)
    else if (!live) setOpen(false)
  }, [expandAll, live])
  useEffect(() => {
    if (live) setOpen(true)
  }, [live])
  const items = group.items || []
  const runningItems = items.filter((e) => e.status === 'running')
  const queuedItems = items.filter((e) => e.status === 'queued')
  const historyItems = items.filter((e) => e.status !== 'running' && e.status !== 'queued')

  return (
    <Card span={12} className="reveal">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="row center between"
        style={{
          width: '100%',
          background: 'none',
          border: 'none',
          padding: 0,
          cursor: 'pointer',
          textAlign: 'left',
          color: 'inherit',
          gap: 12,
        }}
      >
        <div className="row center gap-10 grow" style={{ minWidth: 0 }}>
          <Icon
            name={open ? 'chevron-down' : 'chevron-right'}
            style={{ color: 'var(--ink-4)', width: 14, flexShrink: 0 }}
          />
          <div className="grow" style={{ minWidth: 0 }}>
            <div
              style={{
                fontWeight: 600,
                fontSize: 15,
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
              }}
            >
              {group.title || group.label}
            </div>
            <div className="muted" style={{ fontSize: 12, marginTop: 2 }}>
              {group.noise
                ? 'Automation ticks and other background work'
                : group.work_dir
                  ? nameOf(group.work_dir)
                  : group.label}
              {' · '}
              {items.length} event{items.length === 1 ? '' : 's'}
              {live ? ` · ${group.live_count} live` : ''}
            </div>
          </div>
        </div>
        <div className="row center gap-8" style={{ flexShrink: 0 }}>
          {live ? <Chip tone="accent" dot>Live</Chip> : null}
          {group.noise ? <Chip tone="neutral">Background</Chip> : null}
          {!live && historyItems[0]?.duration_s != null ? (
            <span className="muted mono" style={{ fontSize: 11.5 }}>
              last {fmtDuration(historyItems[0].duration_s)}
            </span>
          ) : null}
        </div>
      </button>

      {open && (
        <div className="stream" style={{ marginTop: 14 }}>
          {runningItems.map((ev) => (
            <EventRow key={ev.id || `${ev.name}-${ev.started_at}`} ev={ev} go={go} />
          ))}
          {queuedItems.map((ev) => (
            <EventRow key={ev.id || `${ev.name}-${ev.started_at}`} ev={ev} go={go} />
          ))}
          {historyItems.map((ev) => (
            <EventRow key={ev.id || `${ev.name}-${ev.ts}`} ev={ev} go={go} />
          ))}
          {items.length === 0 && (
            <div className="muted" style={{ fontSize: 13, padding: '8px 0' }}>No events.</div>
          )}
        </div>
      )}
    </Card>
  )
}

export default function Activity({ go }) {
  const [data, setData] = useState({
    live: [], history: [], groups: [], live_count: 0,
    render_active: false, render_pct: 0, render_title: '', render_eta: null,
  })
  const [error, setError] = useState('')
  const [filter, setFilter] = useState('all') // all | live | films | background
  const [expandAll, setExpandAll] = useState(false)

  const poll = useCallback(async () => {
    try {
      const d = await api.getActivity({ limit: 120 })
      setData(d)
      setError('')
    } catch (e) {
      setError(e.message)
    }
  }, [])

  useEffect(() => {
    poll()
    const t = setInterval(poll, 2500)
    return () => clearInterval(t)
  }, [poll])

  const groups = useMemo(() => {
    const raw = data.groups || []
    if (filter === 'all') return raw
    if (filter === 'live') return raw.filter((g) => g.live_count > 0)
    if (filter === 'films') return raw.filter((g) => String(g.key || '').startsWith('film:'))
    if (filter === 'background') return raw.filter((g) => g.noise)
    return raw
  }, [data.groups, filter])

  const liveCount = data.live_count || (data.live || []).length || 0
  const isActive = liveCount > 0 || data.render_active

  return (
    <div>
      <div className="page-head">
        <div className="page-head__intro">
          <span className="label-sm reveal">Studio</span>
          <h1 className="display-md reveal reveal-d1">Activity</h1>
          <p className="muted reveal reveal-d1" style={{ marginTop: 6, maxWidth: 560 }}>
            Everything the studio is doing — renders, upscales, scripts — with ETAs when we have them,
            plus a history of how long each step took. Fold a film to see only its work; background
            automation is grouped separately.
          </p>
        </div>
        <div className="row center gap-10 reveal reveal-d1">
          {isActive
            ? <Chip tone="accent" dot>{liveCount || 1} live</Chip>
            : <Chip tone="neutral" dot>Idle</Chip>}
          {data.render_active && data.render_eta ? (
            <Chip tone="info" dot>{data.render_eta} left</Chip>
          ) : null}
          <Button variant="ghost" icon="rotate" onClick={poll}>Refresh</Button>
        </div>
      </div>

      <Banner tone="danger">{error}</Banner>

      {data.render_active && (
        <Card span={12} padLg className="reveal reveal-d1" style={{ marginBottom: 16 }}>
          <div className="row center between gap-16" style={{ flexWrap: 'wrap' }}>
            <div className="grow" style={{ minWidth: 200 }}>
              <span className="label-sm">Rendering now</span>
              <div style={{ fontWeight: 600, fontSize: 16, marginTop: 6 }}>
                {data.render_title || 'Film render'}
              </div>
              <div className="muted" style={{ fontSize: 13, marginTop: 4 }}>{data.render_msg}</div>
              <div className="mt-12"><ProgressBar pct={data.render_pct || 0} /></div>
            </div>
            <div className="stack gap-10" style={{ alignItems: 'flex-end' }}>
              <span style={{ fontSize: 28, fontWeight: 700, letterSpacing: '-0.02em' }}>
                {data.render_pct || 0}%
              </span>
              {data.render_eta ? (
                <span className="muted">{data.render_eta} remaining</span>
              ) : null}
              <Button
                variant="primary"
                icon="gauge-high"
                onClick={() => {
                  const liveRender = (data.live || []).find((e) => e.category === 'render' && e.work_dir)
                  if (liveRender?.work_dir) go('progress', { workDir: liveRender.work_dir })
                  else go('progress')
                }}
              >
                Open render
              </Button>
            </div>
          </div>
        </Card>
      )}

      <div className="row center between gap-10 reveal reveal-d1" style={{ marginBottom: 14, flexWrap: 'wrap' }}>
        <div className="row gap-6" style={{ flexWrap: 'wrap' }}>
          {[
            { id: 'all', label: 'All' },
            { id: 'live', label: 'Live' },
            { id: 'films', label: 'By film' },
            { id: 'background', label: 'Background' },
          ].map((f) => (
            <button
              key={f.id}
              type="button"
              className={`btn ${filter === f.id ? 'btn--primary' : 'btn--quiet'}`}
              style={{ padding: '8px 14px', fontSize: 13 }}
              onClick={() => setFilter(f.id)}
            >
              {f.label}
            </button>
          ))}
        </div>
        <button
          type="button"
          className="btn btn--quiet"
          style={{ padding: '8px 14px', fontSize: 13 }}
          onClick={() => setExpandAll((v) => !v)}
        >
          {expandAll ? 'Collapse groups' : 'Expand all'}
        </button>
      </div>

      <div className="bento">
        {groups.length === 0 && (
          <Card span={12} className="reveal">
            <div className="muted" style={{ fontSize: 14, padding: '12px 0' }}>
              {filter === 'live'
                ? 'Nothing running right now.'
                : 'No activity recorded yet. Renders, upscales, and edits will show up here.'}
            </div>
          </Card>
        )}
        {groups.map((g) => (
          <GroupCard
            key={g.key}
            group={g}
            expandAll={expandAll}
            go={go}
          />
        ))}
      </div>
    </div>
  )
}
