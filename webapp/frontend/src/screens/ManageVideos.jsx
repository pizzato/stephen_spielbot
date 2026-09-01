import { useState, useEffect, useRef } from 'react'
import { Card, Chip, Button, Banner } from '../components.jsx'
import { api } from '../api.js'
import {
  analyticsCache, annotateSlopScores, dislikePct,
  fmtNum, fmtDate, fmtDuration, fmtWatchTime, fmtPct,
} from './Analytics.jsx'

// "Manage Videos" tab on Channel Analytics: the same published-video catalogue
// as the Analytics tab (same metrics, same Slop Score), but with checkboxes and
// bulk actions — change visibility, add to a playlist, or delete (with
// confirmation). A Columns picker toggles every metric column the Analytics
// table has; the choice persists in localStorage. YouTube only: the X API
// offers no equivalent bulk endpoints.

const PRIVACY_TONES = { public: 'ok', unlisted: 'warn', private: 'neutral' }

// Every metric column from the Analytics table, renderable individually so the
// picker can toggle them. `val` overrides the sort value (derived metrics).
const dim = { color: 'var(--ink-3)' }
const METRIC_COLS = [
  { id: 'duration', label: 'Duration', render: (v) => v.duration || '—', style: () => ({ ...dim, whiteSpace: 'nowrap' }) },
  { id: 'view_count', label: 'Views', key: 'view_count', render: (v) => fmtNum(v.view_count), style: () => ({ fontWeight: 600 }) },
  { id: 'watch_time_minutes', label: 'Watch time', key: 'watch_time_minutes', render: (v) => fmtWatchTime(v.watch_time_minutes), style: () => ({ ...dim, whiteSpace: 'nowrap' }) },
  { id: 'avg_view_duration_secs', label: 'Avg duration', key: 'avg_view_duration_secs', render: (v) => fmtDuration(v.avg_view_duration_secs), style: () => ({ ...dim, whiteSpace: 'nowrap' }) },
  { id: 'avg_view_pct', label: 'Retention', key: 'avg_view_pct', render: (v) => v.avg_view_pct != null ? `${v.avg_view_pct}%` : '—', style: () => dim },
  { id: 'impressions', label: 'Impressions', key: 'impressions', render: (v) => fmtNum(v.impressions), style: () => dim },
  { id: 'ctr', label: 'CTR', key: 'ctr', render: (v) => v.ctr != null ? fmtPct(v.ctr) : '—', style: () => dim },
  { id: 'slop_score', label: 'Slop Score', key: 'slop_score', render: (v) => v.slop_score != null ? v.slop_score : '—',
    style: (v) => ({ fontWeight: 600, color: v.slop_score ? 'var(--danger)' : 'var(--ink-3)' }) },
  { id: 'like_count', label: 'Likes', key: 'like_count', render: (v) => fmtNum(v.like_count), style: () => dim },
  { id: 'dislike_count', label: 'Dislikes', key: 'dislike_count', render: (v) => fmtNum(v.dislike_count),
    style: (v) => ({ color: v.dislike_count ? 'var(--danger)' : 'var(--ink-3)' }) },
  { id: 'dislike_pct', label: 'Dislike %', key: 'dislike_pct', val: dislikePct,
    render: (v) => { const p = dislikePct(v); return p != null ? fmtPct(p) : '—' },
    style: (v) => ({ color: dislikePct(v) ? 'var(--danger)' : 'var(--ink-3)' }) },
  { id: 'comment_count', label: 'Comments', key: 'comment_count', render: (v) => fmtNum(v.comment_count), style: () => dim },
  { id: 'negative_comment_count', label: 'Negative', key: 'negative_comment_count', render: (v) => fmtNum(v.negative_comment_count),
    style: (v) => ({ color: v.negative_comment_count ? 'var(--danger)' : 'var(--ink-3)' }) },
  { id: 'published_at', label: 'Published', key: 'published_at', render: (v) => fmtDate(v.published_at), style: () => ({ ...dim, whiteSpace: 'nowrap' }) },
]
const DEFAULT_COLS = ['duration', 'view_count', 'slop_score', 'like_count', 'comment_count', 'published_at']
const COLS_STORE_KEY = 'manage_videos_cols'

function loadCols() {
  try {
    const saved = JSON.parse(localStorage.getItem(COLS_STORE_KEY))
    if (Array.isArray(saved)) return saved.filter((id) => METRIC_COLS.some((c) => c.id === id))
  } catch { /* fall through */ }
  return DEFAULT_COLS
}

