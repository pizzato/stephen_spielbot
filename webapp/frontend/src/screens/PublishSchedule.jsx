import { useState, useEffect } from 'react'
import { Card, Chip, Button, Icon, Banner } from '../components.jsx'
import { api } from '../api.js'

// Per-platform sub-state → [chip tone, label].
const SUB_CHIP = {
  pending: ['accent', 'Queued'], publishing: ['info', 'Publishing…'],
  done: ['ok', 'Published'], skipped: ['neutral', 'Skipped'], error: ['danger', 'Error'],
}

// Clock time a future epoch-seconds timestamp falls on ("22:10", "tomorrow
// 09:00", "Sat 09:00", "24 Jun 09:00") — when the video will actually publish,
// not how long until then. 24-hour, in the viewer's local zone.
function fmtClock(ts, now) {
  if (!ts) return ''
  if (ts - now <= 30) return 'now'
  const d = new Date(ts * 1000)
  const hm = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false })
  const sod = (t) => { const x = new Date(t * 1000); x.setHours(0, 0, 0, 0); return x.getTime() }
  const days = Math.round((sod(ts) - sod(now)) / 86400000)
  if (days <= 0) return hm
  if (days === 1) return `tomorrow ${hm}`
  if (days < 7) return `${d.toLocaleDateString([], { weekday: 'short' })} ${hm}`
  return `${d.toLocaleDateString([], { day: 'numeric', month: 'short' })} ${hm}`
}

function fmtNum(n) {
  if (n == null) return '—'
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1).replace(/\.0$/, '') + 'M'
  if (n >= 1_000) return (n / 1_000).toFixed(1).replace(/\.0$/, '') + 'K'
  return String(Math.round(n))
}

// Sorts for the publish queue. The picked sort is saved to config
// (publish_sort_order) and is the real release order: the scheduler publishes
// the top waiting item for each channel first. "Manual order" is hand-ordered
// with the arrows.
const SORT_OPTIONS = [
  { value: 'queue', label: 'Manual order' },
  { value: 'newest', label: 'Newest' },
  { value: 'oldest', label: 'Oldest' },
  { value: 'interest', label: 'Interestingness' },
  { value: 'views', label: 'Predicted views' },
]
const validSort = (v) => (SORT_OPTIONS.some((o) => o.value === v) ? v : 'queue')

