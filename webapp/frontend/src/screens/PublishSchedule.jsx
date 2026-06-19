import { useState, useEffect } from 'react'
import { Card, Chip, Button, Icon, Banner } from '../components.jsx'
import { api } from '../api.js'

// Per-platform sub-state → [chip tone, label].
const SUB_CHIP = {
  pending: ['accent', 'Queued'], publishing: ['info', 'Publishing…'],
  done: ['ok', 'Published'], skipped: ['neutral', 'Skipped'], error: ['danger', 'Error'],
}

// Relative time until a future epoch-seconds timestamp ("in 1h 20m" / "now").
function fmtWhen(ts, now) {
  if (!ts) return ''
  const d = ts - now
  if (d <= 30) return 'now'
  const m = Math.round(d / 60)
  if (m < 60) return `in ${m}m`
  const h = Math.floor(m / 60), rem = m % 60
  if (h < 24) return rem ? `in ${h}h ${rem}m` : `in ${h}h`
  return `in ${Math.round(h / 24)}d`
}

export default function PublishSchedule({ go, meta = {} }) {
  // Resolve a channel/account KEY to its friendly name (the rest of the app
  // shows names, not raw UC.../numeric ids).
  const chanName = (k) => (meta.config?.youtube_channels || []).find((c) => c.id === k)?.name || k
  const acctName = (k) => { const a = (meta.config?.x_accounts || []).find((c) => c.id === k); return a ? (a.name ? `@${a.name}` : a.id) : k }

  const [data, setData] = useState({ items: [], channels: {}, accounts: {}, enabled: false, skip_comment: true, now: 0 })
  const [error, setError] = useState('')
  const [status, setStatus] = useState('')
  const [busy, setBusy] = useState('')
  const [loaded, setLoaded] = useState(false)

  const refresh = () => api.publishQueue()
    .then((d) => { setData(d); setLoaded(true) })
    .catch((e) => { setError(e.message); setLoaded(true) })
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
    const next = sub.status === 'pending' ? fmtWhen(cad.next_eligible, now) : ''
    return (
      <div className="row center gap-10" style={{ fontSize: 12.5, flexWrap: 'wrap' }}>
        <Icon name={icon} brand style={{ width: 16, color: 'var(--ink-3)' }} />
        <span className="muted" style={{ minWidth: 90 }}>{target}</span>
        <Chip tone={tone}>{label}</Chip>
        {next && <span className="muted">{next}{cad.count_today ? ` · ${cad.count_today} today` : ''}</span>}
        {sub.url && <a href={sub.url} target="_blank" rel="noopener" className="muted">view ↗</a>}
        {sub.error && <span style={{ color: 'var(--danger)' }}>{sub.error}</span>}
      </div>
    )
  }

  const entryRow = (e, idx, list) => {
    const pendingAny = e.youtube?.status === 'pending' || e.x?.status === 'pending'
    return (
      <div key={e.id} className="row center" style={{ gap: 14, padding: '14px 22px', borderBottom: idx < list.length - 1 ? '1px solid var(--line)' : 'none' }}>
        <div className="grow">
          <div className="row center gap-10" style={{ flexWrap: 'wrap' }}>
            <span style={{ fontWeight: 600, letterSpacing: '-0.01em' }}>{e.title || '(untitled)'}</span>
            {e.source && <Chip tone="info">{e.source}</Chip>}
            {data.skip_comment && e.source === 'comment' && pendingAny && <Chip tone="warn">bypasses schedule</Chip>}
          </div>
          <div className="stack gap-8 mt-8">
            {platformRow(e, 'youtube', data.channels || {})}
            {platformRow(e, 'x', data.accounts || {})}
          </div>
        </div>
        <div className="row gap-10 row--wrap" style={{ justifyContent: 'flex-end' }}>
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

  const section = (title, hint, list, empty) => (
    <Card span={12} className="reveal reveal-d2" style={{ padding: 0, overflow: 'hidden' }}>
      <div className="row center between gap-10 row--wrap" style={{ padding: '16px 22px', borderBottom: '1px solid var(--line)' }}>
        <div>
          <span className="label-sm">{title}</span>
          {hint ? <span className="muted" style={{ fontSize: 12.5, marginLeft: 10 }}>{hint}</span> : null}
        </div>
      </div>
      {loaded && list.length === 0 && <div className="muted" style={{ fontSize: 13, padding: '18px 22px' }}>{empty}</div>}
      {list.map((e, idx) => entryRow(e, idx, list))}
    </Card>
  )

  return (
    <div>
      <div className="page-head">
        <div className="page-head__intro">
          <span className="label-sm reveal">Schedule</span>
          <h1 className="display-md reveal reveal-d1">Publishing schedule</h1>
        </div>
        <Button variant="ghost" icon="gear" onClick={() => go('settings')}>Cadence settings</Button>
      </div>

      <Banner tone="danger">{error}</Banner>
      {status && <Banner tone="ok">{status}</Banner>}
      {loaded && !data.enabled && (
        <Banner tone="info">
          Scheduled publishing is off — finished videos still post immediately. Turn it on in
          Settings → Publishing to release them on a cadence instead. You can still scan and review the queue here.
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

        {section('Waiting & in progress', 'Released oldest-first as each cadence allows', activeItems,
          'Nothing waiting. New finished videos appear here automatically; “Scan for unpublished” pulls in the backlog.')}
        {doneItems.length > 0 && section('History', 'Published, skipped or errored', doneItems, '')}
      </div>
    </div>
  )
}
