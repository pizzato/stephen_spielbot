import { useState, useEffect } from 'react'
import { Card, Chip, Button, Icon, Banner } from '../components.jsx'
import { api } from '../api.js'

// Approvals tab: publish-queue entries held for the publish_require_approval
// gate, split off the Schedule tab so that view carries only videos actually
// scheduled to release. Approving moves the film to the Schedule tab, where it
// releases on its channel/account cadence. Polls like the Schedule tab — an
// approval elsewhere (the Films tab has the same gate) should reflect here.
export default function PublishApprovals({ meta = {} }) {
  const chanName = (k) => (meta.config?.youtube_channels || []).find((c) => c.id === k)?.name || k
  const acctName = (k) => { const a = (meta.config?.x_accounts || []).find((c) => c.id === k); return a ? (a.name ? `@${a.name}` : a.id) : k }

  const [items, setItems] = useState([])
  const [error, setError] = useState('')
  const [status, setStatus] = useState('')
  const [loaded, setLoaded] = useState(false)
  const [busy, setBusy] = useState('')

  const refresh = () => api.publishQueue()
    .then((d) => { setItems((d.items || []).filter((e) => e.awaiting_approval)); setLoaded(true) })
    .catch((e) => { setError(e.message); setLoaded(true) })
  useEffect(() => {
    refresh()
    const t = setInterval(refresh, 5000)
    return () => clearInterval(t)
  }, [])

  const run = async (key, fn, after) => {
    setBusy(key); setError(''); setStatus('')
    try { await fn(); await refresh(); if (after) after() }
    catch (e) { setError(e.message) }
    finally { setBusy('') }
  }

  const platformRow = (e, plat) => {
    const sub = e[plat] || {}
    if (sub.status === 'skipped' && !sub.enabled) return null   // platform not targeted
    const key = plat === 'youtube' ? sub.channel : sub.account
    const target = key ? (plat === 'youtube' ? chanName(key) : acctName(key)) : (plat === 'youtube' ? 'default channel' : '—')
    return (
      <div className="row center gap-10" style={{ fontSize: 12.5, flexWrap: 'wrap' }}>
        <Icon name={plat === 'youtube' ? 'youtube' : 'x-twitter'} brand style={{ width: 16, color: 'var(--ink-3)' }} />
        <span className="muted" style={{ minWidth: 90 }}>{target}</span>
        <Chip tone="warn">Held</Chip>
      </div>
    )
  }

  return (
    <div>
      <Banner tone="danger">{error}</Banner>
      {status && <Banner tone="ok">{status}</Banner>}
      <div className="bento">
        <Card span={12} className="reveal reveal-d1" style={{ padding: 0, overflow: 'hidden' }}>
          <div className="qrow-head" style={{ padding: '16px 22px', borderBottom: '1px solid var(--line)' }}>
            <span className="label-sm">Waiting for approval</span>
            <span className="muted" style={{ fontSize: 12.5, marginLeft: 10 }}>
              Held until you approve them — an approved film moves to the Schedule tab and releases on its cadence.
            </span>
          </div>
          {loaded && items.length === 0 && (
            <div className="muted" style={{ fontSize: 13, padding: '18px 22px' }}>Nothing waiting for approval.</div>
          )}
          {items.map((e, idx) => (
            <div key={e.id} className="row center qrow" style={{ gap: 14, padding: '14px 22px', borderBottom: idx < items.length - 1 ? '1px solid var(--line)' : 'none' }}>
              <div className="grow">
                <div className="row center gap-10" style={{ flexWrap: 'wrap' }}>
                  <span style={{ fontWeight: 600, letterSpacing: '-0.01em' }}>{e.title || '(untitled)'}</span>
                  {e.source && <Chip tone="info">{e.source}</Chip>}
                  {e.interestingness != null && <span style={{ color: 'var(--warm)', fontWeight: 600, fontSize: 13 }}><Icon name="star" style={{ fontSize: 11 }} /> {Number(e.interestingness).toFixed(1)}</span>}
                </div>
                <div className="stack gap-8 mt-8">
                  {platformRow(e, 'youtube')}
                  {platformRow(e, 'x')}
                </div>
              </div>
              <div className="row gap-10 row--wrap qrow__actions" style={{ justifyContent: 'flex-end' }}>
                <Button variant="primary" icon="check" disabled={!!busy}
                  onClick={() => run('ap' + e.id, () => api.publishApprove(e.work_dir, true), () => setStatus('Approved — will publish on cadence.'))}>
                  {busy === 'ap' + e.id ? 'Approving…' : 'Approve'}
                </Button>
                <Button variant="ghost" icon="trash" disabled={!!busy}
                  onClick={() => run('rm' + e.id, () => api.publishRemove(e.id))}>
                  {busy === 'rm' + e.id ? 'Removing…' : 'Remove'}
                </Button>
              </div>
            </div>
          ))}
        </Card>
      </div>
    </div>
  )
}