export default function PublishSchedule({ go, meta = {} }) {
  // Resolve a channel/account KEY to its friendly name (the rest of the app
  // shows names, not raw UC.../numeric ids).
  const chanName = (k) => (meta.config?.youtube_channels || []).find((c) => c.id === k)?.name || k
  const acctName = (k) => { const a = (meta.config?.x_accounts || []).find((c) => c.id === k); return a ? (a.name ? `@${a.name}` : a.id) : k }

  const [data, setData] = useState({ items: [], channels: {}, accounts: {}, enabled: false, skip_comment: true, sort: 'queue', now: 0 })
  const [error, setError] = useState('')
  const [status, setStatus] = useState('')
  const [busy, setBusy] = useState('')
  const [loaded, setLoaded] = useState(false)
  const [sortBy, setSortBy] = useState(() => validSort(meta.config?.publish_sort_order))

  const refresh = () => api.publishQueue()
    .then((d) => { setData(d); setLoaded(true); if (d.sort) setSortBy(validSort(d.sort)) })
    .catch((e) => { setError(e.message); setLoaded(true) })

  // The saved sort is authoritative; mirror the render Queue — persist it so the
  // scheduler releases in this order too, then refresh to get the re-sorted list.
  const changeSort = (v) => {
    setSortBy(v)
    api.saveConfig({ publish_sort_order: v }).then(refresh).catch((e) => setError(e.message))
  }
  // Poll so releases/completions surface without a manual reload.
  useEffect(() => {
    refresh()
    const t = setInterval(refresh, 5000)
    return () => clearInterval(t)
  }, [])

  const run = async (key, fn, after) => {
    setBusy(key); setError(''); setStatus('')
    try { const r = await fn(); await refresh(); if (after) after(r) }
    catch (e) { setError(e.message) }
    finally { setBusy('') }
  }

  const items = data.items || []
  const now = data.now || Date.now() / 1000
  const isActive = (s) => s === 'pending' || s === 'publishing'
  const activeItems = items.filter((e) => isActive(e.youtube?.status) || isActive(e.x?.status))
  const doneItems = items.filter((e) => !isActive(e.youtube?.status) && !isActive(e.x?.status))
  const counts = {
    queued: items.filter((e) => e.youtube?.status === 'pending' || e.x?.status === 'pending').length,
    publishing: items.filter((e) => e.youtube?.status === 'publishing' || e.x?.status === 'publishing').length,
    done: items.filter((e) => e.youtube?.status === 'done' || e.x?.status === 'done').length,
  }

  // One line per platform target: icon, channel/account, status, and (while
  // queued) when its cadence next allows a release.
  const platformRow = (e, plat, summary) => {
    const sub = e[plat] || {}
    if (sub.status === 'skipped' && !sub.enabled) return null   // platform not targeted
    const [tone, label] = SUB_CHIP[sub.status] || ['neutral', sub.status]
    const key = plat === 'youtube' ? sub.channel : sub.account
    const cad = summary[key] || {}
    const icon = plat === 'youtube' ? 'youtube' : 'x-twitter'
    const target = key ? (plat === 'youtube' ? chanName(key) : acctName(key)) : (plat === 'youtube' ? 'default channel' : '—')
    const when = sub.status === 'pending' ? fmtClock(sub.projected_at, now) : ''
    return (
      <div className="row center gap-10" style={{ fontSize: 12.5, flexWrap: 'wrap' }}>
        <Icon name={icon} brand style={{ width: 16, color: 'var(--ink-3)' }} />
        <span className="muted" style={{ minWidth: 90 }}>{target}</span>
        <Chip tone={tone}>{label}</Chip>
        {when && <span className="muted"><Icon name="clock" style={{ fontSize: 10 }} /> {when}{sub.position > 1 ? ` · #${sub.position} in line` : ''}{cad.count_today ? ` · ${cad.count_today} today` : ''}</span>}
        {sub.url && <a href={sub.url} target="_blank" rel="noopener" className="muted">view ↗</a>}
        {sub.error && <span style={{ color: 'var(--danger)' }}>{sub.error}</span>}
      </div>
    )
  }

  const entryRow = (e, idx, list, noMove) => {
    const pendingAny = e.youtube?.status === 'pending' || e.x?.status === 'pending'
    return (
      <div key={e.id} className="row center qrow" style={{ gap: 14, padding: '14px 22px', borderBottom: idx < list.length - 1 ? '1px solid var(--line)' : 'none' }}>
        <div className="stack" style={{ gap: 2 }} title={noMove ? 'Switch to Manual order to reorder' : undefined}>
          <button className="qmove" disabled={!pendingAny || !!busy || noMove} onClick={() => run('mu' + e.id, () => api.publishMove(e.id, -1))}><Icon name="chevron-up" /></button>
          <button className="qmove" disabled={!pendingAny || !!busy || noMove} onClick={() => run('md' + e.id, () => api.publishMove(e.id, 1))}><Icon name="chevron-down" /></button>
        </div>
        <div className="grow">
          <div className="row center gap-10" style={{ flexWrap: 'wrap' }}>
            {pendingAny && e.is_next && <Chip tone="ok" dot>Next up</Chip>}
            <span style={{ fontWeight: 600, letterSpacing: '-0.01em' }}>{e.title || '(untitled)'}</span>
            {e.source && <Chip tone="info">{e.source}</Chip>}
            {data.skip_comment && e.source === 'comment' && pendingAny && <Chip tone="warn">bypasses schedule</Chip>}
            {e.interestingness != null && <span style={{ color: 'var(--warm)', fontWeight: 600, fontSize: 13 }}><Icon name="star" style={{ fontSize: 11 }} /> {Number(e.interestingness).toFixed(1)}</span>}
            {e.predicted_views != null && e.predicted_views >= 0 && <span title="Predicted reach"><Chip tone="accent"><Icon name="chart-line" style={{ fontSize: 10 }} /> ~{fmtNum(e.predicted_views)}</Chip></span>}
          </div>
          <div className="stack gap-8 mt-8">
            {platformRow(e, 'youtube', data.channels || {})}
            {platformRow(e, 'x', data.accounts || {})}
          </div>
        </div>
        <div className="row gap-10 row--wrap qrow__actions" style={{ justifyContent: 'flex-end' }}>
          {pendingAny && (
            <Button variant="ghost" icon="bolt" disabled={!!busy}
              onClick={() => run('now' + e.id, () => api.publishNow(e.id), () => setStatus('Releasing now…'))}>
              {busy === 'now' + e.id ? 'Releasing…' : 'Publish now'}
            </Button>
          )}
          <Button variant="ghost" icon="trash" disabled={!!busy}
            onClick={() => run('rm' + e.id, () => api.publishRemove(e.id))}>
            {busy === 'rm' + e.id ? 'Removing…' : 'Remove'}
          </Button>
        </div>
      </div>
    )
  }

  const section = (title, hint, list, empty, opts = {}) => (
    <Card span={12} className="reveal reveal-d2" style={{ padding: 0, overflow: 'hidden' }}>
      <div className="row center between gap-10 row--wrap qrow-head" style={{ padding: '16px 22px', borderBottom: '1px solid var(--line)' }}>
        <div>
          <span className="label-sm">{title}</span>
          {hint ? <span className="muted" style={{ fontSize: 12.5, marginLeft: 10 }}>{hint}</span> : null}
        </div>
        {opts.extra || null}
      </div>
      {loaded && list.length === 0 && <div className="muted" style={{ fontSize: 13, padding: '18px 22px' }}>{empty}</div>}
      {list.map((e, idx) => entryRow(e, idx, list, opts.noMove))}
    </Card>
  )

  const sortControl = (
    <label className="row center" style={{ gap: 8 }}>
      <span className="muted" style={{ fontSize: 12.5 }}><Icon name="arrow-down-wide-short" /> Sort</span>
      <select className="select" style={{ width: 'auto', padding: '6px 10px', fontSize: 13 }}
        value={sortBy} onChange={(e) => changeSort(e.target.value)}>
        {SORT_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
      </select>
    </label>
  )

  return (
    <div>
      <Banner tone="danger">{error}</Banner>
      {status && <Banner tone="ok">{status}</Banner>}
      {loaded && !data.enabled && (
        <Banner tone="info">
          Scheduled publishing is off — finished videos collect here and post the moment they finish if auto-post is on,
          otherwise they wait for you to publish them. Turn on scheduling in Settings → Publishing to release them on each
          channel/account's cadence instead.
        </Banner>
      )}

      <div className="bento">
        <Card span={12} well className="reveal reveal-d1">
          <div className="row center between row--wrap gap-16">
            <div className="row center gap-10">
              <span className="stream-ico" style={{ background: 'var(--accent-soft)', color: 'var(--accent)' }}><Icon name="clock" /></span>
              <div>
                <div style={{ fontWeight: 600 }}>Publish queue</div>
                <div className="muted" style={{ fontSize: 12.5 }}>
                  Finished, unpublished videos released on each channel/account's cadence.
                  {data.skip_comment ? ' Comment requests post immediately.' : ''}
                </div>
              </div>
            </div>
            <div className="row gap-10 row--wrap">
              <Button variant="ghost" icon="gear" onClick={() => go('settings')}>Cadence settings</Button>
              <Button variant="primary" icon="magnifying-glass" disabled={!!busy}
                onClick={() => run('scan', api.publishScan, (r) => setStatus(`Added ${r.added} video(s) to the publish queue.`))}>
                {busy === 'scan' ? 'Scanning…' : 'Scan for unpublished'}
              </Button>
            </div>
          </div>
        </Card>

        <Card span={4} className="reveal reveal-d1"><span className="label-sm">Queued</span><div className="metric mt-8">{counts.queued}</div><div className="muted" style={{ fontSize: 13 }}>waiting on cadence</div></Card>
        <Card span={4} className="reveal reveal-d2"><span className="label-sm">Publishing</span><div className="metric mt-8">{counts.publishing}</div><div className="muted" style={{ fontSize: 13 }}>uploading now</div></Card>
        <Card span={4} className="reveal reveal-d3"><span className="label-sm">Published</span><div className="metric mt-8">{counts.done}</div><div className="muted" style={{ fontSize: 13 }}>released</div></Card>

        {section('Waiting & in progress',
          sortBy === 'queue'
            ? 'Released top-first as each cadence allows. Reorder with the arrows.'
            : 'Released top-first as each cadence allows. Switch to Manual order to reorder by hand.',
          activeItems,
          'Nothing waiting. New finished videos appear here automatically; “Scan for unpublished” pulls in the backlog.',
          { extra: sortControl, noMove: sortBy !== 'queue' })}
        {doneItems.length > 0 && section('History', 'Published, skipped or errored', doneItems, '')}
      </div>
    </div>
  )
}