function ColumnPicker({ visible, onChange }) {
  const [open, setOpen] = useState(false)
  const ref = useRef(null)
  useEffect(() => {
    if (!open) return
    const close = (e) => { if (!ref.current?.contains(e.target)) setOpen(false) }
    document.addEventListener('mousedown', close)
    return () => document.removeEventListener('mousedown', close)
  }, [open])
  return (
    <div ref={ref} style={{ position: 'relative', marginLeft: 'auto' }}>
      <Button size="sm" icon="table-columns" onClick={() => setOpen((o) => !o)}>Columns</Button>
      {open && (
        <div className="card" style={{ position: 'absolute', right: 0, top: '100%', marginTop: 6, zIndex: 100,
                                       padding: 12, minWidth: 180, boxShadow: '0 8px 30px rgba(0,0,0,.16)' }}>
          {METRIC_COLS.map((c) => (
            <label key={c.id} className="row center" style={{ gap: 8, fontSize: 13, padding: '3px 0', cursor: 'pointer' }}>
              <input type="checkbox" checked={visible.includes(c.id)}
                onChange={(e) => onChange(e.target.checked ? [...visible, c.id] : visible.filter((id) => id !== c.id))} />
              {c.label}
            </label>
          ))}
        </div>
      )}
    </div>
  )
}

function sortVideos(videos, sort, cols) {
  if (!sort.key) return videos
  const col = cols.find((c) => c.key === sort.key)
  const val = (v) => {
    const x = col?.val ? col.val(v) : v[sort.key]
    return x == null ? -1 : x
  }
  const dir = sort.dir === 'asc' ? 1 : -1
  return [...videos].sort((a, b) => { const av = val(a), bv = val(b); return (av < bv ? -1 : av > bv ? 1 : 0) * dir })
}

function DeleteConfirm({ videos, busy, onConfirm, onClose }) {
  const shown = videos.slice(0, 8)
  return (
    <div style={{ position: 'fixed', inset: 0, zIndex: 1100, background: 'rgba(0,0,0,.82)', display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 24 }}
      onClick={busy ? undefined : onClose}>
      <div className="card" style={{ maxWidth: 460, width: '100%', padding: 24 }} onClick={(e) => e.stopPropagation()}>
        <div style={{ fontWeight: 700, fontSize: 16, marginBottom: 8 }}>Delete {videos.length} video{videos.length === 1 ? '' : 's'} from YouTube?</div>
        <p style={{ fontSize: 13, color: 'var(--danger)', marginBottom: 12 }}>
          This permanently deletes the video{videos.length === 1 ? '' : 's'} — views, likes and comments included. It cannot be undone.
        </p>
        <ul style={{ fontSize: 13, margin: '0 0 16px 18px', color: 'var(--ink-3)' }}>
          {shown.map((v) => <li key={v.video_id} style={{ marginBottom: 2 }}>{v.title}</li>)}
          {videos.length > shown.length && <li>…and {videos.length - shown.length} more</li>}
        </ul>
        <div className="row" style={{ gap: 10, justifyContent: 'flex-end' }}>
          <Button onClick={onClose} disabled={busy}>Cancel</Button>
          <Button variant="danger" icon="trash" disabled={busy} onClick={onConfirm}>
            {busy ? 'Deleting…' : `Delete ${videos.length} video${videos.length === 1 ? '' : 's'}`}
          </Button>
        </div>
      </div>
    </div>
  )
}

