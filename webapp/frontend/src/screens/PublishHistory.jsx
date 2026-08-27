import { useState, useEffect } from 'react'
import { Card, Chip, Button, Icon, Banner } from '../components.jsx'
import { api } from '../api.js'

// Published tab: the terminal publish-queue entries (published/skipped),
// split off the Schedule tab so the queue view stays small and fast.
// Fetched once when the tab opens — history only changes when something
// publishes, and reopening the tab refetches.
const SUB_CHIP = {
  done: ['ok', 'Published'], skipped: ['neutral', 'Skipped'], error: ['danger', 'Error'],
}

const fmtDate = (ts) => (ts
  ? new Date(ts * 1000).toLocaleString([], { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', hour12: false })
  : '')

export default function PublishHistory({ meta = {} }) {
  const chanName = (k) => (meta.config?.youtube_channels || []).find((c) => c.id === k)?.name || k
  const acctName = (k) => { const a = (meta.config?.x_accounts || []).find((c) => c.id === k); return a ? (a.name ? `@${a.name}` : a.id) : k }

  const [items, setItems] = useState([])
  const [error, setError] = useState('')
  const [loaded, setLoaded] = useState(false)
  const [busy, setBusy] = useState('')

  const refresh = () => api.publishHistory()
    .then((d) => { setItems(d.items || []); setLoaded(true) })
    .catch((e) => { setError(e.message); setLoaded(true) })
  useEffect(() => { refresh() }, [])

  const remove = async (id) => {
    setBusy(id); setError('')
    try { await api.publishRemove(id); await refresh() }
    catch (e) { setError(e.message) }
    finally { setBusy('') }
  }

  const platformRow = (e, plat) => {
    const sub = e[plat] || {}
    if (sub.status === 'skipped' && !sub.enabled) return null   // platform not targeted
    const [tone, label] = SUB_CHIP[sub.status] || ['neutral', sub.status]
    const key = plat === 'youtube' ? sub.channel : sub.account
    const target = key ? (plat === 'youtube' ? chanName(key) : acctName(key)) : '—'
    const when = fmtDate(sub.published_at)
    return (
      <div className="row center gap-10" style={{ fontSize: 12.5, flexWrap: 'wrap' }}>
        <Icon name={plat === 'youtube' ? 'youtube' : 'x-twitter'} brand style={{ width: 16, color: 'var(--ink-3)' }} />
        <span className="muted" style={{ minWidth: 90 }}>{target}</span>
        <Chip tone={tone}>{label}</Chip>
        {when && <span className="muted">{when}</span>}
        {sub.url && <a href={sub.url} target="_blank" rel="noopener" className="muted">view ↗</a>}
        {sub.error && <span style={{ color: 'var(--danger)' }}>{sub.error}</span>}
      </div>
    )
  }

  return (
    <div>
      <Banner tone="danger">{error}</Banner>
      <div className="bento">
        <Card span={12} className="reveal reveal-d1" style={{ padding: 0, overflow: 'hidden' }}>
          <div className="qrow-head" style={{ padding: '16px 22px', borderBottom: '1px solid var(--line)' }}>
            <span className="label-sm">Published</span>
            <span className="muted" style={{ fontSize: 12.5, marginLeft: 10 }}>
              Everything already released (or skipped). Removing an entry only forgets the record — it never takes an upload down.
            </span>
          </div>
          {loaded && items.length === 0 && (
            <div className="muted" style={{ fontSize: 13, padding: '18px 22px' }}>Nothing published yet.</div>
          )}
          {items.map((e, idx) => (
            <div key={e.id} className="row center qrow" style={{ gap: 14, padding: '14px 22px', borderBottom: idx < items.length - 1 ? '1px solid var(--line)' : 'none' }}>
              <div className="grow">
                <div className="row center gap-10" style={{ flexWrap: 'wrap' }}>
                  <span style={{ fontWeight: 600, letterSpacing: '-0.01em' }}>{e.title || '(untitled)'}</span>
                  {e.source && <Chip tone="info">{e.source}</Chip>}
                </div>
                <div className="stack gap-8 mt-8">
                  {platformRow(e, 'youtube')}
                  {platformRow(e, 'x')}
                </div>
              </div>
              <div className="row gap-10 row--wrap qrow__actions" style={{ justifyContent: 'flex-end' }}>
                <Button variant="ghost" icon="trash" disabled={!!busy} onClick={() => remove(e.id)}>
                  {busy === e.id ? 'Removing…' : 'Remove'}
                </Button>
              </div>
            </div>
          ))}
        </Card>
      </div>
    </div>
  )
}