export default function ManageVideos() {
  const [channels, setChannels] = useState([])
  const [channel, setChannel] = useState('')
  const [videos, setVideos] = useState(null)      // null = loading
  const [selected, setSelected] = useState(new Set())
  const [sort, setSort] = useState({ key: 'published_at', dir: 'desc' })
  const [visibleCols, setVisibleCols] = useState(loadCols)
  const [playlists, setPlaylists] = useState([])
  const [privacy, setPrivacy] = useState('private')
  const [playlistId, setPlaylistId] = useState('')
  const [busy, setBusy] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const [notice, setNotice] = useState(null)      // { tone, text }

  useEffect(() => {
    try { localStorage.setItem(COLS_STORE_KEY, JSON.stringify(visibleCols)) } catch { /* private mode */ }
  }, [visibleCols])

  useEffect(() => {
    api.ytChannels().then((r) => {
      const list = r.channels || []
      setChannels(list); setChannel(list[0]?.id || '')
    }).catch(() => setChannels([]))
  }, [])

  useEffect(() => {
    if (channel === '' && channels.length) return
    setVideos(null); setSelected(new Set()); setNotice(null)
    const cached = analyticsCache.youtube[channel]
    const load = cached ? Promise.resolve(cached) : api.ytAnalytics(channel, false)
    load.then((d) => {
      if (!cached) analyticsCache.youtube[channel] = d
      setVideos(annotateSlopScores(d.videos || []))
    }).catch((e) => { setVideos([]); setNotice({ tone: 'danger', text: e.message }) })
    api.ytPlaylists(channel).then((r) => {
      const pls = r.playlists || []
      setPlaylists(pls); setPlaylistId(pls[0]?.id || '')
    }).catch(() => setPlaylists([]))
  }, [channel, channels])

  const toggle = (id) => setSelected((s) => {
    const next = new Set(s)
    next.has(id) ? next.delete(id) : next.add(id)
    return next
  })
  const metricCols = METRIC_COLS.filter((c) => visibleCols.includes(c.id))
  const shown = sortVideos(videos || [], sort, metricCols)
  const allSelected = shown.length > 0 && shown.every((v) => selected.has(v.video_id))
  const toggleAll = () => setSelected(allSelected ? new Set() : new Set(shown.map((v) => v.video_id)))

  const runBulk = async (action) => {
    const ids = [...selected]
    setBusy(true); setNotice(null)
    try {
      const body = { channel, action, video_ids: ids, privacy, playlist_id: playlistId }
      const res = await api.ytVideosBulk(body)
      const okIds = new Set(res.results.filter((r) => r.success).map((r) => r.video_id))
      // Reflect the change locally and drop the cached snapshot — the backend
      // patched its persisted copy, so the Analytics tab refetches it cheaply.
      delete analyticsCache.youtube[channel]
      if (action === 'delete') {
        setVideos((vs) => vs.filter((v) => !okIds.has(v.video_id)))
      } else if (action === 'privacy') {
        setVideos((vs) => vs.map((v) => okIds.has(v.video_id) ? { ...v, privacy } : v))
      }
      setSelected(new Set())
      const verb = action === 'delete' ? 'Deleted' : action === 'privacy' ? `Set to ${privacy}` : 'Added to playlist'
      if (res.failed > 0) {
        const firstErr = res.results.find((r) => !r.success)?.error || ''
        setNotice({ tone: 'danger', text: `${verb}: ${res.succeeded} succeeded, ${res.failed} failed. ${firstErr}` })
      } else {
        setNotice({ tone: 'ok', text: `${verb}: ${res.succeeded} video${res.succeeded === 1 ? '' : 's'}.` })
      }
    } catch (e) {
      setNotice({ tone: 'danger', text: e.message })
    } finally {
      setBusy(false); setConfirming(false)
    }
  }

  const none = selected.size === 0
  return (
    <div>
      <div className="row center reveal reveal-d1" style={{ justifyContent: 'space-between', marginBottom: 16 }}>
        <Chip tone="neutral">YouTube</Chip>
        {channels.length > 1 && (
          <select className="select" value={channel} onChange={(e) => setChannel(e.target.value)} style={{ maxWidth: 220 }}>
            {channels.map((c) => <option key={c.id} value={c.id}>{c.name || c.id}</option>)}
          </select>
        )}
        {channels.length === 1 && <Chip tone="ok" dot>{channels[0].name || channels[0].id}</Chip>}
      </div>

      {notice && <Banner tone={notice.tone === 'ok' ? 'ok' : 'danger'}>{notice.text}</Banner>}

      <div className="bento">
        <Card span={12} className="reveal reveal-d1" style={{ minWidth: 0 }}>
          <div className="row center" style={{ gap: 12, flexWrap: 'wrap', marginBottom: 14 }}>
            <Chip tone={none ? 'neutral' : 'ok'}>{selected.size} selected</Chip>
            <div className="row center" style={{ gap: 6 }}>
              <select className="select" value={privacy} onChange={(e) => setPrivacy(e.target.value)} disabled={busy} style={{ width: 110 }}>
                {['private', 'unlisted', 'public'].map((p) => <option key={p} value={p}>{p}</option>)}
              </select>
              <Button size="sm" icon="eye" disabled={none || busy} onClick={() => runBulk('privacy')}>Set visibility</Button>
            </div>
            <div className="row center" style={{ gap: 6 }}>
              <select className="select" value={playlistId} onChange={(e) => setPlaylistId(e.target.value)} disabled={busy || !playlists.length} style={{ maxWidth: 200 }}>
                {playlists.length
                  ? playlists.map((p) => <option key={p.id} value={p.id}>{p.title}</option>)
                  : <option value="">No playlists</option>}
              </select>
              <Button size="sm" icon="list" disabled={none || busy || !playlistId} onClick={() => runBulk('playlist')}>Add to playlist</Button>
            </div>
            <Button size="sm" variant="danger" icon="trash" disabled={none || busy} onClick={() => setConfirming(true)}>Delete…</Button>
            <ColumnPicker visible={visibleCols} onChange={setVisibleCols} />
          </div>

          {videos === null && <p className="muted" style={{ fontSize: 13 }}>Loading videos…</p>}
          {videos !== null && videos.length === 0 && <p className="muted" style={{ fontSize: 13 }}>No videos found. Connect YouTube to manage your uploads.</p>}
          {videos !== null && videos.length > 0 && (
            <div style={{ overflowX: 'auto' }}>
              <table style={{ width: '100%', minWidth: 560 + metricCols.length * 90, borderCollapse: 'collapse', fontSize: 13 }}>
                <thead><tr style={{ borderBottom: '1px solid var(--line)' }}>
                  <th style={{ textAlign: 'left', padding: '6px 10px' }}>
                    <input type="checkbox" checked={allSelected} onChange={toggleAll} disabled={busy} />
                  </th>
                  <th />
                  {[{ label: 'Title', key: 'title' }, { label: 'Visibility', key: 'privacy' }, ...metricCols].map((c) => (
                    <th key={c.label}
                      onClick={c.key ? () => setSort((s) => ({ key: c.key, dir: s.key === c.key && s.dir === 'desc' ? 'asc' : 'desc' })) : undefined}
                      style={{ textAlign: c.label === 'Title' || c.label === 'Visibility' ? 'left' : 'right', padding: '6px 10px', fontWeight: 600,
                               color: sort.key === c.key ? 'var(--ink)' : 'var(--ink-3)', whiteSpace: 'nowrap',
                               cursor: c.key ? 'pointer' : 'default', userSelect: 'none' }}>
                      {c.label}{sort.key === c.key ? (sort.dir === 'desc' ? ' ↓' : ' ↑') : ''}
                    </th>
                  ))}
                </tr></thead>
                <tbody>
                  {shown.map((v) => (
                    <tr key={v.video_id} style={{ borderBottom: '1px solid var(--line)', background: selected.has(v.video_id) ? 'var(--paper-2)' : undefined, cursor: 'pointer' }}
                      onClick={() => !busy && toggle(v.video_id)}>
                      <td style={{ padding: '8px 10px' }}>
                        <input type="checkbox" checked={selected.has(v.video_id)} disabled={busy}
                          onChange={() => toggle(v.video_id)} onClick={(e) => e.stopPropagation()} />
                      </td>
                      <td style={{ padding: '8px 10px 8px 0' }}>{v.thumbnail_url ? <img src={v.thumbnail_url} alt="" style={{ width: 48, height: 27, objectFit: 'cover', borderRadius: 4, display: 'block' }} /> : <div style={{ width: 48, height: 27, background: 'var(--surface-2)', borderRadius: 4 }} />}</td>
                      <td style={{ padding: '8px 10px', maxWidth: 320, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={v.title}>
                        <a href={`https://www.youtube.com/watch?v=${v.video_id}`} target="_blank" rel="noreferrer" onClick={(e) => e.stopPropagation()} style={{ color: 'inherit', textDecoration: 'none', fontWeight: 500 }}>{v.title}</a>
                      </td>
                      <td style={{ padding: '8px 10px' }}><Chip tone={PRIVACY_TONES[v.privacy] || 'neutral'}>{v.privacy || '—'}</Chip></td>
                      {metricCols.map((c) => (
                        <td key={c.id} style={{ textAlign: 'right', padding: '8px 10px', ...c.style(v) }}>{c.render(v)}</td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      </div>

      {confirming && (
        <DeleteConfirm
          videos={shown.filter((v) => selected.has(v.video_id))}
          busy={busy}
          onConfirm={() => runBulk('delete')}
          onClose={() => setConfirming(false)}
        />
      )}
    </div>
  )
}
